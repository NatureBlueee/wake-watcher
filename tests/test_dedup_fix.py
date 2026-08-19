"""Regression test (owner, 2026-06-25):

Detection criterion = read the tail of the transcript (.jsonl, state.json.linkScanPath),
and judge whether [the last assistant message] is [a transient API error with no retry
after it]:
  - No (normal output / already moved past it) → never send.
  - Yes, but there's already a user retry after it → don't send again.
  - Yes, and there's no user retry after it → send once (one error, one send — naturally
    deduplicated).
Fully retired: scanning the single state.json.detail field for an error string (stale),
using transcript file size as a progress signal, and error_signature-based dedup.

This test uses a sandbox job + a controlled transcript (manually simulating the AI
continuing / erroring / already having a retry sent) + multiple rounds of --once, with
delivery going through the WAKE_WATCHER_FAKE_DELIVER test seam (deterministic, doesn't
really spawn claude). It asserts four things:
 A. stale error — the AI has already moved past it (the last assistant turn is normal
    output) → never send (0 times).
 B. there's already a user retry after the error → don't send again.
 C. the same error text can legitimately reappear: each "new trailing error with no retry
    after it" sends once.
 D. if the AI keeps failing to respond after retries (a new error keeps showing up at the
    end) → stops after hitting MAX_WAKES, with NEEDS-HUMAN, not sending forever; if the AI
    genuinely moves past it (normal output) → the budget resets, and a later independent
    error gets a full budget again.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# OSS layout: production code lives in ../src/wake_watcher/, tests in ../tests/.
# WAKE_WATCHER_SCRIPT lets a packager point at a different install location.
_SRC = HERE.parent / "src" / "wake_watcher"
WW_SCRIPT = pathlib.Path(os.environ.get("WAKE_WATCHER_SCRIPT", str(_SRC / "wake_watcher.py")))

E1 = "API Error: Unable to connect to API (UNKNOWN_CERTIFICATE_VERIFICATION_ERROR)"
E2 = "API Error: 503 Service temporarily unavailable"


# ── transcript record construction (observed convention: assistant API error = isApiErrorMessage + text) ──
def asst_error(text=E1):
    return {"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def asst_normal(text):
    return {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def user_msg(text):
    return {"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return 0 if cond else 1


class Sandbox:
    """A sandboxed job: controlled transcript + blocked state.json + isolated ledger/log."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.root = (tmp / "MyProject").resolve()
        (self.root / "work").mkdir(parents=True)
        self.home = tmp / "claude-home"
        self.jobs = self.home / "jobs"
        self.jobs.mkdir(parents=True)
        self.jd = self.jobs / "stuck"
        self.jd.mkdir()
        self.transcript = self.jd / "transcript.jsonl"
        self.log = tmp / "watcher.log"
        self.ledger = tmp / "ledger.json"
        self.sid = "sid-stuck"
        (self.jd / "state.json").write_text(json.dumps({
            "state": "blocked", "sessionId": self.sid, "resumeSessionId": self.sid,
            "cwd": str(self.root / "work"), "backend": "daemon",
            "linkScanPath": str(self.transcript),
        }, ensure_ascii=False), encoding="utf-8")

    def set_transcript(self, recs):
        self.transcript.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")

    def append(self, *recs):
        with self.transcript.open("a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def run_once(self, fake_net="1"):
        env = dict(os.environ)
        env.update({
            "WAKE_WATCHER_CLAUDE_HOME": str(self.home), "WAKE_WATCHER_LEDGER": str(self.ledger),
            "WAKE_WATCHER_LOG": str(self.log), "WAKE_WATCHER_REQUIRE_DEAD": "0",
            "WAKE_WATCHER_NEEDS_HUMAN": str(self.tmp / "needs-human.log"),
            "WAKE_WATCHER_FAKE_NET": fake_net,  # network up by default (no dependency on a real network); "0" = simulated offline (T4)
            "WAKE_WATCHER_PROJECT_ROOT": str(self.root), "WAKE_WATCHER_WATERMARK": "",
            "WAKE_WATCHER_BACKOFF": "0", "WAKE_WATCHER_FAKE_DELIVER": "1",
        })
        env.pop("WAKE_WATCHER_DO_NOT_WAKE", None)
        env.pop("WAKE_WATCHER_DO_NOT_WAKE_FILE", None)
        subprocess.run([sys.executable, str(WW_SCRIPT), "--once"],
                       env=env, capture_output=True, text=True, timeout=120)

    @property
    def log_text(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    @property
    def wake_count(self):
        return self.log_text.count(f"WAKE session={self.sid} attempt")


def test_A_stale_error_not_rewoken():
    print("\n=== A: stale error (the AI already moved past it, last assistant turn is normal) → never send ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-A-"))
    try:
        sb = Sandbox(tmp)
        # An error happened at some point in the past, but the AI already retried/moved past it — the last assistant turn is normal output
        sb.set_transcript([asst_normal("开始干活"), asst_error(E1),
                           user_msg("网络波动了请继续"), asst_normal("全部勘测做完了，在等你回答")])
        for _ in range(5):  # repeated scans should never send (old bug: this would keep sending)
            sb.run_once()
        fails += _check(sb.wake_count == 0,
                        f"a stale error the AI already moved past: 0 WAKEs (got {sb.wake_count}; the old bug would keep sending)")
        fails += _check("moved past the error" in sb.log_text, "SKIP trace: moved past the error / still working")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_B_error_with_retry_not_resent():
    print("\n=== B: there's already a user retry after the error → don't send again ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-B-"))
    try:
        sb = Sandbox(tmp)
        sb.set_transcript([asst_normal("干活"), asst_error(E1), user_msg("重试继续")])
        for _ in range(4):
            sb.run_once()
        fails += _check(sb.wake_count == 0,
                        f"a user retry already follows the error: 0 WAKEs (got {sb.wake_count})")
        fails += _check("user retry already followed" in sb.log_text, "SKIP trace: a user retry already follows the error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_C_untreated_trailing_error_sends_once_per_instance():
    print("\n=== C: a trailing transient error with no retry after it → sends once; each 'new trailing error' sends once ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-C-"))
    try:
        sb = Sandbox(tmp)
        # the trailing entry is a transient error with no retry after it
        sb.set_transcript([asst_normal("干活"), asst_error(E1)])
        sb.run_once()
        fails += _check(sb.wake_count == 1, f"the first trailing error → sends once (got {sb.wake_count})")

        # simulates: after the watcher delivers, a user retry continues into the same transcript → shouldn't send again next round
        sb.append(user_msg("刚才网络波动了请重试继续"))
        sb.run_once()
        fails += _check(sb.wake_count == 1, f"scanning again after the retry continues → doesn't resend (still {sb.wake_count})")

        # after the retry the AI throws [a new] trailing error (the same text E1 is also valid) with no retry after it → sends once more
        sb.append(asst_error(E1))
        sb.run_once()
        fails += _check(sb.wake_count == 2,
                        f"a new trailing error (same text) with no retry after it → sends once more (got {sb.wake_count})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_D_cap_then_needs_human_and_escape_resets_budget():
    print("\n=== D: a new error keeps showing up after retries → NEEDS-HUMAN after hitting MAX_WAKES; a real escape → budget resets ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-D-"))
    try:
        sb = Sandbox(tmp)
        sb.set_transcript([asst_normal("干活"), asst_error(E1)])
        # simulates an infinite loop: sends once per round → retry continues → a new error shows up again. MAX_WAKES=3, should stop on the 4th round.
        for _ in range(6):
            sb.run_once()
            # after the watcher delivers, the retry continues, then the AI reports a new error again (the network is still down)
            sb.append(user_msg("retry"), asst_error(E1))
        fails += _check(sb.wake_count == 3,
                        f"an infinite-loop error: stops after at most MAX_WAKES(3) sends (got {sb.wake_count})")
        fails += _check(f"NEEDS-HUMAN session={sb.sid}" in sb.log_text,
                        "NEEDS-HUMAN surfaces after hitting the cap (not silent)")

        # now the AI genuinely moves past it (normal output) → the budget should reset
        sb.append(asst_normal("终于恢复，活干完了"))
        sb.run_once()
        wake_after_escape = sb.wake_count
        fails += _check(wake_after_escape == 3, "the round where it moves past doesn't send (the last turn is normal output)")

        # then a new, independent error comes in (with no retry after it) → full budget, sends again
        sb.append(asst_error(E2))
        sb.run_once()
        fails += _check(sb.wake_count == 4,
                        f"the budget resets after moving past it, the new independent error sends again (got {sb.wake_count}, expected 4)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_E_stalled_mid_stream_wakes_end_to_end():
    """Regression (owner, 2026-07-10): "Response stalled mid-stream" / "Server error
    mid-response" / "Connection closed while thinking" used to be missing from
    classify.py's whitelist, and end-to-end that meant wake_watcher never sent at all
    (default-deny was misjudged as a real stop). The whitelist gap confirmed by the logs
    covers sessions session-C/session-D/session-B/session-E, but the only one where it
    actually caused extra stall time was session-B (~7h; the owner ultimately typed
    "continue" by hand to rescue it — not wake-watcher). The other gap instances are
    just as real, but a faster user retry / teammate message happened to land within the
    same scan window, so no extra stall was observed there; "connection closed while
    thinking" is another historical instance of the same family, found separately during
    a full scan (2026-06-16, session session-F — no retained logs to confirm whether it was
    actually scanned at the time). This doesn't just test the classify() unit — it runs
    the real wake_watcher.py --once end-to-end, pinning down that "this class of error
    really can send a wake."
    """
    print("\n=== E: the stalled/mid-response/conn-closed family with no retry after it → really sends a wake end-to-end ===")
    fails = 0
    for label, text in [
        ("Response stalled mid-stream", "API Error: Response stalled mid-stream. The response above may be incomplete."),
        ("Server error mid-response", "API Error: Server error mid-response. The response above may be incomplete."),
        ("Connection closed while thinking", "API Error: Connection closed while thinking, before producing a response. Try again."),
    ]:
        tmp = Path(tempfile.mkdtemp(prefix="wake-E-"))
        try:
            sb = Sandbox(tmp)
            sb.set_transcript([asst_normal("干活"), asst_error(text)])
            sb.run_once()
            fails += _check(sb.wake_count == 1,
                            f"[{label}] trailing with no retry after it → sends once (got {sb.wake_count})")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return fails


def run():
    fails = 0
    fails += test_A_stale_error_not_rewoken()
    fails += test_B_error_with_retry_not_resent()
    fails += test_C_untreated_trailing_error_sends_once_per_instance()
    fails += test_D_cap_then_needs_human_and_escape_resets_budget()
    fails += test_E_stalled_mid_stream_wakes_end_to_end()
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
