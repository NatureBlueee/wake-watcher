#!/usr/bin/env python3
"""Wake-watcher — auto-detect transient-error-interrupted Claude Code bg jobs and
wake them to retry+continue.

WHY (owner): During repeated deploys, sub-agent / bg sessions get interrupted by
"not your fault" transient errors (Connection closed / rate limit / overloaded / 5xx)
and just sit there stuck until a human manually says "retry and continue." This
watcher auto-detects that kind of interruption → auto `claude --resume <session> -p
"<wake message>"` to make it keep going on its own. Owner's fixed mandate: "when a
session's network gets interrupted, send it a message to keep it going — that's it."

Mechanism (established from real measurement, see README):
  - Claude Code bg/daemon job lifecycle lives in ~/.claude/jobs/<id>/state.json
    (state, detail) + claude agents --json.
  - When a transient error interrupts, state flips to blocked/failed, detail = the
    error string, process exits (no Stop hook fires — measured: job session-A's
    timeline has only a single blocked entry, no hook, no daemon reaction).
    => So Stop hook can't be used for detection; external polling is required.
  - Two paths for waking, depending on whether the session is dead or alive
    (root-caused by the finding that resume gets rejected for live background
    agents, confirmed 2026-06-26; per the project's red line, a live session must
    NEVER get --resume/--fork-session):
      Dead orphan (process has exited / not in `claude agents --json`'s active
        list) → original design: `claude --resume <sessionId> -p "..."` (measured
        to correctly resume the original conversation context, and it appends this
        retry message into the same transcript — this is the basis for decision
        rule #2).
      Live daemon-bg agent (still in `claude agents --json`, kind=background) →
        the CLI flatly rejects --resume ("running as a background agent"); the
        correct primitive = type the message into its input box like a human
        would: `pty.fork()` a fresh PTY → exec `claude attach <short id>` → wait
        for the input box to render → write the message + Enter → Ctrl+Z to
        detach (session keeps running) → read the transcript to verify a user
        turn actually landed (implemented in `_pty_attach_inject`; this path was
        confirmed by direct measurement, not inferred).
      Liveness can't be determined (query failed / a live non-background session)
        → defer, never guess dead-or-alive.

Detection criterion (owner 2026-06-25/26 -- the finding that stale-state detection
is unreliable and live signal is required; verified and working, then lost;
recovered as-is during the 2026-07-02 OOM incident root-cause fix, see the git
commit message):
  Read the transcript (.jsonl, state.json.linkScanPath) and find the [last assistant
  message in the main conversation]:
    Not an API error (normal output) → don't wake, and this counts as the AI having
      genuinely moved past it (the only "real progress" signal; resets the budget).
    Is an API error but non-transient (403/quota/auth etc. — veto) → don't wake
      (a genuine stop; leave the budget untouched).
    Is a transient API error AND already has a user message after it (= a retry
      was already sent) → don't wake (natural dedup).
    Is a transient API error with no user message after it → wake once.
  Deliberately NOT used: scanning the single state.json.detail field (goes stale —
  the AI can move past it and it's still sitting there) / treating transcript file
  size:mtime as "progress" (root cause of the night-of-2026-07-01 OOM incident: the
  watcher's own resume appends to the file, so it necessarily grows, which got
  misread as "progress" and endlessly reset the budget — combine that with "don't
  give up when capped" and you get a 60s infinite wake loop, each wake spinning up
  a new process with a 3GB cold load, stacking up and crashing the machine in 9
  hours) / using error_signature changes as the dedup axis (the same error can
  legitimately recur; whether the signature changes or not is not the correct
  criterion for resetting the budget).

Spec (owner's fixed rules, all implemented in code):
  #1 Only wake for transient infra errors (the transcript-tail criterion has
     classify's allowlist+default-deny+veto baked in).
  #2 Fast retries have a hard cap (MAX_WAKES, exponential backoff BACKOFF_BASE);
     once capped, [never give up permanently] — surface NEEDS-HUMAN once (for
     visibility), then fall through to the hourly-scale slow lane and keep waking
     (owner 2026-07-02: unattended operation needs to ride out multi-hour network
     outages, and once the network recovers it should pick back up automatically —
     it shouldn't get permanently stuck waiting for a human just because it hit
     the cap a few times in a row; but it must NEVER be a 60s fast loop — that's
     the root cause of the crash).
  #3 Every wake/skip/defensive-refusal leaves a visible trace (written to
     wake-watcher.log + ledger.json, never silent).
  #4 Don't touch existing hooks / settings.json (this is a standalone external
     process, zero settings changes).
  #5 Don't wake — and don't count against the retry budget — during a network
     outage/shutdown (a machine-level network outage is not a session-level
     failure; it shouldn't burn fast-lane budget for nothing; once the network
     recovers, normal waking resumes on the very next round).
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify import classify

# ── Config (spec #2 cap + dual-lane backoff) ──────────────────────────────────
HOME = Path(os.environ.get("WAKE_WATCHER_CLAUDE_HOME", str(Path.home() / ".claude")))
JOBS_DIR = HOME / "jobs"
STATE_DIR = Path(__file__).resolve().parent
LOG_FILE = Path(os.environ.get("WAKE_WATCHER_LOG", str(STATE_DIR / "wake-watcher.log")))
# Process liveness heartbeat -- a dedicated file, decoupled from the business log.
# The main loop touches it unconditionally every round (even when idle, with no
# candidates this round, and nothing written to the business log). The watchdog's
# liveness check must read this file's mtime, not LOG_FILE's mtime -- an idle poll
# round legitimately writes nothing to the business log (scan_once only calls
# log() when there's a candidate/state flip/wake action), so treating a quiet
# business log as "process is dead" would misjudge a process that's perfectly
# alive but just has nothing to do, and it would get force-killed and restarted
# by kickstart (measured root cause: ~40 false kills a day).
HEARTBEAT_FILE = Path(
    os.environ.get("WAKE_WATCHER_HEARTBEAT", str(STATE_DIR / "wake-watcher.heartbeat"))
)
LEDGER_FILE = Path(os.environ.get("WAKE_WATCHER_LEDGER", str(STATE_DIR / "ledger.json")))
NEEDS_HUMAN_FILE = Path(
    os.environ.get("WAKE_WATCHER_NEEDS_HUMAN", str(STATE_DIR / "needs-human.log"))
)  # give-up surface (spec #3)

# ── Three-layer resolution of the claude executable + fail-loud on resolution
#    failure ──────────────────────────────────────────────────────────────────
# Root cause (confirmed): launchd's default PATH doesn't include ~/.local/bin,
# while claude=~/.local/bin/claude; calling the bare string 'claude' via PATH
# lookup raises FileNotFoundError under launchd → a liveness-query error gets
# misread as "not enough signal." This module turns "locate the executable" from
# an implicit PATH dependency into an explicit, deterministic three-layer
# resolution (plist PATH → shutil.which → known-location probing).
SERVICE_NAME = "wake-watcher"
CLAUDE_BINARY_NAME = "claude"
# fail-loud dedup slot (alert once per outage period + reset once resolved);
# each service holds its own state, no centralized store.
CLAUDE_RESOLUTION_STATE_FILE = Path(
    os.environ.get(
        "WAKE_WATCHER_CLAUDE_RESOLUTION_STATE",
        str(STATE_DIR / "claude-resolution-state.json"),
    )
)
# One of the three elements of the alert text (the fix hint). Each layer's failure
# reason is returned and rendered by resolve_claude_binary() layer by layer.
CLAUDE_FIX_HINT = (
    "Confirm the claude CLI is installed and on PATH (~/.local/bin/claude), or "
    "inject a PATH containing ~/.local/bin via the launchd plist's "
    "EnvironmentVariables, or set WAKE_WATCHER_CLAUDE_KNOWN_PATHS to the binary's "
    "absolute path."
)

# ── Rescue live-fire dual-signal window ────────────────────────────────────────
# Signal A = delivered (a new main-conversation user turn lands in the transcript);
# Signal B = resumed (within the window after signal A lands, a non-API-error main-
# conversation assistant turn follows). Signal A alone never counts as rescued; if it
# doesn't resume within the window → fall back to the existing retry/alert path.
# Window length is wall-clock seconds, overridable via env.
RESCUE_CONTINUE_WINDOW_SEC = float(
    os.environ.get("WAKE_WATCHER_RESCUE_CONTINUE_WINDOW", "600")
)

# PTY injection types into a session that is still running -- the highest blast
# radius primitive in this repo. Off unless explicitly enabled: someone installing
# this for the first time should get the conservative mode (resume dead orphans
# only) until they have read THREAT-MODEL.md and decided otherwise.
ENABLE_PTY_INJECT = os.environ.get("WAKE_WATCHER_ENABLE_PTY_INJECT", "0") == "1"

MAX_WAKES = int(os.environ.get("WAKE_WATCHER_MAX_WAKES", "3"))  # #2: fast-lane budget
BACKOFF_BASE_SEC = int(os.environ.get("WAKE_WATCHER_BACKOFF", "60"))  # fast lane: 60,120,240...
# Slow-lane cadence (owner 2026-07-02: once capped, [never give up permanently], but
# it must never be a 60s fast loop -- that was the direct root cause of the night-of-
# 2026-07-01 OOM incident). Once MAX_WAKES is hit, every subsequent wake uses this
# fixed interval (no more exponential growth).
SLOW_RETRY_SEC = int(os.environ.get("WAKE_WATCHER_SLOW_RETRY", "3600"))  # defaults to once/hour
POLL_INTERVAL_SEC = int(os.environ.get("WAKE_WATCHER_POLL", "30"))
# Wake message wording (owner-supplied seed text, overridable via env)
WAKE_MESSAGE = os.environ.get(
    "WAKE_WATCHER_MESSAGE",
    "There was a transient network hiccup just now (a transient infra error, not "
    "a failure in your task). Please retry and continue from where you were "
    "interrupted -- finish the step you didn't finish, don't start over.",
)
# Safety valve: only wake jobs whose process has already died (no live process =
# won't race the daemon).
SAFETY_REQUIRE_DEAD_PROCESS = os.environ.get("WAKE_WATCHER_REQUIRE_DEAD", "1") != "0"

# ── Attach+PTY injection primitive for live sessions (owner decided 2026-06-26;
#    per the project's red line, a live daemon-bg agent must NEVER get
# --resume/--fork-session -- the only option is to type the message into its
#    input box like a human would. Established by direct measurement: resume gets
#    rejected for live background agents, confirmed 2026-06-26.) ───────────────
PTY_READY_TIMEOUT_SEC = float(os.environ.get("WAKE_WATCHER_PTY_READY_TIMEOUT", "15"))
# Measured (reference memory): writing the user turn to disk has latency, >8s
# observed -- the verify window must be generous, don't misjudge failure just
# because one read comes up empty.
PTY_DELIVER_VERIFY_TIMEOUT_SEC = float(os.environ.get("WAKE_WATCHER_PTY_VERIFY_TIMEOUT", "15"))
PTY_PROMPT_MARKER = "❯"  # the prompt marker `claude attach` renders once the input box is up (measured)
# Root-cause fix for the 2026-07-08 bug where the live-fire rescue chain went
# non-functional: a hard cap on the reaping phase (if the child process still
# hasn't exited after SIGKILL, wait at most this long and then give up -- never
# block unbounded, see the notes inside
# _reap_pty_child). Both phases are promoted to env-overridable module constants
# (rather than literals) so tests can pin down the "bounded" invariant with short
# timeouts, instead of actually waiting 5s+.
PTY_REAP_SOFT_WAIT_SEC = float(os.environ.get("WAKE_WATCHER_PTY_REAP_SOFT_WAIT", "5"))
PTY_REAP_HARD_KILL_GRACE_SEC = float(os.environ.get("WAKE_WATCHER_PTY_REAP_GRACE", "10"))

# ── Resource-aware gate (last line of defense, not the primary fix) ────────────
# The primary root cause of the crash was loop control getting defeated by "file
# grew = progress" (see the detection-criterion section above), which is fixed at
# the root by the transcript-tail criterion. This gate is just a fuse: in case some
# other stacking pattern shows up in the future, it's one last block when the
# machine is genuinely overloaded -- it is not meant to be the only line of defense.
MAX_LOAD_FACTOR = float(os.environ.get("WAKE_WATCHER_MAX_LOAD_FACTOR", "1.5"))  # 1-min load > cores × this = overloaded → DEFER

# ── Network-outage defer (spec #5, owner 2026-06-27/07-02) ──────────────────────
# A machine-level network outage/shutdown is not a session-level failure and
# shouldn't burn fast-lane budget for nothing -- if WiFi drops for a few hours,
# there should still be a full fast-lane budget left once it's back; a "wake
# failed" during the outage must not get miscounted as transient-retry exhaustion.
_LAST_NET_STATE: bool | None = None  # module-level; only logs on a state flip (valid across rounds within the long-lived poll process)


def network_reachable() -> bool:
    """Probe whether the network is reachable (stdlib socket, no external deps).

    Test seam WAKE_WATCHER_FAKE_NET: "1" = treat as reachable, "0" = treat as
    unreachable, unset = probe for real.
    A real probe failure is always judged "unreachable" (fail-closed to defer,
    safer than an erroneous spawn).
    """
    fake = os.environ.get("WAKE_WATCHER_FAKE_NET")
    if fake is not None:
        return fake != "0"
    import socket

    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


# ── Scope narrowing (owner 2026-06-22, a dry-run on launch measured it waking
#    sessions it shouldn't have) ────────────────
# #5 Only handle jobs from [this project]: sessions whose cwd is not under this
#    project's root (other projects, e.g. an unrelated project’s session
#    session-A) must never be woken. This project's root defaults to two levels up
#    from this script's directory (.claude/wake-watcher → .claude → <project
#    root>), not a hardcoded absolute path. Empty string = disable this filter
#    (sandbox tests point env at their own root).
_PROJECT_ROOT_RAW = os.environ.get("WAKE_WATCHER_PROJECT_ROOT", str(STATE_DIR.parent.parent))
PROJECT_ROOT: Path | None = (
    Path(_PROJECT_ROOT_RAW).resolve() if _PROJECT_ROOT_RAW.strip() else None
)
# Optional external liveness hook, used to double-check a do-not-wake marker
# query (do-not-wake liveness-aware recheck).
# Optional hook for asking an external system whether a session is really alive.
# A command template; "{session_id}" is substituted before it runs. Unset (the default)
# means the check is skipped entirely and the caller stays conservative -- this hook is
# strictly an extra veto, never a reason to wake something we otherwise would not.
#
# SECURITY: whatever this points at runs on every poll with this process's privileges.
# Setting it is the same trust decision as adding a cron entry. See THREAT-MODEL.md.
LIVENESS_CMD = os.environ.get("WAKE_WATCHER_LIVENESS_CMD", "").strip()
LIVENESS_CMD_TIMEOUT_SEC = float(os.environ.get("WAKE_WATCHER_LIVENESS_TIMEOUT", "45"))
# #6 do-not-wake list: a session in this project that was [deliberately stopped]
#    must never be auto-woken (e.g. the fix session session-H that owner/main-
#    control manually stopped -- its cwd is under this project, so the cwd filter
#    above can't exclude it; an explicit deny list is required).
#    Source: env WAKE_WATCHER_DO_NOT_WAKE (comma/whitespace-separated ids) ∪ file
#    do-not-wake.txt (one session id per line, supports trailing # comments).
#    Re-read every round → adding an id to the file while running takes effect
#    immediately, no need to restart the watcher.
DO_NOT_WAKE_FILE = Path(
    os.environ.get("WAKE_WATCHER_DO_NOT_WAKE_FILE", str(STATE_DIR / "do-not-wake.txt"))
)
# #7 "now" watermark (owner 2026-06-22: stop retroactively waking old, already-
#    dead sessions).
#    Only handle sessions that entered the stuck state after the watcher first
#    started -- i.e. state.json.updatedAt is later than this starting point.
#    Persisted + reused (survives restarts): on startup, read watermark.json
#    first; use it if it holds a valid value, otherwise initialize it to now and
#    write it to disk.
#    Reset = delete watermark.json (or --reset-watermark).
#    env WAKE_WATCHER_WATERMARK explicitly set empty → disables this filter (let
#      everything through, for sandbox/backward-compat); non-empty (ISO8601) →
#      use it as a fixed watermark (for tests, doesn't read/write the file).
WATERMARK_FILE = Path(
    os.environ.get("WAKE_WATCHER_WATERMARK_FILE", str(STATE_DIR / "watermark.json"))
)
_WATERMARK_ENV_RAW = os.environ.get("WAKE_WATCHER_WATERMARK")  # None = use the file; "" = disabled; ISO = fixed


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso_from_epoch(ep: float) -> str:
    return datetime.fromtimestamp(ep, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(s: str | None) -> float | None:
    """ISO8601 (with a Z suffix, UTC) → epoch seconds. Returns None if it can't be
    parsed (for fail-closed use).

    Python 3.9's datetime.fromisoformat doesn't accept a 'Z' suffix (only 3.11+
    does) -- and the watcher actually runs on 3.9. So swap 'Z' for '+00:00' before
    parsing (confirmed on 3.9: it can parse fractional seconds + +00:00).
    """
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def log(msg: str) -> None:
    """Spec #3: leave a visible trace -- append to the log file + stderr."""
    line = f"[{_now_iso()}] {msg}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


def touch_heartbeat() -> None:
    """Lightweight process-liveness heartbeat -- called unconditionally once per
    main-loop round, regardless of whether there's a business-log entry to write.
    The watchdog's liveness check reads this file's mtime, not LOG_FILE's mtime."""
    try:
        HEARTBEAT_FILE.touch()
    except OSError:
        pass


def surface_needs_human(session_id: str, detail: str, wakes: int) -> None:
    """Spec #3: hitting the fast-lane budget cap = needs a human to take a look,
    must not silently vanish.

    This is not "giving up" -- it falls through to the slow lane and keeps waking
    (owner 2026-07-02: no need to give up).
    """
    line = (
        f"[{_now_iso()}] NEEDS-HUMAN session={session_id} "
        f"auto-woken {wakes} times and still stuck on a transient error; "
        f"fast-lane budget exhausted, falling through to the slow lane "
        f"(retrying every {SLOW_RETRY_SEC}s) to keep waiting for the network to "
        f"recover -- please take a look."
        f" detail={detail[:160]}"
    )
    for f_path in (NEEDS_HUMAN_FILE, LOG_FILE):
        try:
            with f_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    print(line, file=sys.stderr, flush=True)


# Auth-family non-transient errors (Login expired / OAuth invalidated...):
# auto-retry is useless here, only a human can fix it.
# Before 2026-07-12 these sessions were silently skipped by default-deny, and
# owner never found out (instance: a main coordination session sat stuck all
# night on 'Login expired' at the end, zero needs-human records).
AUTH_BLOCKED_PATTERN = r"login|logged.?out|authenticat|credential|oauth"


def _is_auth_blocked_detail(detail: str | None) -> bool:
    """Pick the auth family out of non-transient skips -- these must surface to
    needs-human, silence is not allowed."""
    return bool(detail) and re.search(AUTH_BLOCKED_PATTERN, detail, re.IGNORECASE) is not None


def surface_auth_blocked(session_id: str, detail: str) -> None:
    """Report an auth-family error: unlike hitting the budget cap, there will be no
    automatic wake here at all -- the message text needs to say so honestly."""
    line = (
        f"[{_now_iso()}] NEEDS-HUMAN session={session_id} "
        f"auth/credential-type error, auto-wake is useless here (the watcher "
        f"won't attempt it), needs human re-authentication."
        f" detail={detail[:160]}"
    )
    for f_path in (NEEDS_HUMAN_FILE, LOG_FILE):
        try:
            with f_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    print(line, file=sys.stderr, flush=True)


# ── Three-layer claude resolution protocol ─────────────────────────────────────
def _claude_known_candidates() -> list[str]:
    """Layer 3 known-install-location candidates. First entry = the expansion of
    ~/.local/bin/claude (pinned by design).

    Test seam WAKE_WATCHER_CLAUDE_KNOWN_PATHS (os.pathsep-separated) overrides the
    entire candidate list -- still within Layer 3 (doesn't introduce a 4th layer,
    the 1→2→3 layer order is unchanged), letting tests deterministically construct
    "known-location hit" / "all candidates miss" cases.
    """
    raw = os.environ.get("WAKE_WATCHER_CLAUDE_KNOWN_PATHS")
    if raw is not None:
        return [p for p in raw.split(os.pathsep) if p.strip()]
    return [os.path.expanduser("~/.local/bin/claude")]


def resolve_claude_binary() -> tuple[str | None, list[dict[str, str]]]:
    """Deterministically resolve the claude executable across three layers.

    Layer order is fixed 1→2→3 and short-circuits (the first layer that hits
    returns its absolute path):
      Layer 1  plist EnvironmentVariables PATH injection -- a prerequisite layer,
               not judged as a hit on its own (once it's in place, the normal path
               is a Layer 2 hit); this layer only records whether PATH contains
               ~/.local/bin, for the fail-loud per-layer message text.
      Layer 2  runtime shutil.which('claude') -- a non-None absolute path
               returned counts as a hit (primary).
      Layer 3  known-install-location probing -- os.path.exists ∧ os.access(X_OK)
               one by one, the first one that passes is the hit (fallback).

    Returns (resolved_path | None, per_layer_reasons). If all three layers fail →
    (None, reasons), delegated to the fail-loud path to handle (the caller defers
    for this round): never guess a path, never raise bare, never call the bare
    string 'claude'.
    """
    reasons: list[dict[str, str]] = []
    local_bin = os.path.expanduser("~/.local/bin")
    # Layer 1 (prerequisite layer)
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if local_bin in path_dirs:
        reasons.append({"layer": "1-plist-PATH", "reason": f"PATH contains {local_bin} (Layer 1 prerequisite satisfied)"})
    else:
        reasons.append({
            "layer": "1-plist-PATH",
            "reason": f"PATH does not contain {local_bin} (launchd minimal PATH? plist didn't inject "
                      f"EnvironmentVariables PATH) -- falling to Layer 2/3",
        })
    # Layer 2 (primary)
    which_hit = shutil.which(CLAUDE_BINARY_NAME)
    if which_hit:
        reasons.append({"layer": "2-shutil-which", "reason": f"hit {which_hit}"})
        return which_hit, reasons
    reasons.append({"layer": "2-shutil-which", "reason": "shutil.which('claude') returned None (not found in current PATH)"})
    # Layer 3 (fallback)
    candidates = _claude_known_candidates()
    for cand in candidates:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            reasons.append({"layer": "3-known-paths", "reason": f"hit {cand} (exists and executable)"})
            return cand, reasons
    reasons.append({"layer": "3-known-paths", "reason": f"all candidates missed {candidates} (don't exist or aren't executable)"})
    return None, reasons


# ── claude resolution failure fail-loud ──────────────
def _load_resolution_state() -> dict:
    try:
        return json.loads(CLAUDE_RESOLUTION_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_resolution_state(state: dict) -> None:
    try:
        tmp = CLAUDE_RESOLUTION_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(CLAUDE_RESOLUTION_STATE_FILE)
    except OSError as e:
        log(f"WARN claude-resolution state save failed: {e}")


def _render_claude_unresolvable_alert(reasons: list[dict[str, str]]) -> str:
    """Three elements of the alert text: (a) service name (b) each layer's own failure reason
    (per-layer, not a single 'not found') (c) fix hint."""
    layer_lines = "; ".join(f"[{r['layer']}] {r['reason']}" for r in reasons)
    return (
        f"NEEDS-HUMAN service={SERVICE_NAME} claude-unresolvable — claude binary three-layer resolution failed entirely, "
        f"all rescue actions this round deferred (no resume/attach/respawn/kill). Per-layer failure reasons: {layer_lines}. "
        f"Fix hint: {CLAUDE_FIX_HINT}"
    )


# In-process memory dedup fallback: lives only in the current process (across scan rounds of the
# persistent while-True loop). When the state file can't be persisted (scenario B) it still prevents
# rewriting needs-human.log every round, so 'exactly 1 alert per failure episode' doesn't
# rely solely on the persistable state file. Across processes (successive --once invocations) it
# still falls back to the state file on restart — an unavoidable boundary when there's no persistent
# state, and the contract explicitly accepts this in-memory fallback.
_unresolvable_alerted_mem = False


def note_claude_unresolvable(reasons: list[dict[str, str]]) -> None:
    """① Alert (not silent → write the needs-human.log three elements) + ③ dedup within the same
    failure episode (already alerted → only log it, don't rewrite).

    ② Resolution failure always defers, guaranteed by the caller (this function is only responsible
    for alert/dedup, it triggers no rescue action itself).

    Dedup is bound to 'the human-facing alert (needs-human.log) actually got written' rather than
    'attempted': when needs-human.log write fails, don't set the dedup slot — retry and backfill next
    round, so that '① not silent' isn't swallowed by dedup under an IO failure; layered on top, the
    in-process memory fallback (_unresolvable_alerted_mem) keeps dedup from relying solely on the
    persistable state file, so '③ exactly 1' doesn't flood the log when the state write fails.
    """
    global _unresolvable_alerted_mem
    state = _load_resolution_state()
    if _unresolvable_alerted_mem or state.get("unresolvable_alerted"):
        log("claude-unresolvable already alerted this failure episode, logging only, not rewriting needs-human.log (dedup).")
        return
    line = f"[{_now_iso()}] {_render_claude_unresolvable_alert(reasons)}"
    # The human-facing give-up surface = needs-human.log: whether dedup gets set is bound only to
    # whether it actually got written (LOG_FILE/stderr are secondary surfaces — whether they succeed
    # or not doesn't affect the dedup decision, otherwise needs-human.log staying silent while LOG
    # succeeds would wrongly set dedup and reproduce scenario A).
    needs_human_written = False
    try:
        with NEEDS_HUMAN_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        needs_human_written = True
    except OSError as e:
        log(f"WARN claude-unresolvable needs-human.log write failed, not setting dedup slot this round, retry and backfill next round: {e}")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)
    if not needs_human_written:
        return  # human-facing write failed → don't set dedup, don't persist state, retry and backfill next round (① not silent)
    _unresolvable_alerted_mem = True
    state["unresolvable_alerted"] = True
    state["alerted_at"] = _now_iso()
    _save_resolution_state(state)


def clear_claude_unresolvable_alert() -> None:
    """③ Reset after resolution: when three-layer resolution succeeds again (failure resolved), clear
    the dedup marker → alert once more the next time it fails.

    The only reset condition = resolution succeeding again (not a time window / manual clear / restart).
    No-op when there's no marker (doesn't create a state file, keeping the normal path zero-side-effect).
    The in-memory fallback marker is reset in lockstep — otherwise under scenario B (state never
    persisted to disk), after the failure resolves, a lingering in-memory marker would silently dedup
    away the next real failure, which is itself a regression of '① not silent'.
    """
    global _unresolvable_alerted_mem
    state = _load_resolution_state()
    if _unresolvable_alerted_mem or state.get("unresolvable_alerted"):
        _unresolvable_alerted_mem = False
        state["unresolvable_alerted"] = False
        state["resolved_at"] = _now_iso()
        _save_resolution_state(state)
        log("claude-unresolvable resolved (three-layer resolution succeeded again) — dedup marker reset.")


def ensure_claude_or_fail_loud() -> str | None:
    """Unified entry point for three-layer resolution + fail-loud dedup + reset-on-success.

    Returns resolved_path (success, dedup marker reset) or None (all three layers failed, already
    alerted + deduped, caller defers this round).
    This is where the rescue_action_on_failure == defer_only load-bearing invariant is honored:
    None → the caller must never act.
    """
    claude_path, reasons = resolve_claude_binary()
    if claude_path is None:
        note_claude_unresolvable(reasons)
        return None
    clear_claude_unresolvable_alert()
    return claude_path


# ── ledger: persists per-session wake count/backoff (spec #2; survives watcher restarts) ──────
def load_ledger() -> dict:
    try:
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_ledger(ledger: dict) -> None:
    try:
        tmp = LEDGER_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(LEDGER_FILE)
    except OSError as e:
        log(f"WARN ledger save failed: {e}")


def read_job_state(job_dir: Path) -> dict | None:
    sf = job_dir / "state.json"
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ── transcript tail criterion (owner 2026-06-25/26) ──
def _load_transcript_records(path: str) -> list[dict]:
    """Read the transcript (.jsonl), parse line by line, skip lines that can't be read/parsed."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    recs: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            recs.append(obj)
    return recs


def _message_text(rec: dict) -> str:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_main_assistant(rec: dict) -> bool:
    return rec.get("type") == "assistant" and not rec.get("isSidechain")


def _is_main_user(rec: dict) -> bool:
    return rec.get("type") == "user" and not rec.get("isSidechain")


def _rec_timestamp(rec: dict) -> float | None:
    """The transcript record's persisted-to-disk moment (epoch). Missing/unparseable → None."""
    return _parse_iso_utc(rec.get("timestamp"))


# ── rescue-success acceptance dual-signal mechanical decision ──────────────────
def evaluate_dual_signal(
    transcript_path: str | None, baseline_len: int, window_sec: float, now: float
) -> dict[str, object]:
    """Decide which of the three states an already-initiated rescue action is currently in (rescued =
    signal A ∧ signal B within the window; signal A alone never counts as rescued).

    baseline_len = the transcript record count before the rescue action was initiated (the baseline
    before signal A, a boundary guard against mistaking a historical old turn for this one).
      Signal A (delivered) = a main-conversation user turn appearing after baseline_len (the wake
                    message has been delivered).
      Signal B (resumed) = a main-conversation assistant turn after signal A, that is 【not an
                    API-error】 (isApiErrorMessage is not True), and falls within
                    [user_ts, user_ts+window_sec] (an assistant turn past the window doesn't promote
                    to rescued). A tool-use-only (no text) main-conversation assistant turn still
                    counts (the session being active at all means it resumed).
    Returns {"state","signal_a","signal_b","over_window","user_ts","reason"}:
      state=pending   No signal A (rescue action initiated, delivery unconfirmed).
      state=delivered Signal A holds, signal B within the window doesn't hold yet (over_window: now
                    has already passed the window → falls back to retry/alert).
      state=rescued   Signal A ∧ signal B within the window both hold = truly rescued.
    """
    if not transcript_path:
        return {"state": "pending", "signal_a": False, "signal_b": False,
                "over_window": False, "user_ts": None, "reason": "no transcript path"}
    recs = _load_transcript_records(transcript_path)
    user_idx: int | None = None
    for i in range(max(baseline_len, 0), len(recs)):
        if _is_main_user(recs[i]):
            user_idx = i
            break
    if user_idx is None:
        return {"state": "pending", "signal_a": False, "signal_b": False,
                "over_window": False, "user_ts": None,
                "reason": "signal A not established: no new main-conversation user turn after the baseline"}
    user_ts = _rec_timestamp(recs[user_idx])
    for j in range(user_idx + 1, len(recs)):
        rec = recs[j]
        if not _is_main_assistant(rec):
            continue
        if rec.get("isApiErrorMessage") is True:
            continue  # an API-error assistant turn isn't "genuinely continuing to work", doesn't count as signal B
        asst_ts = _rec_timestamp(rec)
        if user_ts is not None and asst_ts is not None and (asst_ts - user_ts) > window_sec:
            continue  # an assistant turn past the window doesn't count as signal B (past the window doesn't promote to rescued)
        return {"state": "rescued", "signal_a": True, "signal_b": True, "over_window": False,
                "user_ts": user_ts,
                "reason": "signal A (delivered) AND in-window signal B (a non-API-error main-conversation assistant turn followed) both hold = rescued"}
    over = user_ts is not None and (now - user_ts) > window_sec
    return {"state": "delivered", "signal_a": True, "signal_b": False, "over_window": over,
            "user_ts": user_ts,
            "reason": ("window expired with no signal B: delivered only, never resumed" if over else "signal A holds, signal B still pending in-window")}


def surface_delivered_only(session_id: str, window_sec: float) -> None:
    """The past-window honoring of the dual-signal rescue mechanism: rescue only delivered (signal A),
    not resumed within the window (signal B) → falls back to the alert path.

    This is the visible honoring of "signal A alone never counts as rescued": the rescue is suspected
    to not have taken effect (the old 2026-06-26 silent-false-success defect was exactly missing this
    step). Uses the distinct marker 'RESCUE-DELIVERED-ONLY', not to be confused with
    'NEEDS-HUMAN session=' which caps out the fast-lane budget.
    """
    line = (
        f"[{_now_iso()}] RESCUE-DELIVERED-ONLY session={session_id} the rescue wake message was delivered (signal A) but "
        f"within {window_sec:.0f}s the session did not resume with a main-conversation assistant turn (signal B) — delivered only is not rescued, the rescue is suspected to not have taken effect, "
        f"please take a human look."
    )
    for f_path in (NEEDS_HUMAN_FILE, LOG_FILE):
        try:
            with f_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    print(line, file=sys.stderr, flush=True)


def transcript_wake_decision(st: dict) -> dict:
    """Read the tail of the transcript, decide whether it's genuinely stuck right now on a
    【transient API error that hasn't been retried】.

    Empirically observed shape (test_dedup_fix.py): API error = an assistant message with
    isApiErrorMessage=True, message.content=[{"type":"text","text":"API Error: ..."}].

    Algorithm (owner 2026-06-25 screenshot evidence + 2026-06-26 refined):
      1. Find the 【last main-conversation assistant message】 in the transcript (sidechain excluded).
         Not found (no transcript / all sidechain) → skip, escaped=False (insufficient data, fail-closed).
      2. It's not an API error (normal output) → skip, escaped=True (the AI has genuinely moved past
         it, the only true progress signal).
      3. It's an API error but classify() judges it non-transient (403/quota/auth etc. veto) →
         skip, escaped=False (a real stop, not a network problem, don't touch the budget).
      4. It's a transient API error: check whether there's a main-conversation user message【positioned
         after it】(= a retry has already been sent).
         Yes → skip, escaped=False (already handled, naturally deduped).
         No → send (should wake once).

    Returns {"action": "send"|"skip", "reason": str, "escaped": bool}.
    """
    path = st.get("linkScanPath") or st.get("transcriptPath")
    if not path:
        return {"action": "skip", "reason": "no transcript path", "escaped": False}
    recs = _load_transcript_records(path)

    last_asst_idx: int | None = None
    last_asst: dict | None = None
    for i, rec in enumerate(recs):
        if _is_main_assistant(rec):
            last_asst_idx = i
            last_asst = rec

    if last_asst is None or last_asst_idx is None:
        return {"action": "skip", "reason": "no assistant message in transcript", "escaped": False}

    if not last_asst.get("isApiErrorMessage"):
        return {
            "action": "skip",
            "reason": "moved past the error -- the last main-conversation message is normal output / it is working",
            "escaped": True,
        }

    text = _message_text(last_asst)
    # Anchor to the error record's own timestamp: 'resets 5:10am' means
    # the next 5:10 after the moment the error occurred. Parsing at scan time would drift to "tomorrow"
    # once past the reset point, and send would become permanently unreachable.
    verdict = classify(text, error_epoch=_rec_timestamp(last_asst))
    if not verdict["transient"]:
        # ── Third category: session limit carrying a reset time (owner 2026-07-07) ──
        # Don't wake immediately (immediate retry = hitting-the-wall infinite loop), but should
        # resume once the time arrives. The daemon scans once every 30s; every round before the time
        # arrives skips, and the round after it arrives naturally falls into send — no extra
        # scheduler needed.
        # 180s buffer: wake at reset time + 3 minutes, to guard against boundary jitter.
        if verdict.get("session_limit"):
            reset_epoch = verdict.get("reset_epoch")
            if reset_epoch is None:
                return {
                    "action": "skip",
                    "reason": "session limit but the reset time parse failed (fail-closed, treated as an ordinary veto)",
                    "escaped": False,
                }
            wake_at = reset_epoch + 180.0
            if time.time() < wake_at:
                return {
                    "action": "skip",
                    "reason": (
                        "session limit, waiting for reset before waking "
                        f"(wake_at={time.strftime('%m-%d %H:%M', time.localtime(wake_at))})"
                    ),
                    "escaped": False,
                }
            has_retry = any(_is_main_user(r) for r in recs[last_asst_idx + 1 :])
            if has_retry:
                return {
                    "action": "skip",
                    "reason": "session limit reset point passed, but a user retry already followed the error",
                    "escaped": False,
                }
            return {
                "action": "send",
                "reason": "session limit reset point passed, resuming (mechanism added 2026-07-07)",
                "escaped": False,
            }
        return {
            "action": "skip",
            "reason": f"trailing error is non-transient ({verdict['reason']})",
            "escaped": False,
        }

    has_retry_after = any(_is_main_user(r) for r in recs[last_asst_idx + 1 :])
    if has_retry_after:
        return {
            "action": "skip",
            "reason": "a user retry already followed this error, not sending again",
            "escaped": False,
        }

    return {
        "action": "send",
        "reason": f"live error, no retry sent yet ({verdict['reason']})",
        "escaped": False,
    }


def session_has_live_process(session_id: str) -> bool:
    """Spec safety valve: is there a live process currently holding this session (avoid contending with the daemon)."""
    try:
        out = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return True  # can't probe => conservatively assume "there's a live process", don't wake
    for ln in out.splitlines():
        if session_id in ln and "wake_watcher" not in ln:
            return True
    return False


def system_under_pressure() -> tuple[bool, str]:
    """Resource-aware gate (last line of defense): is the machine overloaded? If overloaded, DEFER the
    wake — don't pile another cold-load process onto an already-full machine.
    Returns (overloaded?, reason string). Uses stdlib os.getloadavg. Can't probe load = conservatively
    let it through (the gate itself shouldn't be the thing that deadlocks waking)."""
    cores = os.cpu_count() or 4
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        return False, ""  # getloadavg unavailable (rare) → don't let the gate itself deadlock waking
    if load1 > cores * MAX_LOAD_FACTOR:
        return True, f"load1={load1:.1f} > cores({cores})×{MAX_LOAD_FACTOR}"
    return False, ""


def load_do_not_wake() -> set[str]:
    """Scope narrowing #6: the current set of do-not-wake session ids (env ∪ file). Re-read every round (takes effect immediately)."""
    ids: set[str] = set()
    for part in os.environ.get("WAKE_WATCHER_DO_NOT_WAKE", "").replace(",", " ").split():
        if part.strip():
            ids.add(part.strip())
    try:
        for line in DO_NOT_WAKE_FILE.read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                ids.add(entry)
    except OSError:
        pass  # file doesn't exist = empty list, normal
    return ids


def vitality_verdict(session_id: str) -> str | None:
    """Query the vitality true-signal verdict (vitality-first, the same root-cause fix as the
    placement finding).

    do-not-wake is a one-off human/stale judgment; before honoring it, recheck with the vitality true
    signal first — don't let a stale orphan marker permanently veto a session that's genuinely still
    alive (= the same root disease as the placement finding: treating a stale/one-off signal as the
    life-or-death decision rule). Failure/timeout/no hook configured → return None, caller conservatively
    honors it.

    read-only, doesn't attach (won't wake a parked session).
    """
    # Test seam (same style as the watcher's other env test seams): "sid1=verdict1,sid2=verdict2".
    fake = os.environ.get("WAKE_WATCHER_FAKE_VITALITY", "")
    if fake:
        for pair in fake.split(","):
            k, _, v = pair.partition("=")
            if k.strip() == session_id:
                return v.strip() or None
        return None
    if not LIVENESS_CMD:
        return None
    try:
        argv = [part.replace("{session_id}", session_id) for part in shlex.split(LIVENESS_CMD)]
    except ValueError:
        return None
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=LIVENESS_CMD_TIMEOUT_SEC, check=False,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    # Read the last non-empty line and take its first token as the verdict. Deliberately
    # forgiving about surrounding text so an existing tool can be pointed at this without
    # being rewritten -- but never guess: anything unrecognised comes back as None, and
    # None always means "conservatively honor whatever the caller was going to do".
    for ln in reversed(out.splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        verdict = ln.split()[-1] if ":" in ln else ln.split()[0]
        return verdict.strip(":").strip() or None
    return None


def cwd_in_project(cwd: str | None) -> bool:
    """Scope narrowing #5: is the job's cwd under this project's root (including .claude/worktrees,
    .worktrees subdirectories).

    PROJECT_ROOT=None (env explicitly set empty) → no project filtering, let everything through
    (backward compat/sandbox).
    No cwd → False (can't confirm it's this project → conservatively don't wake; better to miss a
    wake than mistakenly poke another project's session).
    Uses is_relative_to for true path-boundary determination, not startswith (otherwise 'MyProject'
    would falsely match a sibling-directory prefix like 'MyProject-v3-migration').
    """
    if PROJECT_ROOT is None:
        return True
    if not cwd:
        return False
    try:
        return Path(cwd).resolve().is_relative_to(PROJECT_ROOT)
    except (OSError, ValueError):
        return False


def load_or_init_watermark() -> float | None:
    """Scope narrowing #7: get the "now" watermark's epoch (UTC).

    Return value:
      None  → filtering disabled (env WAKE_WATCHER_WATERMARK == ""), let everything through.
      float → the watermark epoch; a stuck session with updatedAt <= it is treated as an old
              (retroactive) session and skipped.

    Priority:
      1. env WAKE_WATCHER_WATERMARK explicitly given a value:
           ""        → None (filtering disabled)
           ISO8601   → use it as the fixed watermark (doesn't read/write the file; for tests)
      2. Otherwise read watermark.json: reuse it if it has a valid started_watermark (not lost on
         restart).
      3. File doesn't exist/is corrupt → initialize to now, write to disk (first startup).
    """
    if _WATERMARK_ENV_RAW is not None:
        if not _WATERMARK_ENV_RAW.strip():
            return None  # explicitly disabled
        wm = _parse_iso_utc(_WATERMARK_ENV_RAW)
        if wm is not None:
            return wm
        # env was given but couldn't be parsed → fall through to the file logic (don't let a bad env silently turn off protection)
    try:
        data = json.loads(WATERMARK_FILE.read_text(encoding="utf-8"))
        wm = _parse_iso_utc(data.get("started_watermark"))
        if wm is not None:
            return wm
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    # First startup (or corrupt file): record now and write to disk
    now_iso = _now_iso()
    _write_watermark(now_iso, reason="init")
    log(f"watermark initialized = {now_iso} (now-forward starting point; sessions already stuck before this moment are never retroactively woken).")
    return _parse_iso_utc(now_iso)


def _write_watermark(started_iso: str, reason: str) -> None:
    payload = {"started_watermark": started_iso, "set_at": _now_iso(), "reason": reason}
    try:
        tmp = WATERMARK_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WATERMARK_FILE)
    except OSError as e:
        log(f"WARN watermark save failed: {e}")


def reset_watermark() -> None:
    """Reset: delete the watermark file → next startup re-records now."""
    try:
        WATERMARK_FILE.unlink()
        log("watermark reset (deleted watermark.json); next startup will use a new now as the starting point.")
    except FileNotFoundError:
        log("watermark reset: file didn't exist to begin with, nothing to delete.")
    except OSError as e:
        log(f"WARN watermark reset failed: {e}")


# On resume we manage these flags ourselves; even if the job's respawnFlags also has them, dedup overrides with our own.
_OWNED_FLAGS_WITH_VALUE = {"--resume", "-p", "--print", "--output-format"}


def _build_resume_cmd(
    session_id: str, respawn_flags: list[str] | None, claude_path: str
) -> list[str]:
    """Build the resume command, splicing the job's original respawnFlags back in (spec: replicate
    the original execution environment).

    argv[0] = claude_path (the absolute path resolved via three-layer claude binary resolution), no longer a
    bare call to the string 'claude'.

    Why respawnFlags must be included: the respawnFlags in the job's state.json define which model /
    which tools are allowed / permission-mode / MCP restrictions this session should use. For example
    the verification fork (job session-A) is
    `--disallowed-tools Edit Write --model opus --strict-mcp-config`
    — deliberately read-only to prevent self-deception. If you resume bare without respawnFlags, you'd
    turn a deliberately read-only judge session into a writable default session (violating the
    "the player can't also be the referee who changes things" invariant).

    Dedup: we control --resume/-p/--output-format ourselves; if respawnFlags also contains these
    (the ones that take a value), skip them and use ours instead. permission-mode uses the job's own
    (it knows what it should use); only fall back to bypassPermissions if the job didn't specify one.
    """
    base = [claude_path, "--resume", session_id, "-p", WAKE_MESSAGE, "--output-format", "json"]
    extra: list[str] = []
    flags = list(respawn_flags or [])
    has_perm_mode = False
    i = 0
    while i < len(flags):
        f = flags[i]
        if f in _OWNED_FLAGS_WITH_VALUE:
            # skip this flag and its value (we supply our own)
            i += 2
            continue
        if f == "--permission-mode":
            has_perm_mode = True
        extra.append(f)
        i += 1
    if not has_perm_mode:
        extra += ["--permission-mode", "bypassPermissions"]
    return base + extra


# ── liveness discrimination: claude agents --json (owner red line: never resume/fork-session a live session — if you can't tell, don't guess) ──
def _list_active_agents(claude_path: str | None = None) -> list[dict] | None:
    """Query the current list of active agents (without --all — sessions that have already ended
    shouldn't appear here; those are dead orphans and should go through the resume path. Mixing in --all's historical/ended records would misjudge a dead session as "once alive" and misroute it).

    Returns None = query failed/parse failed, i.e. "can't be determined" (the caller must never treat
    None as "dead" and go resume it — that would be betting it's dead while liveness is unknown,
    exactly what the red line above exists to prevent).

    claude_path: the absolute path already resolved via three-layer claude binary resolution; when None this function
    resolves it itself on demand (the dry-run preview path doesn't go through fail-loud — if it can't
    be resolved, treat it as unknown → None). The real rescue routing path has deliver_wake resolve it
    first and pass it through.

    Test seam WAKE_WATCHER_FAKE_AGENTS: directly gives a JSON array string, skipping the real
    `claude agents --json` call (in production this command itself is fast, ~0.2s, but tests need
    determinism + must never read/depend on other real sessions on the machine).
    """
    fake = os.environ.get("WAKE_WATCHER_FAKE_AGENTS")
    if fake is not None:
        try:
            agents = json.loads(fake)
        except json.JSONDecodeError:
            log("WARN WAKE_WATCHER_FAKE_AGENTS is not valid JSON")
            return None
        return agents if isinstance(agents, list) else None
    if claude_path is None:
        claude_path, _reasons = resolve_claude_binary()
        if claude_path is None:
            log("WARN claude agents --json skipped: claude binary three-layer resolution failed entirely (liveness undetermined, treated as unknown)")
            return None
    try:
        out = subprocess.run(
            [claude_path, "agents", "--json"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"WARN claude agents --json query failed (subprocess): {e}")
        return None
    if out.returncode != 0:
        log(f"WARN claude agents --json non-zero exit ({out.returncode}): {out.stderr[:200]!r}")
        return None
    try:
        agents = json.loads(out.stdout)
    except json.JSONDecodeError:
        log("WARN claude agents --json output parsing failed")
        return None
    return agents if isinstance(agents, list) else None


def agent_liveness_lookup(session_id: str, claude_path: str | None = None) -> dict:
    """Classify session_id's current liveness (owner red line: if you can't tell, don't guess — never
    let "uncertain" degrade into "dead").

    Returns {"status": ..., "short_id": str|None, "entry": dict|None}, status is one of four:
      "live_bg"    A live daemon-managed background agent, has a short id, can `claude attach` → the
                   PTY injection path.
      "live_other" Alive but not an injectable background kind (e.g. an interactive session, or a
                   background one that's abnormally missing a short id) → no known safe wake path,
                   defer and don't touch it (it's alive, so never resume either).
      "dead"       Query succeeded and session_id is not in the active list → a dead orphan → the
                   original resume path still applies.
      "unknown"    query/parse failed, cannot determine → defer, recheck next round (never treat as "dead").
    """
    agents = _list_active_agents(claude_path)
    if agents is None:
        return {"status": "unknown", "short_id": None, "entry": None}
    for a in agents:
        if isinstance(a, dict) and a.get("sessionId") == session_id:
            if a.get("kind") == "background" and a.get("id"):
                return {"status": "live_bg", "short_id": a["id"], "entry": a}
            return {"status": "live_other", "short_id": None, "entry": a}
    return {"status": "dead", "short_id": None, "entry": None}


# ── PTY injection implementation ───────────────────────────────
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\r")


def _strip_ansi(raw: bytes) -> str:
    """Strip ANSI escape/cursor-control sequences, keeping only the readable text used to judge whether the input-box prompt has rendered."""
    return _ANSI_RE.sub(b"", raw).decode("utf-8", errors="replace")


def _pty_read_until(fd: int, marker: str, timeout: float) -> tuple[bool, str]:
    """Read from the pty master fd until marker appears (after ANSI-stripping), or until timeout.

    Returns (whether it arrived in time, the last 500 chars of accumulated rendered text —
    on timeout this text is the first-hand evidence for debugging).
    """
    deadline = time.time() + timeout
    buf = b""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            r, _, _ = select.select([fd], [], [], min(0.5, remaining))
        except (OSError, ValueError):
            break
        if fd not in r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if marker in _strip_ansi(buf):
            return True, _strip_ansi(buf)[-500:]
    return False, _strip_ansi(buf)[-500:]


def _pty_drain_for(fd: int, duration: float) -> bytes:
    """Keep draining the pty master fd for `duration` seconds, returning the raw bytes read
    (for debugging/logging use; the caller is not required to consume them).

    Root-cause fix (revealed by a live-fire incident):
    the earlier implementation only read the fd while waiting for the input-box prompt, and never
    read it again after writing the injection sequence. The `claude attach` child process's
    subsequent screen redraws/streamed replies keep writing into the pty, and with nobody draining
    it → the kernel pty output buffer fills up → the child process blocks inside the write()
    syscall and can't get out. This block does not "resolve itself eventually" — measured in
    practice, it doesn't even wake up on SIGKILL (a known Darwin pty gotcha: a write() blocked on
    the write side is only released once the peer master is read empty or closed); the only action
    confirmed to release it is closing the master fd. The child process getting stuck here means
    its own event loop never even reaches the step of "handle the Enter we just sent" — this is
    the real cause of the visible symptom "the message landed in the input box but was never
    submitted," not a bug in the key sequence itself. Use this function in place of a bare
    time.sleep() so draining stays continuous throughout the wait, preventing the buffer from ever
    getting the chance to fill up in the first place.
    """
    deadline = time.time() + duration
    buf = b""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            r, _, _ = select.select([fd], [], [], min(0.2, remaining))
        except (OSError, ValueError):
            break
        if fd not in r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _reap_pty_child(pid: int, fd: int) -> None:
    """Reap the attach-client child process: wait for it to exit on its own (≤5s, continuously
    draining fd throughout — see the root-cause note on _pty_drain_for; without draining, the
    child process can itself get stuck unable to return from write(), and we'd never see it exit).
    On timeout, fall back to SIGKILL; even after SIGKILL, keep draining + wait bounded by
    PTY_REAP_HARD_KILL_GRACE_SEC — never use the old implementation's unbounded blocking
    os.waitpid(pid, 0) (that was the direct cause of "one stuck injection target freezes the
    entire wake-watcher single-threaded scan loop, permanently" — confirmed by live-fire testing:
    the real Darwin pty write-block is immune to SIGKILL, and only closing the master fd can free
    a stuck child process). Even if the child
    still hasn't exited once the hard deadline is hit, just log a WARN, give up waiting, and close
    fd — better to leave behind one orphan process than to freeze the entire rescue service along
    with it. Finally, unconditionally close the master fd (this step must always be reachable, and
    must never get stuck behind anything before it).
    """
    reaped = False
    if pid > 0:
        try:
            deadline = time.time() + PTY_REAP_SOFT_WAIT_SEC
            while time.time() < deadline:
                _pty_drain_for(fd, 0.2)
                wpid, _status = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    reaped = True
                    break
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                kill_deadline = time.time() + PTY_REAP_HARD_KILL_GRACE_SEC
                while time.time() < kill_deadline:
                    _pty_drain_for(fd, 0.2)
                    wpid, _status = os.waitpid(pid, os.WNOHANG)
                    if wpid == pid:
                        reaped = True
                        break
                if not reaped:
                    log(f"WARN pty-attach-inject child pid={pid} still had not exited after SIGKILL within "
                        f"{PTY_REAP_HARD_KILL_GRACE_SEC}s (a known Darwin pty write-blocking "
                        f"pitfall) -- giving up the wait and closing the master fd to release "
                        f"it (may leave an orphan process, which beats freezing the service).")
        except (ChildProcessError, ProcessLookupError, OSError):
            pass
    try:
        os.close(fd)
    except OSError:
        pass


def _verify_transcript_user_turn(transcript_path: str, message: str, timeout: float,
                                 baseline_len: int = 0) -> bool:
    """Poll the tail of the transcript to confirm a main-conversation user turn containing the
    message's signature fragment was added after baseline_len.

    Why baseline_len must be enforced (don't just check "is there a matching line" — that gives
    false positives): `WAKE_MESSAGE` is fixed copy, so the same sentence has likely already landed
    for real in an earlier historical wake cycle. Without this boundary, as soon as this sentence
    appears anywhere in the transcript, this verification would immediately judge "success," even
    if this round's actual injection sequence typed in nothing at all — mistaking "that success
    long ago" for "this one succeeded too." So only lines added after baseline_len (the record
    count the caller read *before* starting the injection action) count.

    Measured in practice: user turns have
    write-to-disk latency (>8s observed), so give the polling window enough timeout — don't judge
    failure just because the first read comes up empty. The signature fragment uses a first-24-
    character substring match (not requiring exact character-for-character equality — the CLI
    rendering/attach layer may normalize whitespace line-wrapping).
    """
    needle = message.strip()[:24]
    if not needle:
        return True  # nothing to verify for an empty message (defensive; shouldn't happen in theory)
    deadline = time.time() + timeout
    while True:
        recs = _load_transcript_records(transcript_path)
        for idx in range(len(recs) - 1, baseline_len - 1, -1):
            rec = recs[idx]
            if _is_main_user(rec) and needle in _message_text(rec):
                return True
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


def _pty_attach_inject_impl(short_id: str, message: str,
                           session_id: str | None, transcript_path: str | None) -> bool:
    """Inject message as keyboard input into the terminal of a live daemon-bg session (mechanism
    described at the top of the module):
    `pty.fork()` builds its own PTY (works even with no controlling terminal — the launchd daemon
    environment has no tty, which is exactly the point of building our own PTY) → child process
    execs `claude attach <short_id>` → wait for the input box (❯) to render → Ctrl+U to clear the
    input line (prevents accumulating old, uncommitted draft text) → write message → Enter to
    submit → brief wait → Ctrl+Z to detach (the session keeps running, unaffected) → reap the
    child process.
    """
    # Baseline for delivery verification: must snapshot the transcript length before acting,
    # otherwise verification can't distinguish "a genuinely new user turn from this round" from
    # "the same fixed copy some long-ago wake already wrote" (WAKE_MESSAGE is a constant, so the
    # same sentence may well have appeared more than once in history).
    baseline_len = len(_load_transcript_records(transcript_path)) if transcript_path else 0

    # execvp itself does a PATH lookup; the whole point of resolving the path ourselves
    # is to no longer depend on an implicit PATH — resolve the absolute path before
    # forking, and exec precisely with execv (not execvp). If resolution fails → don't fork, go
    # fail-loud/defer instead (all three tiers failing means:
    # dedupe the alert + defer this round's action).
    claude_path = ensure_claude_or_fail_loud()
    if claude_path is None:
        log(f"WAKE-FAIL pty-attach-inject short_id={short_id} session={session_id} — "
            f"all three layers of claude binary resolution failed; deferring without injecting.")
        return False

    try:
        pid, fd = pty.fork()
    except OSError as e:
        log(f"WARN pty.fork failed short_id={short_id}: {e}")
        return False

    if pid == 0:
        # Child process: exec claude attach <short_id> (absolute-path argv[0]). If execv fails,
        # exit — don't let two interpreters end up running stacked on top of each other.
        try:
            os.execv(claude_path, [claude_path, "attach", short_id])
        except OSError:
            os._exit(127)
        os._exit(126)  # unreachable, defensive

    # ── Parent process ──
    try:
        ready, tail = _pty_read_until(fd, PTY_PROMPT_MARKER, PTY_READY_TIMEOUT_SEC)
        if not ready:
            log(f"WAKE-FAIL pty-attach-inject short_id={short_id} session={session_id} — "
                f"timed out waiting for the input box (marker={PTY_PROMPT_MARKER!r}) after "
                f"{PTY_READY_TIMEOUT_SEC}s, tail of render: {tail!r}")
            return False
        # Between each of the following steps, use _pty_drain_for() instead of a bare
        # time.sleep() — draining must stay continuous, otherwise the `claude attach` child
        # process's own screen redraws/streamed replies will fill up the pty kernel buffer,
        # blocking inside write() with no way out, so its own event loop never even reaches the
        # step of "handle the keystroke we just sent" (root-cause explanation in the
        # _pty_drain_for docstring; live-fire testing confirmed this is the real cause of "the
        # message landed in the input box but was never submitted," not a bug in the key sequence
        # itself).
        os.write(fd, b"\x15")           # Ctrl+U: clear the input line (prevents accumulating old text)
        _pty_drain_for(fd, 0.3)
        os.write(fd, message.encode("utf-8"))
        _pty_drain_for(fd, 0.3)
        os.write(fd, b"\r")             # Enter to submit
        # The rendering drained right after Enter is the most direct first-hand evidence for
        # diagnosing whether the submit actually took effect (for debugging only, not part of the
        # verdict — the verdict still only recognizes a user turn that actually landed in the
        # transcript, see _verify_transcript_user_turn below).
        post_enter_tail = _strip_ansi(_pty_drain_for(fd, 1.0))[-500:]
        os.write(fd, b"\x1a")           # Ctrl+Z: detach (the session keeps running, unaffected)
    except OSError as e:
        log(f"WARN pty-attach-inject injection sequence raised short_id={short_id} session={session_id}: {e}")
        return False
    finally:
        _reap_pty_child(pid, fd)

    if not transcript_path:
        log(f"WARN pty-attach-inject short_id={short_id} session={session_id} has no transcript_path "
            f"to verify delivery -- conservatively judged a failure (\"it did not raise, so it "
            f"worked\" is exactly what was wrong with the old resume implementation).")
        return False

    landed = _verify_transcript_user_turn(transcript_path, message, PTY_DELIVER_VERIFY_TIMEOUT_SEC,
                                          baseline_len=baseline_len)
    if not landed:
        log(f"WAKE-FAIL pty-attach-inject short_id={short_id} session={session_id} -- injection "
            f"sequence ran, but transcript verification waited {PTY_DELIVER_VERIFY_TIMEOUT_SEC}s "
            f"without seeing a user turn land. Tail of render after Enter: {post_enter_tail!r}")
        return False
    return True


def _pty_attach_inject(short_id: str, message: str,
                      session_id: str | None = None,
                      transcript_path: str | None = None) -> bool:
    """Defensive shell around `_pty_attach_inject_impl`: no unforeseen exception may crash the
    main loop — always log + return False.

    The signature has two more optional keyword args than the original task description
    (session_id/transcript_path) — delivery verification (part of what the owner required) needs
    to know which session's which transcript to read; without these two args there'd be no way to
    verify inside this function.
    """
    try:
        return _pty_attach_inject_impl(short_id, message, session_id, transcript_path)
    except Exception as e:  # noqa: BLE001 -- deliberate: see below
        # Catching bare Exception is intentional here and should stay. This is the highest
        # blast-radius primitive in the codebase (it types into a session that is still
        # running), and it touches ptys, fork, and terminal rendering -- surfaces that raise
        # things no caller can usefully enumerate. A watchdog whose whole job is to survive
        # other people's failures must not itself die on an unexpected one: log it loudly and
        # report failure, so the caller falls back to "leave the session alone".
        log(f"WARN pty-attach-inject unexpected exception short_id={short_id} session={session_id}: {e!r}")
        return False


def deliver_wake(session_id: str, cwd: str | None, dry_run: bool,
                 respawn_flags: list[str] | None = None,
                 transcript_path: str | None = None) -> tuple[bool, str]:
    """Wake: forks into two paths by dead-vs-alive (root-caused by a prior incident finding; the
    resulting red line: a live session must never get --resume/--fork-session).

      "live_bg"    a live daemon-bg agent → inject via `claude attach` over PTY (_pty_attach_inject).
      "dead"       a dead orphan (process already exited / not in the active list) → `claude --resume` (the original design).
      "live_other" / "unknown"  alive but with no known safe path / liveness can't be determined →
                   defer, don't touch it, don't guess.

    Returns (ok, info).

    Test seams:
      WAKE_WATCHER_FAKE_DELIVER=1: skip every real-world action (liveness query/PTY/spawn all
        skipped), judge "delivered" immediately (the test itself manually simulates a retry
        appending to the transcript, without depending on a real claude call).
      WAKE_WATCHER_FAKE_AGENTS: see agent_liveness_lookup()/`_list_active_agents`.
    """
    if dry_run:
        liveness = agent_liveness_lookup(session_id)
        if liveness["status"] == "live_bg" and not ENABLE_PTY_INJECT:
            return True, (f"DRY-RUN would DEFER (live background agent; PTY injection disabled, "
                          f"set WAKE_WATCHER_ENABLE_PTY_INJECT=1 to allow it), session={session_id}")
        if liveness["status"] == "live_bg":
            return True, (f"DRY-RUN would pty-attach-inject short_id={liveness['short_id']} "
                          f"(live daemon-bg agent, session={session_id})")
        if liveness["status"] == "live_other":
            return True, f"DRY-RUN would DEFER (alive but not an injectable background kind, session={session_id})"
        if liveness["status"] == "unknown":
            return True, f"DRY-RUN would DEFER (liveness query failed, never guess liveness, session={session_id})"
        flagstr = " ".join(respawn_flags or []) or "(none)"
        return True, f"DRY-RUN would resume {session_id} with respawnFlags=[{flagstr}] (cwd={cwd})"
    if os.environ.get("WAKE_WATCHER_FAKE_DELIVER") == "1":
        return True, f"FAKE-DELIVER {session_id} (test hook, no real spawn)"

    # Before any rescue action
    # (liveness query/resume/attach), resolve through all three tiers first. If all three tiers
    # fail → dedupe the alert + defer_only this round, never act blindly (load-bearing safety
    # invariant: with no executable claude available, any "action" at all is acting blind).
    claude_path = ensure_claude_or_fail_loud()
    if claude_path is None:
        return False, (f"DEFER session={session_id} -- all three layers of claude binary resolution "
                       f"failed (fail-loud already alerted); no rescue action this round "
                       f"(defer_only: never resume/attach/respawn/kill)")

    liveness = agent_liveness_lookup(session_id, claude_path)

    if liveness["status"] == "unknown":
        return False, (f"DEFER session={session_id} -- liveness query failed / cannot determine; "
                       f"never guess liveness (red line: never resume or fork-session a live "
                       f"session), leaving it for the next round")

    if liveness["status"] == "live_other":
        return False, (f"DEFER session={session_id} -- alive but not an injectable daemon-bg agent "
                       f"(entry={liveness['entry']}), no known safe wake path, leaving it alone")

    if liveness["status"] == "live_bg" and not ENABLE_PTY_INJECT:
        return False, (f"DEFER session={session_id} — live background agent, but PTY injection is "
                       f"disabled by default. Set WAKE_WATCHER_ENABLE_PTY_INJECT=1 to allow typing "
                       f"into a running session (see THREAT-MODEL.md).")
    if liveness["status"] == "live_bg":
        short_id = liveness["short_id"]
        ok = _pty_attach_inject(short_id, WAKE_MESSAGE, session_id=session_id,
                                transcript_path=transcript_path)
        if ok:
            return True, f"pty-attach-inject delivered+verified short_id={short_id} (live bg agent)"
        return False, (f"pty-attach-inject FAILED/UNVERIFIED short_id={short_id} session={session_id} "
                       f"(live bg agent) -- no fallback to --resume (the CLI rejects resume on a "
                       f"live session, and the red line forbids fork-session), leaving it for the "
                       f"next round")

    # liveness["status"] == "dead": a dead orphan, take the original resume path
    cmd = _build_resume_cmd(session_id, respawn_flags, claude_path)
    try:
        # detached + background: waking must not block the poll loop; let claude run the resume
        # to completion on its own.
        proc = subprocess.Popen(
            cmd,
            cwd=cwd if cwd and Path(cwd).is_dir() else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, f"spawned wake pid={proc.pid} (dead orphan, resume path)"
    except (subprocess.SubprocessError, OSError, FileNotFoundError) as e:
        return False, f"wake spawn failed: {e}"


def _find_transcript_path(session_id: str) -> str | None:
    """Glob to the transcript file by sessionId (HOME/projects/<encoded cwd>/<sessionId>.jsonl).

    Deliberately does NOT replicate Claude Code's own "cwd → directory name" encoding rule: that's
    its internal implementation detail (slashes/dots/non-ASCII are each handled differently, and
    it has changed across versions); replicating it would just be burying ourselves a trap where
    "upstream changes it once and we silently mismatch, and the mismatch shows up as 'nothing
    happened at all'" — exactly the class of illness this module exists to cure. sessionId itself
    is globally unique, so a single-level directory glob is enough; no dependency on any encoding
    assumption needed.

    Multiple hits (rare: the same session's directory got moved/rebuilt) take the one with the
    newest mtime. stat each one individually rather than inside sorted(key=...): ~/.claude/projects
    is a live directory Claude Code is actively writing to, and if a hit gets moved away mid-sort,
    only that one hit should be dropped — the whole lookup should not degrade into "found nothing
    at all."
    Not found → None (fail-closed: no evidence, no guess; the caller skips this candidate).
    """
    try:
        hits = list((HOME / "projects").glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    scored: list[tuple[float, str]] = []
    for p in hits:
        try:
            scored.append((p.stat().st_mtime, str(p)))
        except OSError:
            continue  # this hit just got moved/deleted → skip only it, the remaining surviving hits still participate
    return max(scored)[1] if scored else None


def _interactive_candidates() -> list[dict]:
    """Second candidate source: entries in `claude agents --json` with kind=="interactive" —
    interactive main sessions.

    Mutually exclusive with the JOBS_DIR candidate source: this only lets kind=="interactive"
    through; not a single kind=="background" entry ever comes out of this function — the latter
    already has ~/.claude/jobs/<id>/state.json and is handled by the loop above; having both
    sides process the same session would double-count budget/backoff.

    Scope-narrowing #5 (only handle this project) applies equally on this branch, reusing the same
    cwd_in_project (not replicating a second copy of the criterion). The filtering is silent:
    interactive sessions from other projects are the normal case, not an anomaly — logging each
    one individually would just flood the log with other people's project paths.

    Query failed / no interactive sessions → empty list (the caller has no interactive candidates
    this round, and doesn't guess).
    """
    agents = _list_active_agents()
    if not agents:
        return []
    out: list[dict] = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        if a.get("kind") != "interactive":
            continue
        if not a.get("sessionId"):
            continue
        if not cwd_in_project(a.get("cwd")):
            continue
        out.append(a)
    return out


def scan_once(dry_run: bool = False, watermark: float | None = None) -> int:
    """One poll pass. Returns number of wakes delivered this pass.

    Two candidate sources, mutually exclusive:
      ① JOBS_DIR (~/.claude/jobs/<id>/state.json) — the lifecycle file for a bg/daemon job.
      ② `claude agents --json` entries with kind=="interactive" — interactive main sessions a
         human runs directly in a terminal; these structurally never appear in ① (see the note
         below + docs/).
    """
    global _LAST_NET_STATE

    ledger = load_ledger()
    do_not_wake = load_do_not_wake()  # Scope-narrowing #6: reread every round; adding an id while running takes effect immediately
    woke = 0
    now = time.time()

    # Spec #5: defer when offline — probe once per round, cache it for all candidates this round.
    # Only log when it flips (avoid flooding the log).
    net_ok = network_reachable()
    if _LAST_NET_STATE is None or _LAST_NET_STATE != net_ok:
        if net_ok:
            log("NET reachable again (api.anthropic.com) -- resuming normal wake behaviour.")
        else:
            log("NET unreachable (api.anthropic.com) -- deferring every candidate from this round on; it does not consume the retry budget and resumes automatically once the network returns.")
        _LAST_NET_STATE = net_ok

    # A missing JOBS_DIR should only leave the FIRST candidate source empty — the whole round must
    # never return 0: interactive main sessions are exactly the kind of thing that's "never in
    # JOBS_DIR" in the first place; making ②'s reachability depend on ①'s directory existing would
    # weld a new gap right back in.
    if JOBS_DIR.is_dir():
        job_dirs = sorted(JOBS_DIR.iterdir())
    else:
        log(f"jobs dir not found: {JOBS_DIR}")
        job_dirs = []

    for job_dir in job_dirs:
        if not job_dir.is_dir():
            continue
        st = read_job_state(job_dir)
        if not st:
            continue

        state = st.get("state")
        detail = st.get("detail")
        session_id = st.get("sessionId") or st.get("resumeSessionId")
        cwd = st.get("cwd") or st.get("originCwd")
        respawn_flags = st.get("respawnFlags")
        transcript_path = st.get("linkScanPath") or st.get("transcriptPath")

        if not session_id:
            continue

        # ── Scope-narrowing (owner 2026-06-22): two skip checks before any wake determination ──
        # #6 do-not-wake: a session that was deliberately stopped must never be woken (highest
        # priority, before anything else).
        #   liveness-aware (vitality-first, same root cause as the placement finding): do-not-wake is
        #   a one-time human judgment call; a session mistagged as orphan that is actually alive should
        #   not be permanently and silently vetoed by it. Before honoring the tag, recheck against the
        #   real vitality signal — verdict alive_working ⟹ tag contradicts reality ⟹ don't silently
        #   veto forever, surface a warning instead so a human can review and remove the tag (still no
        #   wake: waking a live session requires dual drive, and SAFETY_REQUIRE_DEAD_PROCESS blocks it
        #   anyway). Not alive_working (naturally true for a deliberately stopped session) ⟹ honor as
        #   usual.
        if session_id in do_not_wake:
            rec = ledger.setdefault(session_id, {})
            if vitality_verdict(session_id) == "alive_working":
                if rec.get("logged_skip_for") != "do_not_wake_but_alive":
                    log(
                        f"WARN session={session_id} in do-not-wake but vitality=alive_working "
                        f"— one-time orphan tag contradicts real vitality; not waking (session alive), but the tag should be reviewed and removed by a human "
                        f"(vitality-first, same root as the placement finding)."
                    )
                    rec["logged_skip_for"] = "do_not_wake_but_alive"
                continue
            if rec.get("logged_skip_for") != "do_not_wake":
                log(f"SKIP session={session_id} in do-not-wake list (deliberately stopped / not alive, never auto-wake).")
                rec["logged_skip_for"] = "do_not_wake"
            continue
        # #5 only this project's business: cwd not under this project's root → another project's session, don't wake it.
        if not cwd_in_project(cwd):
            rec = ledger.setdefault(session_id, {})
            if rec.get("logged_skip_for") != "foreign_project":
                log(
                    f"SKIP session={session_id} cwd={cwd!r} not under this project ({PROJECT_ROOT}) "
                    f"— session belongs to another project, don't wake it."
                )
                rec["logged_skip_for"] = "foreign_project"
            continue

        rec = ledger.get(session_id)

        # only blocked/failed states are candidates
        if state not in ("blocked", "failed"):
            if rec:
                rec["last_state"] = state
            continue

        # ── scope narrowing #7 (owner 2026-06-22): now-forward watermark ──
        #   Only handle sessions that entered the stuck state after the watcher started. For a stuck
        #   session, updatedAt is the moment it got stuck; updatedAt <= watermark → this is an ancient
        #   session (already stuck before the watcher started watching) → don't wake it retroactively.
        #   Missing/unparsable updatedAt → fail-closed and skip (better to miss a wake than to wrongly
        #   wake an old session whose death time we don't even know).
        #   watermark is None → filter disabled (let everything through).
        if watermark is not None:
            upd = _parse_iso_utc(st.get("updatedAt"))
            if upd is None or upd <= watermark:
                rec = ledger.setdefault(session_id, {})
                if rec.get("logged_skip_for") != "pre_watermark":
                    when = st.get("updatedAt") or "(no updatedAt)"
                    log(
                        f"SKIP session={session_id} updatedAt={when} ≤ watermark "
                        f"— this is an old session that was already stuck before the watcher started, not waking it retroactively."
                    )
                    rec["logged_skip_for"] = "pre_watermark"
                continue

        rec = ledger.setdefault(session_id, {})
        decision = transcript_wake_decision(st)
        rec["last_state"] = state
        rec["last_detail"] = (detail or "")[:200]

        # ── dual-signal recheck of an already-delivered rescue
        #   (truly rescued = signal A ∧ signal B within the window) ──
        #   Last round actually delivered a rescue (rescue_ref_len was recorded = the pre-delivery
        #   baseline) → this round reads the transcript and judges one of three states:
        #     rescued   → leave a trace (truly rescued), clear the reference.
        #     over_window (signal A was delivered but the window expired with no signal B) → fall back
        #                 to the alert path (surface RESCUE-DELIVERED-ONLY, dedup'd once), clear the
        #                 reference. This is exactly what makes good on "signal A alone never counts as
        #                 rescued" (the old 2026-06-26 silent false-success incident).
        #     pending/delivered-still within the window → keep the reference, judge again next round.
        #   Purely additive — doesn't change the existing escape-progress judgment or backoff cadence
        #   (the concept's behavior differs by exactly two lines, zero other changes).
        if rec.get("rescue_ref_len") is not None and transcript_path:
            dual = evaluate_dual_signal(
                transcript_path, int(rec["rescue_ref_len"]), RESCUE_CONTINUE_WINDOW_SEC, now
            )
            if dual["state"] == "rescued":
                log(f"RESCUED session={session_id} dual signal confirmed (signal A delivered ∧ signal B continued within window) — truly rescued.")
                rec.pop("rescue_ref_len", None)
                rec.pop("rescue_ref_ts", None)
                rec.pop("delivered_only_surfaced", None)
            elif dual["over_window"]:
                if not rec.get("delivered_only_surfaced"):
                    surface_delivered_only(session_id, RESCUE_CONTINUE_WINDOW_SEC)
                    rec["delivered_only_surfaced"] = True
                rec.pop("rescue_ref_len", None)  # fall back to the existing retry/alert path
                rec.pop("rescue_ref_ts", None)

        # The one and only "real progress" signal: the AI's last main-conversation turn is normal
        # output (it has escaped the transient error) → reset the budget.
        # Never treat a growing transcript file as progress (root cause of the 2026-07-01 night OOM
        # incident: the watcher's own resume appends to the file, so it necessarily grows, and this
        # wrong signal would reset the budget forever).
        if decision["escaped"] and (rec.get("wakes") or rec.get("surfaced_needs_human")):
            log(f"ESCAPE session={session_id} AI has escaped (last main-conversation turn is normal output) — fast-lane budget reset.")
        if decision["escaped"]:
            rec["wakes"] = 0
            rec["surfaced_needs_human"] = False
            rec.pop("next_eligible_at", None)
            # Escaped = the session has continued: clear the rescue dual-signal reference (start the
            # new round from a clean state, to keep the "rescued" tag from sticking across incidents).
            rec.pop("rescue_ref_len", None)
            rec.pop("rescue_ref_ts", None)
            rec.pop("delivered_only_surfaced", None)

        if decision["action"] != "send":
            skip_key = f"{state}:{decision['reason']}"
            if rec.get("logged_skip_for") != skip_key:
                log(f"SKIP session={session_id} state={state} {decision['reason']} — not waking.")
                rec["logged_skip_for"] = skip_key
            # Not waking for a non-transient auth issue (Login expired, etc.) is correct, but staying
            # silent means the owner never finds out. Surface it once (the surfaced flag resets on
            # escape → can surface again for a new incident).
            if (
                "non-transient" in decision["reason"]
                and _is_auth_blocked_detail(detail)
                and not rec.get("surfaced_needs_human")
            ):
                surface_auth_blocked(session_id, detail or "")
                rec["surfaced_needs_human"] = True
            continue

        rec.pop("logged_skip_for", None)  # became eligible, don't carry over a stale skip key

        wakes = rec.get("wakes", 0)

        # #2 fast-lane budget hit the ceiling → surface NEEDS-HUMAN once (visibility, not silence),
        # but NOT a permanent give-up — this round only surfaces the alert and starts the slow-lane
        # timer from this moment; it does not send in the same round (the moment of hitting the
        # ceiling and "the next slow-lane wake" are kept separate, clean semantics: exactly MAX_WAKES
        # fast wakes, then one every SLOW_RETRY_SEC after that, resuming automatically once the network
        # recovers; definitely not the pre-ceiling 60s fast loop — that was the root cause of the
        # 2026-07-01 night OOM).
        if wakes >= MAX_WAKES and not rec.get("surfaced_needs_human"):
            surface_needs_human(session_id, detail or "", wakes)
            rec["surfaced_needs_human"] = True
            rec["next_eligible_at"] = now + SLOW_RETRY_SEC
            continue  # the moment it hits the ceiling only surfaces the alert, doesn't send this round; slow lane starts counting from now

        # Rule #2: backoff — enough time must have passed since the last wake
        next_eligible = rec.get("next_eligible_at", 0)
        if now < next_eligible:
            continue

        # Safety valve: don't contend with a live process
        if SAFETY_REQUIRE_DEAD_PROCESS and session_has_live_process(session_id):
            log(
                f"DEFER session={session_id} a live process holds the session, not waking for now (avoid contending with the daemon)."
            )
            continue

        # idempotency: only wake while still blocked/failed (re-read once to confirm it hasn't changed)
        st2 = read_job_state(job_dir)
        if not st2 or st2.get("state") not in ("blocked", "failed"):
            continue

        # Rule #5: offline defer — a machine-level network outage is not a session-level failure; it doesn't consume budget or push back the backoff.
        if not net_ok:
            continue

        # Resource-aware gate (last line of defense): DEFER when the machine is overloaded, don't pile
        # another cold-loading process onto an already-full machine. Don't reset wakes/backoff — the
        # next poll after the machine frees up will resume normally, preserving network-recovery resilience.
        pressure, why = system_under_pressure()
        if pressure:
            log(f"DEFER session={session_id} machine overloaded ({why}) — not waking for now, waiting for resources to free up (avoid OOM pile-up)")
            continue

        # Completion-check double confirmation (owner's accuracy-first principle): the transcript-tail
        # decision rule says "send", but vitality is an independent, real-signal recheck (it reads the
        # canonical/terminal-state artifact, not just the transcript tail) —— both sources have to judge
        # "not done" before we act. Even if the transcript-tail decision rule hasn't caught up with the
        # real completion state due to some race, this insurance still blocks a false wake of a session
        # that has actually already finished (the cost is "fewer wakes"; the owner explicitly wants
        # conservatism in this direction, not the other way around).
        if vitality_verdict(session_id) == "done":
            skip_key = "vitality_done"
            if rec.get("logged_skip_for") != skip_key:
                log(f"SKIP session={session_id} vitality verdict=done (finished) — leaving it alone, not waking (completion-check double confirmation).")
                rec["logged_skip_for"] = skip_key
            continue

        # Snapshot the transcript baseline right before delivery,
        # record it as rescue_ref_len on success —— next round's dual-signal recheck uses it as the
        # "baseline before signal A" cutoff (to identify the genuinely new user turn / continuation
        # this injection produced).
        pre_deliver_len = len(_load_transcript_records(transcript_path)) if transcript_path else 0
        ok, info = deliver_wake(session_id, cwd, dry_run, respawn_flags, transcript_path)
        if ok:
            rec["wakes"] = wakes + 1
            if rec["wakes"] <= MAX_WAKES:
                lane = "fast"
                backoff = BACKOFF_BASE_SEC * (2 ** wakes)  # wakes = count before this wake: 60,120,240
            else:
                lane = "slow"
                backoff = SLOW_RETRY_SEC
            rec["next_eligible_at"] = now + backoff
            rec["last_wake_at"] = _now_iso()
            if not dry_run:  # dry-run doesn't actually deliver, so don't set up the rescue dual-signal reference
                rec["rescue_ref_len"] = pre_deliver_len
                rec["rescue_ref_ts"] = now
                rec.pop("delivered_only_surfaced", None)
            woke += 1
            log(
                f"WAKE session={session_id} attempt #{rec['wakes']}/{MAX_WAKES} lane={lane} "
                f"({decision['reason']}) next_backoff={backoff}s — {info}"
            )
        else:
            log(f"WAKE-FAIL session={session_id} — {info}")

    # ══ candidate source ②: interactive main sessions (kind=="interactive") ═══════════════
    # Root cause: the loop above only draws candidates from JOBS_DIR, and state.json there is a
    # lifecycle file Claude Code only creates for bg/daemon jobs —— an interactive main session someone
    # is running directly in a terminal STRUCTURALLY never shows up there. So when it hits the session
    # limit and sits stuck all night, the watcher process is alive, its heartbeat is fresh, it dutifully
    # spins idle, and there's zero logging the whole time: it's not a misjudgment, it was never a
    # candidate to begin with. This block fills that gap.
    #
    # ⚠️ Honest boundary —— what this branch buys you is VISIBILITY, not an automatic rescue; don't let
    #   users expect otherwise:
    #   `claude attach` is the only external wake primitive, and it officially only supports background
    #   sessions; and in real `claude agents --json` output, entries with kind=="interactive"
    #   STRUCTURALLY have no short id. So an interactive candidate always lands on "live_other" in
    #   agent_liveness_lookup() → deliver_wake() DEFERs: never PTY-inject, never --resume/--fork-session
    #   against a live session. What it CAN do is "time it correctly (recognize the session-limit
    #   moment, reusing the same transcript_wake_decision as the bg branch) + say so visibly once that
    #   moment arrives" (log + one NEEDS-HUMAN surface). Actually getting the session to keep running
    #   past that point still requires a human to take one action in that terminal. This is the current
    #   CLI's capability boundary, not "not finished yet" —— before this, the behavior was total
    #   silence; going from silent to visible is this branch's entire value.
    #
    # ⚠️ The field schema forks between the two kinds (verified against the real CLI on 2026-08-18):
    #     background: has `id`  + `state`      interactive: has `pid` + `status`, no `id`
    #   Any code that only reads `state` will silently get None for an interactive entry (= yet another
    #   "dutifully spinning idle"), so status reads below always check both the status and state fields.
    #
    # Escape hatch: WAKE_WATCHER_COVER_INTERACTIVE=0 → skip this whole block, fall back completely to
    # the old jobs-only behavior (if this new branch ever misbehaves, no need to roll back the whole
    # codebase; turning it off means `claude agents --json` isn't even called).
    #
    # The watermark (#7) doesn't apply to this branch: interactive entries have no state.json updatedAt
    # to compare against, and this branch's normal output is "surface one alert" rather than a wake
    # action, so there's no risk of "retroactively waking an ancient session" (surfacing also comes with
    # one-time dedup), so we don't introduce a second watermark semantics based on some other timestamp
    # field.
    if os.environ.get("WAKE_WATCHER_COVER_INTERACTIVE", "1") != "0":
        for cand in _interactive_candidates():
            session_id = cand.get("sessionId")
            cwd = cand.get("cwd")
            status = cand.get("status") or cand.get("state") or "?"  # schema forks: read both
            if not session_id or session_id in do_not_wake:
                continue

            rec = ledger.setdefault(session_id, {})
            transcript_path = _find_transcript_path(session_id)
            if not transcript_path:
                if rec.get("logged_skip_for") != "interactive_no_transcript":
                    log(f"SKIP session={session_id} (interactive, status={status}) "
                        f"transcript not found — no decision rule, not guessing (fail-closed).")
                    rec["logged_skip_for"] = "interactive_no_transcript"
                continue

            decision = transcript_wake_decision({"linkScanPath": transcript_path})
            if decision["escaped"]:
                # The session escaped the error on its own and kept running → clear the one-time flag so it can surface again for the next incident.
                rec.pop("interactive_surfaced", None)
                rec.pop("interactive_next_eligible_at", None)
            if decision["action"] != "send":
                skip_key = f"interactive:{decision['reason']}"
                if rec.get("logged_skip_for") != skip_key:
                    log(f"SKIP session={session_id} (interactive, cwd={cwd}, status={status}) "
                        f"{decision['reason']} — not waking.")
                    rec["logged_skip_for"] = skip_key
                continue
            rec.pop("logged_skip_for", None)

            # Backoff window: inside the window we neither re-evaluate nor spam the log again; outside
            # the window we still periodically re-evaluate (so that once "the human closes the terminal
            # and the session becomes a truly dead orphan," the next round can be picked up by the
            # existing resume path below).
            if now < rec.get("interactive_next_eligible_at", 0):
                continue
            if vitality_verdict(session_id) == "done":
                if rec.get("logged_skip_for") != "interactive_vitality_done":
                    log(f"SKIP session={session_id} (interactive) vitality verdict=done (finished) "
                        f"— leaving it alone (completion-check double confirmation).")
                    rec["logged_skip_for"] = "interactive_vitality_done"
                continue

            # ── how SAFETY_REQUIRE_DEAD_PROCESS is honored on this branch ──────────────────
            # Semantics unchanged (never act while the process is still alive), but the decision rule
            # switches to pid: an interactive entry carries its own pid, whereas the old
            # session_has_live_process() greps `ps ax` output for the sessionId text —— an interactive
            # process's command line necessarily contains the sessionId, so it always matches, making
            # the check trivially true in this context, and its log wording ("avoid contending with the
            # daemon") is simply wrong for this scenario.
            # What this gate actually guards against is a false send: in the instant between candidate
            # enumeration and delivery, if `claude agents --json` happens to omit it, deliver_wake()
            # would judge it "dead" and go --resume a session that's actually alive —— exactly the
            # red line this guards against: two instances must never drive one session at
            # once.
            # Can't probe it (permissions, etc.) → conservatively treat it as "alive," fail-closed in
            # the same direction as the old gate.
            pid = cand.get("pid")
            pid_alive = False
            if isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, 0)
                    pid_alive = True
                except ProcessLookupError:
                    pid_alive = False
                except OSError:
                    pid_alive = True

            # The network/overload gates only block the "take action" half —— they shouldn't block
            # surfacing the alert: when offline/overloaded, a human needs even more to know a session is
            # stuck there, and writing one log line doesn't need the network.
            pressure, why = system_under_pressure()
            if SAFETY_REQUIRE_DEAD_PROCESS and pid_alive:
                ok, info = False, (
                    f"interactive session process still alive (pid={pid}, status={status}) — REQUIRE_DEAD safety valve: "
                    f"never resume/fork-session against a live session; and there's no known safe wake path (can only alert)"
                )
            elif not net_ok:
                ok, info = False, "machine-level network outage (api.anthropic.com unreachable) — no wake action this round"
            elif pressure:
                ok, info = False, f"machine overloaded ({why}) — no wake action this round (avoid OOM pile-up)"
            else:
                # Reuses the very same deliver_wake: alive → live_other → DEFER (the normal case); if
                # the process has actually exited (the human closed the terminal) and it's no longer on
                # the live list → "dead" → takes the existing, already-verified-safe dead-orphan resume path.
                ok, info = deliver_wake(session_id, cwd, dry_run, None, transcript_path)

            if ok and dry_run:
                log(f"DRY-RUN session={session_id} (interactive) — {info}")
                rec["interactive_next_eligible_at"] = now + 300.0
                continue
            if ok:
                wakes = rec.get("wakes", 0)
                rec["wakes"] = wakes + 1
                backoff = (BACKOFF_BASE_SEC * (2 ** wakes)
                           if rec["wakes"] <= MAX_WAKES else SLOW_RETRY_SEC)
                rec["interactive_next_eligible_at"] = now + backoff
                rec["last_wake_at"] = _now_iso()
                woke += 1
                log(f"WAKE session={session_id} (interactive process exited → dead-orphan resume path) "
                    f"attempt #{rec['wakes']}/{MAX_WAKES} ({decision['reason']}) "
                    f"next_backoff={backoff}s — {info}")
                continue

            # Normal case: the moment has arrived, but there's no safe automatic path → must DEFER
            # VISIBLY; a silent DEFER is not allowed.
            log(f"DEFER session={session_id} (interactive) — {info}; "
                f"claude attach only supports background sessions, an interactive main session can only be alerted, not auto-resumed.")
            if not rec.get("interactive_surfaced"):
                # Surface once (only one line per incident; the flag clears once the session escapes
                # the error on its own, so it can surface again next time).
                # This line deliberately shows the session id only once and carries no transcript path
                # —— it's a reminder meant for a human to read, not a record meant for a machine to parse.
                line = (
                    f"[{_now_iso()}] NEEDS-HUMAN session={session_id} (interactive main session) "
                    f"has reached the point where it needs to resume, but automatic wake isn't feasible for interactive sessions (claude attach only supports "
                    f"background sessions) — a human needs to manually resume it in that terminal."
                    f" criterion={decision['reason']}"
                )
                for f_path in (NEEDS_HUMAN_FILE, LOG_FILE):
                    try:
                        with f_path.open("a", encoding="utf-8") as f:
                            f.write(line + "\n")
                    except OSError:
                        pass
                print(line, file=sys.stderr, flush=True)
                rec["interactive_surfaced"] = True
            rec["interactive_next_eligible_at"] = now + 300.0

    save_ledger(ledger)
    return woke


# ══════════════════════════════════════════════════════════════════════════════════════
# Disconnected-session auto-recovery daemon
#   (supersedes the earlier transient-resume mechanism)
# ══════════════════════════════════════════════════════════════════════════════════════
# This is the next-generation implementation on the "wake-watcher side": it only handles the
# stuck_waiting state among the external tool vitality's five verdict states (process still alive, stuck on a
# transient error with no follow-up retry), injecting a continuation via claude attach/PTY every 10 minutes,

RECOVERY_DAEMON_NAME = "disconnected-session-recovery-daemon"

# The 6 canonical event kinds the recovery daemon produces itself (the payload.kind of the
# NodeTouched stub-rewrap); GoalSessionTerminated is the session's own event — the recovery daemon
# only observes it, never produces it (transitions 4/7/8).
RECOVERY_INJECTION_ATTEMPTED = "RecoveryInjectionAttempted"
SESSION_REAL_OUTPUT_OBSERVED = "SessionRealOutputObserved"
INJECTION_OBSERVATION_WINDOW_EXPIRED = "InjectionObservationWindowExpired"
RECOVERY_RETRY_TRIGGERED = "RecoveryRetryTriggered"
RECOVERY_BUDGET_EXHAUSTED = "RecoveryBudgetExhausted"
SESSION_REFROZE_DETECTED = "SessionRefrozeDetected"
_RECOVERY_EVENT_KINDS = frozenset({
    RECOVERY_INJECTION_ATTEMPTED, SESSION_REAL_OUTPUT_OBSERVED,
    INJECTION_OBSERVATION_WINDOW_EXPIRED, RECOVERY_RETRY_TRIGGERED,
    RECOVERY_BUDGET_EXHAUSTED, SESSION_REFROZE_DETECTED,
})
# The key recovery events hang off subjects[] with: follows the existing session-lineage convention
# (GoalSessionStarted etc. land their subject with entity_type=task + entity_id=session_id;
# SubjectEntityType has no separate session type), so that get_events_by_entity("task", session_id)
# goes through the index and returns only events related to that session (O(increment), not a full scan).
_RECOVERY_SUBJECT_ENTITY_TYPE = "task"


























def main() -> None:
    ap = argparse.ArgumentParser(description="Wake-watcher for transient-interrupted Claude jobs")
    ap.add_argument("--once", action="store_true", help="single scan then exit")
    ap.add_argument("--dry-run", action="store_true", help="classify+plan but don't actually wake")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC)
    ap.add_argument("--reset-watermark", action="store_true",
                    help="delete the watermark file then exit (next start begins from a fresh now)")
    args = ap.parse_args()

    if args.reset_watermark:
        reset_watermark()
        return


    touch_heartbeat()  # set it once at startup, to avoid the gap before the first loop round being mistaken for silence
    watermark = load_or_init_watermark()  # None = filter disabled; otherwise an epoch origin (persisted and reused)
    wm_disp = "DISABLED" if watermark is None else _now_iso_from_epoch(watermark)
    log(
        f"wake-watcher start (jobs={JOBS_DIR}, max_wakes={MAX_WAKES}, "
        f"backoff_base={BACKOFF_BASE_SEC}s, slow_retry={SLOW_RETRY_SEC}s, "
        f"interval={args.interval}s, dry_run={args.dry_run}, "
        f"require_dead={SAFETY_REQUIRE_DEAD_PROCESS}, watermark={wm_disp})"
    )
    if args.once:
        n = scan_once(dry_run=args.dry_run, watermark=watermark)
        log(f"scan once complete: {n} wake(s) delivered")
        return
    try:
        while True:
            touch_heartbeat()  # unconditional heartbeat every round — set it even on an idle poll (no candidates/no state flip)
            scan_once(dry_run=args.dry_run, watermark=watermark)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("wake-watcher stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
