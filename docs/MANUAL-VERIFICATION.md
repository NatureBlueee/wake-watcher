# Manual verification

Everything in `tests/` runs for free: no API calls, no live sessions, no network.
That is deliberate — a test suite you hesitate to run is a test suite you stop
running. But two things cannot be established that way, and this file records
what was done about them.

---

## 1. The end-to-end test (`tests/manual/test_e2e.py`)

**Why it is not in CI.** It creates a real `claude -p` session and really resumes
it, so it costs real quota. Running it on every push would make the suite
something you avoid.

**What it actually proves, and what it fakes.** The trigger is fabricated: a
`state.json` whose `detail` field contains a genuine transient error string. We
cannot make the API drop a connection on demand. Everything after that is real —
a real `claude --resume` against a real session, verified by having the woken
session recall a codeword it was told before the interruption. If it recalls the
codeword, the session continued its original context rather than starting fresh.

### Run of 2026-08-19

Run after the extraction, the English translation pass, and eight declared
changes — specifically to answer one question:

> **Are the free test seams (`--dry-run`, `FAKE_DELIVER`, `FAKE_AGENTS`, the stub
> binary) a faithful proxy for real resume behaviour?**

Predictions were written down *before* the run (kept out of this repository, with
the rest of the extraction working notes) so the result could falsify them rather
than be rationalised afterwards.

**Result — the core prediction held:**

```
=== TEST 2: REAL wake delivery + continuation (resume real session) ===
  verifying real continuation (resume + recall codeword)...
  [PASS] session continued context (codeword recalled: True)
  [PASS] awaiting-owner session was NOT woken
```

The delivery chain survived the extraction intact. The free suite's green is
trustworthy, so later work does not need to spend quota again to find that out.

**The same run also exposed a defect in the test itself.** Every "should wake"
assertion returned zero. The cause was not delivery: the sandbox job's `cwd` lives
under a temp directory, and `WAKE_WATCHER_PROJECT_ROOT` defaults to the source
tree, so the this-project filter skipped every candidate. The dry-run helper had
already disabled that filter; the real-delivery path had not. Diffing against the
pre-extraction copy showed the same gap there — this test could not have passed in
its original home either. Fixed by setting `WAKE_WATCHER_PROJECT_ROOT=""` on both
paths.

Worth stating, since it is the general lesson: *"the wake never happened"* and
*"the wake was correctly skipped"* look identical in a pass/fail count. Only the
log distinguishes them.

### Running it yourself

```sh
python3 tests/manual/test_e2e.py
```

Needs a working `claude` CLI and available quota. It creates one session and
resumes it; nothing is left running afterwards.

---

## 2. PTY injection against a live agent

`_pty_attach_inject` opens a pty, runs `claude attach`, waits for the input box to
render, types, detaches, and then reads the transcript to confirm a user turn
actually landed. The routing around it is unit-tested, and the mutation gate
verifies the `kind == "background"` guard is genuinely load-bearing. The injection
itself is not exercised automatically: it needs a live background agent and a
human watching a terminal.

If you want to exercise it, the procedure is written out at the bottom of
`tests/test_attach_inject_routing.py`. Note that PTY injection ships **disabled**
(`WAKE_WATCHER_ENABLE_PTY_INJECT=0`); you have to turn it on first, and you should
read THREAT-MODEL.md before you do.

**Honest status:** this path has run in production many times and has also failed
in production — one session took six consecutive wake attempts, each delivered and
none continuing. That case is described in README.md. It is the least
automatically-covered code in the repository, which is why it is off by default.

---

## 3. What is covered automatically, for contrast

- Ten deterministic test files, no API cost, run via `scripts/run-tests.sh`
- A mutation gate (`scripts/verify-mutations.py`) that breaks each of seven safety
  contracts in turn and asserts a test goes red — coverage says a line ran, this
  says you would find out if it were wrong
- CI on Ubuntu and macOS, Python 3.9 and 3.13
- A Linux job that renders the systemd unit and has systemd accept it
