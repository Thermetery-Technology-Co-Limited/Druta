# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Paired driver views, including direct runtime and I2C calls through the core."""
import ctypes
import gc
import threading
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from druta import native, nvbackend, railctl
from druta.controllers import up9512r
from druta.native_companions import BoundNvAPI, BoundNvml
from tests.controllers.test_up9512r import Bus
from tests.fake_driver import TITAN, XP, FakeNvapi, FakeNvml, install


class SharedInterfaceTableTests(unittest.TestCase):
    def test_python_resolves_the_shared_table_in_order_with_unchanged_signatures(self):
        calls = []

        def interface(_self, identifier, *argtypes):
            calls.append((identifier, argtypes))
            return lambda: -1  # Initialization stops after every name is bound.

        def query(_identifier):
            return None
        with patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(nvapi_QueryInterface=query), create=True), \
                patch.object(nvbackend.NvAPI, "_i", interface):
            api = nvbackend.NvAPI()
        self.assertEqual(calls, [(identifier, argtypes) for _, identifier, argtypes in nvbackend.NVAPI_INTERFACES])
        self.assertEqual(len({name for name, _, _ in nvbackend.NVAPI_INTERFACES}), len(calls))
        self.assertEqual(api.err_detail, "NvAPI_Initialize status -1")
        self.assertTrue(all(callable(getattr(api, name)) for name, _, _ in nvbackend.NVAPI_INTERFACES))


@unittest.skipUnless(native.available(), "native core not built (native/build.sh linux)")
class NativeCompanionTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def open(self, *, nvapi=None, nvml=None, slot="0000:01:00.0"):
        api = nvapi or FakeNvapi([TITAN, XP])
        ml = nvml or FakeNvml([XP, TITAN])
        install(ml, api)
        gpu = native.Gpu(slot)
        self.addCleanup(gpu.close)
        return gpu, api, ml

    def test_companions_use_selected_core_handles_when_enumeration_order_differs(self):
        gpu, _, _ = self.open()
        self.assertIsInstance(gpu.nvapi, BoundNvAPI)
        self.assertIsInstance(gpu.nvml, BoundNvml)
        self.assertEqual(gpu.nvapi.gpu, 0x2000)
        self.assertEqual(gpu.nvml.dev.value, 0x1001)
        bus = ctypes.c_uint32()
        self.assertEqual(gpu.nvapi.GetBusId(gpu.nvapi.gpu, ctypes.byref(bus)), 0)
        self.assertEqual(bus.value, 1)
        name = ctypes.create_string_buffer(96)
        self.assertEqual(gpu.nvml.dll.nvmlDeviceGetName(gpu.nvml.dev, name, len(name)), 0)
        self.assertEqual(name.value.decode(), TITAN["name"])

    def test_alternate_selected_card_reaches_its_own_exports(self):
        gpu, _, _ = self.open(slot="0000:02:00.0")
        self.assertEqual(gpu.nvapi.gpu, 0x2001)
        self.assertEqual(gpu.nvml.dev.value, 0x1000)
        bus = ctypes.c_uint32()
        gpu.nvapi.GetBusId(gpu.nvapi.gpu, ctypes.byref(bus))
        self.assertEqual(bus.value, 2)
        name = ctypes.create_string_buffer(96)
        gpu.nvml.dll.nvmlDeviceGetName(gpu.nvml.dev, name, len(name))
        self.assertEqual(name.value.decode(), XP["name"])

    def test_named_table_and_arbitrary_interface_presence_follow_core_resolver(self):
        gpu, _, _ = self.open()
        for name, identifier, argtypes in nvbackend.NVAPI_INTERFACES:
            with self.subTest(name=name):
                named = getattr(gpu.nvapi, name)
                self.assertEqual(named is not None, gpu.nvapi_interface_present(identifier))
                self.assertEqual(gpu.nvapi._i(identifier, *argtypes) is not None,
                                 gpu.nvapi_interface_present(identifier))
                self.assertIs(named, getattr(gpu.nvapi, name))
                if named:
                    self.assertEqual(named.argtypes, argtypes)
        self.assertIsNone(gpu.nvapi._i(0x12345678))
        with self.assertRaises(AttributeError):
            _ = gpu.nvapi.not_a_named_interface

    def test_pairing_failure_retains_diagnostics_and_clears_live_handle(self):
        gpu, _, _ = self.open(nvapi=FakeNvapi([dict(TITAN, devid=0x1234)]))
        self.assertFalse(gpu.nvapi.ok)
        self.assertEqual(gpu.nvapi.err_detail, gpu.pairing_error)
        self.assertIsNone(gpu.nvapi.gpu)
        self.assertEqual(gpu.nvapi.selected["devid"], 0x1234)
        self.assertTrue(gpu.nvml.ok)

    def test_missing_library_and_partial_exports_are_independent(self):
        for missing in ("nvapi", "nvml"):
            with self.subTest(missing=missing):
                install(None if missing == "nvml" else FakeNvml([TITAN]),
                        None if missing == "nvapi" else FakeNvapi([TITAN]))
                with native.Gpu() as gpu:
                    self.assertFalse(getattr(gpu, missing).ok)
                    self.assertTrue(gpu.nvapi.ok if missing == "nvml" else gpu.nvml.ok)
                    if missing == "nvapi":
                        self.assertIsNone(gpu.nvapi.GetBusId)
                    else:
                        self.assertIsNone(gpu.nvml.dev)
                        self.assertFalse(gpu.nvml.has("nvmlDeviceGetName"))

    def test_nvml_functions_are_cached_and_have_mutable_cdll_signatures(self):
        gpu, _, _ = self.open()
        fn = gpu.nvml.dll.nvmlDeviceGetName
        self.assertIs(fn, gpu.nvml.dll.nvmlDeviceGetName)
        self.assertEqual(fn.restype, ctypes.c_int)
        self.assertIsNone(fn.argtypes)
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        name = ctypes.create_string_buffer(96)
        self.assertEqual(fn(gpu.nvml.dev, name, len(name)), 0)
        self.assertEqual(name.value.decode(), TITAN["name"])
        self.assertEqual(gpu.nvml.dll.nvmlDeviceGetName.restype, ctypes.c_uint32)
        self.assertFalse(gpu.nvml.has("nvmlAbsent"))
        with self.assertRaises(AttributeError):
            _ = gpu.nvml.dll.nvmlAbsent
        self.assertEqual(gpu.nvml.errstr(42), "42")

    def test_version_words_match_python(self):
        gpu, _, _ = self.open()
        for revision in (1, 2, 3):
            self.assertEqual(gpu.nvapi.ver(ctypes.c_uint32, revision), nvbackend.NvAPI.ver(ctypes.c_uint32, revision))
            self.assertEqual(gpu.nvml.ver(ctypes.c_uint32, revision), nvbackend.Nvml.ver(ctypes.c_uint32, revision))

    def test_dynamic_controller_attributes_and_named_functions_are_stable(self):
        gpu, _, _ = self.open()
        mutex = gpu.nvapi._up9512r_mutex = threading.RLock()
        self.assertIs(gpu.nvapi._up9512r_mutex, mutex)
        self.assertIs(gpu.nvapi.GetBusId, gpu.nvapi.GetBusId)

    def test_optional_controller_state_and_capabilities_do_not_log_missing_members(self):
        from druta.native_frontend import drain_missing_members, reset_missing_members_for_tests
        gpu, _, _ = self.open()
        reset_missing_members_for_tests()
        self.addCleanup(reset_missing_members_for_tests)
        self.assertFalse(hasattr(gpu.nvapi, "_up9512r_mutex"))
        self.assertIsNone(gpu.nvapi.ClkMeasureFreq)
        self.assertFalse(gpu.nvml.has("nvmlAbsent"))
        self.assertEqual(drain_missing_members(), [])
        for _ in range(2):
            self.assertIsNone(getattr(gpu.nvapi, "unexpected", None))
            self.assertIsNone(getattr(gpu.nvml, "unexpected", None))
        messages = drain_missing_members()
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("BoundNvAPI.unexpected" in message for message in messages))
        self.assertTrue(any("BoundNvml.unexpected" in message for message in messages))

    def test_error_string_pointer_uses_nvmls_character_return_type(self):
        buffer = ctypes.create_string_buffer(b"fake error")

        @ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)
        def error_string(status):
            return ctypes.addressof(buffer)

        ml = FakeNvml([TITAN], extra={"nvmlErrorString": error_string})
        gpu, _, _ = self.open(nvml=ml)
        self.assertEqual(gpu.nvml.errstr(17), "fake error")

    def test_descriptor_failures_are_logged_as_internal_without_repeating_the_read(self):
        from druta.native_frontend import drain_missing_members, reset_missing_members_for_tests
        gpu, _, _ = self.open()
        reset_missing_members_for_tests()
        self.addCleanup(reset_missing_members_for_tests)
        reads = []

        def broken(_self):
            reads.append(1)
            raise AttributeError("original driver-view failure")

        for companion in (gpu.nvapi, gpu.nvml):
            with patch.object(type(companion), "test_broken", property(broken), create=True):
                with self.assertRaisesRegex(AttributeError, "original driver-view failure"):
                    _ = companion.test_broken
        self.assertEqual(reads, [1, 1])
        messages = drain_missing_members()
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("internal error reading" in message for message in messages))

    def test_close_invalidates_views_and_previously_cached_callables(self):
        gpu, _, _ = self.open()
        api, ml = gpu.nvapi, gpu.nvml
        named = api.GetBusId
        raw = api._i(0x1BE0B8E5, ctypes.c_void_p, ctypes.c_void_p)
        ml_call = ml.dll.nvmlDeviceGetName
        gpu.close()
        self.assertFalse(api.ok or ml.ok)
        self.assertIsNone(api.gpu)
        self.assertIsNone(ml.dev)
        self.assertIsNone(api._i(0x1BE0B8E5))
        self.assertIsNone(api.GetBusId)
        self.assertFalse(ml.has("nvmlDeviceGetName"))
        for function in (named, raw, ml_call):
            with self.assertRaises(native.NativeClosed):
                function(None, None)
        with self.assertRaises(native.NativeClosed):
            _ = ml.dll.nvmlDeviceGetName

    def test_cached_call_holds_box_but_does_not_keep_gpu_alive(self):
        install(FakeNvml([TITAN]), FakeNvapi([TITAN]))
        gpu = native.Gpu()
        owner, box = weakref.ref(gpu), weakref.ref(gpu._box)
        function = gpu.nvapi.GetBusId
        del gpu
        gc.collect()
        self.assertIsNone(owner())
        self.assertIsNotNone(box())
        with self.assertRaises(native.NativeClosed):
            function(None, None)
        del function
        gc.collect()
        self.assertIsNone(box())

    def test_rail_and_up9512r_reads_match_python_without_reaching_python_hooks(self):
        results = []
        for kind in ("python", "native"):
            bus = Bus()
            api = FakeNvapi([TITAN])
            handles = []

            @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
            def read(handle, packet, extra, _handles=handles, _bus=bus):
                _handles.append(handle)
                return _bus.read(handle, packet, extra)

            api.interfaces[railctl.I2C_READ_EX] = read
            install(FakeNvml([TITAN]), api)
            with patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(nvapi_QueryInterface=api.query), create=True), \
                    patch.object(nvbackend.GPU, "_capture_current_limit_transport", side_effect=AssertionError("Python hook")), \
                    patch.object(nvbackend.GPU, "_capture_legacy_clk_transport", side_effect=AssertionError("Python hook")), \
                    patch.object(nvbackend.GPU, "_write_rail_records", side_effect=AssertionError("Python hook")):
                owner = native.Gpu() if kind == "native" else None
                companion = owner.nvapi if owner else nvbackend.NvAPI()
                try:
                    controller = up9512r.UP9512R(companion)
                    another = up9512r.UP9512R(companion)
                    self.assertIs(controller._mutex, another._mutex)
                    rail = railctl.Rail(SimpleNamespace(addr7=0x25, port=2), companion)
                    results.append((controller.present(), controller.read(0x2D), rail.read(0x28, 1),
                                    list(bus.reads), list(handles)))
                    self.assertTrue(all(handle == 0x2000 for handle in handles))
                finally:
                    if owner:
                        owner.close()
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][:3], (True, 90, 0x2B))


if __name__ == "__main__":
    unittest.main()
