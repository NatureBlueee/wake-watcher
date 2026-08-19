# Security

## What this program holds

Nothing that authenticates as you. wake-watcher stores or reads no
credential of its own — when it acts, it does so by invoking your
already-authenticated `claude` CLI, the same way you would by hand from a
terminal. That's a structural difference from this project's sibling,
`quotapool`, which does hold OAuth credentials on disk: there's no
credential file here to secure in the first place, and no "what happens if
it leaks" question to answer.

What local state it *does* write — a log, a ledger of what it's woken and
when — contains **session ids and working-directory paths** from your
machine. Treat those the way you'd treat shell history: not secret, but not
something to paste into a public issue or a bug report either.
`scripts/scrub-check.sh` exists to keep them out of *this repository*; it
doesn't reach into your own runtime log files.

## Reporting a vulnerability

Open a GitHub security advisory (Security → Report a vulnerability) rather
than a public issue, and include a way to reproduce it. If you'd rather not
use GitHub, open an issue titled "security contact request" with no details
in it and we'll find another channel.

There is no bounty. There is a commitment to answer.

## The three surfaces that matter

wake-watcher's whole job is to make a stalled session continue. If the
session's next step was going to deploy something, move money, or delete
data, continuing does that step — nothing in this tool asks a human first,
because "resume without asking" is the entire feature. So the places that
matter most for security review are the ones with more leverage than "read
a log file and decide":

### 1. PTY injection

The only way to hand input to a session that's still *running* in the
background is to open a pseudo-terminal and simulate keystrokes into its
input box — `claude attach` doesn't accept a payload, only a human paying
attention does, so this path imitates one. That is the single
highest-blast-radius primitive in this codebase: it types into a live
session, and whatever that session does next is not something wake-watcher
reviews first.

What actually bounds it: it only targets sessions of kind `"background"`.
Interactive sessions are structurally excluded from this path — they don't
carry the id field it keys on, and are only ever surfaced for a human to
look at, never typed into. That guard is a two-condition check, and it has
a known history of weak test coverage — see `CONTRIBUTING.md`'s second hard
rule for the specific way that failed and the pinning discipline now
required around it. A dedicated, separately-gated opt-in for this primitive
— rather than it being reachable anywhere on the default scan/wake path —
is this project's stated direction. Don't assume that gating is already in
place for the version you're running; check that release's `--help` /
README for what's actually true of it.

### 2. `WAKE_WATCHER_LIVENESS_CMD`

This is an arbitrary-command-execution hook, plainly stated: if you set it,
wake-watcher runs your command template once per scan cycle (every ~30
seconds with the default interval) and reads its stdout as a verdict. Left
unset — the default — this whole layer is skipped; that's a real, exercised
code path, not just "nobody's configured it yet."

Set it, and you've handed wake-watcher the same trust you'd hand a line in
your crontab: whatever that command can do, wake-watcher can now do on the
same cadence, without asking. Review the command template the way you'd
review a new cron entry, not the way you'd review a config toggle.

### 3. `patterns.json` as a data-poisoning surface

The retry allow/veto rules live in a plain JSON file rather than in Python,
deliberately, so that fixing a newly-worded error string doesn't require
knowing Python — you edit a data file, not a script. That's a real
usability win, and it's also, honestly, a lowered bar: anyone who can edit a
text file can now decide which errors get retried automatically and which
don't, including turning an error that should never be retried (a
permission failure, an account restriction) into one that silently loops.

The regression suite protects rules that already exist; it structurally
cannot protect a *newly added* rule, because there's no history for a new
rule to contradict. That's why `CONTRIBUTING.md` requires a human security
review for any change to this file, or to the veto logic in `classify.py`
that reads it — a green CI run is necessary there, not sufficient.

## Emergency shutdown

wake-watcher does not start on install — you run `start` explicitly. If you
never have, there's nothing running to shut down.

For a running instance:

1. `wake-watcherctl stop` — the intended lever. Stops the scan loop for
   that instance.
2. If that doesn't land — the process is wedged, or you don't trust it to
   honor its own stop path — go around it at the OS level, which works
   regardless of what the daemon's own code is doing:
   - macOS: `launchctl bootout gui/$(id -u) <label>` (the label is whatever
     `ctl init` registered; `launchctl list | grep wake-watcher` finds it)
   - Linux: `systemctl --user stop wake-watcher@<name>`
3. As an absolute last resort, `wake-watcherctl status` prints the pid it's
   tracking; kill that directly.

Any of the three stops the scan loop, which stops everything downstream of
it — no wake, no PTY injection, no `LIVENESS_CMD` execution. There's no
separate "kill switch" beyond stopping the process, because the process is
the only thing that ever acts.
