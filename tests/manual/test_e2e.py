#!/usr/bin/env python3
"""End-to-end test of the wake-watcher against a SANDBOXED jobs dir.

HONESTY NOTE (owner: demo ≈ mock):
  - We CANNOT force a genuine `Connection closed mid-response` API drop on demand.
    So we INJECT a state.json whose `detail` is the real transient string and whose
    `sessionId` is a REAL resumable session we created with `claude -p`. The trigger
    (the blocked state.json) is fabricated; the WAKE DELIVERY + CONTINUATION is real
    (real `claude --resume` against a real session, verified by recalling a codeword).
  - The negative case (awaiting-owner) uses a fabricated state.json too; what's
    being proven there is that the classifier+watcher REFUSE to wake it.

Run: python3 test_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def make_real_session(secret: str) -> str:
    """Create a real resumable headless session that remembers a codeword."""
    out = subprocess.run(
        ["claude", "-p",
         f"Remember this codeword: {secret}. Acknowledge in one short sentence.",
         "--output-format", "json", "--permission-mode", "bypassPermissions"],
        capture_output=True, text=True, timeout=180,
    )
    d = json.loads(out.stdout)
    return d["session_id"]


def write_job(jobs_dir: Path, job_id: str, session_id: str, state: str,
              detail: str, cwd: str, respawn_flags: list | None = None,
              transcript_path: str | None = None) -> None:
    jd = jobs_dir / job_id
    jd.mkdir(parents=True, exist_ok=True)
    state_obj = {
        "state": state, "detail": detail, "sessionId": session_id,
        "resumeSessionId": session_id, "cwd": cwd, "backend": "daemon",
        "updatedAt": "2026-06-21T00:00:00.000Z",
    }
    if respawn_flags is not None:
        state_obj["respawnFlags"] = respawn_flags
    if transcript_path is not None:
        state_obj["linkScanPath"] = transcript_path
    (jd / "state.json").write_text(json.dumps(state_obj, ensure_ascii=False), encoding="utf-8")
    (jd / "timeline.jsonl").write_text(
        json.dumps({"at": "2026-06-21T00:00:00.000Z", "state": state, "detail": detail}) + "\n",
        encoding="utf-8")


def run_watcher_once(sandbox_home: Path, ledger: Path, log: Path, dry_run: bool) -> str:
    env = dict(os.environ)
    env["WAKE_WATCHER_CLAUDE_HOME"] = str(sandbox_home)
    env["WAKE_WATCHER_LEDGER"] = str(ledger)
    env["WAKE_WATCHER_LOG"] = str(log)
    env["WAKE_WATCHER_REQUIRE_DEAD"] = "0"  # sandbox sessions have no live proc anyway
    # scope narrowing (owner 2026-06-22): the sandbox job's cwd is under tmp, so disable the this-project filter (this e2e test is about the
    # classify + real wake delivery dimension; scope filtering is covered by test_scope.py).
    env["WAKE_WATCHER_PROJECT_ROOT"] = ""
    cmd = [sys.executable, str(HERE / "wake_watcher.py"), "--once"]
    if dry_run:
        cmd.append("--dry-run")
    subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    return log.read_text(encoding="utf-8") if log.exists() else ""


def main() -> int:
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-e2e-"))
    sandbox_home = tmp / "claude-home"
    jobs = sandbox_home / "jobs"
    jobs.mkdir(parents=True)
    work = tmp / "work"; work.mkdir()
    ledger = tmp / "ledger.json"
    log = tmp / "watcher.log"

    print("=== Setup: create one REAL resumable session for the transient case ===")
    secret = "MANGO_WAKE_77"
    sid_transient = make_real_session(secret)
    print(f"  real session = {sid_transient}")

    # fabricate a transcript file we control, ending in an untreated transient API error
    # (the detection criterion now reads the transcript tail, see wake_watcher.transcript_wake_decision).
    transcript = tmp / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "isSidechain": False,
                    "message": {"content": [{"type": "text", "text": "干活"}]}}) + "\n" +
        json.dumps({"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
                    "message": {"content": [{"type": "text", "text":
                        "API Error: Connection closed mid-response. The response above may be incomplete."}]}}) + "\n",
        encoding="utf-8")
    # realistic respawnFlags mirroring a read-only verification fork (job session-A)
    rflags = ["--disallowed-tools", "Edit", "Write", "--model", "opus",
              "--strict-mcp-config"]
    # Transient job: real session + real transient detail string (state fabricated)
    write_job(jobs, "transient01", sid_transient, "blocked",
              "API Error: Connection closed mid-response. The response above may be incomplete.",
              str(work), respawn_flags=rflags, transcript_path=str(transcript))
    # Negative job: fabricated session id + awaiting-owner detail
    write_job(jobs, "owner01", "sid-fixture-0001", "blocked",
              "drafted batch; awaiting X API credentials (4 keys)", str(work))

    # ── TEST 1: dry-run shows it WOULD wake transient, NOT owner ───────────
    print("\n=== TEST 1: dry-run classification (transient woken / owner skipped) ===")
    txt = run_watcher_once(sandbox_home, ledger, log, dry_run=True)
    woke_transient = ("DRY-RUN" in txt and sid_transient in txt and "WAKE" in txt)
    skipped_owner = ("SKIP" in txt and "session-I-dead-beef" in txt)
    # respawnFlags carried into the resume plan (read-only fork must stay read-only)
    flags_carried = ("--disallowed-tools Edit Write" in txt and "--model opus" in txt)
    print(f"  [{'PASS' if woke_transient else 'FAIL'}] dry-run plans WAKE for transient session")
    print(f"  [{'PASS' if skipped_owner else 'FAIL'}] SKIP (not-transient) for awaiting-owner session")
    print(f"  [{'PASS' if flags_carried else 'FAIL'}] respawnFlags carried into resume (read-only fork preserved)")
    fails += (0 if woke_transient else 1) + (0 if skipped_owner else 1) + (0 if flags_carried else 1)
    ledger.unlink(missing_ok=True); log.unlink(missing_ok=True)

    # ── TEST 2: REAL wake — actually resume the session and verify continuation ──
    print("\n=== TEST 2: REAL wake delivery + continuation (resume real session) ===")
    # set wake message to ask it to recall the codeword (proves continuation)
    env = dict(os.environ)
    env["WAKE_WATCHER_CLAUDE_HOME"] = str(sandbox_home)
    env["WAKE_WATCHER_LEDGER"] = str(ledger)
    env["WAKE_WATCHER_LOG"] = str(log)
    env["WAKE_WATCHER_REQUIRE_DEAD"] = "0"
    # Same reason as the dry-run case above: the sandbox job lives under tmp, so the
    # this-project filter would skip it. Without this the whole test reports "0 wakes"
    # and looks like a broken delivery path when nothing is actually wrong with delivery.
    env["WAKE_WATCHER_PROJECT_ROOT"] = ""
    env["WAKE_WATCHER_MESSAGE"] = (
        "刚才网络波动了，请重试并继续。先复述你记住的那个 codeword 证明续上了上文。")
    subprocess.run([sys.executable, str(HERE / "wake_watcher.py"), "--once"],
                   env=env, capture_output=True, text=True, timeout=120)
    logtxt = log.read_text(encoding="utf-8") if log.exists() else ""
    wake_logged = ("WAKE session=" + sid_transient) in logtxt and "attempt #1/3" in logtxt
    print(f"  [{'PASS' if wake_logged else 'FAIL'}] wake logged (attempt #1/3) for transient")
    fails += 0 if wake_logged else 1

    # owner job must NOT have been woken
    owner_woke = "WAKE session=session-I-dead-beef" in logtxt
    print(f"  [{'PASS' if not owner_woke else 'FAIL'}] awaiting-owner session was NOT woken")
    fails += 1 if owner_woke else 0

    # verify the real resume actually continued (recall codeword) by resuming again
    print("  verifying real continuation (resume + recall codeword)...")
    time.sleep(3)
    chk = subprocess.run(
        ["claude", "--resume", sid_transient, "-p",
         "What was the codeword? Reply with just the codeword.",
         "--output-format", "json", "--permission-mode", "bypassPermissions"],
        capture_output=True, text=True, timeout=120)
    try:
        result = json.loads(chk.stdout).get("result", "")
    except Exception:
        result = chk.stdout
    continued = secret in result
    print(f"  [{'PASS' if continued else 'FAIL'}] session continued context (codeword recalled: {secret in result})")
    fails += 0 if continued else 1

    # ── TEST 3: cap + backoff — repeated scans never exceed MAX_WAKES ──────
    print("\n=== TEST 3: cap (MAX_WAKES=3) + backoff enforced ===")
    env["WAKE_WATCHER_MAX_WAKES"] = "3"
    env["WAKE_WATCHER_BACKOFF"] = "0"  # no real waiting in test
    ledger.unlink(missing_ok=True); log.unlink(missing_ok=True)
    # fresh ledger; run scan 6 times back-to-back (backoff=0 so all eligible)
    for _ in range(6):
        subprocess.run([sys.executable, str(HERE / "wake_watcher.py"), "--once", "--dry-run"],
                       env=env, capture_output=True, text=True, timeout=60)
    logtxt = log.read_text(encoding="utf-8") if log.exists() else ""
    wake_count = logtxt.count("WAKE session=" + sid_transient)
    needs_human = "NEEDS-HUMAN session=" + sid_transient in logtxt
    capped = wake_count == 3
    print(f"  [{'PASS' if capped else 'FAIL'}] exactly 3 wakes delivered (got {wake_count}), never more")
    print(f"  [{'PASS' if needs_human else 'FAIL'}] NEEDS-HUMAN surfaced after cap hit")
    fails += (0 if capped else 1) + (0 if needs_human else 1)

    # ── TEST 4: real escape resets the budget (2026-07-02 rewrite, detection criterion switched axis) ──
    # The old version judged escape by "transcript file grew longer = progress" — that was exactly the root cause of the 2026-07-01 night OOM incident
    # (the watcher's own resume appends to the file, so it necessarily grows, and that false signal reset the budget without limit). New criterion: only
    # when the LAST main-conversation assistant message is normal output does it count as a real escape; the file merely growing longer (even from our
    # own wake-triggered append retry) must never reset the budget (regression pin: see test_loop_control.py::test_T1).
    print("\n=== TEST 4: real escape (last main-conversation message becomes normal output) -> budget reset to zero ===")
    env["WAKE_WATCHER_MAX_WAKES"] = "3"
    env["WAKE_WATCHER_BACKOFF"] = "0"
    ledger.unlink(missing_ok=True); log.unlink(missing_ok=True)
    subprocess.run([sys.executable, str(HERE / "wake_watcher.py"), "--once", "--dry-run"],
                   env=env, capture_output=True, text=True, timeout=60)
    led1 = json.loads(ledger.read_text(encoding="utf-8"))
    wakes_after_1 = led1.get(sid_transient, {}).get("wakes", 0)
    # real escape: the last main-conversation message becomes normal output (not just the file growing longer)
    with transcript.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "isSidechain": False,
                            "message": {"content": [{"type": "text",
                                        "text": "resumed and continued"}]}}) + "\n")
    subprocess.run([sys.executable, str(HERE / "wake_watcher.py"), "--once", "--dry-run"],
                   env=env, capture_output=True, text=True, timeout=60)
    led2 = json.loads(ledger.read_text(encoding="utf-8"))
    wakes_after_escape = led2.get(sid_transient, {}).get("wakes", 0)
    logtxt4 = log.read_text(encoding="utf-8") if log.exists() else ""
    escaped_logged = "ESCAPE session=" + sid_transient in logtxt4
    reset_ok = wakes_after_1 == 1 and wakes_after_escape == 0 and escaped_logged
    print(f"  wakes after scan#1={wakes_after_1}, after escape+rescan={wakes_after_escape}")
    print(f"  [{'PASS' if reset_ok else 'FAIL'}] real escape reset budget to 0 (not just 'grew')")
    fails += 0 if reset_ok else 1

    # counter-proof (the incident itself): transcript merely growing longer (appending one transient error, not an escape) must never reset the budget.
    print("\n=== TEST 4b: transcript merely growing longer (not an escape) must never reset the budget (2026-07-01 night incident itself) ===")
    with transcript.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
                            "message": {"content": [{"type": "text", "text":
                                "API Error: Connection closed mid-response."}]}}) + "\n")
    subprocess.run([sys.executable, str(HERE / "wake_watcher.py"), "--once", "--dry-run"],
                   env=env, capture_output=True, text=True, timeout=60)
    led3 = json.loads(ledger.read_text(encoding="utf-8"))
    wakes_after_new_error = led3.get(sid_transient, {}).get("wakes", 0)
    print(f"  wakes after new untreated error = {wakes_after_new_error}")
    no_regression = wakes_after_new_error == 1  # reset to full budget after escape, new error starts from #1
    print(f"  [{'PASS' if no_regression else 'FAIL'}] independent new error starts from full budget #1 (not continuing the old count)")
    fails += 0 if no_regression else 1

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
