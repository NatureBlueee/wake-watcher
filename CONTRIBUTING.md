# Contributing

Thanks for looking. This is a small, unattended daemon with an unusual
amount of test discipline, and the reasons are worth stating before the
mechanics.

## Run it first

```sh
git clone https://github.com/NatureBlueee/wake-watcher
cd wake-watcher
pip install -e '.[dev]'      # zero runtime deps; pytest is dev-only, for the pytest-style tests

./scripts/run-tests.sh       # the only authoritative way to run this suite
```

Everything runs offline, at zero API cost. There are no live-service tests
in CI and no credentials anywhere in this repo — wake-watcher doesn't hold
any (see `SECURITY.md`).

### Why `scripts/run-tests.sh` and not `pytest tests/`

This project's test suite is not uniform. Most files are script-style —
plain assertions with their own `if __name__ == "__main__":` runner,
executed as `python3 test_X.py` — and only a handful are pytest-native.
Point plain `pytest` at the whole directory and the script-style files
collect zero pytest test functions; pytest exits `0` on that by default. It
reads as a clean run and checks nothing. `scripts/run-tests.sh` runs each
file the way its baseline was actually measured, and is the only command
whose result you should trust or quote in a PR.

If you're adding a new test file and aren't sure whether it should be
script-style or pytest-style, ask in the PR — don't guess, and don't invoke
`pytest` directly on the suite as a substitute.

## The one thing that is not negotiable

**A test that cannot fail is worse than no test**, because it converts
"unknown" into "verified" without doing any work — and a reviewer who sees
green has no reason to look closer.

So: **after writing a test, break the code it covers and watch it fail.**
Not "convince yourself it would fail" — actually change the source, run the
test, see red, then put the source back. If it stays green, the test is
measuring something else.

The canonical example of how this goes wrong in this codebase is the
PTY-injection guard, described in full under the second hard rule below.
Read it even if you never touch that file — the same shape of mistake is
easy to reproduce anywhere a guard has more than one condition.

## Two hard rules

These aren't style preferences. A PR that skips either gets sent back
regardless of what color CI is.

### 1. The retry-decision surface needs a human, not just green CI

Changes to `patterns.json`, the veto logic in `classify.py`,
`WAKE_WATCHER_LIVENESS_CMD`, or anything in the PTY-injection path require a
human security review before merge. A passing CI run is necessary here, but
it is not sufficient, and treating it as sufficient is the specific mistake
this rule exists to prevent.

Why: the patterns regression suite only protects rules that already exist.
A *newly added* allow-rule has no history to regress against — as long as
it doesn't contradict an existing test, it merges on green. Its effect is to
make some class of real failure get retried automatically instead of
surfaced to a human. Reviewing the diff itself — not just the test result —
is the only check that catches that before it ships. The same logic applies
to the veto side: a rule quietly removed or narrowed is invisible to a
regression suite built only to protect what's already there.

### 2. Multi-condition guards must be pinned condition by condition

The safety guard that decides whether to type into a *live* agent session
(PTY injection) is a two-condition check: the target has to be a
background-kind session, **and** it has to carry an id field. A test
fixture for the "don't inject" branch happened to also be missing that id
field. Delete the first condition from the guard — the one actually doing
the work of excluding non-background sessions — and the test suite stayed
green, because the second condition was quietly carrying the whole check by
itself. The bug was never really in the guard; it was in a test that could
pass whether or not the guard was doing its job.

The general rule, not just the specific incident:

- When a guard is `A and B`, write **one fixture per condition**, and make
  that fixture satisfy every *other* condition on purpose. The only thing
  left unmet should be the one you're testing.
- Assert on the **effect**, not the decision. Spy on the dangerous call
  (PTY injection, a liveness-command exec, a wake delivery) and assert it
  was **not invoked** — don't just assert that some routing function
  returned the expected label. A function that "decided correctly" but
  whose decision nobody enforced downstream is exactly the bug this pattern
  is meant to catch.

If your PR touches a multi-condition guard anywhere in this codebase, add
one fixture per condition and say so in the PR description.

## What a change should look like

- **Say why in the code, not just in the PR.** Comments here explain the
  failure that motivated the line, because the reader six months later
  needs the reason more than the mechanism.
- **Small and behaviour-preserving by default.** This runs unattended, and
  its whole job is to poke other unattended sessions back to life. A
  refactor that improves elegance and changes behaviour is a bad trade
  here.
- **No new runtime dependencies.** Zero today, on purpose — production runs
  under the system Python, and a service that gets adopted into someone's
  `launchd`/`systemd` shouldn't drag a dependency tree into a process that
  runs unattended and unwatched.
- **Never weaken a safety default to make a change pass.** `REQUIRE_DEAD`,
  project-root scoping, and the do-not-wake list all exist because
  something broke without them. If a default is genuinely wrong, that's its
  own PR with its own reasoning — not a side effect of an unrelated change.

## Things that will be refused

- Reintroducing the recovery/escalation path that was cut before this
  repository's history starts. It depended on infrastructure this project
  doesn't ship, and it was never exercised by a production instance's
  ordinary traffic. Open an issue first if you think you need it back.
- A new runtime dependency.
- Weakening `REQUIRE_DEAD`, project-root scoping, or the do-not-wake list to
  make a use case more convenient.
- Silently swallowing an error anywhere in the scan/wake path. Loud and
  wrong beats quiet and wrong — see `THREAT-MODEL.md` on what silent
  failure has cost here before.
- Anything that makes it easier to change `patterns.json` or the veto logic
  without the human review required above — for example, a "fast path" that
  skips review for small diffs.

## Reporting a bug

Include: the exact error string wake-watcher failed to classify (or
misclassified), the output of `python3 src/wake_watcher/classify.py
--check-string "<that string>"` (or `wake-watcherctl check "<that string>"` if
you installed it), and your Python version. Most failures here are either an unmatched error
string or an environment mismatch (wrong interpreter, wrong project root),
and those two things together usually show which.

If the bug is security-relevant, read `SECURITY.md` first and do not open a
public issue.

## Where things are

| | |
|---|---|
| `src/wake_watcher/wake_watcher.py` | the daemon: scan loop, wake decision, PTY injection |
| `src/wake_watcher/classify.py` | error classifier — reads `patterns.json`, doesn't embed rules |
| `src/wake_watcher/patterns.json` | the allow/veto rule data — the file most PRs will actually touch |
| `bin/wake-watcherctl` | start/stop/status/once/dry/tail/init wrapper |
| `packaging/launchd/`, `packaging/systemd/` | service templates used by `ctl init` |
| `scripts/run-tests.sh` | runs the test suite — see above |
| `scripts/release-gate.sh` | full release gate: suite + patterns regression + scrub check |
| `tests/` | the test manifest; `tests/manual/` needs a real API call and isn't run in CI |
| `docs/WHY.md` | why this exists and what it has actually caught |
| `THREAT-MODEL.md` | the honest risk read — read before touching anything in the review list above |

## Strings that must never be translated

Two kinds of string in this codebase look like leftovers from before the English
translation pass. They are not. Translating them breaks things silently — nothing
raises, a check just stops matching and quietly never fires again.

**1. Parse anchors.** `vitality_verdict()` reads the stdout of whatever external
command `WAKE_WATCHER_LIVENESS_CMD` points at. If that command speaks a language
other than English, the substring it is matched against has to stay in that
language. There are currently two such lines, both marked in place.

**2. Log phrases that tests assert on.** Several tests check visibility by
matching a fragment of a log line:

```python
_check("moved past the error" in log_text, "SKIP trace: ...")
```

This means log wording and test assertions are coupled **word for word**. Rewording
a log message without updating the assertion turns a real check into one that can
never fail again. If you touch a log line, grep the tests for a fragment of it.

This coupling is a known weakness, not a design goal — a future change should
assert on stable markers (`SKIP`, `WAKE`, a session id) instead of prose. Until
then, the two move together.

## Two failure modes this project keeps running into

Both cost real debugging time here, and both are easy to reproduce by accident.

**A green test suite that never ran what you think it ran.** Seven of the ten test
files report failures by returning a count rather than raising, so `pytest` scores
them as passing no matter what they found. This is why `scripts/run-tests.sh`
exists and why CI calls only that script — never bare `pytest`. There is a check in
the suite that fails if `ci.yml` ever grows a bare `pytest` invocation.

**Behaviour that only works on the machine it was written on.** The first CI run
after publication failed on every job, because the tests needed a `claude` binary
that happened to be on the author's PATH. The second failed only on macOS, because
the load gate deferred every wake on a busy runner. Neither was findable locally.
If you add a test that depends on something outside the repository — a binary,
spare CPU, a network route, a TTY — assume it will fail on someone else's machine
and give it a seam.
