# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native private clock-domain rows against nvbackend.

One fake private getter (0x1BD69F49) serves both nvbackend.GPU.read_clock_domains
and the core's dc_gpu_clock_domains; the pure classifier is compared with
nvbackend.classify_domain_names over the rows of Python's own loop. Rows are
compared whole, so a name, a grade, a scale or a float-versus-int type that
differs on either side fails the test."""
import threading
import unittest

from druta import native, nvbackend
from tests.test_native_telemetry import Rig, struct_callback, u32_callback
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

NV = nvbackend
PRIV_ID = 0x1BD69F49
UNKNOWN = object()  # DomainRig(arch=UNKNOWN): no architecture export at all


def payload(domains):
    """288 words from {domain: (prog_khz, flags, meas_khz, srcid)}."""
    w = [0] * 288
    for dom, (prog, flags, meas, srcid) in domains.items():
        w[2 * dom], w[2 * dom + 1] = prog, flags
        w[64 + 7 * dom], w[65 + 7 * dom] = meas, srcid
        w[66 + 7 * dom] = 0xA5A50000 | dom  # a trailing B word the loop must not read as anything
    return w


# Fixture values are shaped after the cards the Python comments describe; they are
# inputs to a pure transform, not claims about any board.
TURING = payload({
    0: (1935000, 0x11, 1920000, 32),   # measured below programmed: a negative delta
    1: (1600500, 0x11, 1601000, 0),
    2: (1695000, 0x11, 1695000, 0),
    3: (405000, 0x01, 405000, 32),
    4: (7000500, 0x13, 7000500, 32),
    5: (1350000, 0x11, 1350000, 0),
    6: (1080000, 0x01, 1080000, 32),
    20: (540000, 0x01, 540000, 32),
    21: (1710000, 0x11, 1710000, 0),
    22: (108000, 0x01, 108000, 32),
    31: (3, 0x00, 3, 32),             # a PCIe generation, not a clock
})

PASCAL = payload({                  # GPC2CLK at 15, slaves 16/17 carry srcid 15; 0/1/2/21 empty
    4: (5005000, 0x13, 5005000, 32),
    15: (3796000, 0x11, 3795000, 32),
    16: (3670000, 0x11, 3668000, 15),
    17: (3200000, 0x11, 3200000, 15),
    31: (3, 0x00, 3, 32),
})

LEGACY = payload({                  # the Kepler / Maxwell domain set
    4: (3004000, 0x13, 3004000, 32),
    6: (405000, 0x01, 405000, 32),
    15: (2012000, 0x11, 2012000, 32),
    16: (2330000, 0x11, 2330000, 15),
    17: (2240000, 0x11, 2240000, 15),
    18: (540000, 0x01, 540000, 32),
    20: (324000, 0x01, 324000, 32),
    21: (540000, 0x01, 540000, 32),
    25: (2260000, 0x11, 2260000, 32),
    31: (2, 0x00, 2, 32),
})

BLACKWELL = payload({               # domain 5 reads zero with flags 0: an empty slot
    0: (2617000, 0x11, 2610000, 32),
    1: (2400000, 0x11, 2399000, 0),
    2: (2100000, 0x11, 2100000, 0),
    4: (15001000, 0x13, 15001000, 32),
    20: (2090000, 0x11, 2088000, 0),
    21: (2317000, 0x11, 310000, 0),
    31: (4, 0x00, 4, 32),
})


class DomainRig(Rig):
    """Rig whose private getter answers `words` (or `status`) and whose NVML
    reports `arch` (UNKNOWN removes the export, so arch() is None on both sides)."""

    def __init__(self, words, *, arch=6, status=0, **kw):
        super().__init__(**kw)
        self.words_config, self.status = list(words), status
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names
        if arch is UNKNOWN:
            e.pop("nvmlDeviceGetArchitecture", None)
        else:
            e["nvmlDeviceGetArchitecture"] = u32_callback(arch)

        def priv(pc):
            self.calls.append(("AllClocksPriv", pc.version))
            if self.status:
                return self.status
            for k, word in enumerate(self.words):
                pc.w[k] = word
            return 0
        n["AllClocksPriv"] = i[PRIV_ID] = struct_callback(NV._AllClocksPriv, priv)
        self.reset()

    def reset(self):
        self.calls = []
        self.words = list(self.words_config)

    def state(self):
        return list(self.words)


def read(core=None, mem=None, pc=None):
    """The same read_clock_domains call on either side; `pc` is a word list."""
    def action(g):
        given = pc
        if pc is not None and isinstance(g, NV.GPU):
            given = NV._AllClocksPriv()
            for k, word in enumerate(pc):
                given.w[k] = word
        return g.read_clock_domains(pc=given, core_mhz=core, mem_nvml=mem)
    return action


def by_domain(rows):
    return {r["domain"]: (r["name"], r["grade"], r["scale"]) for r in rows}


class ReadClockDomainsTests(ParityCase):
    def check(self, words, arch=6, **kw):
        rig = DomainRig(words, arch=arch)
        (rows, err), calls, _ = self.both(rig, read(**kw))
        self.assertIsNone(err)
        self.assertEqual(calls, [("AllClocksPriv", NV.NvAPI.ver(NV._AllClocksPriv, 2))])
        return rows

    def test_turing_full_table_and_row_fields(self):
        rows = self.check(TURING, core=1935, mem=7000)
        self.assertEqual(by_domain(rows), {
            0: ("GPC", "confirmed", 1), 1: ("XBAR", "confirmed", 1), 2: ("SYSCLK", "likely", 1),
            3: ("", "unnamed", 1), 4: ("MEM", "confirmed", 1), 5: ("LTCCLK", "likely", 1),
            6: ("", "unnamed", 1), 20: ("", "unnamed", 1), 21: ("VIDEO", "confirmed", 1),
            22: ("", "unnamed", 1), 31: ("PCIe link gen", "likely", 1)})
        gpc = rows[0]
        self.assertEqual(gpc, {"domain": 0, "name": "GPC", "grade": "confirmed", "kind": "freq",
                               "prog_khz": 1935000, "meas_khz": 1920000, "flags": 0x11, "srcid": 32, "scale": 1,
                               "prog_mhz": 1935.0, "meas_mhz": 1920.0, "delta_mhz": -15.0})
        self.assertIs(type(gpc["prog_mhz"]), float)
        self.assertIs(type(gpc["flags"]), int)
        link = rows[-1]
        self.assertEqual(link["kind"], "pcie_gen")
        self.assertEqual((link["prog_mhz"], link["meas_mhz"], link["delta_mhz"]), (None, None, None))
        self.assertEqual(link["prog_khz"], 3)

    def test_pascal_gpc2clk_and_its_slaves(self):
        rows = self.check(PASCAL, arch=4, core=1898, mem=5005)
        names = by_domain(rows)
        self.assertEqual(names[15], ("GPC2CLK", "confirmed", 2))
        self.assertEqual(names[16], ("XBAR2CLK", "likely", 2))
        self.assertEqual(names[17], ("SYS2CLK", "likely", 2))
        self.assertEqual(names[4], ("MEM", "confirmed", 1))
        for dom in (0, 1, 2, 21):  # TU102 names are never put on Pascal's empty slots
            self.assertEqual(names[dom], ("", "unpopulated", 1))
        self.assertEqual(names[31], ("PCIe link gen", "likely", 1))

    def test_kepler_and_maxwell_tables(self):
        kepler = by_domain(self.check(LEGACY, arch=2, core=1006, mem=3004))
        self.assertEqual(kepler[15], ("GPC2CLK", "confirmed", 2))
        self.assertEqual(kepler[4], ("MEM", "confirmed", 1))
        self.assertEqual(kepler[16], ("XBAR2CLK", "likely", 2))
        self.assertEqual(kepler[21], ("MSD", "likely", 1))
        self.assertEqual(kepler[25], ("L2C2CLK", "likely", 2))
        self.assertEqual(kepler[6], ("DISP", "likely", 1))
        maxwell = by_domain(self.check(LEGACY, arch=3, core=1006, mem=3004))
        self.assertEqual(maxwell[17], ("SYS2CLK", "likely", 2))
        self.assertEqual(maxwell[20], ("PWR", "likely", 1))
        # Kepler's 21/25 labels do not transfer to Maxwell
        self.assertEqual(maxwell[21], ("", "unnamed", 1))
        self.assertEqual(maxwell[25], ("", "unnamed", 1))

    def test_ampere_and_ada_tables(self):
        for arch in (7, 8):
            names = by_domain(self.check(TURING, arch=arch, core=1935, mem=7000))
            self.assertEqual(names[0], ("GPC", "confirmed", 1))
            self.assertEqual(names[1], ("Crossbar", "confirmed", 1))
            self.assertEqual(names[2], ("SYS", "confirmed", 1))
            self.assertEqual(names[5], ("LTC", "likely", 1))
            self.assertEqual(names[21], ("Video", "confirmed", 1))
            self.assertEqual(names[3], ("", "unnamed", 1))
            self.assertEqual(names[31], ("PCIe link gen", "likely", 1))

    def test_blackwell_table(self):
        # at idle nothing correlates, and the slots still name GPC and MEM
        names = by_domain(self.check(BLACKWELL, arch=10, core=180, mem=405))
        self.assertEqual(names[0], ("GPC", "confirmed", 1))
        self.assertEqual(names[4], ("MEM", "confirmed", 1))
        self.assertEqual(names[1], ("XBAR", "confirmed", 1))
        self.assertEqual(names[5], ("", "unpopulated", 1))
        self.assertEqual(names[20], ("", "unnamed", 1))
        self.assertEqual(names[21], ("VIDEO", "confirmed", 1))
        self.assertEqual(names[31], ("PCIe link gen", "likely", 1))

    def test_unknown_architecture_unique_correlation(self):
        names = by_domain(self.check(TURING, arch=UNKNOWN, core=1935, mem=7000))
        self.assertEqual(names[0], ("GPC", "confirmed", 1))   # turing_like via gpc_dom == 0
        self.assertEqual(names[4], ("MEM", "confirmed", 1))
        self.assertEqual(names[1], ("XBAR", "confirmed", 1))
        names = by_domain(self.check(PASCAL, arch=UNKNOWN, core=1898, mem=5005))
        self.assertEqual(names[15], ("GPC2CLK", "confirmed", 2))  # scale 2
        self.assertEqual(names[16], ("XBAR2CLK", "likely", 2))      # pascal_like via gpc_dom == 15

    def test_unknown_architecture_ambiguous_correlation(self):
        # domains 0 and 1 both match 1x core: no GPC, nothing turing-like, only domain 31 keeps its name
        words = payload({0: (1500000, 1, 1500000, 32), 1: (1500000, 1, 1500000, 0), 31: (3, 0, 3, 32)})
        names = by_domain(self.check(words, arch=UNKNOWN, core=1500))
        self.assertEqual(names, {0: ("", "unnamed", 1), 1: ("", "unnamed", 1), 2: ("", "unpopulated", 1),
                                 3: ("", "unpopulated", 1), 4: ("", "unpopulated", 1), 5: ("", "unpopulated", 1),
                                 6: ("", "unpopulated", 1), 20: ("", "unpopulated", 1),
                                 21: ("", "unpopulated", 1), 22: ("", "unpopulated", 1),
                                 31: ("PCIe link gen", "likely", 1)})
        # 1x on one domain and 2x on another is just as ambiguous
        words = payload({0: (1000000, 1, 1000000, 32), 1: (2000000, 1, 2000000, 0)})
        names = by_domain(self.check(words, arch=UNKNOWN, core=1000))
        self.assertEqual(names[0], ("", "unnamed", 1))
        self.assertEqual(names[1], ("", "unnamed", 1))

    def test_unknown_architecture_gpc_equal_to_mem_collapses(self):
        words = payload({0: (1000000, 1, 1000000, 32), 4: (3000000, 1, 3000000, 32)})
        names = by_domain(self.check(words, arch=UNKNOWN, core=1000, mem=1000))
        self.assertEqual(names[0], ("", "unnamed", 1))
        self.assertEqual(names[4], ("", "unnamed", 1))

    def test_blind_demotes_confirmed_to_likely(self):
        names = by_domain(self.check(TURING, arch=UNKNOWN, mem=7000))
        self.assertEqual(names[0], ("GPC", "likely", 1))
        self.assertEqual(names[4], ("MEM", "confirmed", 1))  # correlated against NVML's memory clock
        self.assertEqual(names[1], ("XBAR", "likely", 1))
        self.assertEqual(names[2], ("SYSCLK", "likely", 1))
        self.assertEqual(names[21], ("VIDEO", "likely", 1))
        names = by_domain(self.check(TURING, arch=UNKNOWN))
        self.assertEqual(names[4], ("MEM", "likely", 1))

    def test_unlisted_domains_are_reported_when_non_zero(self):
        words = list(TURING)
        words[2 * 9 + 1] = 5                          # domain 9: flags only
        words[2 * 12], words[64 + 7 * 12] = 777000, 776000  # domain 12: a real frequency
        words[2 * 13] = 500000                        # domain 13: programmed, never measured
        words[64 + 7 * 14] = 600000                   # domain 14: measured, nothing programmed
        rows = self.check(words)
        names = by_domain(rows)
        self.assertEqual(names[9], ("", "unpopulated", 1))
        self.assertEqual(names[12], ("", "unnamed", 1))
        self.assertNotIn(10, names)
        row12 = next(r for r in rows if r["domain"] == 12)
        self.assertEqual((row12["prog_mhz"], row12["meas_mhz"], row12["delta_mhz"]), (777.0, 776.0, -1.0))
        row9 = next(r for r in rows if r["domain"] == 9)
        self.assertEqual((row9["prog_mhz"], row9["meas_mhz"], row9["delta_mhz"]), (0.0, 0.0, None))
        # a delta needs both arrays; a measured-only row is not populated but is not empty either
        row13 = next(r for r in rows if r["domain"] == 13)
        self.assertEqual((row13["prog_mhz"], row13["meas_mhz"], row13["delta_mhz"]), (500.0, 0.0, None))
        row14 = next(r for r in rows if r["domain"] == 14)
        self.assertEqual((row14["prog_mhz"], row14["meas_mhz"], row14["delta_mhz"]), (0.0, 600.0, None))
        self.assertEqual(names[14], ("", "unnamed", 1))

    def test_getter_unavailable(self):
        result, calls, _ = self.both(DomainRig(TURING, status=-1), read(core=1935))
        self.assertEqual(result, (None, NV.PRIV_UNAVAIL))
        self.assertEqual(len(calls), 1)
        result, calls, _ = self.both(DomainRig(TURING, with_nvapi=False), read(core=1935))
        self.assertEqual(result, (None, NV.PRIV_UNAVAIL))
        self.assertEqual(calls, [])

    def test_explicit_payload_skips_the_getter(self):
        rig = DomainRig(TURING, arch=4, status=-1)  # the getter would fail if it were asked
        (rows, err), calls, _ = self.both(rig, read(core=1898, mem=5005, pc=PASCAL))
        self.assertIsNone(err)
        self.assertEqual(calls, [])
        self.assertEqual(by_domain(rows)[16], ("XBAR2CLK", "likely", 2))  # the handed payload, not the getter's


def python_rows(words):
    """The rows of nvbackend's own loop, unnamed: read_clock_domains with the
    classifier stubbed out, so the pure comparison starts from Python's rows."""
    gpu = NV.GPU.__new__(NV.GPU)
    gpu._lock = threading.RLock()
    gpu.clkdom_is_blackwell = lambda: False
    gpu.arch = lambda: None
    pc = NV._AllClocksPriv()
    for k, word in enumerate(words):
        pc.w[k] = word
    original = NV.classify_domain_names
    NV.classify_domain_names = lambda rows, *a, **kw: rows
    try:
        rows, err = gpu.read_clock_domains(pc=pc)
    finally:
        NV.classify_domain_names = original
    assert err is None
    return rows


class ClassifyTests(unittest.TestCase):
    CASES = [
        (TURING, 1935, 7000, 6), (TURING, None, None, 6), (PASCAL, 1898, 5005, 4), (LEGACY, 1006, 3004, 2),
        (LEGACY, 1006, 3004, 3), (TURING, 1935, 7000, 7), (TURING, 1935, 7000, 8), (BLACKWELL, 180, 405, 10),
        (TURING, 1935, 7000, None), (PASCAL, 1898, 5005, None), (TURING, None, 7000, None),
        (TURING, 0, 0, None), (PASCAL, 1898, 5005, 9),  # an architecture outside every table
    ]

    def test_pure_classifier_matches(self):
        for words, core, mem, arch in self.CASES:
            with self.subTest(core=core, mem=mem, arch=arch):
                expected = NV.classify_domain_names(python_rows(words), core, mem,
                                                    blackwell=arch == 10, architecture=arch)
                actual = native.classify_clock_domains(words, core, mem, arch)
                self.assertEqual(actual, expected)
                for e, a in zip(expected, actual):
                    self.assertEqual({k: type(v) for k, v in a.items()}, {k: type(v) for k, v in e.items()})

    def test_payload_geometry_is_checked(self):
        with self.assertRaises(ValueError):
            native.classify_clock_domains(TURING[:-1])
