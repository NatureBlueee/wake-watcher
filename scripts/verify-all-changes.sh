#!/usr/bin/env bash
# Assert every declared change class actually landed in the code.
#
# Written after two incidents in one session: (1) a flag reported as landed had
# been overwritten by a concurrent writer, and (2) an entire change class was
# missed during a manual inventory and only surfaced because a scrub rule
# happened to hit a hardcoded string. Neither a syntax check nor a passing test
# suite is sensitive to "the change was never written" -- only a direct
# assertion on the symbol is.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
SRC=src/wake_watcher
rc=0
check() { # <class> <expect present|absent> <symbol> <file>
  local cls="$1" want="$2" sym="$3" file="$4" n
  # NOTE: `grep -c ... || echo 0` produces "0\n0" on no-match (grep already
  # printed 0, then echo adds another) and silently breaks the comparison --
  # which made this very script report a missing symbol as ok. Count lines instead.
  n=$(grep -c -- "$sym" "$file" 2>/dev/null)
  [ -z "$n" ] && n=0
  local got=present; [ "$n" = "0" ] && got=absent
  if [ "$got" = "$want" ]; then printf "  ok    %-34s %s\n" "$cls" "$sym"
  else printf "  FAIL  %-34s %s (want %s, got %s)\n" "$cls" "$sym" "$want" "$got"; rc=1; fi
}
echo "=== declared change classes ==="
check "1-delete-recovery"      absent  "run_recovery_once"           $SRC/wake_watcher.py
check "1-delete-feishu"        absent  "FEISHU"                      $SRC/wake_watcher.py
check "1-delete-eventlog"      absent  "_recovery_towow_eventlog"    $SRC/wake_watcher.py
check "2-patterns-datafied"    present "patterns.json"               $SRC/classify.py
check "2-check-string-cli"     present "check-string"                $SRC/classify.py
check "2-error-epoch"          present "error-epoch"                 $SRC/classify.py
check "3-liveness-seam"        present "WAKE_WATCHER_LIVENESS_CMD"   $SRC/wake_watcher.py
check "3-no-hardcoded-external" absent "towow.cli.main"              $SRC/wake_watcher.py
check "6-interactive-coverage" present "_interactive_candidates"     $SRC/wake_watcher.py
check "6-interactive-escape"   present "COVER_INTERACTIVE"           $SRC/wake_watcher.py
check "7-pty-safety-test"      present "ENABLE_PTY_INJECT"           tests/test_attach_inject_routing.py
check "8-pty-optin"            present "ENABLE_PTY_INJECT"           $SRC/wake_watcher.py
echo
[ $rc -eq 0 ] && echo "all declared changes are present in the code" \
              || echo "some declared changes are NOT in the code -- see FAIL above"
exit $rc
