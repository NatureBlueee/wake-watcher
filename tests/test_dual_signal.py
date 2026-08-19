#!/usr/bin/env python3
"""Mechanical-verdict regression test for the rescue-acceptance dual-signal criterion.

Criterion: after a rescue action, "rescued" is only accepted when both signals hold;
signal A alone (delivery) never counts as rescued.
  Signal A = a main-conversation user turn after the baseline (the wake message has been
  delivered).
  Signal B = a main-conversation assistant turn after signal A, not an API-error, landing
  within [user_ts, user_ts+window].
  Three states: pending (no A) → delivered (A holds / B still pending within the window or
  the window has elapsed) → rescued (A ∧ B within the window).

pytest-assert style (the gate re-runs `python -m pytest <selector>`). Fabricated transcript
sequences + an explicit `now`, deterministically verifying the delivered/rescued/over-window
three states + an API-error assistant not counting as signal B + tool-use-only counting +
the baseline cutoff.
Run: python3 -m pytest test_dual_signal.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src" / "wake_watcher"))
import wake_watcher as ww  # noqa: E402

WINDOW = 600.0
T0_ISO = "2026-07-06T10:00:00Z"
T0 = ww._parse_iso_utc(T0_ISO)
assert T0 is not None


def _iso_at(offset_sec: float) -> str:
    return ww._now_iso_from_epoch(T0 + offset_sec)


def _user(offset: float, text: str = "刚才网络波动了请重试继续", sidechain: bool = False) -> dict:
    return {"type": "user", "isSidechain": sidechain, "timestamp": _iso_at(offset),
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _asst(offset: float, text: str = "继续干活中", sidechain: bool = False) -> dict:
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": _iso_at(offset),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _asst_apierror(offset: float) -> dict:
    return {"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
            "timestamp": _iso_at(offset),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "API Error: Connection closed"}]}}


def _asst_tooluse(offset: float) -> dict:
    """A main-conversation assistant turn with no text, only tool_use (the session is active and continuing, so it should count as signal B)."""
    return {"type": "assistant", "isSidechain": False, "timestamp": _iso_at(offset),
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}


def _write(tmp_path: Path, records: list[dict]) -> str:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                 encoding="utf-8")
    return str(p)


# ── pending: no signal A ────────────────────────────────────────────────────────────
def test_pending_when_no_new_user_turn(tmp_path):
    """A rescue action has been initiated but there's no new main-conversation user turn after the baseline → pending (delivery unconfirmed)."""
    path = _write(tmp_path, [_asst_apierror(-10)])
    v = ww.evaluate_dual_signal(path, baseline_len=1, window_sec=WINDOW, now=T0 + 30)
    assert v["state"] == "pending"
    assert v["signal_a"] is False and v["signal_b"] is False


# ── delivered: signal A holds, signal B does not ─────────────────────────────────────
def test_delivered_in_window_waiting_for_signal_b(tmp_path):
    """Signal A delivered, no signal B within the window yet, now hasn't exceeded the window → delivered (still pending within the window; not judged rescued unless the window is exceeded)."""
    path = _write(tmp_path, [_user(0)])
    v = ww.evaluate_dual_signal(path, baseline_len=0, window_sec=WINDOW, now=T0 + 100)
    assert v["state"] == "delivered"
    assert v["signal_a"] is True and v["signal_b"] is False
    assert v["over_window"] is False


def test_over_window_no_continuation_is_delivered_not_rescued(tmp_path):
    """Signal A delivered but the window has elapsed (now-user_ts>window) and there's still no signal B → delivered + over_window, never escalates to rescued."""
    path = _write(tmp_path, [_user(0)])
    v = ww.evaluate_dual_signal(path, baseline_len=0, window_sec=WINDOW, now=T0 + 700)
    assert v["state"] == "delivered"
    assert v["over_window"] is True
    assert v["signal_b"] is False


# ── rescued: signal A ∧ signal B within the window ───────────────────────────────────
def test_rescued_when_assistant_continues_in_window(tmp_path):
    """A non-API-error main-conversation assistant turn continues within the window after signal A → rescued (a genuine rescue)."""
    path = _write(tmp_path, [_user(0), _asst(100)])
    v = ww.evaluate_dual_signal(path, baseline_len=0, window_sec=WINDOW, now=T0 + 120)
    assert v["state"] == "rescued"
    assert v["signal_a"] is True and v["signal_b"] is True
    assert v["over_window"] is False


def test_tool_use_only_assistant_counts_as_signal_b(tmp_path):
    """A tool-use-only (no text) main-conversation assistant turn still counts as signal B (the session is active and continuing)."""
    path = _write(tmp_path, [_user(0), _asst_tooluse(50)])
    v = ww.evaluate_dual_signal(path, baseline_len=0, window_sec=WINDOW, now=T0 + 60)
    assert v["state"] == "rescued"
    assert v["signal_b"] is True


# ── boundary: an API-error assistant turn does not count as signal B ─────────────────
def test_api_error_assistant_does_not_count_as_signal_b(tmp_path):
    """Even if there's an assistant turn after signal A, if it's an API-error (isApiErrorMessage) → doesn't count as signal B, not rescued."""
    path = _write(tmp_path, [_user(0), _asst_apierror(100)])
    v = ww.evaluate_dual_signal(path, baseline_len=0, window_sec=WINDOW, now=T0 + 120)
    assert v["state"] == "delivered"
    assert v["signal_b"] is False


# ── boundary: an assistant turn beyond the window does not escalate to rescued ───────
def test_assistant_beyond_window_does_not_rescue(tmp_path):
    """The assistant turn after signal A lands outside the window (asst_ts-user_ts>window) → doesn't count as signal B → delivered."""
    path = _write(tmp_path, [_user(0), _asst(700)])  # 700s > the 600s window
    v = ww.evaluate_dual_signal(path, baseline_len=0, window_sec=WINDOW, now=T0 + 800)
    assert v["state"] == "delivered"
    assert v["signal_b"] is False
    assert v["over_window"] is True


# ── boundary: baseline cutoff (a stale prior user turn doesn't count as this rescue's signal A) ──
def test_baseline_excludes_stale_prior_user_turn(tmp_path):
    """A user turn that already existed before the baseline doesn't count as this rescue's signal A (guards against mistaking a stale old wake for this delivery)."""
    # index 0 = a stale old user turn (before the baseline); baseline_len=1 → no new user turn after it → pending.
    path = _write(tmp_path, [_user(-500), _asst_apierror(-400)])
    v = ww.evaluate_dual_signal(path, baseline_len=1, window_sec=WINDOW, now=T0)
    assert v["state"] == "pending"
    assert v["signal_a"] is False


# ── boundary: a sidechain doesn't count as the main conversation ─────────────────────
def test_sidechain_assistant_does_not_count_as_signal_b(tmp_path):
    """Only a sidechain assistant turn after signal A → not the main conversation, doesn't count as signal B."""
    path = _write(tmp_path, [_user(0), _asst(100, sidechain=True)])
    v = ww.evaluate_dual_signal(path, baseline_len=0, window_sec=WINDOW, now=T0 + 120)
    assert v["state"] == "delivered"
    assert v["signal_b"] is False


# ── the three states strung together (pending → delivered → rescued) ────────────────
def test_three_state_progression(tmp_path):
    """The same rescue as the transcript evolves: pending → delivered (signal A) → rescued (signal B within the window)."""
    # pending
    p0 = _write(tmp_path, [_asst_apierror(-10)])
    assert ww.evaluate_dual_signal(p0, 1, WINDOW, T0 + 10)["state"] == "pending"
    # delivered (signal A is on record, still pending within the window)
    p1 = _write(tmp_path, [_asst_apierror(-10), _user(0)])
    assert ww.evaluate_dual_signal(p1, 1, WINDOW, T0 + 50)["state"] == "delivered"
    # rescued (an assistant turn follows within the window)
    p2 = _write(tmp_path, [_asst_apierror(-10), _user(0), _asst(120)])
    assert ww.evaluate_dual_signal(p2, 1, WINDOW, T0 + 130)["state"] == "rescued"


# ── gate anchor: the existing test suite is fully green (zero changes to the three-state liveness-routing defer branch) ──
def test_existing_suite_green():
    """The existing wake-watcher test suite (the non-networked part) is deterministically
    fully green — the machine-checkable criterion for "the existing test suite is fully
    green".

    Why written this way (instead of having the gate run test_e2e.py directly): test_e2e.py's
    continuation smoke test needs to spin up a real `claude -p` session + a real resume (real
    API quota), which isn't suitable for an unattended gate re-run; and it's written in
    `python test_e2e.py` script style (main()), with no pytest-collectible test_ functions →
    `pytest test_e2e.py` returns rc=5 'no tests ran' → the gate would always judge it failed.
    So this test instead runs each existing test file's **real verification entry point** via
    subprocess (they use _check + sys.exit to assert true/false — any internal assertion
    failing makes the whole file exit non-zero), and any non-zero exit is a fail — this is a
    gate anchor that genuinely catches regressions, doesn't burn API, and is pytest-collectible.
    The real-API continuation smoke test is still run manually via test_e2e.py's main() (see
    its HONESTY NOTE), and is not part of this gate.

    Covers the full existing suite of non-networked tests (including the three-state
    liveness-routing defer branch in test_loop_control / test_dedup_fix /
    test_attach_inject_routing), checked point-by-point against the owner's hard constraint
    of "zero changes to the defer branch".
    """
    import subprocess

    existing = [
        "test_classify",
        "test_dedup_fix",
        "test_loop_control",
        "test_scope",
        "test_watermark",
        "test_attach_inject_routing",
    ]
    for name in existing:
        r = subprocess.run(
            [sys.executable, str(HERE / f"{name}.py")],
            capture_output=True, text=True, timeout=180,
        )
        assert r.returncode == 0, (
            f"既有测试 {name}.py 未全绿 (rc={r.returncode}) — 违反 dc5『既有测试套件全绿』:\n"
            f"stdout tail:\n{r.stdout[-700:]}\nstderr tail:\n{r.stderr[-300:]}"
        )
