"""Configuration constants for the SDR stream web application.

Protocol constants (RS codes, hopping sequences, preamble patterns, etc.)
live in ``hubble_satnet_decoder.constants`` and are re-exported here for backward
compatibility.  SDR-specific and display-specific values are defined locally.
"""

import os

import hubble_satnet_decoder.constants as _fdc
from hubble_satnet_decoder.constants import (  # noqa: F401 — re-exported
    ADC_FULL_SCALE,
    DATA_LEN_VNEG1,
    DETECTION_THRESHOLD,
    F0_TOL,
    FREQ_STEP_VNEG1,
    GAP_DURATIONS,
    HOPPING_SEQS,
    LO_CHANNEL,
    MAX_RAW,
    MIN_ENERGY_DBFS,
    NFFT_DET,
    NFFT_VIS,
    NMS_FREQ_BINS,
    NMS_TIME_BINS,
    NOVERLAP_DET,
    NOVERLAP_VIS,
    NUM_CHANNELS,
    NUM_FSK_BINS,
    NUM_HEADER_SYMS,
    NUM_SYM_PER_HOP,
    PAYLOAD_LEN_BYTES_V1,
    PREAMBLE_BITS,
    PREAMBLE_CODE_V1,
    PREAMBLE_F0_SNR_MIN,
    PREAMBLE_LEN,
    RS_K_V1,
    RS_K_VNEG1,
    RS_N_V1,
    RS_N_VNEG1,
    SYMBOL_DURATION_S,
    SYMBOLS_PER_PACKET_VNEG1,
    SYNTH_RES,
    TEMPLATE_FREQ_BINS,
    TIME_TOL,
    bins_on,
    fft_freqs,
    off_indices_v1,
    on_indices_v1,
    preamble_off_idx,
    preamble_on_idx,
    samples_per_symbol,
    slot_samples,
    templates,
    time_step_s,
)

# -- SDR selection (override with environment variables) --------------------
# "pluto" (ADALM-PLUTO & PlutoPlus) or "bladerf"
SDR_TYPE = os.environ.get("SDR_TYPE", "pluto").lower()

# -- PlutoSDR connection (ignored when SDR_TYPE != "pluto") -----------------
PLUTO_URI = os.environ.get("PLUTO_URI", "ip:192.168.2.1")

# -- Radio parameters (shared across SDR backends) -------------------------
CENTER_FREQ_HZ = 2_482_440_375
SAMPLE_RATE = 781_250  # 6.25 MHz / 8
RX_BUFFER_SIZE = 2 ** 16  # ~84 ms per read
RF_BANDWIDTH = int(SAMPLE_RATE)
RX_GAIN_MODE = "manual"
RX_INITIAL_GAIN_DB = 20
RX_GAIN_MIN_DB = 0
RX_GAIN_STEP_DB = 2

if SDR_TYPE == "bladerf":
    RX_GAIN_MAX_DB = 60
else:
    RX_GAIN_MAX_DB = 71

# -- Spectrogram (visualisation) -------------------------------------------
SPEC_DURATION_S = 10.0
SPEC_CHUNK_S = 0.5
SPEC_CHUNK_SAMPLES = int(SPEC_CHUNK_S * SAMPLE_RATE)
MAX_SPEC_CHUNKS = int(SPEC_DURATION_S / SPEC_CHUNK_S)

# Spectrum-analyzer averaging window: number of recent 0.5 s chunks to average
# into the trace. Shorter than MAX_SPEC_CHUNKS (the full spectrogram window) so
# the spectrum reacts faster to changes -- half the window = ~2x faster.
SPECTRUM_AVG_CHUNKS = max(1, MAX_SPEC_CHUNKS // 2)

# IQ circular buffer: ~2 s for decode + headroom
IQ_BUFFER_DURATION_S = 2.0
IQ_BUFFER_SIZE = int(IQ_BUFFER_DURATION_S * SAMPLE_RATE)

# Target image size for web display
SPEC_IMG_WIDTH = 1200
SPEC_IMG_HEIGHT = 200

# Cosmetic only: interpolate across +-N FFT bins around DC to hide residual
# LO-leakage in the spectrogram/spectrum *display*. Operates on the rendered
# Sxx copy, never on the decoder's IQ, so it cannot affect decode. At
# NFFT_VIS=4096 each bin is ~191 Hz. The leakage skirt reaches ~10 bins out
# before it settles into the noise floor, so 10 bins (~+-1.9 kHz) is needed for
# the notch anchors to sit at noise -- still far narrower than a ~20 kHz FSK
# channel. Set to 0 to disable.
SPEC_DC_NOTCH_BINS = 10

# FCC-compliance overlay on the spectrum-analyzer view: draw an overlay on the
# strongest tone only when it rises at least this many dB above the noise-floor
# median (so a quiet band shows no overlay).
FCC_OVERLAY_MIN_SNR_DB = 15.0

# Spurious-emission detection on the spectrum-analyzer view. Peaks (other than
# the main carrier) that stand SPUR_MIN_SNR_DB above the noise floor are flagged
# and checked against SPUR_LIMIT_DBC: FCC 15.247(d) requires spurs to be at
# least 20 dB below the in-band carrier. SPUR_MIN_SEP_KHZ is the minimum
# frequency separation between distinct detected peaks; SPUR_MIN_PROMINENCE_DB
# rejects shoulders/sidelobes of stronger peaks.
SPUR_MIN_SNR_DB = 10.0
SPUR_MIN_PROMINENCE_DB = 6.0
SPUR_MIN_SEP_KHZ = 10.0
SPUR_LIMIT_DBC = 20.0

# -- Decoder scheduling ----------------------------------------------------
# Decode window is 2.5x the Decode interval, so packets that overlap
# the decode interval boundary are still decoded.
DECODE_WINDOW_S = 1.5

# Decode interval, set to 0.6, which is higher than our longest packet
# duration of 0.53s, with some margin
DECODE_INTERVAL_S = 0.6
DECODE_SAMPLES = int(DECODE_WINDOW_S * SAMPLE_RATE)

# Two decodes of the same packet (same device id, auth tag, payload) within this window are
# treated as one -- shared by the live processor and the offline record-analyze sweep.
DEDUP_START_TOL_S = 1.0

# -- Web server & app behaviour --------------------------------------------
FLASK_PORT = 8050
VERBOSE = False
MAX_DECODE_HISTORY = 200
SDR_RETRY_INTERVAL_S = 3

# -- Time-domain viewer ----------------------------------------------------
TD_WINDOW_S = 0.75

# Cap on pending-capture age: must stay below the buffer duration, else a
# stale hit's absolute position aliases back into the window (see processor.py).
_TD_MAX_AGE_S = 0.9 * IQ_BUFFER_DURATION_S  # 10% headroom for scheduling jitter

# Retry a not-yet-renderable hit across cycles for this long, then give up.
TD_PENDING_TIMEOUT_S = min(3.0, _TD_MAX_AGE_S)
# Hold a truncation-only failure this long to confirm it's real, not a
# window-boundary cutoff (see _TRUNCATION_REASONS in td_capture.py).
TD_TRUNCATION_CONFIRM_S = min(2.0, _TD_MAX_AGE_S)
# Tolerance for matching the same packet across cycles by start sample.
TD_TRUNCATION_MATCH_S = 0.01
# Padding around a decoded packet's span when suppressing phantom detections.
TD_PHANTOM_PAD_S = 0.02
# Fraction of the render window placed before the packet start (pre-roll), so
# the captured plot shows a little lead-in rather than starting exactly on it.
TD_PRE_ROLL_FRAC = 0.1

# -- Sync hubble_satnet_decoder with this SDR config -----------------------
_fdc.CHANNEL_SPACING = 25_750.0
_fdc.DEVICE_CHANNEL_SPACING = {
    name: round(_fdc.CHANNEL_SPACING / sr) * sr
    for name, sr in _fdc.SYNTH_RES.items()
}
CHANNEL_SPACING = _fdc.CHANNEL_SPACING
DEVICE_CHANNEL_SPACING = _fdc.DEVICE_CHANNEL_SPACING
_fdc.configure(SAMPLE_RATE)

