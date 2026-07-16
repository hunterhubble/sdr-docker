"""Pure, testable cross-cycle state machine for the Signal Viewer's
time-domain capture (failure-by-chipset and device-id search).

Deliberately free of numpy / matplotlib / decoder imports so it can be unit
tested in isolation with plain dicts (see tests/test_td_capture.py).
processor.py owns the IQ buffer and rendering; this module owns only the
*decision* logic: which detection to capture this cycle, and how the pending
state carries across cycles.

The tricky parts this encodes:
  * Truncation confirmation -- a failure whose reason is only ever produced by
    the decode window cutting the packet short (_TRUNCATION_REASONS) is held
    and re-checked rather than reported immediately.
  * Phantom suppression -- a failed attempt landing inside a successfully
    decoded packet's span is a spurious secondary detection, not a real
    failure.
  * Backlog -- a confirmed hit too close to the trailing window edge to render
    yet is kept and retried next cycle.
  * Fixed-frame tracking -- pending hits are keyed by absolute buffer position
    and re-projected into each cycle's sliding window.
"""

from dataclasses import dataclass

from . import config

# Failure reasons that ONLY ever mean "the decode window cut the packet short",
# never a real signal/RS failure -- see processor.py / the decoder's demod
# loops. Held for confirmation instead of reported immediately.
_TRUNCATION_REASONS = ("pdu_incomplete", "unknown")


def decoded_span(entry: dict) -> tuple[float, float]:
    """(start_s, end_s) for a decoded attempt/packet, in decode_chunk time.

    ``packets``/``result`` carry ``signal_duration_s`` directly; a raw
    ``attempts`` entry marked decoded doesn't, but does carry
    ``num_pdu_symbols`` (set once header decode succeeds), enough to estimate
    the same span.
    """
    start = entry.get("time_s", 0.0)
    dur = entry.get("signal_duration_s")
    if not dur:
        ver = entry.get("phy_ver", 1)
        n_sym = (config.PREAMBLE_LEN + config.NUM_HEADER_SYMS
                 + (entry.get("num_pdu_symbols") or 0))
        slot = config.slot_samples.get(ver, config.slot_samples[1])["slot"]
        dur = n_sym * slot / config.SAMPLE_RATE
    return start, start + dur


def _start_sample(entry: dict) -> int:
    """Best estimate of an attempt's start sample within the current window."""
    return entry.get(
        "start_sample",
        int(round(entry.get("time_s", 0.0) * config.SAMPLE_RATE)),
    )


@dataclass
class TdCaptureState:
    """Cross-cycle state for TD capture. One instance lives in processor_main."""

    # A confirmed hit that couldn't be rendered yet (too near the window edge);
    # retried next cycle. Keyed by absolute buffer position via "_abs_start".
    pending_hit: dict | None = None
    # A truncation-only hit awaiting confirmation it's a real failure.
    pending_confirm: dict | None = None
    # The capture target this state belongs to; changing it resets the above.
    target: tuple | None = None

    def reset_pending(self) -> None:
        self.pending_hit = None
        self.pending_confirm = None


@dataclass
class TdCaptureResult:
    """What the caller should do this cycle."""

    # None, or {"td_hit", "td_start", "td_end", "td_center"} ready to render.
    capture: dict | None = None
    # Status line for the UI, or None to leave the previous one unchanged.
    status: str | None = None


def select_td_capture(
    state: TdCaptureState,
    *,
    td_on: bool,
    td_chipset: str | None,
    td_ntw_id: int | None,
    attempts: list[dict],
    packets: list[dict],
    decode_start: int,
    buf_len: int,
    chunk_len: int,
    now: float,
) -> TdCaptureResult:
    """Advance the capture state machine one cycle and return what to do.

    Mutates *state* in place. Pure otherwise (no I/O, no globals besides
    config constants), so it is fully unit-testable with synthetic dicts.
    """
    # Reset pending state if the user changed target (chipset<->device, or
    # stopped). Every cross-cycle field is cleared so nothing leaks across a
    # user action.
    current_target = (
        ("chipset", td_chipset) if td_chipset
        else ("ntw_id", td_ntw_id) if td_ntw_id is not None
        else None
    )
    if not td_on or current_target != state.target:
        state.reset_pending()
        state.target = current_target

    if not td_on:
        return TdCaptureResult()

    hit = None
    if td_chipset:
        hit = _select_chipset(state, td_chipset, attempts, packets,
                              decode_start, buf_len, now)
    elif td_ntw_id is not None:
        hit = _select_device(state, td_ntw_id, attempts,
                             decode_start, buf_len, now)

    if hit is not None:
        td_center, td_start, td_end = _render_window(hit)
        if td_end > chunk_len:
            # Not enough trailing IQ yet. Both paths assign their fresh hit to
            # state.pending_hit, so it is kept and retried next cycle. (The
            # `is not` guard is defensive: anything reaching here that isn't
            # the pending hit is dropped rather than left dangling.)
            if hit is not state.pending_hit:
                state.pending_hit = None
            return TdCaptureResult(capture=None, status=None)
        if hit is state.pending_hit:
            state.pending_hit = None  # served -- look for a new one next cycle
        return TdCaptureResult(
            capture={"td_hit": hit, "td_start": td_start,
                     "td_end": td_end, "td_center": td_center},
            status=_captured_status(hit, td_center),
        )

    return TdCaptureResult(capture=None,
                           status=_waiting_status(state, td_chipset,
                                                  td_ntw_id, packets))


# --- selection helpers -----------------------------------------------------

def _retry_pending(state: TdCaptureState, decode_start: int, buf_len: int,
                   now: float) -> dict | None:
    """Re-project a render-deferred pending hit into this cycle's window."""
    if state.pending_hit is None:
        return None
    rel = (state.pending_hit["_abs_start"] - decode_start) % buf_len
    if (rel >= config.DECODE_SAMPLES
            or now - state.pending_hit["_first_seen"] > config.TD_PENDING_TIMEOUT_S):
        state.pending_hit = None      # aged out of the window
        return None
    state.pending_hit["_rel_start"] = rel
    return state.pending_hit


def _select_chipset(state: TdCaptureState, td_chipset: str,
                    attempts: list[dict], packets: list[dict],
                    decode_start: int, buf_len: int, now: float) -> dict | None:
    match_tol = int(config.TD_TRUNCATION_MATCH_S * config.SAMPLE_RATE)

    # A truncation-only match awaiting confirmation: did it decode OK now
    # (window caught up -> not a real failure), fail for a real reason
    # (surface it), or is it still incomplete (keep waiting, up to a timeout)?
    if state.pending_confirm is not None:
        abs_c = state.pending_confirm["_abs_start"]
        rel_c = (abs_c - decode_start) % buf_len
        resolved_ok = any(
            a.get("chipset") == td_chipset
            and abs(_start_sample(a) - rel_c) < match_tol
            for a in attempts if a.get("decoded")
        )
        if resolved_ok or rel_c >= config.DECODE_SAMPLES:
            state.pending_confirm = None
        else:
            re_attempt = next(
                (a for a in attempts
                 if a.get("chipset") == td_chipset and not a.get("decoded")
                 and abs(_start_sample(a) - rel_c) < match_tol),
                None,
            )
            if (re_attempt is not None
                    and re_attempt.get("reason") not in _TRUNCATION_REASONS):
                confirmed = dict(re_attempt, _abs_start=abs_c, _rel_start=rel_c,
                                 _first_seen=now)
                state.pending_confirm = None
                state.pending_hit = confirmed
                return confirmed
            if now - state.pending_confirm["_first_seen"] > config.TD_TRUNCATION_CONFIRM_S:
                state.pending_confirm = None  # gave it a fair chance

    hit = _retry_pending(state, decode_start, buf_len, now)
    if hit is not None:
        return hit

    if state.pending_confirm is not None:
        return None  # don't start a new search while confirming one

    # Fresh match. Sourced from `attempts` (never content-deduped) and
    # suppressing any failed attempt whose time falls inside a packet decoded
    # this cycle -- that's a phantom secondary detection, not a real failure.
    pkt_spans = [decoded_span(a) for a in attempts if a.get("decoded")]
    pad = config.TD_PHANTOM_PAD_S
    matches = [a for a in attempts
               if a.get("chipset") == td_chipset and not a.get("decoded")
               and not any(s - pad <= a.get("time_s", 0) <= e + pad
                           for s, e in pkt_spans)]
    if not matches:
        return None

    candidate = dict(matches[0])
    rel = _start_sample(candidate)
    candidate["_abs_start"] = (decode_start + rel) % buf_len
    candidate["_first_seen"] = now
    if candidate.get("reason") in _TRUNCATION_REASONS:
        state.pending_confirm = candidate     # hold for confirmation
        return None
    candidate["_rel_start"] = rel
    state.pending_hit = candidate
    return candidate


def _select_device(state: TdCaptureState, td_ntw_id: int,
                   attempts: list[dict], decode_start: int, buf_len: int,
                   now: float) -> dict | None:
    hit = _retry_pending(state, decode_start, buf_len, now)
    if hit is not None:
        return hit

    # Source from `attempts` (decoded), not the display-level `packets` list:
    # a device transmitting the same payload is stripped from `packets` by the
    # content dedup after its first cycle in view, which would make it
    # invisible for the in-between cycles.
    matches = [a for a in attempts
               if a.get("decoded") and a.get("ntw_id") == td_ntw_id]
    if not matches:
        return None

    candidate = dict(matches[0])
    candidate.setdefault("reason", "ok")
    rel = _start_sample(candidate)
    candidate["_abs_start"] = (decode_start + rel) % buf_len
    candidate["_rel_start"] = rel
    candidate["_first_seen"] = now
    state.pending_hit = candidate
    return candidate


# --- window + status helpers -----------------------------------------------

def _render_window(hit: dict) -> tuple[int, int, int]:
    """(td_center, td_start, td_end) sample offsets for the render segment.

    td_center is always current-window-relative (_rel_start is refreshed each
    cycle), unlike the possibly-stale start_sample/time_s carried on the hit.
    """
    td_samples = int(config.TD_WINDOW_S * config.SAMPLE_RATE)
    td_center = hit.get(
        "_rel_start",
        hit.get("start_sample", int(round(hit.get("time_s", 0.0) * config.SAMPLE_RATE))),
    )
    td_start = max(0, td_center - int(td_samples * config.TD_PRE_ROLL_FRAC))
    return td_center, td_start, td_start + td_samples


def _captured_status(hit: dict, td_center: int) -> str:
    t_s = td_center / config.SAMPLE_RATE
    if hit.get("decoded"):
        return f"t={t_s:.3f}s | DECODED seq={hit.get('seq_num')}"
    return f"t={t_s:.3f}s | FAILED: {hit.get('reason', 'unknown')}"


def _waiting_status(state: TdCaptureState, td_chipset: str | None,
                    td_ntw_id: int | None, packets: list[dict]) -> str | None:
    if state.pending_confirm is not None:
        return (f"Possible {td_chipset} failure at "
                f"t={state.pending_confirm.get('time_s', 0):.3f}s "
                f"cut off by decode window -- confirming...")
    if td_chipset:
        return (f"Waiting for {td_chipset} failure... "
                f"({len(packets)} decoded this cycle)")
    if td_ntw_id is not None:
        pkt_ids = [p["ntw_id"] for p in packets]
        return f"Searching... ({len(packets)} pkts, IDs: {pkt_ids[:5]})"
    return None
