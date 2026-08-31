"""No local may be read before every path has assigned it.

gokulsarasan1999@gmail.com and sachitjamwal19@gmail.com were never replied to.
Four processing_failed events, two per candidate, all of them:

    cannot access local variable 'application_id' where it is not associated
    with a value

Both had hit the same branch first - "LLM requirement match confidence below
threshold" - so the code reached a log line that reads application_id on a path
that had not yet created the application row. The same shape as the
cv_role_summary crash found in the event log a few days earlier, which is why
this is a check rather than another one-line fix.
"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULES = ["recruiter_agent.py", "recruiter_dashboard.py", "llm_factory.py"]


def comprehension_names(fn: ast.AST) -> set[int]:
    """The Name nodes belonging to a comprehension, by identity.

    `{k: v for k, v in d.items()}` puts the read before the assignment in source
    order without any risk at all, and there are dozens of those. Identity and
    not line number: `requirement = next((row for row in rows), None)` carries a
    genexp on the same line as a real assignment, and skipping the whole line
    hid that assignment and produced a false report.
    """
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    names.add(id(sub))
    return names


def read_before_assigned(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    problems = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        if fn.args.vararg:
            params.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            params.add(fn.args.kwarg.arg)
        declared = set()
        for node in ast.walk(fn):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                declared.update(node.names)
        skip = comprehension_names(fn)

        stores, loads = {}, {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and id(node) not in skip:
                if isinstance(node.ctx, ast.Store):
                    stores.setdefault(node.id, []).append(node.lineno)
                elif isinstance(node.ctx, ast.Load):
                    loads.setdefault(node.id, []).append(node.lineno)
            elif isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
                stores.setdefault(node.target.id, []).append(node.lineno)

        for name, store_lines in stores.items():
            if name in params or name in declared:
                continue
            earlier = [n for n in loads.get(name, []) if n < min(store_lines)]
            if earlier:
                problems.append(
                    f"{path.name}:{fn.name} reads {name!r} at line {min(earlier)} "
                    f"but first assigns it at line {min(store_lines)}"
                )
    return problems


@pytest.mark.parametrize("module", MODULES)
def test_no_local_is_read_before_it_is_assigned(module):
    problems = read_before_assigned(ROOT / module)
    assert not problems, "\n".join(problems)


def test_the_check_actually_catches_the_shape_that_bit_us():
    """Guard the guard: the real crash must be detectable by this check."""
    import tempfile

    bad = '''
def process(items):
    for item in items:
        log(application_id)
        application_id = save(item)
'''
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(bad)
        tmp = Path(f.name)
    try:
        found = read_before_assigned(tmp)
        assert found and "application_id" in found[0]
    finally:
        tmp.unlink()


def test_comprehensions_are_not_reported():
    import tempfile

    fine = '''
def fine(rows):
    a = {key: value for key, value in rows}
    b = [word for word in rows if word]
    requirement = next((row for row in rows if row), None)
    return a, b, requirement
'''
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(fine)
        tmp = Path(f.name)
    try:
        assert read_before_assigned(tmp) == []
    finally:
        tmp.unlink()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
