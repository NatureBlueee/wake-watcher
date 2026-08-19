#!/usr/bin/env python3
"""Mutation gate: for each safety contract, break it and assert a test goes red.

Coverage tells you a line ran. This tells you that if the line were wrong, you
would find out. This project has already shipped one suite that was green while
a guard was disabled -- that is what this gate exists to prevent.

Usage:  python3 scripts/verify-mutations.py [--baseline path/to/baseline.json]
Exit 0 only if every contract behaves exactly as the baseline says it should.
"""
import json, os, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC, TESTS = ROOT / "src" / "wake_watcher", ROOT / "tests"

# (name, target file, find, replace, tests that must go red, expected)
CONTRACTS = [
    ("offline-defer", "wake_watcher.py", "def network_reachable", None,
     ["test_loop_control", "test_dedup_fix"], "caught"),
    ("project-root-scoping", "wake_watcher.py", "def cwd_in_project", None,
     ["test_scope"], "caught"),
    ("do-not-wake-list", "wake_watcher.py", "def load_do_not_wake", None,
     ["test_scope"], "caught"),
    ("wake-cap", "wake_watcher.py", '"WAKE_WATCHER_MAX_WAKES", "3"',
     '"WAKE_WATCHER_MAX_WAKES", "999"', ["test_loop_control", "test_dedup_fix"], "caught"),
    ("now-forward-watermark", "wake_watcher.py", "def load_or_init_watermark", None,
     ["test_watermark", "test_dedup_fix"], "caught"),
    # Rules now live in patterns.json, so the mutation targets the data file:
    # drop one transient rule and assert the classifier suite notices.
    ("transient-allowlist", "patterns.json", "__DROP_ONE_TRANSIENT__", None,
     ["test_classify"], "caught"),
    ("pty-safety-line", "wake_watcher.py", 'a.get("kind") == "background"',
     'a.get("kind") != "__never__"', ["test_attach_inject_routing"], "caught"),
]

def stub_function(text, fname, ret):
    """Replace a function body with `return <ret>` -- the crudest possible break."""
    i = text.index(f"def {fname}")
    j = text.index("\ndef ", i + 10)
    head = text[i:text.index(":", i) + 1]
    return text[:i] + head + f"\n    return {ret}\n\n" + text[j + 1:]

STUB_RET = {"network_reachable": "True", "cwd_in_project": "True",
            "load_do_not_wake": "set()", "load_or_init_watermark": "None"}

def run(contract, workdir):
    name, target, find, repl, tests, expected = contract
    shutil.copytree(SRC, workdir / "src" / "wake_watcher", dirs_exist_ok=True)
    shutil.copytree(TESTS, workdir / "tests", dirs_exist_ok=True)
    p = workdir / "src" / "wake_watcher" / target
    s = p.read_text(encoding="utf-8")
    if find == "__DROP_ONE_TRANSIENT__":
        import json as _json
        d = _json.loads(s)
        if not d.get("TRANSIENT_PATTERNS"):
            return name, "INJECT-FAILED"
        d["TRANSIENT_PATTERNS"].pop()
        p.write_text(_json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        s2 = "mutated"
    elif repl is None:
        fn = find.replace("def ", "")
        s2 = stub_function(s, fn, STUB_RET[fn])
    else:
        s2 = s.replace(find, repl)
    if s2 == s:
        return name, "INJECT-FAILED"
    p.write_text(s2, encoding="utf-8")
    env = dict(os.environ, WAKE_WATCHER_CLAUDE_HOME=str(workdir / "fake"))
    (workdir / "fake" / "jobs").mkdir(parents=True, exist_ok=True)
    for t in tests:
        r = subprocess.run([sys.executable, str(workdir / "tests" / f"{t}.py")],
                           capture_output=True, cwd=workdir, env=env, timeout=300)
        if r.returncode != 0:
            return name, "caught"
    return name, "SURVIVED"

if __name__ == "__main__":
    results, bad = [], 0
    for c in CONTRACTS:
        with tempfile.TemporaryDirectory() as d:
            n, got = run(c, pathlib.Path(d))
        ok = got == c[5]
        bad += not ok
        results.append((n, got, c[5], ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {n:24s} got={got:14s} want={c[5]}")
    print(f"\n{len(results) - bad}/{len(results)} contracts behave as declared")
    sys.exit(1 if bad else 0)
