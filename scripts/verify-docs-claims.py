#!/usr/bin/env python3
"""Assert every file path a doc references actually exists in the repo.

Documentation that promises a file which was never written is the same failure
class as a guard that was documented but never implemented: the claim reads as
verified when nothing verified it. This gate makes the docs falsifiable.
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Backticked tokens that look like repo-relative paths.
PATHISH = re.compile(r'`([A-Za-z0-9_./-]+\.(?:py|sh|json|yml|yaml|md|toml|template|txt))`')
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}
# Referenced by name but legitimately not repo files.
ALLOW = {"state.json", "settings.json", "package.json", "continuation.md", "AGENTS.md", "CLAUDE.md"}

def main():
    missing, checked = [], 0
    for md in sorted(ROOT.rglob("*.md")):
        if any(p in SKIP_DIRS for p in md.parts):
            continue
        for m in PATHISH.finditer(md.read_text(encoding="utf-8")):
            ref = m.group(1)
            if ref in ALLOW or ref.startswith(("http", "~")):
                continue
            checked += 1
            hits = list(ROOT.rglob(pathlib.PurePath(ref).name))
            hits = [h for h in hits if not any(p in SKIP_DIRS for p in h.parts)]
            if not hits:
                missing.append((md.relative_to(ROOT).as_posix(), ref))
    for doc, ref in missing:
        print(f"  MISSING  {ref:38s} referenced by {doc}")
    print(f"\n{checked - len(missing)}/{checked} referenced paths exist")
    return 1 if missing else 0

if __name__ == "__main__":
    sys.exit(main())
