"""Unit tests for the Signal Viewer's cross-cycle capture state machine.

These exercise td_capture.select_td_capture() with synthetic attempt/packet
dicts -- no SDR, no rendering -- covering the behaviours that each caused a
field bug during development:
  * truncation confirmation (discard / promote / timeout)
  * phantom secondary-detection suppression
  * device-id sourcing from `attempts` (survives content dedup)
  * near-edge render backlog
  * target-change reset
"""

from stream_web import config
from stream_web.td_capture import (
    _TRUNCATION_REASONS,
    TdCaptureState,
    select_td_capture,
)

SR = config.SAMPLE_RATE
BUF = config.IQ_BUFFER_SIZE
CHUNK = config.DECODE_SAMPLES  # a full-length window

# td_center that renders within a full window (td_end <= CHUNK):
FITS = 300_000
# td_center too close to the trailing edge to render this cycle:
NEAR_EDGE = 700_000


def _attempt(*, start_sample, chipset="A", decoded=False, reason="header_fail",
             **extra):
    a = {"chipset": chipset, "decoded": decoded, "reason": reason,
         "start_sample": start_sample, "time_s": start_sample / SR}
    a.update(extra)
    return a


def _run(state, *, attempts=(), packets=(), td_on=True, td_chipset="A",
         td_ntw_id=None, decode_start=0, now=100.0, chunk_len=CHUNK):
    return select_td_capture(
        state, td_on=td_on, td_chipset=td_chipset, td_ntw_id=td_ntw_id,
        attempts=list(attempts), packets=list(packets),
        decode_start=decode_start, buf_len=BUF, chunk_len=chunk_len, now=now,
    )


# --- fresh failure ---------------------------------------------------------

def test_real_failure_captured_immediately():
    state = TdCaptureState()
    res = _run(state, attempts=[_attempt(start_sample=FITS, reason="header_fail")])
    assert res.capture is not None
    assert res.capture["td_center"] == FITS
    assert "FAILED: header_fail" in res.status


def test_failure_for_wrong_chipset_ignored():
    state = TdCaptureState()
    res = _run(state, attempts=[_attempt(start_sample=FITS, chipset="B")])
    assert res.capture is None
    assert "Waiting for A" in res.status


# --- truncation confirmation ----------------------------------------------

def test_truncation_held_not_reported():
    for reason in _TRUNCATION_REASONS:
        st = TdCaptureState()
        res = _run(st, attempts=[_attempt(start_sample=FITS, reason=reason)])
        assert res.capture is None
        assert st.pending_confirm is not None
        assert "confirming" in res.status


def test_truncation_then_decode_is_discarded():
    state = TdCaptureState()
    _run(state, attempts=[_attempt(start_sample=FITS, reason="pdu_incomplete")])
    assert state.pending_confirm is not None
    # next cycle: same packet now decodes fine -> not a failure
    res = _run(state, attempts=[_attempt(start_sample=FITS, decoded=True,
                                         reason="ok", seq_num=3)])
    assert res.capture is None
    assert state.pending_confirm is None


def test_truncation_then_real_failure_is_promoted():
    state = TdCaptureState()
    _run(state, attempts=[_attempt(start_sample=FITS, reason="pdu_incomplete")])
    res = _run(state, attempts=[_attempt(start_sample=FITS, reason="pdu_fail")])
    assert res.capture is not None
    assert "FAILED: pdu_fail" in res.status
    assert state.pending_confirm is None


def test_truncation_times_out():
    state = TdCaptureState()
    _run(state, attempts=[_attempt(start_sample=FITS, reason="unknown")], now=100.0)
    # nothing matching re-appears; past the confirm window -> give up
    res = _run(state, attempts=[], now=100.0 + config.TD_TRUNCATION_CONFIRM_S + 0.1)
    assert state.pending_confirm is None
    assert res.capture is None


# --- phantom suppression ---------------------------------------------------

def test_phantom_inside_decoded_packet_suppressed():
    state = TdCaptureState()
    decoded = _attempt(start_sample=FITS, decoded=True, reason="ok",
                       time_s=0.30, signal_duration_s=0.05, seq_num=1)
    phantom = _attempt(start_sample=FITS + 100, reason="header_fail", time_s=0.34)
    res = _run(state, attempts=[decoded, phantom])
    assert res.capture is None            # phantom suppressed
    assert state.pending_confirm is None


def test_failure_outside_decoded_span_still_captured():
    state = TdCaptureState()
    decoded = _attempt(start_sample=FITS, decoded=True, reason="ok",
                       time_s=0.30, signal_duration_s=0.05, seq_num=1)
    real = _attempt(start_sample=FITS, reason="header_fail", time_s=0.60)
    res = _run(state, attempts=[decoded, real])
    assert res.capture is not None


# --- device-id -------------------------------------------------------------

def test_device_match_sourced_from_attempts_not_packets():
    state = TdCaptureState()
    # packets empty (content-deduped away), but the decode is still in attempts
    dev = _attempt(start_sample=FITS, decoded=True, reason="ok",
                   ntw_id=0xABCD, seq_num=7)
    res = _run(state, attempts=[dev], packets=[], td_chipset=None,
               td_ntw_id=0xABCD)
    assert res.capture is not None
    assert "DECODED seq=7" in res.status


def test_device_no_match_searching_status():
    state = TdCaptureState()
    res = _run(state, attempts=[], packets=[], td_chipset=None, td_ntw_id=0xABCD)
    assert res.capture is None
    assert "Searching" in res.status


# --- backlog (near-edge) ---------------------------------------------------

def test_near_edge_hit_deferred_then_served():
    state = TdCaptureState()
    res = _run(state, attempts=[_attempt(start_sample=NEAR_EDGE)])
    assert res.capture is None            # too close to edge to render yet
    assert state.pending_hit is not None

    # next cycle: window slid forward 200k samples so the packet fits
    res2 = _run(state, attempts=[], decode_start=200_000)
    assert res2.capture is not None
    assert state.pending_hit is None      # served


def test_pending_hit_ages_out():
    state = TdCaptureState()
    _run(state, attempts=[_attempt(start_sample=NEAR_EDGE)])
    assert state.pending_hit is not None
    # far in the future -> pending times out, nothing captured
    res = _run(state, attempts=[], now=100.0 + config.TD_PENDING_TIMEOUT_S + 0.1)
    assert res.capture is None
    assert state.pending_hit is None


# --- reset semantics -------------------------------------------------------

def test_target_change_resets_pending():
    state = TdCaptureState()
    _run(state, attempts=[_attempt(start_sample=FITS, reason="pdu_incomplete")])
    assert state.pending_confirm is not None
    _run(state, attempts=[], td_chipset="B")   # switched chipset target
    assert state.pending_confirm is None
    assert state.pending_hit is None


def test_capture_off_resets_and_is_silent():
    state = TdCaptureState()
    _run(state, attempts=[_attempt(start_sample=FITS, reason="pdu_incomplete")])
    res = _run(state, attempts=[], td_on=False)
    assert res.capture is None
    assert res.status is None
    assert state.pending_confirm is None
    assert state.pending_hit is None


# --- invariant guard -------------------------------------------------------

def test_timeouts_below_buffer_duration():
    # aliasing guard: a pending hit must be dropped before its buffer samples
    # can be overwritten (see config.py invariant comment).
    buffer_s = config.IQ_BUFFER_SIZE / config.SAMPLE_RATE
    assert config.TD_PENDING_TIMEOUT_S < buffer_s
    assert config.TD_TRUNCATION_CONFIRM_S < buffer_s
