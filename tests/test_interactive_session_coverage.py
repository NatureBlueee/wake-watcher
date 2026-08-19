#!/usr/bin/env python3
"""Interactive main-session coverage (owner, 2026-07-24, explicit scope-widening fix).

Root cause (see DIAGNOSIS-2026-07-24.md): scan_once() originally only enumerated JOBS_DIR
(~/.claude/jobs/<id>/state.json, the bg/daemon job lifecycle files). Interactive main
sessions (a `claude` run directly in a terminal by a human; kind=="interactive" in
`claude agents --json`) structurally never show up in JOBS_DIR — this is the direct
reason wake-watcher stayed completely silent while the main session hit its session
limit resets 9:50pm on 2026-07-23. It wasn't "DEFER judged wrong" — the candidate
discovery layer never saw this session in the first place.

This file pins down the behavior contract of this fix, five groups:
  ① _find_transcript_path: glob to the transcript by sessionId, without relying on
     reimplementing Claude Code's own cwd directory-encoding rules (fragile).
  ② _interactive_candidates: only picks kind=="interactive"; project-scope filtering
     (#5 only covers this project) still applies; kind=="background" entries never come
     out of this function (the two candidate sources are mutually exclusive, never
     double-processed).
  ③ End-to-end (real scan_once, not dry-run, WAKE_WATCHER_FAKE_AGENTS isolates the real
     CLI, never touches any real session):
     - Before the session-limit reset point → stand pat (the timer is reachable but
       hasn't fired yet; waking too early is more dangerous than waking too late).
     - After the reset point → the interactive branch is reachable, deliver_wake judges
       live_other and correctly DEFERs — but it must never be a "traceless" DEFER: it's
       visible in the log + NEEDS-HUMAN surfaces once (this is exactly the visibility
       this fix adds — "correct DEFER" is not the same as "silent DEFER").
     - Repeated scans within the 300s backoff → doesn't surface again (no log spam).
  ④ WAKE_WATCHER_COVER_INTERACTIVE=0 (escape-hatch switch) → falls back completely to the
     old behavior, interactive sessions become invisible again.
  ⑤ Safety line unchanged: never actually reaches any real keystroke injection
     (_pty_attach_inject only fires for kind=="background" — that contract itself isn't
     what this file tests, test_attach_inject_routing.py already pins it down — here we
     only confirm that interactive candidates flowing through deliver_wake do land in the
     live_other/DEFER branch, and never trigger an injection attempt).

Run: python3 test_interactive_session_coverage.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# OSS layout: production code lives in ../src/wake_watcher/, tests in ../tests/.
# WAKE_WATCHER_SCRIPT lets a packager point at a different install location.
_SRC = HERE.parent / "src" / "wake_watcher"
WW_SCRIPT = pathlib.Path(os.environ.get("WAKE_WATCHER_SCRIPT", str(_SRC / "wake_watcher.py")))

sys.path.insert(0, str(HERE.parent / "src" / "wake_watcher"))
import wake_watcher as ww  # noqa: E402

LIMIT = "You've hit your session limit · resets 3:20am (Australia/Melbourne)"


def _check(cond: bool, label: str) -> int:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return 0 if cond else 1


def _write_transcript(path: Path, detail_text: str, err_ts: float) -> None:
    """Write a minimal transcript: one user turn + one API-error assistant turn (session-limit copy)."""
    from datetime import datetime, timezone

    ts_iso = datetime.fromtimestamp(err_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recs = [
        {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
         "timestamp": ts_iso, "message": {"content": [{"type": "text", "text": detail_text}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def test_find_transcript_path() -> int:
    print("\n=== UNIT: _find_transcript_path (glob by sessionId, doesn't guess the cwd encoding rule) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-interactive-find-"))
    try:
        home = tmp / "claude-home"
        proj = home / "projects" / "-some-encoded-project-dir"
        proj.mkdir(parents=True)
        sid = "sid-fixture-0001"
        (proj / f"{sid}.jsonl").write_text("", encoding="utf-8")

        saved_home = ww.HOME
        try:
            ww.HOME = home
            got = ww._find_transcript_path(sid)
            fails += _check(got == str(proj / f"{sid}.jsonl"), f"found the correct path → {got}")
            fails += _check(ww._find_transcript_path("no-such-id") is None, "not found → None (fail-closed)")
        finally:
            ww.HOME = saved_home
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_interactive_candidates_filters_kind_and_project() -> int:
    print("\n=== UNIT: _interactive_candidates (only picks interactive + project-scope filtering) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-interactive-cand-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        sibling = (tmp / "MyProject-other").resolve()
        sibling.mkdir(parents=True)

        fake_agents = json.dumps([
            {"id": "bg1", "kind": "background", "sessionId": "sid-bg",
             "cwd": str(root / "work"), "state": "blocked"},
            {"pid": 111, "kind": "interactive", "sessionId": "sid-interactive-in-project",
             "cwd": str(root / "work"), "status": "idle"},
            {"pid": 222, "kind": "interactive", "sessionId": "sid-interactive-foreign",
             "cwd": str(sibling), "status": "busy"},
        ])
        saved_root = ww.PROJECT_ROOT
        saved_fake = os.environ.get("WAKE_WATCHER_FAKE_AGENTS")
        try:
            ww.PROJECT_ROOT = root
            os.environ["WAKE_WATCHER_FAKE_AGENTS"] = fake_agents
            cands = ww._interactive_candidates()
            ids = {c["sessionId"] for c in cands}
            fails += _check(ids == {"sid-interactive-in-project"},
                            f"kind==background excluded + sibling project excluded, only this project's interactive left → {ids}")
        finally:
            ww.PROJECT_ROOT = saved_root
            if saved_fake is None:
                os.environ.pop("WAKE_WATCHER_FAKE_AGENTS", None)
            else:
                os.environ["WAKE_WATCHER_FAKE_AGENTS"] = saved_fake
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def _base_env(tmp: Path, root: Path, home: Path, fake_agents: str) -> dict:
    env = dict(os.environ)
    env.update({
        "WAKE_WATCHER_CLAUDE_HOME": str(home),
        "WAKE_WATCHER_LOG": str(tmp / "watcher.log"),
        "WAKE_WATCHER_NEEDS_HUMAN": str(tmp / "needs-human.log"),
        "WAKE_WATCHER_LEDGER": str(tmp / "ledger.json"),
        "WAKE_WATCHER_PROJECT_ROOT": str(root),
        "WAKE_WATCHER_WATERMARK": "",
        "WAKE_WATCHER_FAKE_AGENTS": fake_agents,
        # vitality_verdict really runs `uv run ... <the external tool> vitality` — the test's
        # temp dir has no real install of that tool, so we explicitly give a non-matching
        # fake mapping, making it short-circuit straight to None instead of waiting on a
        # subprocess that's bound to fail for real.
        "WAKE_WATCHER_FAKE_VITALITY": "unused-sid=alive_working",
    })
    env.pop("WAKE_WATCHER_DO_NOT_WAKE_FILE", None)
    env.pop("WAKE_WATCHER_DO_NOT_WAKE", None)
    return env


def _run_once(env: dict) -> None:
    subprocess.run([sys.executable, str(WW_SCRIPT), "--once"],
                   env=env, capture_output=True, text=True, timeout=120)


def test_e2e_before_reset_does_not_defer_yet() -> int:
    """The reset point hasn't arrived yet → the timer correctly waits, no early DEFER/surface
    (waking too early is more dangerous than waking too late: jumping the gun before the
    session-limit reset just means running into the wall — you burn an API call and still
    hit the limit)."""
    print("\n=== E2E: interactive session, session-limit reset point not yet reached → stand pat ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-interactive-e2e-early-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        home = tmp / "claude-home"
        proj_dir = home / "projects" / "-fake-proj"
        proj_dir.mkdir(parents=True)
        (home / "jobs").mkdir(parents=True)  # scan_once requires JOBS_DIR to exist (a precondition of the bg branch)

        sid = "sid-fixture-0001"
        now = time.time()
        lt = time.localtime(now + 3 * 3600)  # doesn't reset for another 3 hours, clearly not due yet
        h12 = lt.tm_hour % 12 or 12
        ampm = "am" if lt.tm_hour < 12 else "pm"
        text = f"You've hit your session limit · resets {h12}:{lt.tm_min:02d}{ampm} (Australia/Melbourne)"
        _write_transcript(proj_dir / f"{sid}.jsonl", text, now)

        fake_agents = json.dumps([{"pid": 999, "kind": "interactive", "sessionId": sid,
                                    "cwd": str(root / "work"), "status": "idle"}])
        env = _base_env(tmp, root, home, fake_agents)
        _run_once(env)

        log_p, nh_p = Path(env["WAKE_WATCHER_LOG"]), Path(env["WAKE_WATCHER_NEEDS_HUMAN"])
        txt = log_p.read_text(encoding="utf-8") if log_p.exists() else ""
        nh = nh_p.read_text(encoding="utf-8") if nh_p.exists() else ""

        fails += _check(f"DEFER session={sid} (interactive)" not in txt,
                        "before the reset point → no DEFER (the timer is still waiting, doesn't wake by default)")
        fails += _check(sid not in nh, "before the reset point → NEEDS-HUMAN doesn't surface (not time to remind yet)")
        fails += _check(f"SKIP session={sid} (interactive" in txt and "waiting for reset before waking" in txt,
                        "but it must be visible: the log shows this session was evaluated, just not due yet (not traceless)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_e2e_interactive_session_limit_defer_and_surface() -> int:
    """Core regression anchor: an interactive main session hits session-limit, the reset point
    has already passed → must DEFER visibly in the log + NEEDS-HUMAN surfaces once (not
    completely traceless like on 2026-07-23); a second scan within the 300s backoff must not
    surface again. Entirely isolated via WAKE_WATCHER_FAKE_AGENTS — never touches the real
    claude CLI / a real session."""
    print("\n=== E2E: interactive session, session-limit reset point already passed → DEFER visible + NEEDS-HUMAN (no spam) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-interactive-e2e-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        home = tmp / "claude-home"
        proj_dir = home / "projects" / "-fake-proj"
        proj_dir.mkdir(parents=True)
        (home / "jobs").mkdir(parents=True)

        sid = "sid-fixture-0001"
        _write_transcript(proj_dir / f"{sid}.jsonl", LIMIT, time.time() - 26 * 3600)  # long past the reset point

        fake_agents = json.dumps([{"pid": 999, "kind": "interactive", "sessionId": sid,
                                    "cwd": str(root / "work"), "status": "idle"}])
        env = _base_env(tmp, root, home, fake_agents)
        log_p, nh_p = Path(env["WAKE_WATCHER_LOG"]), Path(env["WAKE_WATCHER_NEEDS_HUMAN"])

        _run_once(env)  # first run: should DEFER + surface
        txt1 = log_p.read_text(encoding="utf-8") if log_p.exists() else ""
        nh1 = nh_p.read_text(encoding="utf-8") if nh_p.exists() else ""

        fails += _check(f"DEFER session={sid} (interactive)" in txt1,
                        "reset point has passed → the interactive branch is reachable and DEFERs (not traceless)")
        fails += _check("no known safe wake path" in txt1 or "cannot" in txt1 or "attach" in txt1.lower(),
                        "DEFER reason text is readable (not an empty reason)")
        fails += _check(f"session={sid} (interactive main session)" in nh1,
                        "this session appears in NEEDS-HUMAN (the owner can see it without having to discover it themselves)")
        fails += _check(nh1.count(sid) == 1, "the first surface is exactly one entry (no duplicate)")
        fails += _check("_pty_attach_inject" not in txt1 and "pty-attach-inject delivered" not in txt1,
                        "never actually reaches real keystroke injection the whole time (interactive is always live_other, never live_bg)")

        _run_once(env)  # second run (milliseconds later, well within the 300s backoff window): should not surface again
        nh2 = nh_p.read_text(encoding="utf-8") if nh_p.exists() else ""
        fails += _check(nh2.count(sid) == 1, "a second scan within the 300s backoff → doesn't surface again (log doesn't spam)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_cover_interactive_toggle_off_restores_old_behavior() -> int:
    """WAKE_WATCHER_COVER_INTERACTIVE=0 → falls back completely to the old behavior (interactive
    sessions become invisible again). This is the "config/env-var override path" the task
    requires: if the new branch misbehaves, you can fall back immediately without rolling
    back the whole codebase."""
    print("\n=== E2E: COVER_INTERACTIVE_SESSIONS=0 escape-hatch switch takes effect ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-interactive-toggle-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        home = tmp / "claude-home"
        proj_dir = home / "projects" / "-fake-proj"
        proj_dir.mkdir(parents=True)
        (home / "jobs").mkdir(parents=True)

        sid = "sid-fixture-0001"
        _write_transcript(proj_dir / f"{sid}.jsonl", LIMIT, time.time() - 26 * 3600)

        fake_agents = json.dumps([{"pid": 999, "kind": "interactive", "sessionId": sid,
                                    "cwd": str(root / "work"), "status": "idle"}])
        env = _base_env(tmp, root, home, fake_agents)
        env["WAKE_WATCHER_COVER_INTERACTIVE"] = "0"
        _run_once(env)

        log_p, nh_p = Path(env["WAKE_WATCHER_LOG"]), Path(env["WAKE_WATCHER_NEEDS_HUMAN"])
        txt = log_p.read_text(encoding="utf-8") if log_p.exists() else ""
        nh = nh_p.read_text(encoding="utf-8") if nh_p.exists() else ""
        fails += _check(sid not in txt, "switch off → interactive sessions no longer appear in the log at all (back to old behavior)")
        fails += _check(sid not in nh, "switch off → doesn't surface")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def run() -> int:
    fails = 0
    fails += test_find_transcript_path()
    fails += test_interactive_candidates_filters_kind_and_project()
    fails += test_e2e_before_reset_does_not_defer_yet()
    fails += test_e2e_interactive_session_limit_defer_and_surface()
    fails += test_cover_interactive_toggle_off_restores_old_behavior()
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
