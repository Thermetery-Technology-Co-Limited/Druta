# Druta - hardware-free R470 voltage-rail regression tests.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.nvbackend import GPU, i32, u32
from tests.test_volt_rails import fake_gpu


def legacy_gpu(kind="turing", **identity):
    gpu = fake_gpu(kind, driver="472.12", **identity)
    api = gpu.nvapi

    def control(handle, buf):
        if api.escape_hook is not None:
            return api.escape_hook(handle, buf)
        assert handle is api.gpu
        p = ctypes.cast(buf, ctypes.POINTER(u32))
        api.calls.append(("control", p[0], p[1]))
        if p[0] != 0x10AC8:
            return -9
        # This is the measured R470 behavior: success with an empty record
        # for a requested absent rail. The live getter rejects that rail.
        if p[1] != 1:
            return 0
        signed = ctypes.cast(buf, ctypes.POINTER(i32))
        for i, value in enumerate([1] + api.control[0]):
            signed[18 + i] = value
        return 0

    api.VoltRailsCtlGet = control
    return gpu


class LegacyRailReadTests(unittest.TestCase):
    def test_profile_bases_and_boost_are_specific_to_measured_r470_boards(self):
        for kind, overvoltage in (("turing", 1125), ("pascal", 1200)):
            with self.subTest(kind=kind):
                gpu = legacy_gpu(kind)
                self.assertTrue(gpu.volt_rail_limits_supported())
                fields = gpu.read_volt_rail_limits()[0]
                self.assertEqual(fields["_base_mv"], {
                    "reliability": 1068.75, "alt_reliability": 1093.75,
                    "overvoltage": overvoltage, "vmin": 650})
                self.assertEqual(fields["_headroom_mv"], 25)
                self.assertEqual(gpu._volt_rail_profile()["control_version"], 0x10AC8)
        self.assertFalse(legacy_gpu(vbios="unmeasured").volt_rail_limits_supported())

    def test_v1_success_for_an_absent_rail_does_not_expose_msvdd(self):
        gpu = legacy_gpu()
        self.assertEqual(set(gpu.read_volt_rail_limits()), {0})
        self.assertEqual(set(gpu.read_volt_rail_state()), {0})
        self.assertEqual(gpu.volt_rail_limit_fields(1), ())
        self.assertIn(("control", 0x10AC8, 2), gpu.nvapi.calls)

    def test_successful_fallback_version_is_cached_per_instance(self):
        gpu = legacy_gpu()
        self.assertIsNotNone(gpu.read_volt_rail_limits())
        self.assertIn(("control", 0x20AC8, 1), gpu.nvapi.calls)
        gpu.nvapi.calls.clear()
        self.assertIsNotNone(gpu.read_volt_rail_limits())
        self.assertTrue(all(v == 0x10AC8 for name, v, _ in gpu.nvapi.calls
                            if name == "control"))
        fresh = fake_gpu("turing")
        fresh.read_volt_rail_limits()
        self.assertTrue(all(v == 0x20AC8 for name, v, _ in fresh.nvapi.calls
                            if name == "control"))


class LegacyRailEscapeTests(unittest.TestCase):
    def run_escape(self, kind="turing", *, packet_size=716, params_size=648,
                   command=0x20803213, mask=1, callback_thread=731):
        gpu = legacy_gpu(kind)
        records = {0: [-12500, 25000, -6250, 12500]}
        payload = (u32 * 179)(*[0x55660000 + i for i in range(179)])
        payload[14], payload[15], payload[16] = command, params_size, 0
        payload[17], payload[18] = mask, 0
        escape = GPU._Escape(pPrivateDriverData=ctypes.addressof(payload),
                             PrivateDriverDataSize=packet_size)
        before = bytes(payload)
        code = ctypes.create_string_buffer(b"original bytes")
        original_code = bytes(code)
        address = ctypes.addressof(code)
        callback_buffer = ctypes.create_string_buffer(14)
        sent = []

        def real(pesc):
            self.assertEqual(bytes(code), original_code)
            self.assertEqual(pesc, ctypes.addressof(escape))
            sent.append(bytes(payload))
            return 0

        def prototype(result_type, argument_type):
            def bind(target):
                if callable(target):
                    def getter(handle, buf):
                        self.assertIs(handle, gpu.nvapi.gpu)
                        p = ctypes.cast(buf, ctypes.POINTER(u32))
                        self.assertEqual((p[0], p[1]), (0x10AC8, 1))
                        return target(ctypes.addressof(escape))
                    gpu.nvapi.escape_hook = getter
                    return ctypes.c_void_p(ctypes.addressof(callback_buffer))
                self.assertEqual(target, address)
                return real
            return bind

        kernel = SimpleNamespace(VirtualProtect=Mock(return_value=1),
                                 GetCurrentThreadId=Mock(side_effect=[731, callback_thread]))
        gdi = SimpleNamespace(D3DKMTEscape=ctypes.c_void_p(address))
        with patch("druta.nvbackend.ctypes.WinDLL", new=lambda name, **kw:
                   gdi if name == "gdi32.dll" else kernel, create=True), \
               patch("druta.nvbackend.ctypes.WINFUNCTYPE", new=prototype, create=True):
            result = GPU._write_rail_records_locked(gpu, records)
        self.assertEqual(bytes(code), original_code)
        self.assertEqual(len(sent), 1)
        return result, before, sent[0]

    def test_legacy_packet_preserves_all_unknown_fields_and_boost(self):
        for kind in ("turing", "pascal"):
            with self.subTest(kind=kind):
                result, before, after = self.run_escape(kind)
                self.assertEqual(result, (True, 0))
                expected = (u32 * 179).from_buffer_copy(before)
                expected[14], expected[18] = 0x20803214, 37
                signed = ctypes.cast(expected, ctypes.POINTER(i32))
                for i, value in enumerate([1, -12500, 25000, -6250, 12500]):
                    signed[19 + i] = value
                self.assertEqual(after, bytes(expected))

    def test_non_owner_thread_cannot_redirect_a_read_into_a_write(self):
        result, before, after = self.run_escape(callback_thread=999)
        self.assertEqual(result, (False, None))
        self.assertEqual(after, before)

    def test_wrong_command_geometry_or_mask_is_forwarded_without_changes(self):
        for kwargs in ({"command": 0x2080B213}, {"packet_size": 712},
                       {"params_size": 644}, {"mask": 2}):
            with self.subTest(kwargs=kwargs):
                result, before, after = self.run_escape(**kwargs)
                self.assertEqual(result, (False, None))
                self.assertEqual(after, before)

    def test_different_gpu_objects_cannot_install_concurrent_hooks(self):
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        order = []

        def first(_records):
            order.append("first entered")
            entered.set()
            release.wait(2)
            order.append("first exited")

        def second(_records):
            order.append("second entered")
            second_entered.set()

        a = SimpleNamespace(_write_rail_records_locked=first,
                            _volt_rail_profile=lambda: None)
        b = SimpleNamespace(_write_rail_records_locked=second,
                            _volt_rail_profile=lambda: None)

        def start_second():
            second_started.set()
            GPU._write_rail_records(b, {})

        t1 = threading.Thread(target=GPU._write_rail_records, args=(a, {}))
        t2 = threading.Thread(target=start_second)
        t1.start()
        try:
            self.assertTrue(entered.wait(1))
            t2.start()
            self.assertTrue(second_started.wait(1))
            self.assertFalse(second_entered.wait(.05))
        finally:
            release.set()
            t1.join(2)
            if t2.ident is not None:
                t2.join(2)
        self.assertEqual(order, ["first entered", "first exited", "second entered"])


class LegacyFloorSettlingTests(unittest.TestCase):
    def test_pascal_floor_change_resends_only_the_verified_identical_records(self):
        gpu = legacy_gpu("pascal")
        wanted = {0: [0, 0, 0, 225000]}

        def store(records):
            gpu.nvapi.control = {r: list(values) for r, values in records.items()}
            return True, 0

        gpu._write_rail_records_locked = Mock(side_effect=store)
        self.assertEqual(GPU._write_rail_records(gpu, wanted), (True, 0))
        self.assertEqual(gpu._write_rail_records_locked.call_count, 2)
        self.assertTrue(all(c.args == (wanted,) for c in
                            gpu._write_rail_records_locked.call_args_list))
        gpu._write_rail_records_locked.reset_mock()
        self.assertEqual(GPU._write_rail_records(gpu, {0: [0, 0, 0, 0]}),
                         (True, 0))
        self.assertEqual(gpu._write_rail_records_locked.call_count, 2)

    def test_unverified_first_write_cannot_trigger_an_identity_resend(self):
        gpu = legacy_gpu("pascal")
        gpu._write_rail_records_locked = Mock(return_value=(True, 0))
        self.assertEqual(GPU._write_rail_records(gpu, {0: [0, 0, 0, 225000]}),
                         (False, 0))
        gpu._write_rail_records_locked.assert_called_once()

    def test_other_fields_and_drivers_do_not_get_an_extra_write(self):
        for gpu, records in ((legacy_gpu("pascal"), {0: [-12500, 0, 0, 0]}),
                             (legacy_gpu("turing"), {0: [0, 0, 0, 12500]}),
                             (fake_gpu("pascal"), {0: [0, 0, 0, 12500]})):
            gpu._write_rail_records_locked = Mock(return_value=(True, 0))
            self.assertEqual(GPU._write_rail_records(gpu, records), (True, 0))
            gpu._write_rail_records_locked.assert_called_once_with(records)


if __name__ == "__main__":
    unittest.main()