# Threat Model

This is the risk-first read for wake-watcher. If you're deciding whether to
point this at a real project and only read one page before you do, make it
this one — not `README.md`, not `docs/WHY.md`.

## The biggest risk, stated first, not buried

wake-watcher's entire job is to make a stalled session continue. Waking a
session does not create new work — it resumes work that was already in
flight. That distinction is the whole risk: if the task's next step was
going to deploy something, spend money, or delete data, resuming it
executes that step. wake-watcher has no model of what the task is doing. It
reads infrastructure-level signals — an error string, a process state, a
file timestamp — and never the content or intent of the task itself. It
cannot tell the difference between resuming a linter run and resuming a
destructive operation; both look identical from where it sits.

Whatever confirmation exists before a dangerous next step has to come from
the task itself — an agent that pauses and asks a human before doing
something irreversible. wake-watcher does not disable or route around that
kind of gate: if the session it's waking would normally stop and ask, the
retry it delivers doesn't answer on anyone's behalf. But it also doesn't
add a gate of its own. If the task you point this at doesn't already stop
and confirm before its dangerous steps, waking it faster doesn't make it
safer — it just makes it faster.

Every safeguard described below narrows *when* wake-watcher acts. None of
them inspect *what* the session it wakes is about to do. Keep that
separation in mind for everything that follows.

## Three surfaces with outsized blast radius

These three are independent of each other — a misconfiguration in one
doesn't require a misconfiguration in another to cause damage. Each one, on
its own, is enough to matter, which is why each gets its own write-up
instead of a shared bullet list.

### 1. PTY injection: typing into a session that is still alive

The only way to hand a retry to a session that's still running in the
background — not dead, not orphaned, actually held by a live process — is
to simulate a human typing it. The mechanism: open a pseudo-terminal,
attach to the session, wait for its input box to actually render, write
the retry text as if it came from a keyboard, detach (`Ctrl+Z`) without
waiting for the session to react, and only then read the transcript back
to check whether anything happened. That's the whole sequence — not a
resume call, not an API request, an emulated keystroke sequence into a
live process's stdin.

This is the single highest-blast-radius primitive in this codebase.
Whatever the session does with that input is not something wake-watcher
reviews before it happens.

**It fails in production, and the record says so plainly.** There is a
documented case of a single session receiving six consecutive wake
attempts, and all six landed as delivered-but-not-rescued: the retry text
reached the input box, but no real turn followed within the acceptance
window (600 seconds) afterward. More than once in between, the injection
sequence itself came back unverified or failed outright. Every one of
those six surfaced a human-visible alert instead of quietly counting as a
success — but treat the underlying fact as representative, not as a bug
that's since been fixed: PTY injection depends on terminal render timing
it does not control, and that dependency is structural, not a bug to patch
away.

**The guard that decides whether to inject has a known history of being
under-tested — and the gap in it has since been closed, but it's worth
reading exactly how it failed, because the same shape of mistake is easy
to reproduce anywhere else a guard has more than one condition.** The
guard itself is a two-condition check: the target has to be a
background-kind session, *and* it has to carry an id. For a period, the
only test exercising the "don't inject" branch used a fixture that
happened to be missing that id field — so when the *first* condition (the
kind check) was deleted from the guard entirely, the test suite stayed
green, because the second condition was quietly carrying the whole check
by itself. Deleting one half of an `A and B` guard should turn a test red;
it didn't, because no fixture had ever isolated which half was actually
load-bearing. That gap is now closed by a test added specifically to pin
the condition the old fixture never isolated: a fixture that deliberately
satisfies every other condition and leaves only the one under test unmet,
asserting on the effect — a spy confirming the injection call was never
made — rather than on what a routing function happened to return. See
`CONTRIBUTING.md`'s second hard rule for the full account and the general
rule it became.

**On the opt-in switch: don't take it on faith that this primitive is off
by default in the version you're running.** This project's stated
direction is to put PTY injection behind its own separately-gated opt-in,
distinct from the default scan/wake path, precisely because it's the
highest-leverage primitive here. Verify what's actually true of your
release with `--help` or that release's README rather than assuming this
document describes current behavior.

### 2. `WAKE_WATCHER_LIVENESS_CMD`: an arbitrary command execution hook

If you set `WAKE_WATCHER_LIVENESS_CMD`, wake-watcher runs your command
template once per scan cycle and reads its output as a verdict on whether
a session should be treated as alive. Left unset — the default — this
entire step is skipped; that's a real, exercised code path, not just
"nobody's configured it yet."

Set it, and you've handed wake-watcher exactly the trust level you'd hand
a line in your crontab: whatever that command can do, wake-watcher can now
do on the same cadence as its poll loop, without asking first. Write and
review that command template the way you'd review a new cron entry, not
the way you'd review a config flag.

### 3. `patterns.json`: a data-poisoning surface

Classification rules — which error strings are safe to retry
automatically — live in a plain JSON file rather than in Python code.
That's a deliberate trade, and the benefit is real: fixing a
newly-worded error string means editing a text file, not shipping a code
change, so anyone running this can extend the ruleset for their own
environment without knowing Python.

The cost is the same fact seen from the other side: whoever can edit that
file decides which errors get retried automatically and which don't.
Mislabel a permission error or an account-restriction error as
transient — one line, in a data file, no code review required by the tool
itself — and wake-watcher will retry it forever instead of surfacing it,
because from its point of view that's exactly what an allow-rule means.

The regression suite that guards this file only protects rules that
already exist. A newly added allow-rule has no prior test to contradict,
so it merges on green CI with no automated signal about its effect at
all. This is why any change to `patterns.json`, or to the veto logic in
`classify.py` that reads it, requires a human security review before
merge — a passing test suite is necessary here, but it is not sufficient.
See `CONTRIBUTING.md`'s first hard rule.

## Default safeguards

None of the three surfaces above are removed by anything below — they're
bounded by it. This is what a stranger installing wake-watcher actually
gets, out of the box:

- **Off by default.** Installing wake-watcher does not start it. Nothing
  scans, nothing wakes, until you run `start` yourself.
- **Dry-run first.** The first command this project walks a new user
  through is a dry run, not a live one — see what it would do before it
  does anything.
- **`REQUIRE_DEAD`.** By default, wake-watcher only wakes a session that
  has no live process currently holding it — the intent is to never race
  a supervisor that's already driving the same session.
- **Project-root scoping.** By default, wake-watcher only acts on sessions
  inside the project root it's pointed at, resolved through real
  filesystem paths, so a sibling directory with a similar name doesn't get
  swept in.
- **The do-not-wake list.** Re-read every scan cycle, takes effect
  immediately, and overrides every other signal — if a session is on it,
  nothing else above matters.
- **A hard-capped fast lane and an hourly-scale slow lane.** After a small
  number of attempts on the same session, wake-watcher backs off to an
  interval measured in hours, not seconds. It never degenerates into a
  fast retry loop — an overnight incident in this project's history was
  exactly that failure mode, a retry loop that misread its own echoed
  output as progress and stacked processes until the machine went down.
  The two-lane design exists specifically so that shape of failure can't
  recur.
- **Network-down defer.** If the machine itself can't reach the network,
  wake-watcher defers the whole scan rather than treating every session on
  it as an independent fresh failure to react to — a disconnected machine
  doesn't burn through any session's wake budget.

## What's tested, and what isn't

The honest version of "this is safe" isn't a coverage percentage — it's
being able to say exactly which mechanisms would catch a real regression
and which wouldn't. The way to find that out is to break each one on
purpose and see whether a test notices.

**The decision layer — whether to wake a session at all, and which
one — is mutation-tested.** Seven separate contracts were each
deliberately broken, one at a time: the allow-list check, the
network-defer check, project-root scoping, the do-not-wake list, the
wake-count cap, the forward-progress watermark, and the PTY-injection
routing guard described above. In every case, the test suite caught it —
turned red the moment the underlying judgment was flipped or disabled.
This is the layer that determines whether the tool over-wakes, and it's
the layer with the strongest evidence behind it.

**The execution layer — actually typing into a live PTY, actually
checking whether a process is alive, actually judging whether a wake
landed within its acceptance window — is exercised constantly in
production, but automated coverage does not meaningfully pin it down
yet.** The liveness check behind `REQUIRE_DEAD` and the window comparison
inside the dual-signal rescue check both survive the same kind of
deliberate mutation that the decision layer catches without a test
noticing; so does the deeper mechanics of PTY injection itself, beyond the
routing guard covered above. None of this is silent in production — the
fail-loud posture described throughout this document applies here too —
but "a human will notice if it's wrong" and "a test will catch it before
it ships" are different claims, and this document isn't going to blur
them into one.

A project that can say precisely what it has and hasn't pinned down is
more trustworthy than one that only reports a coverage number.

## Compatibility is itself a risk surface

Nothing about how wake-watcher reads Claude Code's state is documented or
stable by contract. It depends on the shape of the on-disk job state
file, the shape of transcript `.jsonl` records, the output of
`claude agents --json`, and the render timing of the `claude attach` TUI
that PTY injection depends on. All of it is internal to a product that
iterates quickly, and none of it is a published interface wake-watcher
was given permission to rely on.

That means an upstream change can silently break this tool. The design
response to that risk is a principle, not a promise: **degrade loudly,
don't degrade silently.** If an expected internal structure can't be
read, that has to surface as a clear warning and a refusal to act — never
as a scan loop that stays alive, keeps its heartbeat fresh, and
faithfully finds nothing to do all night while looking, from the outside,
exactly like it's working.

## CI is part of the attack surface too

A community pull request that edits `patterns.json` is the textbook shape
of untrusted input arriving from a fork — a plain-text change that
directly decides which real failures get retried automatically,
submitted by someone who, by definition, hasn't been vetted. The workflow
that runs on such a PR must use `pull_request`, never
`pull_request_target` — the latter runs with the base repository's
permissions and secrets against code checked out from the fork, which is
exactly the confusion that turns a review-bait PR into a supply-chain
incident. And no workflow reachable by a fork-originated PR should carry
any secret at all, regardless of which trigger it uses. Hold both facts at
once: CI here runs at zero cost against public data, and it is also,
structurally, a place where someone else's input gets executed against
your infrastructure.

## Out of scope: credentials

wake-watcher holds no credential of its own. It authenticates as you by
invoking your already-authenticated `claude` CLI — the same binary you'd
run by hand from a terminal — and stores no API key, no OAuth token,
nothing that would let someone else act as you if this repository, or a
machine running it, were compromised. That's a structural property, not a
configuration choice: there is no credential file here to secure in the
first place, and no "what happens if it leaks" question to answer for
this specific tool.

What local state it does write — a log, a record of what it's woken and
when — contains session ids and working-directory paths, which is a
privacy consideration, not a credential one; see `SECURITY.md` for how
that's handled. Every risk described above is about *leverage* — what a
live session, a command hook, or a rule file lets wake-watcher do — not
about *identity theft*. Keep that distinction in mind reading everything
above it: none of it is asking "can someone steal your account," all of
it is asking "can someone make your own already-trusted tools do
something you didn't want." ---

For what this tool does and how to run it, see `README.md`. For why each
safeguard above exists in the specific shape it does — including the
incidents that produced them — see `docs/WHY.md`. For the review
requirements these risks impose on contributors, see `CONTRIBUTING.md`.
To report a vulnerability, see `SECURITY.md`.
