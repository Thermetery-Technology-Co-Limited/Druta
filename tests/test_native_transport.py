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
import uuid

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
NO_DISPLAY_ENUM = "; display-name fallback lacks user32!EnumDisplayDevicesW"
FROM_INTERFACE = "D3DKMTOpenAdapterFromDeviceName"
INTERFACE_SIZE = "CM_Get_Device_Interface_List_SizeW"
INTERFACE_LIST = "CM_Get_Device_Interface_ListW"


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
                if missing == ENUM2:
                    expected += NO_DISPLAY_ENUM
                self.assertEqual(cap["detail"], expected)
                self.assertEqual(error["message"], expected)

    def test_several_missing_are_all_named(self):
        rig = TransportRig(missing={ENUM2, CLOSE})
        self.assertIsNone(self.both(rig, rig.escape())[0])
        with rig.native_gpu():
            cap = native.escape_capability()
        self.assertEqual(cap["detail"], "gdi32 lacks D3DKMTEnumAdapters2, D3DKMTCloseAdapter" + NO_DISPLAY_ENUM)
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

    def test_enum1_alone_cannot_supply_the_display_list(self):
        rig = TransportRig(missing={ENUM2})
        with rig.native_gpu():
            cap = native.escape_capability()
        self.assertFalse(cap["complete"])
        self.assertTrue(cap["enum_adapters"] and not cap["enum_adapters2"])
        self.assertEqual(cap["method"], "none")
        self.assertEqual(cap["detail"], "gdi32 lacks D3DKMTEnumAdapters2" + NO_DISPLAY_ENUM)
        _, calls, _ = self.both(rig, rig.escape())
        self.assertEqual(calls, [])  # and D3DKMTEnumAdapters is never called


class DisplayDevice(ctypes.Structure):
    _fields_ = [("size", u32), ("name", ctypes.c_uint16 * 32), ("description", ctypes.c_uint16 * 128),
                ("flags", u32), ("device_id", ctypes.c_uint16 * 128), ("registry_key", ctypes.c_uint16 * 128)]


class OpenDisplay(ctypes.Structure):
    _fields_ = [("name", ctypes.c_uint16 * 32), ("handle", u32), ("luid_low", u32), ("luid_high", u32),
                ("source", u32)]


class DisplayRig(TransportRig):
    """Each output supplies its own opened handle, LUID and PCI location. Display numbers are arbitrary."""

    def __init__(self, outputs, *, missing=(ENUM2,), open_fail=(), **kwargs):
        # Output rows are (handle, luid_low, luid_high, bus, device, function).
        super().__init__(adapters=[(handle, *slot) for handle, _, _, *slot in outputs], missing=missing, **kwargs)
        displays = {rf"\\.\DISPLAY{index + 4}": row for index, row in enumerate(outputs)}
        names = list(displays)
        enum_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
            ctypes.c_int32, ctypes.c_void_p, u32, ctypes.POINTER(DisplayDevice), u32)

        @enum_type
        def enumerate_display(parent, index, record, flags):
            self.calls.append(("EnumDisplayDevicesW", index))
            if parent or record.contents.size != 840 or index >= len(names):
                return 0
            name = names[index]
            record.contents.name[:len(name)] = [ord(c) for c in name]
            record.contents.flags = 1  # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
            return 1

        @KMT
        def open_display(pointer):
            request = OpenDisplay.from_address(pointer)
            name = "".join(chr(unit) for unit in request.name).partition("\0")[0]
            self.calls.append(("OpenDisplay", name))
            if name not in displays:
                return -1
            handle, low, high, *_ = displays[name]
            if handle in open_fail:
                return -1
            request.handle, request.luid_low, request.luid_high = handle, low, high
            return 0

        self.user32 = {"EnumDisplayDevicesW": enumerate_display}
        self._callbacks[FROM_NAME] = open_display

    def native_gpu(self):
        install(self.nvml, self.nvapi, self.gdi_table(), user32=self.user32)
        return native.Gpu()


class OpenInterface(ctypes.Structure):
    _fields_ = [("name", ctypes.POINTER(ctypes.c_uint16)), ("handle", u32), ("luid_low", u32), ("luid_high", u32)]


class InterfaceRig(DisplayRig):
    def __init__(self, displays, interfaces, *, list_statuses=(), **kwargs):
        super().__init__(displays, **kwargs)
        self.adapters.extend((handle, *slot) for handle, _, _, *slot in interfaces)
        paths = {rf"\\?\PCI#DISPLAY{index}": row for index, row in enumerate(interfaces)}
        self.multi_sz = ("".join(path + "\0" for path in paths) + "\0").encode("utf-16-le")
        self.reported_chars = None
        list_statuses = iter(list_statuses)
        arrival_guid = uuid.UUID("1ca05180-a699-450a-9a0c-de4fbe3ddd89").bytes_le
        stdcall = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

        def valid_args(guid, device_id, flags):
            return guid and ctypes.string_at(guid, 16) == arrival_guid and device_id is None and flags == 0

        @stdcall(u32, ctypes.POINTER(u32), ctypes.c_void_p, ctypes.c_void_p, u32)
        def get_size(length, guid, device_id, flags):
            self.calls.append(("InterfaceSize",))
            if not valid_args(guid, device_id, flags):
                return 3  # CR_INVALID_POINTER
            length[0] = len(self.multi_sz) // 2 if self.reported_chars is None else self.reported_chars
            return 0

        @stdcall(u32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, u32, u32)
        def get_list(guid, device_id, buffer, char_count, flags):
            self.calls.append(("InterfaceList",))
            if not valid_args(guid, device_id, flags):
                return 3
            status = next(list_statuses, 0)
            if status or char_count * 2 < len(self.multi_sz):
                return status or 0x1A  # CR_BUFFER_SMALL
            ctypes.memmove(buffer, self.multi_sz, len(self.multi_sz))
            return 0

        @KMT
        def open_interface(pointer):
            request = OpenInterface.from_address(pointer)
            # All fictitious names have a unique bounded, NUL-terminated spelling.
            for path, (handle, low, high, *_) in paths.items():
                if all(request.name[i] == ord(c) for i, c in enumerate(path + "\0")):
                    self.calls.append(("OpenInterface", path))
                    request.handle, request.luid_low, request.luid_high = handle, low, high
                    return 0
            return -1

        self.cfgmgr32 = {INTERFACE_SIZE: get_size, INTERFACE_LIST: get_list}
        self._callbacks[FROM_INTERFACE] = open_interface

    def native_gpu(self):
        install(self.nvml, self.nvapi, self.gdi_table(), user32=self.user32, cfgmgr32=self.cfgmgr32)
        return native.Gpu()


class LegacyDisplayTests(TransportCase):
    def run_escape(self, rig):
        with rig.native_gpu() as gpu:
            cap = native.escape_capability()
            status = rig.escape()(gpu)
        return cap, status

    def assert_handles(self, rig, opened, selected=None):
        self.assertCountEqual([call[1] for call in rig.calls if call[0] == "CloseAdapter"], opened)
        escapes = [call for call in rig.calls if call[0] == "Escape"]
        if selected is None:
            self.assertEqual(escapes, [])
            self.assertEqual(list(rig.packet), after_writes({}))
        else:
            self.assertEqual(len(escapes), 1)
            self.assertIn(escapes[0], [escape_call(handle, PATTERN) for handle in selected])
            self.assertNotIn(("CloseAdapter", escapes[0][1]), rig.calls[:rig.calls.index(escapes[0])])
            self.assertEqual(list(rig.packet), after_writes({16: 0, 17: 2}))

    def test_matches_exact_pci_slot_and_deduplicates_two_outputs_of_one_adapter(self):
        rig = DisplayRig([(10, 1, 0, 0xFE, 0x1A, 1), (20, 2, 3, 0xFE, 0x1A, 0),
                          (30, 2, 3, 0xFE, 0x1A, 0)], bus=0xFE, device=0x1A)
        cap, status = self.run_escape(rig)
        self.assertTrue(cap["complete"])
        self.assertEqual(cap["method"], "gdi_display_name")
        self.assertEqual(status, 0)
        self.assert_handles(rig, [10, 20, 30], selected=[20, 30])

    def test_distinct_adapters_and_unknown_luids_do_not_merge_at_the_same_slot(self):
        for low, high_a, high_b in ((12, 1, 2), (0, 0, 0)):
            with self.subTest(luid=(low, high_a, high_b)):
                rig = DisplayRig([(20, low, high_a, 1, 0, 0), (30, low, high_b, 1, 0, 0)])
                _, status = self.run_escape(rig)
                self.assertIsNone(status)
                self.native_error(rig, "transient", False)
                self.assert_handles(rig, [20, 30])

    def test_failed_open_and_query_leave_no_match_and_close_only_opened_handles(self):
        rig = DisplayRig([(10, 1, 0, 1, 0, 0), (20, 2, 0, 1, 0, 0), (30, 3, 0, 2, 0, 0)],
                         open_fail={10}, query_fail={20})
        _, status = self.run_escape(rig)
        self.assertIsNone(status)
        self.native_error(rig, "transient", False)
        self.assert_handles(rig, [20, 30])

    def test_partial_display_name_exports_never_dispatch(self):
        for absent in (FROM_NAME, "EnumDisplayDevicesW"):
            with self.subTest(absent=absent):
                rig = DisplayRig([], missing={ENUM2, absent})
                rig.user32.pop(absent, None)
                cap, status = self.run_escape(rig)
                self.assertFalse(cap["complete"])
                self.assertEqual(cap["method"], "none")
                self.assertIn(absent, cap["detail"])
                self.assertIsNone(status)
                self.native_error(rig, "unsupported", True)
                self.assertEqual(rig.calls, [])

    def test_enum2_stays_preferred_even_if_legacy_exports_are_present(self):
        rig = DisplayRig([(42, 11, 3, 1, 0, 0)], missing=())
        cap, status = self.run_escape(rig)
        self.assertEqual((cap["method"], status), ("enum2", 0))
        self.assertEqual(rig.calls, [("EnumAdapters2",)] + queries(42) + [escape_call(42, PATTERN)] + closes(42))

    def test_failed_enum2_preserves_transient_error_and_never_retries_a_different_route(self):
        rig = DisplayRig([(42, 11, 3, 1, 0, 0)], missing=(), enum_status=-1)
        _, status = self.run_escape(rig)
        self.assertIsNone(status)
        self.native_error(rig, "transient", False)
        self.assertEqual(rig.calls, [("EnumAdapters2",)])

    def test_headless_target_matches_only_by_pci_slot_after_a_display_miss(self):
        # GPU 51 has a monitor; the selected GPU is absent from the display list.
        rig = InterfaceRig([(51, 4, 0, 1, 0, 0)],
                           [(61, 4, 0, 1, 0, 0), (72, 8, 0, 2, 0x1F, 0), (73, 9, 0, 2, 0x1F, 1)],
                           bus=2, device=0x1F)
        _, status = self.run_escape(rig)
        self.assertEqual(status, 0)
        self.assert_handles(rig, [51, 61, 72, 73], selected=[72])

    def test_interface_route_does_not_retry_ambiguous_or_issued_display_request(self):
        ambiguity = [(21, 1, 0, 1, 0, 0), (31, 2, 0, 1, 0, 0)]
        for displays, driver_status in ((ambiguity, 0), (ambiguity[:1], NTSTATUS_INVALID_PARAMETER)):
            with self.subTest(driver_status=driver_status):
                rig = InterfaceRig(displays, [(41, 3, 0, 1, 0, 0)], escape_status=driver_status)
                _, status = self.run_escape(rig)
                self.assertNotIn(("InterfaceSize",), rig.calls)
                self.assertNotIn(("InterfaceList",), rig.calls)
                if driver_status == 0:
                    self.assertIsNone(status)
                    self.assert_handles(rig, [21, 31])
                else:
                    self.assertEqual(status, driver_status)
                    self.assert_handles(rig, [21], selected=[21])

    def test_interface_only_retries_a_racing_size_then_selects_the_adapter(self):
        rig = InterfaceRig([], [(72, 8, 0, 1, 0, 0)], missing={ENUM2, FROM_NAME}, list_statuses=[0x1A])
        cap, status = self.run_escape(rig)
        self.assertEqual((cap["complete"], cap["method"], status), (True, "device_interface", 0))
        self.assertEqual(rig.calls.count(("InterfaceSize",)), 2)
        self.assertEqual(rig.calls.count(("InterfaceList",)), 2)
        self.assert_handles(rig, [72], selected=[72])

    def test_interface_size_race_is_bounded_and_other_errors_do_not_dispatch(self):
        for status, attempts in ((0x1A, 3), (3, 1)):
            with self.subTest(status=status):
                rig = InterfaceRig([(52, 4, 0, 2, 0, 0)], [(72, 8, 0, 1, 0, 0)], list_statuses=[status] * 4)
                _, result = self.run_escape(rig)
                self.assertIsNone(result)
                self.native_error(rig, "transient", False)
                self.assertEqual(rig.calls.count(("InterfaceSize",)), attempts)
                self.assertEqual(rig.calls.count(("InterfaceList",)), attempts)
                self.assert_handles(rig, [52])

    def test_truncated_interface_list_cannot_dispatch_a_partially_discovered_match(self):
        rig = InterfaceRig([], [(72, 8, 0, 1, 0, 0)])
        # A valid matching path is followed by an unterminated entry.
        rig.multi_sz = rig.multi_sz[:-2] + "broken".encode("utf-16-le")
        _, status = self.run_escape(rig)
        self.assertIsNone(status)
        self.native_error(rig, "transient", False)
        self.assertIn(("InterfaceList",), rig.calls)
        self.assertNotIn("OpenInterface", [call[0] for call in rig.calls])
        self.assert_handles(rig, [])

    def test_invalid_interface_size_is_rejected_before_allocating_a_list(self):
        for count in (0, 0xFFFFFFFF):
            with self.subTest(count=count):
                rig = InterfaceRig([], [(72, 8, 0, 1, 0, 0)])
                rig.reported_chars = count
                _, status = self.run_escape(rig)
                self.assertIsNone(status)
                self.native_error(rig, "transient", False)
                self.assertIn(("InterfaceSize",), rig.calls)
                self.assertNotIn(("InterfaceList",), rig.calls)
                self.assert_handles(rig, [])

    def test_incomplete_device_interface_exports_do_not_select_that_route(self):
        for absent in (INTERFACE_SIZE, INTERFACE_LIST, FROM_INTERFACE):
            with self.subTest(absent=absent):
                rig = InterfaceRig([], [(72, 8, 0, 1, 0, 0)], missing={ENUM2, FROM_NAME, absent})
                rig.cfgmgr32.pop(absent, None)
                cap, status = self.run_escape(rig)
                self.assertEqual((cap["complete"], cap["method"], status), (False, "none", None))
                self.native_error(rig, "unsupported", True)
                self.assertEqual(rig.calls, [])


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
