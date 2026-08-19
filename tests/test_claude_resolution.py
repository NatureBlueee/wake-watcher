#!/usr/bin/env python3
"""Regression tests for the three-layer claude-binary resolution protocol, plus
fail-loud behavior on unresolvable resolution.

pytest-assert style (gated by really re-running `python -m pytest <selector>`; only
counts as passing if rc==0):
  - Case 1: simulating a minimal launchd PATH (no ~/.local/bin) still makes the
    resolution function return an executable absolute path (this whole file green).
  - Case 2 (test_fail_loud_dedup_and_reset): all three layers unreachable -> a
    three-element alert written to needs-human.log; a second round in the same
    failure window writes nothing more; after recovery (resolution succeeds again)
    a fresh failure re-alerts.
  - Case 3 (test_unresolved_means_defer_only): a resolution-failure round takes zero
    rescue actions (defer_only, no resume/attach/respawn branch reachable).

Deterministic (doesn't depend on a real claude on the machine): Layer 2 is controlled
via monkeypatching PATH; Layer 3 via the test seam WAKE_WATCHER_CLAUDE_KNOWN_PATHS
pointed at a temp executable / a nonexistent path. The needs-human/log/state files are
all monkeypatched to tmp -- never touches real production files.
Run: python3 -m pytest test_claude_resolution.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src" / "wake_watcher"))
import wake_watcher as ww  # noqa: E402


def _make_exec(path: Path) -> Path:
    """Build an executable fake claude binary (stdlib only, no external dependency)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Isolate the three fail-loud files (needs-human / log / dedup state) to tmp; clear the
    claude env test seams."""
    monkeypatch.setattr(ww, "NEEDS_HUMAN_FILE", tmp_path / "needs-human.log")
    monkeypatch.setattr(ww, "LOG_FILE", tmp_path / "wake-watcher.log")
    monkeypatch.setattr(ww, "CLAUDE_RESOLUTION_STATE_FILE", tmp_path / "claude-resolution-state.json")
    # The in-process memory dedup flag is a module global and leaks across tests: force it
    # to zero at the start of every test; teardown restores it automatically.
    monkeypatch.setattr(ww, "_unresolvable_alerted_mem", False)
    monkeypatch.delenv("WAKE_WATCHER_CLAUDE_KNOWN_PATHS", raising=False)
    monkeypatch.delenv("WAKE_WATCHER_FAKE_AGENTS", raising=False)
    monkeypatch.delenv("WAKE_WATCHER_FAKE_DELIVER", raising=False)
    return tmp_path


def _make_unresolvable(monkeypatch, tmp_path):
    """Construct all three layers unreachable: Layer2's PATH has no claude + Layer3's known
    location points at a nonexistent path."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # no claude
    monkeypatch.setenv("WAKE_WATCHER_CLAUDE_KNOWN_PATHS", str(tmp_path / "nope" / "claude"))


# -- Case 1: three-layer resolution (including the launchd minimal-PATH fallback) -------
def test_layer2_which_hit_returns_absolute_path(isolated_state, monkeypatch):
    """Layer 2: claude is on PATH -> shutil.which hits, returns an executable absolute
    path."""
    binp = _make_exec(isolated_state / "bin" / "claude")
    monkeypatch.setenv("PATH", str(binp.parent))
    monkeypatch.delenv("WAKE_WATCHER_CLAUDE_KNOWN_PATHS", raising=False)
    resolved, reasons = ww.resolve_claude_binary()
    assert resolved is not None
    assert os.path.isabs(resolved)
    assert os.access(resolved, os.X_OK)
    assert Path(resolved).resolve() == binp.resolve()
    assert any(r["layer"] == "2-shutil-which" and "hit" in r["reason"] for r in reasons)


def test_minimal_launchd_path_falls_back_to_layer3(isolated_state, monkeypatch):
    """Case 1's core scenario: simulate a minimal launchd PATH (no ~/.local/bin) -> Layer2
    misses -> Layer3's known location hits, and the resolution function still returns an
    executable absolute path."""
    known = _make_exec(isolated_state / "local-bin" / "claude")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # a launchd-style minimal default PATH, no ~/.local/bin
    monkeypatch.setenv("WAKE_WATCHER_CLAUDE_KNOWN_PATHS", str(known))
    resolved, reasons = ww.resolve_claude_binary()
    assert resolved is not None
    assert os.path.isabs(resolved)
    assert os.access(resolved, os.X_OK)
    assert Path(resolved).resolve() == known.resolve()
    # Layer1 should record that PATH lacks ~/.local/bin (a minimal-launchd-PATH signature),
    # Layer3 hits.
    assert any(r["layer"] == "1-plist-PATH" and "does not contain" in r["reason"] for r in reasons)
    assert any(r["layer"] == "3-known-paths" and "hit" in r["reason"] for r in reasons)


def test_layer_ordering_short_circuit_prefers_which(isolated_state, monkeypatch):
    """Layer order 1->2->3 short-circuits: Layer2 hitting returns immediately, never falling
    through to Layer3 (even if Layer3 also has a candidate)."""
    which_bin = _make_exec(isolated_state / "path-bin" / "claude")
    known_bin = _make_exec(isolated_state / "known-bin" / "claude")
    monkeypatch.setenv("PATH", str(which_bin.parent))
    monkeypatch.setenv("WAKE_WATCHER_CLAUDE_KNOWN_PATHS", str(known_bin))
    resolved, reasons = ww.resolve_claude_binary()
    assert Path(resolved).resolve() == which_bin.resolve()  # uses Layer2, not Layer3
    # Short-circuit: Layer3 should never even be attempted (no 3-known-paths entry in reasons).
    assert not any(r["layer"] == "3-known-paths" for r in reasons)


def test_all_layers_fail_returns_none_with_three_layer_reasons(isolated_state, monkeypatch):
    """All three layers fail -> (None, reasons); reasons carries a failure reason per layer
    (all three), feeding the fail-loud three-element rendering."""
    _make_unresolvable(monkeypatch, isolated_state)
    resolved, reasons = ww.resolve_claude_binary()
    assert resolved is None
    layers = {r["layer"] for r in reasons}
    assert layers == {"1-plist-PATH", "2-shutil-which", "3-known-paths"}
    # Every layer has a non-empty reason (not just a single "not found" for all of them).
    assert all(r["reason"].strip() for r in reasons)


# -- Case 2: fail-loud alerting + dedup within a failure window + reset after recovery --
def test_fail_loud_dedup_and_reset(isolated_state, monkeypatch):
    nh = ww.NEEDS_HUMAN_FILE

    # Round 1: all three layers unreachable -> ensure returns None + one three-element alert
    # written to needs-human.log.
    _make_unresolvable(monkeypatch, isolated_state)
    assert ww.ensure_claude_or_fail_loud() is None
    assert nh.exists()
    body1 = nh.read_text(encoding="utf-8")
    lines1 = [ln for ln in body1.splitlines() if "claude-unresolvable" in ln]
    assert len(lines1) == 1
    # Three elements: (a) service name (b) each layer's own failure reason (c) fix hint.
    assert f"service={ww.SERVICE_NAME}" in body1
    assert "[1-plist-PATH]" in body1 and "[2-shutil-which]" in body1 and "[3-known-paths]" in body1
    assert ww.CLAUDE_FIX_HINT in body1

    # Round 2: fails again within the same failure window -> deduped, needs-human.log gets no
    # new line.
    assert ww.ensure_claude_or_fail_loud() is None
    lines2 = [ln for ln in nh.read_text(encoding="utf-8").splitlines() if "claude-unresolvable" in ln]
    assert len(lines2) == 1  # still 1 line (dedup)

    # Round 3: failure clears (resolution succeeds again) -> dedup flag resets (no new alert
    # line).
    known = _make_exec(isolated_state / "recover-bin" / "claude")
    monkeypatch.setenv("WAKE_WATCHER_CLAUDE_KNOWN_PATHS", str(known))
    assert ww.ensure_claude_or_fail_loud() == str(known)
    lines3 = [ln for ln in nh.read_text(encoding="utf-8").splitlines() if "claude-unresolvable" in ln]
    assert len(lines3) == 1  # clearing itself writes no alert

    # Round 4: fails again after clearing -> re-alerts once (line 2).
    _make_unresolvable(monkeypatch, isolated_state)
    assert ww.ensure_claude_or_fail_loud() is None
    lines4 = [ln for ln in nh.read_text(encoding="utf-8").splitlines() if "claude-unresolvable" in ln]
    assert len(lines4) == 2  # failure recurs, re-alerts


_REASONS = [
    {"layer": "1-plist-PATH", "reason": "no claude on PATH"},
    {"layer": "2-shutil-which", "reason": "which miss"},
    {"layer": "3-known-paths", "reason": "known path absent"},
]


def _nh_lines(nh: Path) -> list[str]:
    if not nh.exists() or nh.is_dir():
        return []
    return [ln for ln in nh.read_text(encoding="utf-8").splitlines() if "claude-unresolvable" in ln]


# -- Case 2b: needs-human.log write fails -> dedup must not be set, retried next round
#    (scenario A, never permanently silent) -----------------------------------------
def test_needs_human_write_failure_does_not_dedup_and_retries(isolated_state, monkeypatch):
    """needs-human.log unwritable while the state file is writable: the dedup slot must not
    be set, retried next round; once writable again, exactly 1 line (not 0).

    Structurally construct an OSError (not via chmod -- as root, chmod gets bypassed and
    gives a false pass): point NEEDS_HUMAN_FILE at a directory, so open(dir, 'a') raises
    IsADirectoryError (an OSError subclass).
    """
    sf = ww.CLAUDE_RESOLUTION_STATE_FILE
    nh_dir = isolated_state / "nh_is_a_dir"
    nh_dir.mkdir()
    monkeypatch.setattr(ww, "NEEDS_HUMAN_FILE", nh_dir)  # unwritable (it's a directory)

    # Rounds 1&2: the human-facing write fails -> dedup slot stays unset (state has no
    # unresolvable_alerted), retries every round instead of going permanently silent.
    ww.note_claude_unresolvable(_REASONS)
    assert ww._load_resolution_state().get("unresolvable_alerted") is not True
    assert ww._unresolvable_alerted_mem is False
    ww.note_claude_unresolvable(_REASONS)
    assert ww._load_resolution_state().get("unresolvable_alerted") is not True

    # Round 3: the human-facing file becomes writable again -> retry writes it, exactly 1 line
    # (proving it's not permanently silent), and only now does the dedup slot get set.
    nh_file = isolated_state / "needs-human-recovered.log"
    monkeypatch.setattr(ww, "NEEDS_HUMAN_FILE", nh_file)
    ww.note_claude_unresolvable(_REASONS)
    assert len(_nh_lines(nh_file)) == 1
    assert ww._load_resolution_state().get("unresolvable_alerted") is True

    # Round 4: called again within the same failure window -> dedup hits, no flooding
    # (still 1 line).
    ww.note_claude_unresolvable(_REASONS)
    assert len(_nh_lines(nh_file)) == 1
    assert sf.exists()  # the state file's writable path, already persisted


# -- Case 2c: state file can't persist -> falls back to in-memory dedup, no rewrite per
#    round (scenario B, no flooding); and doesn't go silent after recovery -------------
def test_state_unpersistable_uses_memory_dedup_and_reset_relalerts(isolated_state, monkeypatch):
    """state file unpersistable while needs-human.log is writable: memory fallback prevents
    rewriting every round (exactly 1 line); after clearing, a fresh failure must re-alert.

    Structurally construct a state-write failure: point CLAUDE_RESOLUTION_STATE_FILE at a
    path under a directory that doesn't exist, so _save's tmp.write_text raises OSError,
    which gets swallowed and never persists; _load always returns {} -> dedup can only
    fall back to memory. This case must walk the full failure cycle: alert -> dedup ->
    clear -> fail again -> must re-alert (proving the in-memory flag really gets reset on
    clear, instead of silently swallowing the next real failure).
    """
    nh = ww.NEEDS_HUMAN_FILE  # a writable file path in the fixture
    bad_state = isolated_state / "no_such_dir" / "state.json"  # parent dir doesn't exist -> write must raise OSError
    monkeypatch.setattr(ww, "CLAUDE_RESOLUTION_STATE_FILE", bad_state)

    # Round 1: the human-facing write succeeds, state persistence fails -> the in-memory flag
    # gets set as a fallback.
    ww.note_claude_unresolvable(_REASONS)
    assert len(_nh_lines(nh)) == 1
    assert bad_state.exists() is False  # confirm state was never persisted
    assert ww._unresolvable_alerted_mem is True

    # Rounds 2&3: state always reads back as {} (never persisted), dedup relies on the memory
    # fallback -> no flooding (still 1 line).
    ww.note_claude_unresolvable(_REASONS)
    ww.note_claude_unresolvable(_REASONS)
    assert len(_nh_lines(nh)) == 1

    # Clear (resolution succeeds again): the in-memory flag must also reset (otherwise the
    # next real failure gets silently swallowed -- reproducing the silent-regression bug).
    ww.clear_claude_unresolvable_alert()
    assert ww._unresolvable_alerted_mem is False

    # Fails again: must re-alert once (line 2) -- proving clearing genuinely unlocks
    # non-silence for the next failure.
    ww.note_claude_unresolvable(_REASONS)
    assert len(_nh_lines(nh)) == 2


# -- Case 3: a resolution-failure round takes zero rescue actions (defer_only) ----------
def test_unresolved_means_defer_only(isolated_state, monkeypatch):
    """When all three resolution layers fail, deliver_wake always defers: no
    resume/attach/respawn branch is reachable.

    Even with FAKE_AGENTS='[]' (which would normally be judged dead -> the resume path),
    a resolution failure must defer before the liveness check / rescue action even runs
    (a load-bearing safety invariant: with no executable claude, any "action" is blind).
    """
    _make_unresolvable(monkeypatch, isolated_state)
    monkeypatch.setenv("WAKE_WATCHER_FAKE_AGENTS", "[]")  # would normally be judged dead -> resume path

    liveness_called = {"n": 0}
    inject_called = {"n": 0}

    def _spy_liveness(*a, **k):
        liveness_called["n"] += 1
        return {"status": "dead", "short_id": None, "entry": None}

    def _spy_inject(*a, **k):
        inject_called["n"] += 1
        return True

    monkeypatch.setattr(ww, "agent_liveness_lookup", _spy_liveness)
    monkeypatch.setattr(ww, "_pty_attach_inject", _spy_inject)
    with mock.patch("wake_watcher.subprocess.Popen") as popen_mock:
        ok, info = ww.deliver_wake("sid-unresolvable-001", "/tmp/x", dry_run=False,
                                   respawn_flags=["--model", "opus"], transcript_path=None)

    assert ok is False
    assert "defer_only" in info
    # A resolution failure blocks in front of every rescue action: liveness check / PTY
    # injection / resume Popen are all called zero times.
    assert liveness_called["n"] == 0
    assert inject_called["n"] == 0
    assert not popen_mock.called
