"""The commands the README hands to someone who has installed nothing.

Why this file exists: `wake-watcher --check-string "<text>"` appeared in
README.md, README.zh-CN.md and CONTRIBUTING.md as the one thing a stranger
could run before committing to anything -- and it did not exist. The flag was
implemented only on classify.py's own __main__; wake_watcher.py's parser
rejected it, and install.sh puts only `wake-watcherctl` on PATH. Both
installation routes produced a command that exits 2.

Every gate stayed green through that: the docs gate checks referenced *paths*,
the change gate found the `check-string` symbol (in classify.py, where it
really was), and no test traverses main()'s argument parsing. A README is a set
of falsifiable claims about behaviour, and nothing here was falsifying them.

So this file pins the claims themselves:

  1. all three entry points accept --check-string and exit 0
  2. they agree -- character for character
  3. the check touches no state (no ledger, no heartbeat, no watermark), which
     is what makes it safe to tell a stranger to run it
  4. every `wake-watcherctl <sub>` in the README is a subcommand that exists
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLASSIFY = REPO / "src" / "wake_watcher" / "classify.py"
WW = REPO / "src" / "wake_watcher" / "wake_watcher.py"
CTL = REPO / "bin" / "wake-watcherctl"

TRANSIENT = "API Error: 500 internal server error"
VETOED = "Claude AI usage limit reached"
UNKNOWN = "Error: something nobody has ever seen"


def _run(argv, env=None):
    e = dict(os.environ)
    # Pin the interpreter wake-watcherctl shells out to. Without this the ctl
    # path runs whatever `python3` resolves to, which on a CI matrix is not
    # necessarily the interpreter running these tests -- and then a
    # character-for-character comparison between entry points is comparing two
    # Pythons, not two code paths.
    e["WAKE_WATCHER_PYTHON"] = sys.executable
    if env:
        e.update(env)
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=e, cwd=str(REPO))


def test_all_three_entry_points_accept_the_flag():
    """The claim: three different installs, one working command in each."""
    for argv in (
        [sys.executable, str(CLASSIFY), "--check-string", TRANSIENT],
        [sys.executable, str(WW), "--check-string", TRANSIENT],
        [str(CTL), "check", TRANSIENT],
    ):
        r = _run(argv)
        assert r.returncode == 0, f"{argv[-2:]} exited {r.returncode}: {r.stderr[:400]}"
        assert "verdict:" in r.stdout, f"{argv[-2:]} printed no verdict: {r.stdout[:200]}"


def test_the_three_agree_character_for_character():
    """Two commands disagreeing about what the classifier said is worse than one."""
    outs = [
        _run([sys.executable, str(CLASSIFY), "--check-string", VETOED]).stdout,
        _run([sys.executable, str(WW), "--check-string", VETOED]).stdout,
        _run([str(CTL), "check", VETOED]).stdout,
    ]
    assert outs[0] == outs[1] == outs[2], "entry points disagree:\n" + "\n---\n".join(outs)


def test_the_two_refusals_are_distinguishable():
    """A veto and a default-deny are different facts, and the README says so."""
    vetoed = _run([str(CTL), "check", VETOED]).stdout
    unknown = _run([str(CTL), "check", UNKNOWN]).stdout
    assert "vetoed:" in vetoed and "would NOT wake" in vetoed
    assert "default-deny" in unknown and "would NOT wake" in unknown
    assert vetoed != unknown, "a recognised refusal reads the same as an unrecognised one"


def test_it_writes_no_state_anywhere():
    """What makes it safe to hand to a stranger: it cannot leave anything behind.

    Point every state path at an empty directory and assert the directory is
    still empty afterwards. If --check-string ever starts running after the
    heartbeat/watermark setup, this goes red.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ww-checkstring-"))
    try:
        env = {
            "WAKE_WATCHER_LOG": str(tmp / "log"),
            "WAKE_WATCHER_LEDGER": str(tmp / "ledger.json"),
            "WAKE_WATCHER_HEARTBEAT": str(tmp / "heartbeat"),
            "WAKE_WATCHER_WATERMARK_FILE": str(tmp / "watermark.json"),
            "WAKE_WATCHER_NEEDS_HUMAN": str(tmp / "needs-human.log"),
        }
        r = _run([sys.executable, str(WW), "--check-string", TRANSIENT], env=env)
        assert r.returncode == 0, r.stderr[:400]
        leftovers = sorted(p.name for p in tmp.iterdir())
        assert leftovers == [], f"--check-string left state behind: {leftovers}"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_readme_only_names_subcommands_that_exist():
    """The smallest version of the gate that was missing.

    Pull every `wake-watcherctl <sub>` out of both READMEs and check the
    dispatcher actually handles it. This would have caught the original defect
    class, and it costs nothing to keep running.
    """
    ctl_src = CTL.read_text(encoding="utf-8")
    # subcommands are the case labels in the dispatcher at the bottom
    handled = set(re.findall(r"^\s{2}([a-z|-]+)\)$", ctl_src, re.M))
    handled = {part for label in handled for part in label.split("|")}
    assert "check" in handled, "the dispatcher lost the `check` subcommand"

    named = set()
    for readme in ("README.md", "README.zh-CN.md"):
        text = (REPO / readme).read_text(encoding="utf-8")
        named |= set(re.findall(r"wake-watcherctl\s+([a-z][a-z-]*)", text))
    unknown = sorted(named - handled)
    assert not unknown, f"README names wake-watcherctl subcommands that do not exist: {unknown}"
    assert named, "no wake-watcherctl commands found in the READMEs -- did the parse break?"


def test_the_reset_time_caveat_appears_only_when_there_is_a_reset_time():
    """It used to print on every verdict, including ones with no reset time.

    A warning that shows up unconditionally is a warning readers learn to skip,
    so it is now conditional -- which means both branches need pinning, or the
    condition can silently invert.
    """
    with_reset = _run([str(CTL), "check", "Claude AI usage limit reached — resets 3:20am"]).stdout
    assert "reset_epoch: None" not in with_reset, "the fixture stopped parsing a reset time"
    assert "note: reset_epoch based on current time" in with_reset

    anchored = _run([str(CTL), "check", "Claude AI usage limit reached — resets 3:20am",
                     "--error-epoch", "1787012400"]).stdout
    assert "note:" not in anchored, "the caveat survived an explicit --error-epoch"

    no_reset = _run([str(CTL), "check", TRANSIENT]).stdout
    assert "note:" not in no_reset, "the caveat printed for a verdict with no reset time in it"


def test_json_stays_parseable_alongside_the_report():
    """patterns.json is the data the report is derived from; keep it loadable."""
    data = json.loads((REPO / "src" / "wake_watcher" / "patterns.json").read_text(encoding="utf-8"))
    assert data, "patterns.json is empty"
