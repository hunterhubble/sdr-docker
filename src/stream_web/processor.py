"""Processor — runs in a **separate OS process** to avoid GIL contention
with the real-time SDR RX thread.

Communicates with the main process via:
  * POSIX shared memory  — IQ circular buffer (read-only here)
  * multiprocessing.Value — buf_write_idx and control scalars
  * multiprocessing.Queue — results back to main, drop positions in

Time-domain plot rendering (the Signal Viewer's failure/device-id capture)
is handed off to a separate td_renderer process rather than done inline, for
the same GIL-contention reason -- see td_renderer.py.
"""

import queue
import time
from collections import deque
from multiprocessing import shared_memory

import numpy as np
from hubble_satnet_decoder import compute_spec_chunk, decode_signal, get_chipset_stats

from . import config
from .spectrogram import render_spec_image, render_spectrum_image
from .td_capture import TdCaptureState, select_td_capture
from .timing import correct_symbol_edges, edges_to_timing_stats


def processor_main(shm_name, buf_write_idx_val, rx_peak_frac_val,
                   rx_overflows_val, rx_gain_dB_val, td_running_val,
                   td_ntw_id_val, td_has_ntw_val, td_chipset_arr,
                   td_zoom_n_syms_val, view_mode_val, lo_freq_val,
                   running_event, drop_queue, result_queue, td_job_queue):
    """Entry point for the processor process."""

    shm = shared_memory.SharedMemory(name=shm_name, create=False)
    iq_buffer = np.ndarray(config.IQ_BUFFER_SIZE, dtype=np.complex64,
                           buffer=shm.buf)

    spec_chunks: deque = deque(maxlen=config.MAX_SPEC_CHUNKS)
    detection_history: list[dict] = []

    buf_len = config.IQ_BUFFER_SIZE

    # Content identity (ntw_id, auth_tag, payload) -> most recent start time,
    # for strict de-duplication of a packet decoded more than once.
    recent_decodes: dict = {}

    # Cross-cycle capture state (backlog + truncation-confirmation +
    # target tracking) lives in this object; all the decision logic is the
    # pure, unit-tested select_td_capture() in td_capture.py.
    td_state = TdCaptureState()

    # Time-domain plot rendering (matplotlib, much heavier than the main
    # spectrogram tile) is done entirely by the separate td_renderer process
    # (see td_renderer.py) -- a background *thread* here would still share
    # this loop's GIL and could steal CPU time from real-time decoding, so
    # rendering gets a fully separate process instead, same as this
    # processor is itself split from the RX thread. Only the single most
    # recent render request is kept -- if the renderer is still busy when a
    # new one arrives, the stale request is dropped.
    def _submit_td_render(td_seg, decode_info, n_syms):
        try:
            td_job_queue.get_nowait()  # drop a stale pending request, if any
        except queue.Empty:
            pass
        try:
            td_job_queue.put_nowait((td_seg, decode_info, n_syms))
        except queue.Full:
            pass

    print("[PROC] Processor process started (separate GIL).", flush=True)

    while running_event.is_set():
        t0 = time.perf_counter()

        widx = buf_write_idx_val.value

        # Drain drop positions from queue
        drop_positions = []
        while True:
            try:
                drop_positions.append(drop_queue.get_nowait())
            except queue.Empty:
                break

        decode_start = (widx - config.DECODE_SAMPLES) % buf_len
        drop_sample_offsets = []
        for dp in drop_positions:
            if decode_start < widx:
                if decode_start <= dp < widx:
                    drop_sample_offsets.append(dp - decode_start)
            else:
                if dp >= decode_start:
                    drop_sample_offsets.append(dp - decode_start)
                elif dp < widx:
                    drop_sample_offsets.append(buf_len - decode_start + dp)

        def _extract_last(n):
            start = (widx - n) % buf_len
            if start < widx:
                return iq_buffer[start:widx].copy()
            return np.concatenate([iq_buffer[start:], iq_buffer[:widx]])

        # 1) Spectrogram
        t_spec0 = time.perf_counter()
        spec_chunk_iq = _extract_last(config.SPEC_CHUNK_SAMPLES)
        try:
            sxx_chunk = compute_spec_chunk(spec_chunk_iq)
            spec_chunks.append(sxx_chunk)
        except Exception as e:
            print(f"[PROC] Spec error: {e}")
        t_spec_ms = (time.perf_counter() - t_spec0) * 1000

        # 2) Decode
        t_dec0 = time.perf_counter()
        decode_chunk = _extract_last(config.DECODE_SAMPLES)
        try:
            packets, detections, attempts = decode_signal(decode_chunk)
        except Exception as e:
            print(f"[PROC] Decode error: {e}")
            packets, detections, attempts = [], [], []
        t_dec_ms = (time.perf_counter() - t_dec0) * 1000

        # De-duplicate packets that are the same and overlap in time
        recent_decodes = {k: v for k, v in recent_decodes.items()
                          if t0 - v < 3.0}
        deduped = []
        for p in packets:
            start_t = t0 - (config.DECODE_WINDOW_S - p.get("time_s", 0.0))
            key = (p.get("ntw_id"), p.get("auth_tag"), p.get("payload_val"))
            prev = recent_decodes.get(key)
            if prev is not None and abs(start_t - prev) < config.DEDUP_START_TOL_S:
                continue  # same packet within the tolerance -> drop the repeat
            recent_decodes[key] = start_t
            deduped.append(p)
        packets = detections = deduped

        # Annotate sample-drop failures
        if drop_sample_offsets and attempts:
            for att in attempts:
                if att.get("decoded"):
                    continue
                pkt_start = att.get("start_sample",
                                    int(att.get("time_s", 0) * config.SAMPLE_RATE))
                dur_s = att.get("signal_duration_s", 0.05)
                pkt_end = pkt_start + int(dur_s * config.SAMPLE_RATE)
                for doff in drop_sample_offsets:
                    if pkt_start <= doff <= pkt_end:
                        att["sample_drop"] = True
                        if att.get("reason") and att["reason"] != "ok":
                            att["reason"] = f"{att['reason']}+sample_drop"
                        else:
                            att["reason"] = "sample_drop"
                        break

        # 3) Detection history (process-local)
        for d in detection_history:
            d["offset_from_right"] += config.SPEC_CHUNK_S
        detection_history = [
            d for d in detection_history
            if d["offset_from_right"] <= config.SPEC_DURATION_S
        ]
        for det in detections:
            new_offset = config.DECODE_WINDOW_S - det["time_s"]
            is_dup = False
            for existing in detection_history:
                if (abs(existing["offset_from_right"] - new_offset) < 0.15
                        and abs(existing["freq_hz"] - det.get("F0_hz", det["freq_hz"])) < 5000):
                    is_dup = True
                    break
            if not is_dup:
                detection_history.append({
                    "offset_from_right": new_offset,
                    "freq_hz": det.get("F0_hz", det["freq_hz"]),
                    "phy_ver": det["phy_ver"],
                    "signal_duration_s": det.get(
                        "signal_duration_s",
                        det.get("preamble_duration_s", 0.05),
                    ),
                    "chipset": det.get("chipset", "v-1"),
                })

        # 4) Render spectrogram (or spectrum-analyzer trace when toggled)
        t_render0 = time.perf_counter()
        img_bytes = b""
        try:
            if int(view_mode_val.value) == 1:
                img_bytes = render_spectrum_image(
                    list(spec_chunks), lo_freq_val.value,
                )
            else:
                img_bytes = render_spec_image(list(spec_chunks), detection_history)
        except Exception as e:
            print(f"[PROC] Render error: {e}")
        t_render_ms = (time.perf_counter() - t_render0) * 1000

        dt_ms = (time.perf_counter() - t0) * 1000

        ts = time.strftime("%H:%M:%S")
        unix_ts = time.time()
        decode_entries = []
        for pkt in packets:
            ver = pkt["phy_ver"]
            ntw_hex = f"0x{pkt['ntw_id']:09X}" if ver == -1 else f"0x{pkt['ntw_id']:08X}"

            pkt_start = pkt.get("start_sample")
            timing: dict = {
                "sym_count": None, "sym_mean_ms": None, "sym_std_ms": None,
                "gap_count": None, "gap_mean_ms": None, "gap_std_ms": None,
            }
            if pkt_start is not None:
                slot = config.slot_samples.get(ver, config.slot_samples[1])["slot"]
                n_sym = (config.PREAMBLE_LEN + config.NUM_HEADER_SYMS
                         + (pkt.get("num_pdu_symbols") or 0))
                edges = correct_symbol_edges(
                    decode_chunk, pkt_start, 0, n_sym, 0, slot, config.samples_per_symbol,
                )
                if edges:
                    timing = edges_to_timing_stats(edges, config.SAMPLE_RATE)

            decode_entries.append({
                "timestamp": ts,
                "unix_ts": unix_ts,
                "phy_ver": ver,
                "ntw_id": pkt["ntw_id"],
                "ntw_id_hex": ntw_hex,
                "seq_num": pkt["seq_num"],
                "auth_tag": pkt["auth_tag"],
                "energy_dB": round(pkt["total_energy_dB"], 1),
                "chipset": pkt.get("chipset", ""),
                "channel_num": pkt.get("channel_num"),
                "freq_delta_hz": pkt.get("freq_delta_hz"),
                "payload_val": pkt.get("payload_val"),
                "payload_bytes": pkt.get("payload_bytes"),
                "header_n_corr": pkt.get("header_n_corr"),
                "pdu_n_corr": pkt.get("pdu_n_corr"),
                "num_pdu_symbols": pkt.get("num_pdu_symbols"),
                **timing,
            })

        stats = {
            "process_time_ms": round(dt_ms, 1),
            "n_detections": len(packets),
            "timestamp": ts,
            "t_spec_ms": round(t_spec_ms, 1),
            "t_render_ms": round(t_render_ms, 1),
            "t_decode_ms": round(t_dec_ms, 1),
            "rx_gain_dB": round(rx_gain_dB_val.value, 1),
            "rx_peak_pct": round(rx_peak_frac_val.value * 100, 1),
            "rx_overflows": rx_overflows_val.value,
        }

        # 5) Time-domain plot
        td_img = None
        td_zoom_img = None
        td_status_str = None
        td_decode_info_out = None
        td_iq_seg_out = None

        td_on = bool(td_running_val.value)
        td_chipset_raw = td_chipset_arr.value
        td_chipset = td_chipset_raw.decode() if td_chipset_raw else None
        td_ntw_id = td_ntw_id_val.value if td_has_ntw_val.value else None

        # All the cross-cycle selection/confirmation/backlog logic lives in
        # the pure, unit-tested select_td_capture(); the loop just supplies
        # this cycle's observations and acts on the decision.
        td_result = select_td_capture(
            td_state, td_on=td_on, td_chipset=td_chipset, td_ntw_id=td_ntw_id,
            attempts=attempts, packets=packets, decode_start=decode_start,
            buf_len=buf_len, chunk_len=len(decode_chunk), now=t0,
        )
        td_status_str = td_result.status
        if td_result.capture is not None:
            cap = td_result.capture
            td_hit = cap["td_hit"]
            td_center, td_start, td_end = (
                cap["td_center"], cap["td_start"], cap["td_end"],
            )
            td_seg = decode_chunk[td_start:td_end]
            decode_info = {k: v for k, v in td_hit.items()
                           if not k.startswith("_")}
            decode_info.setdefault("decoded", False)
            decode_info.setdefault("reason", "unknown")
            # td_center is always current-decode_chunk-relative (freshly
            # recomputed each cycle for a retried pending hit), unlike the
            # possibly-stale "start_sample"/"time_s" carried over from the
            # cycle the failure was first matched on.
            decode_info["start_sample"] = td_center - td_start
            decode_info["time_s"] = td_center / config.SAMPLE_RATE
            if not decode_info.get("energy_dB"):
                decode_info["energy_dB"] = td_hit.get("total_energy_dB")

            # The actual plot rendering (matplotlib, slow) is handed off to
            # the separate td_renderer process -- this loop must keep pace
            # with the real-time decode cadence regardless of how often a
            # failure/device match is found. td_img etc. stay None in *this*
            # cycle's result; the renderer pushes its own result once ready.
            _submit_td_render(
                td_seg.copy(), decode_info, td_zoom_n_syms_val.value,
            )

        # Send results to main process
        result = {
            "img": img_bytes,
            "detections": detections,
            "decode_entries": decode_entries,
            "stats": stats,
            "chipset_stats": get_chipset_stats(),
            "td_img": td_img,
            "td_zoom_img": td_zoom_img,
            "td_status": td_status_str,
            "td_decode_info": td_decode_info_out,
            "td_iq_segment": td_iq_seg_out,
        }
        try:
            result_queue.put_nowait(result)
        except Exception:
            pass

        if config.VERBOSE:
            print(
                f"[PROC] total={dt_ms:6.1f} ms | spec={t_spec_ms:5.1f} | "
                f"render={t_render_ms:5.1f} | decode={t_dec_ms:5.1f} | det={len(packets)}"
            )

        elapsed = time.perf_counter() - t0
        sleep_s = max(0, config.DECODE_INTERVAL_S - elapsed)
        if sleep_s > 0:
            time.sleep(sleep_s)

    shm.close()
    print("[PROC] Processor process exiting.", flush=True)
