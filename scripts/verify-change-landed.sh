#!/usr/bin/env bash
# Assert that a change you claim to have made is actually present.
#
# Written after a real incident: a new module-level flag was reported as landed,
# passed a syntax check and a live `--once --dry-run` run, and was not in the
# file at all -- a concurrent writer had overwritten it. Both checks were blind
# to that, because they test "nothing broke", not "the thing I claimed exists".
#
#   verify-change-landed.sh <symbol> <file> [<symbol> <file> ...]
set -uo pipefail
[ $# -lt 2 ] || [ $(( $# % 2 )) -ne 0 ] && {
  echo "usage: verify-change-landed.sh <symbol> <file> [...]" >&2; exit 2; }
rc=0
while [ $# -gt 0 ]; do
  sym="$1"; file="$2"; shift 2
  if [ ! -f "$file" ]; then
    printf "  MISSING FILE  %-34s %s\n" "$sym" "$file"; rc=1; continue
  fi
  n=$(grep -c -- "$sym" "$file" 2>/dev/null || true)
  if [ "${n:-0}" -gt 0 ]; then
    printf "  present (%2s)  %-34s %s\n" "$n" "$sym" "$file"
  else
    printf "  ABSENT        %-34s %s\n" "$sym" "$file"; rc=1
  fi
done
exit $rc
