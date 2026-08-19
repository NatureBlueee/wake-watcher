#!/usr/bin/env bash
# Scrub check: grep the tree for strings that would leak the maintainer's
# private infrastructure into a public release -- an internal tool name, a
# home directory, a session id shaped like the ones this daemon watches.
# Every rule is case-insensitive.
#
# Usage: scripts/scrub-check.sh [ROOT]
#   ROOT defaults to the repo root. The optional argument exists so this
#   script's own self-test (see CONTRIBUTING.md / the PR that added this
#   file) can point it at a throwaway directory instead of the real repo.
#
# Scanned: every file under ROOT except:
#   - .git/ -- not shipped, and full of noise (blobs, packs)
#   - tests/manual/ -- excluded for the same reason it isn't run in CI:
#                          see CONTRIBUTING.md's test manifest table. It
#                          needs a real API call and isn't part of what a
#                          normal `git clone` + contribution touches.
#   - this script itself -- every banned string below has to appear
#     literally in this file as pattern data, or the check couldn't exist.
#     This is the ONE additional exclusion beyond the two above. Do not add
#     more without asking the maintainer first -- a scrub check that keeps
#     growing its own exception list is a scrub check quietly turning
#     itself off, and that is a worse failure mode than a false positive.
#
# A scrub check that only ever prints green is worse than no scrub check at
# all -- it converts "nobody looked" into "verified clean". Before trusting
# this script's output, self-test it: run it clean, confirm exit 0; run it
# against a directory containing a sample of the banned strings, confirm
# exit 1 and that every rule that should fire does. See the PR that added
# this file for that self-test's actual output.
set -uo pipefail
# (deliberately not `set -e`: grep's "no match" exit code 1 is the expected,
# common outcome of most rules on most files, not an error. Every place a
# real command failure must abort is checked explicitly below instead.)

SELF="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")"
ROOT="${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
ROOT="$(CDPATH= cd -- "$ROOT" && pwd)"
cd "$ROOT" || exit 2

# ---------------------------------------------------------------------------
# file list: everything under ROOT except .git/, tests/manual/, and SELF
# ---------------------------------------------------------------------------
FILES=()
while IFS= read -r f; do
  FILES+=("$f")
done < <(find "$ROOT" \
    \( -path "$ROOT/.git" -o -path "$ROOT/tests/manual" \) -prune -o \
    -type f -print)

SCAN_FILES=()
for f in "${FILES[@]}"; do
  [ "$f" = "$SELF" ] && continue
  # verify-all-changes.sh must spell out the very symbols it asserts are absent --
  # that is how it proves a declared deletion happened. Scanning it would report
  # those assertions as leaks.
  case "$f" in */verify-all-changes.sh) continue;; esac
  # Runtime artifacts land next to the code when the daemon is run from a checkout
  # (state paths default to the script directory). They are gitignored, so they can
  # never reach a release -- but they do sit in the working tree, and a log full of
  # real session ids is exactly what this check is for. Skip anything git ignores:
  # scanning it produces noise that trains people to ignore a red scrub.
  if git -C "$ROOT" check-ignore -q "$f" 2>/dev/null; then continue; fi
  SCAN_FILES+=("$f")
done

if [ "${#SCAN_FILES[@]}" -eq 0 ]; then
  printf 'scrub-check: no files to scan under %s\n' "$ROOT" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# the rules -- "name|F-or-E|pattern"
#   F = fixed string (grep -F), E = extended regex (grep -E)
# Rules deliberately overlap (e.g. "harness" already covers "/opt/harness")
# -- that overlap is not something to dedupe away. Each string was named
# individually and should be reportable by that name if it fires.
# ---------------------------------------------------------------------------
RULES=(
  "home-path|F|/Users/nature"
  "towow|F|towow"
  "feishu-cjk|F|飞书"
  # The CJK rule above does NOT catch the romanized form, which is what
  # actually appears in identifiers (env vars, function names). Separate rule.
  "feishu-latin|F|feishu"
  "harness|F|harness"
  "person-name-lisa|F|Lisa"
  "gate-b|F|gate-b"
  "opt-harness|F|/opt/harness"
  "etc-harness|F|/etc/harness"
  "account-token|E|account[0-9]+\\.token"
  "debt-id|E|debt-[0-9a-f]{6,}"
  # Internal knowledge-graph identifiers. These leak the shape of a private
  # system even when every project name around them has been scrubbed, and they
  # are meaningless to an outside reader -- the prose around them is the value.
  # Matches the @vN suffix on its own: these ids appear both with and without
  # a "concept:" prefix, and requiring the prefix missed the bare form.
  # Excludes "<owner>/<action>@vN" (GitHub Actions refs), which are not internal ids.
  "concept-id|E|(^|[^/[:alnum:]])[a-z][a-z0-9-]{5,}@v[0-9]"
  "voi-id|E|\\bvoi-[A-Za-z0-9-]{2,}"
  "finding-doc-id|E|\\bFINDING-[a-z][a-z0-9-]{6,}"
  "finding-id|E|\\bf-[a-z0-9]+(-[a-z0-9]+){3,}"
  "reference-id|E|\\breference_[a-z_]{10,}"
  "invariant-id|E|\\binv-[a-z-]{6,}"
  "summon|F|summon"
  "allbuddy|F|allbuddy"
  "summon-cjk|F|召集台"
  "uuid-session-id|E|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
  # UUIDs are already covered above. This is the separate rule the pattern
  # above CANNOT catch: this codebase's own session ids are bare 8-hex-digit
  # tokens (see tests/test_classify.py's fixtures, e.g. "a real job",
  # "a real job") with no dashes at all, so they don't match a UUID shape.
  "short-hex-id|E|\\b[0-9a-f]{8}\\b"
)

FAIL=0
for rule in "${RULES[@]}"; do
  name="${rule%%|*}"
  rest="${rule#*|}"
  kind="${rest%%|*}"
  pattern="${rest#*|}"

  case "$kind" in
    F) out=$(grep -niIF -- "$pattern" "${SCAN_FILES[@]}" 2>/dev/null) ;;
    E) out=$(grep -niIE -- "$pattern" "${SCAN_FILES[@]}" 2>/dev/null) ;;
    *) printf 'scrub-check: bad rule kind %s for %s\n' "$kind" "$name" >&2; exit 2 ;;
  esac
  rc=$?

  if [ "$rc" -eq 0 ]; then
    FAIL=1
    printf '\n=== BANNED: %s (%s) ===\n' "$name" "$pattern"
    printf '%s\n' "$out"
  elif [ "$rc" -gt 1 ]; then
    printf 'scrub-check: grep error on rule %s (exit %s)\n' "$name" "$rc" >&2
    FAIL=1
  fi
done

printf '\n'
if [ "$FAIL" -ne 0 ]; then
  printf 'scrub-check: FAILED -- banned string(s) found above\n' >&2
  exit 1
fi
printf 'scrub-check: clean (%d files scanned, %d rules, root=%s)\n' \
  "${#SCAN_FILES[@]}" "${#RULES[@]}" "$ROOT"
exit 0
