"""GNU Radio flowgraph for SDR RX -- unified backend for all SDR hardware.

Uses gr-soapy (built into GNU Radio >= 3.9) so any SoapySDR-supported device
(PlutoSDR, bladeRF, RTL-SDR, HackRF, LimeSDR, …) works with a single code
path.  The only thing that changes per-device is the SoapySDR driver string.

Samples flow through a tight C++ thread into a custom Python sink that writes
directly into the application's shared circular buffer, keeping the USB
transfer thread unblocked (critical for bladeRF stability).

When the SDR connection drops mid-stream (e.g. network glitch, PlutoSDR power
cycle), libiio cannot recover within the same process -- a new iio_context
still inherits the broken pipe state.  Therefore this module exits the process
on connection loss, relying on Docker's restart policy (or a supervisor) to
bring up a clean instance.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import numpy as np
from gnuradio import gr, soapy

if TYPE_CHECKING:
    from .app import SharedState

from . import config

# ---------------------------------------------------------------------------
# Device driver strings
# ---------------------------------------------------------------------------

def _soapy_driver_args() -> str:
    """Build the SoapySDR device argument string from config / env."""
    import os

    sdr_type = config.SDR_TYPE
    if sdr_type == "bladerf":
        args = "driver=bladerf"
        serial = os.environ.get("BLADERF_SERIAL", "")
        if serial:
            args += f",serial={serial}"
        return args

    if sdr_type == "signalhound":
        args = "driver=SignalHoundVSG60"
        if config.SIGNALHOUND_SERIAL:
            args += f",serial={config.SIGNALHOUND_SERIAL}"
        return args

    # PlutoSDR / PlutoPlus (default) — both use the same SoapyPlutoSDR driver
    uri = config.PLUTO_URI
    if uri.startswith("ip:"):
        # Use uri= (not hostname=) so libiio calls iio_create_context_from_uri
        # instead of iio_create_network_context, which fails when the client
        # libiio (e.g. 0.23) is older than the firmware (e.g. 0.26).
        return f"driver=plutosdr,uri={uri}"
    # USB or bare: let SoapyPlutoSDR auto-discover via iio_create_default_context
    return "driver=plutosdr"


# ---------------------------------------------------------------------------
# Custom GNU Radio sink → shared circular buffer
# ---------------------------------------------------------------------------

class _BufferSink(gr.sync_block):
    """GNU Radio sink block that copies IQ samples into the app's circular
    buffer and updates peak-fraction state for the web dashboard.

    GNU Radio calls ``work()`` with small chunks (often ~1024 samples).
    This is the real-time hot path — no locks are taken here.  The
    circular buffer is single-producer (this thread) / single-consumer
    (processor thread), so lock-free operation is safe.  Python's GIL
    guarantees atomic integer writes for ``buf_write_idx``.
    """

    def __init__(self, state: SharedState):
        gr.sync_block.__init__(
            self,
            name="buffer_sink",
            in_sig=[np.complex64],
            out_sig=[],
        )
        self._state = state
        self._running_peak: float = 0.0
        self.last_work_time: float = time.monotonic()
        self._prev_work_time: float = self.last_work_time

    def work(self, input_items, output_items):  # noqa: ARG002
        samples = input_items[0]
        n = len(samples)
        state = self._state
        now = time.monotonic()

        # Overflow detection: a gap > 50 ms between work() calls means the
        # GNU Radio scheduler was starved — the SDR's USB/DMA buffers likely
        # overflowed and samples were silently lost.
        gap_s = now - self._prev_work_time
        if gap_s > 0.050 and self._prev_work_time > 0:
            state.rx_overflows += 1
            try:
                state.drop_queue.put_nowait(state.buf_write_idx)
            except Exception:
                pass
            if state.rx_overflows <= 20 or state.rx_overflows % 100 == 0:
                print(f"[RX] WARNING: probable sample drop #{state.rx_overflows} "
                      f"(gap={gap_s*1000:.1f}ms)")
        self._prev_work_time = now
        self.last_work_time = now

        peak = float(max(np.max(np.abs(samples.real)),
                        np.max(np.abs(samples.imag))))
        scaled = peak / config.ADC_FULL_SCALE
        if scaled > self._running_peak:
            self._running_peak = scaled
        state.rx_peak_frac = self._running_peak
        self._running_peak *= 0.999

        buf = state.iq_buffer
        wi = state.buf_write_idx
        space = config.IQ_BUFFER_SIZE - wi
        if n <= space:
            buf[wi:wi + n] = samples
            state.buf_write_idx = wi + n
        else:
            buf[wi:] = samples[:space]
            remainder = n - space
            buf[:remainder] = samples[space:]
            state.buf_write_idx = remainder

        return n


# ---------------------------------------------------------------------------
# Flowgraph: soapy.source → _BufferSink
# ---------------------------------------------------------------------------

class SDRFlowgraph(gr.top_block):
    """Top-level GNU Radio flowgraph that connects an SDR source to the
    application's shared IQ buffer via :class:`_BufferSink`.
    """

    def __init__(self, state: SharedState):
        gr.top_block.__init__(self, "sdr_rx")

        dev_args = _soapy_driver_args()

        self._source = soapy.source(dev_args, "fc32", 1, "", "",
                                    [""], [""])

        self._source.set_sample_rate(0, config.SAMPLE_RATE)
        self._source.set_frequency(0, config.CENTER_FREQ_HZ)
        self._source.set_bandwidth(0, config.RF_BANDWIDTH)

        if config.RX_GAIN_MODE == "manual":
            self._source.set_gain_mode(0, False)
            self._source.set_gain(0, config.RX_INITIAL_GAIN_DB)
        else:
            self._source.set_gain_mode(0, True)

        self._sink = _BufferSink(state)
        self.connect(self._source, self._sink)
        self._state = state
        self._dev_args = dev_args

    @property
    def info_string(self) -> str:
        return (
            f"{config.SDR_TYPE} via gr-soapy ({self._dev_args}) -- "
            f"LO={config.CENTER_FREQ_HZ / 1e9:.5f} GHz, "
            f"fs={config.SAMPLE_RATE:,} Hz"
        )

    def set_gain(self, gain_db: float) -> None:
        self._source.set_gain_mode(0, False)
        self._source.set_gain(0, gain_db)

    def set_frequency(self, freq_hz: int) -> None:
        self._source.set_frequency(0, freq_hz)

    def seconds_since_last_sample(self) -> float:
        """How long since ``_BufferSink.work()`` was last called."""
        return time.monotonic() - self._sink.last_work_time


# ---------------------------------------------------------------------------
# Public entry point used by app.py
# ---------------------------------------------------------------------------

_STALE_TIMEOUT_S = 5.0
_EXIT_CODE_CONNECTION_LOST = 3


def rx_loop(state: SharedState) -> None:
    """Start the GNU Radio flowgraph and block until ``state.running`` is
    cleared.  Retries initial connection, but exits the process on mid-stream
    connection loss (libiio cannot recover in-process).
    """
    fg = _connect(state)
    if fg is None:
        return

    cur_gain = config.RX_INITIAL_GAIN_DB
    cur_freq = config.CENTER_FREQ_HZ

    while state.running.is_set():
        if fg.seconds_since_last_sample() > _STALE_TIMEOUT_S:
            print(f"[RX] No samples for {_STALE_TIMEOUT_S}s "
                  "-- connection lost.  libiio cannot recover in-process; "
                  "exiting so Docker/supervisor can restart.",
                  flush=True)
            _teardown(fg, state)
            os._exit(_EXIT_CODE_CONNECTION_LOST)

        if config.RX_GAIN_MODE == "manual" and state.rx_gain_dB != cur_gain:
            new_gain = int(
                max(config.RX_GAIN_MIN_DB,
                    min(config.RX_GAIN_MAX_DB, state.rx_gain_dB))
            )
            try:
                fg.set_gain(new_gain)
                cur_gain = new_gain
                state.rx_gain_dB = cur_gain
                print(f"[GAIN] Set to {cur_gain} dB")
            except Exception as e:
                print(f"[GAIN] Failed to set {new_gain} dB: {e}")
                state.rx_gain_dB = cur_gain

        if state.lo_freq_hz != cur_freq:
            new_freq = state.lo_freq_hz
            try:
                fg.set_frequency(new_freq)
                cur_freq = new_freq
                print(f"[LO] Tuned to {cur_freq / 1e9:.6f} GHz")
            except Exception as e:
                print(f"[LO] Failed to set {new_freq}: {e}")
                state.lo_freq_hz = cur_freq

        time.sleep(0.2)

    _teardown(fg, state)
    if config.VERBOSE:
        print("[RX] Stopped.")


def _connect(state: SharedState) -> SDRFlowgraph | None:
    """Try to create and start the flowgraph, retrying until success or
    ``state.running`` is cleared.
    """
    while state.running.is_set():
        try:
            fg = SDRFlowgraph(state)
            fg.start()
            state.rx_connected.set()
            state.rx_gain_dB = config.RX_INITIAL_GAIN_DB
            print(f"[RX] Connected -- {fg.info_string}, "
                  f"gain_mode={config.RX_GAIN_MODE}, "
                  f"gain={config.RX_INITIAL_GAIN_DB} dB")
            return fg
        except Exception as e:
            print(
                f"[RX] SDR not found ({e}), "
                f"retrying in {config.SDR_RETRY_INTERVAL_S}s..."
            )
            for _ in range(config.SDR_RETRY_INTERVAL_S * 10):
                if not state.running.is_set():
                    print("[RX] Stopped (never connected).")
                    return None
                time.sleep(0.1)
    return None


def _teardown(fg: SDRFlowgraph, state: SharedState) -> None:
    """Stop the flowgraph and clear the connected flag."""
    state.rx_connected.clear()
    try:
        fg.stop()
        fg.wait()
    except Exception:
        pass
