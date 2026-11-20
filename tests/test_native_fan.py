# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native fan writers against their Python twins.

One set of fake NVML/NVAPI callbacks, with mutable fan state and a call log,
serves nvbackend.GPU and the native core in turn. For every entry point the
outcome text, the sequence of driver calls, the resulting fan state and the
readback waits must be identical."""
import ctypes
import unittest
from unittest.mock import patch

from druta import native, nvbackend

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend

from tests.fake_driver import ERROR_TEXT, FanRig  # noqa: E402, F401


class FanParityTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)
        native.set_sleep_hook(None)

    def both(self, rig, action):
        """action(gpu) on the Python twin, then on the core, from the same fake state.

        Returns (result, driver calls, fan state afterwards, waits in ms) and
        asserts the two runs agree on all four."""
        runs = []
        for kind in ("python", "native"):
            rig.reset()
            waits = []
            if kind == "python":
                gpu = rig.python_gpu()
                with patch.object(NV.time, "sleep", lambda seconds: waits.append(round(seconds * 1000))):
                    result = action(gpu)
            else:
                gpu = rig.native_gpu()
                native.set_sleep_hook(lambda ms: waits.append(ms))
                try:
                    result = action(gpu)
                finally:
                    native.set_sleep_hook(None)
                    gpu.close()
            runs.append((result, list(rig.calls), rig.state(), waits))
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]

    # ---- readers ------------------------------------------------------------------------------------

    def test_state_from_nvml_policy_getters(self):
        rig = FanRig()
        state, *_ = self.both(rig, lambda g: g.read_fan_control_state())
        self.assertEqual(state["source"], "nvml")
        self.assertEqual([f["id"] for f in state["fans"]], [0, 1])
        self.assertEqual(state["fans"][0], {"id": 0, "level": 41, "policy": 0, "default_policy": 0,
                                            "manual": False, "min": 25, "max": 100})
        manual, *_ = self.both(rig, lambda g: g.read_fan_manual())
        self.assertIs(manual, False)

    def test_state_falls_back_to_client_coolers_without_policy_getters(self):
        rig = FanRig(nvml_policy=False)
        state, *_ = self.both(rig, lambda g: g.read_fan_control_state())
        self.assertEqual(state["source"], "nvapi_client")
        self.assertEqual([f["id"] for f in state["fans"]], [5, 7])
        self.assertEqual(state["fans"][1]["rpm"], 1700)

    def test_state_from_the_classic_cooler_with_tach(self):
        rig = FanRig(nvml_policy=False, cooler="classic")
        state, *_ = self.both(rig, lambda g: g.read_fan_control_state())
        self.assertEqual(state, {"source": "nvapi_cooler", "fans": [
            {"id": 0, "level": 44, "policy": 16, "default_policy": 16, "manual": False, "min": 30, "max": 100,
             "rpm": 1480}]})

    def test_mixed_policies_read_as_unknown_manual(self):
        rig = FanRig()
        original = rig.reset

        def mixed():
            original()
            rig.nvml_fans[1]["policy"] = 1
        rig.reset = mixed
        manual, *_ = self.both(rig, lambda g: g.read_fan_manual())
        self.assertIsNone(manual)

    def test_no_fan_interface_at_all(self):
        rig = FanRig(nvml_policy=False, cooler="none", with_nvapi=False)
        state, *_ = self.both(rig, lambda g: g.read_fan_control_state())
        self.assertIsNone(state)
        manual, *_ = self.both(rig, lambda g: g.read_fan_manual())
        self.assertIsNone(manual)

    def test_capabilities(self):
        caps, *_ = self.both(FanRig(), lambda g: g.fan_capabilities())
        self.assertEqual(caps, {"manual": True, "auto": True, "source": "nvml", "min": 30, "max": 100})
        caps, *_ = self.both(FanRig(nvml_setters=False), lambda g: g.fan_capabilities())
        self.assertEqual(caps, {"manual": True, "auto": True, "source": "nvapi_client", "min": 30, "max": 100})
        caps, *_ = self.both(FanRig(nvml_setters=False, cooler="none"), lambda g: g.fan_capabilities())
        self.assertEqual(caps, {"manual": False, "auto": False, "source": "nvml", "min": 25, "max": 100})
        caps, *_ = self.both(FanRig(nvml_setters=False, cooler="classic"), lambda g: g.fan_capabilities())
        self.assertEqual(caps, {"manual": True, "auto": True, "source": "nvapi_cooler", "min": 30, "max": 100})

    # ---- set_fan / reset_fan through NVML ------------------------------------------------------------

    def test_set_fan_through_nvml(self):
        rig = FanRig()
        result, calls, (fans, _, _), waits = self.both(rig, lambda g: g.set_fan(60))
        self.assertEqual(result, (True, "fans set to manual 60%"))
        self.assertEqual(calls, [("nvmlDeviceSetFanSpeed_v2", 0, 60), ("nvmlDeviceSetFanSpeed_v2", 1, 60)])
        self.assertEqual(fans, {0: {"policy": 1, "level": 60}, 1: {"policy": 1, "level": 60}})
        self.assertEqual(waits, [])

    def test_set_fan_below_the_floor_is_refused_before_any_write(self):
        result, calls, *_ = self.both(FanRig(), lambda g: g.set_fan(10))
        self.assertEqual(result, (False, "fan 10% below hardware minimum 25% - use Auto for the zero-RPM idle curve"))
        self.assertEqual(calls, [])

    def test_set_fan_clamps_above_100(self):
        result, calls, *_ = self.both(FanRig(), lambda g: g.set_fan(140))
        self.assertEqual(result, (True, "fans set to manual 100%"))
        self.assertEqual(calls[0], ("nvmlDeviceSetFanSpeed_v2", 0, 100))

    def test_set_fan_nvml_error_text(self):
        result, *_ = self.both(FanRig(set_status=4), lambda g: g.set_fan(60))
        self.assertEqual(result, (False, "fan0:Insufficient Permissions; fan1:Insufficient Permissions (needs admin)"))
        result, *_ = self.both(FanRig(set_status=4, error_string=False), lambda g: g.set_fan(60))
        self.assertEqual(result, (False, "fan0:4; fan1:4 (needs admin)"))

    def test_reset_fan_through_nvml(self):
        rig = FanRig()
        original = rig.reset

        def manual():
            original()
            for fan in rig.nvml_fans.values():
                fan.update(policy=1, level=70)
        rig.reset = manual
        result, calls, (fans, _, _), _ = self.both(rig, lambda g: g.reset_fan())
        self.assertEqual(result, (True, "fans returned to automatic"))
        self.assertEqual(calls, [("nvmlDeviceSetDefaultFanSpeed_v2", 0), ("nvmlDeviceSetDefaultFanSpeed_v2", 1)])
        self.assertEqual([f["policy"] for f in fans.values()], [0, 0])
        result, *_ = self.both(FanRig(set_status=3), lambda g: g.reset_fan())
        self.assertEqual(result, (False, "fan0:Not Supported; fan1:Not Supported"))

    def test_fan_count_unavailable(self):
        rig = FanRig(cooler="none")
        rig.nvml.exports["nvmlDeviceGetNumFans"] = CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, p: 3)
        result, calls, *_ = self.both(rig, lambda g: g.set_fan(60))
        self.assertEqual(result, (False, "fan count unavailable; cannot select fans to control"))
        result, calls, *_ = self.both(rig, lambda g: g.reset_fan())
        self.assertEqual(result, (False, "fan count unavailable; cannot select fans to control"))

    # ---- set_fan / reset_fan through the NVAPI coolers -----------------------------------------------

    def test_set_fan_through_client_coolers(self):
        rig = FanRig(nvml_setters=False)
        result, calls, (_, _, client), waits = self.both(rig, lambda g: g.set_fan(65))
        self.assertEqual(result, (True, "fans set to manual 65%"))
        version = NV.NvAPI.ver(NV._FanCoolersControl, 1)
        self.assertEqual(calls, [("FanCoolersSetControl", version, 2,
                                  ((5, 65, 1, 0x5A5A0005), (7, 65, 1, 0x5A5A0007)))])
        self.assertEqual(client, {5: {"level": 65, "mode": 1}, 7: {"level": 65, "mode": 1}})
        self.assertEqual(waits, [])

    def test_reset_fan_through_client_coolers(self):
        rig = FanRig(nvml_setters=False)
        original = rig.reset

        def manual():
            original()
            for fan in rig.client.values():
                fan.update(mode=1, level=80)
        rig.reset = manual
        result, calls, (_, _, client), _ = self.both(rig, lambda g: g.reset_fan())
        self.assertEqual(result, (True, "fans returned to automatic"))
        self.assertEqual(calls[0][3], ((5, 0, 0, 0x5A5A0005), (7, 0, 0, 0x5A5A0007)))
        self.assertEqual(client, {5: {"level": 0, "mode": 0}, 7: {"level": 0, "mode": 0}})

    def test_set_and_reset_through_the_classic_cooler(self):
        rig = FanRig(nvml_setters=False, cooler="classic")
        result, calls, (_, classic, _), _ = self.both(rig, lambda g: g.set_fan(70))
        self.assertEqual(result, (True, "fans set to manual 70%"))
        self.assertEqual(calls, [("CoolerLevelsSet", 0, NV.NvAPI.ver(NV._CoolerLevels, 1), 1, 70, 1)])
        self.assertEqual(classic, {"level": 70, "policy": 1, "default": 16})
        original = rig.reset

        def manual():
            original()
            rig.classic.update(level=70, policy=1)
        rig.reset = manual
        result, calls, (_, classic, _), _ = self.both(rig, lambda g: g.reset_fan())
        self.assertEqual(result, (True, "fans returned to automatic"))
        self.assertEqual(calls, [("CoolerRestore", (0,))])
        self.assertEqual(classic, {"level": 70, "policy": 16, "default": 16})

    def test_nvapi_set_failure_text(self):
        result, *_ = self.both(FanRig(nvml_setters=False, set_status=-104), lambda g: g.set_fan(65))
        self.assertEqual(result, (False, "NVAPI client fan status -104"))
        result, *_ = self.both(FanRig(nvml_setters=False, cooler="classic", set_status=-6), lambda g: g.set_fan(65))
        self.assertEqual(result, (False, "fan0:NVAPI status -6"))
        result, *_ = self.both(FanRig(nvml_setters=False, cooler="none"), lambda g: g.set_fan(65))
        self.assertEqual(result, (False, "NVAPI fan controls not available"))

    def test_nvapi_write_that_never_reads_back(self):
        rig = FanRig(nvml_setters=False, settle_after=1000)
        result, calls, _, waits = self.both(rig, lambda g: g.set_fan(65))
        self.assertEqual(result, (False, "fan write accepted but requested policy/level did not read back"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(waits, [100] * 20)

    def test_nvapi_write_that_settles_after_a_few_reads(self):
        rig = FanRig(nvml_setters=False, settle_after=3)
        result, _, (_, _, client), waits = self.both(rig, lambda g: g.set_fan(65))
        self.assertEqual(result, (True, "fans set to manual 65%"))
        self.assertEqual(client[5], {"level": 65, "mode": 1})
        self.assertEqual(waits, [100, 100])

    # ---- restore_fan_control_state ------------------------------------------------------------------

    def nvml_snapshot(self, **changes):
        fans = [{"id": 0, "level": 55, "policy": 1, "default_policy": 0, "manual": True, "min": 25, "max": 100},
                {"id": 1, "level": 41, "policy": 0, "default_policy": 0, "manual": False, "min": 25, "max": 100}]
        snapshot = {"source": "nvml", "fans": fans}
        snapshot.update(changes)
        return snapshot

    def test_restore_through_nvml(self):
        result, calls, (fans, _, _), waits = self.both(FanRig(), lambda g: g.restore_fan_control_state(self.nvml_snapshot()))
        self.assertEqual(result, (True, "fan control state restored"))
        self.assertEqual(calls, [("nvmlDeviceSetFanSpeed_v2", 0, 55), ("nvmlDeviceSetDefaultFanSpeed_v2", 1)])
        self.assertEqual(fans, {0: {"policy": 1, "level": 55}, 1: {"policy": 0, "level": 42}})
        self.assertEqual(waits, [])

    def test_restore_refusals_keep_their_text_and_order(self):
        rig = FanRig()
        cases = [
            ("not a dict", "fan control state unavailable"),
            ({"source": "nvml", "fans": "x"}, "fan count changed; refusing to restore another layout"),
            ({"source": "nvml", "fans": [{}]}, "fan count changed; refusing to restore another layout"),
            (self.nvml_snapshot(source="bogus"), "fan snapshot has an unknown control source"),
            (self.nvml_snapshot(fans=[{"id": 0, "level": 50, "manual": 1}, {"id": 1}]),
             "fan snapshot has an unknown policy"),
            (self.nvml_snapshot(fans=[{"id": 7, "level": 50, "manual": True}, {"id": 1, "manual": False}]),
             "fan IDs changed; refusing to restore another layout"),
            (self.nvml_snapshot(fans=[{"id": 0, "level": True, "manual": True}, {"id": 1, "manual": False}]),
             "fan snapshot has an invalid requested level"),
            (self.nvml_snapshot(fans=[{"id": 0, "level": 50.5, "manual": True}, {"id": 1, "manual": False}]),
             "fan snapshot has an invalid requested level"),
            (self.nvml_snapshot(fans=[{"id": 0, "level": 101, "manual": True}, {"id": 1, "manual": False}]),
             "fan snapshot has an invalid requested level"),
            (self.nvml_snapshot(fans=[{"id": 0, "level": 10, "manual": True}, {"id": 1, "manual": False}]),
             "fan 0 request outside [25..100]%"),
            (self.nvml_snapshot(fans=[{"id": 0, "level": 50, "manual": True, "policy": 1},
                                      {"id": 1, "level": 0, "manual": False, "policy": 2}]),
             "non-default automatic fan policy cannot be restored safely"),
            (self.nvml_snapshot(fans=[{"id": 0, "level": 50, "manual": True, "policy": 1},
                                      {"id": 1, "level": 0, "manual": False}]),
             "non-default automatic fan policy cannot be restored safely"),
        ]
        for snapshot, message in cases:
            with self.subTest(message=message):
                result, calls, *_ = self.both(rig, lambda g: g.restore_fan_control_state(snapshot))
                self.assertEqual(result, (False, message))
                self.assertEqual(calls, [])

    def test_restore_accepts_integer_valued_floats(self):
        snapshot = self.nvml_snapshot(fans=[{"id": 0, "level": 55.0, "manual": True, "policy": 1},
                                            {"id": 1, "level": 41, "manual": False, "policy": 0}])
        result, calls, *_ = self.both(FanRig(), lambda g: g.restore_fan_control_state(snapshot))
        self.assertEqual(result, (True, "fan control state restored"))
        self.assertEqual(calls[0], ("nvmlDeviceSetFanSpeed_v2", 0, 55))

    def test_restore_nvml_snapshot_maps_onto_client_coolers_when_setters_are_missing(self):
        # The modern getters answer but the setters do not: the complete native fallback wins,
        # fan order mapped across the interfaces (NVML 0,1 -> client ids 5,7).
        rig = FanRig(nvml_setters=False)
        snapshot = self.nvml_snapshot()
        result, calls, (_, _, client), _ = self.both(rig, lambda g: g.restore_fan_control_state(snapshot))
        self.assertEqual(result, (True, "fan control state restored"))
        self.assertEqual(calls[0][3], ((5, 55, 1, 0x5A5A0005), (7, 41, 0, 0x5A5A0007)))
        self.assertEqual(client, {5: {"level": 55, "mode": 1}, 7: {"level": 41, "mode": 0}})

    def test_restore_client_snapshot_on_client_coolers(self):
        rig = FanRig(nvml_policy=False, nvml_setters=False)
        snapshot = {"source": "nvapi_client", "fans": [
            {"id": 5, "level": 70, "policy": 1, "default_policy": 0, "manual": True, "min": 30, "max": 100},
            {"id": 7, "level": 0, "policy": 0, "default_policy": 0, "manual": False, "min": 30, "max": 100}]}
        result, calls, (_, _, client), _ = self.both(rig, lambda g: g.restore_fan_control_state(snapshot))
        self.assertEqual(result, (True, "fan control state restored"))
        self.assertEqual(client, {5: {"level": 70, "mode": 1}, 7: {"level": 0, "mode": 0}})
        wrong_ids = {"source": "nvapi_client", "fans": [dict(snapshot["fans"][0], id=6), snapshot["fans"][1]]}
        result, calls, *_ = self.both(rig, lambda g: g.restore_fan_control_state(wrong_ids))
        self.assertEqual(result, (False, "fan IDs changed; refusing to restore another layout"))

    def test_restore_classic_snapshot(self):
        rig = FanRig(nvml_policy=False, nvml_setters=False, cooler="classic")
        snapshot = {"source": "nvapi_cooler", "fans": [
            {"id": 0, "level": 66, "policy": 1, "default_policy": 16, "manual": True, "min": 30, "max": 100}]}
        result, calls, (_, classic, _), _ = self.both(rig, lambda g: g.restore_fan_control_state(snapshot))
        self.assertEqual(result, (True, "fan control state restored"))
        self.assertEqual(calls, [("CoolerLevelsSet", 0, NV.NvAPI.ver(NV._CoolerLevels, 1), 1, 66, 1)])
        self.assertEqual(classic, {"level": 66, "policy": 1, "default": 16})
        automatic = {"source": "nvapi_cooler", "fans": [dict(snapshot["fans"][0], manual=False, policy=16)]}
        result, calls, *_ = self.both(rig, lambda g: g.restore_fan_control_state(automatic))
        self.assertEqual(result, (True, "fan control state restored"))
        self.assertEqual(calls, [("CoolerRestore", (0,))])

    def test_restore_with_missing_nvml_setter_and_no_fallback(self):
        rig = FanRig(nvml_setters=False, cooler="none")
        result, calls, *_ = self.both(rig, lambda g: g.restore_fan_control_state(self.nvml_snapshot()))
        self.assertEqual(result, (False, "nvmlDeviceSetFanSpeed_v2 not available"))
        self.assertEqual(calls, [])

    def test_restore_rejects_a_snapshot_of_another_layout(self):
        big = {"source": "nvml", "fans": [{"id": k, "level": 50, "manual": True} for k in range(40)]}
        result, calls, *_ = self.both(FanRig(), lambda g: g.restore_fan_control_state(big))
        self.assertEqual(result, (False, "fan count changed; refusing to restore another layout"))


if __name__ == "__main__":
    unittest.main()
