#!/usr/bin/env bash
# Release gate: the same checks the maintainer runs, runnable by anyone.
# Adapted from a sibling project's release-gate.sh -- same shape (STRICT /
# not_applicable / skip, run every check, fail at the end if any failed),
# retargeted at this project's own checks.
#
# Chains: the test suite (scripts/run-tests.sh -- see that file for why
# nothing here calls pytest or python3 tests/*.py directly), the scrub
# check, the extraction-fidelity skeleton check (opt-in, see below), the
# mutation gate, lint, and coverage. Every check's raw output and exit code
# is printed. Exits non-zero if ANY check failed -- not just the first;
# a reader should see every red check in one run, not one at a time across
# repeated invocations.
#
# Exits non-zero on the first failure it cannot recover from (bad args,
# nowhere to run from). Safe to run at any time -- every check here either
# reads the tree or writes to a temp directory; nothing here touches a live
# service or a real ~/.claude.
set -euo pipefail

PREFIX=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PREFIX"

# --release turns "I could not run this check" into a failure. See
# not_applicable() vs skip() below for the distinction that matters here.
STRICT=0
for arg in "$@"; do
  case "$arg" in --release) STRICT=1 ;;
    *) printf 'release-gate.sh: unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

FAILED=0
FAILED_NAMES=""
run() {
  label=$1
  shift
  printf '\n=== %s\n' "$label"
  if "$@"; then
    printf '    ok\n'
  else
    printf '    FAILED\n' >&2
    FAILED=1
    FAILED_NAMES="$FAILED_NAMES
  - $label"
  fi
}

# A check that does not exist ON THIS CHECKOUT -- as opposed to one that
# should have run and could not. Never fatal, even under --release: the
# artifact under test genuinely does not exist here (see the skeleton
# check below for the one case that uses this).
not_applicable() {
  printf '\n=== %s\n    N/A: %s\n' "$1" "$2"
}

# A check that could not run. Reported either way; fatal only for a release.
# The difference from not_applicable(): the thing being checked DOES apply
# here, but the tool to check it wasn't available -- e.g. `uvx` missing.
# Pretending that's the same as "nothing to check" is how a release ships
# unlinted.
skip() {
  if [ "$STRICT" -eq 1 ]; then
    printf '\n=== %s\n    FAILED (--release requires this to actually run): %s\n' "$1" "$2" >&2
    FAILED=1
    FAILED_NAMES="$FAILED_NAMES
  - $1 (could not run)"
  else
    printf '\n=== %s\n    SKIPPED: %s\n' "$1" "$2" >&2
  fi
}

PYTHON=${PYTHON:-python3}

# --- tests -------------------------------------------------------------
# scripts/run-tests.sh isolates its own state (log/ledger/heartbeat/etc.
# redirected to a temp dir) internally -- nothing to set up here.
run "Test suite (scripts/run-tests.sh)" \
  "$PREFIX/scripts/run-tests.sh"

# --- scrub ---------------------------------------------------------------
run "Scrub check (scripts/scrub-check.sh)" \
  "$PREFIX/scripts/scrub-check.sh"

# --- extraction fidelity --------------------------------------------------
# scripts/verify-ast-skeleton.py proves a translation/extraction pass
# touched only comments by diffing this repo's src/ against the
# pre-extraction original it was extracted from. That original lives
# outside this repo BY DESIGN -- the entire point of extracting an
# open-source release is to produce a tree that does not carry it -- so a
# plain checkout of this repo has no second file to diff against, ever.
# That is a genuinely absent artifact, not a check that failed to run: the
# same distinction the launchd-plist-on-Linux check makes in the sibling
# project this gate was adapted from (a Linux box cannot lint a macOS
# plist; not having node cannot be conflated with that).
#
# Set WAKE_WATCHER_SKELETON_REFERENCE to a directory containing the
# pre-extraction classify.py / wake_watcher.py to run this for real (the
# maintainer's own machine does; CI does not, and should not need to).
# scripts/.p0-declared-deletions is the allowlist of symbols the extraction
# pass was declared to remove -- it is one comma-separated line, matching
# verify-ast-skeleton.py's own --allow parsing.
if [ -n "${WAKE_WATCHER_SKELETON_REFERENCE:-}" ]; then
  ALLOW=$(tr -d '\n' < "$PREFIX/scripts/.p0-declared-deletions")
  run "AST skeleton: classify.py vs pre-extraction original" \
    "$PYTHON" "$PREFIX/scripts/verify-ast-skeleton.py" \
      "$WAKE_WATCHER_SKELETON_REFERENCE/classify.py" \
      "$PREFIX/src/wake_watcher/classify.py" \ --allow "$ALLOW"
  run "AST skeleton: wake_watcher.py vs pre-extraction original" \
    "$PYTHON" "$PREFIX/scripts/verify-ast-skeleton.py" \
      "$WAKE_WATCHER_SKELETON_REFERENCE/wake_watcher.py" \
      "$PREFIX/src/wake_watcher/wake_watcher.py" \ --allow "$ALLOW"
else
  not_applicable "AST skeleton (extraction fidelity)" \
    "WAKE_WATCHER_SKELETON_REFERENCE not set -- no pre-extraction original to diff against in a plain checkout"
fi

# --- mutation gate ---------------------------------------------------------
# verify-mutations.py isolates itself (copies src/+tests/ into a fresh temp
# dir per contract before running anything) -- nothing to set up here, and
# nothing here should export WAKE_WATCHER_* state-file overrides before this
# runs: if it inherited a fixed WAKE_WATCHER_LEDGER/LOG/etc. path, every one
# of its 7 contract tempdirs would share that same state file instead of
# getting its own, and a mutation in contract A could leak state into
# contract B's run.
run "Mutation gate (scripts/verify-mutations.py)" \
  "$PYTHON" "$PREFIX/scripts/verify-mutations.py"

# --- lint / coverage -------------------------------------------------------
# Fetched on demand via uvx, not a project dependency -- this project ships
# with zero runtime deps and pytest as its only dev dep (see pyproject.toml).
# Missing uvx is a "could not run", not a "does not apply": skip(), not
# not_applicable().
if command -v uvx >/dev/null 2>&1; then
  run "Lint (uvx ruff check src/)" \
    uvx ruff check "$PREFIX/src/"
else
  skip "Lint (uvx ruff check src/)" "uvx not on PATH"
fi

run_coverage_pass() {
  # Own isolation, scoped to this function only (it is the LAST check in
  # this file, so exporting here has nothing downstream left to pollute --
  # unlike the mutation gate above, this DOES run tests directly against
  # the real src/ tree via `coverage run tests/test_X.py`, bypassing
  # run-tests.sh's own internal isolation, so it needs the same redirection
  # run-tests.sh applies to itself.
  cov_tmp=$(mktemp -d)
  trap 'rm -rf "$cov_tmp"' RETURN

  export WAKE_WATCHER_CLAUDE_HOME="$cov_tmp/claude-home"
  export WAKE_WATCHER_LOG="$cov_tmp/wake-watcher.log"
  export WAKE_WATCHER_LEDGER="$cov_tmp/ledger.json"
  export WAKE_WATCHER_HEARTBEAT="$cov_tmp/wake-watcher.heartbeat"
  export WAKE_WATCHER_WATERMARK_FILE="$cov_tmp/watermark.json"
  export WAKE_WATCHER_NEEDS_HUMAN="$cov_tmp/needs-human.log"
  export WAKE_WATCHER_CLAUDE_RESOLUTION_STATE="$cov_tmp/claude-resolution-state.json"
  export WAKE_WATCHER_DO_NOT_WAKE_FILE="$cov_tmp/do-not-wake.txt"
  export PYTHONDONTWRITEBYTECODE=1
  mkdir -p "$WAKE_WATCHER_CLAUDE_HOME/jobs"
  export COVERAGE_FILE="$cov_tmp/.coverage"

  # Driven by run-tests.sh's own --manifest, not a second hardcoded list --
  # see that file's header comment on why there is exactly one manifest.
  while read -r mode name; do
    if [ "$mode" = "script" ]; then
      # A per-file failure here is a TEST failure, already reported (and
      # gated on) by the "Test suite" check above -- this pass exists to
      # measure coverage, not to re-report the same red a second time under
      # a different label. `|| true` so one red test doesn't stop coverage
      # from being gathered for the rest, and doesn't make THIS check's
      # pass/fail hinge on a result run() already covered.
      uvx coverage run --source=src/wake_watcher --append "tests/${name}.py" \
        >/dev/null 2>&1 || true
    else
      uvx coverage run --source=src/wake_watcher --append \
        -m pytest "tests/${name}.py" -q -p no:cacheprovider \
        >/dev/null 2>&1 || true
    fi
  done < <("$PREFIX/scripts/run-tests.sh" --manifest)

  # This is the one command in this function whose exit code actually
  # decides the check: did coverage itself run and produce a report.
  uvx coverage report -m
}

if command -v uvx >/dev/null 2>&1; then
  run "Coverage (uvx coverage over the run-tests.sh manifest)" run_coverage_pass
else
  skip "Coverage (uvx coverage)" "uvx not on PATH"
fi

printf '\n'
if [ "$FAILED" -ne 0 ]; then
  printf 'release gate: FAILED%s\n' "$FAILED_NAMES" >&2
  exit 1
fi
printf 'release gate: all checks passed\n'
