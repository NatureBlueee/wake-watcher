"""session-limit, third category: auto-resume once the reset time passes (mechanism
requested by owner, 2026-07-07).

Five-state anchor: not yet reset (wait) / past reset (resume) / past reset but already
has a manual retry (dedup) / limit with no reset time (fall through as non-transient) /
the old transient path doesn't regress. Run this file before changing
classify/transcript_wake_decision.
"""
import json
import os
import sys
import tempfile
import time
import unittest.mock as um

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "wake_watcher"))
import wake_watcher as ww  # noqa: E402

LIMIT = "You've hit your session limit · resets 3:20am (Australia/Melbourne)"


def _iso_utc(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _mk(detail_text, retry_after=False, err_ts=None):
    err_rec = {"type": "assistant", "isApiErrorMessage": True,
               "message": {"role": "assistant",
                           "content": [{"type": "text", "text": detail_text}]}}
    if err_ts is not None:
        err_rec["timestamp"] = _iso_utc(err_ts)
    recs = [
        {"type": "user", "message": {"role": "user", "content": "task"}},
        err_rec,
    ]
    if retry_after:
        recs.append({"type": "user", "message": {"role": "user", "content": "继续"}})
    f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False, encoding='utf-8')
    for r in recs:
        f.write(json.dumps(r) + "\n")
    f.close()
    return f.name


def test_limit_before_reset_skips_with_wake_at():
    p = _mk(LIMIT)
    try:
        d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "skip" and "waiting for reset before waking" in d["reason"]
    finally:
        os.unlink(p)


def _fake_verdict(reset_epoch):
    return {"transient": False, "session_limit": True, "reset_epoch": reset_epoch,
            "reason": "t", "matched": None, "vetoed_by": "t"}


def test_limit_after_reset_sends():
    p = _mk(LIMIT)
    try:
        with um.patch.object(ww, 'classify', return_value=_fake_verdict(time.time() - 3600)):
            d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "send"
    finally:
        os.unlink(p)


def test_limit_after_reset_but_retried_skips():
    p = _mk(LIMIT, retry_after=True)
    try:
        with um.patch.object(ww, 'classify', return_value=_fake_verdict(time.time() - 3600)):
            d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "skip" and "user retry already followed" in d["reason"]
    finally:
        os.unlink(p)


def test_limit_without_reset_time_stays_plain_veto():
    p = _mk("You've reached your usage limit")
    try:
        d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "skip" and "non-transient" in d["reason"]
    finally:
        os.unlink(p)


def test_transient_old_path_unaffected():
    p = _mk("API Error: Connection closed mid-response")
    try:
        d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "send" and "live error" in d["reason"]
    finally:
        os.unlink(p)


def test_unparseable_reset_time_fails_closed():
    p = _mk(LIMIT)
    try:
        with um.patch.object(ww, 'classify', return_value=_fake_verdict(None)):
            d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "skip" and "parse failed" in d["reason"]
    finally:
        os.unlink(p)


# -- incident regression (2026-07-12), all go through the real classify, no mocking -----
# test_limit_after_reset_sends above hand-fakes a "past reset_epoch" via mock; the real
# classify, re-parsing at scan time, can never produce a past timestamp on its own -> the
# send branch was unreachable, and the mock happened to paper right over that. After the
# fix, reset_epoch anchors to the error record's own timestamp; the tests below pin all
# three states via the real path.


def test_limit_scan_after_reset_sends_real_path():
    """The error happened 26h ago -> the "next 3:20am" it names has already passed by at
    least 2h -> this scan round must send. This is exactly the scene of three sessions
    getting delayed 24h on the night of 2026-07-11 (scanned at 05:10:07, 7 seconds after
    reset)."""
    p = _mk(LIMIT, err_ts=time.time() - 26 * 3600)
    try:
        d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "send", d
        assert "reset point passed" in d["reason"]
    finally:
        os.unlink(p)


def test_limit_scan_before_reset_still_waits_real_path():
    """The error just happened, the reset clock-time is 2h away -> still waits; the anchor
    fix must not introduce an early wake."""
    now = time.time()
    lt = time.localtime(now + 2 * 3600)
    h12 = lt.tm_hour % 12 or 12
    ampm = "am" if lt.tm_hour < 12 else "pm"
    text = f"You've hit your session limit · resets {h12}:{lt.tm_min:02d}{ampm} (Australia/Melbourne)"
    p = _mk(text, err_ts=now)
    try:
        d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "skip" and "waiting for reset before waking" in d["reason"], d
    finally:
        os.unlink(p)


def test_limit_after_reset_with_retry_still_dedups_real_path():
    p = _mk(LIMIT, err_ts=time.time() - 26 * 3600, retry_after=True)
    try:
        d = ww.transcript_wake_decision({"transcriptPath": p})
        assert d["action"] == "skip" and "user retry already followed" in d["reason"], d
    finally:
        os.unlink(p)


# -- auth-class non-transient errors must never go silent --


def test_auth_blocked_detail_predicate():
    assert ww._is_auth_blocked_detail("Login expired")
    assert ww._is_auth_blocked_detail("OAuth token has been revoked · please run /login")
    assert not ww._is_auth_blocked_detail(LIMIT)
    assert not ww._is_auth_blocked_detail("API Error: Connection closed mid-response")
    assert not ww._is_auth_blocked_detail(None)
    assert not ww._is_auth_blocked_detail("")
