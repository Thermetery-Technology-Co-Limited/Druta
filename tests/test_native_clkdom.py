# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native per-domain clock-offset control block against nvbackend.

One stateful fake ClkDomCtlGet/ClkDomCtlSet/ClkMeasureFreq trio serves
nvbackend.GPU and the native core in turn. Every entry point must give the
same result, the same driver call sequence (every SET logs the whole declared
block it was handed, so a write that disturbs an unknown byte fails), and the
same fake-driver state afterwards. The fixture values are inputs to the
control logic, not claims about any board."""
import copy
import ctypes
import math
import unittest

from druta import native, nvbackend
from tests.test_native_telemetry import Rig, u32_callback
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend
UNKNOWN = object()  # ClkDomRig(arch=UNKNOWN): no architecture export at all

GET_ID, SET_ID, MEASURE_ID = 0xF58938F5, 0xD14B69CF, 0x527FC458
VERSION, SIZE = 0x000261A4, 0x61A4
MEASURE_VERSION = 0x0001000C
DRIFT_WORD = 3  # a header word the drifting fake bumps on every GET
TURING, BLACKWELL = NV.CLKDOM_LAYOUT_TURING, NV.CLKDOM_LAYOUT_BLACKWELL
NO_GETTER = ("unavailable", "clock-control getter is unavailable; Refresh capabilities retries discovery")
NO_DOMAIN = ("unavailable", "no clock-control domain answered; Refresh capabilities retries discovery")


def word(layout, control, field):
    """Dword index of `field` (a layout byte offset) in control `control`'s entry."""
    return (layout.header + control * layout.stride + field) // 4


def filler(control, k):
    return (0xC0DE0000 | (control << 8) | k) & 0xFFFFFFFF


class Block(bytes):
    """The SET's bytes; a short repr keeps a failing parity diff readable."""

    def __repr__(self):
        return f"Block(len={len(self)}, crc={hash(bytes(self)) & 0xFFFFFFFF:08x})"


def diff_words(a, b):
    wa = (ctypes.c_uint32 * (len(a) // 4)).from_buffer_copy(a)
    wb = (ctypes.c_uint32 * (len(b) // 4)).from_buffer_copy(b)
    return [(i, hex(wa[i]), hex(wb[i])) for i in range(len(wa)) if wa[i] != wb[i]]


class ClkDomRig(Rig):
    """Rig plus a stateful clock-control block.

    accepted:     control indices the fake driver accepts (any other mask bit fails the WHOLE GET with -1)
    get_status:   every GET fails with this status
    fail_after:   GETs fail with fail_status once this many GETs have succeeded (fail_times times, or forever)
    echo_version / echo_mask: what a successful GET echoes, from the echo_from'th successful GET on
    drift:        every GET bumps header word 3, so two reads never match
    reassert:     the SET returns 0 and changes nothing
    preload:      {control: {field: value}} over the default entries
    """

    def __init__(self, *, arch=6, accepted=None, with_get=True, with_set=True, get_status=0, set_status=0,
                 echo_version=VERSION, echo_mask=True, echo_from=0, drift=False, reassert=False,
                 fail_after=None, fail_status=-5, fail_times=None, with_measure=False, measure_values=None,
                 preload=None, **kw):
        super().__init__(**kw)
        self.accepted_config = list(range(10)) if accepted is None else list(accepted)
        self.get_status, self.set_status = get_status, set_status
        self.echo_version, self.echo_mask, self.echo_from = echo_version, echo_mask, echo_from
        self.drift, self.reassert = drift, reassert
        self.fail_after, self.fail_status, self.fail_times = fail_after, fail_status, fail_times
        self.measure_values = dict(measure_values or {})
        self.preload = copy.deepcopy(preload or {})
        # the fake driver's field placement for this generation (Kepler/Maxwell/unknown: the Turing shape)
        self.layout = BLACKWELL if arch == 10 else TURING
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names
        if arch is UNKNOWN:
            e.pop("nvmlDeviceGetArchitecture", None)
        else:
            e["nvmlDeviceGetArchitecture"] = u32_callback(arch)
        self.reset()

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def get(handle, pointer):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            version, mask = words[0], words[2]
            self.calls.append(("ClkDomCtlGet", mask))
            self.gets += 1
            if version != VERSION:
                return -9
            if self.get_status:
                return self.get_status
            if self.fail_after is not None and self.successes >= self.fail_after and (
                    self.fail_times is None or self.failures < self.fail_times):
                self.failures += 1
                return self.fail_status
            allowed = 0
            for d in self.accepted:
                allowed |= 1 << d
            if mask == 0 or mask & ~allowed:
                return -1
            self.fill(words, mask, bad_echo=self.successes >= self.echo_from)
            self.successes += 1
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def set_(handle, pointer):
            data = Block(ctypes.string_at(pointer, SIZE))
            self.calls.append(("ClkDomCtlSet", data))
            if self.set_status:
                return self.set_status
            if not self.reassert:
                words = (ctypes.c_int32 * (SIZE // 4)).from_buffer_copy(data)
                mask = words[2] & 0xFFFFFFFF
                lay = self.layout
                for d in range(32):
                    if mask >> d & 1:
                        self.entries[d] = {
                            "mode": words[word(lay, d, lay.mode)], "freq": words[word(lay, d, lay.freq_khz)],
                            "nvvdd": words[word(lay, d, lay.nvvdd_uv)], "msvdd": words[word(lay, d, lay.msvdd_uv)]}
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def measure(handle, pointer):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            self.calls.append(("ClkMeasureFreq", words[0], words[1]))
            if words[0] != MEASURE_VERSION:
                return -9
            if words[1] not in self.measure_values:
                return -104
            words[2] = self.measure_values[words[1]]
            return 0

        if with_get:
            n["ClkDomCtlGet"] = i[GET_ID] = get
        if with_set:
            n["ClkDomCtlSet"] = i[SET_ID] = set_
        if with_measure:
            n["ClkMeasureFreq"] = i[MEASURE_ID] = measure

    def fill(self, words, mask, *, bad_echo=True):
        lay = self.layout
        signed = ctypes.cast(words, ctypes.POINTER(ctypes.c_int32))
        for d in range(32):
            if not mask >> d & 1:
                continue
            base = (lay.header + d * lay.stride) // 4
            for k in range(lay.stride // 4):
                words[base + k] = filler(d, k)
            entry = self.entries[d]
            signed[word(lay, d, lay.mode)] = entry["mode"]
            signed[word(lay, d, lay.freq_khz)] = entry["freq"]
            signed[word(lay, d, lay.nvvdd_uv)] = entry["nvvdd"]
            signed[word(lay, d, lay.msvdd_uv)] = entry["msvdd"]
        words[0] = self.echo_version if bad_echo else VERSION
        words[2] = mask if (self.echo_mask or not bad_echo) else 0
        if self.drift:
            words[DRIFT_WORD] = self.gets

    def expected_block(self, mask, changes=()):
        """The declared block a GET of `mask` produces from the configured state, with `changes` applied."""
        saved = (self.entries, self.gets)
        self.entries = self.initial_entries()
        try:
            buf = (ctypes.c_uint32 * (SIZE // 4))()
            buf[0], buf[2] = VERSION, mask
            self.fill(buf, mask)
        finally:
            self.entries, self.gets = saved
        signed = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int32))
        for index, value in dict(changes).items():
            signed[index] = value
        return bytes(buf)

    def initial_entries(self):
        entries = {d: {"mode": 8 if d % 2 == 0 else 9, "freq": 0, "nvvdd": 0, "msvdd": 0} for d in range(32)}
        for d, fields in self.preload.items():
            entries[d].update(fields)
        return entries

    def reset(self):
        self.calls = []
        self.accepted = list(self.accepted_config)
        self.entries = self.initial_entries()
        self.successes = self.failures = self.gets = 0

    def state(self):
        return copy.deepcopy(self.entries)

    def python_gpu(self):
        gpu = super().python_gpu()
        for name in ("ClkDomCtlGet", "ClkDomCtlSet", "ClkMeasureFreq"):
            setattr(gpu.nvapi, name, self.names.get(name))
        return gpu


def attr(gpu, name, default=None):
    """A discovery attribute: a property on the native handle, set lazily on the Python twin."""
    return getattr(gpu, name, default)


def gets(*masks):
    return [("ClkDomCtlGet", m) for m in masks]


DISCOVERY = gets(*(1 << d for d in range(32)))


class ClkDomCase(ParityCase):
    def primed(self, rig, action, *, msvdd=False, xoc=False):
        """Discover and validate first (its calls dropped), set the opt-ins, then act."""
        def run(g):
            g.clkdom_layout()
            if msvdd:
                g.msvdd_write_enabled = True
            if xoc:
                g.voltage_xoc_enabled = True
            rig.calls.clear()
            return action(g)
        return run

    def call(self, rig, action, **kw):
        return self.both(rig, self.primed(rig, action, **kw))

    def assert_single_dword_set(self, rig, logged, mask, index, value):
        expected = rig.expected_block(mask, {index: value})
        self.assertEqual(diff_words(logged, expected), [])
        self.assertEqual(len(diff_words(logged, rig.expected_block(mask))), 1)


class DiscoveryTests(ClkDomCase):
    def test_domains_are_probed_one_bit_at_a_time(self):
        rig = ClkDomRig(accepted=[0, 1, 2, 3, 5, 9])
        (domains, errors), calls, _ = self.both(rig, lambda g: (g.clkdom_domains(), attr(g, "_clkdom_probe_errors")))
        self.assertEqual(domains, [0, 1, 2, 3, 5, 9])
        self.assertEqual(calls, DISCOVERY)
        self.assertEqual(errors, {d: "status -1" for d in range(32) if d not in (0, 1, 2, 3, 5, 9)})

    def test_echo_mismatch_is_recorded_per_domain(self):
        rig = ClkDomRig(accepted=[0, 1], echo_mask=False)
        (domains, errors), *_ = self.both(rig, lambda g: (g.clkdom_domains(), attr(g, "_clkdom_probe_errors")))
        self.assertEqual(domains, [])
        self.assertEqual(errors[0], "version/mask echo mismatch")
        self.assertEqual(errors[1], "version/mask echo mismatch")
        self.assertEqual(errors[2], "status -1")

    def test_result_is_cached(self):
        rig = ClkDomRig()

        def twice(g):
            first = g.clkdom_domains()
            rig.calls.clear()
            return first, g.clkdom_domains()
        (first, second), calls, _ = self.both(rig, twice)
        self.assertEqual(first, second)
        self.assertEqual(calls, [])

    def test_no_getter(self):
        (domains, errors), calls, _ = self.both(
            ClkDomRig(with_get=False), lambda g: (g.clkdom_domains(), attr(g, "_clkdom_probe_errors")))
        self.assertEqual((domains, errors, calls), ([], {}, []))
        self.assertEqual(self.both(ClkDomRig(with_get=False), lambda g: g.clkdom_ok())[0], False)
        self.assertEqual(self.both(ClkDomRig(), lambda g: g.clkdom_ok())[0], True)


class LayoutTests(ClkDomCase):
    def layout(self, rig):
        return self.both(rig, lambda g: (g.clkdom_layout(), attr(g, "_clkdom_capability_error")))

    def test_measured_generations(self):
        for arch, expected in ((6, TURING), (7, TURING), (8, TURING), (4, TURING), (10, BLACKWELL)):
            with self.subTest(arch=arch):
                (layout, error), calls, _ = self.layout(ClkDomRig(arch=arch, accepted=[3, 4, 5]))
                self.assertEqual(layout, expected)
                self.assertIsNone(error)
                self.assertEqual(calls, DISCOVERY + gets(1 << 3))  # validation uses the first ACCEPTED bit

    def test_unmeasured_generations_fail_closed_without_a_call(self):
        for arch in (2, 3, 5, 9, UNKNOWN):
            with self.subTest(arch=arch):
                (layout, error), calls, _ = self.layout(ClkDomRig(arch=arch))
                self.assertIsNone(layout)
                shown = None if arch is UNKNOWN else arch
                self.assertEqual(error, ("unsupported_layout",
                                         f"clock-control field layout is unknown for architecture {shown}"))
                self.assertEqual(calls, [])

    def test_unavailable_and_failed_validation(self):
        (layout, error), calls, _ = self.layout(ClkDomRig(with_get=False))
        self.assertEqual((layout, error, calls), (None, NO_GETTER, []))
        (layout, error), calls, _ = self.layout(ClkDomRig(accepted=[]))
        self.assertEqual((layout, error, calls), (None, NO_DOMAIN, DISCOVERY))
        (layout, error), calls, _ = self.layout(ClkDomRig(fail_after=10, fail_status=-5))
        self.assertEqual((layout, error), (None, ("read_failure", "clock-control validation read failed with status -5")))
        self.assertEqual(calls, DISCOVERY + gets(1))
        # an echo that is wrong from the start fails discovery itself ...
        (layout, error), *_ = self.layout(ClkDomRig(echo_version=0x000161A4))
        self.assertEqual((layout, error), (None, NO_DOMAIN))
        # ... and one that turns wrong after discovery fails validation
        (layout, error), *_ = self.layout(ClkDomRig(echo_version=0x000161A4, echo_from=10))
        self.assertEqual((layout, error), (None, ("unsupported_response",
                                                  "clock-control response did not echo the expected version and mask")))
        (layout, error), *_ = self.layout(ClkDomRig(echo_mask=False, echo_from=10))
        self.assertEqual(error[0], "unsupported_response")

    def test_result_is_cached(self):
        for config in ({}, {"accepted": []}, {"fail_after": 10}):
            with self.subTest(**config):
                rig = ClkDomRig(**config)

                def twice(g):
                    first = (g.clkdom_layout(), attr(g, "_clkdom_capability_error"))
                    rig.calls.clear()
                    return first, (g.clkdom_layout(), attr(g, "_clkdom_capability_error"))
                (first, second), calls, _ = self.both(rig, twice)
                self.assertEqual(first, second)
                self.assertEqual(calls, [])


class ControlTableTests(ClkDomCase):
    def test_controls_for_ui(self):
        full = {6: [0, 1, 2, 3, 5, 9], 7: [1, 2, 3, 5, 9], 8: [1, 2, 3, 5, 9], 10: [1, 2, 3, 4], 4: [0, 1, 2, 3, 5, 9]}
        subset = {6: [1, 3, 9], 7: [1, 3, 9], 8: [1, 3, 9], 10: [1, 3], 4: [1, 3, 9]}
        for arch in full:
            for accepted, expected in ((list(range(10)), full[arch]), ([1, 3, 9], subset[arch])):
                with self.subTest(arch=arch, accepted=accepted):
                    value, *_ = self.both(ClkDomRig(arch=arch, accepted=accepted), lambda g: g.clkdom_controls_for_ui())
                    self.assertEqual(value, expected)
        for config in ({"arch": 2}, {"with_get": False}, {"accepted": []}):
            with self.subTest(**config):
                self.assertEqual(self.both(ClkDomRig(**config), lambda g: g.clkdom_controls_for_ui())[0], [])

    def test_labels_polarity_and_step(self):
        expected = {
            6: {1: "XBAR", 9: "LTC", 4: "domain 4", 7: "domain 7"},
            4: {1: "XBAR", 7: "domain 7"},
            2: {1: "XBAR", 7: "domain 7"},
            7: {1: "Crossbar", 9: "LTC", 0: "domain 0", 7: "domain 7"},
            8: {1: "Crossbar", 9: "LTC?", 7: "domain 7"},
            10: {1: "XBAR", 3: "SYSCLK", 4: "VIDEO", 0: "control 0", 7: "control 7"},
        }
        for arch, labels in expected.items():
            with self.subTest(arch=arch):
                def action(g):
                    return ({c: g.clkdom_control_label(c) for c in labels},
                            [g.clkdom_control_polarity(c) for c in range(10)], g.clkdom_step_mhz(),
                            g.clkdom_is_blackwell())
                (got, polarity, step, blackwell), calls, _ = self.both(ClkDomRig(arch=arch), action)
                self.assertEqual(got, labels)
                self.assertEqual(polarity, [1] * 10)
                self.assertEqual(step, 1 if arch == 10 else None)
                self.assertEqual(blackwell, arch == 10)
                self.assertEqual(calls, [])

    def test_measured_delta_tables(self):
        cases = [
            ("clkdom_delta_inert", 4, 1, True), ("clkdom_delta_inert", 4, 2, False),
            ("clkdom_delta_inert", 4, 5, True), ("clkdom_delta_inert", 6, 2, False),
            ("clkdom_delta_inert", 6, 1, None), ("clkdom_delta_inert", 10, 2, False),
            ("clkdom_delta_inert", 6, None, None), ("clkdom_delta_inert", 7, 2, None),
            ("clkdom_offset_slider_offered", 4, 2, True), ("clkdom_offset_slider_offered", 4, 1, False),
            ("clkdom_offset_slider_offered", 4, 0, False), ("clkdom_offset_slider_offered", 6, 1, True),
            ("clkdom_offset_slider_offered", 2, 7, True),
            ("clkdom_delta_clears_ceiling", 4, 2, True), ("clkdom_delta_clears_ceiling", 10, 2, False),
            ("clkdom_delta_clears_ceiling", 6, 2, None), ("clkdom_delta_clears_ceiling", 4, 1, None),
        ]
        for method, arch, domain, expected in cases:
            with self.subTest(method=method, arch=arch, domain=domain):
                value, *_ = self.both(ClkDomRig(arch=arch), lambda g: getattr(g, method)(domain))
                self.assertIs(value, expected)
        self.assertIsNone(self.both(ClkDomRig(arch=6), lambda g: g.clkdom_delta_inert())[0])


class ReadOffsetsTests(ClkDomCase):
    PRELOAD = {0: {"freq": -15000, "msvdd": 6250}, 1: {"freq": 45000}, 3: {"mode": 3, "msvdd": -12500}}

    def expected_rows(self, accepted):
        rows = {d: {"mode": 8 if d % 2 == 0 else 9, "freq_khz": 0, "msvdd_uv": 0} for d in accepted}
        rows[0].update(freq_khz=-15000, msvdd_uv=6250)
        rows[1].update(freq_khz=45000)
        rows[3].update(mode=3, msvdd_uv=-12500)
        return rows

    def test_one_get_for_every_accepted_control(self):
        for arch in (6, 7, 10):
            with self.subTest(arch=arch):
                rig = ClkDomRig(arch=arch, accepted=[0, 1, 3, 7], preload=self.PRELOAD)
                (rows, err), calls, _ = self.call(rig, lambda g: g.read_clk_domain_offsets())
                self.assertIsNone(err)
                self.assertEqual(rows, self.expected_rows([0, 1, 3, 7]))
                self.assertEqual(calls, gets(0b10001011))

    def test_first_read_discovers_and_validates(self):
        rig = ClkDomRig(accepted=[2, 5], preload=self.PRELOAD)
        (rows, err), calls, _ = self.both(rig, lambda g: g.read_clk_domain_offsets())
        self.assertEqual(sorted(rows), [2, 5])
        self.assertEqual(calls, DISCOVERY + gets(1 << 2, (1 << 2) | (1 << 5)))

    def test_refusals(self):
        cases = [
            ({"with_get": False}, "per-domain clock control unavailable (0xF58938F5)"),
            ({"arch": 2}, "clock-domain layout is not validated for this generation/runtime ABI"),
            ({"accepted": []}, "clock-domain layout is not validated for this generation/runtime ABI"),
            ({"fail_after": 11, "fail_status": -7}, "clock-domain read failed (status -7)"),
            ({"echo_mask": False, "echo_from": 11}, "clock-domain block did not echo its version and requested mask"),
        ]
        for config, message in cases:
            with self.subTest(**config):
                rig = ClkDomRig(**config)
                (result, pending), *_ = self.both(
                    rig, lambda g: (g.read_clk_domain_offsets(), attr(g, "_clkdom_offset_read_pending", False)))
                self.assertEqual(result, (None, message))
                self.assertIs(pending, "read failed" in message or "echo" in message)

    def test_xbar_offset(self):
        rig = ClkDomRig(preload=self.PRELOAD)
        value, calls, _ = self.call(rig, lambda g: g.read_xbar_offset_mhz())
        self.assertEqual(value, 45.0)
        self.assertIs(type(value), float)
        self.assertEqual(calls, gets(0b1111111111))
        value, *_ = self.both(ClkDomRig(accepted=[0, 2], preload=self.PRELOAD), lambda g: g.read_xbar_offset_mhz())
        self.assertIsNone(value)
        value, *_ = self.both(ClkDomRig(arch=10, preload={1: {"freq": -200000}}), lambda g: g.read_xbar_offset_mhz())
        self.assertEqual(value, -200.0)
        self.assertIsNone(self.both(ClkDomRig(with_get=False), lambda g: g.read_xbar_offset_mhz())[0])


class SetClockOffsetTests(ClkDomCase):
    def test_write_changes_exactly_the_frequency_dword(self):
        for arch, label in ((6, "XBAR"), (7, "Crossbar"), (8, "Crossbar"), (10, "XBAR"), (4, "XBAR")):
            with self.subTest(arch=arch):
                rig = ClkDomRig(arch=arch)
                result, calls, state = self.call(rig, lambda g: g.set_clk_domain_offset(1, 50))
                self.assertEqual(result, (True, f"{label} offset +0 -> +50 MHz "
                                                "(requested; the driver floors to whole clock bins)"))
                self.assertEqual(calls[:2], gets(2, 2))
                self.assertEqual([c[0] for c in calls], ["ClkDomCtlGet", "ClkDomCtlGet", "ClkDomCtlSet"])
                layout = BLACKWELL if arch == 10 else TURING
                self.assert_single_dword_set(rig, calls[2][1], 2, word(layout, 1, layout.freq_khz), 50000)
                self.assertEqual(state[1]["freq"], 50000)  # Blackwell polarity +1: the stored value is the request
                self.assertEqual({d: e for d, e in state.items() if d != 1}, {
                    d: e for d, e in rig.initial_entries().items() if d != 1})

    def test_rounding_and_signs(self):
        for mhz, khz, text in ((-30, -30000, "-30"), (49.5, 50000, "+50"), (50.5, 50000, "+50"),
                               (51.5, 52000, "+52"), (-2.5, -2000, "-2"), (7.6, 8000, "+8")):
            with self.subTest(mhz=mhz):
                rig = ClkDomRig(arch=10)
                result, calls, state = self.call(rig, lambda g: g.set_clk_domain_offset(3, mhz))
                self.assertEqual(result, (True, f"SYSCLK offset +0 -> {text} MHz "
                                                "(requested; the driver floors to whole clock bins)"))
                self.assertEqual(state[3]["freq"], khz)
                self.assert_single_dword_set(rig, calls[2][1], 8, word(BLACKWELL, 3, BLACKWELL.freq_khz), khz)

    def test_already_set_writes_nothing(self):
        rig = ClkDomRig(preload={1: {"freq": 50000}})
        for mhz in (50, 49.5, 50.4):
            with self.subTest(mhz=mhz):
                result, calls, _ = self.call(rig, lambda g: g.set_clk_domain_offset(1, mhz))
                self.assertEqual(result, (True, "XBAR offset already +50 MHz; no write needed"))
                self.assertEqual(calls, gets(2, 2))
        result, calls, _ = self.call(ClkDomRig(), lambda g: g.set_clk_domain_offset(1, -0.4))
        self.assertEqual(result, (True, "XBAR offset already +0 MHz; no write needed"))
        self.assertEqual(calls, gets(2, 2))

    def test_argument_refusals_write_nothing(self):
        cases = [
            ((1.0, 50), "clock domain must be an integer control index"),
            ((True, 50), "clock domain must be an integer control index"),
            ((40, 50), "clock domain must be an integer control index"),
            ((-1, 50), "clock domain must be an integer control index"),
            ((1, "x"), "clock-domain offset must be a finite number"),
            ((1, None), "clock-domain offset must be a finite number"),
            ((1, math.nan), "clock-domain offset exceeds the signed 32-bit kHz range"),
            ((1, math.inf), "clock-domain offset exceeds the signed 32-bit kHz range"),
            ((1, -math.inf), "clock-domain offset exceeds the signed 32-bit kHz range"),
            ((1, 3e6), "clock-domain offset exceeds the signed 32-bit kHz range"),
            ((1, 10 ** 400), "clock-domain offset exceeds the signed 32-bit kHz range"),
            ((12, 50), "domain 12 is not one this driver accepts ([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                rig = ClkDomRig()
                result, calls, state = self.call(rig, lambda g: g.set_clk_domain_offset(*args))
                self.assertEqual(result, (False, message))
                self.assertEqual(calls, [])
                self.assertEqual(state, rig.initial_entries())

    def test_driver_refusals_write_nothing(self):
        cases = [
            ({"with_set": False}, "per-domain clock control unavailable"),
            ({"with_get": False}, "per-domain clock control unavailable"),
            ({"arch": 2}, "clock-domain layout is not validated for this generation/runtime ABI"),
            ({"accepted": []}, "clock-domain layout is not validated for this generation/runtime ABI"),
            ({"fail_after": 11, "fail_status": -6}, "clock-domain read failed (status -6)"),
            ({"fail_after": 12, "fail_status": -6}, "verification read failed (status -6)"),
            ({"echo_version": 1, "echo_from": 11},
             "clock-domain block did not echo its version and requested mask"),
            ({"echo_mask": False, "echo_from": 12},
             "verification block did not echo its version and requested mask"),
            ({"drift": True}, "clock-domain block changed between reads; no write issued"),
        ]
        for config, message in cases:
            with self.subTest(**config):
                rig = ClkDomRig(**config)
                result, calls, state = self.both(rig, lambda g: g.set_clk_domain_offset(1, 50))
                self.assertEqual(result, (False, message))
                self.assertNotIn("ClkDomCtlSet", [c[0] for c in calls])
                self.assertEqual(state, rig.initial_entries())

    def test_failed_set(self):
        rig = ClkDomRig(set_status=-3)
        result, calls, state = self.call(rig, lambda g: g.set_clk_domain_offset(1, 50))
        self.assertEqual(result, (False, "clock-domain write failed (status -3)"))
        self.assertEqual([c[0] for c in calls], ["ClkDomCtlGet", "ClkDomCtlGet", "ClkDomCtlSet"])
        self.assertEqual(state, rig.initial_entries())


class RailOffsetTests(ClkDomCase):
    PRELOAD = {0: {"nvvdd": 12500, "msvdd": -6250}, 3: {"nvvdd": 25000, "msvdd": 10000},
               7: {"nvvdd": 1000, "msvdd": 2000}}

    def test_read(self):
        rig = ClkDomRig(preload=self.PRELOAD)
        value, calls, _ = self.call(rig, lambda g: g.read_rail_offset_mv())
        self.assertEqual((value, calls), (12.5, gets(1)))
        value, calls, _ = self.call(rig, lambda g: g.read_rail_offset_mv(3))
        self.assertEqual((value, calls), (25.0, gets(8)))
        value, calls, _ = self.call(rig, lambda g: g.read_rail_offset_mv(3, rail=1))
        self.assertEqual((value, calls), (10.0, gets(8)))
        value, *_ = self.call(rig, lambda g: g.read_rail_offset_mv(7, rail=1))
        self.assertIsNone(value)  # accepted but not an understood control
        value, *_ = self.call(rig, lambda g: g.read_rail_offset_mv(12))
        self.assertIsNone(value)  # the driver rejects the mask
        value, *_ = self.call(ClkDomRig(arch=10, preload=self.PRELOAD), lambda g: g.read_rail_offset_mv(3))
        self.assertEqual(value, 25.0)
        self.assertIsNone(self.both(ClkDomRig(arch=2), lambda g: g.read_rail_offset_mv())[0])

    def test_capability(self):
        def unavailable(rail, domain, reason):
            return {"available": False, "domain": domain, "value_mv": None, "experimental": rail == 1,
                    "reason": reason}

        def available(rail, domain, value):
            return {"available": True, "domain": domain, "value_mv": value, "experimental": rail == 1,
                    "reason": "request field readable; physical voltage response is unverified"}
        cases = [
            ((0,), {}, available(0, 0, 12.5)), ((0, 3), {}, available(0, 3, 25.0)),
            ((1,), {}, available(1, 0, -6.25)), ((1, 3), {}, available(1, 3, 10.0)),
            ((2,), {}, unavailable(2, None, "unknown rail offset index")),
            ((0, 40), {}, unavailable(0, 40, "invalid clock-control index")),
            ((1, 7), {}, unavailable(1, 7, "no understood rail-offset control record answered")),
            ((1, 12), {}, unavailable(1, 12, "no understood rail-offset control record answered")),
            ((0,), {"arch": 2}, unavailable(0, None, "rail-offset field layout is unavailable")),
            ((1, 3), {"with_get": False}, unavailable(1, 3, "rail-offset field layout is unavailable")),
            # without control 0 the default falls to the first understood accepted control
            ((0,), {"accepted": [2, 3, 7]}, available(0, 2, 0.0)),
            ((1,), {"arch": 10}, available(1, 0, -6.25)),
            ((1, 3), {"fail_after": 11}, unavailable(1, 3, "no understood rail-offset control record answered")),
        ]
        for args, config, expected in cases:
            with self.subTest(args=args, **config):
                rig = ClkDomRig(preload=self.PRELOAD, **config)
                value, *_ = self.both(rig, lambda g: g.rail_offset_capability(*args))
                self.assertEqual(value, expected)

    def test_nvvdd_write(self):
        rig = ClkDomRig()
        result, calls, state = self.call(rig, lambda g: g.set_rail_offset_mv(25.0))
        self.assertEqual(result, (True, "core rail (NVVDD) stored request +0.00 -> +25.00 mV; "
                                        "physical voltage response is unverified"))
        self.assertEqual([c[0] for c in calls], ["ClkDomCtlGet"] * 2 + ["ClkDomCtlSet", "ClkDomCtlGet"])
        self.assertEqual([c[1] for c in calls if c[0] == "ClkDomCtlGet"], [1, 1, 1])
        self.assert_single_dword_set(rig, calls[2][1], 1, word(TURING, 0, TURING.nvvdd_uv), 25000)
        self.assertEqual(state[0]["nvvdd"], 25000)
        for mv, uv in ((6.25, 6250), (-12.5, -12500), (0.0004, None), (0.0015, 2), (0.0025, 2)):
            with self.subTest(mv=mv):
                result, calls, state = self.call(ClkDomRig(), lambda g: g.set_rail_offset_mv(mv))
                if uv is None:
                    self.assertEqual(result, (True, "core rail (NVVDD) offset already +0.00 mV"))
                    self.assertEqual(calls, gets(1))
                else:
                    self.assertTrue(result[0], result)
                    self.assertEqual(state[0]["nvvdd"], uv)

    def test_blackwell_write_uses_the_shifted_field(self):
        rig = ClkDomRig(arch=10)
        result, calls, state = self.call(rig, lambda g: g.set_rail_offset_mv(-10, 3))
        self.assertTrue(result[0], result)
        self.assert_single_dword_set(rig, calls[2][1], 8, word(BLACKWELL, 3, BLACKWELL.nvvdd_uv), -10000)
        self.assertEqual(state[3]["nvvdd"], -10000)

    def test_bounds(self):
        result, calls, _ = self.call(ClkDomRig(), lambda g: g.set_rail_offset_mv(250))
        self.assertEqual(result, (False, "NVVDD offset 250 mV is outside Druta's -100..200 mV bound"))
        self.assertEqual(calls, gets(1))
        result, *_ = self.call(ClkDomRig(), lambda g: g.set_rail_offset_mv(-100.5))
        self.assertEqual(result, (False, "NVVDD offset -100.5 mV is outside Druta's -100..200 mV bound"))
        for mv in (250, -400, 500, -500):
            with self.subTest(xoc=mv):
                result, _, state = self.call(ClkDomRig(), lambda g: g.set_rail_offset_mv(mv), xoc=True)
                self.assertTrue(result[0], result)
                self.assertEqual(state[0]["nvvdd"], mv * 1000)
        result, *_ = self.call(ClkDomRig(), lambda g: g.set_rail_offset_mv(501), xoc=True)
        self.assertEqual(result, (False, "NVVDD offset 501 mV is outside Druta's -500..500 mV bound"))
        # a stored value beyond the bound widens it rather than trapping the user
        rig = ClkDomRig(preload={0: {"nvvdd": 300000}})
        result, calls, _ = self.call(rig, lambda g: g.set_rail_offset_mv(300))
        self.assertEqual((result, calls), ((True, "core rail (NVVDD) offset already +300.00 mV"), gets(1)))
        result, _, state = self.call(rig, lambda g: g.set_rail_offset_mv(250))
        self.assertEqual(result, (True, "core rail (NVVDD) stored request +300.00 -> +250.00 mV; "
                                        "physical voltage response is unverified"))
        self.assertEqual(state[0]["nvvdd"], 250000)
        result, *_ = self.call(rig, lambda g: g.set_rail_offset_mv(301))
        self.assertEqual(result, (False, "NVVDD offset 301 mV is outside Druta's -100..300 mV bound"))

    def test_failures(self):
        cases = [
            ({"reassert": True}, (25.0,), "NVVDD request readback mismatch: requested +25, stored +0 mV"),
            ({"set_status": -3}, (25.0,), "core rail (NVVDD) write failed (status -3)"),
            ({"drift": True}, (25.0,),
             f"refusing to write: 2 dwords differ, expected only {word(TURING, 0, TURING.nvvdd_uv)}"),
            ({}, (25.0, 0, 2), "unknown rail offset index"),
            ({}, (25.0, 40), "clock-control index must be an integer from 0 to 31"),
            ({}, (25.0, True), "clock-control index must be an integer from 0 to 31"),
            ({}, ("x",), "NVVDD offset must be a finite number"),
            ({}, (math.inf,), "NVVDD offset exceeds the signed 32-bit microvolt range"),
            ({}, (math.nan,), "NVVDD offset exceeds the signed 32-bit microvolt range"),
            ({}, (3e6,), "NVVDD offset exceeds the signed 32-bit microvolt range"),
            ({"with_set": False}, (25.0,), "per-domain clock control unavailable"),
            ({"arch": 2}, (25.0,), "NVVDD layout is not validated for this generation/runtime ABI"),
            ({}, (25.0, 12), "NVVDD request read failed or returned an invalid block"),
            ({"fail_after": 11}, (25.0,), "NVVDD request read failed or returned an invalid block"),
            ({"fail_after": 12}, (25.0,), "NVVDD verification read failed or returned an invalid block"),
            ({"fail_after": 13}, (25.0,), "NVVDD request sent, but its readback is unavailable or invalid"),
        ]
        for config, args, message in cases:
            with self.subTest(args=args, **config):
                rig = ClkDomRig(**config)
                result, calls, state = self.both(rig, lambda g: g.set_rail_offset_mv(*args))
                self.assertEqual(result, (False, message))
                if "write failed" not in message and "readback" not in message:
                    self.assertNotIn("ClkDomCtlSet", [c[0] for c in calls])
                    self.assertEqual(state, rig.initial_entries())


class MsvddTests(ClkDomCase):
    def test_opt_in_gates_the_write(self):
        rig = ClkDomRig()
        result, calls, state = self.call(rig, lambda g: g.set_rail_offset_mv(10, 3, rail=1))
        self.assertEqual(result, (False, "MSVDD offset requires the experimental XOC opt-in"))
        self.assertEqual((calls, state), (gets(8), rig.initial_entries()))
        flags, *_ = self.both(rig, lambda g: (g.msvdd_write_enabled, g.voltage_xoc_enabled))
        self.assertEqual(flags, (False, False))
        # XOC alone is not the MSVDD opt-in
        result, *_ = self.call(rig, lambda g: g.set_rail_offset_mv(10, 3, rail=1), xoc=True)
        self.assertEqual(result, (False, "MSVDD offset requires the experimental XOC opt-in"))

    def test_write_changes_exactly_the_msvdd_dword(self):
        for arch, layout in ((6, TURING), (10, BLACKWELL)):
            with self.subTest(arch=arch):
                rig = ClkDomRig(arch=arch)
                result, calls, state = self.call(rig, lambda g: g.set_rail_offset_mv(10, 3, rail=1), msvdd=True)
                self.assertEqual(result, (True, "MSVDD request stored at control 3: +10 mV; register readback "
                                                "matched, physical voltage response is unverified"))
                self.assertEqual([c[0] for c in calls], ["ClkDomCtlGet"] * 3 + ["ClkDomCtlSet", "ClkDomCtlGet"])
                self.assertEqual([c[1] for c in calls if c[0] == "ClkDomCtlGet"], [8, 8, 8, 8])
                self.assert_single_dword_set(rig, calls[3][1], 8, word(layout, 3, layout.msvdd_uv), 10000)
                self.assertEqual(state[3]["msvdd"], 10000)

    def test_already_reset_and_bounds(self):
        rig = ClkDomRig(preload={3: {"msvdd": 10000}})
        result, calls, _ = self.call(rig, lambda g: g.set_rail_offset_mv(10, 3, rail=1), msvdd=True)
        self.assertEqual((result, calls), ((True, "MSVDD request already +10 mV; no write needed"), gets(8)))
        result, calls, _ = self.call(rig, lambda g: g.set_rail_offset_mv(10.0004, 3, rail=1))
        self.assertEqual(result, (True, "MSVDD request already +10 mV; no write needed"))
        # reset clears a stored request even with the opt-in off
        result, calls, state = self.call(rig, lambda g: g.reset_msvdd_offset_mv(3))
        self.assertEqual(result, (True, "MSVDD request stored at control 3: +0 mV; register readback matched, "
                                        "physical voltage response is unverified"))
        self.assert_single_dword_set(rig, calls[3][1], 8, word(TURING, 3, TURING.msvdd_uv), 0)
        self.assertEqual(state[3]["msvdd"], 0)
        result, calls, _ = self.call(ClkDomRig(), lambda g: g.reset_msvdd_offset_mv(3))
        self.assertEqual((result, calls), ((True, "MSVDD request already +0 mV; no write needed"), gets(8)))
        result, *_ = self.call(ClkDomRig(), lambda g: g.set_rail_offset_mv(-600, 3, rail=1), msvdd=True)
        self.assertEqual(result, (False, "MSVDD offset is outside -500..500 mV"))
        result, *_ = self.call(ClkDomRig(preload={3: {"msvdd": 700000}}),
                               lambda g: g.set_rail_offset_mv(650, 3, rail=1), msvdd=True)
        self.assertTrue(result[0], result)  # a stored value beyond the bound widens it

    def test_refusals(self):
        cases = [
            ({}, (10, 7), {}, "no understood rail-offset control record answered"),
            ({}, (10, 40), {}, "clock-control index must be an integer from 0 to 31"),
            ({}, ("x", 3), {}, "MSVDD offset must be a finite number"),
            ({}, (math.inf, 3), {}, "MSVDD offset exceeds the signed 32-bit microvolt range"),
            ({"with_set": False}, (10, 3), {}, "per-domain clock control unavailable"),
            ({"arch": 2}, (10, 3), {}, "rail-offset field layout is unavailable"),
            ({"reassert": True}, (10, 3), {"msvdd": True}, "MSVDD request readback mismatch: requested +10, stored +0 mV"),
            ({"set_status": -4}, (10, 3), {"msvdd": True}, "MSVDD request write failed (status -4)"),
            ({"drift": True}, (10, 3), {"msvdd": True}, "MSVDD control block changed between reads; no write issued"),
            ({"fail_after": 12}, (10, 3), {"msvdd": True}, "MSVDD request read failed or returned an invalid block"),
            ({"fail_after": 14}, (10, 3), {"msvdd": True}, "MSVDD request sent, but its readback is unavailable"),
        ]
        for config, args, flags, message in cases:
            with self.subTest(args=args, **config):
                rig = ClkDomRig(**config)
                result, *_ = self.call(rig, lambda g: g.set_rail_offset_mv(args[0], args[1], rail=1), **flags)
                self.assertEqual(result, (False, message))


TURING_ROWS = [{"domain": 0, "prog_mhz": 1935.0}, {"domain": 1, "prog_mhz": 1600.5}, {"domain": 4, "prog_mhz": 7000.5}]
PASCAL_ROWS = [{"domain": 15, "prog_mhz": 3796.0}, {"domain": 0, "prog_mhz": 0.0}, {"domain": 4, "prog_mhz": 5005.0}]


class PairingTests(ClkDomCase):
    def pairing(self, arch, *row_sets):
        return self.both(ClkDomRig(arch=arch), lambda g: [g.clkdom_pairing(rows) for rows in row_sets])

    def test_by_populated_domain(self):
        result, calls, _ = self.pairing(6, TURING_ROWS)
        self.assertEqual(result, [NV.CLKDOM_PAIR_TURING])
        self.assertEqual(calls, [])
        self.assertEqual(self.pairing(6, PASCAL_ROWS)[0], [{}])
        # nothing recognised is not cached; the Turing answer is, and later rows do not replace it
        self.assertEqual(self.pairing(6, [], [{"domain": 0, "prog_mhz": None}], TURING_ROWS, [], PASCAL_ROWS)[0],
                         [{}, {}, NV.CLKDOM_PAIR_TURING, NV.CLKDOM_PAIR_TURING, NV.CLKDOM_PAIR_TURING])
        # the Pascal (empty) answer is not sticky either
        self.assertEqual(self.pairing(4, PASCAL_ROWS, TURING_ROWS)[0], [{}, NV.CLKDOM_PAIR_TURING])
        self.assertEqual(self.pairing(10, TURING_ROWS)[0], [NV.CLKDOM_PAIR_TURING])
        self.assertEqual(self.pairing(UNKNOWN, TURING_ROWS)[0], [NV.CLKDOM_PAIR_TURING])

    def test_measured_tables_and_kepler(self):
        self.assertEqual(self.pairing(7, [], PASCAL_ROWS)[0], [NV.CLKDOM_PAIR_AMPERE] * 2)
        self.assertEqual(self.pairing(8, [])[0], [NV.CLKDOM_PAIR_ADA])
        self.assertEqual(self.pairing(2, TURING_ROWS)[0], [{}])
        self.assertEqual(self.pairing(3, TURING_ROWS)[0], [NV.CLKDOM_PAIR_TURING])


class MeasurementTests(ClkDomCase):
    def test_blackwell_counters(self):
        values = {0: 2_915_500, 1: 500, 4: 14_778_000}  # 1: below the plausibility floor; 2: refused by the driver
        rig = ClkDomRig(arch=10, with_measure=True, measure_values=values)
        result, calls, _ = self.both(rig, lambda g: g.clkdom_measurements())
        self.assertEqual(result, {"gpc": 2_915_500, "xbar": None, "sys": None, "memory": 14_778_000})
        self.assertEqual(list(result), ["gpc", "xbar", "sys", "memory"])
        self.assertEqual(calls, [("ClkMeasureFreq", MEASURE_VERSION, k) for k in (0, 1, 2, 4)])
        result, *_ = self.both(ClkDomRig(arch=10, with_measure=True, measure_values={0: 1_000, 1: 40_000_000,
                                                                                     2: 40_000_001, 4: 999}),
                               lambda g: g.clkdom_measurements())
        self.assertEqual(result, {"gpc": 1_000, "xbar": 40_000_000, "sys": None, "memory": None})

    def test_elsewhere_and_without_the_export(self):
        result, calls, _ = self.both(ClkDomRig(arch=8, with_measure=True, measure_values={0: 2_000_000}),
                                     lambda g: g.clkdom_measurements())
        self.assertEqual((result, calls), ({}, []))
        result, calls, _ = self.both(ClkDomRig(arch=10), lambda g: g.clkdom_measurements())
        self.assertEqual((result, calls), ({"gpc": None, "xbar": None, "sys": None, "memory": None}, []))


class RetryTests(ClkDomCase):
    def test_bounded_retry_publishes_new_controls(self):
        rig = ClkDomRig(accepted=[0, 2])

        def action(g):
            out = [g.clkdom_controls_for_ui()]
            for now in (0, 0.5, 1.0, 3.9):
                rig.calls.clear()
                out.append((now, g.retry_clock_capabilities(now=now), list(rig.calls)))
            rig.accepted = [0, 1, 2, 3, 5, 9]
            for now in (4.0, 100.0):
                rig.calls.clear()
                out.append((now, g.retry_clock_capabilities(now=now), list(rig.calls)))
            out.append(g.clkdom_controls_for_ui())
            out.append(attr(g, "_clkdom_probe_errors"))
            return out
        out, *_ = self.both(rig, action)
        self.assertEqual(out[0], [0, 2])
        self.assertEqual(out[1], (0, False, []))       # arms the one-second delay
        self.assertEqual(out[2], (0.5, False, []))
        self.assertEqual(out[3], (1.0, False, gets(1 << 1, 1 << 3, 1 << 5, 1 << 9)))  # only the missing understood
        self.assertEqual(out[4], (3.9, False, []))     # the final attempt waits three more seconds
        self.assertEqual(out[5], (4.0, True, gets(1 << 1, 1 << 3, 1 << 5, 1 << 9)))
        self.assertEqual(out[6], (100.0, False, []))   # the budget is spent
        self.assertEqual(out[7], [0, 1, 2, 3, 5, 9])
        self.assertEqual({d: e for d, e in out[8].items() if d in (1, 3, 5, 9)}, {})

    def test_refresh_releases_the_caches(self):
        rig = ClkDomRig(accepted=[])

        def action(g):
            out = [g.clkdom_layout(), attr(g, "_clkdom_capability_error")]
            rig.calls.clear()
            out.append(g.clkdom_domains())
            out.append(list(rig.calls))  # cached: no probe
            out.append(g.refresh_capabilities())
            out.append(attr(g, "_clkdom_capability_error"))
            rig.accepted = [0, 1]
            rig.calls.clear()
            out.append(g.clkdom_domains())
            out.append(list(rig.calls))
            out.append(g.refresh_capabilities())
            return out
        out, *_ = self.both(rig, action)
        self.assertEqual(out[:4], [None, NO_DOMAIN, [], []])
        self.assertEqual(out[4], {"previous_clock_error": NO_DOMAIN, "retry_pending": True})
        self.assertIsNone(out[5])
        self.assertEqual(out[6:8], [[0, 1], DISCOVERY])
        self.assertEqual(out[8], {"previous_clock_error": None, "retry_pending": True})

    def test_refresh_restores_the_retry_budget(self):
        rig = ClkDomRig(accepted=[0, 2])

        def action(g):
            g.clkdom_layout()
            out = [g.retry_clock_capabilities(now=t) for t in (0, 1, 4, 10)]
            g.refresh_capabilities()
            rig.calls.clear()
            out += [g.retry_clock_capabilities(now=t) for t in (20, 21)]
            out.append(list(rig.calls))
            return out
        out, *_ = self.both(rig, action)
        self.assertEqual(out[:5], [False] * 5)
        # after a refresh the final attempt reprobes every understood control as a singleton
        # (no 32-bit discovery) and its accepted answer re-validates the layout
        self.assertIs(out[5], True)
        self.assertEqual(out[6], gets(1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 5, 1 << 9))

    def test_unrecoverable_cases_stop_without_probing(self):
        for config in ({"arch": 2}, {"accepted": list(range(10))}):
            with self.subTest(**config):
                rig = ClkDomRig(**config)

                def action(g):
                    g.clkdom_layout()
                    rig.calls.clear()
                    out = [g.retry_clock_capabilities(now=t) for t in (0, 1, 4, 10)]
                    return out, list(rig.calls)
                (out, calls), *_ = self.both(rig, action)
                self.assertEqual((out, calls), ([False] * 4, []))

    def test_failed_validation_recovers(self):
        rig = ClkDomRig(fail_after=7, fail_times=1, accepted=[0, 1, 2, 3, 5, 9, 31])  # discovery ends on a success

        def action(g):
            out = [g.clkdom_layout(), attr(g, "_clkdom_capability_error"), g.clkdom_controls_for_ui()]
            rig.calls.clear()
            out += [g.retry_clock_capabilities(now=t) for t in (0, 1)]
            out += [list(rig.calls), g.clkdom_layout(), attr(g, "_clkdom_capability_error"), g.clkdom_controls_for_ui()]
            return out
        out, *_ = self.both(rig, action)
        self.assertEqual(out[:3], [None, ("read_failure", "clock-control validation read failed with status -5"), []])
        self.assertEqual(out[3:5], [False, True])
        self.assertEqual(out[5], gets(1))  # one validation read of the lowest accepted control
        self.assertEqual(out[6:], [TURING, None, [0, 1, 2, 3, 5, 9]])

    def test_pending_request_read_recovers(self):
        rig = ClkDomRig(fail_after=11, fail_times=1)

        def action(g):
            out = [g.read_clk_domain_offsets()[1], attr(g, "_clkdom_offset_read_pending", False)]
            rig.calls.clear()
            out += [g.retry_clock_capabilities(now=t) for t in (0, 1)]
            out += [list(rig.calls), attr(g, "_clkdom_offset_read_pending", False)]
            return out
        out, *_ = self.both(rig, action)
        self.assertEqual(out[:2], ["clock-domain read failed (status -5)", True])
        self.assertEqual(out[2:4], [False, True])
        self.assertEqual(out[4], gets(0b1111111111))
        self.assertIs(out[5], False)

    def test_non_finite_time_is_refused(self):
        rig = ClkDomRig()
        for gpu in (rig.python_gpu(), rig.native_gpu()):
            with self.subTest(gpu=type(gpu).__name__):
                with self.assertRaises(ValueError):
                    gpu.retry_clock_capabilities(now=math.nan)
                if isinstance(gpu, native.Gpu):
                    gpu.close()


class ReadParityTests(ClkDomCase):
    def test_read_carries_the_rows_and_the_blackwell_requests(self):
        for arch in (6, 10):
            with self.subTest(arch=arch):
                rig = ClkDomRig(arch=arch, preload={1: {"freq": 45000}})
                keys = ("clk_domains", "clk_domains_err", "clkdom_offsets", "clkdom_offsets_err")

                def action(g):
                    d = g.read()
                    return {k: d[k] for k in keys if k in d}
                result, calls, _ = self.both(rig, action)
                self.assertIsNone(result["clk_domains_err"])
                self.assertTrue(result["clk_domains"])
                if arch == 10:
                    self.assertIsNone(result["clkdom_offsets_err"])
                    self.assertEqual(result["clkdom_offsets"][1], {"mode": 9, "freq_khz": 45000, "msvdd_uv": 0})
                    self.assertEqual(calls, DISCOVERY + gets(1, 0b1111111111))
                else:
                    self.assertNotIn("clkdom_offsets", result)
                    self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
