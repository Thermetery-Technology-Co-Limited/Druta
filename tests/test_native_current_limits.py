# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native current-limit operations, increment 6a of the RM escape transport
(the layout choice, the read-only info probe, the four policy commands and the
validated policy-table read) against nvbackend.GPU._current_limit_abi,
_current_limit_probe_info, _current_limit_rm, _power_policy_table and
_current_limit_capacity.

Both sides answer from tests.test_current_limits.fixture: the Python twin calls
the fixture's escape through its mocked _legacy_clk_escape, the core reaches it
through a fake D3DKMTEscape that hands the packet (and the escape fields it
received) to the same function. The fixture's one-shot flags and its writes
are state, so every side gets a FRESH fixture: a case builds the Python side
and the native side separately, from the same arguments, and compares

- the outcome: the value, or the exception type, text and .fixed;
- the driver log: every escape with its size, its 17 header words and its
  parameter words as the driver saw them, the escape fields, the NTSTATUS
  returned and the RM status word written back;
- the fixture's writes, rejected info sizes and control block afterwards;
- the cache afterwards: the transport, the layout choice (Python keeps the
  layout dict, the core the candidate index into
  _current_limit_transport_layouts(); compared as that index), the policy
  mask and the last policy table.
"""
import ctypes
import unittest

from druta import native, nvbackend
from tests.test_current_limits import fixture
from tests.test_native_telemetry import u32_callback
from tests.test_native_transport import ESCAPE, KMT, TransportRig
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_power_layouts  # noqa: E402  (needs the core)

NV = nvbackend
GPU = NV.GPU
u32 = ctypes.c_uint32
INFO, STATUS, CONTROL, SET = 0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B
LEGACY_WIRE = {INFO: 0x20802618, STATUS: 0x20802619, CONTROL: 0x2080261A, SET: 0x2080261B}
FIELDS = dict(hAdapter=3, hDevice=0, Type=0, Flags=8, hContext=0)  # the fixture's transport fields
MATCHED_ADAPTER = 111  # TransportRig's adapter at the card's PCI location
RM_SIZE = 0x1F  # RM_CURRENT_POLICY_SIZE_REJECTED
VOLTA = 5


class Driver:
    """The fixture's escape behind a log. fail: NTSTATUS returned (packet untouched) for the
    escapes whose 0-based index is in fail_at; refuse(packet) -> an RM code written to word 16
    instead of answering, or None to answer."""

    def __init__(self, inner):
        self.inner = inner
        self.log = []
        self.fail_at, self.fail = set(), -1
        self.refuse = None

    def __call__(self, packet, fields):
        index = len(self.log)
        entry = [ctypes.sizeof(packet), tuple(packet[:17]), tuple(packet[17:]), dict(fields)]
        self.log.append(entry)
        if index in self.fail_at:
            entry += [self.fail, packet[16]]
            return self.fail
        rm = self.refuse(packet) if self.refuse else None
        if rm is not None:
            packet[16] = rm
            status = 0
        else:
            status = self.inner(packet, fields)
        entry += [status, packet[16]]
        return status


class CLRig(TransportRig):
    """TransportRig whose fake D3DKMTEscape is a Driver: the core's escapes reach the fixture."""

    def __init__(self, arch, driver):
        self.driver = driver
        super().__init__()
        self.errors = []
        if arch is None:  # NVML cannot report an architecture
            self.nvml.exports["nvmlDeviceGetArchitecture"] = u32_callback(0, status=3)
        else:
            self.nvml.exports["nvmlDeviceGetArchitecture"] = u32_callback(arch)

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            try:
                e = GPU._Escape.from_address(pointer)
                words = (u32 * (e.PrivateDriverDataSize // 4)).from_address(e.pPrivateDriverData)
                packet = (u32 * len(words))(*words)
                fields = {name: getattr(e, name) for name in ("hAdapter", "hDevice", "Type", "Flags", "hContext")}
                # The real _legacy_clk_escape swaps in its own PCI-matched adapter handle (111, the
                # card on bus 1; test_native_transport compares that); the twin's escape is mocked
                # below that point and sees the transport's. Map exactly that swap back.
                if fields["hAdapter"] == MATCHED_ADAPTER:
                    fields["hAdapter"] = FIELDS["hAdapter"]
                status = self.driver(packet, fields)
                words[:] = packet[:]
                return status
            except BaseException as exc:  # the fixture's own asserts: surfaced through rig.errors
                self.errors.append(repr(exc))
                return -1
        table[ESCAPE] = escape
        return table


class Side:
    """One side of a case on a fresh fixture: the Python twin or the core."""

    def __init__(self, kind, arch=10, **kw):
        self.kind = kind
        twin, self.state = fixture(arch, **kw)
        self.header = list(twin._capture_current_limit_transport.return_value[0])
        self.driver = Driver(twin._legacy_clk_escape.side_effect)
        self.rig = None
        if kind == "python":
            twin._legacy_clk_escape.side_effect = self.driver
            self.gpu = twin
        else:
            self.rig = CLRig(twin.arch(), self.driver)
            self.gpu = self.rig.native_gpu()
            self.gpu.capabilities()
        self.gpu._current_limit_transport = (list(self.header), dict(FIELDS))

    def candidates(self):
        key = GPU._current_limit_key(self.header)
        layout = GPU._CURRENT_LIMIT_LAYOUTS.get(key)
        return () if layout is None else (layout, *GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS.get(key, ()))

    def set_choice(self, index):
        """Store candidate `index` as the layout choice, in each side's own representation."""
        self.gpu._current_limit_layout_choice = (None if index is None else
                                                 self.candidates()[index] if self.kind == "python" else index)

    def choice(self):
        value = getattr(self.gpu, "_current_limit_layout_choice", None)
        if self.kind == "native" or value is None:
            return value
        hits = [k for k, layout in enumerate(self.candidates()) if layout is value]
        return hits[0] if hits else ("not a candidate", value)

    def cache(self):
        transport = getattr(self.gpu, "_current_limit_transport", None)
        if transport is not None:
            transport = (list(transport[0]), dict(transport[1]))
        return {"transport": transport, "choice": self.choice(),
                "mask": getattr(self.gpu, "_power_policy_mask", 0),
                "table": getattr(self.gpu, "_last_policy_table", None)}

    def fixture_state(self):
        return {"writes": self.state.writes, "rejected": self.state.rejected_info_bytes,
                "control": self.state.control}

    def close(self):
        if self.kind == "native":
            self.gpu.close()


def outcome(fn):
    try:
        return ("ok", fn())
    except (ValueError, TypeError) as exc:
        return (type(exc).__name__, str(exc), getattr(exc, "fixed", False))


class CurrentLimitCase(ParityCase):
    def side(self, kind, setup=None, **kw):
        side = Side(kind, **kw)
        self.addCleanup(side.close)
        if setup is not None:
            setup(side)
        side.driver.log.clear()
        return side

    def run_side(self, side, action):
        result = outcome(lambda: action(side.gpu))
        if side.rig is not None:
            self.assertEqual(side.rig.errors, [])
        return result, side.driver.log, side.fixture_state(), side.cache()

    def both(self, action, setup=None, **kw):
        """action(gpu) on a fresh Python twin and a fresh core, both after setup(side); the runs
        must be identical. Returns the Python run."""
        runs = [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]
        for name, python, core in zip(("outcome", "driver log", "fixture state", "cache"), *runs):
            self.assertEqual(python, core, f"Python and native {name} differ")
        return runs[0]

    def sides(self, action, setup=None, **kw):
        """action on each side without requiring equal results (documented divergences)."""
        return [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]

    # ---- expectations -------------------------------------------------------------------------

    def assertGets(self, log, header, expected):
        """log entries are GETs of (wire command, parameter bytes, status, rm) with the transport's
        header words carried over and word 16 cleared on entry."""
        self.assertEqual([(entry[1][14], entry[1][15], entry[4], entry[5]) for entry in log], expected)
        for size, words, params, fields, *_ in log:
            self.assertEqual(size, 68 + words[15])
            self.assertEqual(words[2], size)
            self.assertEqual(words[16], 0)
            self.assertEqual(list(words[:2]) + list(words[3:14]), header[:2] + header[3:14])
            self.assertEqual(words[12:14], (1234, 5678))
            self.assertEqual(fields, FIELDS)


def setup_choice(index):
    return lambda side: side.set_choice(index)


def no_transport(side):
    side.gpu._current_limit_transport = None


def setup_table(side):
    side.gpu._power_policy_table()


def chain(*steps):
    def run(side):
        for step in steps:
            step(side)
    return run


def refuse_info(code_for):
    """refuse every info GET whose parameter size code_for maps to an RM code."""
    def setup(side):
        side.driver.refuse = lambda p: code_for(p[15]) if p[14] == INFO else None
    return setup


def control_set(gpu, policy, limit=None):
    """A SET buffer from gpu's last validated table selecting `policy`."""
    layout, _, control, _ = gpu._last_policy_table
    params = list(control)
    params[layout.get("control_mask_word", 4)] = 1 << policy
    if limit is not None:
        base, stride = layout["control"]
        params[(base + policy * stride) // 4 + 1] = limit
    return params


NEWER = dict(arch=10, newer=True)
OLDER = dict(arch=GPU.ARCH_PASCAL)
SHORT = dict(arch=GPU.ARCH_PASCAL, short_status=True)
AMPERE = dict(ampere_layout=True)
LEGACY = dict(arch=GPU.ARCH_TURING, legacy=True)
TURING = dict(arch=GPU.ARCH_TURING, newer=True)


# ---- the layout choice ---------------------------------------------------------------------------

class LayoutChoiceTests(CurrentLimitCase):
    def test_newer_first_candidate_accepted_and_cached(self):
        (result, log, state, cache) = self.both(lambda g: g._current_limit_abi(), **NEWER)
        self.assertEqual(result, ("ok", GPU._CURRENT_LIMIT_LAYOUTS[(0x2080A612, 54420, 54352)]))
        self.assertEqual(cache["choice"], 0)
        side = Side("python", **NEWER)
        self.assertGets(log, side.header, [(INFO, 20000, 0, 0)])
        self.assertEqual(log[0][2], (0,) * 5000)  # the probe's parameters are all zero
        self.assertEqual(state["writes"], [])
        # a second call reuses the choice and issues no escape
        result2, log2, _, cache2 = self.both(lambda g: g._current_limit_abi(),
                                             setup=lambda s: s.gpu._current_limit_abi(), **NEWER)
        self.assertEqual(result2, result)
        self.assertEqual(log2, [])
        self.assertEqual(cache2["choice"], 0)

    def test_ampere_20000_refused_8632_accepted(self):
        result, log, state, cache = self.both(lambda g: g._current_limit_abi(), **AMPERE)
        self.assertEqual(result, ("ok", GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS[(0x2080A612, 54420, 54352)][0]))
        self.assertEqual(cache["choice"], 1)
        self.assertGets(log, Side("python", **AMPERE).header, [(INFO, 20000, 0, RM_SIZE), (INFO, 8632, 0, 0)])
        self.assertEqual(state["rejected"], [20000])

    def test_all_candidates_refused_is_unsupported(self):
        result, log, _, cache = self.both(lambda g: g._current_limit_abi(),
                                          setup=refuse_info(lambda size: RM_SIZE), **NEWER)
        self.assertEqual(result, ("CurrentLimitUnsupported",
                                  "no known policy-info layout fits this driver ("
                                  "20000-byte info: policy info layout rejected (RM 0x1F); "
                                  "8632-byte info: policy info layout rejected (RM 0x1F); "
                                  "20512-byte info: policy info layout rejected (RM 0x1F))", False))
        self.assertEqual([entry[1][15] for entry in log], [20000, 8632, 20512])
        self.assertIsNone(cache["choice"])
        self.assertIsNotNone(cache["transport"])

    def test_mixed_refusal_is_a_plain_failure_with_every_candidate(self):
        result, log, _, cache = self.both(lambda g: g._current_limit_abi(),
                                          setup=refuse_info(lambda size: 0x57 if size == 8632 else RM_SIZE), **NEWER)
        self.assertEqual(result, ("ValueError",
                                  "20000-byte info: policy info layout rejected (RM 0x1F); "
                                  "8632-byte info: policy info layout rejected (RM 0x57); "
                                  "20512-byte info: policy info layout rejected (RM 0x1F)", False))
        self.assertEqual(len(log), 3)
        self.assertIsNone(cache["choice"])
        self.assertIsNotNone(cache["transport"])

    def test_escape_failure_during_the_choice_drops_the_transport(self):
        def setup(side):
            side.driver.fail_at = {1}
        result, log, _, cache = self.both(lambda g: g._current_limit_abi(), setup=setup, **AMPERE)
        self.assertEqual(result, ("ValueError", "policy request failed (NTSTATUS -1, RM 0x0)", False))
        self.assertEqual([(e[1][15], e[4]) for e in log], [(20000, 0), (8632, -1)])
        self.assertEqual((cache["transport"], cache["choice"]), (None, None))

    def test_single_candidate_chosen_without_an_escape(self):
        for name, kw, key in (("older", OLDER, (0x2080A612, 11748, 11680)),
                              ("short_status", SHORT, (0x2080A612, 11740, 11672)),
                              ("legacy", LEGACY, (0x20802612, 3548, 3480))):
            with self.subTest(name):
                result, log, _, cache = self.both(lambda g: g._current_limit_abi(), **kw)
                self.assertEqual(result, ("ok", GPU._CURRENT_LIMIT_LAYOUTS[key]))
                self.assertEqual(log, [])
                self.assertEqual(cache["choice"], 0)

    def test_assigned_choice_reused_while_a_transport_is_present(self):
        result, log, _, cache = self.both(lambda g: g._current_limit_abi(), setup=setup_choice(1), **NEWER)
        self.assertEqual(result, ("ok", GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS[(0x2080A612, 54420, 54352)][0]))
        self.assertEqual((log, cache["choice"]), ([], 1))

    def test_assigned_choice_dropped_without_a_transport(self):
        # DIVERGENCE (accepted, the fixture's capture): with the choice assigned but no transport
        # both sides drop the choice and capture again. The Python fixture's mocked capture always
        # succeeds, so it probes and re-chooses; the core has no PowerPolInfo getter to capture with.
        def setup(side):
            side.gpu._current_limit_transport = None
            side.set_choice(1)
        (py, py_log, _, py_cache), (core, core_log, _, core_cache) = self.sides(
            lambda g: g._current_limit_abi(), setup=setup, **NEWER)
        self.assertEqual(py, ("ok", GPU._CURRENT_LIMIT_LAYOUTS[(0x2080A612, 54420, 54352)]))
        self.assertEqual((len(py_log), py_cache["choice"]), (1, 0))
        self.assertEqual(core, ("ValueError", "unsupported current-policy transport: "
                                              "no recognized power GET captured", False))
        self.assertEqual((core_log, core_cache["transport"], core_cache["choice"]), ([], None, None))

    def test_legacy_family_uses_its_26xx_wire_commands(self):
        def action(g):
            table = g._power_policy_table()
            return table[0]
        result, log, _, _ = self.both(action, **LEGACY)
        self.assertEqual(result, ("ok", GPU._CURRENT_LIMIT_LAYOUTS[(0x20802612, 3548, 3480)]))
        self.assertGets(log, Side("python", **LEGACY).header,
                        [(0x20802618, 5520, 0, 0), (0x2080261A, 2124, 0, 0), (0x20802619, 69148, 0, 0)])


# ---- the read-only info probe --------------------------------------------------------------------

NEWER_LAYOUT = GPU._CURRENT_LIMIT_LAYOUTS[(0x2080A612, 54420, 54352)]


class ProbeInfoTests(CurrentLimitCase):
    def test_accepted_info(self):
        result, log, _, cache = self.both(lambda g: g._current_limit_probe_info(NEWER_LAYOUT), **NEWER)
        self.assertEqual(result[0], "ok")
        self.assertEqual(len(result[1]), 5000)
        self.assertEqual(result[1][1], 0x3FFFF)
        self.assertEqual(len(log), 1)
        self.assertIsNone(cache["choice"])  # a probe alone chooses nothing

    def test_size_rejection_is_fixed(self):
        result, *_, cache = self.both(lambda g: g._current_limit_probe_info(NEWER_LAYOUT),
                                      setup=refuse_info(lambda size: RM_SIZE), **NEWER)
        self.assertEqual(result, ("ValueError", "policy info layout rejected (RM 0x1F)", True))
        self.assertIsNotNone(cache["transport"])

    def test_other_rm_is_not_fixed(self):
        result, *_ = self.both(lambda g: g._current_limit_probe_info(NEWER_LAYOUT),
                               setup=refuse_info(lambda size: 0x57), **NEWER)
        self.assertEqual(result, ("ValueError", "policy info layout rejected (RM 0x57)", False))

    def test_mask_outside_the_capacity_is_fixed(self):
        # Every measured layout has capacity 32, so a synthesized one with a 4-record info block
        # (same info size, so the fixture still answers) is the only way to exercise this check.
        narrow = dict(NEWER_LAYOUT, info=(0xCC, 0x1000))
        self.assertEqual(GPU._current_limit_capacity(narrow), 4)
        result, log, *_ = self.both(lambda g: g._current_limit_probe_info(narrow), **NEWER)
        self.assertEqual(result, ("ValueError", "current-policy mask does not fit this info layout", True))
        self.assertEqual(len(log), 1)

    def test_escape_failure_drops_transport_and_choice(self):
        def setup(side):
            side.set_choice(0)
            side.driver.fail_at = {0}
        result, log, _, cache = self.both(lambda g: g._current_limit_probe_info(NEWER_LAYOUT), setup=setup, **NEWER)
        self.assertEqual(result, ("ValueError", "policy request failed (NTSTATUS -1, RM 0x0)", False))
        self.assertEqual(len(log), 1)
        self.assertEqual((cache["transport"], cache["choice"]), (None, None))

    def test_no_transport(self):
        # DIVERGENCE (accepted): the Python twin unpacks None (TypeError); the core refuses.
        (py, *_), (core, *_) = self.sides(lambda g: g._current_limit_probe_info(NEWER_LAYOUT),
                                          setup=no_transport, **NEWER)
        self.assertEqual(py[0], "TypeError")
        self.assertEqual(core, ("ValueError", "the power transport is not captured", False))


# ---- the four policy commands --------------------------------------------------------------------

class RmRefusalTests(CurrentLimitCase):
    """Every refusal, in the Python twin's order, with what reached the driver."""

    def refused(self, action, message, *, kw=NEWER, setup=None, escapes=0):
        result, log, state, cache = self.both(action, setup=setup, **kw)
        self.assertEqual(result, ("ValueError", message, False))
        self.assertEqual(len(log), escapes)
        self.assertEqual(state["writes"], [])
        return cache

    def test_unsupported_generation(self):
        for arch in (VOLTA, None):
            with self.subTest(arch=arch):
                self.refused(lambda g: g._current_limit_rm(INFO),
                             "current limits are not validated for this GPU generation", kw=dict(arch=arch))
                self.refused(lambda g: g._current_limit_rm(SET, [0] * 3460),
                             "current limits are not validated for this GPU generation", kw=dict(arch=arch))

    def test_profile_supported(self):
        for name, kw in (("volta", dict(arch=VOLTA)), ("none", dict(arch=None)), ("newer", NEWER),
                         ("turing", TURING), ("pascal", OLDER), ("ampere", AMPERE)):
            with self.subTest(name):
                result, *_ = self.both(lambda g: g._current_limit_profile_supported(), **kw)
                self.assertEqual(result, ("ok", name not in ("volta", "none")))

    def test_bad_command(self):
        for command in (0x2080A617, 0x2080A612, 0x20802618, 0x2080261B, 0):
            with self.subTest(command=hex(command)):
                self.refused(lambda g: g._current_limit_rm(command), "unvalidated current-policy command")

    def test_set_without_params(self):
        self.refused(lambda g: g._current_limit_rm(SET), "current-policy writes require a getter buffer")

    def test_set_of_unknown_size_before_any_capture(self):
        cache = self.refused(lambda g: g._current_limit_rm(SET, [0, 0, 0, 255, 1 << 13] + [0] * 95),
                             "unexpected current-policy buffer size", setup=lambda s: setattr(
                                 s.gpu, "_current_limit_transport", None))
        self.assertIsNone(cache["transport"])  # refused before any capture

    def test_set_not_selecting_a_generation_rail(self):
        for mask in (1 << 5, 0, (1 << 13) | (1 << 14), 1 << 31):
            with self.subTest(mask=hex(mask)):
                params = [0, 0, 0, 255, mask] + [0] * 3455
                self.refused(lambda g: g._current_limit_rm(SET, params),
                             "current-policy write must select one generation-supported rail")

    def test_set_any_live_policy_not_in_the_table(self):
        params = [0, 0, 0, 255, 1 << 13] + [0] * 3455
        # no table read yet: the live mask is 0
        self.refused(lambda g: g._current_limit_rm(SET, params, any_live_policy=True),
                     "current-policy write must select one policy the validated table lists")
        bad = list(params)
        bad[4] = 1 << 31  # not live on the fixture's Blackwell table
        self.refused(lambda g: g._current_limit_rm(SET, bad, any_live_policy=True),
                     "current-policy write must select one policy the validated table lists",
                     setup=setup_table)

    def test_live_non_generation_policy_on_turing(self):
        # Policy 14 is live in the Turing table (0xEBBF) but is not its generation policy (13).
        def p14(g):
            return control_set(g, 14)
        self.refused(lambda g: g._current_limit_rm(SET, p14(g)),
                     "current-policy write must select one generation-supported rail", kw=TURING, setup=setup_table)
        result, log, state, cache = self.both(lambda g: g._current_limit_rm(SET, p14(g), any_live_policy=True),
                                              setup=setup_table, **TURING)
        self.assertEqual(result[0], "ok")
        self.assertEqual([w[4] for w in state["writes"]], [1 << 14])
        self.assertEqual([e[1][14] for e in log], [SET])
        self.assertEqual(cache["mask"], 0xEBBF)
        # clearing the live mask withdraws it again
        self.refused(lambda g: g._current_limit_rm(SET, p14(g), any_live_policy=True),
                     "current-policy write must select one policy the validated table lists", kw=TURING,
                     setup=chain(setup_table, lambda s: setattr(s.gpu, "_power_policy_mask", 0)))

    def test_wrong_buffer_size_before_a_choice(self):
        for name, command, params in (("info short", INFO, [0] * 10), ("status", STATUS, [0] * 3460),
                                      ("control older size", CONTROL, [0] * (4432 // 4))):
            with self.subTest(name):
                cache = self.refused(lambda g: g._current_limit_rm(command, params),
                                     "unexpected current-policy buffer size")
                self.assertIsNone(cache["choice"])

    def test_wrong_buffer_size_after_a_choice(self):
        for name, command, params in (("info short", INFO, [0] * 10),
                                      ("info of the fallback", INFO, [0] * (8632 // 4)),
                                      ("set of the older size", SET, [0, 0, 0, 255, 1 << 13] + [0] * 1103)):
            with self.subTest(name):
                self.refused(lambda g: g._current_limit_rm(command, params),
                             "unexpected current-policy buffer size", setup=setup_choice(0))

    def test_ampere_buffer_of_the_first_candidate_probes_then_refuses(self):
        # 20000 bytes fits a candidate, so the choice is made (the 20000 probe refused, 8632
        # accepted) and the buffer is then the wrong size for it.
        result, log, state, cache = self.both(lambda g: g._current_limit_rm(INFO, [0] * 5000), **AMPERE)
        self.assertEqual(result, ("ValueError", "unexpected current-policy buffer size", False))
        self.assertEqual([(e[1][15], e[5]) for e in log], [(20000, RM_SIZE), (8632, 0)])
        self.assertEqual(state["rejected"], [20000])
        self.assertEqual(cache["choice"], 1)

    def test_status_and_control_require_a_mask(self):
        for command, message in ((STATUS, "current-policy status requires a validated mask"),
                                 (CONTROL, "current-policy control requires a validated mask")):
            for mask in (None, -1, 1 << 32, True, "1", 1.0):
                with self.subTest(command=hex(command), mask=mask):
                    self.refused(lambda g: g._current_limit_rm(command, policy_mask=mask), message,
                                 setup=setup_choice(0))

    def test_set_mask_word_must_select_on_the_chosen_layout(self):
        # 2108 bytes is the short RM26 CONTROL, whose selector is word 0: it passes the
        # pre-capture selector check with word 0, then the chosen (newer) layout's word 4 refuses it.
        params = [1 << 13] + [0] * 526
        self.refused(lambda g: g._current_limit_rm(SET, params),
                     "current-policy write mask does not select one validated policy", setup=setup_choice(0))

    def test_set_of_another_layout_size_after_the_mask_check(self):
        params = [0, 0, 0, 255, 1 << 13] + [0] * (4432 // 4 - 5)
        self.refused(lambda g: g._current_limit_rm(SET, params), "unexpected current-policy buffer size",
                     setup=setup_choice(0))

    # "legacy current-policy SET has not been identified" needs a layout with a "commands" table
    # that lacks 0x2080E61B; every measured legacy layout maps all four commands, so it is not
    # reachable through the fixture or the table and is not covered here.


class RmPacketTests(CurrentLimitCase):
    def get(self, kw, command, mask=None):
        result, log, state, cache = self.both(lambda g: g._current_limit_rm(command, policy_mask=mask),
                                              setup=setup_choice(0), **kw)
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(len(log), 1)
        self.assertEqual(state["writes"], [])
        return result[1], log[0], cache

    def test_gets_as_the_driver_saw_them(self):
        for name, kw, wire, sizes, status_word in (
                ("newer", NEWER, {}, (20000, 396024, 13840), 1),
                ("older", OLDER, {}, (8620, 172080, 4432), 1),
                ("short_status", SHORT, {}, (8620, 171048, 4432), 1),
                ("legacy", LEGACY, LEGACY_WIRE, (5520, 69148, 2124), 0)):
            with self.subTest(name):
                header = Side("python", **kw).header
                live = Side("python", **kw).state.info[1]
                info, entry, cache = self.get(kw, INFO)
                self.assertGets([entry], header, [(wire.get(INFO, INFO), sizes[0], 0, 0)])
                self.assertEqual(entry[2], (0,) * (sizes[0] // 4))
                self.assertEqual(len(info), sizes[0] // 4)
                self.assertEqual(info[1], live)
                self.assertIsNotNone(cache["transport"])

                status, entry, _ = self.get(kw, STATUS, live)
                self.assertGets([entry], header, [(wire.get(STATUS, STATUS), sizes[1], 0, 0)])
                expected = [0] * (sizes[1] // 4)
                expected[status_word] = live
                self.assertEqual(entry[2], tuple(expected))
                self.assertEqual(status[status_word], live)

                control, entry, _ = self.get(kw, CONTROL, live)
                self.assertGets([entry], header, [(wire.get(CONTROL, CONTROL), sizes[2], 0, 0)])
                expected = [0] * (sizes[2] // 4)
                expected[4] = live
                self.assertEqual(entry[2], tuple(expected))
                self.assertEqual(control[:5], [0, 0, 0, 255, live])

    def test_info_with_explicit_zero_params(self):
        result, log, *_ = self.both(lambda g: g._current_limit_rm(INFO, [0] * 5000), **NEWER)
        self.assertEqual(result[0], "ok")
        self.assertEqual([e[1][15] for e in log], [20000, 20000])  # the choice probe, then the GET

    def test_set_as_the_driver_saw_it(self):
        for name, kw, wire, policy in (("newer", NEWER, SET, 13), ("newer policy 14", NEWER, SET, 14),
                                       ("older", OLDER, SET, 13), ("ampere", AMPERE, SET, 13),
                                       ("legacy", LEGACY, 0x2080261B, 13)):
            with self.subTest(name):
                sent = []

                def action(g):
                    params = control_set(g, policy, 299000)
                    sent.append(params)
                    return g._current_limit_rm(SET, params)
                result, log, state, cache = self.both(action, setup=setup_table, **kw)
                self.assertEqual(sent[0], sent[1])
                self.assertEqual(result, ("ok", sent[0]))  # the driver echoes nothing back into a SET
                self.assertEqual([(e[1][14], e[1][15], e[4], e[5]) for e in log], [(wire, len(sent[0]) * 4, 0, 0)])
                self.assertEqual(log[0][2], tuple(sent[0]))
                self.assertEqual(state["writes"], [sent[0]])
                self.assertIsNotNone(cache["transport"])

    def test_status_after_a_set_reflects_it(self):
        def action(g):
            g._current_limit_rm(SET, control_set(g, 13, 299000))
            return g._current_limit_rm(STATUS, policy_mask=0x3FFFF)
        result, log, *_ = self.both(action, setup=setup_table, **NEWER)
        self.assertEqual(result[1][(0x9C + 13 * 0x1720) // 4 + 1], 299000)
        self.assertEqual([e[1][14] for e in log], [SET, STATUS])

    def test_refused_set_keeps_the_transport(self):
        def setup(side):
            setup_table(side)
            side.state.refused = True

        def action(g):
            return g._current_limit_rm(SET, control_set(g, 13, 299000))
        result, log, state, cache = self.both(action, setup=setup, **NEWER)
        self.assertEqual(result, ("ValueError", "policy request failed (NTSTATUS 0, RM 0x1F)", False))
        self.assertEqual(len(state["writes"]), 1)
        self.assertEqual((cache["transport"] is not None, cache["choice"]), (True, 0))

    def test_get_refused_for_its_size_is_unsupported(self):
        for command, mask, size in ((INFO, None, 20000), (STATUS, 0x3FFFF, 396024), (CONTROL, 0x3FFFF, 13840)):
            with self.subTest(command=hex(command)):
                def setup(side):
                    side.set_choice(0)
                    side.driver.refuse = lambda p: RM_SIZE
                result, log, _, cache = self.both(lambda g: g._current_limit_rm(command, policy_mask=mask),
                                                  setup=setup, **NEWER)
                self.assertEqual(result, ("CurrentLimitUnsupported",
                                          f"policy GET 0x{command:08X} size {size} rejected (RM 0x1F)", False))
                self.assertEqual(len(log), 1)
                self.assertEqual((cache["transport"] is not None, cache["choice"]), (True, 0))

    def test_get_refused_otherwise_is_a_failed_read(self):
        def setup(side):
            side.set_choice(0)
            side.driver.refuse = lambda p: 0x57
        result, _, _, cache = self.both(lambda g: g._current_limit_rm(INFO), setup=setup, **NEWER)
        self.assertEqual(result, ("ValueError", "policy request failed (NTSTATUS 0, RM 0x57)", False))
        self.assertEqual((cache["transport"] is not None, cache["choice"]), (True, 0))

    def test_escape_failure_drops_transport_and_choice(self):
        for name, command, mask in (("info", INFO, None), ("status", STATUS, 0x3FFFF)):
            with self.subTest(name):
                def setup(side):
                    side.set_choice(0)
                    side.driver.fail_at = {0}
                result, log, _, cache = self.both(lambda g: g._current_limit_rm(command, policy_mask=mask),
                                                  setup=setup, **NEWER)
                self.assertEqual(result, ("ValueError", "policy request failed (NTSTATUS -1, RM 0x0)", False))
                self.assertEqual(len(log), 1)
                self.assertEqual((cache["transport"], cache["choice"]), (None, None))

    def test_set_escape_failure_drops_transport_and_choice(self):
        def setup(side):
            setup_table(side)
            side.driver.fail_at = {0}

        def action(g):
            return g._current_limit_rm(SET, control_set(g, 13, 299000))
        result, log, state, cache = self.both(action, setup=setup, **NEWER)
        self.assertEqual(result, ("ValueError", "policy request failed (NTSTATUS -1, RM 0x0)", False))
        self.assertEqual(state["writes"], [])  # failed before the fixture saw it
        self.assertEqual((cache["transport"], cache["choice"]), (None, None))

    def test_parameter_words_outside_u32(self):
        # A SET buffer word that is not a 32-bit unsigned int. The Python twin packs params into a
        # c_uint32 array when it builds the packet: a float or a str is refused there with
        # TypeError, on both sides, and nothing is sent. RECORDED DIVERGENCE for an out-of-range
        # int: the c_uint32 array silently wraps -1 and 1 << 32 and the twin SENDS the SET; the
        # binding refuses the word before any escape, so no word the caller did not mean reaches
        # the driver. Word 100 is opaque control data, outside every selector, so both pass the
        # selector checks.
        for value, python_sent, python_error, core_sent, core_error in (
                (-1, 0xFFFFFFFF, None, None, ("ValueError", "current-policy parameters are 32-bit words")),
                (1 << 32, 0, None, None, ("ValueError", "current-policy parameters are 32-bit words")),
                (1.0, None, ("TypeError", "'float' object cannot be interpreted as an integer"), None,
                 ("TypeError", "'float' object cannot be interpreted as an integer")),
                ("7", None, ("TypeError", "'str' object cannot be interpreted as an integer"), None,
                 ("TypeError", "'str' object cannot be interpreted as an integer"))):
            with self.subTest(value=value):
                def action(g):
                    params = control_set(g, 13)
                    params[100] = value
                    return g._current_limit_rm(SET, params)
                runs = self.sides(action, setup=setup_table, **NEWER)
                for (result, _, state, _), sent, error in zip(runs, (python_sent, core_sent),
                                                              (python_error, core_error)):
                    if error is None:
                        self.assertEqual(result[0], "ok")
                        self.assertEqual([w[100] for w in state["writes"]], [sent])
                    else:
                        self.assertEqual(result[:2], error)
                        self.assertEqual(state["writes"], [])

    def test_no_transport(self):
        # DIVERGENCE (accepted, the fixture's capture): the Python twin's mocked capture supplies
        # the transport again; the core has nothing to capture with and refuses.
        (py, py_log, _, py_cache), (core, core_log, _, core_cache) = self.sides(
            lambda g: g._current_limit_rm(INFO), setup=no_transport, **NEWER)
        self.assertEqual(py[0], "ok")
        self.assertIsNotNone(py_cache["transport"])
        self.assertEqual(core, ("ValueError", "unsupported current-policy transport: "
                                              "no recognized power GET captured", False))
        self.assertEqual((core_log, core_cache["transport"]), ([], None))


# ---- the validated policy table ------------------------------------------------------------------

class PolicyTableTests(CurrentLimitCase):
    def test_table_on_every_layout(self):
        for name, kw, wires in (
                ("newer", NEWER, [INFO, INFO, CONTROL, STATUS]),  # the choice probe first
                ("ampere", AMPERE, [INFO, INFO, INFO, CONTROL, STATUS]),
                ("older", OLDER, [INFO, CONTROL, STATUS]),
                ("short_status", SHORT, [INFO, CONTROL, STATUS]),
                ("legacy", LEGACY, [0x20802618, 0x2080261A, 0x20802619])):
            with self.subTest(name):
                result, log, _, cache = self.both(lambda g: g._power_policy_table(), **kw)
                self.assertEqual(result[0], "ok")
                layout, info, control, status = result[1]
                self.assertEqual(cache["table"], result[1])
                self.assertEqual(cache["mask"], info[1])
                self.assertEqual(cache["choice"], 1 if name == "ampere" else 0)
                self.assertEqual([e[1][14] for e in log], wires)
                self.assertEqual(control[layout.get("control_mask_word", 4)], info[1])
                self.assertEqual(status[layout.get("status_mask_word", 1)], info[1])

    def test_last_table_and_mask_before_any_read(self):
        for attribute, expected in (("_last_policy_table", None), ("_power_policy_mask", 0)):
            with self.subTest(attribute):
                result, *_ = self.both(lambda g: getattr(g, attribute, expected), **NEWER)
                self.assertEqual(result, ("ok", expected))

    def test_empty_mask(self):
        def setup(side):
            side.state.info[1] = 0
            side.gpu._power_policy_mask = 5  # replaced by the read
        result, log, _, cache = self.both(lambda g: g._power_policy_table(), setup=setup, **NEWER)
        layout, info, control, status = result[1]
        self.assertEqual((control, status), (None, None))
        self.assertEqual(info[1], 0)
        self.assertEqual(cache["mask"], 0)
        self.assertEqual(cache["table"], (layout, info, None, None))
        self.assertEqual([e[1][14] for e in log], [INFO, INFO])  # the probe and the table's GET

    def test_mismatched_status_or_control(self):
        def status_word(side):
            side.state.dynamic[1] = 0x1FFFF

        def control_header(side):
            side.state.control[3] = 254

        def legacy_status_word(side):
            side.state.dynamic[0] = 0x1

        for name, kw, setup in (("status mask word", NEWER, status_word),
                                ("control header", NEWER, control_header),
                                ("legacy status mask word", LEGACY, legacy_status_word)):
            with self.subTest(name):
                result, log, _, cache = self.both(lambda g: g._power_policy_table(), setup=setup, **kw)
                self.assertEqual(result, ("CurrentLimitUnsupported",
                                          "current-policy layout or mask differs from the measured ABI", False))
                self.assertEqual(len(log), 3 if name.startswith("legacy") else 4)
                self.assertIsNone(cache["table"])
                self.assertEqual(cache["mask"], 0)

    def test_mismatch_keeps_the_previous_table(self):
        def setup(side):
            setup_table(side)
            side.state.dynamic[1] = 0x1FFFF
        result, _, _, cache = self.both(lambda g: g._power_policy_table(), setup=setup, **NEWER)
        self.assertEqual(result[0], "CurrentLimitUnsupported")
        self.assertEqual(cache["mask"], 0x3FFFF)
        self.assertEqual(cache["table"][3][1], 0x3FFFF)  # the earlier read, not the mismatched one

    # Not reachable through the fixture, so not covered:
    # - "current-policy info size differs from the measured ABI": _current_limit_rm always returns
    #   exactly the layout's info size, on both sides.
    # - "current-policy mask exceeds the validated ABI capacity": every measured layout has capacity
    #   32, so no 32-bit mask exceeds it, and a synthesized layout cannot be made the core's choice
    #   (it stores a candidate index).

    def test_last_table_reset_native(self):
        # native-only: assigning None resets the core's table; anything else is refused.
        side = self.side("native", **NEWER)
        side.gpu._power_policy_table()
        self.assertIsNotNone(side.gpu._last_policy_table)
        with self.assertRaisesRegex(ValueError, "only None can be assigned"):
            side.gpu._last_policy_table = side.gpu._last_policy_table
        self.assertIsNotNone(side.gpu._last_policy_table)
        side.gpu._last_policy_table = None
        self.assertIsNone(side.gpu._last_policy_table)
        self.assertEqual(side.rig.errors, [])


# ---- capacity ------------------------------------------------------------------------------------

class CapacityTests(unittest.TestCase):
    def test_every_measured_layout(self):
        keys = list(GPU._CURRENT_LIMIT_LAYOUTS)
        self.assertTrue(keys)
        for key in keys:
            layouts = native_power_layouts.transport_layouts(*key)
            python = (GPU._CURRENT_LIMIT_LAYOUTS[key], *GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS.get(key, ()))
            self.assertEqual(layouts, python)
            for index, layout in enumerate(layouts):
                with self.subTest(key=tuple(map(hex, key)), candidate=index):
                    self.assertEqual(native.Gpu._current_limit_capacity(layout),
                                     GPU._current_limit_capacity(python[index]))

    def test_synthesized_layouts(self):
        base = GPU._CURRENT_LIMIT_LAYOUTS[(0x2080A612, 54420, 54352)]
        for name, change in (("narrow info", dict(info=(0xCC, 0x1000))),
                             ("narrow status", dict(status=(0x9C, 396024 // 3))),
                             ("narrow control", dict(control=(0x14, 13840 // 5))),
                             ("base at the end", dict(info=(20000, 4)))):
            with self.subTest(name):
                layout = dict(base, **change)
                self.assertEqual(native.Gpu._current_limit_capacity(layout), GPU._current_limit_capacity(layout))


if __name__ == "__main__":
    unittest.main()
