#!/usr/bin/env python3
"""Assert the rules loaded from patterns.json match the frozen snapshot exactly.

The classifier decides which errors get retried automatically; a rule silently
dropped during the JSON migration would not fail any test, because the test
suite would simply stop asserting on it. A count-based check ("32/32 passed")
cannot catch that -- it proves internal consistency after the edit, not
equivalence with what came before. This compares rule-for-rule.

Usage: verify-patterns-golden.py [golden.json]   (default: tests/patterns-golden.json)
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = ROOT / "tests" / "patterns-golden.json"
TABLES = ("TRANSIENT_PATTERNS", "NON_TRANSIENT_VETO")


def load_classify():
    spec = importlib.util.spec_from_file_location(
        "classify", ROOT / "src" / "wake_watcher" / "classify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def normalise(patterns):
    out = []
    for p in patterns:
        if hasattr(p, "pattern"):
            out.append(["regex", p.pattern])
        elif isinstance(p, (list, tuple)):
            out.append(["tuple", [str(x) for x in p]])
        else:
            out.append(["str", str(p)])
    return out


def main(argv):
    golden_path = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_GOLDEN
    if not golden_path.exists():
        print(f"golden snapshot not found: {golden_path}", file=sys.stderr)
        return 2
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    mod = load_classify()

    failed = False
    for name in TABLES:
        want = [[d["kind"], d["value"]] for d in golden[name]]
        got = normalise(getattr(mod, name))
        if want == got:
            print(f"  OK    {name:22s} {len(got):3d} rules identical")
            continue
        failed = True
        missing = [v for v in want if v not in got]
        added = [v for v in got if v not in want]
        print(f"  FAIL  {name:22s} golden={len(want)} current={len(got)}")
        for kind, val in missing[:5]:
            print(f"          dropped: {val}")
        for kind, val in added[:5]:
            print(f"          added:   {val}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
