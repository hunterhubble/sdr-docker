"""Signal Hound VSG60A TX via the native VSG API (not gr-soapy IQ streaming).

Tone mode uses :func:`vsgOutputCW` so the hardware generates a stable CW carrier.
Packet mode uses :func:`vsgRepeatWaveform` / :func:`vsgOutputWaveform` with file IQ.

Pluto / bladeRF continue to use :class:`gnuradio_tx.TXFlowgraph` (Soapy + GNU Radio).
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, byref, c_float, c_int

import numpy as np

from . import config

# VSG60 API level limits (vsg_api.h); product CW spec is narrower — see config.
_VSG_API_LEVEL_MIN = -120.0
_VSG_API_LEVEL_MAX = 10.0


def _load_vsg_lib() -> ctypes.CDLL:
    for name in ("vsg_api", "libvsg_api.so.1", "libvsg_api.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OSError(
        "libvsg_api not found — install Signal Hound VSG60 libraries (see Dockerfile / SDK)"
    )


_lib = None


def _vsg() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        _lib = _load_vsg_lib()
        _lib.vsgGetDeviceList.argtypes = [POINTER(c_int), POINTER(c_int)]
        _lib.vsgGetDeviceList.restype = c_int
        _lib.vsgOpenDeviceBySerial.argtypes = [POINTER(c_int), c_int]
        _lib.vsgOpenDeviceBySerial.restype = c_int
        _lib.vsgOpenDevice.argtypes = [POINTER(c_int)]
        _lib.vsgOpenDevice.restype = c_int
        _lib.vsgCloseDevice.argtypes = [c_int]
        _lib.vsgCloseDevice.restype = c_int
        _lib.vsgAbort.argtypes = [c_int]
        _lib.vsgAbort.restype = c_int
        _lib.vsgSetFrequency.argtypes = [c_int, ctypes.c_double]
        _lib.vsgSetFrequency.restype = c_int
        _lib.vsgSetSampleRate.argtypes = [c_int, ctypes.c_double]
        _lib.vsgSetSampleRate.restype = c_int
        _lib.vsgSetLevel.argtypes = [c_int, ctypes.c_double]
        _lib.vsgSetLevel.restype = c_int
        _lib.vsgOutputCW.argtypes = [c_int]
        _lib.vsgOutputCW.restype = c_int
        _lib.vsgRepeatWaveform.argtypes = [c_int, POINTER(c_float), c_int]
        _lib.vsgRepeatWaveform.restype = c_int
        _lib.vsgOutputWaveform.argtypes = [c_int, POINTER(c_float), c_int]
        _lib.vsgOutputWaveform.restype = c_int
        _lib.vsgGetErrorString.argtypes = [c_int]
        _lib.vsgGetErrorString.restype = ctypes.c_char_p
    return _lib


def _check(status: int, what: str = "VSG API") -> None:
    if status == 0 or status == 2:  # vsgNoError, vsgSettingClamped
        return
    msg = _vsg().vsgGetErrorString(status)
    text = msg.decode() if msg else f"status {status}"
    raise RuntimeError(f"{what}: {text}")


def _clamp_level_dbm(dbm: float) -> float:
    lo = config.SIGNALHOUND_TX_DBM_MIN
    hi = config.SIGNALHOUND_TX_DBM_MAX
    return max(lo, min(hi, float(dbm)))


class SignalHoundTX:
    """VSG60 TX controller with the same lifecycle methods as :class:`TXFlowgraph`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handle = c_int(-1)
        self._running = False
        self._mode: str | None = None
        self._freq_hz: int = config.CENTER_FREQ_HZ
        self._power_dbm: float = config.SIGNALHOUND_TX_DEFAULT_DBM
        self._packet_iq: np.ndarray | None = None
        self._packet_repeat = True
        self._open_device()

    def _open_device(self) -> None:
        vsg = _vsg()
        serials = (c_int * config.VSG_MAX_DEVICES)()
        count = c_int(config.VSG_MAX_DEVICES)
        _check(vsg.vsgGetDeviceList(serials, byref(count)), "vsgGetDeviceList")
        if count.value < 1:
            raise RuntimeError("No VSG60 devices found")

        handle = c_int(-1)
        if config.SIGNALHOUND_SERIAL:
            serial = int(config.SIGNALHOUND_SERIAL)
            _check(vsg.vsgOpenDeviceBySerial(byref(handle), serial), "vsgOpenDeviceBySerial")
        else:
            _check(vsg.vsgOpenDevice(byref(handle)), "vsgOpenDevice")
        self._handle = handle
        self._apply_rf_settings()
        print(
            f"[TX] Signal Hound VSG60 open (handle={handle.value}), "
            f"power={self._power_dbm:.1f} dBm",
            flush=True,
        )

    def _apply_rf_settings(self) -> None:
        vsg = _vsg()
        h = self._handle.value
        _check(vsg.vsgSetSampleRate(h, float(config.SAMPLE_RATE)), "vsgSetSampleRate")
        _check(vsg.vsgSetFrequency(h, float(self._freq_hz)), "vsgSetFrequency")
        _check(vsg.vsgSetLevel(h, float(self._power_dbm)), "vsgSetLevel")

    # -- mode switching (mirrors TXFlowgraph) ---------------------------------

    def tone_mode(self) -> None:
        with self._lock:
            if self._running:
                self.stop()
            self._mode = "tone"
            self._packet_iq = None

    def packet_mode(self, file_path: str, repeat: bool = True) -> None:
        if not __import__("os").path.isfile(file_path):
            raise FileNotFoundError(f"TX IQ file not found: {file_path}")
        data = np.fromfile(file_path, dtype=np.complex64)
        if data.size == 0:
            raise ValueError(f"TX IQ file is empty: {file_path}")
        iq = np.empty(data.size * 2, dtype=np.float32)
        iq[0::2] = data.real.astype(np.float32, copy=False)
        iq[1::2] = data.imag.astype(np.float32, copy=False)
        with self._lock:
            if self._running:
                self.stop()
            self._packet_iq = np.ascontiguousarray(iq)
            self._packet_repeat = repeat
            self._mode = "packet"

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            if self._mode is None:
                raise RuntimeError("Set tone_mode() or packet_mode() before start()")
            self._apply_rf_settings()
            vsg = _vsg()
            h = self._handle.value
            if self._mode == "tone":
                _check(vsg.vsgOutputCW(h), "vsgOutputCW")
            elif self._mode == "packet" and self._packet_iq is not None:
                n_samps = len(self._packet_iq) // 2
                ptr = self._packet_iq.ctypes.data_as(POINTER(c_float))
                if self._packet_repeat:
                    _check(vsg.vsgRepeatWaveform(h, ptr, n_samps), "vsgRepeatWaveform")
                else:
                    _check(vsg.vsgOutputWaveform(h, ptr, n_samps), "vsgOutputWaveform")
            else:
                raise RuntimeError(f"Unknown TX mode: {self._mode}")
            self._running = True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            _vsg().vsgAbort(self._handle.value)
            self._running = False

    # -- runtime controls -----------------------------------------------------

    def set_frequency(self, freq_hz: int) -> None:
        with self._lock:
            self._freq_hz = int(freq_hz)
            _check(
                _vsg().vsgSetFrequency(self._handle.value, float(self._freq_hz)),
                "vsgSetFrequency",
            )
            if self._running and self._mode == "tone":
                _check(_vsg().vsgOutputCW(self._handle.value), "vsgOutputCW")

    def set_level_dbm(self, power_dbm: float) -> None:
        with self._lock:
            self._power_dbm = _clamp_level_dbm(power_dbm)
            _check(
                _vsg().vsgSetLevel(self._handle.value, float(self._power_dbm)),
                "vsgSetLevel",
            )
            if self._running and self._mode == "tone":
                _check(_vsg().vsgOutputCW(self._handle.value), "vsgOutputCW")

    def set_attn(self, attn_db: float) -> None:
        """Pluto-style attenuation API — not used for Signal Hound; use set_level_dbm."""
        raise NotImplementedError(
            "Signal Hound VSG60 uses output power in dBm; call set_level_dbm or /api/tx/power"
        )

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
        """Legacy field for shared status polling — not meaningful on VSG60."""
        return self._power_dbm

    @property
    def power_dbm(self) -> float:
        return self._power_dbm

    def status_dict(self) -> dict:
        return {
            "running": self._running,
            "mode": self._mode,
            "freq_hz": self._freq_hz,
            "power_dbm": self._power_dbm,
            "power_dbm_min": config.SIGNALHOUND_TX_DBM_MIN,
            "power_dbm_max": config.SIGNALHOUND_TX_DBM_MAX,
            "power_dbm_step": config.SIGNALHOUND_TX_DBM_STEP,
            "api_level_min": _VSG_API_LEVEL_MIN,
            "api_level_max": _VSG_API_LEVEL_MAX,
        }
