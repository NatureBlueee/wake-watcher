#!/usr/bin/env python3
"""Transient-vs-real interruption classifier for the wake-watcher.

Rule #1 (fixed): only auto-wake on a *transient infra error* -- connection
cut / rate limit / overloaded / 5xx / "this is not your fault" and the like.
Never auto-retry a genuine task failure, a user rejection (rejected/denied),
a real error, or a legitimately blocked session that is waiting on owner
input.

Design principle: ALLOWLIST + DEFAULT-DENY.
  Only classified transient if the detail text matches the transient
  allowlist *and* does not match the veto list.
  Everything else (completion summary / waiting on owner / real error /
  unrecognized) -> not transient -> do not wake.

Why keyed on `detail`, not on `state`:
  In practice, state="blocked" in ~/.claude/jobs/*/state.json carries an
  "overloaded" meaning that covers both a transient infra interruption
  (detail="Connection closed mid-response") and a legitimate wait for owner
  input (detail="awaiting X API credentials" / a completion summary). So
  `state` alone is not reliable -- the `detail` text has to be read to tell
  them apart.

Error strings come from a real scan of ~/.claude/{tasks,jobs}/ for transient
errors that actually occurred (see README, error catalogue).

The two rule tables (TRANSIENT_PATTERNS / NON_TRANSIENT_VETO) live in
patterns.json next to this file, not inline in Python -- see that file for
per-pattern provenance (how many times each string was observed, and which
real session/job it interrupted).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PATTERNS_PATH = Path(__file__).resolve().parent / "patterns.json"


def _load_pattern_table(entries: list[dict]) -> list[str]:
    """Pull just the regex strings out of patterns.json, preserving file
    order. Order does not affect veto-vs-transient precedence -- that's
    guaranteed by the `if veto is not None` short-circuit in classify()
    below, not by position in either list.
    """
    return [entry["pattern"] for entry in entries]


with _PATTERNS_PATH.open(encoding="utf-8") as _f:
    _PATTERN_DATA = json.load(_f)

# ── Transient infra-error allowlist (case-insensitive substring / regex) ───
# Loaded from patterns.json; see that file for the provenance note on each
# entry (how many times it was observed, or which real session/job it
# interrupted).
TRANSIENT_PATTERNS: list[str] = _load_pattern_table(_PATTERN_DATA["TRANSIENT_PATTERNS"])

# ── Veto list: even if the transient allowlist matches, matching one of
# these still forces NOT transient (guards against waking a real problem) ──
# These are "actually stopped" signals -- user rejection / genuine task
# failure / waiting on owner / quota exhausted / a request error too
# malformed to understand.
NON_TRANSIENT_VETO: list[str] = _load_pattern_table(_PATTERN_DATA["NON_TRANSIENT_VETO"])


def _matches_any(text: str, patterns: list[str]) -> str | None:
    """Return the first matching pattern (for logging provenance), else None."""
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            return p
    return None


# -- Third category: session/usage limit with a reset time (explicitly
# requested by the owner, 2026-07-07) --------------------------------------
# Strings like "You've hit your session limit · resets 3:20am
# (Australia/Melbourne)": retrying immediately is useless (so this still
# classifies as transient=False, leaving the old immediate-wake path
# untouched), but it carries its own reset time -- resuming after that time
# is correct. classify() flags this separately and parses reset_epoch, so
# transcript_wake_decision can decide to send once [now > reset_epoch +
# buffer].
# Timezone semantics: the clock time in the error text is in the account's
# timezone, and this machine happens to share it (Melbourne), so it is
# parsed as local time directly. If this is ever deployed across timezones,
# prefer waking late over waking early -- waking late just costs one more
# scan cycle, waking early burns budget hitting the wall again.
SESSION_LIMIT_PATTERN = r"(?:session|weekly|usage)\s+limit.{0,40}?resets\s+\d"


def parse_reset_epoch(text: str, now: float | None = None) -> float | None:
    """Parse 'resets 3:20am' / 'resets 10pm' into the next local epoch that
    hits that clock time.

    Unparseable -> None (caller fails closed: treated as a plain veto, no
    auto-wake).
    Clock time already past -> treated as tomorrow at the same time (if the
    error says "resets X" and X is already in the past, it can only mean
    tomorrow).

    WARNING: `now` must be anchored to *the moment the error message was
    written*, not the scan moment (2026-07-12):
    the clock time in the error text means "the next X" relative to when the
    error fired. If every scan cycle re-parses against the current time,
    then once the reset point has passed, the same "resets 5:10am" string
    gets parsed as *tomorrow* 5:10 -- wake_at is then always in the future,
    which makes the "resume once the reset time hits" branch mathematically
    unreachable (this is the root cause of that feature firing zero times
    successfully since it shipped on 07-07).
    """
    import time as _time

    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, flags=re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    minute = int(m.group(2) or 0)
    if now is None:
        now = _time.time()
    lt = _time.localtime(now)
    candidate = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if candidate <= now:
        candidate += 86400.0
    return candidate


def classify(detail: str | None, error_epoch: float | None = None) -> dict:
    """Classify an interruption `detail` string.

    error_epoch: timestamp (epoch) of the error message itself. The
    session-limit reset_epoch is anchored to it; if not passed, this falls
    back to anchoring on the scan moment (only acceptable when the error has
    no timestamp of its own -- correct on first scan, but drifts across a
    reset boundary).

    Returns dict:
      {
        "transient": bool,          # True => eligible for auto-wake
        "reason": str,              # human-readable why
        "matched": str | None,      # the pattern that decided it (provenance)
        "vetoed_by": str | None,    # if a veto fired despite transient match
      }

    Default-deny: empty / None / unrecognized => transient=False.
    Extra keys (2026-07-07 session-limit third category, backward compatible -- old callers that only read `transient` are unaffected):
      "session_limit": bool   # a limit that carries its own reset time (not
                               # woken immediately, woken after the reset)
      "reset_epoch": float|None  # local epoch; None if unparseable (fails
                                  # closed as a plain veto)
    """
    _extra = {"session_limit": False, "reset_epoch": None}
    if not detail or not detail.strip():
        return {
            "transient": False,
            "reason": "empty detail (default-deny)",
            "matched": None,
            "vetoed_by": None,
            **_extra,
        }

    text = detail.strip()

    # Veto first: a real-stop signal always wins, even if a transient word also appears.
    veto = _matches_any(text, NON_TRANSIENT_VETO)
    transient_hit = _matches_any(text, TRANSIENT_PATTERNS)

    if veto is not None:
        # Third-category detection: within the veto family, "limit + resets
        # <clock time>" self-heals -- it can resume once the reset time
        # hits. Still keeps transient=False (never enters the immediate-wake
        # path), just also lights up the session_limit flag.
        if re.search(SESSION_LIMIT_PATTERN, text, flags=re.IGNORECASE):
            epoch = parse_reset_epoch(text, now=error_epoch)
            return {
                "transient": False,
                "reason": f"session limit with reset time (vetoed /{veto}/, wake after reset)",
                "matched": transient_hit,
                "vetoed_by": veto,
                "session_limit": True,
                "reset_epoch": epoch,
            }
        return {
            "transient": False,
            "reason": f"vetoed: matched non-transient signal /{veto}/",
            "matched": transient_hit,
            "vetoed_by": veto,
            **_extra,
        }

    if transient_hit is not None:
        return {
            "transient": True,
            "reason": f"transient infra error: matched /{transient_hit}/",
            "matched": transient_hit,
            "vetoed_by": None,
            **_extra,
        }

    return {
        "transient": False,
        "reason": "no transient pattern matched (default-deny)",
        "matched": None,
        "vetoed_by": None,
        **_extra,
    }


# Stable signature for a (sessionId, error) pair — used by the watcher's
# per-session retry counter so unrelated later errors get a fresh budget.
def error_signature(detail: str | None) -> str:
    """A coarse, stable signature of the error class (not the exact string)."""
    res = classify(detail)
    if res["matched"]:
        # normalize the matched pattern to a short tag
        return re.sub(r"[^a-z0-9]+", "-", res["matched"].lower()).strip("-")[:48]
    return "unknown"


def check_string_report(text, error_epoch=None):
    """Render the human-readable verdict for one error string.

    Returns the report as a string rather than printing it, so both entry
    points that expose this -- `classify.py --check-string` and
    `wake-watcher --check-string` -- format it identically. Two commands
    that disagree about what the classifier said would be worse than
    having only one.
    """
    result = classify(text, error_epoch=error_epoch)
    verdict = "TRANSIENT -> would wake" if result["transient"] else "NOT transient -> would NOT wake"
    lines = [
        f"verdict:     {verdict}",
        f"reason:      {result['reason']}",
        f"matched:     {result['matched']!r}",
        f"vetoed_by:   {result['vetoed_by']!r}",
        f"session_limit: {result['session_limit']}",
        f"reset_epoch: {result['reset_epoch']!r}",
    ]
    # The reset-time note is only meaningful when a reset time was actually
    # parsed. Printing it on every verdict trains readers to ignore it.
    if error_epoch is None and result["reset_epoch"] is not None:
        lines.append(
            "note: reset_epoch based on current time, may be inaccurate "
            "(pass --error-epoch to anchor it to when the error actually happened)"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="classify.py",
        description=(
            "Classify a Claude Code interruption error string as transient "
            "(would wake the session) or not (would not wake it)."
        ),
    )
    parser.add_argument(
        "--check-string",
        metavar="TEXT",
        help="the error detail text to classify",
    )
    parser.add_argument(
        "--error-epoch",
        type=float,
        default=None,
        metavar="EPOCH",
        help=(
            "unix timestamp of when the error actually happened. Anchors "
            "session-limit reset-time parsing (parse_reset_epoch) to the "
            "right moment. If omitted, reset_epoch is computed from the "
            "current time instead, which is wrong for anything but a "
            "brand-new error."
        ),
    )
    args = parser.parse_args()

    if args.check_string is not None:
        print(check_string_report(args.check_string, error_epoch=args.error_epoch))
        sys.exit(0)

    # quick manual smoke (legacy positional-arg mode, no flags)
    print(classify(sys.argv[1] if len(sys.argv) > 1 else None))
