# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native V/F point lock, frequency-lock readers and voltage boost against
their Python twins.

One mutable fake lock table and voltage-control block serve nvbackend.GPU and
the native core in turn. Every entry point must produce the same result, the
same setter buffers (every byte the driver wrote, not just the changed ones)
and the same state afterwards."""
import ctypes
import unittest

from druta import native, nvbackend

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend

from tests.fake_driver import BOOST_LOCK, VF_LOCK_SET, VOLT_CTRL_GET, VOLT_CTRL_SET, LockRig, entry  # noqa: E402, F401


class ParityCase(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, rig, action):
        """action(gpu) on the Python twin, then on the core, from the same fake state."""
        runs = []
        for kind in ("python", "native"):
            rig.reset()
            if kind == "python":
                gpu = rig.python_gpu()
                result = action(gpu)
            else:
                gpu = rig.native_gpu()
                try:
                    # GPU.static is read once at construction in Python and the twin carries it
                    # pre-filled; read it on the native handle before the action so the call
                    # logs compare only what the action did.
                    gpu.capabilities()
                    rig.calls.clear()
                    result = action(gpu)
                finally:
                    gpu.close()
            runs.append((result, list(rig.calls), rig.state()))
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]


class VoltageBoostTests(ParityCase):
    def test_read_and_set(self):
        rig = LockRig()
        value, *_ = self.both(rig, lambda g: g.read_voltage_boost())
        self.assertEqual(value, 25)
        result, calls, (_, vboost) = self.both(rig, lambda g: g.set_voltage_boost(40))
        self.assertEqual(result, (True, "core voltage boost set to 40% (ceiling unlock)"))
        self.assertEqual(calls, [("VoltCtrlSet", NV.NvAPI.ver(NV._VoltBoost, 1), 40,
                                  tuple(0x5A000000 | k for k in range(8)))])  # reserved words preserved
        self.assertEqual(vboost["percent"], 40)

    def test_refusals(self):
        result, calls, _ = self.both(LockRig(), lambda g: g.set_voltage_boost(101))
        self.assertEqual(result, (False, "voltage boost 101% out of [0..100]"))
        self.assertEqual(calls, [])
        result, *_ = self.both(LockRig(with_volt_set=False), lambda g: g.set_voltage_boost(40))
        self.assertEqual(result, (False, "voltage-control APIs unavailable"))
        result, *_ = self.both(LockRig(volt_get_status=-1), lambda g: g.set_voltage_boost(40))
        self.assertEqual(result, (False, "voltage GetControl failed (cannot read-modify-write)"))
        result, *_ = self.both(LockRig(volt_set_status=-6), lambda g: g.set_voltage_boost(40))
        self.assertEqual(result, (False, "voltage set failed (status -6) - needs admin"))
        value, *_ = self.both(LockRig(volt_get_status=-1), lambda g: g.read_voltage_boost())
        self.assertIsNone(value)


class VfLockReaderTests(ParityCase):
    def test_unlocked_table(self):
        rig = LockRig()
        self.assertEqual(self.both(rig, lambda g: g.read_vf_lock_status())[0], (None, None))
        self.assertIsNone(self.both(rig, lambda g: g.read_vf_lock())[0])
        self.assertIsNone(self.both(rig, lambda g: g.read_clk_lock())[0])
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock_status())[0], (None, None))

    def test_point_lock_and_domain_filter(self):
        rig = LockRig()
        original = rig.reset

        def locked():
            original()
            rig.table[6] = entry(6, 3, 900000)
        rig.reset = locked
        status, *_ = self.both(rig, lambda g: g.read_vf_lock_status())
        self.assertEqual(status, ({"domain": 6, "lockMode": 3, "volt_uV": 900000, "volt_mv": 900.0, "count": 7}, None))
        self.assertEqual(self.both(rig, lambda g: g.read_vf_lock_status(domain=6))[0], status)
        self.assertEqual(self.both(rig, lambda g: g.read_vf_lock_status(domain=2))[0], (None, None))
        self.assertEqual(self.both(rig, lambda g: g.read_vf_lock_status(domain=9))[0],
                         (None, "V/F lock domain 9 is missing; its current state is unknown"))
        self.assertEqual(self.both(rig, lambda g: g.read_vf_lock())[0], status[0])

    def test_getter_failure_is_unknown_not_unlocked(self):
        rig = LockRig()
        original = rig.reset

        def failing():
            original()
            rig.get_status = -1
        rig.reset = failing
        self.assertEqual(self.both(rig, lambda g: g.read_vf_lock_status())[0],
                         (None, "V/F lock getter failed; the current lock is unknown"))
        self.assertIsNone(self.both(rig, lambda g: g.read_clk_lock())[0])
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock_status())[0],
                         (None, "frequency-lock getter failed; current ownership is unknown"))

    def test_kepler_has_no_vf_table(self):
        rig = LockRig(arch=2)
        self.assertEqual(self.both(rig, lambda g: g.read_vf_lock_status())[0],
                         (None, "V/F lock getter failed; the current lock is unknown"))
        self.assertEqual(self.both(rig, lambda g: g.set_vf_lock(900000))[0],
                         (False, "V/F point lock unavailable: 0xE440B867 / 0x39442CFB did not both resolve"))

    def test_frequency_lock_records(self):
        rig = LockRig()
        original = rig.reset

        def locked():
            original()
            rig.table[0] = entry(0, 2, 1500000)
            rig.table[1] = entry(1, 2, 300000)
        rig.reset = locked
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock())[0], (300, 1500))
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock_status())[0], ((300, 1500), None))
        # a point lock elsewhere does not disturb the frequency readers, and vice versa
        rig.table[6] = entry(6, 3, 900000)
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock())[0], (300, 1500))

        def max_only():
            original()
            rig.table[0] = entry(0, 2, 1500000)
        rig.reset = max_only
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock())[0], (1500, 1500))
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock_status())[0],
                         (None, "frequency-lock domains have an unsupported or conflicting mode"))

    def test_frequency_lock_status_refusals(self):
        rig = LockRig()
        original = rig.reset
        cases = []

        def bad_units():
            original()
            rig.table[0] = entry(0, 2, 1500500)
            rig.table[1] = entry(1, 2, 300000)
        cases.append((bad_units, (None, "frequency-lock range has invalid units or bounds")))

        def inverted():
            original()
            rig.table[0] = entry(0, 2, 300000)
            rig.table[1] = entry(1, 2, 1500000)
        cases.append((inverted, (None, "frequency-lock range has invalid units or bounds")))

        def zero_count():
            original()
            rig.count = 0
        cases.append((zero_count, (None, "frequency-lock table has an invalid count")))

        def repeated():
            original()
            rig.table[2] = entry(0)
        cases.append((repeated, (None, "frequency-lock table repeats a domain")))

        def missing():
            original()
            rig.table = [entry(d) for d in (0, 2, 3)]
        cases.append((missing, (None, "frequency-lock domains are missing; current ownership is unknown")))
        for setup, expected in cases:
            rig.reset = setup
            with self.subTest(expected=expected[1]):
                self.assertEqual(self.both(rig, lambda g: g.read_clk_lock_status())[0], expected)

    def test_turing_without_the_legacy_records_is_ambiguous(self):
        # Mode-0 records on Turing need the RM legacy read to prove nobody holds
        # the lock; neither side can read them here, so ownership stays unknown.
        rig = LockRig(arch=6)
        self.assertEqual(self.both(rig, lambda g: g.read_clk_lock_status())[0],
                         (None, "legacy frequency-lock state is unreadable or ambiguous"))


class VfLockWriterTests(ParityCase):
    def test_set_lock_on_the_default_domain(self):
        rig = LockRig()
        result, calls, (table, _) = self.both(rig, lambda g: (g.set_vf_lock(900000), g.vf_lock_recovery_pending()))
        self.assertEqual(result, ((True, "V/F point lock set on domain 6, requested 900.00 mV - the hardware holds "
                                         "the highest V/F point at or below that"), False))
        writes = [c for c in calls if c[0] == "VfLockSet"]
        self.assertEqual(len(writes), 1)
        _, version, flags, count, rows = writes[0]
        self.assertEqual((version, flags, count), (NV.NvAPI.ver(NV._ClockLock, 2), 0xF00D, 7))
        self.assertEqual(rows[6], entry(6, 3, 900000))
        self.assertEqual(rows[:6], [entry(d) for d in range(6)])  # every other byte as the driver wrote it
        self.assertEqual(table[6], entry(6, 3, 900000))

    def test_set_lock_moves_an_existing_lock_and_takes_an_explicit_domain(self):
        rig = LockRig()
        original = rig.reset

        def held():
            original()
            rig.table[3] = entry(3, 3, 850000)
        rig.reset = held
        result, calls, (table, _) = self.both(rig, lambda g: g.set_vf_lock(893750))
        self.assertEqual(result, (True, "V/F point lock set on domain 3, requested 893.75 mV - the hardware holds "
                                        "the highest V/F point at or below that"))
        self.assertEqual(table[3], entry(3, 3, 893750))
        result, _, (table, _) = self.both(rig, lambda g: g.set_vf_lock(893750, domain=5))
        self.assertEqual(result[0], True)
        self.assertEqual((table[3], table[5]), (entry(3, 3, 850000), entry(5, 3, 893750)))
        result, calls, _ = self.both(rig, lambda g: g.set_vf_lock(893750, domain=9))
        self.assertEqual(result, (False, "V/F lock: domain 9 is not in the driver's lock table "
                                         "(it lists [0, 1, 2, 3, 4, 5, 6])"))
        self.assertEqual([c for c in calls if c[0] == "VfLockSet"], [])

    def test_set_lock_refusals_before_any_write(self):
        rig = LockRig()
        result, calls, _ = self.both(rig, lambda g: g.set_vf_lock(2000000))
        self.assertEqual(result, (False, "V/F lock: 2000000 uV outside the sanity envelope [400000..1300000] uV "
                                         "- the argument is MICROvolts"))
        self.assertEqual(calls, [])
        result, *_ = self.both(rig, lambda g: g.set_vf_lock(900.0))
        self.assertEqual(result[1][:38], "V/F lock: 900 uV outside the sanity en")
        result, *_ = self.both(LockRig(with_setter=False), lambda g: g.set_vf_lock(900000))
        self.assertEqual(result, (False, "V/F point lock unavailable: 0xE440B867 / 0x39442CFB did not both resolve"))
        original = rig.reset

        def failing():
            original()
            rig.get_status = -1
        rig.reset = failing
        result, *_ = self.both(rig, lambda g: g.set_vf_lock(900000))
        self.assertEqual(result, (False, "V/F lock: getter failed, refusing to write blind"))

    def test_refused_write_confirms_the_previous_state(self):
        result, _, (table, _) = self.both(LockRig(set_status=-104), lambda g: (g.set_vf_lock(900000),
                                                                               g.vf_lock_recovery_pending()))
        self.assertEqual(result, ((False, "V/F lock write failed (status -104) - needs admin; "
                                          "previous V/F lock state is confirmed restored"), False))
        self.assertEqual(table[6], entry(6))

    def test_accepted_write_that_does_not_read_back(self):
        result, *_ = self.both(LockRig(reassert=True), lambda g: g.set_vf_lock(900000))
        self.assertEqual(result, (False, "V/F lock write returned OK but the requested lock was not read back; "
                                         "previous V/F lock state is confirmed restored"))

    def test_verification_failure_leaves_a_recovery_to_retry(self):
        rig = LockRig(fail_get_after_set=True)

        def scenario(g):
            first = g.set_vf_lock(900000)
            pending = g.vf_lock_recovery_pending()
            second = g.set_vf_lock(950000)
            rig.get_status = 0  # the getter answers again
            recovered = g.recover_vf_lock()
            return first, pending, second, recovered, g.vf_lock_recovery_pending(), g.recover_vf_lock()
        result, calls, (table, _) = self.both(rig, scenario)
        self.assertEqual(result, (
            (False, "V/F lock write returned OK but its verification read failed; "
                    "RECOVERY REQUIRED: V/F lock recovery getter failed; refusing to write blind"),
            True,
            (False, "V/F lock: the previous change needs recovery before another hold"),
            (True, "previous V/F lock state restored and verified"),
            False,
            (True, "no V/F lock recovery is pending")))
        writes = [c for c in calls if c[0] == "VfLockSet"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[1][4][6], entry(6))  # the rollback restores only the target entry
        self.assertEqual(table[6], entry(6))

    def test_recovery_refuses_a_target_another_writer_changed(self):
        rig = LockRig(fail_get_after_set=True)

        def scenario(g):
            first = g.set_vf_lock(900000)
            rig.get_status = 0
            rig.table[6] = entry(6, 3, 1000000)  # somebody else re-targeted it meanwhile
            return first[0], g.recover_vf_lock(), g.vf_lock_recovery_pending()
        result, *_ = self.both(rig, scenario)
        self.assertEqual(result, (False, (False, "V/F lock target changed concurrently; leaving its current request "
                                                 "untouched"), True))

    def test_clear_lock(self):
        rig = LockRig()
        self.assertEqual(self.both(rig, lambda g: g.clear_vf_lock())[0], (True, "no V/F point lock was set"))
        original = rig.reset

        def held():
            original()
            rig.table[0] = entry(0, 2, 1500000)  # the NVML frequency lock shares the table
            rig.table[1] = entry(1, 2, 300000)
            rig.table[3] = entry(3, 3, 850000)
            rig.table[6] = entry(6, 3, 900000)
        rig.reset = held
        result, calls, (table, _) = self.both(rig, lambda g: g.clear_vf_lock())
        self.assertEqual(result, (True, "V/F point lock released (domains 3, 6)"))
        rows = [c for c in calls if c[0] == "VfLockSet"][0][4]
        self.assertEqual((rows[3], rows[6]), (entry(3, 0, 850000), entry(6, 0, 900000)))  # volt_uV left as it was
        self.assertEqual((rows[0], rows[1]), (entry(0, 2, 1500000), entry(1, 2, 300000)))  # mode 2 untouched
        self.assertEqual(table, rows)
        result, _, (table, _) = self.both(rig, lambda g: g.clear_vf_lock(domain=6))
        self.assertEqual(result, (True, "V/F point lock released (domain 6)"))
        self.assertEqual((table[3], table[6]), (entry(3, 3, 850000), entry(6, 0, 900000)))
        self.assertEqual(self.both(rig, lambda g: g.clear_vf_lock(domain=6, expected_uv=900000))[0],
                         (True, "V/F point lock released (domain 6)"))
        self.assertEqual(self.both(rig, lambda g: g.clear_vf_lock(domain=6, expected_uv=875000))[0],
                         (True, "this V/F request is no longer held; the current target request was left untouched"))
        self.assertEqual(self.both(rig, lambda g: g.clear_vf_lock(domain=9))[0],
                         (False, "V/F lock domain 9 is missing; refusing to assume it was released"))

    def test_clear_lock_failures(self):
        def held(rig):
            original = rig.reset

            def setup():
                original()
                rig.table[6] = entry(6, 3, 900000)
            rig.reset = setup
            return rig
        self.assertEqual(self.both(held(LockRig(set_status=-6)), lambda g: g.clear_vf_lock())[0],
                         (False, "V/F lock release failed (status -6) - needs admin"))
        self.assertEqual(self.both(held(LockRig(reassert=True)), lambda g: g.clear_vf_lock())[0],
                         (False, "V/F lock release returned OK but the card still reports a lock - another tool "
                                 "is re-asserting it"))
        self.assertEqual(self.both(held(LockRig(fail_get_after_set=True)), lambda g: g.clear_vf_lock())[0],
                         (False, "V/F lock release was sent, but the verification read failed"))
        self.assertEqual(self.both(held(LockRig(with_setter=False)), lambda g: g.clear_vf_lock())[0],
                         (False, "V/F point lock unavailable: 0xE440B867 / 0x39442CFB did not both resolve"))

    def test_self_test(self):
        rig = LockRig()
        result, calls, (table, _) = self.both(rig, lambda g: g.vf_lock_self_test())
        self.assertEqual(result, (True, "V/F lock self-test passed: identity write accepted, state unchanged, "
                                        "7 entries, locked none"))
        self.assertEqual([c for c in calls if c[0] == "VfLockSet"][0][4], [entry(d) for d in range(7)])
        original = rig.reset

        def held():
            original()
            rig.table[0] = entry(0, 2, 1500000)
            rig.table[6] = entry(6, 3, 900000)
        rig.reset = held
        self.assertEqual(self.both(rig, lambda g: g.vf_lock_self_test())[0],
                         (True, "V/F lock self-test passed: identity write accepted, state unchanged, "
                                "7 entries, locked ['0:mode2', '6:mode3']"))
        self.assertEqual(self.both(LockRig(set_status=-6), lambda g: g.vf_lock_self_test())[0],
                         (False, "V/F lock self-test: the driver REFUSED an identity write (status -6) - do not use "
                                 "the V/F lock here"))
        self.assertEqual(self.both(LockRig(fail_get_after_set=True), lambda g: g.vf_lock_self_test())[0],
                         (False, "V/F lock self-test: identity write was accepted but the getter stopped answering"))
        changing = LockRig()
        base = changing.reset

        def drift():
            base()
            changing.get_status = 0

        changing.reset = drift
        set_fn = changing.nvapi.interfaces[VF_LOCK_SET]

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def set_and_drift(handle, pointer):
            status = set_fn(handle, pointer)
            changing.table[2] = entry(2, 3, 700000)  # another tool wrote between the reads
            return status
        changing.names["VfLockSet"] = changing.nvapi.interfaces[VF_LOCK_SET] = set_and_drift
        self.assertEqual(self.both(changing, lambda g: g.vf_lock_self_test())[0],
                         (False, "V/F lock self-test: identity write accepted but the state CHANGED - either the "
                                 "layout is wrong or another tool wrote between the reads"))


if __name__ == "__main__":
    unittest.main()
