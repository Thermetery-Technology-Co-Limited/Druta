"""Find Python 3.9+ constructs that would fail on the legacy Python 3.8 runtime.

The Windows 7 and Vista bundles run CPython 3.8 because newer CPython releases
import Windows 8+ API sets (for example ``api-ms-win-core-path-l1-1-0.dll``).
Main targets a current CPython, so every propagation can bring code that
compiles on 3.8 but fails when a function is defined or called. This checker
reports those constructs statically, so a whole propagation can be fixed in
one pass instead of one crash per test run on the legacy machine.

``zip(..., strict=...)`` is not reported: :mod:`druta.wincompat` installs a
strict-capable ``zip`` before Druta's modules load on Python < 3.10.

Usage: python -m druta.tools.py38_compat [PATH ...]   (defaults to src/druta)
"""
import ast
import sys
from pathlib import Path

BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}

# attribute calls that do not exist on 3.8 builtins/stdlib objects
NEW_METHODS = {
    "removeprefix": "3.9", "removesuffix": "3.9", "bit_count": "3.10",
    "is_relative_to": "3.9", "with_stem": "3.9", "readlink": "3.9",
    "hardlink_to": "3.10", "walk": None,  # Path.walk is 3.12; os.walk is fine
    "add_note": "3.11",  # use druta.legacy_python.add_note
}
# module attributes that do not exist on 3.8
NEW_MODULE_ATTRS = {
    ("math", "lcm"): "3.9", ("math", "nextafter"): "3.9", ("math", "ulp"): "3.9",
    ("functools", "cache"): "3.9", ("random", "randbytes"): "3.9",
    ("itertools", "pairwise"): "3.10", ("itertools", "batched"): "3.12",
    ("ast", "unparse"): "3.9", ("asyncio", "to_thread"): "3.9",
    ("typing", "Annotated"): "3.9", ("typing", "ParamSpec"): "3.10",
    ("typing", "TypeAlias"): "3.10", ("typing", "TypeGuard"): "3.10",
    ("typing", "Self"): "3.11", ("typing", "override"): "3.12",
    ("hashlib", "file_digest"): "3.11", ("datetime", "UTC"): "3.11",
    ("enum", "StrEnum"): "3.11", ("contextlib", "chdir"): "3.11",
    ("contextlib", "aclosing"): "3.10", ("sys", "exception"): "3.11",
    ("os", "waitstatus_to_exitcode"): "3.9", ("statistics", "correlation"): "3.10",
    ("bisect", None): None,
}
NEW_MODULES = {"tomllib": "3.11", "zoneinfo": "3.9", "graphlib": "3.9"}


def _has_future_annotations(tree):
    return any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
               and any(alias.name == "annotations" for alias in node.names)
               for node in tree.body)


def _new_style_annotation(node):
    """True if evaluating this annotation needs 3.9/3.10 typing syntax."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
            return True
        if (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name)
                and sub.value.id in BUILTIN_GENERICS):
            return True
    return False


def _is_dictish(node):
    return (isinstance(node, (ast.Dict, ast.DictComp))
            or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "dict"))


class _Checker(ast.NodeVisitor):
    def __init__(self, path, future):
        self.path, self.future, self.issues = path, future, []
        self._function_depth = 0
        self._annotation_ids = set()

    def report(self, node, message):
        self.issues.append(f"{self.path}:{node.lineno}: {message}")

    def _annotation(self, node, evaluated):
        if node is None:
            return
        for sub in ast.walk(node):
            self._annotation_ids.add(id(sub))
        if evaluated and not self.future and _new_style_annotation(node):
            self.report(node, "3.9+/3.10+ annotation is evaluated at definition time "
                              "(add 'from __future__ import annotations')")

    def _visit_function(self, node):
        args = node.args
        for arg in args.posonlyargs + args.args + args.kwonlyargs:
            self._annotation(arg.annotation, True)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                self._annotation(arg.annotation, True)
        self._annotation(node.returns, True)
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_AnnAssign(self, node):
        # local-variable annotations are never evaluated; module/class ones are
        self._annotation(node.annotation, self._function_depth == 0)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        index = node.slice
        if sys.version_info < (3, 9) and isinstance(index, ast.Index):
            index = index.value
        if (id(node) not in self._annotation_ids and isinstance(node.value, ast.Name)
                and node.value.id in BUILTIN_GENERICS
                and isinstance(index, (ast.Name, ast.Tuple, ast.Attribute, ast.Subscript))):
            self.report(node, f"runtime {node.value.id}[...] generic needs 3.9")
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if (id(node) not in self._annotation_ids and isinstance(node.op, ast.BitOr)
                and (_is_dictish(node.left) or _is_dictish(node.right))):
            self.report(node, "dict | dict needs 3.9")
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in NEW_METHODS and NEW_METHODS[func.attr]:
                self.report(node, f".{func.attr}() needs {NEW_METHODS[func.attr]}")
            if func.attr in ("to_bytes", "from_bytes"):
                byteorder = any(k.arg == "byteorder" for k in node.keywords)
                positional = 2 if func.attr == "to_bytes" else 2
                if not byteorder and len(node.args) < positional:
                    self.report(node, f"{func.attr}() without length/byteorder needs 3.11")
            if (isinstance(func.value, ast.Name) and func.value.id == "traceback"
                    and func.attr in ("format_exception", "print_exception")
                    and len(node.args) == 1 and not node.keywords):
                self.report(node, f"traceback.{func.attr}(exc) single-argument form needs 3.10")
            if func.attr in ("write_text",) and any(k.arg == "newline" for k in node.keywords):
                self.report(node, "Path.write_text(newline=) needs 3.10")
            if func.attr in ("read_text",) and any(k.arg == "newline" for k in node.keywords):
                self.report(node, "Path.read_text(newline=) needs 3.13")
        if isinstance(func, ast.Name) and func.id == "isinstance" and len(node.args) == 2:
            if any(isinstance(s, ast.BinOp) and isinstance(s.op, ast.BitOr)
                   for s in ast.walk(node.args[1])):
                self.report(node, "isinstance(x, A | B) needs 3.10")
        if isinstance(func, ast.Name) and func.id in ("dataclass",) or (
                isinstance(func, ast.Attribute) and func.attr == "dataclass"):
            for keyword in node.keywords:
                if keyword.arg in ("slots", "kw_only", "match_args", "weakref_slot"):
                    self.report(node, f"@dataclass({keyword.arg}=...) needs 3.10")
        if isinstance(func, ast.Attribute) and func.attr == "field":
            if any(k.arg == "kw_only" for k in node.keywords):
                self.report(node, "dataclasses.field(kw_only=) needs 3.10")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name):
            version = NEW_MODULE_ATTRS.get((node.value.id, node.attr))
            if version:
                self.report(node, f"{node.value.id}.{node.attr} needs {version}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module in NEW_MODULES and node.level == 0:
            if not self._guarded(node):
                self.report(node, f"module {node.module} needs {NEW_MODULES[node.module]}")
        for alias in node.names:
            version = NEW_MODULE_ATTRS.get((node.module, alias.name))
            if version and not self._guarded(node):
                self.report(node, f"from {node.module} import {alias.name} needs {version}")
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name in NEW_MODULES and not self._guarded(node):
                self.report(node, f"module {alias.name} needs {NEW_MODULES[alias.name]}")
        self.generic_visit(node)

    def visit_Try(self, node):
        for child in node.body:
            for sub in ast.walk(child):
                sub._py38_guarded = True
        self.generic_visit(node)

    def visit_Match(self, node):  # only parsed on 3.10+ hosts
        self.report(node, "match statement needs 3.10")
        self.generic_visit(node)

    @staticmethod
    def _guarded(node):
        return getattr(node, "_py38_guarded", False)


def check_source(source, path="<source>"):
    tree = ast.parse(source, filename=str(path))
    checker = _Checker(path, _has_future_annotations(tree))
    checker.visit(tree)
    return checker.issues


def check_paths(paths):
    issues = []
    for root in paths:
        root = Path(root)
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            source = path.read_bytes().decode("utf-8-sig")
            issues.extend(check_source(source, path.as_posix()))
    return issues


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = argv or [str(Path(__file__).resolve().parents[1])]
    issues = check_paths(paths)
    for issue in issues:
        print(issue)
    print(f"{len(issues)} Python 3.8 compatibility issue(s)", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
