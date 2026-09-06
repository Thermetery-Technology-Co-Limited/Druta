"""Hardware-free fan regressions for the 472.12 Pascal/Turing interfaces."""
import copy
import ctypes
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import profiles
from nvbackend import (_CoolerSettings, _CoolerLevels, _FanCoolersControl,
                       _FanCoolersStatus, u32)
from test_nvml_legacy import gpu_with, output


def copy_to(ptr, value):
    ctypes.memmove(ptr, ctypes.byref(value), ctypes.sizeof(value))
    return 0


class ClassicFans:
    """Xp: SetCoolerLevels always enters manual; only Restore returns Auto."""
    ok = True
    gpu = object()

    @staticmethod
    def ver(struct, version):
        return ctypes.sizeof(struct) | version << 16

    def __init__(self):
        self.packet = _CoolerSettings(version=self.ver(_CoolerSettings, 1), count=1)
        row = self.packet.entries[0]
        row.current_min, row.current_max = 23, 100
        row.current_level, row.current_policy, row.default_policy = 23, 16, 16
        self.CoolerSettings = Mock(side_effect=lambda gpu, target, ptr: copy_to(ptr, self.packet))
        self.CoolerLevelsSet = Mock(side_effect=self.set_level)
        self.CoolerRestore = Mock(side_effect=self.restore)
        self.TachReading = output(1100)

    def set_level(self, gpu, index, ptr, count):
        assert count == 1
        request = ctypes.cast(ptr, ctypes.POINTER(_CoolerLevels)).contents.entries[0]
        self.packet.entries[index].current_level = request.level
        self.packet.entries[index].current_policy = 1
        return 0

    def restore(self, gpu, indexes, count):
        for index in range(count):
            row = self.packet.entries[indexes[index]]
            row.current_policy = row.default_policy
            row.current_level = 23
        return 0


class ClientFans:
    """RTX: two nonzero fan IDs and opaque fields shared by getter/setter."""
    ok = True
    gpu = object()
    ver = staticmethod(ClassicFans.ver)

    def __init__(self):
        self.packet = _FanCoolersControl(version=self.ver(_FanCoolersControl, 1), count=2)
        self.status = _FanCoolersStatus(version=self.ver(_FanCoolersStatus, 1), count=2)
        self.packet.unknown = 0x13579
        self.packet.reserved[3] = 0x24680
        for index in range(2):
            row, status = self.packet.entries[index], self.status.entries[index]
            row.id = status.id = index + 1
            row.reserved[4] = 0x5555 + index
            status.min, status.max, status.rpm = 41, 100, 1000 + index * 300
        self.CoolerSettings = Mock(return_value=-104)
        self.FanCoolersControl = Mock(side_effect=lambda gpu, ptr: copy_to(ptr, self.packet))
        self.FanCoolersStatus = Mock(side_effect=lambda gpu, ptr: copy_to(ptr, self.status))
        self.FanCoolersSetControl = Mock(side_effect=self.set_control)

    def set_control(self, gpu, ptr):
        copy_to(ctypes.byref(self.packet), ctypes.cast(ptr, ctypes.POINTER(_FanCoolersControl)).contents)
        return 0


def with_native(native, **exports):
    gpu = gpu_with(**exports)
    gpu.nvapi = native
    return gpu


class FanFallbackTests(unittest.TestCase):
    def test_classic_manual_and_auto_restore_original_policy(self):
        api = ClassicFans()
        gpu = with_native(api)
        baseline = gpu.read_fan_control_state()
        self.assertFalse(gpu.read_fan_manual())
        self.assertEqual(gpu.fan_capabilities(),
                         dict(manual=True, auto=True, source="nvapi_cooler", min=23, max=100))
        self.assertTrue(gpu.set_fan(35)[0])
        self.assertTrue(gpu.read_fan_manual())
        self.assertEqual(gpu.read_fan_control_state()["fans"][0]["level"], 35)
        self.assertTrue(gpu.restore_fan_control_state(baseline)[0])
        self.assertEqual(gpu.read_fan_control_state(), baseline)
        self.assertEqual(api.CoolerLevelsSet.call_count, 1)
        self.assertEqual(api.CoolerRestore.call_count, 1)
        self.assertEqual(api.CoolerRestore.call_args.args[1][0], 0)

    def test_missing_modern_fan_count_uses_known_native_layout(self):
        modern = Mock()
        api = ClassicFans()
        gpu = with_native(api, nvmlDeviceSetFanSpeed_v2=modern)
        self.assertTrue(gpu.set_fan(35)[0])
        modern.assert_not_called()
        api.CoolerLevelsSet.assert_called_once()

    def test_classic_manual_and_auto_capabilities_are_independent(self):
        api = ClassicFans()
        api.CoolerLevelsSet = None
        gpu = with_native(api)
        self.assertFalse(gpu.fan_capabilities()["manual"])
        self.assertTrue(gpu.fan_capabilities()["auto"])
        self.assertTrue(gpu.reset_fan()[0])
        api.CoolerLevelsSet = Mock(side_effect=api.set_level)
        api.CoolerRestore = None
        self.assertTrue(gpu.set_fan(35)[0])
        self.assertFalse(gpu.fan_capabilities()["auto"])

    def test_modern_readers_without_writers_still_allow_native_restoration(self):
        api = ClientFans()
        gpu = with_native(api, nvmlDeviceGetNumFans=output(2),
                          nvmlDeviceGetFanControlPolicy_v2=output(0),
                          nvmlDeviceGetTargetFanSpeed=output(0))
        state = gpu.read_fan_control_state()
        self.assertEqual(state["source"], "nvml")
        self.assertEqual([row["id"] for row in state["fans"]], [0, 1])
        for row in state["fans"]:
            row.update(level=50, manual=True, policy=1)
        self.assertTrue(gpu.restore_fan_control_state(state)[0])
        self.assertEqual([(row.id, row.level, row.mode) for row in api.packet.entries[:2]],
                         [(1, 50, 1), (2, 50, 1)])

    def test_client_round_trip_preserves_ids_opaque_data_and_individual_levels(self):
        api = ClientFans()
        gpu = with_native(api)
        original_bytes = bytes(api.packet)
        baseline = gpu.read_fan_control_state()
        wanted = copy.deepcopy(baseline)
        for row, level in zip(wanted["fans"], (50, 65)):
            row.update(level=level, manual=True, policy=1)
        self.assertTrue(gpu.restore_fan_control_state(wanted)[0])
        self.assertEqual(gpu.read_fan_control_state(), wanted)
        self.assertEqual(api.packet.unknown, 0x13579)
        self.assertEqual(api.packet.reserved[3], 0x24680)
        self.assertEqual(api.packet.entries[1].reserved[4], 0x5556)
        self.assertTrue(gpu.restore_fan_control_state(baseline)[0])
        self.assertEqual(bytes(api.packet), original_bytes)

    def test_mixed_per_fan_policy_is_preserved_and_reported_mixed(self):
        api = ClientFans()
        gpu = with_native(api)
        wanted = gpu.read_fan_control_state()
        wanted["fans"][1].update(level=55, manual=True, policy=1)
        self.assertTrue(gpu.restore_fan_control_state(wanted)[0])
        self.assertIsNone(gpu.read_fan_manual())
        self.assertEqual(gpu.read_fan_control_state(), wanted)

    def test_all_fan_requests_are_validated_before_any_write(self):
        for field, value in (("level", 35), ("level", float("nan")),
                             ("level", 500), ("manual", None), ("id", 77)):
            with self.subTest(field=field, value=value):
                api = ClientFans()
                gpu = with_native(api)
                state = gpu.read_fan_control_state()
                for row in state["fans"]:
                    row.update(level=50, manual=True, policy=1)
                state["fans"][1][field] = value
                self.assertFalse(gpu.restore_fan_control_state(state)[0])
                api.FanCoolersSetControl.assert_not_called()

    def test_nondefault_classic_auto_policy_is_not_silently_replaced(self):
        api = ClassicFans()
        gpu = with_native(api)
        state = gpu.read_fan_control_state()
        state["fans"][0]["policy"] = 4
        self.assertFalse(gpu.restore_fan_control_state(state)[0])
        api.CoolerRestore.assert_not_called()
        api.CoolerLevelsSet.assert_not_called()

    def test_bad_client_identity_does_not_enable_control(self):
        for change in ("duplicate", "missing"):
            with self.subTest(change=change):
                api = ClientFans()
                api.status.entries[1].id = 1 if change == "duplicate" else 9
                gpu = with_native(api)
                self.assertIsNone(gpu.read_fan_control_state())
                self.assertFalse(gpu.fan_capabilities()["manual"])
                self.assertFalse(gpu.set_fan(50)[0])
                api.FanCoolersSetControl.assert_not_called()

    def test_accepted_write_without_readback_is_failure(self):
        api = ClientFans()
        api.FanCoolersSetControl = Mock(return_value=0)
        gpu = with_native(api)
        with patch("nvbackend.time.sleep"):
            ok, message = gpu.set_fan(50)
        self.assertFalse(ok)
        self.assertIn("did not read back", message)

    def test_native_count_and_rpm_preserve_nvml_measured_duty(self):
        def speed(dev, index, ptr):
            ctypes.cast(ptr, ctypes.POINTER(u32))[0] = (43, 48)[index]
            return 0
        api = ClientFans()
        api.packet.entries[0].level = 50
        api.packet.entries[1].level = 65
        gpu = with_native(api, nvmlDeviceGetFanSpeed_v2=Mock(side_effect=speed))
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"num_fans": 2, "fans": [(43, 1000), (48, 1300)]})
        self.assertEqual([row["level"] for row in gpu.read_fan_control_state()["fans"]], [50, 65])

    def test_classic_telemetry_still_works_without_nvml(self):
        gpu = with_native(ClassicFans())
        gpu.nvml.ok = False
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"num_fans": 1, "fans": [(23, 1100)]})

    def test_modern_nvml_captures_target_instead_of_ramping_speed(self):
        gpu = gpu_with(nvmlDeviceGetNumFans=output(1),
                       nvmlDeviceGetFanControlPolicy_v2=output(1),
                       nvmlDeviceGetTargetFanSpeed=output(65),
                       nvmlDeviceGetFanSpeed_v2=output(42))
        self.assertEqual(gpu.read_fan_control_state()["fans"][0]["level"], 65)
        self.assertTrue(gpu.read_fan_manual())

    def test_fanless_device_does_not_advertise_writes(self):
        gpu = gpu_with(nvmlDeviceGetNumFans=output(0),
                       nvmlDeviceSetFanSpeed_v2=Mock(),
                       nvmlDeviceSetDefaultFanSpeed_v2=Mock())
        self.assertFalse(gpu.fan_capabilities()["manual"])
        self.assertFalse(gpu.fan_capabilities()["auto"])


class FanProfileTests(unittest.TestCase):
    def test_snapshot_and_restore_use_exact_per_fan_requested_state(self):
        gpu = with_native(ClientFans())
        gpu.nvapi.packet.entries[1].level = 65
        gpu.nvapi.packet.entries[1].mode = 1
        gpu.read = Mock(return_value={"fans": [(0, 0), (43, 1300)]})
        gpu.read_vf_curve = Mock(return_value=([{"idx": 0, "delta_khz": 0}], None))
        gpu.read_voltage_boost = Mock(return_value=None)
        gpu.mem_offset_scale = Mock(return_value=(1, "test"))
        state = json.loads(json.dumps(profiles.capture(gpu)))
        self.assertIsNone(state["fan_manual"])
        self.assertEqual([row["level"] for row in state["fan_control_state"]["fans"]], [0, 65])
        self.assertTrue(gpu.set_fan(80)[0])
        self.assertEqual(profiles.restore(gpu, state, apply_curve=False),
                         [(True, "fan control state restored")])
        self.assertEqual(gpu.read_fan_control_state(), state["fan_control_state"])

    def test_old_profile_below_minimum_measured_duty_keeps_auto_fallback(self):
        gpu = SimpleNamespace(static={"fan_min": 41},
                              reset_fan=Mock(return_value=(True, "auto")), set_fan=Mock())
        results = profiles.restore(gpu, {"fan_manual": True, "fan_pct": 0}, apply_curve=False)
        self.assertEqual(results, [(True, "auto")])
        gpu.set_fan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
