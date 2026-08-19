#!/usr/bin/env python3
"""Unit + sandbox test for the wake-watcher scope narrowing (owner 2026-06-22).

Two filters:
  #5 only this project's own sessions (cwd under this project's root) — excludes sessions from other projects (another-project etc.).
  #6 do-not-wake list — a deliberately stopped session in this project is never woken (the cwd filter alone can't exclude it).

Does not depend on the real claude CLI (dry-run verifies the SKIP/WAKE decision, not a real resume).
Run: python3 test_scope.py
"""
from __future__ import annotations

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

sys.path.insert(0, str(HERE.parent / "src" / "wake_watcher"))
import wake_watcher as ww  # noqa: E402

TRANSIENT = "API Error: Connection closed mid-response. The response above may be incomplete."


def _check(cond: bool, label: str) -> int:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return 0 if cond else 1


def test_cwd_in_project() -> int:
    """Pure function: is_relative_to real path boundary (sibling prefix directory doesn't false-hit)."""
    print("\n=== UNIT: cwd_in_project (path boundary) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-scope-cwd-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        (root / ".claude" / "worktrees" / "wt1").mkdir(parents=True)
        sibling = (tmp / "MyProject-sibling").resolve()  # sibling project with a colliding name prefix
        (sibling / "work").mkdir(parents=True)

        saved = ww.PROJECT_ROOT
        try:
            ww.PROJECT_ROOT = root
            fails += _check(ww.cwd_in_project(str(root)), "project root itself -> True")
            fails += _check(ww.cwd_in_project(str(root / "work")), "project root subdirectory -> True")
            fails += _check(
                ww.cwd_in_project(str(root / ".claude" / "worktrees" / "wt1")),
                "worktree subdirectory -> True",
            )
            fails += _check(
                not ww.cwd_in_project(str(sibling / "work")),
                "sibling prefix directory MyProject-v3-migration -> False (not a startswith false-hit)",
            )
            fails += _check(not ww.cwd_in_project("/tmp/some-other-project"), "a different project -> False")
            fails += _check(not ww.cwd_in_project(None), "no cwd -> False (conservative, don't wake)")
            fails += _check(not ww.cwd_in_project(""), "empty cwd -> False")
            ww.PROJECT_ROOT = None
            fails += _check(ww.cwd_in_project("/anywhere"), "PROJECT_ROOT=None -> True (filter disabled)")
        finally:
            ww.PROJECT_ROOT = saved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_load_do_not_wake() -> int:
    """Pure function: env union file, comment/delimiter parsing."""
    print("\n=== UNIT: load_do_not_wake (env union file) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-scope-dnw-"))
    try:
        dnw_file = tmp / "do-not-wake.txt"
        dnw_file.write_text(
            "# deliberately stopped session\nfile-id-1\nfile-id-2   # inline comment\n\n",
            encoding="utf-8",
        )
        saved_file = ww.DO_NOT_WAKE_FILE
        saved_env = os.environ.get("WAKE_WATCHER_DO_NOT_WAKE")
        try:
            ww.DO_NOT_WAKE_FILE = dnw_file
            os.environ["WAKE_WATCHER_DO_NOT_WAKE"] = "env-id-1, env-id-2"
            got = ww.load_do_not_wake()
            fails += _check(got == {"file-id-1", "file-id-2", "env-id-1", "env-id-2"},
                            f"env union file (comment stripped) -> {sorted(got)}")
            os.environ.pop("WAKE_WATCHER_DO_NOT_WAKE", None)
            ww.DO_NOT_WAKE_FILE = tmp / "missing.txt"
            fails += _check(ww.load_do_not_wake() == set(), "no env no file -> empty set (doesn't crash)")
        finally:
            ww.DO_NOT_WAKE_FILE = saved_file
            if saved_env is None:
                os.environ.pop("WAKE_WATCHER_DO_NOT_WAKE", None)
            else:
                os.environ["WAKE_WATCHER_DO_NOT_WAKE"] = saved_env
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def _write_job(jobs: Path, jid: str, sid: str, cwd: str) -> None:
    """Write one job: state.json + transcript (tail is a transient error not yet followed by a
    retry; the detection criterion now reads the transcript tail instead of state.json.detail, see wake_watcher.transcript_wake_decision)."""
    jd = jobs / jid
    jd.mkdir(parents=True, exist_ok=True)
    transcript = jd / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "isSidechain": False,
                    "message": {"content": [{"type": "text", "text": "干活"}]}}) + "\n" +
        json.dumps({"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
                    "message": {"content": [{"type": "text", "text": TRANSIENT}]}}) + "\n",
        encoding="utf-8")
    (jd / "state.json").write_text(json.dumps({
        "state": "blocked", "detail": TRANSIENT, "sessionId": sid,
        "resumeSessionId": sid, "cwd": cwd, "backend": "daemon",
        "linkScanPath": str(transcript),
    }, ensure_ascii=False), encoding="utf-8")


def test_scan_skips_in_sandbox() -> int:
    """Integration: scan_once (dry-run) WAKE/SKIP decisions across 4 job categories. No real claude needed."""
    print("\n=== SANDBOX: scan_once dry-run (this project wakes / other project + deliberately stopped SKIP) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-scope-scan-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        sibling = (tmp / "MyProject-sibling").resolve()
        (sibling / "work").mkdir(parents=True)
        home = tmp / "claude-home"
        jobs = home / "jobs"
        jobs.mkdir(parents=True)
        log = tmp / "watcher.log"
        ledger = tmp / "ledger.json"

        _write_job(jobs, "A_local", "sid-A-local", str(root / "work"))          # → WAKE
        _write_job(jobs, "B_sibling", "sid-B-sibling", str(sibling / "work"))   # → SKIP foreign
        _write_job(jobs, "C_other", "sid-C-other", "/tmp/other-proj")           # → SKIP foreign
        _write_job(jobs, "D_stopped", "sid-D-stopped", str(root / "work"))      # → SKIP do_not_wake

        env = dict(os.environ)
        env["WAKE_WATCHER_CLAUDE_HOME"] = str(home)
        env["WAKE_WATCHER_LEDGER"] = str(ledger)
        env["WAKE_WATCHER_LOG"] = str(log)
        env["WAKE_WATCHER_REQUIRE_DEAD"] = "0"
        env["WAKE_WATCHER_PROJECT_ROOT"] = str(root)
        env["WAKE_WATCHER_DO_NOT_WAKE"] = "sid-D-stopped"
        env["WAKE_WATCHER_WATERMARK"] = ""  # disable watermark filtering (this test only verifies cwd/do-not-wake; watermark coverage belongs to test_watermark.py)
        env.pop("WAKE_WATCHER_DO_NOT_WAKE_FILE", None)
        subprocess.run([sys.executable, str(WW_SCRIPT), "--once", "--dry-run"],
                       env=env, capture_output=True, text=True, timeout=120)
        txt = log.read_text(encoding="utf-8") if log.exists() else ""

        fails += _check("WAKE session=sid-A-local" in txt, "this project's transient session -> WAKE")
        fails += _check("SKIP session=sid-B-sibling" in txt and "WAKE session=sid-B-sibling" not in txt,
                        "sibling-prefix project session -> SKIP, never WAKE")
        fails += _check("SKIP session=sid-C-other" in txt and "WAKE session=sid-C-other" not in txt,
                        "a different project's session -> SKIP, never WAKE")
        fails += _check("SKIP session=sid-D-stopped" in txt and "WAKE session=sid-D-stopped" not in txt,
                        "do-not-wake session (cwd inside this project) -> SKIP, never WAKE")
        fails += _check("do-not-wake" in txt, "do-not-wake skip leaves a visible trace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_do_not_wake_liveness_aware() -> int:
    """do-not-wake liveness-aware (vitality-first, same root as the vitality-first placement finding):
    a session marked do-not-wake but vitality=alive_working -> WARN surfaces the contradiction, does not silently veto it permanently,
    and is never WAKE-d (it's alive). A session that's do-not-wake and not alive -> silently SKIP as usual."""
    print("\n=== SANDBOX: do-not-wake liveness-aware (alive->WARN, not alive->SKIP) ===")
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="wake-scope-dnw-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        home = tmp / "claude-home"
        jobs = home / "jobs"
        jobs.mkdir(parents=True)
        log = tmp / "watcher.log"
        ledger = tmp / "ledger.json"

        _write_job(jobs, "E_alive", "sid-E-alive", str(root / "work"))    # do-not-wake but alive
        _write_job(jobs, "D_stopped", "sid-D-stopped", str(root / "work"))  # do-not-wake and not alive

        env = dict(os.environ)
        env["WAKE_WATCHER_CLAUDE_HOME"] = str(home)
        env["WAKE_WATCHER_LEDGER"] = str(ledger)
        env["WAKE_WATCHER_LOG"] = str(log)
        env["WAKE_WATCHER_REQUIRE_DEAD"] = "0"
        env["WAKE_WATCHER_PROJECT_ROOT"] = str(root)
        env["WAKE_WATCHER_DO_NOT_WAKE"] = "sid-E-alive sid-D-stopped"
        env["WAKE_WATCHER_WATERMARK"] = ""
        # test seam: E is alive, D gets no verdict (-> None -> conservative honor SKIP)
        env["WAKE_WATCHER_FAKE_VITALITY"] = "sid-E-alive=alive_working"
        env.pop("WAKE_WATCHER_DO_NOT_WAKE_FILE", None)
        subprocess.run([sys.executable, str(WW_SCRIPT), "--once", "--dry-run"],
                       env=env, capture_output=True, text=True, timeout=120)
        txt = log.read_text(encoding="utf-8") if log.exists() else ""

        fails += _check("WARN session=sid-E-alive" in txt and "WAKE session=sid-E-alive" not in txt,
                        "do-not-wake but alive_working -> WARN surfaces the contradiction, never WAKE")
        fails += _check("SKIP session=sid-D-stopped" in txt and "WAKE session=sid-D-stopped" not in txt,
                        "do-not-wake and not alive (verdict None) -> silently SKIP as usual")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def run() -> int:
    fails = 0
    fails += test_cwd_in_project()
    fails += test_load_do_not_wake()
    fails += test_scan_skips_in_sandbox()
    fails += test_do_not_wake_liveness_aware()
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
