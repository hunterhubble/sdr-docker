"""Spectrogram image rendering (computation lives in hubble_satnet_decoder)."""

import io

import matplotlib

matplotlib.use("Agg")
import numpy as np  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from scipy.signal import find_peaks  # noqa: E402
from scipy.signal import spectrogram as scipy_spectrogram  # noqa: E402

from . import config  # noqa: E402
from .timing import correct_symbol_edges, measure_transition_us  # noqa: E402

# -- Pre-computed LUT and font ----------------------------------------------

_VIRIDIS_LUT = (matplotlib.colormaps["viridis"](np.arange(256))[:, :3] * 255).astype(np.uint8)

try:
    _SPEC_FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size=14)
except Exception:
    try:
        _SPEC_FONT = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size=14)
    except Exception:
        _SPEC_FONT = ImageFont.load_default()


# ===========================================================================
# Spectrogram image rendering
# ===========================================================================

def _interpolate_dc(spec: np.ndarray, half_bins: int) -> np.ndarray:
    """Interpolate across +-``half_bins`` around the DC (centre) bin, in place.

    Operates on the frequency axis (axis 0) and works for both a 2-D ``Sxx``
    (freq x time) and a 1-D trace via broadcasting: the notched rows are
    replaced with a linear blend of the two rows just outside the notch, so the
    residual LO-leakage disappears from the display. Cosmetic only -- the caller
    must pass a copy that never feeds the decoder.
    """
    if half_bins <= 0 or spec.shape[0] < 3:
        return spec
    n = spec.shape[0]
    dc = n // 2
    lo, hi = dc - half_bins, dc + half_bins
    if lo - 1 < 0 or hi + 1 >= n:
        return spec
    below, above = spec[lo - 1], spec[hi + 1]
    steps = (hi + 1) - (lo - 1)
    for k, idx in enumerate(range(lo, hi + 1), start=1):
        w = k / steps
        spec[idx] = (1.0 - w) * below + w * above
    return spec


def render_spec_image(chunks: list[np.ndarray], detections: list[dict] | None = None) -> bytes:
    """Concat Sxx_dB chunks, apply viridis LUT, draw detection boxes, return JPEG bytes."""
    if not chunks:
        return b""
    Sxx_all = np.concatenate(chunks, axis=1)
    full_cols = int(config.MAX_SPEC_CHUNKS * chunks[0].shape[1])
    if Sxx_all.shape[1] < full_cols:
        pad = np.full((Sxx_all.shape[0], full_cols - Sxx_all.shape[1]),
                      np.min(Sxx_all), dtype=Sxx_all.dtype)
        Sxx_dB = np.concatenate([pad, Sxx_all], axis=1)
    else:
        Sxx_dB = Sxx_all

    # np.concatenate always returns a fresh array, so this never mutates the
    # cached chunks (or the decoder's IQ).
    _interpolate_dc(Sxx_dB, config.SPEC_DC_NOTCH_BINS)

    plow, phigh = np.percentile(Sxx_dB, [2, 99.5])
    if phigh <= plow:
        phigh = plow + 1.0
    idx = np.clip((Sxx_dB - plow) / (phigh - plow) * 255, 0, 255).astype(np.uint8)

    rgb = _VIRIDIS_LUT[idx]
    rgb = rgb[::-1, :, :]
    img = Image.fromarray(rgb, mode="RGB")
    img = img.resize((config.SPEC_IMG_WIDTH, config.SPEC_IMG_HEIGHT), Image.BILINEAR)

    if detections:
        draw = ImageDraw.Draw(img)
        total_time_s = config.SPEC_DURATION_S
        box_h = 20

        for det in detections:
            abs_t = total_time_s - det["offset_from_right"]
            dur = det.get("signal_duration_s", 0.05)
            x0 = int(abs_t / total_time_s * config.SPEC_IMG_WIDTH)
            box_w = max(4, int(dur / total_time_s * config.SPEC_IMG_WIDTH))
            x1 = min(config.SPEC_IMG_WIDTH - 1, x0 + box_w)
            if x1 < 0 or x0 >= config.SPEC_IMG_WIDTH:
                continue

            y_center = int((0.5 - det["freq_hz"] / config.SAMPLE_RATE) * config.SPEC_IMG_HEIGHT)
            y0 = max(0, y_center - box_h // 2)
            y1 = min(config.SPEC_IMG_HEIGHT - 1, y_center + box_h // 2)

            color = (255, 200, 0) if det["phy_ver"] == -1 else (255, 50, 50)
            draw.rectangle([x0, y0, x1, y1], outline=color, width=2)
            label = det.get("chipset", "")
            if label:
                tx = max(0, x0 + 3)
                ty = max(0, y0 - 18)
                bbox = draw.textbbox((tx, ty), label, font=_SPEC_FONT)
                draw.rectangle(
                    [bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1],
                    fill=(10, 10, 20),
                )
                draw.text((tx, ty), label, fill=color, font=_SPEC_FONT)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.read()


# ===========================================================================
# Spectrum analyzer (power vs frequency) rendering
# ===========================================================================

def _draw_fcc_tone_overlay(ax, freqs_mhz: np.ndarray, avg_dB: np.ndarray,
                           bin_hz: float) -> None:
    """Overlay an FCC 15.247 FHSS check on the strongest tone in ``avg_dB``.

    Only draws when a tone stands at least ``FCC_OVERLAY_MIN_SNR_DB`` above the
    noise-floor median. Aligned to the detected peak, it shows the allocated
    channel band (``config.CHANNEL_SPACING``) and the 20 dB bandwidth, and
    checks §15.247(a)(1): carrier separation must be >= 2/3 of the 20 dB
    bandwidth (equivalently, 20 dB BW <= 1.5 x channel spacing).
    """
    noise_dB = float(np.median(avg_dB))
    peak_idx = int(np.argmax(avg_dB))
    peak_dB = float(avg_dB[peak_idx])
    if peak_dB - noise_dB < config.FCC_OVERLAY_MIN_SNR_DB:
        return  # no strong tone -> no overlay

    peak_mhz = float(freqs_mhz[peak_idx])

    # 20 dB-down bandwidth: walk out from the peak until the trace drops 20 dB.
    thr_dB = peak_dB - 20.0
    li = peak_idx
    while li > 0 and avg_dB[li] > thr_dB:
        li -= 1
    ri = peak_idx
    while ri < len(avg_dB) - 1 and avg_dB[ri] > thr_dB:
        ri += 1
    bw_hz = (ri - li) * bin_hz

    spacing_hz = config.CHANNEL_SPACING
    spacing_ok = (2.0 / 3.0) * bw_hz <= spacing_hz

    pass_col, fail_col = "#4ade80", "#f87171"
    # Distinct magenta accent for the carrier geometry so the marker doesn't
    # blend with the cyan average / orange peak-hold traces.
    accent = "#ff5ec4"
    spacing_col = pass_col if spacing_ok else fail_col

    half_ch_mhz = (spacing_hz / 2.0) / 1e6
    ax.axvspan(peak_mhz - half_ch_mhz, peak_mhz + half_ch_mhz,
               color=spacing_col, alpha=0.14, zorder=1)
    ax.axvline(peak_mhz, color=accent, linewidth=2.0, alpha=1.0, zorder=6)
    ax.text(peak_mhz, ax.get_ylim()[1], " carrier", color=accent, fontsize=8,
            fontweight="bold", va="top", ha="left", fontfamily="monospace",
            zorder=7)
    ax.hlines(thr_dB, freqs_mhz[li], freqs_mhz[ri], colors=accent,
              linewidth=1.4, zorder=6)
    for xf in (freqs_mhz[li], freqs_mhz[ri]):
        ax.plot([xf], [thr_dB], marker="|", markersize=10,
                markeredgewidth=1.6, color=accent, zorder=6)

    # -- Spurious emissions: peaks elsewhere in the band vs the -N dBc limit ---
    limit_dbc = config.SPUR_LIMIT_DBC
    limit_dB = peak_dB - limit_dbc
    ax.axhline(limit_dB, color="#cbd5e1", linewidth=1.0, linestyle="--",
               alpha=0.6, zorder=4)
    ax.text(freqs_mhz[-1], limit_dB, f"-{limit_dbc:.0f} dBc ", color="#cbd5e1",
            fontsize=7, va="bottom", ha="right", fontfamily="monospace",
            alpha=0.85, zorder=4)

    sep_bins = max(1, int(config.SPUR_MIN_SEP_KHZ * 1e3 / bin_hz))
    cand, _ = find_peaks(
        avg_dB,
        height=noise_dB + config.SPUR_MIN_SNR_DB,
        distance=sep_bins,
        prominence=config.SPUR_MIN_PROMINENCE_DB,
    )
    # Exclude the carrier and its own skirt/channel from the spur list.
    guard = max(peak_idx - li, ri - peak_idx, int(spacing_hz / 2 / bin_hz)) + 3
    spurs = [int(p) for p in cand if abs(int(p) - peak_idx) > guard]

    n_spur_fail = 0
    worst_dbc = None
    for p in spurs:
        dbc = float(avg_dB[p]) - peak_dB  # negative: dB below carrier
        if worst_dbc is None or dbc > worst_dbc:
            worst_dbc = dbc
        spur_ok = dbc <= -limit_dbc
        if not spur_ok:
            n_spur_fail += 1
        scol = pass_col if spur_ok else fail_col
        ax.plot([freqs_mhz[p]], [avg_dB[p]], marker="v", markersize=7,
                color=scol, zorder=6)
        ax.text(freqs_mhz[p], avg_dB[p] + 1.5, f"{dbc:.0f}", color=scol,
                fontsize=7, va="bottom", ha="center", fontfamily="monospace",
                zorder=6)

    overall_ok = spacing_ok and n_spur_fail == 0
    box_col = pass_col if overall_ok else fail_col

    def _row(label: str, value: str) -> str:
        return f"{label:<9}{value}"

    lines = [
        "FCC 15.247 compliance",
        _row("carrier", f"{peak_mhz:.5f} MHz  {peak_dB:.1f} dB"),
        _row("20 dB BW", f"{bw_hz / 1e3:.2f} kHz"),
        _row("spacing", f"{'PASS' if spacing_ok else 'FAIL'}  "
                        f"(2/3 BW <= {spacing_hz / 1e3:.2f} kHz)"),
    ]
    if spurs:
        lines.append(_row("spurs", f"{len(spurs)} found, worst {worst_dbc:.1f} dBc"))
        lines.append(_row("spur att", f"{'PASS' if n_spur_fail == 0 else 'FAIL'}  "
                          + (f"(limit -{limit_dbc:.0f} dBc)" if n_spur_fail == 0
                             else f"({n_spur_fail} over -{limit_dbc:.0f} dBc)")))
    else:
        lines.append(_row("spurs", "none"))
        lines.append(_row("spur att", f"PASS  (limit -{limit_dbc:.0f} dBc)"))
    ax.text(0.01, 0.97, "\n".join(lines), transform=ax.transAxes,
            fontsize=8, color=box_col, fontfamily="monospace",
            va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="#1a1a2e",
                      edgecolor=box_col, alpha=0.92), zorder=7)


def render_spectrum_image(chunks: list[np.ndarray], lo_freq_hz: float) -> bytes:
    """Render a spectrum-analyzer trace (power vs absolute frequency).

    Collapses the same Sxx_dB chunks used by the spectrogram over the time
    axis into an *average* trace (proper linear-power mean) plus a *peak-hold*
    trace, so the frequency of interest (``lo_freq_hz``) sits at the centre and
    the captured band spans the full width of the view.  Returns JPEG bytes so
    it can be served through the existing ``/spectrogram.jpg`` slot.
    """
    if not chunks:
        return b""

    # Average over a shorter window than the spectrogram so the trace reacts
    # faster to changes.
    chunks = chunks[-config.SPECTRUM_AVG_CHUNKS:]
    Sxx_dB = np.concatenate(chunks, axis=1)
    n_bins = Sxx_dB.shape[0]

    # Average in the linear-power domain (mean of dB understates real power),
    # and hold the max over the visible window as a peak trace.
    lin = np.power(10.0, Sxx_dB / 10.0)
    avg_dB = 10.0 * np.log10(np.mean(lin, axis=1) + 1e-12)
    peak_dB = np.max(Sxx_dB, axis=1)

    # compute_spec_chunk zeroes the DC bin (a -120 dB notch at the exact centre
    # frequency) and LO leakage smears into a few neighbours; interpolate across
    # +-SPEC_DC_NOTCH_BINS so the trace isn't a spike/notch at our frequency of
    # interest. Cosmetic only -- these traces never feed the decoder.
    _interpolate_dc(avg_dB, config.SPEC_DC_NOTCH_BINS)
    _interpolate_dc(peak_dB, config.SPEC_DC_NOTCH_BINS)

    # Frequency axis: bins run -fs/2 .. +fs/2 (ascending), centred on the LO.
    fs = config.SAMPLE_RATE
    freqs_mhz = (np.linspace(-fs / 2.0, fs / 2.0, n_bins) + lo_freq_hz) / 1e6
    center_mhz = lo_freq_hz / 1e6

    dpi = 100
    fig = Figure(
        figsize=(config.SPEC_IMG_WIDTH / dpi, (config.SPEC_IMG_HEIGHT + 60) / dpi),
        dpi=dpi, facecolor="#0f0f23",
    )
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1)
    ax.set_facecolor("#1a1a2e")

    ax.plot(freqs_mhz, peak_dB, color="#f59e0b", linewidth=0.8, alpha=0.55,
            label="peak hold")
    ax.plot(freqs_mhz, avg_dB, color="#7fdbca", linewidth=1.3, label="average")

    ax.axvline(center_mhz, color="#3399ff", linewidth=1.2, linestyle="--",
               alpha=0.9)

    y_lo = float(np.percentile(avg_dB, 2)) - 5.0
    y_hi = float(np.max(peak_dB)) + 5.0
    if y_hi <= y_lo:
        y_hi = y_lo + 1.0
    ax.set_ylim(y_lo, y_hi)
    ax.set_xlim(freqs_mhz[0], freqs_mhz[-1])

    # FCC compliance overlay locked onto the strongest tone (skipped if none).
    _draw_fcc_tone_overlay(ax, freqs_mhz, avg_dB, fs / n_bins)

    ax.text(center_mhz, y_hi, f" {center_mhz:.5f} MHz", color="#3399ff",
            fontsize=8, fontweight="bold", va="top", ha="left",
            fontfamily="monospace")

    ax.set_xlabel("Frequency (MHz)", color="#ccc", fontsize=9)
    ax.set_ylabel("Power (dB)", color="#ccc", fontsize=9)
    ax.tick_params(colors="#888", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333")
    ax.grid(True, color="#333", linewidth=0.3, alpha=0.6)
    ax.legend(loc="upper right", fontsize=8, facecolor="#1a1a2e",
              edgecolor="#333", labelcolor="#ccc")

    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    canvas.print_jpg(buf)
    buf.seek(0)
    return buf.read()


# ===========================================================================
# Time-domain plot rendering
# ===========================================================================


def _draw_decoder_overlay(ax, decode_info: dict):
    """Draw start line, hop boundaries, and expected channel F0 on spectrogram."""
    sr = config.SAMPLE_RATE
    start = decode_info["start_sample"]
    slot = config.slot_samples[1]["slot"]
    F0 = decode_info.get("F0_hz")
    channel_num = decode_info.get("channel_num")
    hop_seq_idx = decode_info.get("hop_seq_idx")
    chipset = decode_info.get("chipset")
    num_pdu = decode_info.get("num_pdu_symbols", 0)

    if F0 is None or channel_num is None or hop_seq_idx is None:
        return
    if hop_seq_idx >= len(config.HOPPING_SEQS):
        return

    synth_res = config.SYNTH_RES.get(chipset, 400.0)
    q_step = round(config.CHANNEL_SPACING / abs(synth_res)) * abs(synth_res)
    hop_seq = config.HOPPING_SEQS[hop_seq_idx]
    try:
        hop_index = hop_seq.index(channel_num)
    except ValueError:
        return

    preamble_len = config.PREAMBLE_LEN
    header_len = config.NUM_HEADER_SYMS
    sym_per_hop = config.NUM_SYM_PER_HOP
    total_syms = preamble_len + header_len + num_pdu
    half_sym = config.samples_per_symbol / 2

    # Signal start line (center of the first FFT window)
    t_start_ms = (start + half_sym) / sr * 1e3
    ax.axvline(t_start_ms, color="#3399ff", linewidth=2.5, linestyle="--",
               alpha=1.0, label="start")
    ax.text(t_start_ms, ax.get_ylim()[1], " start", color="#3399ff",
            fontsize=8, fontweight="bold", va="top", ha="left",
            fontfamily="monospace")

    # Walk through hops and draw boundaries + channel F0 lines
    cur_ch = channel_num
    f0_cur = F0
    hop_boundary_syms = set()
    segments = []  # (sym_start, sym_end, channel, f0)
    seg_start = 0

    for sym_abs in range(total_syms + 1):
        nxt = hop_seq[
            (hop_index + sym_abs // sym_per_hop) % len(hop_seq)
        ]
        if nxt != cur_ch:
            segments.append((seg_start, sym_abs, cur_ch, f0_cur))
            hop_boundary_syms.add(sym_abs)
            f0_cur += (nxt - cur_ch) * q_step
            cur_ch = nxt
            seg_start = sym_abs
    segments.append((seg_start, total_syms, cur_ch, f0_cur))

    color = "#3399ff"

    for i, (s_sym, e_sym, ch, f0_ch) in enumerate(segments):
        t0_ms = (start + s_sym * slot + half_sym) / sr * 1e3
        t1_ms = (start + e_sym * slot + half_sym) / sr * 1e3

        # Hop boundary vertical line
        if s_sym in hop_boundary_syms:
            ax.axvline(t0_ms, color=color, linewidth=2.0, linestyle=":",
                       alpha=1.0)

        # Horizontal line at F0 (bin 0) for this channel
        f0_khz = f0_ch / 1e3
        f63_khz = (f0_ch + 63 * abs(synth_res)) / 1e3
        ax.hlines(f0_khz, t0_ms, t1_ms, colors=color, linewidth=2.5,
                  alpha=1.0, linestyle="-")
        ax.hlines(f63_khz, t0_ms, t1_ms, colors=color, linewidth=2.0,
                  alpha=0.9, linestyle="--")
        ax.text(t0_ms + 0.5, f63_khz + 2, f"ch{ch}",
                color=color, fontsize=8, fontweight="bold",
                fontfamily="monospace", va="bottom", ha="left", alpha=1.0)


def render_symbol_zoom_plot(
    iq_segment: np.ndarray,
    decode_info: dict | None = None,
    n_symbols: int = 6,
) -> bytes:
    """Magnified view of the last N preamble symbols with rise/fall time annotations."""
    if decode_info is None or decode_info.get("start_sample") is None:
        return b""

    sr = config.SAMPLE_RATE
    sym_len = config.samples_per_symbol
    slot = config.slot_samples[1]["slot"]
    preamble_len = config.PREAMBLE_LEN

    start_sample = decode_info["start_sample"]
    n_sym = min(max(1, n_symbols), preamble_len)
    # Show the last n_sym symbols
    sym_offset = preamble_len - n_sym

    first_sym_abs = start_sample + sym_offset * slot
    last_sym_end_abs = start_sample + preamble_len * slot
    # Half-symbol margin on each side so edge transitions aren't clipped
    margin = sym_len // 2

    view_start = max(0, first_sym_abs - margin)
    view_end = min(len(iq_segment), last_sym_end_abs + margin)
    if view_end <= view_start or view_end > len(iq_segment):
        return b""

    zoom_seg = iq_segment[view_start:view_end]
    n = len(zoom_seg)
    if n < sym_len:
        return b""

    t_us = np.arange(n) / sr * 1e6
    mag = np.abs(zoom_seg)
    mag_dbfs = 20.0 * np.log10(np.clip(mag, 1e-12, None) / config.ADC_FULL_SCALE)

    gap_samples = slot - sym_len
    # Half the inter-symbol gap to center selected symbols
    meas_margin = gap_samples // 2

    # Correct symbol positions using global envelope crossings + chaining
    edges = correct_symbol_edges(
        zoom_seg, start_sample, view_start, n_sym, sym_offset, slot, sym_len,
    )

    # Measure 10%-90% rise/fall time at each corrected edge
    sym_infos = []
    for k, (s, e) in enumerate(edges):
        rise_us = measure_transition_us(zoom_seg, s - meas_margin, s + meas_margin, sr, rise=True)
        fall_us = measure_transition_us(zoom_seg, e - meas_margin, e + meas_margin, sr, rise=False)
        sym_infos.append({
            "preamble_idx": sym_offset + k,
            "s_us": s / sr * 1e6,
            "e_us": e / sr * 1e6,
            "rise_us": rise_us,
            "fall_us": fall_us,
        })

    # Layout - top is envelope magnitude, bottom is spectrogram (shared x-axis)
    fig = Figure(figsize=(12, 7), dpi=100, facecolor="#0f0f23")
    canvas = FigureCanvasAgg(fig)
    ax_td = fig.add_subplot(2, 1, 1)
    ax_sg = fig.add_subplot(2, 1, 2, sharex=ax_td)

    # Show symbol edges at 0.5 at 80% opacity
    ax_td.set_facecolor("#1a1a2e")
    ax_td.plot(t_us, mag_dbfs, color="#7fdbca", linewidth=0.5, alpha=0.8)

    # Floor is about 40 dBFS below symbol.
    sig_peak = float(np.max(mag_dbfs)) if len(mag_dbfs) else -10.0
    y_floor = max(-80.0, sig_peak - 40.0)
    ax_td.set_ylim(y_floor, 2.0)
    y_mid = (sig_peak + y_floor) / 2.0

    for si in sym_infos:
        s_us, e_us = si["s_us"], si["e_us"]
        # Shaded region shows the symbol body; vertical lines mark the edges
        ax_td.axvspan(s_us, e_us, alpha=0.10, color="#22d3ee")
        ax_td.axvline(s_us, color="#22d3ee", linewidth=0.8, alpha=0.5)
        ax_td.axvline(e_us, color="#22d3ee", linewidth=0.8, alpha=0.5)

        # Symbol index label centred vertically in the body
        ax_td.text(
            (s_us + e_us) / 2, y_mid,
            f"P{si['preamble_idx']}",
            ha="center", va="center", fontsize=9, color="#e2e8f0",
            fontfamily="monospace", fontweight="bold", rotation=90,
        )

        # Drawing lines on the rise/fall edges
        if si["rise_us"] is not None:
            ax_td.text(
                s_us, sig_peak - 1,
                f"↑{si['rise_us']:.1f}µs",
                ha="left", va="top", fontsize=7, color="#4ade80",
                fontfamily="monospace",
            )
        if si["fall_us"] is not None:
            ax_td.text(
                e_us, sig_peak - 7,
                f"↓{si['fall_us']:.1f}µs",
                ha="right", va="top", fontsize=7, color="#f87171",
                fontfamily="monospace",
            )

    # Aggregate rise/fall stats box in the top-right corner
    rise_vals = [si["rise_us"] for si in sym_infos if si["rise_us"] is not None]
    fall_vals = [si["fall_us"] for si in sym_infos if si["fall_us"] is not None]
    stats_lines = []
    if rise_vals:
        stats_lines.append(
            f"Rise  mean={np.mean(rise_vals):.1f}µs  "
            f"std={np.std(rise_vals):.1f}µs  "
            f"min={np.min(rise_vals):.1f}  max={np.max(rise_vals):.1f}"
        )
    if fall_vals:
        stats_lines.append(
            f"Fall  mean={np.mean(fall_vals):.1f}µs  "
            f"std={np.std(fall_vals):.1f}µs  "
            f"min={np.min(fall_vals):.1f}  max={np.max(fall_vals):.1f}"
        )
    if stats_lines:
        ax_td.text(
            0.99, 0.97, "\n".join(stats_lines), transform=ax_td.transAxes,
            fontsize=8, color="#7fdbca", fontfamily="monospace",
            va="top", ha="right",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#1a1a2e",
                      edgecolor="#333", alpha=0.92),
        )

    # Build panels for the spectrogram and time-domain plot, sharing the same x-axis.
    title = f"Preamble symbols {sym_offset}–{preamble_len - 1} (last {n_sym})"
    if decode_info.get("chipset"):
        title += f"  |  {decode_info['chipset']}"
    ax_td.set_title(title, color="#ccc", fontsize=9, fontfamily="monospace")
    ax_td.set_ylabel("Magnitude (dBFS)", color="#ccc")
    ax_td.tick_params(colors="#888", labelbottom=False)
    for spine in ax_td.spines.values():
        spine.set_color("#333")
    ax_td.grid(True, color="#333", linewidth=0.3, alpha=0.5)
    ax_td.set_xlim(t_us[0], t_us[-1])

    ax_sg.set_facecolor("#1a1a2e")

    # Window size scales with segment length so short and long captures both get
    # reasonable time/frequency resolution. 75% overlap smooths the time axis
    nperseg_sg = min(128, n // 4) if n > 128 else max(16, n // 2)
    noverlap_sg = nperseg_sg * 3 // 4

    # scipy_spectrogram slices zoom_seg into overlapping windows, FFTs each one,
    # and returns Sxx: a 2D power grid (freq bins × time bins)
    f_sg, t_sg, Sxx = scipy_spectrogram(
        zoom_seg, fs=sr,
        nperseg=nperseg_sg, noverlap=noverlap_sg, return_onesided=False,
    )
    # fftshift reorders bins so the FSK tone sits centred on screen instead of
    # split across the top and bottom edges
    f_sg = np.fft.fftshift(f_sg)
    Sxx = np.fft.fftshift(Sxx, axes=0)
    Sxx_dB = 10.0 * np.log10(Sxx + 1e-12)
    t_sg_us = t_sg * 1e6
    f_sg_khz = f_sg / 1e3

    # Clip colour range so a single noise spike doesn't wash out spectrogram
    plow, phigh = np.percentile(Sxx_dB, [2, 99.5])
    if phigh <= plow:
        phigh = plow + 1.0
    # pcolormesh paints the spectrogram x=time, y=frequency, colour=power
    ax_sg.pcolormesh(t_sg_us, f_sg_khz, Sxx_dB, vmin=plow, vmax=phigh,
                     cmap="viridis", shading="auto")
    ax_sg.set_xlabel("Time (µs)", color="#ccc")
    ax_sg.set_ylabel("Freq (kHz)", color="#ccc")
    ax_sg.tick_params(colors="#888")
    for spine in ax_sg.spines.values():
        spine.set_color("#333")
    ax_sg.set_xlim(t_us[0], t_us[-1])

    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)
    return buf.read()


def render_td_plot(
    iq_segment: np.ndarray, decode_info: dict | None = None
) -> tuple[bytes, dict]:
    """Render a time-domain magnitude plot + spectrogram with annotations.

    Returns ``(image_bytes, stats)`` where *stats* contains symbol and gap
    duration statistics derived from detected symbol edges (protocol-corrected
    when ``decode_info.start_sample`` is available; otherwise envelope-threshold
    based): ``sym_count``, ``sym_mean_ms``, ``sym_std_ms``,
    ``gap_count``, ``gap_mean_ms``, ``gap_std_ms``.
    """
    n = len(iq_segment)
    t_ms = np.arange(n) / config.SAMPLE_RATE * 1e3
    mag = np.abs(iq_segment)

    mag_dbfs = 20.0 * np.log10(np.clip(mag, 1e-12, None) / config.ADC_FULL_SCALE)
    DBFS_FLOOR = -80.0

    # Symbol edge detection: use protocol-aware correction when decode_info
    # has start_sample (same method as the zoomed symbol plot), otherwise fall
    # back to envelope threshold detection.
    starts = np.array([], dtype=int)
    ends = np.array([], dtype=int)

    if decode_info is not None and decode_info.get("start_sample") is not None:
        sym_len = config.samples_per_symbol
        phy_ver = decode_info.get("phy_ver", 1)
        slot = config.slot_samples[phy_ver]["slot"]
        n_sym = (config.PREAMBLE_LEN
                 + config.NUM_HEADER_SYMS
                 + (decode_info.get("num_pdu_symbols") or 0))
        corrected = correct_symbol_edges(
            iq_segment, decode_info["start_sample"], 0, n_sym, 0, slot, sym_len,
        )
        if corrected:
            starts = np.array([s for s, _e in corrected])
            ends = np.array([e for _s, e in corrected])

    if len(starts) == 0:
        # Fallback: envelope threshold detection
        # Smooth with a 0.3ms boxcar to merge intra-symbol ripple without
        # smearing the 800us inter-symbol gaps
        win_samples = max(1, int(0.3e-3 * config.SAMPLE_RATE))
        envelope = np.convolve(mag, np.ones(win_samples) / win_samples, mode="same")
        # Threshold at 40% of the noise-to-peak dynamic range so weak packets
        # still register without false triggers from noise
        noise_floor = np.percentile(envelope, 10)
        signal_peak = np.percentile(envelope, 95)
        thresh = noise_floor + 0.4 * (signal_peak - noise_floor)
        above = envelope > thresh
        padded = np.concatenate([[False], above, [False]])
        diff = np.diff(padded.astype(np.int8))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]
        # Drop glitches shorter than 2ms — real FSK symbols are 8ms
        min_sym = int(2e-3 * config.SAMPLE_RATE)
        mask = (ends - starts) >= min_sym
        starts, ends = starts[mask], ends[mask]
        # Merge segments separated by less than 0.1ms; the boxcar smoothing can
        # split a single symbol into multiple runs if there's a deep mid-symbol dip
        min_gap = int(0.1e-3 * config.SAMPLE_RATE)
        m_starts, m_ends = [], []
        for s, e in zip(starts, ends):
            if m_ends and (s - m_ends[-1]) < min_gap:
                m_ends[-1] = e
            else:
                m_starts.append(s)
                m_ends.append(e)
        starts, ends = np.array(m_starts), np.array(m_ends)

    sym_dur_ms = (ends - starts) / config.SAMPLE_RATE * 1e3
    gap_starts = ends[:-1]
    gap_ends = starts[1:]
    gap_dur_ms = (gap_ends - gap_starts) / config.SAMPLE_RATE * 1e3

    # -- Plotting (2 subplots: time domain + spectrogram) ------------------
    fig = Figure(figsize=(12, 7), dpi=100, facecolor="#0f0f23")
    canvas = FigureCanvasAgg(fig)
    ax_td = fig.add_subplot(2, 1, 1)
    ax_sg = fig.add_subplot(2, 1, 2, sharex=ax_td)

    # --- Top: time-domain magnitude ---
    ax_td.set_facecolor("#1a1a2e")
    ax_td.plot(t_ms, mag_dbfs, color="#7fdbca", linewidth=0.4, alpha=0.7)

    sig_peak_dbfs = np.max(mag_dbfs) if len(mag_dbfs) else -10.0
    y_floor = max(DBFS_FLOOR, sig_peak_dbfs - 60)
    ax_td.set_ylim(y_floor, 0)

    # FFT-based per-symbol tone frequency: blank the DC bin (bottom 2%) so
    # IQ imbalance spurs don't eclipse the real FSK carrier
    sym_freqs = []
    for s, e in zip(starts, ends):
        sym_iq = iq_segment[s:e]
        spec = np.fft.fft(sym_iq)
        psd = np.abs(spec) ** 2
        freqs = np.fft.fftfreq(len(sym_iq), d=1.0 / config.SAMPLE_RATE)
        dc_zone = int(len(psd) * 0.02)
        if dc_zone > 0:
            psd[:dc_zone] = 0
            psd[-dc_zone:] = 0
        pk = np.argmax(psd)
        sym_freqs.append(freqs[pk])

    # F0 reference: use the decoder's measured value if available (most accurate),
    # otherwise fall back to the second detected symbol (first is the F63 preamble tone)
    if decode_info and decode_info.get("F0_hz") is not None:
        f0 = decode_info["F0_hz"]
    else:
        f0 = sym_freqs[1] if len(sym_freqs) > 1 else (sym_freqs[0] if sym_freqs else 0.0)

    # Annotate each symbol: cyan shaded body + frequency label.
    # sym[0] = F63 preamble (absolute freq); sym[1] = F0 reference;
    # all subsequent symbols show their offset from F0 in Hz
    for i, (s, e) in enumerate(zip(starts, ends)):
        t0 = s / config.SAMPLE_RATE * 1e3
        t1 = e / config.SAMPLE_RATE * 1e3
        ax_td.axvspan(t0, t1, alpha=0.08, color="#22d3ee")
        ax_td.axvline(t0, color="#22d3ee", linewidth=0.5, alpha=0.4)
        ax_td.axvline(t1, color="#22d3ee", linewidth=0.5, alpha=0.4)
        df = sym_freqs[i] - f0
        if i == 0:
            lbl = f"F63={sym_freqs[i]:.0f}"
        elif i == 1:
            lbl = f"F0={f0:.0f}"
        else:
            lbl = f"{df:+.0f}"
        y_mid = (0 + y_floor) / 2
        ax_td.text(
            (t0 + t1) / 2, y_mid, lbl,
            ha="center", va="center", fontsize=8, color="#e2e8f0",
            fontfamily="monospace", fontweight="bold", rotation=90,
        )

    # Red shading over inter-symbol gaps so gap width irregularities are obvious
    for i in range(len(gap_dur_ms)):
        t0 = gap_starts[i] / config.SAMPLE_RATE * 1e3
        t1 = gap_ends[i] / config.SAMPLE_RATE * 1e3
        ax_td.axvspan(t0, t1, alpha=0.12, color="#f87171")

    # Stats box: symbol/gap means + std, plus cumulative drift vs expected 8.8ms slot.
    # Drift = (actual first-to-last span) - (n_periods × 8.8ms); positive = TX clock fast
    EXPECTED_PERIOD_MS = 8.8
    lines = []
    if len(sym_dur_ms):
        lines.append(
            f"Symbols   {len(sym_dur_ms):>3d}   "
            f"mean {np.mean(sym_dur_ms):5.2f} ms   "
            f"std {np.std(sym_dur_ms):5.3f} ms"
        )
    if len(gap_dur_ms):
        lines.append(
            f"Gaps      {len(gap_dur_ms):>3d}   "
            f"mean {np.mean(gap_dur_ms):5.2f} ms   "
            f"std {np.std(gap_dur_ms):5.3f} ms"
        )
    if len(starts) >= 2:
        sym_starts_ms = starts / config.SAMPLE_RATE * 1e3
        n_periods = len(sym_starts_ms) - 1
        actual_span = sym_starts_ms[-1] - sym_starts_ms[0]
        expected_span = n_periods * EXPECTED_PERIOD_MS
        drift_ms = actual_span - expected_span
        lines.append(
            f"Drift     {drift_ms:+.3f} ms over {n_periods} periods "
            f"({drift_ms / n_periods * 1e3:+.1f} \u00b5s/period)"
        )
    if lines:
        ax_td.text(
            0.99, 0.97, "\n".join(lines), transform=ax_td.transAxes,
            fontsize=8, color="#7fdbca", fontfamily="monospace",
            va="top", ha="right",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#1a1a2e",
                      edgecolor="#333", alpha=0.92),
        )

    ax_td.set_ylabel("ABS (dBFS)", color="#ccc")
    ax_td.tick_params(colors="#888", labelbottom=False)
    for spine in ax_td.spines.values():
        spine.set_color("#333")
    ax_td.grid(True, color="#333", linewidth=0.3, alpha=0.5)
    ax_td.set_xlim(t_ms[0], t_ms[-1])

    # Decode info box in top-left: cyan border on success, red on failure.
    # Shows seq number, network ID, SNR, synth resolution, header correlation,
    # and PDU head bytes (the last, most useful for debugging a pdu_fail)
    if decode_info:
        di_lines = []
        if decode_info.get("decoded"):
            di_lines.append("DECODED OK")
            if decode_info.get("seq_num") is not None:
                di_lines.append(f"seq={decode_info['seq_num']}")
            if decode_info.get("ntw_id") is not None:
                di_lines.append(f"id=0x{decode_info['ntw_id']:08X}")
        else:
            di_lines.append(f"DECODE FAILED: {decode_info.get('reason', '?')}")

        if decode_info.get("chipset"):
            di_lines.append(f"chipset: {decode_info['chipset']}")
        if decode_info.get("energy_dB") is not None:
            di_lines.append(f"energy: {decode_info['energy_dB']:.1f} dBFS")

        if decode_info.get("F63_snr") is not None:
            di_lines.append(
                f"SNR={decode_info['F63_snr']:.1f}  "
                f"synth_res={decode_info.get('measured_synth_res', '?')} Hz"
            )
        if decode_info.get("F0_hz") is not None:
            di_lines.append(f"F0={decode_info['F0_hz']:.0f} Hz")
        if decode_info.get("header_syms") is not None:
            di_lines.append(f"hdr_syms={decode_info['header_syms']}")
        if decode_info.get("header_n_corr") is not None:
            di_lines.append(
                f"hdr_corr={decode_info['header_n_corr']}  "
                f"ch={decode_info.get('channel_num', '?')}  "
                f"hop={decode_info.get('hop_seq_idx', '?')}  "
                f"pdu_len={decode_info.get('num_pdu_symbols', '?')}"
            )
        reason = decode_info.get("reason", "")
        if reason == "pdu_fail" and decode_info.get("pdu_syms_head") is not None:
            di_lines.append(f"pdu[0:10]={decode_info['pdu_syms_head']}")

        di_color = "#22d3ee" if decode_info.get("decoded") else "#f87171"
        ax_td.text(
            0.01, 0.97, "\n".join(di_lines), transform=ax_td.transAxes,
            fontsize=8, color=di_color, fontfamily="monospace",
            va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#1a1a2e",
                      edgecolor=di_color, alpha=0.92),
        )

    # --- Bottom: spectrogram ---
    # fftshift reorders the one-sided FFT bins into -fs/2…+fs/2 so the
    # FSK tone pair appears centred around baseband rather than split at edges
    ax_sg.set_facecolor("#1a1a2e")
    nperseg_td = min(256, n // 4) if n > 256 else max(16, n // 2)
    noverlap_td = nperseg_td * 3 // 4
    f_sg, t_sg, Sxx = scipy_spectrogram(
        iq_segment, fs=config.SAMPLE_RATE,
        nperseg=nperseg_td, noverlap=noverlap_td, return_onesided=False,
    )
    f_sg = np.fft.fftshift(f_sg)
    Sxx = np.fft.fftshift(Sxx, axes=0)
    Sxx_dB = 10.0 * np.log10(Sxx + 1e-12)
    t_sg_ms = t_sg * 1e3
    f_sg_khz = f_sg / 1e3

    # Clip color range to 2nd–99.5th percentile so a single hot pixel doesn't
    # wash out the color scale
    plow, phigh = np.percentile(Sxx_dB, [2, 99.5])
    if phigh <= plow:
        phigh = plow + 1.0

    ax_sg.pcolormesh(
        t_sg_ms, f_sg_khz, Sxx_dB,
        vmin=plow, vmax=phigh, cmap="viridis", shading="auto",
    )
    ax_sg.set_xlabel("Time (ms)", color="#ccc")
    ax_sg.set_ylabel("Freq (kHz)", color="#ccc")
    ax_sg.tick_params(colors="#888")
    for spine in ax_sg.spines.values():
        spine.set_color("#333")
    ax_sg.set_xlim(t_ms[0], t_ms[-1])

    # --- Decoder overlay: start, hop boundaries, expected channel F0 ---
    if decode_info and decode_info.get("start_sample") is not None:
        _draw_decoder_overlay(ax_sg, decode_info)

    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)

    stats: dict = {
        "sym_count": int(len(sym_dur_ms)),
        "sym_mean_ms": float(round(np.mean(sym_dur_ms), 4)) if len(sym_dur_ms) else None,
        "sym_std_ms": float(round(np.std(sym_dur_ms), 4)) if len(sym_dur_ms) else None,
        "gap_count": int(len(gap_dur_ms)),
        "gap_mean_ms": float(round(np.mean(gap_dur_ms), 4)) if len(gap_dur_ms) else None,
        "gap_std_ms": float(round(np.std(gap_dur_ms), 4)) if len(gap_dur_ms) else None,
    }
    return buf.read(), stats
