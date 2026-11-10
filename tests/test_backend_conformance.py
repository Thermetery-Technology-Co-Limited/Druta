# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Front-end surface conformance between nvbackend.GPU and native.Gpu (G4).

native/DESIGN-FRONTEND.md section (f) item 2. The scanner and the tables are in
tests/frontend_surface.py. KNOWN_GAPS is empty after G4e/f, and remains a ratchet that
fails in both directions: a gap that closes must be removed from the list, and
a new gap (a front-end use native lacks, or a public nvbackend.GPU member
native lacks) must be either closed or listed with its evidence.

All checks (a)-(h) run, plus the ResetStep identity. Static checks need no
built library. Core constants and checks that construct a native.Gpu
over a fake card (the fakes of tests/test_native_identity.py) skip cleanly when
the library is not built, as tests/test_native_identity.py does:
ConstructedNativeTests (b on an instance, stored-name round-trips, ResetStep
identity, and the real loader-backed companions)."""
import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from druta import native, nvbackend
from tests import frontend_surface as fs

NATIVE_BUILT = native.available()
SKIP_REASON = "native core not built (native/build.sh linux)"

# (file, name, via or None): sites DESIGN-FRONTEND section (b) (and section 0)
# cites. Line numbers drift; the scanner must find the name in that file.
DESIGN_B_SITES = [
    # b1: native_rails
    ("druta.py", "read_volt_rail_limits", None),
    ("druta.py", "read_volt_rail_state", None),
    ("voltage_bios_runtime.py", "read_volt_rail_state", None),
    ("druta.py", "read_rail_live_mv", None),
    ("druta.py", "_volt_rail_profile", None),
    ("druta.py", "volt_rail_diagnostics", "getattr"),
    ("profiles.py", "volt_rail_limits_supported", None),
    ("druta.py", "volt_rail_limit_fields", None),
    ("profiles.py", "volt_rail_limit_fields", None),
    ("rail_limit_profiles.py", "volt_rail_limit_fields", None),
    ("druta.py", "stock_limit_mv", None),
    ("profiles.py", "stock_limit_mv", None),
    ("rail_limit_profiles.py", "stock_limit_mv", None),
    ("voltage_bios_runtime.py", "_read_volt_rail_deltas", None),
    ("voltage_bios_runtime.py", "_refresh_volt_rail_capabilities", "getattr"),
    # b1: native_legacy_p0
    ("druta.py", "legacy_p0_supported", "getattr"),
    ("druta.py", "legacy_p0_owned", None),
    ("druta.py", "hold_legacy_p0", None),
    ("druta.py", "release_legacy_p0", None),
    ("gpuload.py", "verification_p0", None),
    ("druta.py", "_vf_lock_recovery", "getattr"),
    # b1: native_vfplan and native_rail_limits
    ("app.py", "compute_deflatten", "attr"),
    ("profiles.py", "abs_limit_mv", None),
    ("rail_limit_profiles.py", "abs_limit_mv", None),
    ("registry_panel.py", "read_power_arbitration", None),
    ("registry_panel.py", "read_power_relationships", None),
    # b2: constants, on the instance and as GPU.X
    ("druta.py", "LOCK_STEP", None),
    ("druta.py", "VF_LOCK_STEP", None),
    ("druta.py", "P0_LOCK_STEP", None),
    ("druta.py", "ARCH_PASCAL", "class"),
    ("druta.py", "VOLT_LIMIT_MIN_MV", "attr"),
    ("druta.py", "VOLT_LIMIT_MAX_MV", "attr"),
    ("druta.py", "VOLT_LIMIT_XOC_MAX_MV", "attr"),
    ("druta.py", "RAIL_OFFSET_MAX_MV", "attr"),
    ("druta.py", "RAIL_OFFSET_XOC_MAX_MV", "attr"),
    ("druta.py", "VOLT_LIMIT_FIELDS", "attr"),
    ("rail_limit_profiles.py", "VOLT_LIMIT_XOC_MAX_MV", None),
    ("tuning_profiles.py", "RAIL_OFFSET_MAX_MV", None),
    # profiles.py's literal getattr reads of the five envelope constants
    # (design section b2) became backend_envelope's one dynamic read in G4f:
    # see UNRESOLVED_DYNAMIC and test_envelope_constants_are_tracked.
    ("app.py", "peak_info", "class"),
    ("app.py", "MAX_ABS_DELTA_KHZ", "class"),
    ("app.py", "compute_deflatten", "class"),
    ("vfheadroom.py", "HOLD_HEADROOM_MAX_MARGIN_MV", "class"),
    # b3: attributes the front end writes
    ("druta.py", "voltage_xoc_enabled", None),
    ("druta.py", "msvdd_write_enabled", None),
    ("druta.py", "volt_limits_write_enabled", None),
    ("rail_limit_profiles.py", "volt_limits_write_enabled", None),
    ("druta.py", "power_policy_names", None),
    ("profiles.py", "power_policy_names", None),
    ("druta.py", "_power_policy_pins", None),
    ("power_action_profiles.py", "_power_policy_pins", None),
    ("profiles.py", "_power_policy_pins", None),
    ("power_integral_runtime.py", "_power_policy_mask", None),
    ("client_power_runtime.py", "_current_limit_transport", None),
    ("client_power_runtime.py", "_current_limit_layout_choice", None),
    ("druta.py", "_foreign_hold_headroom", None),
    ("druta.py", "_hold_headroom_live_trip", None),
    # b4: private reaches
    # (G4f: the design's _msvdd_offset_domains_written / _seen, _read_power and
    # _offset_range sites now use the public methods that replace them)
    ("profiles.py", "msvdd_offset_domains", "getattr"),
    ("druta.py", "read_enforced_power_limit_mw", "getattr"),
    ("tuning_profiles.py", "clock_offset_range", "attr"),
    ("all_pstates_registry.py", "_read_pstates20", None),
    ("bios_runtime.py", "_read_pstates20", None),
    ("voltage_rel_live.py", "_read_pstates20", None),
    ("bios_runtime.py", "_pstate20_clock", None),
    ("voltage_rel_live.py", "_clkdom_get", None),
    ("voltage_rel_live.py", "_clkdom_valid_block", None),
    ("voltage_rel_live.py", "_vfp_curve_error", None),
    ("druta.py", "legacy_p0_profile", "getattr"),
    # the false positive section (b) names
    ("pcie_speed_panel.py", "get", None),
]
# (file, half, name): companion sites DESIGN-FRONTEND cites (b3 and section 0).
DESIGN_COMPANION_SITES = [
    ("controllers/up9512r.py", "nvapi", "_up9512r_mutex"),
    ("bios_runtime.py", "nvml", "dll"),
    ("profiles.py", "nvml", "dll"),
]


def ratchet_problems(nvb_cls=None, native_cls=None):
    """Every way the measured gap set differs from KNOWN_GAPS (empty when it matches)."""
    measured = fs.measured_gaps(nvb_cls, native_cls)
    problems = [f"unlisted gap: {name} (found by check {'+'.join(sorted(checks))})"
                for name, checks in sorted(measured.items()) if name not in fs.KNOWN_GAPS]
    problems += [f"listed gap closed, remove it from KNOWN_GAPS: {name}"
                 for name in sorted(fs.KNOWN_GAPS) if name not in measured]
    return problems


def private_reach_problems():
    reached = fs.private_reaches_measured()
    return ([f"unlisted private reach: {n}" for n in sorted(reached - set(fs.PRIVATE_REACHES))]
            + [f"listed private reach no longer used: {n}" for n in sorted(set(fs.PRIVATE_REACHES) - reached)])


def added(problems, baseline):
    """The problems a synthetic change adds over the tree's own, so these mutation tests
    stay about their own injection whatever the real tree's ratchet says."""
    return sorted(set(problems) - set(baseline))


def dynamic_problems():
    found = {}
    for site in fs.dynamic_sites():
        found.setdefault(site.key, []).append(site)
    problems = []
    for key in sorted(found):
        sites = sorted(found[key], key=lambda d: d.line)
        listed = fs.UNRESOLVED_DYNAMIC.get(key)
        if listed is None:
            problems += [f"unreviewed dynamic access: {d.file}:{d.line} {d.call}" for d in sites]
        elif len(sites) != listed[0]:
            lines = ", ".join(f"{d.file}:{d.line}" for d in sites)
            problems.append(f"reviewed dynamic access {key} occurs {len(sites)} times, "
                            f"listed {listed[0]}: {lines}")
    return problems + [f"listed dynamic access gone: {k}" for k in sorted(set(fs.UNRESOLVED_DYNAMIC) - set(found))]


def _scan_source(source, filename="sample.py"):
    """Scan one source text as if it were a front-end file: (gpu_uses, companion_uses, dynamic)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / filename
        path.write_text(source)
        dynamic = []
        with patch.object(fs, "SRC", Path(tmp)):
            gpu, companion = fs.scan_file(path, dynamic)
    return gpu, companion, dynamic


def _with_extra(source, filename="sample.py"):
    """Patch the cached front-end scan to include ``source``'s uses and dynamic sites,
    attributed to ``filename`` (an existing front-end file name adds to that file)."""
    gpu, companion = fs.scan()
    extra_gpu, extra_companion, extra_dynamic = _scan_source(source, filename)
    return patch.dict(fs._CACHE, {"scan": (gpu + extra_gpu, companion + extra_companion),
                                  "dynamic": fs.dynamic_sites() + extra_dynamic})


def _lock_uses(source):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.py"
        path.write_text(source)
        return fs.lock_method_uses([path])


def _kind(cls, name):
    value = inspect.getattr_static(cls, name)
    for label, kind in (("staticmethod", staticmethod), ("classmethod", classmethod), ("property", property)):
        if isinstance(value, kind):
            return label
    if inspect.isfunction(value):
        return "function"
    return "callable" if callable(value) else "data"


def _shared_names():
    both = set(dir(nvbackend.GPU)) & set(dir(native.Gpu))
    return sorted(n for n in both if not (n.startswith("__") and n.endswith("__")))


def _companion_static_surface():
    """The real companion classes plus their explicit named interface table."""
    from druta import native_companions
    path = Path(native_companions.__file__)
    return {
        "nvapi": set(dir(native_companions.BoundNvAPI))
                 | fs.init_stores(path, "BoundNvAPI")
                 | {row[0] for row in nvbackend.NVAPI_INTERFACES},
        "nvml": set(dir(native_companions.BoundNvml))
                | fs.init_stores(path, "BoundNvml"),
    }


class ScannerTests(unittest.TestCase):
    def test_scan_covers_the_front_ends_and_excludes_the_backends(self):
        files = {p.relative_to(fs.SRC).as_posix() for p in fs.frontend_files()}
        for name in ("druta.py", "app.py", "profiles.py", "controllers/up9512r.py"):
            self.assertIn(name, files)
        for name in ("nvbackend.py", "native.py", "native_rails.py", "gpu_backend.py"):
            self.assertNotIn(name, files)
        self.assertFalse([f for f in files if f.startswith("tools/")])

    def test_scanner_finds_every_site_the_design_cites(self):
        self.assertGreaterEqual(len(DESIGN_B_SITES), 10)
        uses = fs.scan()[0]
        missing = [(file, name, via) for file, name, via in DESIGN_B_SITES
                   if not any(u.file == file and u.name == name and (via is None or u.via == via) for u in uses)]
        self.assertEqual(missing, [])
        companion = fs.scan()[1]
        missing = [site for site in DESIGN_COMPANION_SITES
                   if not any((u.file, u.receiver, u.name) == site for u in companion)]
        self.assertEqual(missing, [])

    def test_scanner_records_each_receiver_and_access_form(self):
        source = (
            "def f(self, gpu, other_gpu, fresh, g, app, nvapi, nv):\n"
            "    gpu.a; other_gpu.b; fresh.c; g.d; self.gpu.e; app.gpu.f; self.app.gpu.g2\n"
            "    self.gpu.h = 1\n"
            "    del gpu.i\n"
            "    getattr(gpu, 'j', None); hasattr(self.gpu, 'k'); setattr(gpu, 'l', 1)\n"
            "    GPU.M; type(gpu).N; getattr(type(self.gpu), 'O')\n"
            "    nvapi.P; self.gpu.nvapi.Q; nv.dll.R; gpu.nvml.S\n"
            "    unrelated.T\n"
            "    getattr(getattr(self, 'gpu', None), 'U', None); getattr(self, 'gpu').V\n"
            "    type(getattr(self, 'gpu', None)).W; getattr(self.app, 'gpu').X2\n"
            "    nvapi.gpu.not_a_backend; getattr(self.nvapi, 'gpu', None).also_not\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(source)
            with patch.object(fs, "SRC", Path(tmp)):
                gpu, companion = fs.scan_file(path)
        got = {(u.name, u.kind, u.via) for u in gpu}
        for expected in [("a", "load", "attr"), ("b", "load", "attr"), ("c", "load", "attr"),
                         ("d", "load", "attr"), ("e", "load", "attr"), ("f", "load", "attr"),
                         ("g2", "load", "attr"), ("h", "store", "attr"), ("i", "del", "attr"),
                         ("j", "load", "getattr"), ("k", "load", "getattr"), ("l", "store", "getattr"),
                         ("M", "load", "class"), ("N", "load", "type"), ("O", "load", "type"),
                         ("U", "load", "getattr"), ("V", "load", "attr"), ("W", "load", "type"),
                         ("X2", "load", "attr")]:
            self.assertIn(expected, got)
        names = {u.name for u in gpu}
        self.assertNotIn("T", names)
        self.assertFalse({"not_a_backend", "also_not"} & names)
        self.assertEqual({(u.receiver, u.name) for u in companion} - {("nvml", "dll"), ("nvapi", "gpu")},
                         {("nvapi", "P"), ("nvapi", "Q"), ("nvml", "dll.R"), ("nvml", "S")})


class DynamicNameTests(unittest.TestCase):
    """getattr / hasattr / setattr / delattr with a non-literal name (S2)."""

    def test_literal_loop_and_local_names_are_resolved(self):
        gpu, _, dynamic = _scan_source(
            "def f(gpu):\n"
            "    for name in ('_a', '_b'):\n"
            "        setattr(gpu, name, None)\n"
            "    names = ['_c', '_d']\n"
            "    for other in names:\n"
            "        hasattr(self.gpu, other)\n"
            "    one = '_e'\n"
            "    delattr(gpu, one)\n")
        self.assertEqual(dynamic, [])
        self.assertEqual({(u.name, u.kind) for u in gpu},
                         {("_a", "store"), ("_b", "store"), ("_c", "load"), ("_d", "load"), ("_e", "del")})

    def test_unresolvable_names_are_collected_not_ignored(self):
        _, _, dynamic = _scan_source(
            "def f(gpu, name):\n"
            "    getattr(gpu, name)\n"
            "def g(gpu, rows):\n"
            "    for name in rows:\n"
            "        setattr(gpu, name, None)\n"
            "def h(gpu):\n"
            "    getattr(gpu, 'x' + 'y')\n"
            "    for n in ('_a',):\n"
            "        pass\n"
            "    n = compute()\n"
            "    getattr(gpu, n)\n"
            "def unrelated(obj, name):\n"
            "    getattr(obj, name)\n")
        self.assertEqual(sorted((d.line, d.scope, d.call) for d in dynamic),
                         [(2, "f", "getattr(gpu, name)"), (5, "g", "setattr(gpu, name, None)"),
                          (7, "h", "getattr(gpu, 'x' + 'y')"), (11, "h", "getattr(gpu, n)")])

    def test_a_dynamic_site_records_its_qualified_scope(self):
        _, _, dynamic = _scan_source(
            "getattr(gpu, name)\n"
            "class C:\n"
            "    def m(self, name):\n"
            "        def inner(n):\n"
            "            return getattr(self.gpu, n)\n"
            "        return (lambda k: getattr(gpu, k))\n")
        self.assertEqual(sorted((d.line, d.scope) for d in dynamic),
                         [(1, "<module>"), (5, "C.m.inner"), (6, "C.m.<lambda>")])

    def test_the_vfe_loop_stores_are_scanned(self):
        sites = {(u.name, u.kind) for u in fs.scan()[0] if u.file == "vfe_equation_runtime.py"}
        self.assertLessEqual({("_current_limit_transport", "store"), ("_current_limit_layout_choice", "store")}, sites)

    def test_unresolved_dynamic_accesses_are_reviewed(self):
        self.assertEqual(dynamic_problems(), [])
        for key, (count, reason) in fs.UNRESOLVED_DYNAMIC.items():
            self.assertEqual(len(key), 3)
            self.assertIs(type(count), int)
            self.assertGreaterEqual(count, 1)
            self.assertTrue(reason)

    def test_the_same_call_in_another_function_of_the_file_turns_the_ratchet_red(self):
        # A reviewed key whitelists one function, not every identical call text in the file.
        before = dynamic_problems()
        with _with_extra("def _sneak(gpu, name):\n    return getattr(gpu, name)\n", "profiles.py"):
            self.assertEqual(added(dynamic_problems(), before),
                             ["unreviewed dynamic access: profiles.py:2 getattr(gpu, name)"])

    def test_another_identical_call_inside_the_reviewed_function_turns_the_ratchet_red(self):
        before = dynamic_problems()
        source = "def backend_envelope(gpu, name):\n    return getattr(gpu, name)\n"
        _, _, extra = _scan_source(source, "profiles.py")
        self.assertEqual([d.key for d in extra], [("profiles.py", "backend_envelope", "getattr(gpu, name)")])
        with _with_extra(source, "profiles.py"):
            problems = added(dynamic_problems(), before)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith(
            "reviewed dynamic access ('profiles.py', 'backend_envelope', 'getattr(gpu, name)') occurs 2 times, "
            "listed 1: profiles.py:"), problems)

    def test_envelope_constants_are_tracked(self):
        # backend_envelope's dynamic read reaches exactly ENVELOPE_MEANINGS'
        # keys: each must be an nvbackend.GPU number and, while native.Gpu
        # lacks it, a listed b2-constant gap, so the dynamic read hides none.
        # Either on native.Gpu's static surface or a listed b2-constant gap,
        # never both and never neither; the library plays no part.
        from druta import profiles
        self.assertIn(("profiles.py", "backend_envelope", "getattr(gpu, name)"), fs.UNRESOLVED_DYNAMIC)
        native_surface = fs.native_static_surface()
        for name in profiles.ENVELOPE_MEANINGS:
            with self.subTest(name=name):
                self.assertIn(type(getattr(nvbackend.GPU, name)), (int, float))
                on_native = name in native_surface
                gap = fs.KNOWN_GAPS.get(name, (None,))[0] == "b2-constant"
                self.assertNotEqual(on_native, gap,
                                    f"{name}: on native.Gpu={on_native}, b2-constant gap={gap}; "
                                    "exactly one must hold")

    def test_a_new_dynamic_access_turns_the_ratchet_red(self):
        before = dynamic_problems()
        with _with_extra("def f(self, name):\n    setattr(self.gpu, name, None)\n"):
            self.assertEqual(added(dynamic_problems(), before),
                             ["unreviewed dynamic access: sample.py:2 setattr(self.gpu, name, None)"])

    def test_a_new_private_name_from_a_literal_loop_turns_the_ratchet_red(self):
        source = "def _g4a_probe(self):\n    for _n in ('_third_new_private',):\n        setattr(self.gpu, _n, None)\n"
        with _with_extra(source):
            self.assertIn("unlisted private reach: _third_new_private", private_reach_problems())
            self.assertIn("unlisted gap: _third_new_private (found by check b)", ratchet_problems())


class SurfaceRatchetTests(unittest.TestCase):
    """Checks (a) and (b) on the classes, frozen by KNOWN_GAPS."""

    def test_known_gaps_equal_the_measured_gaps(self):
        self.assertEqual(ratchet_problems(), [])

    def test_a_new_public_nvbackend_member_turns_the_ratchet_red(self):
        class Grown(nvbackend.GPU):
            def dummy_public_member(self):
                return None
        before = ratchet_problems()
        self.assertEqual(added(ratchet_problems(nvb_cls=Grown), before),
                         ["unlisted gap: dummy_public_member (found by check a)"])
        with patch.object(nvbackend.GPU, "DUMMY_CONSTANT", 1, create=True):
            self.assertEqual(added(ratchet_problems(), before), ["unlisted gap: DUMMY_CONSTANT (found by check a)"])
        self.assertEqual(ratchet_problems(), before)

    def test_a_new_front_end_use_turns_the_ratchet_red(self):
        extra = fs.Use("dummy_front_end_name", "load", "attr", "druta.py", 1)
        gpu, companion = fs.scan()
        before = ratchet_problems()
        with patch.dict(fs._CACHE, {"scan": (gpu + [extra], companion)}):
            self.assertEqual(added(ratchet_problems(), before), ["unlisted gap: dummy_front_end_name (found by check b)"])

    def test_a_new_use_through_getattr_self_gpu_turns_the_ratchet_red(self):
        source = ("def f(self):\n"
                  "    getattr(getattr(self, 'gpu', None), 'brand_new_member', None)\n"
                  "    getattr(self, 'gpu').another_new_member()\n"
                  "    type(getattr(self, 'gpu', None)).third_new_member\n")
        before = ratchet_problems()
        with _with_extra(source):
            self.assertEqual(added(ratchet_problems(), before),
                             ["unlisted gap: another_new_member (found by check b)",
                              "unlisted gap: brand_new_member (found by check b)",
                              "unlisted gap: third_new_member (found by check b)"])

    def test_closing_a_gap_turns_the_ratchet_red_until_it_is_removed(self):
        # The production ratchet is empty; a stale entry must still fail.
        with patch.dict(fs.KNOWN_GAPS, {"read_rail_live_mv": ("b1-shim", "druta.py")}):
            self.assertIn("listed gap closed, remove it from KNOWN_GAPS: read_rail_live_mv",
                          ratchet_problems())

    def test_every_known_gap_is_grouped_and_its_evidence_holds(self):
        uses = fs.by_name(fs.scan()[0])
        public = fs.nvbackend_public_members()
        for name, (group, evidence) in fs.KNOWN_GAPS.items():
            with self.subTest(name=name):
                self.assertIn(group, fs.GROUPS)
                files = fs.evidence_files(evidence)
                if "check a" in evidence:
                    self.assertEqual(files, set())
                    self.assertNotIn(name, uses)  # no front-end use: found by (a) alone
                    self.assertIn(name, public)
                else:
                    self.assertTrue(files)
                    self.assertLessEqual(files, {u.file for u in uses.get(name, [])})

    def test_table_evidence_carries_no_line_numbers(self):
        # Lines drift; a table mixing line snapshots of different revisions points at
        # the wrong code. Cite the file; `python3 tests/frontend_surface.py NAME` prints lines.
        import re
        stale = [(table, key, text) for table, key, text in fs.table_strings()
                 if re.search(fs.LINE_CITATION, text)]
        self.assertEqual(stale, [], "table evidence must cite files, not file:line (lines come from "
                                    "fs.lookup / `python3 tests/frontend_surface.py NAME`)")

    def test_evidence_files_are_citations_not_prose_or_definitions(self):
        self.assertEqual(fs.evidence_files("druta.py (getattr), controllers/up9512r.py, profiles.py, and more"),
                         {"druta.py", "controllers/up9512r.py", "profiles.py"})
        self.assertEqual(fs.evidence_files("nvbackend.py (no front-end use; check a)"), set())
        self.assertEqual(fs.evidence_files("druta.py; also [profiles.py] backend_envelope"), {"druta.py"})

    def test_a_cited_file_without_the_name_fails_the_evidence_check(self):
        # The evidence checks stay as strict as with line citations: a file in which
        # the scanner does not find the name is not accepted.
        uses = fs.by_name(fs.scan()[0])
        evidence = "druta.py, rail_limit_profiles.py; also [profiles.py] backend_envelope"
        self.assertLessEqual(fs.evidence_files(evidence), {u.file for u in uses["VOLT_LIMIT_MAX_MV"]})
        self.assertNotIn("profiles.py", {u.file for u in uses["VOLT_LIMIT_MAX_MV"]})
        bad = evidence.replace("[profiles.py]", "profiles.py")
        self.assertFalse(fs.evidence_files(bad) <= {u.file for u in uses["VOLT_LIMIT_MAX_MV"]})

    def test_lookup_prints_the_scanners_live_lines(self):
        uses = [u for u in fs.scan()[0] if u.name == "clock_offset_range"]
        self.assertTrue(uses)
        out = fs.lookup(["clock_offset_range"])
        self.assertEqual(out[0], "clock_offset_range:")
        self.assertEqual(sorted(line.split()[0] for line in out[1:] if not line.lstrip().startswith(("dynamic",
                                                                                                       "defined"))),
                         sorted(u.where for u in uses))
        self.assertEqual({u.file for u in uses}, {"tuning_profiles.py"})
        definition = [line for line in out if line.lstrip().startswith("defined at nvbackend.py:")]
        self.assertEqual(len(definition), 1)
        line = int(definition[0].rsplit(":", 1)[1])
        source = fs.NVBACKEND.read_text(encoding="utf-8-sig").splitlines()
        self.assertIn("def clock_offset_range(", source[line - 1])
        dynamic = fs.lookup(["VOLT_LIMIT_MAX_MV", "power_policy_names"])
        self.assertIn("  no front-end use", fs.lookup(["_g4f_no_such_name"]))
        self.assertTrue(any(line.startswith("  dynamic in profiles.py:") and "backend_envelope" in line
                            for line in dynamic))

    def test_exempt_members_exist_on_nvbackend_only_and_have_no_front_end_use(self):
        uses = fs.by_name(fs.scan()[0])
        for name, reason in fs.EXEMPT.items():
            with self.subTest(name=name):
                self.assertTrue(reason)
                self.assertTrue(hasattr(nvbackend.GPU, name))
                self.assertNotIn(name, fs.native_static_surface())
                self.assertNotIn(name, uses)

    def test_front_end_owned_names_belong_to_neither_backend(self):
        uses = fs.by_name(fs.scan()[0])
        for name, reason in fs.FRONTEND_OWNED.items():
            with self.subTest(name=name):
                self.assertLessEqual(fs.evidence_files(reason), {u.file for u in uses.get(name, [])})
                self.assertTrue(fs.evidence_files(reason))
                self.assertNotIn(name, fs.nvbackend_public_members())
                self.assertFalse(hasattr(nvbackend.GPU, name))
                self.assertNotIn(name, fs.native_static_surface())
                self.assertNotIn(name, fs.KNOWN_GAPS)

    def test_private_reaches_are_listed_with_a_decision(self):
        self.assertEqual(private_reach_problems(), [])
        self.assertIn("_policy_amount", fs.PRIVATE_REACHES)  # reached only as GPU._policy_amount
        uses = fs.by_name(fs.scan()[0])
        native_surface = fs.native_static_surface()
        for name, (decision, evidence) in fs.PRIVATE_REACHES.items():
            with self.subTest(name=name):
                self.assertLessEqual(fs.evidence_files(evidence), {u.file for u in uses[name]})
                if decision.startswith("keep"):
                    self.assertIn(name, native_surface)
                else:
                    self.assertIn(name, fs.KNOWN_GAPS)

    def test_a_new_private_class_level_reach_turns_the_ratchet_red(self):
        before = private_reach_problems()
        with _with_extra("def f(gpu):\n    GPU._policy_amount(1, 'mW')\n    GPU._something(1)\n"
                         "    type(gpu)._other_thing\n"):
            self.assertEqual(added(private_reach_problems(), before),
                             ["unlisted private reach: _other_thing", "unlisted private reach: _something"])

    def test_gpu_namespace_references_exist_on_nvbackend(self):
        missing = sorted({u.name for u in fs.class_uses() if not hasattr(nvbackend.GPU, u.name)})
        self.assertEqual(missing, [])


class KindSignatureValueTests(unittest.TestCase):
    """Checks (d) and (e)."""

    def test_shared_members_have_the_same_descriptor_kind(self):
        differ = {n for n in _shared_names() if _kind(nvbackend.GPU, n) != _kind(native.Gpu, n)}
        self.assertEqual(sorted(differ), sorted(fs.KIND_DIFFERENCES))
        for name in fs.KIND_DIFFERENCES:
            self.assertIsInstance(inspect.getattr_static(native.Gpu, name), property)
            self.assertIsNotNone(inspect.getattr_static(native.Gpu, name).fset)

    def test_shared_callables_have_the_same_signature(self):
        differ = []
        for name in _shared_names():
            kind = _kind(nvbackend.GPU, name)
            if kind not in ("function", "staticmethod", "classmethod") or name in fs.KIND_DIFFERENCES:
                continue
            ours, theirs = inspect.signature(getattr(nvbackend.GPU, name)), inspect.signature(getattr(native.Gpu, name))
            if ours != theirs:
                differ.append(f"{name}: nvbackend{ours} native{theirs}")
        self.assertEqual(differ, [])

    def test_synchronized_methods_expose_their_real_signatures(self):
        wrapped = [n for n in dir(nvbackend.GPU)
                   if inspect.isfunction(inspect.getattr_static(nvbackend.GPU, n))
                   and hasattr(getattr(nvbackend.GPU, n), "__wrapped__")]
        self.assertGreaterEqual(len(wrapped), 40)
        for name in ("read", "reset_all", "set_power_limit_mw", "hold_legacy_p0"):
            self.assertIn(name, wrapped)
        for name in wrapped:
            fn = getattr(nvbackend.GPU, name)
            self.assertEqual(inspect.signature(fn), inspect.signature(inspect.unwrap(fn)), name)
            self.assertNotEqual(list(inspect.signature(fn).parameters)[1:], ["a", "kw"], name)

    def test_synchronized_still_holds_the_lock_and_keeps_name_and_doc(self):
        entered = []

        class Lock:
            def __enter__(self):
                entered.append(threading.current_thread())

            def __exit__(self, *exc):
                return False

        def sample(self, x, *, y=2):
            """doc"""
            return (x, y, len(entered))

        wrapped = nvbackend._synchronized(sample)
        holder = type("Holder", (), {"_lock": Lock()})()
        self.assertEqual(wrapped(holder, 1, y=3), (1, 3, 1))
        self.assertEqual((wrapped.__name__, wrapped.__doc__), ("sample", "doc"))
        self.assertEqual(str(inspect.signature(wrapped)), "(self, x, *, y=2)")

    def test_shared_constants_are_equal(self):
        differ = []
        for name in _shared_names():
            if _kind(nvbackend.GPU, name) == "data" and name not in fs.KIND_DIFFERENCES:
                from druta.native_frontend import _CoreConstant
                if not NATIVE_BUILT and isinstance(inspect.getattr_static(native.Gpu, name), _CoreConstant):
                    continue  # the core supplies these values; checked when built below
                ours, theirs = getattr(nvbackend.GPU, name), getattr(native.Gpu, name)
                if ours != theirs:
                    differ.append(f"{name}: {ours!r} != {theirs!r}")
        self.assertEqual(differ, [])

    def test_gpu_namespace_references_native_carries_match(self):
        native_surface = set(dir(native.Gpu))
        for name in sorted({u.name for u in fs.class_uses()} & native_surface):
            with self.subTest(name=name):
                self.assertEqual(_kind(nvbackend.GPU, name), _kind(native.Gpu, name))
                if _kind(nvbackend.GPU, name) == "data":
                    from druta.native_frontend import _CoreConstant
                    if not NATIVE_BUILT and isinstance(inspect.getattr_static(native.Gpu, name), _CoreConstant):
                        continue
                    self.assertEqual(getattr(nvbackend.GPU, name), getattr(native.Gpu, name))

    @unittest.skipUnless(NATIVE_BUILT, SKIP_REASON)
    def test_core_envelope_matches_nvbackend(self):
        from druta import native_frontend
        for name, value in native_frontend.request_envelopes().items():
            self.assertEqual(value, getattr(nvbackend.GPU, name.upper()), name)


class LockUseTests(unittest.TestCase):
    """Check (h)."""

    def test_front_ends_never_acquire_release_or_query_a_gpu_lock(self):
        self.assertEqual(fs.lock_method_uses(), [])

    def test_the_detector_finds_direct_and_aliased_lock_calls(self):
        source = ("def f(self, gpu):\n"
                  "    gpu._lock.acquire()\n"
                  "    lock = getattr(self.gpu, '_lock', None)\n"
                  "    lock.release()\n"
                  "    with gpu._lock as held:\n"
                  "        held.locked()\n"
                  "    self._lock.acquire()\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(source)
            self.assertEqual(sorted(fs.lock_method_uses([path])),
                             ["sample.py:2 acquire", "sample.py:4 release", "sample.py:6 locked"])

    def test_the_detector_finds_attribute_conditional_and_dunder_holds(self):
        self.assertEqual(_lock_uses("class C:\n"
                                    "    def hold(self):\n"
                                    "        self._held = self.gpu._lock\n"
                                    "    def go(self):\n"
                                    "        self._held.acquire()\n"), ["sample.py:5 acquire"])
        self.assertEqual(_lock_uses("def f(self, gpu):\n"
                                    "    lock = gpu._lock if gpu else None\n"
                                    "    lock.acquire()\n"
                                    "    other = getattr(self.gpu, '_lock', None) or nullcontext()\n"
                                    "    other.release()\n"),
                         ["sample.py:3 acquire", "sample.py:5 release"])
        self.assertEqual(_lock_uses("def f(gpu):\n"
                                    "    gpu._lock.__enter__()\n"
                                    "    gpu._lock.__exit__(None, None, None)\n"),
                         ["sample.py:2 __enter__", "sample.py:3 __exit__"])

    def test_aliases_are_scoped_to_their_function_and_class(self):
        self.assertEqual(_lock_uses("import threading\n"
                                    "def a(gpu):\n"
                                    "    with gpu._lock as lock:\n"
                                    "        pass\n"
                                    "def b():\n"
                                    "    lock = threading.Lock()\n"
                                    "    lock.acquire()\n"
                                    "class C:\n"
                                    "    def hold(self):\n"
                                    "        self._held = self.gpu._lock\n"
                                    "class D:\n"
                                    "    def go(self):\n"
                                    "        self._held = threading.Lock()\n"
                                    "        self._held.acquire()\n"), [])


class CompanionStaticTests(unittest.TestCase):
    """The names used on nvapi / nvml expressions exist on the companions."""

    def test_companion_uses_are_listed(self):
        surface = _companion_static_surface()
        measured = set()
        for use in fs.scan()[1]:
            key = (use.receiver, use.name)
            if key in fs.COMPANION_FRONTEND_OWNED or use.name.split(".")[0] in surface[use.receiver]:
                continue
            measured.add(key)
        self.assertEqual(sorted(measured ^ set(fs.COMPANION_GAPS)), [])

    def test_companion_evidence_holds(self):
        uses = {}
        for use in fs.scan()[1]:
            uses.setdefault((use.receiver, use.name), set()).add(use.file)
        for key, evidence in {**fs.COMPANION_GAPS, **fs.COMPANION_FRONTEND_OWNED}.items():
            with self.subTest(key=key):
                self.assertTrue(fs.evidence_files(evidence))
                self.assertLessEqual(fs.evidence_files(evidence), uses[key])


@unittest.skipUnless(NATIVE_BUILT, SKIP_REASON)
class ConstructedNativeTests(unittest.TestCase):
    """Checks on a native.Gpu built over the fake card of tests/test_native_identity.py."""

    @staticmethod
    def card():
        from tests.test_native_identity import TITAN, FakeNvapi, FakeNvml
        return FakeNvml([TITAN]), FakeNvapi([TITAN])

    def setUp(self):
        from tests.test_native_identity import install
        install(*self.card())
        self.addCleanup(native.set_export_resolver, None)
        self.gpu = native.Gpu("0000:01:00.0")
        self.addCleanup(self.gpu.close)
        self.assertTrue(self.gpu.available())

    def test_a_failing_set_up_still_resets_the_resolver(self):
        class Failing(ConstructedNativeTests):
            @staticmethod
            def card():
                return None, None  # no driver at all: the Gpu constructs but available() is False

        result = unittest.TestResult()
        Failing("test_front_end_owned_companion_attribute_round_trips").run(result)
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertIn("available", result.failures[0][1])
        self.assertIsNone(native._resolver_keepalive)

    def test_front_end_names_absent_on_the_instance_are_exactly_the_known_gaps(self):
        absent = {u.name for u in fs.scan()[0] if not hasattr(self.gpu, u.name)} - set(fs.FRONTEND_OWNED)
        expected = {n for n, checks in fs.measured_gaps().items() if "b" in checks}
        self.assertEqual(sorted(absent ^ expected), [])
        self.assertLessEqual(absent, set(fs.KNOWN_GAPS))

    def test_stored_names_round_trip(self):
        for name in sorted(fs.stored_names()):
            with self.subTest(name=name):
                if hasattr(self.gpu, name):
                    value = getattr(self.gpu, name)
                    setattr(self.gpu, name, value)
                    self.assertEqual(getattr(self.gpu, name), value)
                else:
                    self.assertTrue(name in fs.KNOWN_GAPS or name in fs.FRONTEND_OWNED, name)
                    marker = object()
                    setattr(self.gpu, name, marker)
                    self.assertIs(getattr(self.gpu, name), marker)
                    delattr(self.gpu, name)

    def test_reset_all_returns_nvbackend_reset_steps(self):
        steps = self.gpu.reset_all()
        self.assertTrue(steps)
        for step in steps:
            self.assertIs(type(step), nvbackend.ResetStep)
            self.assertIsInstance(step.name, str)
            ok, message = step
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(message, str)

    def test_companion_names_absent_on_the_halves_are_exactly_the_companion_gaps(self):
        absent = set()
        for use in fs.scan()[1]:
            key = (use.receiver, use.name)
            if key in fs.COMPANION_FRONTEND_OWNED:
                continue
            target = getattr(self.gpu, use.receiver)
            if use.receiver == "nvml" and use.name.startswith("dll."):
                # A driver export can be absent. Its presence probe must agree
                # with the loader, rather than requiring a fake to serve it.
                name = use.name.split(".", 1)[1]
                self.assertEqual(target.has(name), hasattr(target.dll, name))
                continue
            for part in use.name.split("."):
                if not hasattr(target, part):
                    absent.add(key)
                    break
                target = getattr(target, part)
        self.assertEqual(sorted(absent ^ set(fs.COMPANION_GAPS)), [])

    def test_front_end_owned_companion_attribute_round_trips(self):
        lock = threading.RLock()
        self.gpu.nvapi._up9512r_mutex = lock
        self.assertIs(self.gpu.nvapi._up9512r_mutex, lock)


class FrontendContractTests(unittest.TestCase):
    def test_c_companions_carry_every_name_and_the_interface_table(self):
        from druta import native_companions
        expected = {name: (identifier, args) for name, identifier, args in nvbackend.NVAPI_INTERFACES}
        self.assertEqual(native_companions._INTERFACES, expected)
        self.assertEqual(len(expected), len(nvbackend.NVAPI_INTERFACES))

    def test_f_private_reach_delegates_read_existing_companion_attributes(self):
        from druta import native_frontend
        surface = _companion_static_surface()
        for name, dependencies in native_frontend.DELEGATES.items():
            self.assertTrue(hasattr(native.Gpu, name), name)
            for dependency in dependencies:
                half, member = dependency.split(".", 1)
                self.assertIn(member, surface[half], (name, dependency))

    def test_g_missing_members_agrees_with_the_scan(self):
        from druta import native_frontend
        expected = ((fs.nvbackend_public_members() | {u.name for u in fs.scan()[0]})
                    - set(fs.EXEMPT) - set(fs.FRONTEND_OWNED) - fs.init_stores(fs.NATIVE, "Gpu"))
        self.assertEqual(native_frontend.REQUIRED_MEMBERS, expected)
        self.assertEqual(set(native_frontend.missing_members()), set(fs.measured_gaps()))
        with patch.object(native, "load", side_effect=AssertionError("surface check must not load")):
            self.assertEqual(native_frontend.missing_members(), ())


if __name__ == "__main__":
    unittest.main()
