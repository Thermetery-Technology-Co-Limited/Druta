# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native RM escape transport, increment 1 (gdi32's D3DKMT* exports as a
capability, adapter identification by PCI slot, one raw escape, and closing
every enumerated handle) against nvbackend.GPU._legacy_clk_gdi and
_legacy_clk_escape.

One fake gdi32 serves both sides: the Python twin reaches it through a patched
ctypes.WinDLL, the native core through the resolver seam (library "gdi32").
Every case compares the result, the full gdi call log (including the packet
bytes the escape received) and the packet contents afterwards."""
import ctypes
import unittest

from druta import native
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from tests.fake_driver import (  # noqa: E402, F401
    CLOSE,
    ENUM,
    ENUM2,
    ESCAPE,
    FROM_HDC,
    FROM_NAME,
    KMT,
    NTSTATUS_INVALID_PARAMETER,
    NV,
    QUERY,
    REQUIRED,
    Adapter,
    Enum2,
    Query,
    TransportRig,
    install,
    slot_of,
    u32,
)
from tests.fake_driver import TRANSPORT_FIELDS as FIELDS  # noqa: E402, F401


def escape_call(handle, rig_packet_bytes):
    return ("Escape", handle, 0, 0, 8, 0, 84, rig_packet_bytes)


PATTERN = bytes((NV.u32 * 21)(*(0xA5A50000 | k for k in range(21))))


def after_writes(writes):
    words = [0xA5A50000 | k for k in range(21)]
    for index, value in writes.items():
        words[index] = value
    return words


def queries(*handles):
    return [("QueryAdapterInfo", h, 6, 12) for h in handles]


def closes(*handles):
    return [("CloseAdapter", h) for h in handles]


class TransportCase(ParityCase):
    def native_error(self, rig, kind, fixed):
        error = rig.native_error
        self.assertIsNotNone(error)
        self.assertEqual((error["kind"], error["fixed"], error["ntstatus"]), (kind, fixed, None))
        return error


class HappyPathTests(TransportCase):
    def test_escape_on_the_adapter_at_the_paired_slot(self):
        rig = TransportRig()
        result, calls, (packet,) = self.both(rig, rig.escape())
        self.assertEqual(result, 0)
        self.assertEqual(calls, [("EnumAdapters2",)] + queries(111, 222, 333)
                         + [escape_call(111, PATTERN)] + closes(111, 222, 333))
        self.assertEqual(packet, after_writes({16: 0, 17: 2}))  # the driver's writes reach the caller's buffer
        self.assertIsNone(rig.native_error)
        rig.windll.assert_called_once_with("gdi32.dll")

    def test_fields_pass_through_and_only_hadapter_is_replaced(self):
        rig = TransportRig(adapters=[(7, 1, 0, 0)])
        fields = dict(hAdapter=999, hDevice=0x11, Type=0x22, Flags=0x33, hContext=0x44)
        _, calls, _ = self.both(rig, rig.escape(fields))
        self.assertEqual(calls[2], ("Escape", 7, 0x11, 0x22, 0x33, 0x44, 84, PATTERN))

    def test_non_zero_escape_status_is_returned_signed(self):
        rig = TransportRig(escape_status=NTSTATUS_INVALID_PARAMETER, escape_writes={})
        result, calls, (packet,) = self.both(rig, rig.escape())
        self.assertEqual(result, NTSTATUS_INVALID_PARAMETER)
        self.assertEqual(calls[-3:], closes(111, 222, 333))
        self.assertEqual(packet, after_writes({}))
        self.assertIsNone(rig.native_error)  # an issued escape is not "unavailable", whatever its status

    def test_alternate_slot_including_the_top_bus_and_a_non_zero_device(self):
        rig = TransportRig(bus=0xFF, device=0x1F, adapters=[(5, 1, 0, 0), (6, 0xFF, 0x1F, 0), (8, 0xFF, 0, 0)])
        result, calls, _ = self.both(rig, rig.escape())
        self.assertEqual(result, 0)
        self.assertEqual(calls, [("EnumAdapters2",)] + queries(5, 6, 8) + [escape_call(6, PATTERN)] + closes(5, 6, 8))

    def test_handles_are_never_cached(self):
        rig = TransportRig()

        def twice(gpu):
            first = rig.escape()(gpu)
            return first, rig.escape()(gpu)
        result, calls, _ = self.both(rig, twice)
        self.assertEqual(result, (0, 0))
        one = [("EnumAdapters2",)] + queries(111, 222, 333) + [escape_call(111, PATTERN)] + closes(111, 222, 333)
        second_packet = bytes((NV.u32 * 21)(*after_writes({16: 0, 17: 2})))
        two = [("EnumAdapters2",)] + queries(111, 222, 333) + [escape_call(111, second_packet)] + closes(111, 222, 333)
        self.assertEqual(calls, one + two)

    def test_layer0_does_not_gate_on_nvapi_ok(self):
        # A pairing refusal disables NVAPI but keeps its selected slot (nvbackend.GPU._pair and
        # Nvapi::disable alike); the raw escape layer only reads the slot. Gating belongs to the
        # capture layer above it (_capture_legacy_clk_transport checks a.ok).
        rig = TransportRig(nvml_devid=0x1B02)
        pairing = []

        def action(gpu):
            if isinstance(gpu, NV.GPU):
                gpu.nvapi.ok = False
            else:
                pairing.append(gpu.pairing_error)
            return rig.escape()(gpu)
        result, calls, _ = self.both(rig, action)
        self.assertIn("NVAPI disabled", pairing[0])
        self.assertEqual(result, 0)
        self.assertEqual(calls[3 + 1], escape_call(111, PATTERN))


class IdentityTests(TransportCase):
    def test_no_adapter_at_the_slot(self):
        rig = TransportRig(bus=4)
        result, calls, (packet,) = self.both(rig, rig.escape())
        self.assertIsNone(result)
        self.assertEqual(calls, [("EnumAdapters2",)] + queries(111, 222, 333) + closes(111, 222, 333))
        self.assertEqual(packet, after_writes({}))
        self.assertIn("no adapter at the paired PCI slot 0000:04:00.0", self.native_error(rig, "transient", False)["message"])

    def test_two_adapters_at_one_address(self):
        rig = TransportRig(adapters=[(111, 1, 0, 0), (222, 2, 0, 0), (444, 1, 0, 0)])
        result, calls, _ = self.both(rig, rig.escape())
        self.assertIsNone(result)
        self.assertEqual(calls, [("EnumAdapters2",)] + queries(111, 222, 444) + closes(111, 222, 444))
        self.assertIn("2 adapters", self.native_error(rig, "transient", False)["message"])

    def test_a_failed_query_skips_only_that_adapter(self):
        # The duplicate at 444 is unreadable, so the address is unique among the adapters that answered.
        rig = TransportRig(adapters=[(111, 1, 0, 0), (222, 2, 0, 0), (444, 1, 0, 0)], query_fail={444})
        result, calls, _ = self.both(rig, rig.escape())
        self.assertEqual(result, 0)
        self.assertEqual(calls, [("EnumAdapters2",)] + queries(111, 222, 444) + [escape_call(111, PATTERN)]
                         + closes(111, 222, 444))

    def test_the_matching_adapters_query_fails(self):
        rig = TransportRig(query_fail={111})
        result, calls, _ = self.both(rig, rig.escape())
        self.assertIsNone(result)
        self.assertEqual(calls, [("EnumAdapters2",)] + queries(111, 222, 333) + closes(111, 222, 333))
        self.native_error(rig, "transient", False)

    def test_function_number_is_part_of_the_address(self):
        rig = TransportRig(adapters=[(111, 1, 0, 1), (222, 1, 0, 0)])
        _, calls, _ = self.both(rig, rig.escape())
        self.assertEqual(calls[3], escape_call(222, PATTERN))

    def test_slots_a_real_pairing_can_produce_that_the_rule_refuses(self):
        # NVAPI's bus/device are u32: past 0xFF the formatted slot has three hex digits.
        for bus, device in ((0x100, 0), (1, 0x100)):
            with self.subTest(bus=bus, device=device):
                rig = TransportRig(bus=bus, device=device)
                result, calls, (packet,) = self.both(rig, rig.escape())
                self.assertIsNone(result)
                self.assertEqual(calls, [])  # refused before gdi32 is even loaded
                self.assertEqual(packet, after_writes({}))
                rig.windll.assert_not_called()
                error = self.native_error(rig, "invalid", False)
                self.assertIn(f"'{slot_of(bus, device)}'", error["message"])

    def test_slot_rule_on_the_python_side(self):
        # Only NVAPI's selected slot feeds the rule, and the native core builds that from bus and
        # device with a fixed 0000 domain and function 0, so whitespace, a short bus, a non-zero
        # domain, a function past 7 or a missing function cannot be produced through a real
        # pairing: they are checked on the Python twin, against the regex the native escape_slot
        # mirrors. A refused slot never loads gdi32.
        refused = [" 0000:01:00.0", "0000:01:00.0 ", "0000:1:00.0", "0001:01:00.0", "0000:01:00.8",
                   "0000:01:00", "", ":01:00.0", "0000:01:000.0", "0000:01:00.a", "0000:0g:00.0"]
        for slot in refused:
            with self.subTest(slot=slot):
                rig = TransportRig()
                gpu = rig.python_gpu()
                gpu.nvapi.selected["slot"] = slot
                with rig.python_windll() as windll:
                    self.assertIsNone(gpu._legacy_clk_escape(rig.packet, FIELDS))
                windll.assert_not_called()
                self.assertEqual(rig.calls, [])
        for slot, adapter in (("0000:ff:1f.7", (9, 0xFF, 0x1F, 7)), ("00000000:01:00.0", (9, 1, 0, 0)),
                              ("0000:FF:0A.0", (9, 0xFF, 0x0A, 0))):
            with self.subTest(slot=slot):
                rig = TransportRig(adapters=[adapter])
                gpu = rig.python_gpu()
                gpu.nvapi.selected["slot"] = slot
                with rig.python_windll():
                    self.assertEqual(gpu._legacy_clk_escape(rig.packet, FIELDS), 0)
                self.assertEqual(rig.calls[2][:2], ("Escape", 9))


class EnumerationTests(TransportCase):
    def test_enumeration_failure_closes_nothing(self):
        rig = TransportRig(enum_status=-1)
        result, calls, (packet,) = self.both(rig, rig.escape())
        self.assertIsNone(result)
        self.assertEqual(calls, [("EnumAdapters2",)])
        self.assertEqual(packet, after_writes({}))
        self.assertIn("D3DKMTEnumAdapters2 failed", self.native_error(rig, "transient", False)["message"])

    def test_only_the_first_sixteen_adapters_are_seen(self):
        # A 17th adapter at the card's own address would make the match ambiguous if it were read.
        adapters = [(1000 + k, 0x10 + k, 0, 0) for k in range(16)] + [(2000, 1, 0, 0), (2001, 2, 0, 0)]
        adapters[5] = (1005, 1, 0, 0)
        rig = TransportRig(adapters=adapters)
        handles = [1000 + k for k in range(16)]
        result, calls, _ = self.both(rig, rig.escape())
        self.assertEqual(result, 0)
        self.assertEqual(calls, [("EnumAdapters2",)] + queries(*handles) + [escape_call(1005, PATTERN)]
                         + closes(*handles))

    def test_fewer_adapters_than_capacity(self):
        rig = TransportRig(adapters=[], bus=1)
        result, calls, _ = self.both(rig, rig.escape())
        self.assertIsNone(result)
        self.assertEqual(calls, [("EnumAdapters2",)])


class MissingExportTests(TransportCase):
    def test_each_required_export_missing(self):
        for missing in REQUIRED:
            with self.subTest(missing=missing):
                rig = TransportRig(missing={missing})
                result, calls, (packet,) = self.both(rig, rig.escape())
                self.assertIsNone(result)
                self.assertEqual(calls, [])  # the capability is checked before any call
                self.assertEqual(packet, after_writes({}))
                rig.windll.assert_called_once_with("gdi32.dll")
                error = self.native_error(rig, "unsupported", True)
                with rig.native_gpu() as gpu:
                    cap = native.escape_capability()
                self.assertFalse(cap["complete"])
                self.assertTrue(cap["gdi32_loaded"])
                self.assertEqual({k: cap[k] for k in ("escape", "query_adapter_info", "close_adapter",
                                                      "enum_adapters2", "enum_adapters")},
                                 {"escape": missing != ESCAPE, "query_adapter_info": missing != QUERY,
                                  "close_adapter": missing != CLOSE, "enum_adapters2": missing != ENUM2,
                                  "enum_adapters": True})
                self.assertEqual(cap["method"], "none" if missing == ENUM2 else "enum2")
                expected = ("XPDM: no D3DKMT escape path; driver escapes are not possible on this image"
                            if missing == ESCAPE else f"gdi32 lacks {missing}")
                self.assertEqual(cap["detail"], expected)
                self.assertEqual(error["message"], expected)

    def test_several_missing_are_all_named(self):
        rig = TransportRig(missing={ENUM2, CLOSE})
        self.assertIsNone(self.both(rig, rig.escape())[0])
        with rig.native_gpu():
            cap = native.escape_capability()
        self.assertEqual(cap["detail"], "gdi32 lacks D3DKMTEnumAdapters2, D3DKMTCloseAdapter")
        # without the escape itself the XPDM wording wins over the list
        rig = TransportRig(missing={QUERY, ESCAPE})
        self.assertIsNone(self.both(rig, rig.escape())[0])
        with rig.native_gpu():
            self.assertTrue(native.escape_capability()["detail"].startswith("XPDM: "))

    def test_gdi32_not_loadable(self):
        rig = TransportRig(gdi=False)
        result, calls, _ = self.both(rig, rig.escape())
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        rig.windll.assert_called_once_with("gdi32.dll")
        error = self.native_error(rig, "unsupported", True)
        with rig.native_gpu():
            cap = native.escape_capability()
        self.assertFalse(cap["gdi32_loaded"] or cap["complete"] or cap["escape"] or cap["enum_adapters2"])
        self.assertEqual(cap["method"], "none")
        self.assertTrue(cap["detail"].startswith("gdi32.dll not loadable: "), cap["detail"])
        self.assertEqual(error["message"], cap["detail"])

    def test_capability_follows_the_resolver_installation(self):
        # Cached per installation, not per process: a later installation is probed afresh.
        with TransportRig(missing={QUERY}).native_gpu():
            self.assertFalse(native.escape_capability()["complete"])
        rig = TransportRig()
        with rig.native_gpu():
            self.assertTrue(native.escape_capability()["complete"])
        self.assertEqual(self.both(rig, rig.escape())[0], 0)


class CapabilityRecordTests(TransportCase):
    def test_full_table(self):
        with TransportRig().native_gpu():
            cap = native.escape_capability()
        self.assertEqual(cap, {"gdi32_loaded": True, "escape": True, "query_adapter_info": True,
                               "close_adapter": True, "enum_adapters2": True, "enum_adapters": True,
                               "open_from_display_name": True, "open_from_hdc": True, "complete": True,
                               "method": "enum2", "patch_bytes": 14 if ctypes.sizeof(ctypes.c_void_p) == 8 else 5,
                               "detail": ""})

    def test_minimal_table_is_complete(self):
        with TransportRig(missing={ENUM, FROM_NAME, FROM_HDC}).native_gpu():
            cap = native.escape_capability()
        self.assertTrue(cap["complete"])
        self.assertFalse(cap["enum_adapters"] or cap["open_from_display_name"] or cap["open_from_hdc"])
        self.assertEqual(cap["detail"], "")

    def test_windows7_shape_enum_without_enum2_is_not_complete(self):
        # This increment implements main's EnumAdapters2 path only; the fallback is reported, not chosen.
        rig = TransportRig(missing={ENUM2})
        with rig.native_gpu():
            cap = native.escape_capability()
        self.assertFalse(cap["complete"])
        self.assertTrue(cap["enum_adapters"] and not cap["enum_adapters2"])
        self.assertEqual(cap["method"], "none")
        self.assertEqual(cap["detail"], "gdi32 lacks D3DKMTEnumAdapters2")
        _, calls, _ = self.both(rig, rig.escape())
        self.assertEqual(calls, [])  # and D3DKMTEnumAdapters is never called


class NoNvapiTests(TransportCase):
    def test_native_without_nvapi_has_no_slot(self):
        # The native core has no selected NVAPI adapter, so the slot is "" even though NVML knows
        # the card; it refuses before loading gdi32. Not run through `both`: the Python twin's
        # SimpleNamespace keeps a selected slot, and a real NvAPI() that failed to load has
        # selected = None, on which _legacy_clk_escape raises AttributeError (see below).
        rig = TransportRig(with_nvapi=False)
        rig.reset()
        with rig.native_gpu() as gpu:
            self.assertIsNone(gpu.nvapi.selected)
            self.assertEqual(gpu.slot(), "0000:01:00.0")  # NVML's slot is not used by the transport
            self.assertIsNone(gpu._legacy_clk_escape(rig.packet, FIELDS))
            error = gpu.last_escape_error
        self.assertEqual(rig.calls, [])
        self.assertEqual((error["kind"], error["fixed"]), ("invalid", False))
        self.assertEqual(error["message"], "the paired PCI slot '' is not a usable adapter address")
        self.assertEqual(list(rig.packet), after_writes({}))

    def test_python_twin_with_unloaded_nvapi_raises(self):
        # Recorded divergence: nvbackend.NvAPI leaves selected None when nvapi64.dll does not load,
        # and _legacy_clk_escape calls .get on it. Its only caller reaches it after an NVAPI
        # capture, so this is unreachable on main; the native core returns None instead.
        rig = TransportRig(with_nvapi=False)
        gpu = rig.python_gpu()
        gpu.nvapi.selected = None
        with rig.python_windll() as windll, self.assertRaises(AttributeError):
            gpu._legacy_clk_escape(rig.packet, FIELDS)
        windll.assert_not_called()

    def test_last_escape_error_clears_after_a_success(self):
        rig = TransportRig(bus=4, adapters=[(111, 1, 0, 0), (444, 4, 0, 0)])
        rig.reset()
        with rig.native_gpu() as gpu:
            rig.adapters = [(111, 1, 0, 0)]
            self.assertIsNone(gpu._legacy_clk_escape(rig.packet, FIELDS))
            self.assertEqual(gpu.last_escape_error["kind"], "transient")
            rig.adapters = [(111, 1, 0, 0), (444, 4, 0, 0)]  # the card came back: recovery needs no reopen
            self.assertEqual(gpu._legacy_clk_escape(rig.packet, FIELDS), 0)
            self.assertIsNone(gpu.last_escape_error)


if __name__ == "__main__":
    unittest.main()
