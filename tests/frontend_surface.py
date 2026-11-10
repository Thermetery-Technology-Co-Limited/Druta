# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the Python front ends use on a GPU backend object, measured by an AST
scan, plus the tables the backend-conformance test (G4a) checks it against.

Not a test module: tests/test_backend_conformance.py imports it.

The scan (native/DESIGN-FRONTEND.md section (f) item 2, check (b)) walks
src/druta/**/*.py except the two backends (nvbackend.py, native*.py), the
factory (gpu_backend.py) and tools/, and records every attribute load, store
and delete whose receiver is a GPU expression:

* the names ``gpu``, ``fresh`` and ``g``, any name ending in ``_gpu``, and
  any ``<expr>.gpu`` (``self.gpu``, ``app.gpu``, ``self.app.gpu``,
  ``reader.gpu`` ...);
* ``getattr`` / ``hasattr`` / ``setattr`` / ``delattr`` on such a receiver
  with a literal name;
* ``GPU.X`` (the nvbackend class used as a namespace) and ``type(gpu).X``
  (also through getattr with a literal name).

The same is recorded for companion expressions: the names ``nvapi``, ``api``,
``nvml`` and ``nv``, and any ``<expr>.nvapi`` / ``<expr>.nvml``. A use of
``<companion>.dll.X`` is also recorded as ``dll.X``.

Every table entry carries the evidence (``file:line``) that put it there; the
line numbers are a snapshot of the day G4a measured (cpp/core 30d6096), and
the conformance test checks the file and name, not the line.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "druta"
NVBACKEND = SRC / "nvbackend.py"
NATIVE = SRC / "native.py"

GPU_NAMES = frozenset({"gpu", "fresh", "g"})
COMPANION_NAMES = frozenset({"nvapi", "api", "nvml", "nv"})
COMPANION_ATTRS = frozenset({"nvapi", "nvml"})
_NAME_BUILTINS = {"getattr": "load", "hasattr": "load", "setattr": "store", "delattr": "del"}


@dataclass(frozen=True)
class Use:
    name: str
    kind: str      # "load" | "store" | "del"
    via: str       # "attr" (gpu.X), "getattr" (literal), "class" (GPU.X), "type" (type(gpu).X)
    file: str      # path relative to src/druta, posix
    line: int
    receiver: str = "gpu"  # "gpu", or for companion uses "nvapi" / "nvml"

    @property
    def where(self):
        return f"{self.file}:{self.line}"


def frontend_files():
    """The scanned files: src/druta/**/*.py minus the backends, the factory and tools/."""
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if rel.parts[0] in ("tools", "__pycache__"):
            continue
        name = rel.name
        if name == "nvbackend.py" or name == "gpu_backend.py" or (len(rel.parts) == 1 and name.startswith("native")):
            continue
        yield path


def _is_getattr_gpu(node):
    """``getattr(<any expression>, "gpu"[, default])``: druta.py reaches its backend this way.
    ``getattr(<nvapi>, "gpu")`` is the NVAPI handle, not a backend."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "gpu"
            and not _is_companion(node.args[0]))


def _is_gpu(node):
    if isinstance(node, ast.Name):
        return node.id in GPU_NAMES or node.id.endswith("_gpu")
    if isinstance(node, ast.Attribute):
        return node.attr == "gpu" and not _is_companion(node.value)  # nvapi.gpu is the NVAPI handle
    return _is_getattr_gpu(node)


def _is_type_of_gpu(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type"
            and len(node.args) == 1 and _is_gpu(node.args[0]))


def _is_gpu_class(node):
    return isinstance(node, ast.Name) and node.id == "GPU"


def _is_companion(node):
    return _companion_of(node) is not None


def _companion_of(node):
    """'nvapi' | 'nvml' | None: which half a companion expression names."""
    if isinstance(node, ast.Name) and node.id in COMPANION_NAMES:
        return "nvapi" if node.id in ("nvapi", "api") else "nvml"
    if isinstance(node, ast.Attribute) and node.attr in COMPANION_ATTRS:
        return node.attr
    return None


def _ctx_kind(ctx):
    if isinstance(ctx, ast.Store):
        return "store"
    if isinstance(ctx, ast.Del):
        return "del"
    return "load"


def _receiver_class(node):
    """'gpu' | 'type' | 'class' | 'companion' | None for an attribute receiver."""
    if _is_gpu(node):
        return "gpu"
    if _is_type_of_gpu(node):
        return "type"
    if _is_gpu_class(node):
        return "class"
    if _is_companion(node):
        return "companion"
    return None


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module)


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing(node, parents, kinds):
    node = parents.get(node)
    while node is not None and not isinstance(node, kinds):
        node = parents.get(node)
    return node


def _scope_nodes(scope):
    """The nodes of a function (or module) body, not descending into nested scopes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES + (ast.ClassDef,)):
            stack.extend(ast.iter_child_nodes(node))


def _literal_strings(node):
    """The strings of a str constant or a literal tuple / list of str constants, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List)) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [e.value for e in node.elts]
    return None


def _binds(target, name):
    return any(isinstance(t, ast.Name) and t.id == name for t in ast.walk(target))


def _resolve_name(name, scope):
    """The literal strings a local ``name`` can hold in ``scope`` (a function or the
    module), or None. Resolved: ``for name in <literal tuple/list of str>`` (also
    through a local bound exactly once to such a literal) and ``name = "<str>"``.
    Any other binding (a parameter, a non-literal loop or assignment, with / except /
    import / global) makes the name unresolvable."""
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if name in {a.arg for a in params if a is not None}:
            return None
    nodes = list(_scope_nodes(scope))
    assigned = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
    values = []
    for node in nodes:
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and _binds(node.target, name):
            if not isinstance(node.target, ast.Name):
                return None
            source = node.iter
            if isinstance(source, ast.Name) and len(assigned.get(source.id, ())) == 1:
                source = assigned[source.id][0]
            strings = _literal_strings(source)
            if strings is None:
                return None
            values += strings
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if _binds(target, name):
                    if not (isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, str)):
                        return None
                    values.append(node.value.value)
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)) and _binds(node.target, name):
            return None
        elif isinstance(node, (ast.With, ast.AsyncWith)) and any(
                item.optional_vars is not None and _binds(item.optional_vars, name) for item in node.items):
            return None
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            return None
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name.split(".")[0]) == name for alias in node.names):
            return None
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return None
    return values or None


@dataclass(frozen=True)
class DynamicSite:
    """A getattr / hasattr / setattr / delattr on a GPU or companion expression
    whose name the scanner cannot resolve to literals."""
    file: str
    line: int
    call: str      # ast.unparse of the call

    @property
    def key(self):
        return (self.file, self.call)


def scan_file(path, dynamic=None):
    """(gpu_uses, companion_uses) for one file; unresolved dynamic names go to ``dynamic``."""
    rel = path.relative_to(SRC).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    parents = _parents(tree)
    gpu, companion = [], []

    def record(receiver, name, kind, via, line):
        cls = _receiver_class(receiver)
        if cls is None:
            return
        if cls == "companion":
            companion.append(Use(name, kind, via, rel, line, _companion_of(receiver)))
            return
        if cls == "type":
            via = "type"
        elif cls == "class":
            via = "class"
        gpu.append(Use(name, kind, via, rel, line))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            record(node.value, node.attr, _ctx_kind(node.ctx), "attr", node.lineno)
            # <companion>.dll.X
            if (isinstance(node.value, ast.Attribute) and node.value.attr == "dll"
                    and _is_companion(node.value.value)):
                companion.append(Use("dll." + node.attr, _ctx_kind(node.ctx), "attr", rel, node.lineno,
                                     _companion_of(node.value.value)))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in _NAME_BUILTINS and len(node.args) >= 2):
            kind, arg = _NAME_BUILTINS[node.func.id], node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                record(node.args[0], arg.value, kind, "getattr", node.lineno)
            elif _receiver_class(node.args[0]) is not None:
                names = (_resolve_name(arg.id, _enclosing(node, parents, _SCOPES))
                         if isinstance(arg, ast.Name) else None)
                if names is None:
                    if dynamic is not None:
                        dynamic.append(DynamicSite(rel, node.lineno, ast.unparse(node)))
                else:
                    for name in names:
                        record(node.args[0], name, kind, "getattr", node.lineno)
    return gpu, companion


_CACHE = {}


def scan():
    """(gpu_uses, companion_uses) over every front-end file, cached per process."""
    if "scan" not in _CACHE:
        gpu, companion, dynamic = [], [], []
        for path in frontend_files():
            g, c = scan_file(path, dynamic)
            gpu.extend(g)
            companion.extend(c)
        _CACHE["scan"] = (gpu, companion)
        _CACHE["dynamic"] = dynamic
    return _CACHE["scan"]


def dynamic_sites():
    """Accesses on a GPU or companion expression whose name is not statically resolvable."""
    scan()
    return _CACHE["dynamic"]


def by_name(uses):
    out = {}
    for use in uses:
        out.setdefault(use.name, []).append(use)
    return out


def instance_uses():
    """Uses on a GPU object or its type (not the GPU.X namespace)."""
    return [u for u in scan()[0] if u.via != "class"]


def class_uses():
    """GPU.X: the nvbackend class used as a namespace for constants and pure helpers."""
    return [u for u in scan()[0] if u.via == "class"]


def stored_names():
    return {u.name for u in scan()[0] if u.kind in ("store", "del")}


LOCK_METHODS = frozenset({"acquire", "release", "locked", "__enter__", "__exit__"})


def _is_lock_expr(node, names=(), attrs=()):
    """A GPU lock: ``<gpu>._lock``, ``getattr(<gpu>, "_lock"[, d])``, either branch of a
    conditional expression or any operand of ``x or y`` / ``x and y`` that is one, a local
    name in ``names`` or an attribute (by source text, e.g. ``self._held``) in ``attrs``."""
    if isinstance(node, ast.Attribute):
        return (node.attr == "_lock" and _is_gpu(node.value)) or ast.unparse(node) in attrs
    if isinstance(node, ast.Call):
        return (isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2
                and _is_gpu(node.args[0]) and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "_lock")
    if isinstance(node, ast.IfExp):
        return _is_lock_expr(node.body, names, attrs) or _is_lock_expr(node.orelse, names, attrs)
    if isinstance(node, ast.BoolOp):
        return any(_is_lock_expr(v, names, attrs) for v in node.values)
    if isinstance(node, ast.NamedExpr):
        return _is_lock_expr(node.value, names, attrs)
    return isinstance(node, ast.Name) and node.id in names


def _lock_bindings(node):
    """(target, value) pairs a statement binds."""
    if isinstance(node, ast.Assign):
        return [(t, node.value) for t in node.targets]
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
        return [(node.target, node.value)]
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [(node.optional_vars, node.context_expr)]
    return []


def lock_method_uses(paths=None):
    """Check (h): ``acquire`` / ``release`` / ``locked`` / ``__enter__`` / ``__exit__``
    called on a GPU object's ``_lock`` outside the backends (the front ends only use
    ``with``): directly, through a local bound to it (scoped to its function), or through
    an attribute it was stored on (``self._held = self.gpu._lock``, scoped to its class).
    Returns ``file:line method`` strings."""
    found = []
    for path in (paths if paths is not None else frontend_files()):
        rel = path.relative_to(SRC).as_posix() if path.is_relative_to(SRC) else path.name
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        parents = _parents(tree)
        names, attrs = {}, {}   # scope node -> local aliases; class (or module) node -> attribute aliases

        def context(node, parents=parents):
            scope = _enclosing(node, parents, _SCOPES)
            owner = _enclosing(node, parents, (ast.ClassDef, ast.Module))
            return scope, owner

        changed = True
        while changed:  # to a fixed point: an alias of an alias
            changed = False
            for node in ast.walk(tree):
                for target, value in _lock_bindings(node):
                    scope, owner = context(node)
                    if not _is_lock_expr(value, names.get(scope, ()), attrs.get(owner, ())):
                        continue
                    for t in ([target] if not isinstance(target, (ast.Tuple, ast.List)) else []):
                        if isinstance(t, ast.Name) and t.id not in names.setdefault(scope, set()):
                            names[scope].add(t.id)
                            changed = True
                        elif isinstance(t, ast.Attribute):
                            text = ast.unparse(t)
                            if text not in attrs.setdefault(owner, set()):
                                attrs[owner].add(text)
                                changed = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in LOCK_METHODS:
                scope, owner = context(node)
                if _is_lock_expr(node.value, names.get(scope, ()), attrs.get(owner, ())):
                    found.append(f"{rel}:{node.lineno} {node.attr}")
    return found


# --------------------------------------------------------------------------- #
#  The backends' surfaces, read statically (no library needed)                 #
# --------------------------------------------------------------------------- #
def _class_node(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def init_stores(path, class_name):
    """Names assigned as ``self.X = ...`` in the class's __init__."""
    cls = _class_node(path, class_name)
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    return {n.attr for n in ast.walk(init)
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
            and isinstance(n.value, ast.Name) and n.value.id == "self"}


def class_body_names(path, class_name):
    names = set()
    for n in _class_node(path, class_name).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def nvbackend_public_members(gpu_cls=None):
    """nvbackend.GPU's public surface: every public name on the class (its body,
    plus anything patched on or added by a subclass) and ``self.X =`` in
    nvbackend.GPU.__init__ (read from the source, since an instance would need a
    driver)."""
    if gpu_cls is None:
        from druta import nvbackend
        gpu_cls = nvbackend.GPU
    names = {n for n in dir(gpu_cls) if not n.startswith("_")}
    return names | {n for n in init_stores(NVBACKEND, "GPU") if not n.startswith("_")}


def native_static_surface(gpu_cls=None):
    """What a native.Gpu instance carries without constructing one: every name
    on the class (and its bases, and members assigned after the class body such
    as ``Gpu.reset_all``) plus ``self.X =`` in native.Gpu.__init__."""
    if gpu_cls is None:
        from druta import native
        gpu_cls = native.Gpu
    return set(dir(gpu_cls)) | init_stores(NATIVE, "Gpu")


def measured_gaps(nvb_cls=None, native_cls=None):
    """{name: set of checks ("a", "b") that find it} for today's classes.

    a: a public nvbackend.GPU member native.Gpu lacks (minus EXEMPT);
    b: a name the front ends use on a GPU object, or as GPU.X / type(gpu).X,
       that native.Gpu lacks (minus FRONTEND_OWNED)."""
    native_surface = native_static_surface(native_cls)
    out = {}
    for name in nvbackend_public_members(nvb_cls):
        if name not in native_surface and name not in EXEMPT:
            out.setdefault(name, set()).add("a")
    for use in scan()[0]:
        if use.name not in native_surface and use.name not in FRONTEND_OWNED:
            out.setdefault(use.name, set()).add("b")
    return out


# --------------------------------------------------------------------------- #
#  Tables (measured on cpp/core 30d6096)                                       #
# --------------------------------------------------------------------------- #
#  EXEMPT: public nvbackend.GPU members native.Gpu is not expected to carry.
#  Each must exist on nvbackend.GPU, be absent from native.Gpu and have no
#  front-end use (the conformance test checks all three).
EXEMPT = {
    "clkdom_debug_report": "experiment that writes and samples; not ported (native/README.md); "
                           "no front-end use (DESIGN-FRONTEND b4); nvbackend.py:3409",
    "clkdom_mapping_probe": "experiment that writes and samples; not ported (native/README.md); "
                            "no front-end use (DESIGN-FRONTEND b4); nvbackend.py:3157",
    "LEGACY_P0_PROFILES": "developer-board table (nvbackend.py:4448), recorded as not ported; the front "
                          "end reaches it only through legacy_p0_profile, whose native shim returns "
                          "None (DESIGN-FRONTEND b4, Q4)",
}

#  FRONTEND_OWNED: names the scan finds on a GPU expression that are not the
#  backend's: attributes the front end creates itself, and false positives
#  (a dict that happens to be named gpu / fresh). Not gaps whatever native.Gpu
#  carries; neither backend class defines them (the test checks that).
FRONTEND_OWNED = {
    "power_policy_names": "front-end-owned display table, defined by neither backend: stored at "
                          "druta.py:2714, druta.py:2861, druta.py:2908, read with a getattr default at "
                          "profiles.py:275 (DESIGN-FRONTEND b3)",
    "get": "false positive, dict receivers named gpu / fresh: pcie_speed_panel.py:136, "
           "power_integral_panel.py:157, registry_overrides.py:1345 (DESIGN-FRONTEND b names only "
           "pcie_speed_panel.py:136-140)",
}

#  PRIVATE_REACHES: every private member the front ends reach into on a GPU
#  object, its type or the GPU class namespace -> (decision, evidence). The set is ratcheted: a new private reach
#  fails the test until it is listed here with a decision. Those native.Gpu
#  lacks today are also in KNOWN_GAPS.
PRIVATE_REACHES = {
    "_clkdom_get": ("G4e: delegate nvbackend's function object over the companions (explicit list)",
                    "voltage_rel_live.py:61"),
    "_clkdom_valid_block": ("G4e: delegate nvbackend's function object over the companions (explicit list)",
                            "voltage_rel_live.py:62"),
    "_current_limit_abi": ("keep: already mirrored on native.Gpu",
                           "power_integral_runtime.py:70, power_weight_runtime.py:58"),
    "_current_limit_error": ("keep: already on native.Gpu (not listed in DESIGN-FRONTEND b4)",
                             "profiles.py:238 (getattr), profiles.py:241 (getattr)"),
    "_current_limit_generation_policies": ("keep: already mirrored on native.Gpu",
                                           "profiles.py:225 (getattr)"),
    "_current_limit_layout_choice": ("keep: written by the front end; native.Gpu has a setter (b3)",
                                     "client_power_runtime.py:271, clock_bios_runtime.py:311, "
                                     "clock_vf_registry.py:216, vfe_equation_runtime.py:231 (getattr, "
                                     "a literal loop name), +15 more"),
    "_current_limit_read_error": ("keep: already on native.Gpu (not listed in DESIGN-FRONTEND b4)",
                                  "profiles.py:236 (getattr), profiles.py:259 (getattr)"),
    "_current_limit_rm": ("keep: already mirrored on native.Gpu",
                          "power_integral_runtime.py:77, power_integral_runtime.py:80, "
                          "power_integral_runtime.py:108, +3 more"),
    "_current_limit_transport": ("keep: written by the front end; native.Gpu has a setter (b3)",
                                 "client_power_runtime.py:270, clock_bios_runtime.py:310, "
                                 "clock_vf_registry.py:215, vfe_equation_runtime.py:231 (getattr, "
                                 "a literal loop name), +16 more"),
    "_current_limit_transport_layouts": ("keep: already mirrored on native.Gpu", "perf_diagnostics.py:265"),
    "_foreign_hold_headroom": ("keep: written by the front end; native.Gpu has the class default (b3, R6); "
                               "nvbackend.GPU does not define it (getattr default)",
                               "druta.py:7974 (getattr), druta.py:7975, druta.py:7998"),
    "_hold_headroom_live_trip": ("keep: written by the front end; native.Gpu carries it (b3)",
                                 "druta.py:7444 (getattr), druta.py:7445"),
    "_hold_headroom_unread": ("keep: already on native.Gpu (not listed in DESIGN-FRONTEND b4)",
                              "druta.py:7447 (getattr)"),
    "_legacy_clk_escape": ("keep: already mirrored on native.Gpu",
                           "client_power_runtime.py:358, clock_bios_runtime.py:226, "
                           "clock_vf_registry.py:159, +14 more"),
    "_lock": ("keep: already mirrored on native.Gpu (G4c replaces it with the handle lock)",
              "adaptive_power_live.py:83, adaptive_power_live.py:90, all_pstates_registry.py:375 (getattr), "
              "+89 more"),
    "_msvdd_offset_domains_seen": ("G4f: new public msvdd_offset_domains() on both backends; call site changes",
                                   "profiles.py:747 (getattr)"),
    "_msvdd_offset_domains_written": ("G4f: new public msvdd_offset_domains() on both backends; "
                                      "call site changes", "profiles.py:746 (getattr)"),
    "_offset_range": ("G4f: new public clock_offset_range(ctype) on both backends; call sites change",
                      "tuning_profiles.py:90, tuning_profiles.py:157"),
    "_power_policy_mask": ("keep: written by the front end; native.Gpu has a setter (b3)",
                           "power_integral_runtime.py:84"),
    "_power_policy_pins": ("keep: written by the front end; native.Gpu has a strict setter (b3, Q8)",
                           "druta.py:2875 (getattr), druta.py:2879, power_action_profiles.py:48 (getattr), "
                           "+4 more"),
    "_policy_amount": ("keep: a GPU.X namespace reference (b2: stays on nvbackend.GPU for G4); native.Gpu "
                       "mirrors it (DESIGN-FRONTEND b4 'already mirrored')",
                       "druta.py:2767 (class), druta.py:2942 (class), druta.py:2966 (class)"),
    "_power_policy_table": ("keep: already mirrored on native.Gpu", "linked_current_runtime.py:110"),
    "_pstate20_clock": ("G4e: delegate nvbackend's function object over the companions (explicit list)",
                        "bios_runtime.py:32"),
    "_read_power": ("G4f: new public read_enforced_power_limit_mw() on both backends; call site changes",
                    "druta.py:6558 (getattr)"),
    "_read_pstates20": ("G4e: delegate nvbackend's function object over the companions (explicit list)",
                        "all_pstates_registry.py:346, bios_runtime.py:25, voltage_rel_live.py:53"),
    "_read_volt_rail_deltas": ("G4e: b1 shim over native_rails", "voltage_bios_runtime.py:54"),
    "_refresh_volt_rail_capabilities": ("G4e: b1 shim over native_rails (native_rails.refresh)",
                                        "voltage_bios_runtime.py:106 (getattr)"),
    "_rm_power_transport": ("keep: already mirrored on native.Gpu", "client_power_runtime.py:297"),
    "_vf_lock_recovery": ("G4e: b1 property over vf_lock_recovery / set_vf_lock_recovery",
                          "druta.py:6963 (getattr)"),
    "_vfp_curve_error": ("G4e: delegate nvbackend's function object over the companions (explicit list)",
                         "voltage_rel_live.py:87"),
    "_volt_rail_profile": ("G4e: b1 shim over native_rails", "druta.py:2348"),
    "_write_policy_limit": ("keep: already mirrored on native.Gpu", "client_power_runtime.py:646"),
}

#  KNOWN_GAPS: name -> (group, evidence), grouped as DESIGN-FRONTEND (b) groups
#  them. The ratchet: every listed name is still a gap and no unlisted gap
#  exists. Evidence is front-end use sites ("(class)" = GPU.X, "(getattr)" = a
#  literal getattr/hasattr), or, for a public nvbackend member the front ends
#  never use, its definition line marked "check a".
GROUPS = ("b1-shim", "b2-constant", "b3-written", "b4-private")
KNOWN_GAPS = {
    # b1: shims over existing native helper modules (37)
    "_read_volt_rail_deltas": ("b1-shim", "voltage_bios_runtime.py:54"),
    "_refresh_volt_rail_capabilities": ("b1-shim", "voltage_bios_runtime.py:106 (getattr)"),
    "_vf_lock_recovery": ("b1-shim", "druta.py:6963 (getattr)"),
    "_volt_rail_profile": ("b1-shim", "druta.py:2348"),
    "abs_limit_mv": ("b1-shim", "druta.py:2446 (class), druta.py:2462 (class), druta.py:3060 (class), "
                                "druta.py:3067 (class), profiles.py:714, profiles.py:1378, "
                                "rail_limit_profiles.py:125, +1 more"),
    "cascade_meta": ("b1-shim", "nvbackend.py:8421 (no front-end use; check a)"),
    "compute_deflatten": ("b1-shim", "app.py:1175, app.py:1666 (class), druta.py:10144 (class)"),
    "compute_hard_deflatten": ("b1-shim", "druta.py:10382 (class)"),
    "compute_ramp": ("b1-shim", "druta.py:10231 (class)"),
    "compute_rephase": ("b1-shim", "druta.py:10059 (class), druta.py:10448 (class)"),
    "evaluate_curve_law": ("b1-shim", "nvbackend.py:8362 (no front-end use; check a)"),
    "hold_headroom_allowance_mv": ("b1-shim", "nvbackend.py:7828 (no front-end use; check a)"),
    "hold_headroom_bound_mv": ("b1-shim", "nvbackend.py:7810 (no front-end use; check a)"),
    "hold_headroom_ceiling_mv": ("b1-shim", "nvbackend.py:7445 (no front-end use; check a)"),
    "hold_headroom_targets": ("b1-shim", "nvbackend.py:7485 (no front-end use; check a)"),
    "hold_legacy_p0": ("b1-shim", "druta.py:6880, druta.py:13761"),
    "legacy_p0_owned": ("b1-shim", "druta.py:6144 (getattr), druta.py:6851, druta.py:6872, druta.py:6883, "
                                   "+6 more"),
    "legacy_p0_supported": ("b1-shim", "druta.py:6838 (getattr)"),
    "peak_info": ("b1-shim", "app.py:1144 (class), app.py:1779 (class), druta.py:8997 (class), "
                             "druta.py:9001 (class), +2 more"),
    "predict_curve": ("b1-shim", "nvbackend.py:8407 (no front-end use; check a)"),
    "rail_ceiling_mv": ("b1-shim", "druta.py:2248 (class), druta.py:2464 (class)"),
    "rail_floor_mv": ("b1-shim", "nvbackend.py:7225 (no front-end use; check a)"),
    "read_power_arbitration": ("b1-shim", "registry_panel.py:1134"),
    "read_power_relationships": ("b1-shim", "registry_panel.py:1050"),
    "read_rail_live_mv": ("b1-shim", "druta.py:2221"),
    "read_volt_rail_limits": ("b1-shim", "druta.py:2347, druta.py:2354, druta.py:2380, druta.py:2397, +9 more"),
    "read_volt_rail_state": ("b1-shim", "druta.py:2403, druta.py:2989, druta.py:8358, voltage_bios_runtime.py:54"),
    "release_legacy_p0": ("b1-shim", "druta.py:6862, druta.py:6896, druta.py:6988"),
    "resolve_vf_point": ("b1-shim", "druta.py:9687 (class)"),
    "stock_limit_mv": ("b1-shim", "druta.py:8320, profiles.py:1385 (getattr), rail_limit_profiles.py:133"),
    "verification_legacy_p0": ("b1-shim", "nvbackend.py:6039 (no front-end use; check a)"),
    "verification_p0": ("b1-shim", "gpuload.py:437"),
    "volt_boost_headroom_mv": ("b1-shim", "nvbackend.py:6877 (no front-end use; check a)"),
    "volt_rail_diagnostics": ("b1-shim", "druta.py:2093 (getattr)"),
    "volt_rail_limit_fields": ("b1-shim", "druta.py:3057, profiles.py:700, profiles.py:719, profiles.py:1372, "
                                          "rail_limit_profiles.py:60, rail_limit_profiles.py:64"),
    "volt_rail_limits_mv": ("b1-shim", "nvbackend.py:7298 (no front-end use; check a)"),
    "volt_rail_limits_supported": ("b1-shim", "profiles.py:1364"),
    # b2: class-level constants (28)
    "ARCH_ADA": ("b2-constant", "nvbackend.py:2807 (no front-end use; check a)"),
    "ARCH_AMPERE": ("b2-constant", "nvbackend.py:2806 (no front-end use; check a)"),
    "ARCH_KEPLER": ("b2-constant", "druta.py:4126 (class), druta.py:11762 (class)"),
    "ARCH_MAXWELL": ("b2-constant", "druta.py:4126 (class), druta.py:11764 (class)"),
    "ARCH_NAMES": ("b2-constant", "nvbackend.py:2808 (no front-end use; check a)"),
    "ARCH_PASCAL": ("b2-constant", "druta.py:3071 (class), druta.py:4127 (class), druta.py:11767 (class)"),
    "ARCH_TURING": ("b2-constant", "nvbackend.py:2805 (no front-end use; check a)"),
    "CLKDOM_DELTA_APPLIES": ("b2-constant", "nvbackend.py:2907 (no front-end use; check a)"),
    "CLKDOM_DELTA_CLEARS_CEILING": ("b2-constant", "nvbackend.py:2917 (no front-end use; check a)"),
    "CURRENT_LIMIT_GENERATION_POLICIES": ("b2-constant", "nvbackend.py:4880 (no front-end use; check a)"),
    "EXTRA_POINTS_ABOVE_CAP": ("b2-constant", "nvbackend.py:8259 (no front-end use; check a)"),
    "HARD_FLOOR_MV": ("b2-constant", "druta.py:8613 (class)"),
    "HOLD_HEADROOM_FALLBACK_GRAIN_MV": ("b2-constant", "nvbackend.py:7351 (no front-end use; check a)"),
    "HOLD_HEADROOM_FIELDS": ("b2-constant", "nvbackend.py:7345 (no front-end use; check a)"),
    "HOLD_HEADROOM_MAX_MARGIN_MV": ("b2-constant", "vfheadroom.py:31 (class)"),
    "LIVE_RAIL_BASE": ("b2-constant", "nvbackend.py:6831 (no front-end use; check a)"),
    "LIVE_RAIL_FIELDS": ("b2-constant", "nvbackend.py:6833 (no front-end use; check a)"),
    "LIVE_RAIL_STRIDE": ("b2-constant", "nvbackend.py:6832 (no front-end use; check a)"),
    "LIVE_RAIL_VER": ("b2-constant", "nvbackend.py:6830 (no front-end use; check a)"),
    "MAX_ABS_DELTA_KHZ": ("b2-constant", "app.py:1651 (class), druta.py:9749 (class), profiles.py:1094 (getattr)"),
    "RAIL_OFFSET_MAX_MV": ("b2-constant", "druta.py:3565, druta.py:8334, tuning_profiles.py:168"),
    "RAIL_OFFSET_XOC_MAX_MV": ("b2-constant", "druta.py:3572, profiles.py:1409 (getattr), tuning_profiles.py:168"),
    "VOLT_LIMIT_BASE_MV": ("b2-constant", "nvbackend.py:6874 (no front-end use; check a)"),
    "VOLT_LIMIT_FIELDS": ("b2-constant", "druta.py:2349, druta.py:2463 (class)"),
    "VOLT_LIMIT_MAX_MV": ("b2-constant", "druta.py:3012, druta.py:8327, profiles.py:818 (getattr), "
                                         "profiles.py:1368 (getattr), rail_limit_profiles.py:130"),
    "VOLT_LIMIT_MIN_MV": ("b2-constant", "druta.py:3011, druta.py:8322, profiles.py:1384 (getattr), "
                                         "rail_limit_profiles.py:132"),
    "VOLT_LIMIT_POWERON": ("b2-constant", "nvbackend.py:7240 (no front-end use; check a)"),
    "VOLT_LIMIT_XOC_MAX_MV": ("b2-constant", "druta.py:3013, profiles.py:1367 (getattr), rail_limit_profiles.py:129"),
    # b3: attributes the front end writes (1)
    "volt_limits_write_enabled": ("b3-written", "druta.py:3141, druta.py:6248 (getattr), druta.py:13213, "
                                                "rail_limit_profiles.py:111"),
    # b4: private nvbackend members the front end reaches into, and legacy_p0_profile (10)
    "_clkdom_get": ("b4-private", "voltage_rel_live.py:61"),
    "_clkdom_valid_block": ("b4-private", "voltage_rel_live.py:62"),
    "_msvdd_offset_domains_seen": ("b4-private", "profiles.py:747 (getattr)"),
    "_msvdd_offset_domains_written": ("b4-private", "profiles.py:746 (getattr)"),
    "_offset_range": ("b4-private", "tuning_profiles.py:90, tuning_profiles.py:157"),
    "_pstate20_clock": ("b4-private", "bios_runtime.py:32"),
    "_read_power": ("b4-private", "druta.py:6558 (getattr)"),
    "_read_pstates20": ("b4-private", "all_pstates_registry.py:346, bios_runtime.py:25, voltage_rel_live.py:53"),
    "_vfp_curve_error": ("b4-private", "voltage_rel_live.py:87"),
    "legacy_p0_profile": ("b4-private", "druta.py:6842 (getattr)"),
}

#  Kind differences between shared members (check d), each with its reason.
KIND_DIFFERENCES = {
    "msvdd_write_enabled": "a plain bool on nvbackend.GPU, a settable property on native.Gpu "
                           "(DESIGN-FRONTEND 0 and b3: no action)",
    "voltage_xoc_enabled": "a plain bool on nvbackend.GPU, a settable property on native.Gpu "
                           "(DESIGN-FRONTEND 0 and b3: no action)",
}

#  Companion uses (nvapi / api / nvml / nv expressions) that are not the
#  driver objects' surface: (half, name) -> reason.
COMPANION_FRONTEND_OWNED = {
    ("nvapi", "_up9512r_mutex"): "created by the front end on first use (controllers/up9512r.py:98-100); "
                                 "it must round-trip through setattr on the companion",
    ("nvml", "get"): "false positive: nv is a dict at all_pstates_registry.py:195-199",
    ("nvml", "values"): "false positive: nv is a dict at all_pstates_registry.py:195-196",
}

#  COMPANION_GAPS: (half, name) -> evidence: names used on nvapi / api / nvml /
#  nv expressions that today's native.Gpu(...).nvapi / .nvml (native._Half)
#  lack. Measured against a native.Gpu constructed over a fake card. Check (c)
#  (G4d, the companions) replaces this table.
COMPANION_GAPS = {
    ("nvapi", "AllClocks"): "rated_clock_runtime.py:83 (getattr)",
    ("nvapi", "BoostTableGet"): "factory_clock_runtime.py:70 (getattr), voltage_rel_live.py:70",
    ("nvapi", "ClkDomCtlGet"): "clock_bios_runtime.py:244 (getattr)",
    ("nvapi", "ClkDomCtlSet"): "tuning_profiles.py:97 (getattr), tuning_profiles.py:106 (getattr)",
    ("nvapi", "GetArchInfo"): "controllers/up9512r.py:120 (getattr), power_runtime.py:66 (getattr)",
    ("nvapi", "Pstates20Get"): "voltage_rel_live.py:56 (getattr)",
    ("nvapi", "Pstates20Set"): "tuning_profiles.py:88 (getattr)",
    ("nvapi", "VfpCurve"): "voltage_rel_live.py:66 (getattr), voltage_rel_live.py:71",
    ("nvapi", "VoltCtrlGet"): "tuning_profiles.py:117 (getattr), voltage_ceiling_runtime.py:70 (getattr)",
    ("nvapi", "VoltCtrlSet"): "tuning_profiles.py:117 (getattr)",
    ("nvapi", "VoltRailsCtlGet"): "voltage_rail_control_runtime.py:108 (getattr)",
    ("nvapi", "_i"): "all_pstates_registry.py:329, clock_bios_runtime.py:212, controllers/icx.py:214, +16 more",
    ("nvapi", "ver"): "voltage_rel_live.py:77",
    ("nvml", "dev"): "bios_runtime.py:129, profiles.py:136",
    ("nvml", "dll"): "bios_runtime.py:119, profiles.py:135",
    ("nvml", "dll.nvmlDeviceGetFanControlPolicy_v2"): "profiles.py:135",
    ("nvml", "dll.nvmlDeviceGetTemperatureThreshold"): "bios_runtime.py:119",
    ("nvml", "has"): "bios_runtime.py:117, power_action_profiles.py:13, profiles.py:132, +1 more",
}


#  UNRESOLVED_DYNAMIC: (file, call source) -> "file:line: reason" for each
#  getattr / hasattr / setattr / delattr on a GPU or companion expression whose
#  name the scanner cannot resolve to literals. Ratcheted: a new dynamic access
#  fails the test until it is reviewed and listed here. Keyed by the call's
#  source text rather than its line, so unrelated edits above it do not move
#  the key. Empty on the measured tree: the only dynamic site
#  (vfe_equation_runtime.py:231) loops over a literal tuple and is resolved.
UNRESOLVED_DYNAMIC = {}


def private_reaches_measured():
    """Every private name the front ends reach on a GPU object, its type or GPU.X."""
    return {u.name for u in scan()[0] if u.name.startswith("_")}


def evidence_files(evidence):
    """The front-end files an evidence string cites (``file:line`` tokens),
    excluding nvbackend.py (a definition line, not a use)."""
    import re
    return {m for m in re.findall(r"([\w/]+\.py):\d+", evidence) if m != "nvbackend.py"}
