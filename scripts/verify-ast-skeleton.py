#!/usr/bin/env python3
"""Assert two Python files have identical code skeletons (comments/docstrings ignored).

Used to prove that a translation or extraction pass touched only comments --
any logic change, however small, fails this check. A human reviewing 500 lines
of comment edits will miss a renamed function; this will not.
"""
import ast, sys

def skeleton(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            b = node.body
            if (b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                b.pop(0)
    return tree

def module_level(path):
    """Top-level statements that are not function/class defs.

    Module-level constants (MAX_WAKES, BACKOFF_BASE_SEC, ...) carry real
    contracts; comparing only functions would let an edit to them pass silently.
    """
    tree = skeleton(path)
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = None
        if isinstance(n, ast.Assign) and n.targets and isinstance(n.targets[0], ast.Name):
            name = n.targets[0].id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            name = n.target.id
        out.append((name, ast.dump(n)))
    return out


def per_function(path):
    return {n.name: ast.dump(n) for n in ast.walk(skeleton(path))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

if __name__ == "__main__":
    argv = sys.argv[1:]
    allow = set()
    if "--allow" in argv:                       # functions we declared we would edit
        i = argv.index("--allow")
        allow = set(argv[i + 1].split(","))
        del argv[i:i + 2]
    a, b = per_function(argv[0]), per_function(argv[1])
    if allow:
        print(f"declared-edit allowlist: {', '.join(sorted(allow))}")

    # Comparing only the intersection would silently pass a rename: the function
    # simply drops out of both sets. Check membership first.
    removed = sorted(a.keys() - b.keys() - allow)
    added   = sorted(b.keys() - a.keys() - allow)
    if removed or added:
        if removed: print("REMOVED:", ", ".join(removed))
        if added:   print("ADDED:  ", ", ".join(added))
        sys.exit(1)

    ml_a = [x for x in module_level(argv[0]) if x[0] not in allow]
    ml_b = [x for x in module_level(argv[1]) if x[0] not in allow]
    if ml_a != ml_b:
        only_a = [d for n, d in ml_a if (n, d) not in ml_b]
        only_b = [d for n, d in ml_b if (n, d) not in ml_a]
        print(f"MODULE-LEVEL differs: -{len(only_a)} +{len(only_b)} statement(s)")
        for x in (only_a + only_b)[:3]:
            print("   ", x[:110])
        sys.exit(1)

    shared = a.keys() & b.keys()
    diff = sorted(n for n in shared if a[n] != b[n] and n not in allow)
    print(f"shared functions: {len(shared)}   differing: {len(diff)}")
    if diff:
        print("CHANGED:", ", ".join(diff)); sys.exit(1)
    print("skeleton identical for all shared functions")
