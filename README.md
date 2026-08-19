# wake-watcher

**English** · [简体中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)
[![Zero dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#no-dependencies)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

**Nothing to do with sleep.** wake-watcher watches for Claude Code agents that
stalled mid-turn on a transient error, and sends them the retry that gets them
moving again.

Here is the failure it exists for. A background session is forty minutes into a
task. The connection drops mid-response. The turn never completes — so
`Stop`/`SubagentStop` never fire, nothing crashes, nothing is recorded as
failed. The session just sits there holding work nobody will collect, until you
happen to look. What it needed was one word: `retry`.

wake-watcher is a local daemon that looks so you don't have to. It polls, checks
the error against a rule file, wakes only what is safe to wake, and then goes
back to verify whether the session actually resumed.

**Track record, as of 2026-08-18.** Across roughly two months and six project
instances: **167 sessions were woken a combined 419 times, and 130 of those
sessions — about 78% — were confirmed to have actually resumed.** Confirmed
means both signals: the retry was delivered *and* a real turn followed inside
the acceptance window. Delivery alone never counts as a rescue. The other ~22%
were not written off quietly — every one raised a human-visible alert. ---

## Read this before you install it

Waking a session does not create new work. It resumes work that was already in
flight — and that is the whole risk. **If the task's next step was going to
deploy something, spend money, or delete data, resuming it executes that step.**
wake-watcher has no model of what your task is doing; it reads an error string,
a process state, a file timestamp. Resuming a linter and resuming a destructive
operation look identical from where it sits.

Whatever asks a human before something irreversible has to come from the task
itself. wake-watcher does not route around such a gate — the retry it delivers
answers nothing on your behalf — but it does not add one either.

So the defaults are deliberately timid:

- **Installing it does not start it.** Nothing scans, nothing wakes, until you
  say so.
- **The first command this README gives you is a dry run**, not a live one.
  Watch it decide for a day before it is allowed to act.

[`THREAT-MODEL.md`](THREAT-MODEL.md) is the risk-first read: the three surfaces
with outsized blast radius, what is tested and what isn't, and why an upstream
change could break this tool. If you only read one page before pointing this at
a real project, read that one instead of this one. ---

## What it wakes, and what it will not touch

The rules live in [`src/wake_watcher/patterns.json`](src/wake_watcher/patterns.json)
as data, not in code — every entry carries the reason it is there. Anything the
classifier has not seen before is **denied by default**: an unrecognized error
is a human's problem, not a retry candidate.

| Woken — transient infrastructure | Never woken — a real answer |
|---|---|
| `connection closed mid-response`, `connection closed while thinking`, `stream idle timeout`, `unable to connect to api` | `403`, `request not allowed`, `permission denied` |
| `rate limited` (explicitly *not your usage limit*), `server is temporarily limiting requests`, `overloaded_error` | `usage limit reached`, `quota exceeded`, `out of credits`, `insufficient credits` |
| `API Error: 500 / 502 / 503 / 529`, `503 service unavailable`, `internal server error` | `user rejected` / `cancelled` / `interrupted`, `denied` |
| `this is not your fault`, `please retry`, `temporarily unavailable` | `authentication failed`, `invalid api key`, and `400` request-construction errors |

The asymmetry is on purpose. Under-waking costs you a nudge on one stuck
session; over-waking risks retrying a real failure forever instead of showing it
to you.

To check a string you are unsure about, without running anything:

```sh
wake-watcher --check-string "<the exact error text>"
``` ---

## Install

Requires Python 3.9+ and Claude Code already installed. macOS or Linux.

```sh
git clone https://github.com/NatureBlueee/wake-watcher ~/.local/share/wake-watcher
cd ~/.local/share/wake-watcher
./install.sh --dry-run     # every change it would make, in full
./install.sh
```

That puts one command, `wake-watcherctl`, on your PATH. It starts nothing and
registers no service — wake-watcher is opt-in per project.

Now go to the project you want watched:

```sh
cd /path/to/your/project
wake-watcherctl dry              # foreground, wakes nothing — start here
```

`dry` runs the full scan loop and prints every decision it *would* have made.
Leave it running through a real working session. When what it would have done
matches what you would have done:

```sh
wake-watcherctl init             # a launchd/systemd service scoped to THIS directory
```

`./install.sh --uninstall` puts the machine back.

### The commands

| | |
|---|---|
| `wake-watcherctl dry [name]` | foreground, `--dry-run`. Decides, prints, wakes nothing. **Do this before `start` or `init`.** |
| `wake-watcherctl once [name]` | one real scan pass, then exit. This one **can** wake something — it is for debugging, not for a safe trial. |
| `wake-watcherctl start [name]` | background process, no OS service. Dies at logout or reboot. |
| `wake-watcherctl stop [name]` | stop the process `start` began |
| `wake-watcherctl status [name]` | running or not, and the pid |
| `wake-watcherctl tail [name] [n]` | last n lines of that instance's log (default 40) |
| `wake-watcherctl init [name]` | generate and load a launchd/systemd service for the current directory |
| `wake-watcherctl uninstall [name]` | remove that service; log and state are kept |

`name` identifies one instance and defaults to the current directory's basename.
One name = one project = one state directory = one service, which is what lets a
single install watch several projects at once without them colliding.

The project being watched is always the directory you ran `init` from — baked in
explicitly, every time, because the code's own fallback derives a project root
from where wake-watcher itself is installed, which is not what you want once it
lives in a shared location.

To stop everything in a hurry, see the emergency shutdown steps in
[`SECURITY.md`](SECURITY.md). Stopping the process stops all of it: no scan, no
wake, no command hook. ---

## How it decides

Six brakes, each of them the residue of something that went wrong:

- **Only wake what nothing else is driving.** By default a session is a
  candidate only when no live process still holds it — so wake-watcher never
  races a supervisor already working the same session.
- **Project-root scoping.** It acts only on sessions under the project root it
  was pointed at, resolved through real filesystem paths, so a sibling directory
  with a similar name is not swept in.
- **A do-not-wake list.** Re-read every cycle, effective immediately, and it
  overrides every other signal.
- **A hard-capped fast lane, then an hourly slow lane.** After a few attempts on
  the same session, the interval becomes hours rather than seconds. It cannot
  degenerate into a fast retry loop — an overnight incident in this project's
  history was exactly that, a loop that misread its own echoed output as
  progress and stacked processes until the machine went down.
- **Network-down defer.** If the machine itself cannot reach the network, the
  whole scan defers rather than treating every session as a fresh failure. A
  disconnected laptop burns nobody's wake budget.
- **Dual-signal acceptance.** A wake counts as a rescue only when the message
  landed *and* a real non-error turn followed within the window. "The message
  was delivered" is not "the agent came back."

[`docs/WHY.md`](docs/WHY.md) has the incident behind each one — including the
three detection approaches that shipped, ran in production, and were killed.

### What is tested, and what isn't

The decision layer — whether to wake at all, and which session — is
mutation-tested: seven separate contracts were each deliberately broken, one at
a time, and the suite went red every time. The execution layer — typing into a
live terminal, checking whether a process is alive, judging the acceptance
window — runs constantly in production but is **not** meaningfully pinned down
by automated tests yet. "A human will notice" and "a test will catch it before
it ships" are different claims, and [`THREAT-MODEL.md`](THREAT-MODEL.md) keeps
them apart rather than reporting one coverage number. ---

## A run where it failed six times in a row

One session received six consecutive wake attempts. All six landed as
*delivered but not rescued*: the retry text reached the input box, and no real
turn followed within the 600-second window. Zero for six.

That record is here on purpose, because of what did **not** happen:

- Not one of the six was counted as a success. The dual-signal check refuses to
  score "the message arrived" as "the agent is working again," which is exactly
  the number a less honest tool would have reported.
- It did not turn into a 60-second retry loop. After the fast lane's cap, that
  session dropped to the hourly slow lane and stayed there.
- Every one of the six surfaced a human-visible alert.

PTY injection depends on terminal render timing it does not control, and that
dependency is structural — not a bug that has since been fixed. A tool that will
sometimes fail to rescue a session, and tells you plainly when it failed, is
worth more than one that only reports its wins. ---

## What it cannot do

Boundaries, stated up front so you can check them against what you need:

- **Interactive sessions get an alert, not a rescue.** `claude attach` supports
  background sessions only — that is the current CLI's boundary, not a decision
  taken here. Your main interactive window can be flagged for you to look at; it
  cannot be continued automatically.
- **It depends on undocumented internals.** The on-disk job state file, the
  shape of transcript records, `claude agents --json`, the render timing of the
  attach TUI — none of that is a published interface. An upstream change can
  break this tool. The design answer is to **degrade loudly**: refuse to act and
  say so, never keep a cheerful heartbeat while silently finding nothing.
- **Linux is verified in CI, but the track record is macOS's.** Every push runs a
  job on a real Ubuntu VM that renders the systemd unit through `wake-watcherctl
  init`, has systemd itself accept it (`systemd-analyze verify`, systemd 255),
  asserts it runs non-root with `NoNewPrivileges=yes`, and starts the daemon once.
  What that does *not* cover: the numbers above were earned on macOS/launchd over
  two months. Nobody has yet run this on Linux for weeks against real traffic.
  The systemd template installs a *system* unit under `/etc/systemd/system/` and
  needs sudo. Reports from long-running Linux installs are the thing this project
  most wants to hear about.
- **No Windows.** PTY injection is built on `pty.fork()`, which is POSIX-only.

### PTY injection is opt-in and off

Handing a retry to a session that is still alive means simulating a human typing
it: open a pseudo-terminal, attach, wait for the input box to render, write the
text, detach. It is the highest-blast-radius primitive in this codebase — whatever
the session then does is not something wake-watcher reviews first.

It lives behind its own switch, `WAKE_WATCHER_ENABLE_PTY_INJECT`, and is **off
unless you set it explicitly**. Before you do, read
[`THREAT-MODEL.md`](THREAT-MODEL.md) § *PTY injection*.

The same section covers `WAKE_WATCHER_LIVENESS_CMD`, which is an
arbitrary-command-execution hook: unset by default, and if you set it, review
that template the way you would review a new crontab line. ---

## No dependencies

`pyproject.toml` declares no runtime dependencies. Python's standard library,
nothing else, Python 3.9+.

That is a property, not an aesthetic. This is a daemon that runs unattended
under the system Python and pokes other unattended sessions back to life; a
dependency tree inside that process is a liability nobody is watching.
`pip install -e '.[dev]'` adds exactly one thing, `pytest`, and only for tests. ---

## Its sibling: quotapool

[quotapool](https://github.com/NatureBlueee/quotapool) treats a different
disease. It handles **"the quota window is spent, so everything stops"** — and it
needs you to hold more than one Claude subscription for the pooling to mean
anything. wake-watcher handles **"a network or server-side error stalled one
session mid-turn"**, which happens on a single account, on a single machine, at
three in the morning.

Neither replaces the other. Running out of quota and being cut off mid-response
are different failures with different fixes; if you have one subscription, you
still want this one. ---

## The rest of the documentation

| | |
|---|---|
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | The risk-first read: three high-leverage surfaces, what is and isn't tested, compatibility as a risk |
| [`SECURITY.md`](SECURITY.md) | What it holds (no credentials), reporting a vulnerability, emergency shutdown |
| [`docs/WHY.md`](docs/WHY.md) | Why each mechanism has the shape it does, and the incident that paid for it |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to run the suite, and the two rules that are not negotiable |
| [`src/wake_watcher/patterns.json`](src/wake_watcher/patterns.json) | The rule data itself — each entry carries its provenance |

Changes to the rule data, the veto logic, `WAKE_WATCHER_LIVENESS_CMD`, or the
PTY-injection path require a human security review before merge. A green test
run is necessary there, and not sufficient. ---

## Licence

MIT. See [`LICENSE`](LICENSE).

Not an Anthropic product, and not affiliated with Anthropic. It holds no
credential of its own — when it acts, it does so by invoking the `claude` CLI
you already authenticated, exactly as you would by hand.
