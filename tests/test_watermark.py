#!/usr/bin/env python3
"""Unit tests for the now-forward watermark (owner 2026-06-22: stop retroactively waking dead old sessions).

Covers:
  - _parse_iso_utc accepts the Z suffix (Python 3.9's bare fromisoformat doesn't, must .replace)
  - Old session (updatedAt <= watermark) -> SKIP pre_watermark, never WAKE
  - New session (updatedAt > watermark) -> WAKE
  - Missing updatedAt -> fail-closed SKIP
  - Watermark persistence reuse (restart doesn't re-stamp now) + reset (delete file, re-stamp)

Uses real assert (both pytest and the script entrypoint really check, unlike test_scope.py's return-int fake-green).
Does not depend on the real claude CLI (dry-run verifies the SKIP/WAKE decision).
Run: python3 test_watermark.py   or   pytest test_watermark.py
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


def test_parse_iso_utc_handles_z_suffix() -> None:
    """Python 3.9's bare fromisoformat doesn't accept Z — the helper must .replace before parsing."""
    ep = ww._parse_iso_utc("2026-06-22T03:34:07.315Z")
    assert ep is not None and abs(ep - 1782099247.315) < 0.001
    assert ww._parse_iso_utc("2026-06-22T03:34:07Z") is not None  # no fraction also fine
    assert ww._parse_iso_utc(None) is None        # fail-closed
    assert ww._parse_iso_utc("") is None
    assert ww._parse_iso_utc("not-a-date") is None
    assert ww._parse_iso_utc(12345) is None        # non-str


def test_load_or_init_watermark_persist_and_reset() -> None:
    """Persistence reuse (restart doesn't re-stamp now) + explicit env + disabled + reset."""
    tmp = Path(tempfile.mkdtemp(prefix="wake-wm-"))
    saved_file = ww.WATERMARK_FILE
    saved_env = ww._WATERMARK_ENV_RAW
    try:
        wm_file = tmp / "watermark.json"
        ww.WATERMARK_FILE = wm_file
        ww._WATERMARK_ENV_RAW = None  # go through the file-based path

        # 1) first call: file doesn't exist -> init to now + write to disk
        wm1 = ww.load_or_init_watermark()
        assert wm1 is not None and wm_file.exists()
        # 2) call again (simulate restart): reuse the same value, don't re-stamp now
        wm2 = ww.load_or_init_watermark()
        assert wm2 == wm1, "restart must reuse the old watermark, must not re-stamp now"
        # 3) reset -> file deleted
        ww.reset_watermark()
        assert not wm_file.exists()
        # 4) start again after reset -> re-stamp (new value >= old value)
        wm3 = ww.load_or_init_watermark()
        assert wm3 is not None and wm3 >= wm1

        # 5) env explicitly set empty -> disabled (None)
        ww._WATERMARK_ENV_RAW = ""
        assert ww.load_or_init_watermark() is None
        # 6) env given a fixed ISO -> use it (don't touch the file)
        ww._WATERMARK_ENV_RAW = "2026-01-01T00:00:00Z"
        fixed = ww.load_or_init_watermark()
        assert fixed is not None and abs(fixed - ww._parse_iso_utc("2026-01-01T00:00:00Z")) < 0.001
    finally:
        ww.WATERMARK_FILE = saved_file
        ww._WATERMARK_ENV_RAW = saved_env
        shutil.rmtree(tmp, ignore_errors=True)


def _write_job(jobs: Path, jid: str, sid: str, cwd: str,
               updated_at: str | None, state: str = "blocked") -> None:
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
    payload = {
        "state": state, "detail": TRANSIENT, "sessionId": sid,
        "resumeSessionId": sid, "cwd": cwd, "backend": "daemon",
        "linkScanPath": str(transcript),
    }
    if updated_at is not None:
        payload["updatedAt"] = updated_at
    (jd / "state.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run_scan(home: Path, root: Path, log: Path, ledger: Path,
              watermark_iso: str) -> str:
    """Run one real scan_once (dry-run, subprocess) with the watermark pinned to watermark_iso. Returns the log text."""
    env = dict(os.environ)
    env["WAKE_WATCHER_CLAUDE_HOME"] = str(home)
    env["WAKE_WATCHER_LEDGER"] = str(ledger)
    env["WAKE_WATCHER_LOG"] = str(log)
    env["WAKE_WATCHER_REQUIRE_DEAD"] = "0"
    env["WAKE_WATCHER_PROJECT_ROOT"] = str(root)
    env["WAKE_WATCHER_WATERMARK"] = watermark_iso  # watermark pinned (no file read/write)
    env.pop("WAKE_WATCHER_DO_NOT_WAKE", None)
    env.pop("WAKE_WATCHER_DO_NOT_WAKE_FILE", None)
    subprocess.run([sys.executable, str(WW_SCRIPT), "--once", "--dry-run"],
                   env=env, capture_output=True, text=True, timeout=120)
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_old_session_skipped_new_session_woken() -> None:
    """Old session (updatedAt <= watermark) SKIP; new session (updatedAt > watermark) WAKE; missing updatedAt SKIP."""
    tmp = Path(tempfile.mkdtemp(prefix="wake-wm-scan-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        home = tmp / "claude-home"
        jobs = home / "jobs"
        jobs.mkdir(parents=True)
        log = tmp / "watcher.log"
        ledger = tmp / "ledger.json"

        WM = "2026-06-22T00:00:00Z"
        cwd = str(root / "work")
        # old: updatedAt earlier than the watermark -> retroactive, SKIP
        _write_job(jobs, "OLD", "sid-old", cwd, "2026-06-20T10:00:00Z")
        # boundary: exactly equal to the watermark -> counts as "before watermark", SKIP
        _write_job(jobs, "EQ", "sid-eq", cwd, "2026-06-22T00:00:00Z")
        # new: updatedAt later than the watermark -> WAKE
        _write_job(jobs, "NEW", "sid-new", cwd, "2026-06-22T10:00:00Z")
        # missing updatedAt -> fail-closed SKIP
        _write_job(jobs, "NOUPD", "sid-noupd", cwd, None)

        txt = _run_scan(home, root, log, ledger, WM)

        assert "SKIP session=sid-old" in txt and "WAKE session=sid-old" not in txt, \
            "old session (before watermark) must SKIP, never WAKE"
        assert "SKIP session=sid-eq" in txt and "WAKE session=sid-eq" not in txt, \
            "exactly equal to the watermark counts as before-watermark, SKIP"
        assert "WAKE session=sid-new" in txt, "new session (after watermark) must WAKE"
        assert "SKIP session=sid-noupd" in txt and "WAKE session=sid-noupd" not in txt, \
            "missing updatedAt, fail-closed SKIP"
        assert "pre_watermark" in txt or "watermark" in txt, "pre_watermark skip leaves a visible trace"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_watermark_disabled_wakes_old_session() -> None:
    """Filter disabled (env set empty) -> old sessions get woken too (backward compat/sandbox)."""
    tmp = Path(tempfile.mkdtemp(prefix="wake-wm-off-"))
    try:
        root = (tmp / "MyProject").resolve()
        (root / "work").mkdir(parents=True)
        home = tmp / "claude-home"
        jobs = home / "jobs"
        jobs.mkdir(parents=True)
        log = tmp / "watcher.log"
        ledger = tmp / "ledger.json"
        _write_job(jobs, "OLD", "sid-old", str(root / "work"), "2020-01-01T00:00:00Z")
        txt = _run_scan(home, root, log, ledger, "")  # env="" -> disabled
        assert "WAKE session=sid-old" in txt, "watermark disabled, old session also woken (filter off)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> int:
    fns = [
        test_parse_iso_utc_handles_z_suffix,
        test_load_or_init_watermark_persist_and_reset,
        test_old_session_skipped_new_session_woken,
        test_watermark_disabled_wakes_old_session,
    ]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  [FAIL] {fn.__name__}: {e}")
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
