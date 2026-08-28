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
# Baseband offset applied to the CW tone so it lands 100 kHz above the LO
# (i.e. off the RX centre frequency) instead of sitting on top of it.
TX_TONE_OFFSET_HZ: int = 100_000
TX_SAMPLE_RATE: int = config.SAMPLE_RATE
TX_BANDWIDTH: int = config.RF_BANDWIDTH
TX_SOURCE_DIR: str = os.environ.get(
    "TX_SOURCE_DIR", os.path.join(os.path.dirname(__file__), "source_files")
)
os.makedirs(TX_SOURCE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# TX Flowgraph
# ---------------------------------------------------------------------------

class TXFlowgraph(gr.top_block):
    """SoapySDR-based TX flowgraph with tone and packet-file modes."""

    def __init__(self):
        gr.top_block.__init__(self, "sdr_tx")

        dev_args = _soapy_driver_args()
        self._sink = soapy.sink(dev_args, "fc32", 1, "", "", [""], [""])
        self._sink.set_sample_rate(0, TX_SAMPLE_RATE)
        self._sink.set_frequency(0, TX_DEFAULT_FREQ_HZ)
        self._sink.set_bandwidth(0, TX_BANDWIDTH)
        self._sink.set_gain(0, 89.75 - TX_DEFAULT_ATTN_DB)

        self._source_block = None
        self._mode: str | None = None
        self._lock = threading.Lock()
        self._freq_hz: int = TX_DEFAULT_FREQ_HZ
        self._attn_db: float = TX_DEFAULT_ATTN_DB
        self._running = False

    # -- mode switching -----------------------------------------------------

    def tone_mode(self) -> None:
        """Configure a CW tone offset ``TX_TONE_OFFSET_HZ`` above the LO.

        Generated as a complex exponential at baseband so the transmitted
        carrier appears at ``freq_hz + TX_TONE_OFFSET_HZ`` — 100 kHz off the
        shared RX centre frequency rather than on top of it.
        """
        with self._lock:
            was_running = self._running
            if was_running:
                self.stop()
                self.wait()
            if self._source_block is not None:
                self.disconnect_all()
            src = analog.sig_source_c(
                TX_SAMPLE_RATE, analog.GR_COS_WAVE, TX_TONE_OFFSET_HZ, 1.0, 0,
            )
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
        super().start()
        self._running = True

    def stop(self) -> None:  # type: ignore[override]
        super().stop()
        self.wait()
        self._running = False

    # -- runtime controls ---------------------------------------------------

    def set_frequency(self, freq_hz: int) -> None:
        self._sink.set_frequency(0, freq_hz)
        self._freq_hz = freq_hz

    def set_attn(self, attn_db: float) -> None:
        """Set TX attenuation (0 = max power ≈ 0 dBm, 89 = min power)."""
        attn_db = max(0.0, min(89.75, attn_db))
        self._sink.set_gain(0, 89.75 - attn_db)
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
