"""No-hardware tests for the FCC 15.247 compliance check.

Two layers, both without an SDR:
  1. TestEvaluateFccCompliance -- the pure math (evaluate_fcc_compliance /
     spectrum_traces) against synthetic spectra built the same way real data
     flows: raw IQ -> compute_spec_chunk -> spectrum_traces. No Flask, no
     capture, no lock.
  2. TestFccCheckEndpoint -- the /api/fcc_check route's wiring (status codes,
     JSON shape, the capture-lock and bad-input error paths), with
     _capture_iq monkeypatched to hand back synthetic IQ instead of touching
     hardware.

Real-signal, real-hardware validation lives in hitl-endpoint's
tests/test_fcc_emissions.py -- that's the only layer that can catch an actual
RF/gain problem; this file exists so a bug in the pass/fail math itself
doesn't have to wait for that rig to be caught.
"""

import numpy as np
from hubble_satnet_decoder import compute_spec_chunk

from stream_web import app as app_module
from stream_web import config
from stream_web.app import _capture_lock, app
from stream_web.spectrogram import evaluate_fcc_compliance, spectrum_traces

SR = config.SAMPLE_RATE
# Arbitrary test LO -- evaluate_fcc_compliance only cares about frequencies
# relative to it, so this doesn't need to match any real config default.
_TEST_LO_HZ = 903_000_000.0


# ---------------------------------------------------------------------------
# Helpers -- build synthetic signals the same way the real pipeline does
# ---------------------------------------------------------------------------

def _make_chunks(tones, seconds=5.0, noise_std=0.01, seed=0):
    """Sxx_dB chunks for a sum of CW tones plus noise, via the real compute_spec_chunk.

    tones: list of (offset_hz, amplitude) pairs, offset from _TEST_LO_HZ.
    """
    rng = np.random.default_rng(seed)
    n_chunks = int(seconds / config.SPEC_CHUNK_S)
    n = config.SPEC_CHUNK_SAMPLES
    chunks = []
    for c in range(n_chunks):
        t = (np.arange(n) + c * n) / SR
        iq = rng.normal(0, noise_std, n) + 1j * rng.normal(0, noise_std, n)
        for offset_hz, amp in tones:
            iq = iq + amp * np.exp(2j * np.pi * offset_hz * t)
        chunks.append(compute_spec_chunk(iq.astype(np.complex64)))
    return chunks


def _make_chirp_chunks(f0_hz, f1_hz, seconds=5.0, amp=0.6, noise_std=0.01, seed=0):
    """Sxx_dB chunks for a tone swept f0_hz -> f1_hz within every chunk.

    A single CW tone always measures a narrow 20 dB bandwidth (a few hundred
    Hz) -- there's no way to make one fail the occupied-bandwidth check.
    Sweeping the frequency within each chunk's window spreads real, contiguous
    energy across a wide band, the way an actual over-modulated or drifting
    carrier would.
    """
    rng = np.random.default_rng(seed)
    n_chunks = int(seconds / config.SPEC_CHUNK_S)
    n = config.SPEC_CHUNK_SAMPLES
    dur = n / SR
    k = (f1_hz - f0_hz) / dur  # Hz/sec sweep rate, resets every chunk
    chunks = []
    for _c in range(n_chunks):
        t = np.arange(n) / SR
        phase = 2 * np.pi * (f0_hz * t + 0.5 * k * t**2)
        iq = rng.normal(0, noise_std, n) + 1j * rng.normal(0, noise_std, n)
        iq = iq + amp * np.exp(1j * phase)
        chunks.append(compute_spec_chunk(iq.astype(np.complex64)))
    return chunks


def _evaluate(chunks):
    n_bins = chunks[0].shape[0]
    freqs_hz = np.linspace(-SR / 2.0, SR / 2.0, n_bins) + _TEST_LO_HZ
    bin_hz = SR / n_bins
    avg_dB, _peak_dB = spectrum_traces(chunks)
    return evaluate_fcc_compliance(freqs_hz, avg_dB, bin_hz)


def _make_tone_iq(n_samples, offset_hz=100_000.0, amp=1.0, noise_std=0.01, seed=0):
    """Raw complex64 IQ (not pre-chunked) for the Flask-layer tests below --
    this is what _capture_iq hands back in the real code path."""
    rng = np.random.default_rng(seed)
    t = np.arange(n_samples) / SR
    iq = rng.normal(0, noise_std, n_samples) + 1j * rng.normal(0, noise_std, n_samples)
    iq = iq + amp * np.exp(2j * np.pi * offset_hz * t)
    return iq.astype(np.complex64)


# ---------------------------------------------------------------------------
# 1. Pure math -- evaluate_fcc_compliance / spectrum_traces
# ---------------------------------------------------------------------------


class TestEvaluateFccCompliance:
    def test_noise_only_returns_none(self):
        assert _evaluate(_make_chunks([])) is None

    def test_clean_tone_passes(self):
        result = _evaluate(_make_chunks([(100_000, 1.0)]))
        assert result is not None
        assert result["overall_ok"] is True
        assert result["spacing_ok"] is True
        assert result["spurs_ok"] is True
        assert result["n_spur_fail"] == 0
        assert result["snr_db"] > config.FCC_OVERLAY_MIN_SNR_DB

    def test_wide_tone_fails_occupied_bandwidth(self):
        # 60 kHz sweep centred on +100 kHz -- well past the 38.625 kHz limit
        # (1.5x the 25.75 kHz channel spacing).
        result = _evaluate(_make_chirp_chunks(70_000, 130_000))
        assert result is not None
        assert result["spacing_ok"] is False
        assert result["bw_20db_hz"] > result["bw_limit_hz"]
        assert result["overall_ok"] is False

    def test_strong_nearby_spur_fails_spurious_emissions(self):
        result = _evaluate(_make_chunks([(100_000, 1.0), (150_000, 0.2)]))
        assert result is not None
        assert result["spurs_ok"] is False
        assert result["n_spur_fail"] == 1
        assert result["overall_ok"] is False
        spur = next(s for s in result["spurs"] if not s["pass"])
        assert spur["dbc"] > -result["spur_limit_dbc"]  # less than 20 dB down

    def test_weak_distant_spur_still_detected_but_passes(self):
        result = _evaluate(_make_chunks([(100_000, 1.0), (250_000, 0.03)]))
        assert result is not None
        assert result["spurs_ok"] is True
        assert result["overall_ok"] is True
        # It should still show up in the list -- "passes" isn't "invisible".
        assert len(result["spurs"]) == 1
        assert result["spurs"][0]["pass"] is True

    def test_result_schema(self):
        """Contract test: every key the API/overlay code reads must be present
        and correctly typed, so a refactor can't silently drop one."""
        result = _evaluate(_make_chunks([(100_000, 1.0), (250_000, 0.03)]))
        assert isinstance(result["carrier_freq_hz"], float)
        assert isinstance(result["carrier_power_db"], float)
        assert isinstance(result["snr_db"], float)
        assert isinstance(result["bw_20db_hz"], float)
        assert isinstance(result["bw_limit_hz"], float)
        assert isinstance(result["channel_spacing_hz"], float)
        assert isinstance(result["spacing_ok"], bool)
        assert isinstance(result["spur_limit_dbc"], float)
        assert isinstance(result["spurs"], list)
        assert isinstance(result["n_spur_fail"], int)
        assert isinstance(result["spurs_ok"], bool)
        assert isinstance(result["overall_ok"], bool)
        for spur in result["spurs"]:
            assert {"idx", "freq_hz", "power_db", "dbc", "pass"} <= spur.keys()


# ---------------------------------------------------------------------------
# 2. Flask route -- /api/fcc_check
# ---------------------------------------------------------------------------


class TestFccCheckEndpoint:
    @staticmethod
    def _client():
        app.config["TESTING"] = True
        return app.test_client()

    def test_clean_tone_passes(self, monkeypatch):
        monkeypatch.setattr(app_module, "_capture_iq",
                            lambda n: _make_tone_iq(n))
        resp = self._client().get("/api/fcc_check")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["state"] == "pass"
        assert body["pass"] is True
        assert "carrier" in body and "checks" in body and "analysis" in body
        assert body["checks"]["occupied_bandwidth"]["pass"] is True
        assert body["checks"]["spurious_emissions"]["pass"] is True

    def test_no_signal_reports_no_signal_state(self, monkeypatch):
        monkeypatch.setattr(app_module, "_capture_iq",
                            lambda n: _make_tone_iq(n, amp=0.0))
        resp = self._client().get("/api/fcc_check")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["state"] == "no_signal"
        assert body["pass"] is None

    def test_busy_capture_lock_returns_409(self, monkeypatch):
        monkeypatch.setattr(app_module, "_capture_iq",
                            lambda n: _make_tone_iq(n))
        _capture_lock.acquire()
        try:
            resp = self._client().get("/api/fcc_check")
            assert resp.status_code == 409
        finally:
            _capture_lock.release()

    def test_invalid_seconds_returns_400(self, monkeypatch):
        monkeypatch.setattr(app_module, "_capture_iq",
                            lambda n: _make_tone_iq(n))
        client = self._client()
        assert client.get("/api/fcc_check?seconds=abc").status_code == 400
        assert client.get("/api/fcc_check?seconds=0").status_code == 400
