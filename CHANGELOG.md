# Changelog

Notable changes. The format is loosely [Keep a Changelog](https://keepachangelog.com);
versions follow [SemVer](https://semver.org), and while the major is `0` the minor
may still change behaviour.

The git history of the private repository this was extracted from is not
published — it carries machine paths, session ids and internal project names.
This file is the public record of what changed and why.

## [0.1.1] — 2026-08-19

Three defects, all found by running the code somewhere other than the machine it
was written on, and all invisible to a fully green local suite.

### Fixed

- **The tests needed a `claude` binary that only the author's machine had.** Every
  job on the first CI run failed identically: delivery short-circuits when the
  binary cannot be resolved, so every wake-related assertion collapsed into "no
  wake happened". `run-tests.sh` now writes a stub into its throwaway state
  directory. The stub exits non-zero if anything ever actually runs it, so this
  cannot quietly become a test that passes by executing a fake.
- **The tests' result depended on how busy the host was.** The load gate defers a
  wake when `load1 > cores × 1.5`. That is right in production — an overloaded
  machine is the last place to start another cold-loading process — but it made
  the outcome a function of the runner. CI runners are small and busy; a laptop is
  neither. Reproduced locally by setting the factor to 0.001, which produced the
  identical failing assertion. Tests now pin it high; the gate's own behaviour is
  still covered by the mutation suite.
- **The end-to-end test never woke anything, in any checkout.** Its sandbox job's
  `cwd` lives under a temp directory, so the this-project filter skipped every
  candidate. The dry-run helper disabled that filter; the real-delivery path did
  not. Diffing against the pre-extraction copy shows the same gap — this test could
  not have passed in its original home either.

### Added

- A CI job that renders the systemd unit through `wake-watcherctl init` on a real
  Ubuntu VM, has systemd itself accept it (`systemd-analyze verify`, systemd 255),
  asserts it runs non-root with `NoNewPrivileges=yes`, and starts the daemon once.
  The unit shipped in 0.1.0 had never been executed anywhere.
- `docs/MANUAL-VERIFICATION.md` — what the one paid end-to-end run established, and
  what it faked.
- CONTRIBUTING now names the strings that must never be translated, and the two
  failure modes this project keeps hitting.

## [0.1.0] — 2026-08-19

First release. Everything here ran in production for two months across six
projects before it was published; none of it is aspirational.

### What it does

Polls for Claude Code sessions that stopped mid-turn on a transient
infrastructure error — connection closed, rate limit, 5xx, overloaded — and sends
them a retry so the work continues. Sessions that failed for real reasons are left
alone.

**As of 2026-08-18**: 167 sessions were woken a combined 419 times; 130 of them —
about 78% — were confirmed to have actually resumed. Confirmed means both signals:
the retry was delivered *and* a real turn followed inside the window. Delivery
alone never counts as a rescue.

### The parts that took incidents to learn

- **Stop/SubagentStop hooks cannot see this failure.** When a connection drops
  mid-response the agent never finishes its turn, so the hook never fires. External
  polling is not a design preference; it is the only thing that observes this.
- **`claude --resume` is rejected for a live background agent.** The CLI refuses.
  The only remaining primitive is typing into its input box through a pty — which
  is why that code exists, and why it ships disabled.
- **Three detection axes were tried and rejected**, each after a real incident. The
  worst: treating transcript file growth as progress. The watcher's own resume
  appends to the file, so the file always grows, so "it recovered" was always true,
  so the budget reset forever — a 60-second infinite wake loop that cold-loaded a
  3GB process each time and took nine hours to bring the machine down.
- **A heartbeat must be decoupled from the business log.** An idle poll writes
  nothing, so a quiet log meant "dead" and healthy processes were killed roughly
  40 times a day until the heartbeat got its own file.
- **Default-deny.** An unrecognised error is never woken. Missing a rescue costs a
  manual retry; a wrong one can mask a real failure or loop forever.

See `docs/WHY.md` for the full account.

### Verification that ships with it

- Ten deterministic test files, no API cost, no network, no live sessions
- A mutation gate that breaks each of seven safety contracts in turn and asserts a
  test goes red — coverage says a line ran; this says you would find out if it
  were wrong
- CI on Ubuntu and macOS, Python 3.9 and 3.13

### Known boundaries, stated up front

- Interactive main sessions can be flagged but never auto-resumed: `claude attach`
  only supports background sessions. That is a CLI boundary, not an oversight.
- PTY injection is off by default. It has worked in production and it has also
  failed there — one session took six consecutive attempts, each delivered, none
  continuing.
- It reads undocumented Claude Code internals (`~/.claude/jobs`, transcript shape,
  `claude agents --json`) and will need updating when they change. It degrades
  loudly rather than silently when they do.
