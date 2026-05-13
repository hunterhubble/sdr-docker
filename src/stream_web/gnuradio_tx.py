"""GNU Radio flowgraph for SDR TX -- SoapySDR-based, full-duplex with RX.

Uses gr-soapy so the same code works with PlutoSDR, bladeRF, or any
SoapySDR-supported device.  The TX flowgraph runs independently of the
RX flowgraph (separate ``gr.top_block`` instance), enabling simultaneous
transmit and receive.

Two modes are supported:

* **Tone mode** — transmit a CW carrier (constant-envelope signal).
* **Packet mode** — play back an IQ file (e.g. a pre-generated packet
  waveform), optionally repeating.
"""

from __future__ import annotations

import os
import threading

from gnuradio import analog, blocks, gr, soapy

from . import config
from .gnuradio_rx import _soapy_driver_args

# ---------------------------------------------------------------------------
# TX config defaults
# ---------------------------------------------------------------------------

TX_DEFAULT_FREQ_HZ: int = config.CENTER_FREQ_HZ
TX_DEFAULT_ATTN_DB: float = 30.0
TX_SAMPLE_RATE: int = config.SAMPLE_RATE
TX_BANDWIDTH: int = config.RF_BANDWIDTH
TX_SOURCE_DIR: str = os.environ.get(
    "TX_SOURCE_DIR", os.path.join(os.path.dirname(__file__), "source_files")
)
os.makedirs(TX_SOURCE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# TX Flowgraph
# ---------------------------------------------------------------------------

def _tx_sink_gain_db(attn_db: float) -> float:
    """Soapy sink ``set_gain(0, value)``: Pluto uses fake gain from attenuation; VSG60 uses dBm."""
    if config.SDR_TYPE == "signalhound":
        lo = config.SIGNALHOUND_TX_DBM_MIN
        hi = config.SIGNALHOUND_TX_DBM_MAX
        t = min(1.0, max(0.0, float(attn_db) / 89.75))
        return hi - t * (hi - lo)
    return 89.75 - attn_db


class TXFlowgraph(gr.top_block):
    """SoapySDR-based TX flowgraph with tone and packet-file modes."""

    def __init__(self):
        gr.top_block.__init__(self, "sdr_tx")

        dev_args = _soapy_driver_args()
        self._sink = soapy.sink(dev_args, "fc32", 1, "", "", [""], [""])
        self._sink.set_sample_rate(0, TX_SAMPLE_RATE)
        self._sink.set_frequency(0, TX_DEFAULT_FREQ_HZ)
        self._sink.set_bandwidth(0, TX_BANDWIDTH)
        self._sink.set_gain(0, _tx_sink_gain_db(TX_DEFAULT_ATTN_DB))

        self._source_block = None
        self._mode: str | None = None
        self._lock = threading.RLock()
        self._freq_hz: int = TX_DEFAULT_FREQ_HZ
        self._attn_db: float = TX_DEFAULT_ATTN_DB
        self._running = False

    # -- mode switching -----------------------------------------------------

    def tone_mode(self) -> None:
        """Configure a CW tone source (constant I+jQ = 1+0j)."""
        with self._lock:
            was_running = self._running
            if was_running:
                self.stop()
                self.wait()
            if self._source_block is not None:
                self.disconnect_all()
            src = analog.sig_source_c(0, analog.GR_CONST_WAVE, 0, 1, 0)
            self.connect(src, self._sink)
            self._source_block = src
            self._mode = "tone"
            if was_running:
                self.start()
                self._running = True

    def packet_mode(self, file_path: str, repeat: bool = True) -> None:
        """Configure playback of an IQ file (complex64).

        Parameters
        ----------
        file_path : str
            Absolute path to a raw complex64 IQ file.
        repeat : bool
            If True the file loops indefinitely.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"TX IQ file not found: {file_path}")
        with self._lock:
            was_running = self._running
            if was_running:
                self.stop()
                self.wait()
            if self._source_block is not None:
                self.disconnect_all()
            src = blocks.file_source(gr.sizeof_gr_complex, file_path, repeat, 0, 0)
            self.connect(src, self._sink)
            self._source_block = src
            self._mode = "packet"
            if was_running:
                self.start()
                self._running = True

    # -- start / stop -------------------------------------------------------

    def start(self) -> None:  # type: ignore[override]
        if self._source_block is None:
            raise RuntimeError("Set tone_mode() or packet_mode() before start()")
        with self._lock:
            if self._running:
                return
            super().start()
            self._running = True

    def stop(self) -> None:  # type: ignore[override]
        with self._lock:
            if not self._running:
                return
            super().stop()
            self.wait()
            self._running = False

    # -- runtime controls ---------------------------------------------------

    def set_frequency(self, freq_hz: int) -> None:
        self._sink.set_frequency(0, freq_hz)
        self._freq_hz = freq_hz

    def set_attn(self, attn_db: float) -> None:
        """Set TX attenuation (0 = max power, 89.75 = min power).

        For Pluto/bladeRF this maps to driver gain. For Signal Hound VSG60 the same
        knob maps linearly to output level between SIGNALHOUND_TX_DBM_MAX and MIN.
        """
        attn_db = max(0.0, min(89.75, attn_db))
        self._sink.set_gain(0, _tx_sink_gain_db(attn_db))
        self._attn_db = attn_db

    # -- status -------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def mode(self) -> str | None:
        return self._mode

    @property
    def freq_hz(self) -> int:
        return self._freq_hz

    @property
    def attn_db(self) -> float:
        return self._attn_db

    def status_dict(self) -> dict:
        return {
            "running": self._running,
            "mode": self._mode,
            "freq_hz": self._freq_hz,
            "attn_db": self._attn_db,
        }
