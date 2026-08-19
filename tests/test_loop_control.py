#!/usr/bin/env python3
"""Loop-control regression tests (2026-07-02 OOM incident root-cause fix):

T1 — incident regression pin: a churn session (transcript keeps growing longer from
     retries, but never actually escapes) must have its wake count climb monotonically
     #1/3 → #2/3 → #3/3, and must never be reset back to #1/3 just because "the transcript
     got longer" (old bug: file grew longer = progress = budget cleared = stuck at #1/3
     forever = combined with "never give up once capped" = a 60s infinite wake loop = the
     root cause of the crash).
T2 — "no need to give up" pin: hitting MAX_WAKES doesn't stop things permanently — push
     next_eligible_at into the past and assert the 4th send (slow lane) really does go out,
     and NEEDS-HUMAN surfaces exactly once (no repeat spam).
T3 — a real escape resets the budget: once the last main-conversation turn is normal output
     again → budget clears; a later independent new error → gets a full fresh budget.
T4 — offline defer (2026-06-27/07-02, owner: don't keep firing uselessly while WiFi is down
     or the machine is off, and don't burn through the budget for nothing either): 0 wakes
     while offline and wakes stays unchanged; once the network is back it sends normally,
     with the budget undiminished.
T5 — switching to a different class of transient error (the old error_signature axis has
     been retired; pinned here: a new error still sends normally as long as it's "the last
     entry + no retry after it", with no dependency on the signature changing).

Sandbox + WAKE_WATCHER_FAKE_DELIVER (doesn't really spawn claude) + WAKE_WATCHER_FAKE_NET,
reusing the Sandbox/record-construction helpers from test_dedup_fix.py.
Run: python3 test_loop_control.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "wake_watcher"))
from test_dedup_fix import E1, E2, Sandbox, asst_error, asst_normal, user_msg  # noqa: E402


def _check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return 0 if cond else 1


def _wake_attempts(log_text, sid):
    """Pull this session's 'attempt #N/3' sequence out of the log, in order of appearance."""
    out = []
    for line in log_text.splitlines():
        if f"WAKE session={sid} attempt #" in line:
            frag = line.split("attempt #", 1)[1]
            out.append(frag.split("/", 1)[0])
    return out


def test_T1_churn_never_resets_by_transcript_growth():
    print("\n=== T1: churn session (retry continues but never escapes) wake count climbs monotonically #1→#2→#3, never resets back to #1 ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="loop-T1-"))
    try:
        sb = Sandbox(tmp)
        sb.set_transcript([asst_normal("干活"), asst_error(E1)])
        # Simulates the night of 2026-07-01: after each wake the transcript grows longer
        # from the retry continuing (the old bug's "progress" signal), but the AI never
        # actually escapes (the last main-conversation turn is always a fresh error with
        # no retry after it).
        for _ in range(3):
            sb.run_once()
            sb.append(user_msg("retry"), asst_error(E1))
        attempts = _wake_attempts(sb.log_text, sb.sid)
        fails += _check(attempts == ["1", "2", "3"],
                        f"the wake sequence must be ['1','2','3'] (monotonic climb), got {attempts} "
                        f"(the old bug would be all '1' — transcript growth was misjudged as progress, budget cleared forever)")
        fails += _check("ESCAPE" not in sb.log_text, "never actually escaped, shouldn't see an ESCAPE reset entry in the log")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_T2_slow_lane_never_gives_up():
    print("\n=== T2: hitting the cap isn't giving up forever — push next_eligible_at into the past, the slow lane's 4th send really goes out ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="loop-T2-"))
    try:
        sb = Sandbox(tmp)
        sb.set_transcript([asst_normal("干活"), asst_error(E1)])
        for _ in range(3):
            sb.run_once()
            sb.append(user_msg("retry"), asst_error(E1))
        # 4th scan: wakes (already) = 3 >= MAX_WAKES → this round only surfaces NEEDS-HUMAN,
        # doesn't send (semantics: the moment of hitting the cap is kept separate from "the
        # next slow-lane wake" — see the cap-handling comment in wake_watcher.scan_once).
        sb.run_once()
        attempts = _wake_attempts(sb.log_text, sb.sid)
        fails += _check(len(attempts) == 3, f"the fast lane stops after exactly 3 sends (the round that hits the cap only surfaces, doesn't send), got {len(attempts)}")
        fails += _check(f"NEEDS-HUMAN session={sb.sid}" in sb.log_text, "hitting the cap surfaces NEEDS-HUMAN")
        needs_human_count = sb.log_text.count(f"NEEDS-HUMAN session={sb.sid}")
        fails += _check(needs_human_count == 1, f"NEEDS-HUMAN surfaces only once, no repeat spam (got {needs_human_count})")

        # scan a few more rounds (the slow-lane timer hasn't fired yet) — shouldn't send again
        sb.append(user_msg("retry"), asst_error(E1))
        for _ in range(3):
            sb.run_once()
        fails += _check(len(_wake_attempts(sb.log_text, sb.sid)) == 3,
                        "before the slow-lane timer fires, shouldn't send again (should still be 3)")

        # push next_eligible_at into the past (simulating "SLOW_RETRY_SEC has elapsed") → the next round should send the 4th wake
        led = json.loads(sb.ledger.read_text())
        led[sb.sid]["next_eligible_at"] = 1.0
        sb.ledger.write_text(json.dumps(led))
        sb.run_once()
        attempts_after = _wake_attempts(sb.log_text, sb.sid)
        fails += _check(attempts_after == ["1", "2", "3", "4"],
                        f"once the slow lane is due, the 4th send really does go out (owner: no need to give up), got {attempts_after}")
        fails += _check("lane=slow" in sb.log_text, "the 4th wake's log entry is tagged lane=slow")
        fails += _check(sb.log_text.count(f"NEEDS-HUMAN session={sb.sid}") == 1,
                        "after the 4th (slow-lane) wake, NEEDS-HUMAN still has only 1 entry, no repeat surface")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_T3_real_escape_resets_budget():
    print("\n=== T3: a real escape (last main-conversation turn is normal output) → budget clears; a later independent new error → full budget again ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="loop-T3-"))
    try:
        sb = Sandbox(tmp)
        sb.set_transcript([asst_normal("干活"), asst_error(E1)])
        sb.run_once()  # #1
        sb.append(user_msg("retry"), asst_error(E1))
        sb.run_once()  # #2
        fails += _check(_wake_attempts(sb.log_text, sb.sid) == ["1", "2"], "climbed to #2 first")

        sb.append(asst_normal("终于恢复，全部做完了"))
        sb.run_once()  # real escape, doesn't send
        fails += _check(_wake_attempts(sb.log_text, sb.sid) == ["1", "2"], "the escape round doesn't send a new wake")
        fails += _check("ESCAPE session=" + sb.sid in sb.log_text, "the escape is visibly traced")

        led = json.loads(sb.ledger.read_text())
        fails += _check(led[sb.sid]["wakes"] == 0, f"ledger.wakes clears after the escape (got {led[sb.sid]['wakes']})")

        sb.append(asst_error(E2))  # a new, independent error
        sb.run_once()
        fails += _check(_wake_attempts(sb.log_text, sb.sid) == ["1", "2", "1"],
                        "budget resets after the escape, the new independent error starts fresh from #1 (full fast lane)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_T4_offline_defer_no_budget_loss():
    print("\n=== T4: offline defer — 0 wakes while offline and no budget deducted; sends normally once network is back, budget undamaged ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="loop-T4-"))
    try:
        sb = Sandbox(tmp)
        sb.set_transcript([asst_normal("干活"), asst_error(E1)])
        for _ in range(4):  # repeated scans while offline, none should send
            sb.run_once(fake_net="0")
        fails += _check(_wake_attempts(sb.log_text, sb.sid) == [], "0 WAKEs while offline")
        fails += _check("unreachable" in sb.log_text, "offline trace: log contains 'unreachable'")
        led = json.loads(sb.ledger.read_text()) if sb.ledger.exists() else {}
        fails += _check(led.get(sb.sid, {}).get("wakes", 0) == 0,
                        "doesn't count against the budget while offline (wakes still 0, full budget once network's back)")

        sb.run_once(fake_net="1")  # network is back
        fails += _check(_wake_attempts(sb.log_text, sb.sid) == ["1"],
                        "sends normally once network is back, and it's a full #1 (budget wasn't drained by the offline period)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_T5_new_error_class_sends_without_signature_axis():
    print("\n=== T5: switching to a different class of transient error still sends normally (no dependency on the old error_signature dedup axis) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="loop-T5-"))
    try:
        sb = Sandbox(tmp)
        sb.set_transcript([asst_normal("干活"), asst_error(E1)])
        sb.run_once()
        sb.append(user_msg("retry"), asst_error(E2))  # switch to a different class of transient error (E2 != E1)
        sb.run_once()
        fails += _check(_wake_attempts(sb.log_text, sb.sid) == ["1", "2"],
                        "a new class of transient error (with no retry after it) still wakes normally, climbing monotonically to #2")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def run():
    fails = 0
    fails += test_T1_churn_never_resets_by_transcript_growth()
    fails += test_T2_slow_lane_never_gives_up()
    fails += test_T3_real_escape_resets_budget()
    fails += test_T4_offline_defer_no_budget_loss()
    fails += test_T5_new_error_class_sends_without_signature_axis()
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
