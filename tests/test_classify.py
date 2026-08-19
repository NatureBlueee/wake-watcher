#!/usr/bin/env python3
"""Unit test the classifier against REAL captured detail strings.

These strings are verbatim from scanning ~/.claude/{tasks,jobs}/ — not invented.
Run: python3 test_classify.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "wake_watcher"))
from classify import classify  # noqa: E402

# (detail, expected_transient, label) — strings are real (see README §1)
CASES: list[tuple[str, bool, str]] = [
    # ── transient: SHOULD wake (empirically interrupted real bg jobs) ───────
    ("API Error: Connection closed mid-response. The response above may be incomplete.", True, "conn-closed (killed job session-A)"),
    ("API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited", True, "infra rate-limit (26 occurrences)"),
    ("API Error: 503 Service Unavailable", True, "503"),
    ("API Error: 502 Bad Gateway", True, "502"),
    ("API Error: 529 — server overloaded", True, "529 overload"),
    ("overloaded_error", True, "overloaded_error"),
    ("API Error: The socket connection was closed unexpectedly.", True, "socket closed"),
    ("API Error: Stream idle timeout - partial response received", True, "stream idle timeout"),
    ("API Error: Unable to connect to API (UNKNOWN_CERTIFICATE_VERIFICATION_ERROR)", True, "unable to connect / cert"),
    ("This is not your fault. Please retry.", True, "not your fault"),
    # owner 2026-07-10, missed-rescue confirmed empirically: these two strings used to be outside the whitelist (logs confirmed session-B really stalled ~7h
    # until the owner manually typed "continue" to rescue it; the same whitelist gap is equally real for session-C/session-D/session-E, but each of those had
    # a faster user retry / teammate message land within the same scan window, so no extra stall duration was observed — recording them proves the gap is
    # real, not that every instance actually stalled for hours).
    ("API Error: Response stalled mid-stream. The response above may be incomplete.", True, "stalled mid-stream (confirmed gap: session-C/session-D/session-B; real multi-hour stall only session-B ~7h)"),
    ("API Error: Server error mid-response. The response above may be incomplete.", True, "server error mid-response (confirmed gap: session session-E, no extra stall observed)"),
    ("API Error: Some brand-new wording we've never seen mid-stream, sorry.", True, "mid-stream safety-net catch-all (future unknown variant)"),
    # another historical instance of the same family, found during owner's 2026-07-10 full scan of ~/.claude/projects (2026-06-16 session session-F; the
    # owner manually typed "network hiccup just now, continue, retry, continue" 28s later to get it unstuck; no wake-watcher log survives to confirm whether
    # it was actually scanned at the time — only that this string itself was missing from the whitelist).
    ("API Error: Connection closed while thinking, before producing a response. Try again.", True, "conn-closed-while-thinking (confirmed pattern gap: session session-F, scan-time coverage unverified)"),
    ("API Error: Connection closed by some future wording we've never seen before.", True, "connection-closed safety-net catch-all (future unknown variant)"),
    # ── real stop / awaiting owner / real error: MUST NOT wake (default-deny + veto) ──
    # golden negative: 400 schema error — empirically manually retried 6x, all failed, should never auto-retry
    ("API Error: 400 messages.3.content.0: unexpected `tool_use_id` found in `advisor_tool_result` blocks: srvtoolu_x. Each `advisor_tool_result` block must have a corresponding `server_tool_use` block before it.", False, "400 schema (golden negative; killed job session-G, retried 6x)"),
    ("API Error: 403 Request not allowed", False, "403 not allowed"),
    # detail from a real live blocked job (awaiting owner input — not transient)
    ("drafted the first batch of docs; awaiting third-party API credentials", False, "awaiting owner credentials (live job, id redacted)"),
    ("reorganization plan written to docs/PLAN.md; awaiting review", False, "awaiting owner review (live job, id redacted)"),
    ("service A live and verified (users receiving notifications); service B blocked by a safety gate on its owner approval", False, "blocked by safety gate (live job, id redacted)"),
    ("the refactor is ready for review, with verification attached -- needs a line-by-line accept/reject", False, "awaiting owner verdict (live job, id redacted)"),
    # real task failure / rejection
    ("User rejected the proposed edit", False, "user rejected"),
    ("Permission denied: cannot write to /etc", False, "permission denied"),
    ("Usage limit reached. Resets at 5pm.", False, "usage limit (quota, not infra)"),
    ("Authentication failed: invalid api key", False, "auth failed"),
    ("Task complete: backend wired, all tests green", False, "completion summary"),
    # owner 2026-07-10 confirmed during the full scan that these three existing "API Error:" variants should indeed stay non-waking
    # (default-deny already got it right — golden negative, pinned so future additions to the transient whitelist don't accidentally catch them).
    ("API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy (https://www.anthropic.com/legal/aup). Please double press esc to edit your last message or start a new session.", False, "AUP policy refusal (real stop, retry never helps)"),
    ("API Error: an image in the conversation could not be processed and was removed. Double press esc to edit your message, or re-read the file if you still need it.", False, "unprocessable image (needs user to edit/re-attach, not a stream cut)"),
    ("API Error: 402 This request requires more credits, or fewer max_tokens. You requested up to 128000 tokens, but can only afford 1517. To increase, visit https://openrouter.ai/settings/credits", False, "402 insufficient credits (real quota problem, not infra transient)"),
    ("", False, "empty (default-deny)"),
    (None, False, "None (default-deny)"),
    ("Some unrecognized novel message we've never seen", False, "unknown (default-deny)"),
]


def run() -> int:
    fails = 0
    for detail, expected, label in CASES:
        res = classify(detail)
        got = res["transient"]
        ok = got == expected
        mark = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        snippet = (detail or "<none>")[:55]
        print(f"  [{mark}] transient={got!s:5} expect={expected!s:5} | {label}")
        if not ok:
            print(f"         detail={snippet!r}")
            print(f"         reason={res['reason']}")
    total = len(CASES)
    print(f"\n{total - fails}/{total} passed, {fails} failed")
    return fails


def run_golden_negatives() -> int:
    """Check tests/golden_negatives.json separately from CASES above.

    This fixture is deliberately its own file, not part of patterns.json:
    if a rule and its expected outcome lived in the same editable file, a PR
    that changes both together would pass CI with zero visible drift. Kept
    apart, a loosened veto shows up as a diff against a file the PR did not
    touch — something a reviewer has to notice.
    """
    fixture_path = Path(__file__).resolve().parent / "golden_negatives.json"
    with fixture_path.open(encoding="utf-8") as f:
        fixture = json.load(f)
    fails = 0
    for case in fixture["cases"]:
        res = classify(case["detail"])
        got = res["transient"]
        expected = case["expect_transient"]
        ok = got == expected
        mark = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"  [{mark}] transient={got!s:5} expect={expected!s:5} | golden-negative: {case['label']}")
        if not ok:
            print(f"         detail={case['detail'][:70]!r}")
            print(f"         reason={res['reason']}")
    total = len(fixture["cases"])
    print(f"\ngolden negatives: {total - fails}/{total} passed, {fails} failed")
    return fails


if __name__ == "__main__":
    fails = run()
    fails += run_golden_negatives()
    sys.exit(1 if fails else 0)


# ── session-limit, the third category (owner 2026-07-07: auto-resume once the limit resets) ──
import time as _t

def test_session_limit_with_reset_time_flags_and_epoch():
    from classify import classify
    v = classify("You've hit your session limit · resets 3:20am (Australia/Melbourne)")
    assert v["transient"] is False          # must never take the immediate-wake path
    assert v["session_limit"] is True
    assert v["reset_epoch"] is not None and v["reset_epoch"] > _t.time() - 86400


def test_weekly_limit_with_reset_time_also_flags():
    from classify import classify
    v = classify("You've hit your weekly limit · resets 10pm (Australia/Melbourne)")
    assert v["session_limit"] is True and v["reset_epoch"] is not None


def test_usage_limit_without_reset_time_stays_plain_veto():
    from classify import classify
    v = classify("You've reached your usage limit")
    assert v["transient"] is False
    assert v["session_limit"] is False and v["reset_epoch"] is None


def test_plain_transient_not_marked_session_limit():
    from classify import classify
    v = classify("API Error: Connection closed mid-response")
    assert v["transient"] is True and v["session_limit"] is False


def test_parse_reset_epoch_rolls_to_tomorrow_when_past():
    from classify import parse_reset_epoch
    noon = _t.mktime((2026, 7, 7, 12, 0, 0, 0, 0, -1))
    e = parse_reset_epoch("resets 3:20am", now=noon)
    lt = _t.localtime(e)
    assert (lt.tm_mday, lt.tm_hour, lt.tm_min) == (8, 3, 20)  # already past -> tomorrow 3:20
    e2 = parse_reset_epoch("resets 10pm", now=noon)
    lt2 = _t.localtime(e2)
    assert (lt2.tm_mday, lt2.tm_hour) == (7, 22)  # not yet past -> today 22:00


def test_parse_reset_epoch_unparseable_returns_none():
    from classify import parse_reset_epoch
    assert parse_reset_epoch("resets soon") is None
