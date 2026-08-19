#!/usr/bin/env bash
# The only authoritative way to run this project's test suite. See
# CONTRIBUTING.md, "Why scripts/run-tests.sh and not `pytest tests/`".
#
# This suite is NOT uniform, and that is why this file exists instead of a
# one-line `pytest tests/` in CI:
#
#   - 7 files are script-style: their own `if __name__ == "__main__":`
#     runner, a `_check(cond, msg) -> int` helper that returns 0/1 and NEVER
#     raises, results summed into `fails`, and `sys.exit(1 if run() else 0)`
#     at the end. A failing assertion increments a counter and the test
#     keeps going -- that is what lets one run report every failing case
#     instead of stopping at the first.
#   - 3 files are pytest-native: bare `assert` inside top-level `def
#     test_*()` functions, no runner of their own.
#
# Point bare `pytest tests/` (or `python -m pytest tests/`) at this
# directory and the 7 script-style files collect ZERO pytest test functions
# each -- their logic lives inside `run()`, not in a top-level `test_*()`
# pytest can see -- and pytest's default behaviour on "0 items collected"
# is exit 0. It reads as a clean run and checks nothing.
#
# Measured, not theoretical: break one real classification rule in
# src/wake_watcher/patterns.json and
#   python3 tests/test_classify.py   ->  exit 1, prints [FAIL] lines
#   python3 -m pytest tests/ -q      ->  "6 passed" -- only the 3
#                                         pytest-native files ran; test_classify.py
#                                         contributed zero tests and zero failures.
# A CI job that only ran `pytest tests/` would have shipped that regression
# green. This script is the fix: it is the ONE place the two manifests
# below may be hardcoded. .github/workflows/ci.yml calls only this script,
# never pytest directly -- see the CI meta-check at the bottom of this file,
# which fails loudly if that ever regresses (e.g. via a well-meant "clean up
# CI" PR that doesn't know the history above).
set -uo pipefail
# (deliberately not `set -e`: this script's whole job is to run N commands
# that are EXPECTED to sometimes fail and keep going so every result gets
# reported. Each place a real infrastructure failure must abort does so
# explicitly via an `exit`.)

PREFIX=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PREFIX" || exit 2

QUIET=0
MANIFEST_ONLY=0
for arg in "$@"; do
  case "$arg" in --quiet) QUIET=1 ;; --manifest)
      # Print "<mode> <name>" per test, one per line, and exit -- no test
      # runs. This exists so OTHER tooling (scripts/release-gate.sh's
      # coverage pass) can drive the same manifest without a second,
      # independently-drifting copy of these two lists. If you need the
      # list of tests somewhere new, consume this flag; do not re-hardcode
      # the names.
      MANIFEST_ONLY=1 ;;
    *) printf 'run-tests.sh: unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# THE MANIFEST -- edit ONLY here. Two lists, and nowhere else in this repo
# hardcodes either of them (ci.yml calls this script; release-gate.sh's
# coverage pass reads `--manifest`).
# ---------------------------------------------------------------------------
SCRIPT_TESTS=(
  test_classify
  test_scope
  test_watermark
  test_dedup_fix
  test_loop_control
  test_attach_inject_routing
  test_interactive_session_coverage
)
PYTEST_TESTS=(
  test_dual_signal
  test_session_limit_wake
  test_claude_resolution
)

if [ "$MANIFEST_ONLY" -eq 1 ]; then
  for t in "${SCRIPT_TESTS[@]}"; do printf 'script %s\n' "$t"; done
  for t in "${PYTEST_TESTS[@]}"; do printf 'pytest %s\n' "$t"; done
  exit 0
fi

# ---------------------------------------------------------------------------
# isolate state -- MUST happen before any test file is imported/run,
# script-style or pytest-style.
# ---------------------------------------------------------------------------
# src/wake_watcher/wake_watcher.py defaults every one of its state files --
# log, ledger, heartbeat, needs-human, watermark, do-not-wake list, and the
# claude-binary-resolution dedup state -- to a path INSIDE
# src/wake_watcher/ itself (its module-level STATE_DIR = the running
# script's own directory) unless the matching WAKE_WATCHER_* env var
# overrides it. These are plain module-level `os.environ.get(...)` calls,
# evaluated at IMPORT time -- so a test run that imports the module without
# these set writes ledger.json / *.log / watermark.json / etc. straight
# into src/wake_watcher/ and pollutes the repo working tree. This is not
# hypothetical: it has happened in this exact tree (a stray, gitignored
# src/wake_watcher/wake-watcher.log with real paths and session ids was
# found sitting there while this script was being written -- see
# scripts/scrub-check.sh, which is what caught it).
TMP_STATE=$(mktemp -d)
trap 'rm -rf "$TMP_STATE"' EXIT

export WAKE_WATCHER_CLAUDE_HOME="$TMP_STATE/claude-home"
export WAKE_WATCHER_LOG="$TMP_STATE/wake-watcher.log"
export WAKE_WATCHER_LEDGER="$TMP_STATE/ledger.json"
export WAKE_WATCHER_HEARTBEAT="$TMP_STATE/wake-watcher.heartbeat"
export WAKE_WATCHER_WATERMARK_FILE="$TMP_STATE/watermark.json"
export WAKE_WATCHER_NEEDS_HUMAN="$TMP_STATE/needs-human.log"

# A stub `claude` binary. Without one, every delivery path short-circuits to
# "all three layers of claude binary resolution failed" and every wake-related
# assertion fails -- which is exactly what happened on CI while the maintainer's
# laptop stayed green, because the laptop had the real CLI on PATH. The tests
# never invoke it for real (they patch subprocess or use the FAKE_DELIVER seam);
# they only need resolution to succeed.
export WAKE_WATCHER_CLAUDE_KNOWN_PATHS="$TMP_STATE/bin/claude"
mkdir -p "$TMP_STATE/bin"
printf '#!/bin/sh\necho "stub claude: this binary is never meant to run" >&2\nexit 1\n' \
  > "$TMP_STATE/bin/claude"
chmod +x "$TMP_STATE/bin/claude"

# The interactive candidate source shells out to `claude agents --json`, which is
# NOT gated by WAKE_WATCHER_CLAUDE_HOME -- without this seam the suite can see
# real sessions running on the machine.
export WAKE_WATCHER_FAKE_AGENTS="${WAKE_WATCHER_FAKE_AGENTS:-[]}"
export WAKE_WATCHER_CLAUDE_RESOLUTION_STATE="$TMP_STATE/claude-resolution-state.json"
export WAKE_WATCHER_DO_NOT_WAKE_FILE="$TMP_STATE/do-not-wake.txt"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$WAKE_WATCHER_CLAUDE_HOME/jobs"
# Individual test files are free to further sandbox themselves (most spawn
# wake_watcher.py as a subprocess with their own throwaway tmp dir + env) --
# these exports are the floor everyone gets whether or not they also do that.

PYTHON="${PYTHON:-python3}"
FAILED=0
RESULTS=()   # flat "name|STATUS|rc" list -- kept portable (no assoc arrays)

run_one() {
  mode="$1"; name="$2"
  file="tests/${name}.py"
  if [ ! -f "$file" ]; then
    printf 'run-tests.sh: manifest names %s but %s does not exist\n' "$name" "$file" >&2
    FAILED=1
    RESULTS+=("$name|MISSING|127")
    return
  fi
  if [ "$mode" = "script" ]; then
    cmd=("$PYTHON" "$file")
  else
    cmd=("$PYTHON" -m pytest "$file" -q -p no:cacheprovider)
  fi

  if [ "$QUIET" -eq 1 ]; then
    if out=$("${cmd[@]}" 2>&1); then rc=0; else rc=$?; fi
  else
    printf '\n=== %s (%s): %s ===\n' "$name" "$mode" "${cmd[*]}"
    if "${cmd[@]}"; then rc=0; else rc=$?; fi
    out=""
  fi

  if [ "$rc" -eq 0 ]; then
    RESULTS+=("$name|PASS|$rc")
  else
    FAILED=1
    RESULTS+=("$name|FAIL|$rc")
    if [ "$QUIET" -eq 1 ]; then
      printf '\n=== %s (%s) FAILED (exit %s) ===\n' "$name" "$mode" "$rc"
      printf '%s\n' "$out"
    fi
  fi
}

for t in "${SCRIPT_TESTS[@]}"; do run_one script "$t"; done
for t in "${PYTEST_TESTS[@]}"; do run_one pytest "$t"; done

# ---------------------------------------------------------------------------
# summary -- one line per file, its mode, and its real exit code
# ---------------------------------------------------------------------------
printf '\n=== summary ===\n'
for r in "${RESULTS[@]}"; do
  rname="${r%%|*}"; rest="${r#*|}"; status="${rest%%|*}"; rc="${rest#*|}"
  printf '  %-42s %-7s exit=%s\n' "$rname" "$status" "$rc"
done

# ---------------------------------------------------------------------------
# CI meta-check: ci.yml must call this script and never invoke pytest
# directly. Fatal on ANY occurrence of the literal token "pytest" in
# ci.yml -- a well-meant "simplify CI" PR reaching for `pytest tests/`
# would silently reintroduce the exact bug this whole file exists to close,
# and green CI would not catch it (that IS the bug). This check is what does.
# ---------------------------------------------------------------------------
CI_YML="$PREFIX/.github/workflows/ci.yml"
printf '\n=== CI meta-check (%s) ===\n' "${CI_YML#"$PREFIX"/}"
if [ ! -f "$CI_YML" ]; then
  printf '    FAILED: %s does not exist\n' "$CI_YML" >&2
  FAILED=1
else
  # Strip full-line comments before checking. ci.yml's own header legitimately
  # explains, in prose, why "pytest" and "pull_request_target" must NOT
  # appear in the workflow's real content -- e.g. "pull_request (not
  # pull_request_target) on purpose" -- so those words appearing in a
  # comment is the check working as designed, not a violation. Only real
  # YAML content (keys/values/run: commands) may trip these checks.
  CI_CONTENT=$(grep -v '^[[:space:]]*#' "$CI_YML")
  ci_ok=1
  if printf '%s\n' "$CI_CONTENT" | grep -n 'pytest'; then
    printf '    FAILED: ci.yml contains the literal token "pytest" outside a comment --\n' >&2
    printf '            it must call scripts/run-tests.sh only. See CONTRIBUTING.md.\n' >&2
    ci_ok=0
  fi
  if ! printf '%s\n' "$CI_CONTENT" | grep -q 'run-tests\.sh'; then
    printf '    FAILED: ci.yml never calls scripts/run-tests.sh\n' >&2
    ci_ok=0
  fi
  if printf '%s\n' "$CI_CONTENT" | grep -q 'pull_request_target'; then
    printf '    FAILED: ci.yml uses pull_request_target\n' >&2
    ci_ok=0
  fi
  if printf '%s\n' "$CI_CONTENT" | grep -q 'secrets\.'; then
    printf '    FAILED: ci.yml references secrets. -- this project ships with none\n' >&2
    ci_ok=0
  fi
  if [ "$ci_ok" -eq 1 ]; then
    printf '    ok\n'
  else
    FAILED=1
  fi
fi

printf '\n'
if [ "$FAILED" -ne 0 ]; then
  printf 'run-tests.sh: FAILED\n' >&2
  exit 1
fi
printf 'run-tests.sh: all green\n'
exit 0
