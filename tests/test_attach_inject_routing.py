#!/usr/bin/env python3
"""Unit test for the dead/live routing split (fixed 2026-06-26; red line: a live
daemon-bg agent must NEVER --resume/--fork-session).

Tests only the routing/dispatch logic, not real PTY byte sequences (that depends on
real claude CLI rendering -- see docs/PTY-INJECT-SMOKE-TEST.md or the manual smoke-test
steps in the comment at the end of this file). Method: use WAKE_WATCHER_FAKE_AGENTS to
control what agent_liveness_lookup() decides, use mock.patch to block subprocess.Popen
(the dead path must never really spawn), monkeypatch the module-level
`_pty_attach_inject` to block the real PTY (the live_bg path must never really start
pty.fork). Everything runs in-process via ww.deliver_wake()/ww.scan_once(); nothing
subprocesses out to run the whole wake_watcher.py (that's test_e2e.py's job, which goes
through main()'s watermark loading).

Run: python3 test_attach_inject_routing.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src" / "wake_watcher"))
# This file exercises the PTY-injection routing, which ships disabled by default
# (WAKE_WATCHER_ENABLE_PTY_INJECT). The flag is read at import time, so it has to
# be set before wake_watcher is imported -- not inside a test body.
os.environ.setdefault("WAKE_WATCHER_ENABLE_PTY_INJECT", "1")
import wake_watcher as ww  # noqa: E402

TRANSIENT = "API Error: Connection closed mid-response. The response above may be incomplete."


def _check(cond: bool, label: str) -> int:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return 0 if cond else 1


def _clear_fake_agents() -> None:
    os.environ.pop("WAKE_WATCHER_FAKE_AGENTS", None)


# -- deliver_wake() routing: four states (live_bg / live_other / dead / unknown) --------
def test_live_bg_routes_to_pty_inject() -> int:
    print("\n=== ROUTE: live daemon-bg agent -> PTY injection (never --resume) ===")
    fails = 0
    sid = "sid-live-bg-001"
    os.environ["WAKE_WATCHER_FAKE_AGENTS"] = json.dumps([
        {"sessionId": sid, "id": "bgid-0001", "kind": "background",
         "status": "idle", "state": "blocked"},
    ])
    calls = []

    def fake_inject(short_id, message, session_id=None, transcript_path=None):
        calls.append((short_id, message, session_id, transcript_path))
        return True

    orig = ww._pty_attach_inject
    ww._pty_attach_inject = fake_inject
    try:
        with mock.patch("wake_watcher.subprocess.Popen") as popen_mock:
            ok, info = ww.deliver_wake(sid, "/tmp/somewhere", dry_run=False,
                                       respawn_flags=None, transcript_path="/tmp/fake.jsonl")
        fails += _check(ok, "live_bg -> deliver_wake returns ok=True")
        fails += _check(len(calls) == 1 and calls[0][0] == "bgid-0001",
                        "called _pty_attach_inject(short_id='bgid-0001', ...)")
        fails += _check(not popen_mock.called, "live_bg -> must never call Popen/--resume (owner red line)")
        fails += _check("pty-attach-inject" in info, f"info states it took the pty-attach-inject path (got: {info!r})")
    finally:
        ww._pty_attach_inject = orig
        _clear_fake_agents()
    return fails


def test_dead_orphan_routes_to_resume() -> int:
    print("\n=== ROUTE: dead orphan (not in the active list) -> the original --resume path ===")
    fails = 0
    sid = "sid-dead-orphan-001"
    os.environ["WAKE_WATCHER_FAKE_AGENTS"] = "[]"  # query succeeds, empty list -> not present -> dead
    calls = []

    class _FakeProc:
        pid = 99999

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc()

    try:
        with mock.patch("wake_watcher.subprocess.Popen", fake_popen):
            ok, info = ww.deliver_wake(sid, "/tmp/somewhere", dry_run=False,
                                       respawn_flags=["--model", "opus"], transcript_path=None)
        fails += _check(ok, "dead -> deliver_wake returns ok=True")
        fails += _check(len(calls) == 1 and "--resume" in calls[0] and sid in calls[0],
                        "really called --resume (a dead orphan still takes the original path)")
        fails += _check("resume path" in info, f"info is tagged resume path (got: {info!r})")
    finally:
        _clear_fake_agents()
    return fails


def test_live_other_defers_no_resume_no_inject() -> int:
    print("\n=== ROUTE: alive but not an injectable background (e.g. interactive) -> DEFER, no guessing, no action ===")
    fails = 0
    sid = "sid-live-interactive-001"
    os.environ["WAKE_WATCHER_FAKE_AGENTS"] = json.dumps([
        {"sessionId": sid, "kind": "interactive", "status": "busy"},
    ])
    try:
        with mock.patch("wake_watcher.subprocess.Popen") as popen_mock:
            ok, info = ww.deliver_wake(sid, None, dry_run=False)
        fails += _check(not ok, "live_other -> ok=False (leave it alone)")
        fails += _check(not popen_mock.called, "live_other -> must never call --resume (it's alive)")
        fails += _check("DEFER" in info, f"info is tagged DEFER (got: {info!r})")
    finally:
        _clear_fake_agents()
    return fails


def test_unknown_liveness_defers_never_falls_back_to_resume() -> int:
    print("\n=== ROUTE: liveness check fails (unknown) -> defer, never treat it as 'dead' and go resume (the core red line) ===")
    fails = 0
    sid = "sid-unknown-liveness-001"
    os.environ["WAKE_WATCHER_FAKE_AGENTS"] = "not-valid-json{{{"  # force _list_active_agents to fail parsing
    try:
        with mock.patch("wake_watcher.subprocess.Popen") as popen_mock:
            ok, info = ww.deliver_wake(sid, None, dry_run=False)
        fails += _check(not ok, "unknown -> ok=False")
        fails += _check(not popen_mock.called,
                        "unknown -> must never fall back to --resume (if you can't tell, don't guess -- a hard red line)")
        fails += _check("never guess liveness" in info, f"info contains 'never guess liveness' (got: {info!r})")
    finally:
        _clear_fake_agents()
    return fails


def test_verify_transcript_user_turn_ignores_stale_match_before_baseline() -> int:
    print("\n=== VERIFY: delivery verification must pin the baseline -- don't mistake 'a success from long ago' for 'this one succeeded too' ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-verify-baseline-"))
    try:
        transcript = tmp / "t.jsonl"
        msg = "刚才网络波动了，请重试继续"
        # An identical user turn already exists in history (a previous wake actually succeeded once).
        transcript.write_text(
            json.dumps({"type": "user", "isSidechain": False,
                        "message": {"content": [{"type": "text", "text": msg}]}}) + "\n",
            encoding="utf-8")
        baseline_len = len(ww._load_transcript_records(str(transcript)))

        # Without pinning the baseline (baseline_len=0) -> this historical line gets misjudged as
        # "this one succeeded too" (old behavior, now fixed).
        false_positive = ww._verify_transcript_user_turn(str(transcript), msg, timeout=0.5, baseline_len=0)
        fails += _check(false_positive, "(control group) with baseline=0 the old logic misjudges True -- proving the risk is real")

        # After pinning the baseline (only lines added after this historical one count) -> no new
        # line -> must judge False, must not falsely report success.
        correctly_false = ww._verify_transcript_user_turn(str(transcript), msg, timeout=0.5,
                                                           baseline_len=baseline_len)
        fails += _check(not correctly_false,
                        "with the baseline pinned, an old historical match doesn't count -> correctly judges False (no new user turn)")

        # Now really append a new one (simulating that this injection really succeeded) -> with the
        # baseline pinned, it should correctly judge True.
        with transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "isSidechain": False,
                                "message": {"content": [{"type": "text", "text": msg}]}}) + "\n")
        now_true = ww._verify_transcript_user_turn(str(transcript), msg, timeout=0.5,
                                                    baseline_len=baseline_len)
        fails += _check(now_true, "a user turn added after the baseline -> correctly judges True")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_inject_failure_does_not_fallback_to_resume() -> int:
    print("\n=== ROUTE: PTY injection fails/unverified -> no fallback to --resume, just logs it for the next round ===")
    fails = 0
    sid = "sid-live-bg-inject-fail-001"
    os.environ["WAKE_WATCHER_FAKE_AGENTS"] = json.dumps([
        {"sessionId": sid, "id": "bgid-0002", "kind": "background"},
    ])
    orig = ww._pty_attach_inject
    ww._pty_attach_inject = lambda *a, **k: False  # simulate injection failure / delivery not verified
    try:
        with mock.patch("wake_watcher.subprocess.Popen") as popen_mock:
            ok, info = ww.deliver_wake(sid, None, dry_run=False)
        fails += _check(not ok, "injection fails -> ok=False")
        fails += _check(not popen_mock.called,
                        "injection fails -> must never fall back to --resume (resuming a live session is a dangerous action)")
        fails += _check("no fallback" in info and "next round" in info,
                        f"info contains 'no fallback' + 'next round' retry (got: {info!r})")
    finally:
        ww._pty_attach_inject = orig
        _clear_fake_agents()
    return fails


def test_dry_run_reflects_all_four_routes() -> int:
    print("\n=== ROUTE: --dry-run gives a correct 'what it would do' preview for all four states, without acting for real ===")
    fails = 0
    cases = [
        ("sid-dry-live-bg", json.dumps([{"sessionId": "sid-dry-live-bg", "id": "aa11",
                                         "kind": "background"}]), "pty-attach-inject"),
        ("sid-dry-dead", "[]", "resume"),
        ("sid-dry-live-other", json.dumps([{"sessionId": "sid-dry-live-other",
                                            "kind": "interactive"}]), "DEFER"),
        ("sid-dry-unknown", "{bad json", "DEFER"),
    ]
    for sid, fake_agents, expect_substr in cases:
        os.environ["WAKE_WATCHER_FAKE_AGENTS"] = fake_agents
        try:
            ok, info = ww.deliver_wake(sid, "/tmp/x", dry_run=True)
            fails += _check(ok, f"dry-run always ok=True (sid={sid})")
            fails += _check("DRY-RUN" in info and expect_substr in info,
                            f"sid={sid} dry-run preview contains {expect_substr!r} (got: {info!r})")
        finally:
            _clear_fake_agents()
    return fails


# -- scan_once() level: completion-verdict double-check (vitality=done -> never wake) ---
def test_done_verdict_skips_wake_even_if_transcript_says_send() -> int:
    print("\n=== GUARD: vitality verdict=done -> completion-verdict double-check, never wake (owner accuracy first) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-done-guard-"))
    try:
        jobs = tmp / "jobs"
        jobs.mkdir()
        work = tmp / "work"
        work.mkdir()
        transcript = tmp / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
                        "message": {"content": [{"type": "text", "text": TRANSIENT}]}}) + "\n",
            encoding="utf-8")
        sid = "sid-vitality-done-001"
        jd = jobs / "j1"
        jd.mkdir()
        (jd / "state.json").write_text(json.dumps({
            "state": "blocked", "detail": TRANSIENT, "sessionId": sid, "cwd": str(work),
            "updatedAt": "2099-01-01T00:00:00Z", "linkScanPath": str(transcript),
        }), encoding="utf-8")

        saved = {
            attr: getattr(ww, attr)
            for attr in ("JOBS_DIR", "LEDGER_FILE", "LOG_FILE", "NEEDS_HUMAN_FILE",
                        "DO_NOT_WAKE_FILE", "PROJECT_ROOT", "SAFETY_REQUIRE_DEAD_PROCESS")
        }
        os.environ["WAKE_WATCHER_FAKE_VITALITY"] = f"{sid}=done"
        os.environ["WAKE_WATCHER_FAKE_AGENTS"] = "[]"  # irrelevant (never reaches deliver_wake), defensive setting
        try:
            ww.JOBS_DIR = jobs
            ww.LEDGER_FILE = tmp / "ledger.json"
            ww.LOG_FILE = tmp / "watcher.log"
            ww.NEEDS_HUMAN_FILE = tmp / "needs-human.log"
            ww.DO_NOT_WAKE_FILE = tmp / "do-not-wake.txt"  # doesn't exist = empty list
            ww.PROJECT_ROOT = None
            ww.SAFETY_REQUIRE_DEAD_PROCESS = False
            with mock.patch("wake_watcher.subprocess.Popen") as popen_mock:
                woke = ww.scan_once(dry_run=True, watermark=None)
            logtxt = ww.LOG_FILE.read_text(encoding="utf-8") if ww.LOG_FILE.exists() else ""
        finally:
            for attr, v in saved.items():
                setattr(ww, attr, v)
            os.environ.pop("WAKE_WATCHER_FAKE_VITALITY", None)
            _clear_fake_agents()

        fails += _check(woke == 0, f"vitality=done -> 0 wakes (got {woke})")
        fails += _check(f"SKIP session={sid}" in logtxt and "vitality verdict=done" in logtxt,
                        "audit trail: SKIP ... vitality verdict=done (completion-verdict double-check)")
        fails += _check(f"WAKE session={sid}" not in logtxt, "a WAKE log line must never appear")
        fails += _check(not popen_mock.called, "must never call Popen (DONE blocks it before deliver_wake)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


# -- root-cause regression pin (2026-07-08) --------------------------------------------
# Live-fire evidence: a freshly constructed live session reliably reproduces "the message
# lands in the input box but is never submitted, the child process won't even wake up to
# SIGKILL, the parent process hangs forever." Root cause = nobody was continuously draining
# the pty master fd -> the child fills the kernel buffer and blocks in write() with no way
# out (and on Darwin this block is immune to SIGKILL; the only action confirmed to release
# it is closing the master fd). The old _reap_pty_child fell back to an unboundedly
# blocking os.waitpid(pid, 0) after SIGKILL, so if the child wasn't immediately and truly
# reaped by that SIGKILL, this call would freeze wake-watcher's whole single-threaded scan
# loop indefinitely (ledger evidence: 0 RESCUED entries ever + one smoke-test process
# started 4 days earlier that had still not exited, hung).
# The two tests below pin the fix's two invariants: (1) the reaping phase is always
# bounded, even if the child is genuinely never reported as reaped by waitpid; (2) the
# drain helper actually reads the bytes sitting in the pty buffer. Neither depends on the
# real claude CLI -- both are built entirely from local os.fork()/os.openpty(), run fast,
# and reproduce reliably in an unattended CI environment.


def test_reap_pty_child_bounded_even_if_never_reaped() -> int:
    print("\n=== REGRESSION: the reaping phase must be bounded, even if the child is never judged reaped (the old implementation would block forever) ===")
    fails = 0
    saved_soft = ww.PTY_REAP_SOFT_WAIT_SEC
    saved_grace = ww.PTY_REAP_HARD_KILL_GRACE_SEC
    # Shorten both timeouts so the test runs in milliseconds instead of really waiting 5s+10s --
    # this only pins the structural invariant of "boundedness," not any specific duration.
    ww.PTY_REAP_SOFT_WAIT_SEC = 0.2
    ww.PTY_REAP_HARD_KILL_GRACE_SEC = 0.2

    read_fd, write_fd = os.pipe()  # just used as a select-able placeholder fd, no real pty I/O
    pid = os.fork()
    if pid == 0:
        # Child process: sleep essentially forever, waiting for the parent to really SIGKILL it
        # (_reap_pty_child really calls os.kill).
        os.close(read_fd)
        os.close(write_fd)
        try:
            time.sleep(30)
        finally:
            os._exit(0)

    try:
        # Simulate "this child process is never judged to have been reaped" (regardless of
        # whether it's really the Darwin pty write-block trap behind it -- the unit test only
        # cares that even if waitpid keeps saying 0, the reaping function must never wait
        # unboundedly).
        with mock.patch("wake_watcher.os.waitpid", return_value=(0, 0)):
            t0 = time.time()
            ww._reap_pty_child(pid, read_fd)
            elapsed = time.time() - t0
        fails += _check(
            elapsed < 3.0,
            f"_reap_pty_child returns within a bounded time (not stuck, measured {elapsed:.2f}s, "
            f"the old implementation would block forever in this scenario)",
        )
    finally:
        # Cleanup: read_fd was already closed by _reap_pty_child (it unconditionally closes it at
        # the end of reaping); the child was really SIGKILLed (the mock only intercepts waitpid's
        # return value, os.kill is a real call), so here we use a real waitpid to reap it and
        # avoid leaving a zombie process.
        with contextlib.suppress(OSError):
            os.close(write_fd)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)
        ww.PTY_REAP_SOFT_WAIT_SEC = saved_soft
        ww.PTY_REAP_HARD_KILL_GRACE_SEC = saved_grace
    return fails


def test_pty_drain_for_reads_available_bytes() -> int:
    print("\n=== REGRESSION: _pty_drain_for really reads the bytes already sitting in the pty buffer (fixing the write-block mechanism itself) ===")
    fails = 0
    master_fd, slave_fd = os.openpty()
    try:
        # No \r/\n -- the pty terminal layer has onlcr on by default and would translate \n to
        # \r\n; mixing that in would just make the assertion fragile and dependent on terminal
        # translation details, while what this test actually needs to pin is "drain really reads
        # the bytes the child wrote," not newline translation.
        payload = b"hello-from-simulated-child-render"
        os.write(slave_fd, payload)
        t0 = time.time()
        drained = ww._pty_drain_for(master_fd, 0.5)
        elapsed = time.time() - t0
        fails += _check(payload in drained, f"drain read the bytes that were written (got {drained!r})")
        fails += _check(elapsed < 1.5, f"drain roughly tracks the requested duration, not an unbounded wait (measured {elapsed:.2f}s)")
    finally:
        for fd in (master_fd, slave_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
    return fails


# -- PTY safety line: every clause of a multi-condition guard must be pinned INDIVIDUALLY
#    (mutation testing follow-up, 2026-08-18) -----------------------------------------
def test_interactive_with_id_never_injects() -> int:
    """Pins down the `kind == "background"` clause in isolation.

    Why this was needed (found by mutation testing, not a style preference): the real
    guard is a TWO-CONDITION check -- in agent_liveness_lookup(),
    `if a.get("kind") == "background" and a.get("id"):` only routes to PTY injection if
    both hold. But every other interactive fixture in this file has NO `id`, so if you
    delete the whole `kind` clause, the second clause `a.get("id")` alone still catches
    it and the entire suite stays green -- this safety line was never actually being
    covered by any test.

    Generalizable principle: every clause of a multi-condition guard must be pinned
    individually -- the fixture must deliberately satisfy every other condition and
    leave only the one under test unsatisfied. Otherwise the test only proves "at least
    one clause holds," and if any single clause breaks it won't be caught until another
    clause also breaks -- at which point it's an incident. (In reality the double
    safety net currently holds: real `claude agents --json` interactive entries indeed
    have no `id`. But the day upstream adds one, the guard is left with only `kind`.)

    So this fixture deliberately gives the interactive entry an `id`, splitting the two
    conditions apart; and the assertion checks that the DANGEROUS ACTION DID NOT HAPPEN
    (a spy records whether _pty_attach_inject was actually called), not what the
    decision function returned -- the latter could be gamed by a different
    implementation, the former is a fact about the real execution path.
    """
    print("\n=== SAFETY LINE: an interactive entry WITH an id must still never trigger PTY injection (testing the kind clause alone) ===")
    fails = 0
    called: list[tuple] = []
    orig = ww._pty_attach_inject
    ww._pty_attach_inject = lambda *a, **k: (called.append(a), True)[1]  # spy
    try:
        # (Control arm, guards against a vacuous pass) Same spy, same real path: background + id
        # must really reach injection. Without this arm, "the spy was never called" could just
        # mean deliver_wake returned earlier for an unrelated reason (e.g. claude-binary
        # resolution failing on this machine -> defer_only), which would make this case a silent
        # false pass.
        bg_sid = "sid-guard-control-background"
        os.environ["WAKE_WATCHER_FAKE_AGENTS"] = json.dumps([
            {"sessionId": bg_sid, "kind": "background", "id": "bgid-0001", "state": "blocked"},
        ])
        with mock.patch("wake_watcher.resolve_claude_binary", return_value=("/fake/claude", [])), \
                mock.patch("wake_watcher.subprocess.Popen") as popen_ctl:
            ww.deliver_wake(bg_sid, None, dry_run=False)
        fails += _check(len(called) == 1, "(control arm) background+id -> really reaches the injection path, proving the spy is wired up")
        fails += _check(not popen_ctl.called, "(control arm) must never --resume")

        called.clear()
        sid = "sid-interactive-with-id"
        os.environ["WAKE_WATCHER_FAKE_AGENTS"] = json.dumps([
            # v deliberately carries an id (real CLI interactive entries don't), splitting the two
            #   conditions apart: only `kind` is left guarding
            {"sessionId": sid, "kind": "interactive", "id": "bgid-0001",
             "pid": 4242, "status": "busy"},
        ])
        with mock.patch("wake_watcher.resolve_claude_binary", return_value=("/fake/claude", [])), \
                mock.patch("wake_watcher.subprocess.Popen") as popen_mock:
            ok, info = ww.deliver_wake(sid, None, dry_run=False)
        fails += _check(len(called) == 0,
                        "interactive (with id) must never trigger PTY injection -- the kind clause holds on its own")
        fails += _check(not popen_mock.called, "interactive (with id) likewise must never --resume (it's alive)")
        fails += _check(not ok and "DEFER" in info, f"-> DEFER, leave it alone (got: {info!r})")
    finally:
        ww._pty_attach_inject = orig
        _clear_fake_agents()
    return fails


def test_require_dead_blocks_misfire_on_interactive_branch() -> int:
    """REQUIRE_DEAD's "block the misfire" semantics still hold on the interactive branch
    (where the liveness criterion switches to pid).

    Background: interactive candidates have no state.json, so the old
    session_has_live_process() judged liveness by grepping `ps ax` output for the
    sessionId text -- but an interactive process's own command line contains the
    sessionId, so it always matches, making the check vacuously true by construction.
    So this branch instead uses the entry's own `pid` for liveness (background entries
    have `id`/`state`, interactive entries have `pid`/`status` -- the schema forks).
    Swapping the criterion must not accidentally swap the semantics -- that's exactly
    what this case pins.

    The SPECIFIC misfire being blocked: in the instant between candidate enumeration and
    delivery, if `claude agents --json` happens to omit this session, deliver_wake()'s
    liveness check concludes "dead," and it goes on to --resume a session that is
    actually still alive -- the red line (two instances driving the same session at
    once). Constructed by making _list_active_agents return the entry on the first call
    (candidate enumeration) and an empty list on the second (pre-delivery recheck).

    Two arms. The second arm is a counterproof against a vacuous pass: with REQUIRE_DEAD
    turned off, the exact same setup DOES --resume -- proving this setup can really
    trigger the misfire, so the first arm's "nothing happened" isn't because the path was
    never reached at all.
    """
    print("\n=== GUARD: REQUIRE_DEAD still blocks a misfire on the interactive branch (pid-based liveness) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-interactive-require-dead-"))
    injected: list[tuple] = []
    orig_inject = ww._pty_attach_inject
    saved = {
        attr: getattr(ww, attr)
        for attr in ("HOME", "JOBS_DIR", "LEDGER_FILE", "LOG_FILE", "NEEDS_HUMAN_FILE",
                     "DO_NOT_WAKE_FILE", "PROJECT_ROOT", "SAFETY_REQUIRE_DEAD_PROCESS")
    }
    os.environ["WAKE_WATCHER_FAKE_NET"] = "1"
    os.environ["WAKE_WATCHER_FAKE_VITALITY"] = "unused-sid=alive_working"  # doesn't match this sid -> None
    try:
        home = tmp / "claude-home"
        (home / "jobs").mkdir(parents=True)
        proj = home / "projects" / "-fake-proj"
        proj.mkdir(parents=True)
        work = tmp / "work"
        work.mkdir()
        sid = "sid-interactive-require-dead-001"
        (proj / f"{sid}.jsonl").write_text(
            json.dumps({"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
                        "message": {"content": [{"type": "text", "text": TRANSIENT}]}}) + "\n",
            encoding="utf-8")
        # pid is the test process's own -- guarantees "this interactive session's process is
        # genuinely still alive" is a fact, not an assumption.
        entry = {"sessionId": sid, "kind": "interactive", "pid": os.getpid(),
                 "cwd": str(work), "status": "busy"}

        ww.HOME = home
        ww.JOBS_DIR = home / "jobs"
        ww.LOG_FILE = tmp / "watcher.log"
        ww.NEEDS_HUMAN_FILE = tmp / "needs-human.log"
        ww.DO_NOT_WAKE_FILE = tmp / "do-not-wake.txt"  # doesn't exist = empty list
        ww.PROJECT_ROOT = None
        ww._pty_attach_inject = lambda *a, **k: (injected.append(a), True)[1]  # spy

        # -- Arm 1: REQUIRE_DEAD on (production default) + process genuinely alive -> not a
        #    single action may fire --
        ww.LEDGER_FILE = tmp / "ledger-a.json"
        ww.SAFETY_REQUIRE_DEAD_PROCESS = True
        with mock.patch("wake_watcher._list_active_agents",
                        side_effect=[[entry], [], [], []]), \
                mock.patch("wake_watcher.resolve_claude_binary",
                           return_value=("/fake/claude", [])), \
                mock.patch("wake_watcher.os.getloadavg", return_value=(0.0, 0.0, 0.0)), \
                mock.patch("wake_watcher.subprocess.Popen") as popen_a:
            woke_a = ww.scan_once(dry_run=False, watermark=None)
        log_a = ww.LOG_FILE.read_text(encoding="utf-8") if ww.LOG_FILE.exists() else ""
        nh_a = ww.NEEDS_HUMAN_FILE.read_text(encoding="utf-8") if ww.NEEDS_HUMAN_FILE.exists() else ""

        fails += _check(not popen_a.called,
                        "REQUIRE_DEAD on + pid still alive -> must never --resume (the misfire is blocked)")
        fails += _check(len(injected) == 0, "and must also never PTY-inject (interactive has no injection primitive)")
        fails += _check(woke_a == 0, f"0 wakes this round (got {woke_a})")
        fails += _check(f"DEFER session={sid} (interactive)" in log_a and "REQUIRE_DEAD" in log_a,
                        "audit trail: the reason it was blocked is written to the log (blocked doesn't mean silent)")
        fails += _check(f"session={sid} (interactive main session)" in nh_a,
                        "blocked != silent: a NEEDS-HUMAN still surfaces once (visibility is exactly this branch's value)")

        # -- Arm 2 (counterproof): REQUIRE_DEAD off -> the exact same setup really does misfire
        # --resume --
        # Use a fresh ledger: arm 1 already wrote a backoff timestamp, and reusing it would make
        # this arm get skipped by the backoff window (a false pass).
        ww.LEDGER_FILE = tmp / "ledger-b.json"
        ww.SAFETY_REQUIRE_DEAD_PROCESS = False
        with mock.patch("wake_watcher._list_active_agents",
                        side_effect=[[entry], [], [], []]), \
                mock.patch("wake_watcher.resolve_claude_binary",
                           return_value=("/fake/claude", [])), \
                mock.patch("wake_watcher.os.getloadavg", return_value=(0.0, 0.0, 0.0)), \
                mock.patch("wake_watcher.subprocess.Popen") as popen_b:
            ww.scan_once(dry_run=False, watermark=None)
        cmd_b = list(popen_b.call_args.args[0]) if popen_b.called else []
        fails += _check(popen_b.called and "--resume" in cmd_b and sid in cmd_b,
                        f"(counterproof arm) with REQUIRE_DEAD off, the exact same setup really does --resume a live session (cmd={cmd_b!r}) "
                        f"-- proving arm 1 was blocking a real danger, not spinning idle")
        fails += _check(len(injected) == 0, "(counterproof arm) even so, must never take the PTY injection path (the kind line holds independently)")
    finally:
        ww._pty_attach_inject = orig_inject
        for attr, v in saved.items():
            setattr(ww, attr, v)
        os.environ.pop("WAKE_WATCHER_FAKE_NET", None)
        os.environ.pop("WAKE_WATCHER_FAKE_VITALITY", None)
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def run() -> int:
    # Redirect LOG_FILE to a temp file for the whole run -- these tests call
    # ww.deliver_wake()/agent_liveness_lookup() directly in-process, which really triggers
    # log() writes to disk; test noise (WARN/DEFER/SKIP with fake session ids) must never
    # bleed into the real production .claude/wake-watcher/wake-watcher.log (that's for humans
    # debugging real incidents).
    tmp = Path(tempfile.mkdtemp(prefix="wake-routing-log-"))
    saved_log_file = ww.LOG_FILE
    ww.LOG_FILE = tmp / "test.log"
    try:
        fails = 0
        fails += test_live_bg_routes_to_pty_inject()
        fails += test_dead_orphan_routes_to_resume()
        fails += test_live_other_defers_no_resume_no_inject()
        fails += test_unknown_liveness_defers_never_falls_back_to_resume()
        fails += test_verify_transcript_user_turn_ignores_stale_match_before_baseline()
        fails += test_inject_failure_does_not_fallback_to_resume()
        fails += test_dry_run_reflects_all_four_routes()
        fails += test_done_verdict_skips_wake_even_if_transcript_says_send()
        fails += test_interactive_with_id_never_injects()
        fails += test_require_dead_blocks_misfire_on_interactive_branch()
        fails += test_reap_pty_child_bounded_even_if_never_reaped()
        fails += test_pty_drain_for_reads_available_bytes()
    finally:
        ww.LOG_FILE = saved_log_file
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)


# -- Real-machine smoke test (optional, needs a real claude CLI + really starting a bg
#    agent; run manually, not part of CI) -----------------------------------------------
#
# This file only tests routing/dispatch, not the real byte sequence of pty.fork()/
# `claude attach` (that depends on real terminal rendering, which mocks can't fake
# convincingly; the only way to verify it is to really start a bg agent and really type
# into its input box). Manual verification steps:
#
#   1. Start a dedicated, throwaway bg agent (never try this against a real session you're
#      actually using!):
#        claude --bg -p "Remember this codeword: PTY_SMOKE_TEST_XYZ. You'll be interrupted
#        after this; after the interruption just keep waiting for instructions."
#      Note its sessionId (found in `claude agents --json`, or from the command's output).
#
#   2. Confirm it shows kind=background with a short id in `claude agents --json`:
#        claude agents --json | python3 -m json.tool
#
#   3. Manually run one injection using the short id (from the .claude/wake-watcher/
#      directory):
#        python3 -c "
#        import wake_watcher as ww
#        ok = ww._pty_attach_inject('<short-id>', 'Continue, reply with the codeword to confirm receipt.',
#                                    session_id='<full-sessionId>',
#                                    transcript_path='<~/.claude/projects/.../<sessionId>.jsonl>')
#        print('inject+verify result:', ok)
#        "
#
#   4. Verify: check (a) the function returns True, (b) the session's transcript really has
#      a new user turn at the tail containing "Continue, reply with the codeword to confirm
#      receipt", (c) the bg agent really received it and responded afterward (you can watch
#      its state go from blocked/idle to working via `claude agents --json`, or just look
#      directly with `claude attach <short-id>`).
#
#   5. Clean up afterward: don't leave this throwaway bg agent tying up resources -- once
#      it has finished/responded, end it the normal way.
#
# This step depends on real claude CLI behavior (attach rendering / pty timing) and should
# be run manually once as evidence whenever the environment allows; it was NOT done for
# this delivery (it requires really starting a bg agent that burns real API quota, plus a
# real human watching the terminal to confirm rendering -- not suitable to run unattended
# in an automated environment) -- see the delivery notes for the honest assessment.
