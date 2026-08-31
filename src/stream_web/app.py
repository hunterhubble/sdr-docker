"""Flask web application and process orchestration.

Serves the live spectrogram dashboard on port 8050 and coordinates the
SDR RX thread, processor *process*, and Flask server.

The processor runs in a **separate OS process** so its Python GIL is
independent of the RX thread.  This prevents spectrogram/decode
computation from stalling the real-time sample stream and causing
sample drops.
"""

import base64
import io
import itertools
import json
import logging
import multiprocessing as mp
import os
import tempfile
import threading
import time
from collections import deque
from multiprocessing import shared_memory

import numpy as np
from flask import Flask, Response, jsonify, render_template, send_file
from flask import request as flask_request
from hubble_satnet_decoder import compute_spec_chunk, reset_chipset_stats

from . import analysis, config
from .processor import processor_main
from .spectrogram import evaluate_fcc_compliance, spectrum_traces
from .spectrum_renderer import spectrum_renderer_main
from .td_renderer import td_renderer_main

# GNU Radio imports — deferred so the app can be imported without GNU Radio
# installed (e.g. SDR_TYPE=mock, CI tests).
try:
    from .gnuradio_tx import TX_SOURCE_DIR, TXFlowgraph
    from .sdr import rx_loop
except ImportError:
    TX_SOURCE_DIR = os.environ.get(
        "TX_SOURCE_DIR", os.path.join(os.path.dirname(__file__), "source_files")
    )
    TXFlowgraph = None  # type: ignore[assignment,misc]
    rx_loop = None  # type: ignore[assignment]

# ===========================================================================
# Shared application state
# ===========================================================================

_IQ_SHM_NAME = "pluto_iq_buf"
_IQ_NBYTES = config.IQ_BUFFER_SIZE * np.dtype(np.complex64).itemsize

# Worker-process restart budget: a worker that dies more than
# _WORKER_MAX_RESTARTS times within _WORKER_RESTART_WINDOW_S is assumed to be
# crash-looping (persistent bug, not a transient fault), and the watchdog
# stops restarting it rather than hot-looping forever.
_WORKER_MAX_RESTARTS = 5
_WORKER_RESTART_WINDOW_S = 60.0


class SharedState:
    """State shared between RX thread, processor process, and Flask.

    The IQ circular buffer lives in POSIX shared memory so the processor
    process can read it without any copy or GIL contention.  Simple
    scalars use ``multiprocessing.Value`` (atomic on CPython).  Everything
    else stays in normal Python objects protected by a threading lock
    (only accessed within the main process).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running = mp.Event()
        self.rx_connected = threading.Event()

        # --- shared memory IQ buffer (RX writes, processor reads) ---
        try:
            shared_memory.SharedMemory(name=_IQ_SHM_NAME).unlink()
        except FileNotFoundError:
            pass
        self._shm = shared_memory.SharedMemory(
            name=_IQ_SHM_NAME, create=True, size=_IQ_NBYTES,
        )
        self.iq_buffer = np.ndarray(
            config.IQ_BUFFER_SIZE, dtype=np.complex64, buffer=self._shm.buf,
        )
        self.iq_buffer[:] = 0

        # --- multiprocessing-safe scalars (accessed by RX + processor) ---
        self._buf_write_idx = mp.Value("q", 0)       # unsigned‐long‐long
        self._rx_peak_frac = mp.Value("d", 0.0)
        self._rx_overflows = mp.Value("i", 0)
        self._rx_gain_dB = mp.Value("d", config.RX_INITIAL_GAIN_DB)

        # --- control values read by the processor (mp-safe) ---
        self._td_running = mp.Value("b", 0)
        self._td_target_ntw_id = mp.Value("q", 0)    # 0 = None
        self._td_has_ntw_id = mp.Value("b", 0)       # flag
        self._lo_freq_hz = mp.Value("q", config.CENTER_FREQ_HZ)

        # td_target_chipset needs a string; use a fixed-size mp.Array
        self._td_chipset_arr = mp.Array("c", 32)
        self._td_zoom_n_syms = mp.Value("i", 6)

        # main-view mode read by the processor: 0 = spectrogram, 1 = spectrum
        self._view_mode = mp.Value("b", 0)

        # --- drop positions (RX→processor, lock-free via mp.Queue) ---
        self.drop_queue: mp.Queue = mp.Queue()

        # --- results coming back from processor / td_renderer (via mp.Queue) ---
        self.result_queue: mp.Queue = mp.Queue()

        # --- time-domain render requests (processor→td_renderer); single
        # slot -- only the freshest pending capture is ever worth rendering
        self.td_job_queue: mp.Queue = mp.Queue(1)

        # --- spectrum-analyzer render requests (processor→spectrum_renderer);
        # single slot, same reasoning as td_job_queue.
        self.spectrum_job_queue: mp.Queue = mp.Queue(1)

        # --- worker processes + their spawn closures (for the watchdog) ---
        self.workers: dict = {}
        self.worker_spawns: dict = {}

        # --- main-process-only state (Flask / result drainer) ---
        self.spec_chunks: deque = deque(maxlen=config.MAX_SPEC_CHUNKS)
        self.latest_img: bytes = b""
        self.latest_detections: list[dict] = []
        self.decode_results: list[dict] = []
        self.packet_feed: list[dict] = []
        self.detection_history: list[dict] = []
        self.decode_stats: dict = {
            "process_time_ms": 0, "n_detections": 0, "timestamp": "",
            "t_spec_ms": 0, "t_render_ms": 0, "t_decode_ms": 0,
        }

        self.td_latest_img: bytes = b""
        self.td_zoom_latest_img: bytes = b""
        self.td_status: str = ""
        self.td_decode_info: dict | None = None
        self.td_iq_segment: np.ndarray | None = None
        self.chipset_stats: dict = {}

    def cleanup_shm(self):
        try:
            self._shm.close()
            self._shm.unlink()
        except Exception:
            pass

    # --- properties that wrap mp.Value for transparent access ---

    @property
    def buf_write_idx(self):
        return self._buf_write_idx.value

    @buf_write_idx.setter
    def buf_write_idx(self, v):
        self._buf_write_idx.value = v

    @property
    def rx_peak_frac(self):
        return self._rx_peak_frac.value

    @rx_peak_frac.setter
    def rx_peak_frac(self, v):
        self._rx_peak_frac.value = v

    @property
    def rx_overflows(self):
        return self._rx_overflows.value

    @rx_overflows.setter
    def rx_overflows(self, v):
        self._rx_overflows.value = v

    @property
    def rx_gain_dB(self):
        return self._rx_gain_dB.value

    @rx_gain_dB.setter
    def rx_gain_dB(self, v):
        self._rx_gain_dB.value = v

    @property
    def lo_freq_hz(self):
        return self._lo_freq_hz.value

    @lo_freq_hz.setter
    def lo_freq_hz(self, v):
        self._lo_freq_hz.value = v

    @property
    def td_running(self):
        return bool(self._td_running.value)

    @td_running.setter
    def td_running(self, v):
        self._td_running.value = int(bool(v))

    @property
    def td_target_ntw_id(self):
        if not self._td_has_ntw_id.value:
            return None
        return self._td_target_ntw_id.value

    @td_target_ntw_id.setter
    def td_target_ntw_id(self, v):
        if v is None:
            self._td_has_ntw_id.value = 0
        else:
            self._td_target_ntw_id.value = int(v)
            self._td_has_ntw_id.value = 1

    @property
    def td_target_chipset(self):
        raw = self._td_chipset_arr.value
        return raw.decode() if raw else None

    @td_target_chipset.setter
    def td_target_chipset(self, v):
        self._td_chipset_arr.value = (v or "").encode()[:31]

    @property
    def td_zoom_n_syms(self) -> int:
        return self._td_zoom_n_syms.value

    @td_zoom_n_syms.setter
    def td_zoom_n_syms(self, v: int) -> None:
        self._td_zoom_n_syms.value = max(1, min(int(v), config.PREAMBLE_LEN))

    @property
    def view_mode(self) -> str:
        return "spectrum" if self._view_mode.value else "spectrogram"

    @view_mode.setter
    def view_mode(self, v) -> None:
        self._view_mode.value = 1 if v in (1, "spectrum", True) else 0

    # legacy compat — RX pushes drop positions via queue now
    @property
    def rx_drop_positions(self):
        return []

    @rx_drop_positions.setter
    def rx_drop_positions(self, v):
        pass


state = SharedState()
tx_fg: "TXFlowgraph | None" = None


# ===========================================================================
# Flask app
# ===========================================================================

app = Flask(__name__)

if not config.VERBOSE:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/spectrogram.jpg")
def spectrogram_jpg():
    with state.lock:
        data = state.latest_img
    if not data:
        return Response(status=204)
    return Response(data, mimetype="image/jpeg")


@app.route("/api/status")
def api_status():
    with state.lock:
        devices: dict[int, dict] = {}
        for r in state.decode_results:
            nid = r["ntw_id"]
            if nid not in devices:
                devices[nid] = {
                    "ntw_id": nid,
                    "ntw_id_hex": r["ntw_id_hex"],
                    "phy_ver": r.get("phy_ver", -1),
                    "chipset": r.get("chipset", ""),
                    "max_energy_dB": r["energy_dB"],
                    "seq_nums": [],
                    "last_seen": r["timestamp"],
                }
            d = devices[nid]
            d["max_energy_dB"] = r["energy_dB"]
            d["seq_nums"].append(r["seq_num"])
            d["last_seen"] = r["timestamp"]
            if r.get("freq_delta_hz") is not None:
                d["freq_delta_hz"] = r["freq_delta_hz"]
            pb = r.get("payload_bytes") or 0
            pv = r.get("payload_val")
            if pb > 0 and pv is not None:
                d["payload_b64"] = base64.b64encode(
                    pv.to_bytes(pb, "big")
                ).decode("ascii")
            elif pb == 0:
                d["payload_b64"] = ""

        for d in devices.values():
            # Keep chronological order, including repeats after a wraparound.
            d["seq_nums"] = d["seq_nums"][-10:]
            d.setdefault("freq_delta_hz", None)
            d.setdefault("payload_b64", "")

        dev_list = sorted(devices.values(), key=lambda x: x["ntw_id"])
        stats = dict(state.decode_stats)
        stats["n_unique_devices"] = len(dev_list)

        td_b64 = ""
        if state.td_latest_img:
            td_b64 = base64.b64encode(state.td_latest_img).decode("ascii")
        td_zoom_b64 = ""
        if state.td_zoom_latest_img:
            td_zoom_b64 = base64.b64encode(state.td_zoom_latest_img).decode("ascii")

        cs_stats = dict(state.chipset_stats)

        rs_by_chipset: dict[str, list[float]] = {}
        for r in state.decode_results:
            chip = r.get("chipset", "")
            if not chip:
                continue
            n = r.get("pdu_n_corr")
            total = r.get("num_pdu_symbols")
            if n is not None and total:
                rs_by_chipset.setdefault(chip, []).append(100.0 * n / total)
        for chip, pcts in rs_by_chipset.items():
            if chip in cs_stats:
                cs_stats[chip]["avg_rs_corr_pct"] = round(sum(pcts) / len(pcts), 1)

        return jsonify(
            sdr_connected=state.rx_connected.is_set(),
            devices=dev_list, stats=stats,
            td_img=td_b64, td_zoom_img=td_zoom_b64,
            td_running=state.td_running,
            td_device_id=state.td_target_ntw_id,
            td_chipset=state.td_target_chipset,
            td_status=state.td_status,
            td_decode_info=state.td_decode_info,
            td_zoom_n_syms=state.td_zoom_n_syms,
            chipset_stats=cs_stats,
            known_chipsets=sorted(config.SYNTH_RES.keys()),
            lo_freq_hz=state.lo_freq_hz,
            view_mode=state.view_mode,
        )


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with state.lock:
        state.decode_results.clear()
        state.decode_stats.update(
            process_time_ms=0, n_detections=0, timestamp="",
            t_spec_ms=0, t_render_ms=0, t_decode_ms=0,
        )
        reset_chipset_stats()
        state.chipset_stats.clear()
        state.detection_history = []
    return jsonify(ok=True)


@app.route("/api/gain", methods=["POST"])
def api_gain():
    data = flask_request.get_json(silent=True) or {}
    direction = data.get("direction", 0)
    new_gain = state.rx_gain_dB + direction * config.RX_GAIN_STEP_DB
    new_gain = int(max(config.RX_GAIN_MIN_DB, min(config.RX_GAIN_MAX_DB, new_gain)))
    state.rx_gain_dB = new_gain
    return jsonify(gain=new_gain)


@app.route("/api/view", methods=["GET", "POST"])
def api_view():
    """Toggle the main display between spectrogram and spectrum-analyzer view."""
    if flask_request.method == "POST":
        data = flask_request.get_json(silent=True) or {}
        mode = data.get("mode")
        if mode is None:
            # No explicit mode -> flip the current one.
            mode = "spectrogram" if state.view_mode == "spectrum" else "spectrum"
        if mode not in ("spectrogram", "spectrum"):
            return jsonify(error="mode must be 'spectrogram' or 'spectrum'"), 400
        state.view_mode = mode
    return jsonify(view_mode=state.view_mode)


@app.route("/api/lo", methods=["POST"])
def api_lo():
    data = flask_request.get_json(silent=True) or {}
    delta = data.get("delta_khz", 0)
    new_freq = state.lo_freq_hz + int(delta) * 1000
    state.lo_freq_hz = new_freq
    return jsonify(lo_freq_hz=new_freq)


@app.route("/api/timedomain", methods=["GET", "POST"])
def api_timedomain():
    if flask_request.method == "POST":
        data = flask_request.get_json(silent=True) or {}
        action = data.get("action")
        if action == "start":
            chipset = data.get("chipset")
            dev_id = data.get("device_id")
            if chipset:
                state.td_target_chipset = chipset
                state.td_target_ntw_id = None
                state.td_running = True
            elif dev_id is not None:
                try:
                    state.td_target_ntw_id = int(dev_id)
                    state.td_target_chipset = None
                    state.td_running = True
                except (ValueError, TypeError):
                    return jsonify(error="Invalid device_id"), 400
        elif action == "stop":
            state.td_running = False
        elif action == "set_n_syms":
            try:
                state.td_zoom_n_syms = int(data.get("n_syms", 6))
            except (ValueError, TypeError):
                return jsonify(error="Invalid n_syms"), 400
    with state.lock:
        status = state.td_status
    return jsonify(
        running=state.td_running, device_id=state.td_target_ntw_id,
        chipset=state.td_target_chipset, status=status,
        td_zoom_n_syms=state.td_zoom_n_syms,
    )


@app.route("/api/td_iq", methods=["GET"])
def api_td_iq():
    """Download the current TD IQ capture as a .npy file."""
    with state.lock:
        seg = state.td_iq_segment
    if seg is None:
        return jsonify(error="No IQ capture available"), 404
    buf = io.BytesIO()
    np.save(buf, seg)
    buf.seek(0)
    return send_file(
        buf, mimetype="application/octet-stream",
        as_attachment=True, download_name="td_capture.npy",
    )


@app.route("/api/td_info", methods=["GET"])
def api_td_info():
    """Return just the decode_info for the current TD capture."""
    with state.lock:
        info = state.td_decode_info
    if info is None:
        return jsonify(error="No capture available"), 404
    return jsonify(info)


@app.route("/api/packets", methods=["GET"])
def api_packets():
    """Poll-and-drain: return all decodes since last call as JSONL, then clear.

    Each line is a JSON object with: device_id, seq_num, device_type,
    timestamp, rssi_dB, channel_num, freq_offset_hz, pdu_n_corr, header_n_corr
    and symbol data(sym_count, sym_mean_ms, sym_std_ms, gap_count, gap_mean_ms, gap_std_ms).
    """
    with state.lock:
        entries = list(state.packet_feed)
        state.packet_feed.clear()

    lines = []
    for e in entries:
        pb = e.get("payload_bytes") or 0
        pv = e.get("payload_val")
        if pb > 0 and pv is not None:
            payload_b64 = base64.b64encode(pv.to_bytes(pb, "big")).decode("ascii")
        else:
            payload_b64 = ""
        lines.append(json.dumps({
            "device_id": e["ntw_id_hex"],
            "seq_num": e["seq_num"],
            "auth_tag": e.get("auth_tag"),
            "phy_ver": e.get("phy_ver"),
            "device_type": e.get("chipset", ""),
            "timestamp": e.get("unix_ts", 0),
            "rssi_dB": e.get("energy_dB"),
            "channel_num": e.get("channel_num"),
            "freq_offset_hz": e.get("freq_delta_hz"),
            "payload_b64": payload_b64,
            "pdu_n_corr": e.get("pdu_n_corr"),
            "header_n_corr": e.get("header_n_corr"),
            "sym_count": e.get("sym_count"),
            "sym_mean_ms": e.get("sym_mean_ms"),
            "sym_std_ms": e.get("sym_std_ms"),
            "gap_count": e.get("gap_count"),
            "gap_mean_ms": e.get("gap_mean_ms"),
            "gap_std_ms": e.get("gap_std_ms"),
        }))
    payload = "\n".join(lines) + ("\n" if lines else "")
    return Response(payload, mimetype="application/x-ndjson")


_CAPTURE_DEFAULT_SECONDS = 10
_CAPTURE_MAX_SECONDS = 30

# Only one capture may run at a time. Each holds a Flask worker for up to its full
# duration, and concurrent captures would contend for both workers and buffer
# bandwidth; a second request is rejected rather than queued. Shared by
# /api/iq_capture and /api/record_analyze.
_capture_lock = threading.Lock()


class CaptureError(Exception):
    """Raised by _capture_iq; carries the HTTP status the caller should return."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _samples_advanced(start: int, end: int, buf_size: int) -> int:
    """Number of samples written from *start* to *end* in a circular buffer."""
    if end >= start:
        return end - start
    return buf_size - start + end


def _capture_iq(n_samples: int) -> np.ndarray:
    """Capture *n_samples* of IQ forward from now out of the shared ring buffer.

    Copies straight into one pre-allocated array (single copy). Raises CaptureError on
    SDR stall (504) or if the RX laps the region mid-copy (500). Caller must hold
    _capture_lock.
    """
    buf_size = config.IQ_BUFFER_SIZE
    chunk_n = buf_size // 2
    segment = np.empty(n_samples, dtype=np.complex64)
    filled = 0
    chunk_start = int(state.buf_write_idx)

    while filled < n_samples:
        this_n = min(chunk_n, n_samples - filled)
        deadline = time.monotonic() + (this_n / config.SAMPLE_RATE) + 5.0

        while _samples_advanced(chunk_start, int(state.buf_write_idx), buf_size) < this_n:
            if time.monotonic() > deadline:
                raise CaptureError("Timed out waiting for IQ samples from SDR", 504)
            time.sleep(0.05)

        e = (chunk_start + this_n) % buf_size
        if chunk_start < e:
            segment[filled:filled + this_n] = state.iq_buffer[chunk_start:e]
        else:
            n1 = buf_size - chunk_start
            segment[filled:filled + n1] = state.iq_buffer[chunk_start:]
            segment[filled + n1:filled + this_n] = state.iq_buffer[:e]

        # Integrity guard: the region was read lock-free while the RX kept writing. The
        # RX only overwrites chunk_start after lapping a full buffer past it; if the copy
        # stalled that long, the head is now < this_n ahead (it wrapped past chunk_start)
        # and the samples are corrupt.
        if _samples_advanced(chunk_start, int(state.buf_write_idx), buf_size) < this_n:
            raise CaptureError(
                "SDR overran the capture buffer (host too slow); capture aborted", 500)

        chunk_start = e
        filled += this_n

    return segment


def _parse_seconds(default=_CAPTURE_DEFAULT_SECONDS, maximum=_CAPTURE_MAX_SECONDS):
    """Parse/validate the 'seconds' query param.

    Returns ``(seconds, None)`` on success or ``(None, (response, status))`` on error.
    """
    raw = flask_request.args.get("seconds")
    if raw is None:
        if default is not None:
            return default, None
        return None, (jsonify(error="'seconds' query parameter is required"), 400)
    try:
        seconds = int(raw)
    except (ValueError, TypeError):
        return None, (jsonify(error="'seconds' must be an integer"), 400)
    if seconds < 1:
        return None, (jsonify(error="'seconds' must be >= 1"), 400)
    if seconds > maximum:
        return None, (jsonify(error=f"'seconds' must be <= {maximum}"), 400)
    return seconds, None


@app.route("/api/iq_capture", methods=["GET"])
def api_iq_capture():
    seconds, err = _parse_seconds()
    if err:
        return err

    if not _capture_lock.acquire(blocking=False):
        return jsonify(error="Another capture is already in progress"), 409
    try:
        segment = _capture_iq(seconds * config.SAMPLE_RATE)
    except CaptureError as ce:
        return jsonify(error=str(ce)), ce.status
    finally:
        _capture_lock.release()

    # Spill the .npy to disk past 10 MB so a large capture isn't duplicated in RAM: the
    # segment plus its serialized bytes would otherwise roughly double peak memory
    # (~375 MB for a 30 s capture) and can OOM small containers.
    buf = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    np.save(buf, segment)
    buf.seek(0)
    fname = f"iq_{int(time.time())}_{seconds}s.npy"
    resp = send_file(buf, mimetype="application/octet-stream",
                     as_attachment=True, download_name=fname)
    resp.headers["X-Sample-Rate-Hz"] = str(config.SAMPLE_RATE)
    resp.headers["X-Center-Freq-Hz"] = str(int(state.lo_freq_hz))
    resp.headers["X-Duration-S"] = str(seconds)
    resp.headers["X-N-Samples"] = str(int(len(segment)))
    return resp


@app.route("/api/record_analyze", methods=["GET"])
def api_record_analyze():
    """Record N seconds of IQ and return a plain-text signal-diagnostic report file
    (timing, frequency, channel hopping, amplitude) for the customer to send back."""
    seconds, err = _parse_seconds()
    if err:
        return err

    if not _capture_lock.acquire(blocking=False):
        return jsonify(error="Another capture is already in progress"), 409
    try:
        segment = _capture_iq(seconds * config.SAMPLE_RATE)
    except CaptureError as ce:
        return jsonify(error=str(ce)), ce.status
    finally:
        _capture_lock.release()

    # Analysis is CPU-only on the captured copy; the lock is already released.
    report = analysis.analyze_recording(segment)
    report["capture"] = {
        "seconds": seconds,
        "sample_rate_hz": config.SAMPLE_RATE,
        "center_freq_hz": int(state.lo_freq_hz),
        "n_samples": int(len(segment)),
    }
    text = analysis.build_report(report)
    buf = io.BytesIO(text.encode("utf-8"))
    buf.seek(0)
    fname = f"sat_record_{int(time.time())}_{seconds}s.txt"
    resp = send_file(buf, mimetype="text/plain", as_attachment=True, download_name=fname)
    resp.headers["X-Sample-Rate-Hz"] = str(config.SAMPLE_RATE)
    resp.headers["X-Center-Freq-Hz"] = str(int(state.lo_freq_hz))
    resp.headers["X-Duration-S"] = str(seconds)
    resp.headers["X-N-Samples"] = str(int(len(segment)))
    return resp


_FCC_CHECK_DEFAULT_SECONDS = int(config.SPECTRUM_AVG_CHUNKS * config.SPEC_CHUNK_S)


@app.route("/api/fcc_check", methods=["GET"])
def api_fcc_check():
    """Record N seconds of IQ (default: the same window the spectrum-analyzer
    view averages over) and evaluate FCC 15.247 compliance on the strongest
    tone in it -- a one-shot, machine-readable version of the pass/fail box
    drawn on the spectrum-analyzer overlay. Shares evaluate_fcc_compliance()
    with that overlay, so the verdict here and the picture can't disagree.

    Intended flow: `POST /api/tx/start {"mode":"tone"}`, then this call, which
    captures the *next* N seconds fresh -- not the rolling display buffer --
    so the result reflects only what was transmitted after the call was made.
    """
    seconds, err = _parse_seconds(default=_FCC_CHECK_DEFAULT_SECONDS)
    if err:
        return err

    if not _capture_lock.acquire(blocking=False):
        return jsonify(error="Another capture is already in progress"), 409
    try:
        segment = _capture_iq(seconds * config.SAMPLE_RATE)
    except CaptureError as ce:
        return jsonify(error=str(ce)), ce.status
    finally:
        _capture_lock.release()

    chunk_n = config.SPEC_CHUNK_SAMPLES
    n_chunks = len(segment) // chunk_n
    if n_chunks < 1:
        return jsonify(error=f"'seconds' must be >= {config.SPEC_CHUNK_S} "
                             "to fill one FFT window"), 400
    chunks = [compute_spec_chunk(segment[i * chunk_n:(i + 1) * chunk_n])
              for i in range(n_chunks)]

    n_bins = chunks[0].shape[0]
    fs = config.SAMPLE_RATE
    center_freq_hz = state.lo_freq_hz
    freqs_hz = np.linspace(-fs / 2.0, fs / 2.0, n_bins) + center_freq_hz
    bin_hz = fs / n_bins

    avg_dB, _peak_dB = spectrum_traces(chunks)
    fcc = evaluate_fcc_compliance(freqs_hz, avg_dB, bin_hz)

    analysis_info = {
        "seconds": seconds,
        "n_chunks": n_chunks,
        "nfft": n_bins,
        "bin_hz": bin_hz,
        "span_hz": fs,
        "center_freq_hz": int(center_freq_hz),
        "rx_gain_db": state.rx_gain_dB,
        "min_snr_db": config.FCC_OVERLAY_MIN_SNR_DB,
        "dc_notch_hz": config.SPEC_DC_NOTCH_BINS * bin_hz,
    }

    if fcc is None:
        return jsonify(state="no_signal", **{"pass": None}, analysis=analysis_info)

    return jsonify(
        state="pass" if fcc["overall_ok"] else "fail",
        **{"pass": fcc["overall_ok"]},
        carrier={
            "freq_hz": fcc["carrier_freq_hz"],
            "offset_from_lo_hz": fcc["carrier_freq_hz"] - center_freq_hz,
            "power_db": fcc["carrier_power_db"],
            "snr_db": fcc["snr_db"],
        },
        checks={
            "occupied_bandwidth": {
                "pass": fcc["spacing_ok"],
                "rule": "FCC 15.247(a)(1)",
                "bw_20db_hz": fcc["bw_20db_hz"],
                "limit_hz": fcc["bw_limit_hz"],
                "channel_spacing_hz": fcc["channel_spacing_hz"],
            },
            "spurious_emissions": {
                "pass": fcc["spurs_ok"],
                "rule": "FCC 15.247(d)",
                "n_spurs": len(fcc["spurs"]),
                "n_over_limit": fcc["n_spur_fail"],
                "worst_dbc": fcc["worst_dbc"],
                "limit_dbc": -fcc["spur_limit_dbc"],
            },
        },
        spurs=[
            {"freq_hz": s["freq_hz"], "power_db": s["power_db"],
             "dbc": s["dbc"], "pass": s["pass"]}
            for s in fcc["spurs"]
        ],
        analysis=analysis_info,
    )


# ===========================================================================
# TX API routes
# ===========================================================================

def _get_tx() -> TXFlowgraph:
    """Lazy-init the TX flowgraph singleton."""
    global tx_fg
    if tx_fg is None:
        print("[TX] Initializing TXFlowgraph (opening SoapySDR sink)...", flush=True)
        tx_fg = TXFlowgraph()
        print("[TX] TXFlowgraph initialized.", flush=True)
    return tx_fg


@app.route("/api/tx/start", methods=["POST"])
def api_tx_start():
    data = flask_request.get_json(silent=True) or {}
    mode = data.get("mode", "tone")
    print(f"[TX] /api/tx/start called: mode={mode}, data={data}", flush=True)
    print("[TX] Getting TX flowgraph...", flush=True)
    fg = _get_tx()
    print(f"[TX] Flowgraph ready (running={fg.is_running}, mode={fg.mode})", flush=True)
    try:
        if mode == "tone":
            print("[TX] Switching to tone mode...", flush=True)
            fg.tone_mode()
            print("[TX] Tone mode set.", flush=True)
        elif mode == "packet":
            file_name = data.get("file", "")
            repeat = data.get("repeat", True)
            if not file_name:
                return jsonify(error="file is required for packet mode"), 400
            file_path = os.path.join(TX_SOURCE_DIR, file_name)
            print(f"[TX] Switching to packet mode: {file_path} "
                  f"(exists={os.path.isfile(file_path)}, "
                  f"size={os.path.getsize(file_path) if os.path.isfile(file_path) else 'N/A'}), "
                  f"repeat={repeat}", flush=True)
            fg.packet_mode(file_path, repeat=repeat)
            print("[TX] Packet mode set.", flush=True)
        else:
            return jsonify(error=f"Unknown mode: {mode}"), 400
        if not fg.is_running:
            print("[TX] Starting flowgraph...", flush=True)
            fg.start()
            print("[TX] Flowgraph started.", flush=True)
        else:
            print("[TX] Flowgraph already running.", flush=True)
        return jsonify(fg.status_dict())
    except Exception as e:
        print(f"[TX] ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify(error=str(e)), 500


@app.route("/api/tx/stop", methods=["POST"])
def api_tx_stop():
    print(f"[TX] /api/tx/stop called (fg={tx_fg is not None}, "
          f"running={tx_fg.is_running if tx_fg else False})", flush=True)
    if tx_fg is None or not tx_fg.is_running:
        return jsonify(running=False)
    tx_fg.stop()
    print("[TX] Flowgraph stopped.", flush=True)
    return jsonify(running=False)


@app.route("/api/tx/freq", methods=["GET", "POST"])
def api_tx_freq():
    fg = _get_tx()
    if flask_request.method == "POST":
        data = flask_request.get_json(silent=True) or {}
        freq = int(data.get("freq_hz", fg.freq_hz))
        fg.set_frequency(freq)
        return jsonify(freq_hz=fg.freq_hz)
    return jsonify(freq_hz=fg.freq_hz)


@app.route("/api/tx/attn", methods=["GET", "POST"])
def api_tx_attn():
    fg = _get_tx()
    if flask_request.method == "POST":
        data = flask_request.get_json(silent=True) or {}
        attn = float(data.get("attn_db", fg.attn_db))
        fg.set_attn(attn)
        return jsonify(attn_db=fg.attn_db)
    return jsonify(attn_db=fg.attn_db)


@app.route("/api/tx/status", methods=["GET"])
def api_tx_status():
    if tx_fg is None:
        return jsonify(running=False, mode=None, freq_hz=0, attn_db=30)
    return jsonify(tx_fg.status_dict())


TX_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB


def _file_sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(131072), b""):
            h.update(chunk)
    return h.hexdigest()


@app.route("/api/tx/files", methods=["GET"])
def api_tx_files_list():
    files = []
    for name in sorted(os.listdir(TX_SOURCE_DIR)):
        path = os.path.join(TX_SOURCE_DIR, name)
        if os.path.isfile(path):
            files.append({
                "name": name,
                "size": os.path.getsize(path),
                "sha256": _file_sha256(path),
            })
    return jsonify(files=files)


@app.route("/api/tx/files", methods=["POST"])
def api_tx_files_upload():
    print("[TX] /api/tx/files upload called", flush=True)
    if "file" not in flask_request.files:
        print("[TX] Upload error: no 'file' field", flush=True)
        return jsonify(error="No 'file' field in request"), 400
    f = flask_request.files["file"]
    if not f.filename:
        return jsonify(error="Empty filename"), 400
    name = os.path.basename(f.filename)
    data = f.read()
    print(f"[TX] Upload received: {name}, {len(data)} bytes", flush=True)
    if len(data) > TX_MAX_UPLOAD_BYTES:
        return jsonify(error=f"File exceeds {TX_MAX_UPLOAD_BYTES} byte limit"), 413
    dest = os.path.join(TX_SOURCE_DIR, name)
    with open(dest, "wb") as out:
        out.write(data)
    sha = _file_sha256(dest)
    print(f"[TX] Upload saved: {dest}, sha256={sha}", flush=True)
    return jsonify(name=name, size=len(data), sha256=sha)


@app.route("/api/tx/files/<filename>", methods=["DELETE"])
def api_tx_files_delete(filename):
    path = os.path.join(TX_SOURCE_DIR, os.path.basename(filename))
    if not os.path.isfile(path):
        return jsonify(error="File not found"), 404
    os.remove(path)
    return jsonify(deleted=filename)


# ===========================================================================
# Result drainer — receives processor output via mp.Queue
# ===========================================================================

def _drain_results(state):
    """Background thread: pull results from the processor process."""
    import queue as _queue
    while state.running.is_set():
        try:
            r = state.result_queue.get(timeout=0.1)
        except _queue.Empty:
            continue
        with state.lock:
            if r.get("img"):
                state.latest_img = r["img"]
            if r.get("detections") is not None:
                state.latest_detections = r["detections"]
            if r.get("decode_entries"):
                state.decode_results.extend(r["decode_entries"])
                state.decode_results[:] = state.decode_results[-config.MAX_DECODE_HISTORY:]
                state.packet_feed.extend(r["decode_entries"])
                state.packet_feed[:] = state.packet_feed[-1000:]
            if r.get("stats"):
                state.decode_stats = r["stats"]
            if r.get("chipset_stats"):
                state.chipset_stats = r["chipset_stats"]
            if r.get("td_img") is not None:
                state.td_latest_img = r["td_img"]
            if r.get("td_zoom_img") is not None:
                state.td_zoom_latest_img = r["td_zoom_img"]
            if r.get("td_status") is not None:
                state.td_status = r["td_status"]
            if r.get("td_decode_info") is not None:
                state.td_decode_info = r["td_decode_info"]
            if r.get("td_iq_segment") is not None:
                state.td_iq_segment = r["td_iq_segment"]


def _worker_watchdog(state, poll_s: float = 1.0):
    """Restart a worker process if it dies while the app is still running.

    All three workers are safe to re-spawn: the td_renderer and
    spectrum_renderer are stateless, and the processor simply re-attaches to
    the shared-memory IQ buffer and resumes (losing only in-memory visual
    history, which self-heals within seconds).

    A crash-looping worker (more than _WORKER_MAX_RESTARTS deaths within
    _WORKER_RESTART_WINDOW_S) is given up on so a persistent bug can't turn
    into an infinite respawn loop.
    """
    restarts: dict = {}  # name -> list of recent restart timestamps
    gave_up: set = set()
    while state.running.is_set():
        for name, proc in list(state.workers.items()):
            if proc.is_alive() or name in gave_up or not state.running.is_set():
                continue
            proc.join(timeout=0)  # reap the dead child so it isn't a zombie

            now = time.monotonic()
            recent = [t for t in restarts.get(name, []) if now - t < _WORKER_RESTART_WINDOW_S]
            if len(recent) >= _WORKER_MAX_RESTARTS:
                gave_up.add(name)
                print(f"[main] ERROR: worker '{name}' crash-looped "
                      f"({_WORKER_MAX_RESTARTS}x in {_WORKER_RESTART_WINDOW_S:.0f}s); "
                      f"giving up. Signal Viewer capture may be degraded.",
                      flush=True)
                continue

            recent.append(now)
            restarts[name] = recent
            print(f"[main] WARNING: worker '{name}' died "
                  f"(exit code {proc.exitcode}); restarting "
                  f"({len(recent)}/{_WORKER_MAX_RESTARTS}).", flush=True)
            new_proc = state.worker_spawns[name]()
            new_proc.start()
            state.workers[name] = new_proc
        time.sleep(poll_s)


# ===========================================================================
# Mock mode — synthetic packet injector (SDR_TYPE=mock)
# ===========================================================================

_MOCK_DEVICES = [
    (0xABCD1234, 1, "chipset-A"),
    (0xDEADBEEF, 1, "chipset-B"),
]


def _mock_injector(state, interval_s: float = 2.0):
    """Emit one synthetic packet per device every *interval_s* seconds."""
    seq_counters = {nid: 0 for nid, _, _ in _MOCK_DEVICES}
    for ntw_id, phy_ver, chipset in itertools.cycle(_MOCK_DEVICES):
        if not state.running.is_set():
            break
        time.sleep(interval_s)
        seq = seq_counters[ntw_id]
        seq_counters[ntw_id] = (seq + 1) % 256
        entry = {
            "ntw_id": ntw_id,
            "ntw_id_hex": f"0x{ntw_id:08X}",
            "seq_num": seq,
            "auth_tag": 0,
            "energy_dB": -60.0,
            "chipset": chipset,
            "channel_num": 0,
            "freq_delta_hz": 0.0,
            "payload_val": None,
            "payload_bytes": 0,
            "timestamp": time.strftime("%H:%M:%S"),
            "unix_ts": time.time(),
            "phy_ver": phy_ver,
        }
        with state.lock:
            state.packet_feed.append(entry)
            state.packet_feed[:] = state.packet_feed[-1000:]
            state.decode_results.append(entry)
            state.decode_results[:] = state.decode_results[-config.MAX_DECODE_HISTORY:]


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    """Start RX thread, processor process, result drainer, and Flask."""
    state.running.set()
    mock_mode = config.SDR_TYPE == "mock"

    if mock_mode:
        print("[main] Mock mode active — no SDR hardware required.")
        state.rx_connected.set()
        threading.Thread(target=_mock_injector, args=(state,), daemon=True).start()
    else:
        # Fork the worker processes BEFORE starting any threads (safe on macOS).
        # Each is described by a spawn closure so the watchdog can re-create it
        # if it dies (mp.Process objects can't be restarted in place).
        def _spawn_processor():
            return mp.Process(
                target=processor_main,
                args=(
                    _IQ_SHM_NAME,
                    state._buf_write_idx,
                    state._rx_peak_frac,
                    state._rx_overflows,
                    state._rx_gain_dB,
                    state._td_running,
                    state._td_target_ntw_id,
                    state._td_has_ntw_id,
                    state._td_chipset_arr,
                    state._td_zoom_n_syms,
                    state._view_mode,
                    state._lo_freq_hz,
                    state.running,
                    state.drop_queue,
                    state.result_queue,
                    state.td_job_queue,
                    state.spectrum_job_queue,
                ),
                daemon=True,
            )

        def _spawn_td_renderer():
            return mp.Process(
                target=td_renderer_main,
                args=(state.td_job_queue, state.result_queue, state.running),
                daemon=True,
            )

        def _spawn_spectrum_renderer():
            return mp.Process(
                target=spectrum_renderer_main,
                args=(state.spectrum_job_queue, state.result_queue, state.running),
                daemon=True,
            )

        state.workers = {"processor": _spawn_processor(),
                         "td_renderer": _spawn_td_renderer(),
                         "spectrum_renderer": _spawn_spectrum_renderer()}
        state.worker_spawns = {"processor": _spawn_processor,
                              "td_renderer": _spawn_td_renderer,
                              "spectrum_renderer": _spawn_spectrum_renderer}
        for p in state.workers.values():
            p.start()

        threading.Thread(target=rx_loop, args=(state,), daemon=True).start()
        threading.Thread(target=_drain_results, args=(state,), daemon=True).start()
        threading.Thread(target=_worker_watchdog, args=(state,), daemon=True).start()
        print("[main] RX thread + processor/td_renderer/spectrum_renderer processes started.")

    print(f"[main] Open http://localhost:{config.FLASK_PORT} in a browser.")

    try:
        app.run(
            host="0.0.0.0",
            port=config.FLASK_PORT,
            threaded=True,
            use_reloader=False,
        )
    finally:
        state.cleanup_shm()
