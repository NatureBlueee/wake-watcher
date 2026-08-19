# Why wake-watcher works the way it does

<!-- FILL: 2-4 sentence framing. This document is the moat: every rule below was paid for
by a real production incident, not designed on a whiteboard. Point readers at README.md for
what the tool does and how to run it; this document is only about why each mechanism exists
in the specific shape it does, and what it cost to learn that shape. Source material for the
whole file: the project's own incident write-ups and its README's transient-error catalog —
translate and reorganize, do not invent new claims. --> ---

## <a id="hook-blind-spot"></a> 1. Stop/SubagentStop hooks never fire for this failure mode

<!-- FILL: A background job cut off mid-response by a transient connection error leaves a
hook timeline with a single "blocked" entry and nothing else — Stop/SubagentStop only fire
on normal turn completion, and a mid-stream disconnect is not a normal turn completion.
Conclusion: this class of death is structurally invisible to hooks; detection has to be an
external poller reading state from outside the turn lifecycle, not a hook waiting to be
called. -->

## <a id="daemon-respawn-gap"></a> 2. A daemon's own respawn logic doesn't cover this

<!-- FILL: A supervising daemon's respawn/adopt path only re-attaches jobs when the daemon
itself restarts. A job that is still running and gets cut mid-turn by a transient error gets
zero reaction from that path — respawn and "recover a live job stuck on a transient error"
are different problems, and solving the first does not solve the second. -->

## <a id="resume-rejected"></a> 3. `claude --resume` is refused for live background agents

<!-- FILL: The CLI rejects `--resume` outright ("Session ... is currently running as a
background agent") whenever a supervisor still holds a live process for that session. The
only primitive left for a *live* stuck agent is injecting the retry as if a human typed it
into the agent's own terminal/input — not resuming the session from outside. Draw the line
clearly: dead orphans (process actually exited) can be resumed; live daemon-held agents
cannot, and code that assumes otherwise will discover this in production, not in testing. -->

## <a id="rejected-detection-axes"></a> 4. Three detection axes we tried and killed

<!-- FILL: One-sentence intro — none of these three were hypothetical mistakes caught in
review; each one shipped, ran in production, and was killed by a specific incident. -->

### <a id="stale-detail-field"></a> 4a. Reading a single "last known status" field

<!-- FILL: A status field that records the *last* error seen stays populated after the agent
has already moved past it and produced normal output. Watching that field means watching
history, not the present — it re-wakes sessions that already recovered. -->

### <a id="progress-signal"></a> 4b. Using file growth as a progress signal

<!-- FILL: THIS IS THE LOAD-BEARING SUBSECTION — code comments in the source already link
here (`docs/WHY.md#progress-signal`), keep the anchor exactly as-is. Content: treating
transcript file size/mtime growth as "it made progress" is circular — the watcher's own
retry-delivery appends to that same file, so the watcher's own echo gets misread as
recovery, which resets the wake budget, which combines with "never give up" backoff into an
unbounded fast retry loop, each cycle cold-loading a multi-gigabyte process. Net result: a
~9 hour overnight cascade that stacked processes until the machine went down. This is the
single most expensive lesson in this project's history — give it real weight, not a
one-liner. -->

### <a id="error-signature-dedup"></a> 4c. Deduplicating by error signature

<!-- FILL: The same transient error string can legitimately recur many times in a row — each
occurrence is an independent real transient failure, not a repeat of the same one. "Has the
error signature changed" is the wrong question; "is the agent stuck on it *right now*" is
the right one. -->

## <a id="heartbeat-decoupling"></a> 5. Heartbeat must be decoupled from business logs

<!-- FILL: Using a business/activity log's mtime as a liveness proxy kills processes that
are alive but idle by design (no candidates this cycle → no log line written, on purpose).
Measured: on the order of dozens of false kills in a single day before this was separated
out. The fix is a heartbeat file touched every poll cycle regardless of whether anything
happened — liveness and "did work happen" are different questions and must not share a
signal. -->

## <a id="require-dead"></a> 6. `REQUIRE_DEAD` is the hardest brake in the system

<!-- FILL: Default posture: only wake a session that has no live process currently holding
it, specifically to avoid racing a supervisor that is already driving the same session
(double-drive). Historically credited with blocking a large number of would-be double-drive
attempts in a single high-traffic night; be honest that today's log format can only show the
*configuration* was on, not a per-block counter — describe this as a narrative data point
with a known gap, and note the fix is logging each block as a countable event, not retrofitting
a precise historical count. -->

## <a id="default-deny-asymmetry"></a> 7. Default-deny is a deliberate, asymmetric cost trade

<!-- FILL: An error string the classifier has never seen is denied by default, not allowed by
default. The two failure directions are not equally bad: under-waking costs a human
eventually nudging one stuck session; over-waking risks masking a real, non-transient
failure or feeding a retry loop. The allowlist itself is built from strings actually observed
in the wild with real occurrence counts, not a guess at what "transient" errors are supposed
to look like — that's why it keeps growing as new client versions phrase errors differently. -->

## <a id="track-record"></a> 8. The track record, as of 2026-08-18

<!-- FILL: Lead with the session-level rescue rate, not the raw event count — it is the more
honest and more persuasive number. Across roughly two months and a handful of projects
(generalize as "project A/B/C" or report only the aggregate — never real project/instance
names): 167 distinct sessions were woken a combined 419 times; 130 of those sessions (~78%)
were confirmed rescued under a dual-signal check (message delivered AND a real non-error
turn actually followed within the window — not delivery alone). State plainly that the other
~22% were not silently written off: every one of them surfaced a human-visible alert instead
of pretending to succeed, and that honesty is itself part of the pitch, not a caveat to bury.
Optional: one concrete case study of an honest all-failure run (repeated wakes on a single
session that all landed as "delivered but not rescued") makes a better trust argument than
a clean scoreboard would — it shows the dual-signal check and slow-lane backoff both doing
their job instead of reporting a false win. Do not name the project/instance involved. --> ---

## <a id="what-we-can-and-cannot-test"></a> 9. What we can and cannot test

<!-- FILL: Lead with the honest split, sourced from mutation testing every trust-bearing
mechanism above (flip its core judgment, see if a test goes red). Roughly two-thirds of the
mechanisms — the allowlist, network-defer, project-root scoping, do-not-wake, the wake cap,
the watermark — are caught immediately: these are the decision layer (whether to wake, whom
to wake), pure functions, cheap to test, and they're the layer that determines whether the
tool over-wakes. The remainder — the injection safety guard, the liveness check behind
REQUIRE_DEAD, and the dual-signal acceptance check itself — survive mutation untouched: this
is the execution layer (terminal injection, process liveness, wall-clock verification
windows), it touches real processes and real time, it runs constantly in production, but
automated coverage does not meaningfully pin it down yet. State the general lesson plainly:
a test asserting a routing function's return value is not the same as a test asserting a
dangerous action never happened — the former can stay green after the safety condition it
claims to guard has been deleted underneath it. A project that can say exactly what it does
and doesn't have pinned down is more trustworthy than one that only reports a coverage
percentage. -->

### <a id="docs-drift"></a> 9a. Documentation can drift from implementation

<!-- FILL — EXACT SHAPE REQUIRED, DO NOT EXPAND: use only this flattened framing, do not
name or describe any internal ledger/bookkeeping/debt-tracking/self-check system, do not
describe how the gap was found, do not use any internal project or system names:

"A fix was once documented as done when the implementation behind it had never actually
been written; the test for it sat red for weeks because no CI was running it."

Tie it directly to this repo's own posture, in the spirit of (not verbatim, translate
naturally): this is why CI here is a merge-blocking gate rather than a courtesy check — a
red test nothing runs is worse than no test at all, which is exactly what the mutation
testing in section 9 above exists to make sure the *green* tests aren't quietly doing
either. --> ---

<!-- FILL (optional, keep short if included): pointer to README.md for what the tool does
and how to run it, and to THREAT-MODEL.md for the safety posture referenced in sections 3
and 6-7 above. Do not add new claims here — cross-reference only. -->
