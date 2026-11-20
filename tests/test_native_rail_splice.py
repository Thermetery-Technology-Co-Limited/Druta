# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native rail splice, increment 4 of the RM escape transport: one
VoltRailsCtlGet under the hook, the recognized rail GET read back, and exactly
one SET carrying the prepared records (nvbackend.GPU._write_rail_records_locked
after its Python preconditions; DESIGN-TRANSPORT.md 2.5 and 3.4).

The Python twin runs GPU._write_rail_records_locked with volt_rail_limits_supported,
_volt_rail_profile and read_voltage_boost standing in for the preconditions,
as tests/test_rail_runtime_detection.exercise_native_write does; the core runs
Gpu._rail_write_locked(control_version, rail_mask, records, boost). The getter
and the hook-aware driver model are test_native_rm_transport.HookRig's; the
fake D3DKMTEscape here answers the rail GETs and SETs. Every case compares
(done, status), _volt_rail_write_error, _volt_rail_write_protocol,
_volt_rail_observed_packets, the getter's block words 0-1, every forwarded
escape with its packet bytes at the moment of the forward, and the final
packet bytes."""
import ctypes
import threading
import unittest
from unittest.mock import Mock

from druta import native, nvbackend, rail_transport
from tests.test_native_rm_transport import FIELDS, HookCase, HookRig, armed, forced, foreign, owner
from tests.test_native_transport import ENUM2, ESCAPE, KMT

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

NV = nvbackend
u32 = ctypes.c_uint32
GPU = NV.GPU
HANDLE = 0x2000
GETS = (0x2080B213, 0x20803213)
SETS = (0x2080F214, 0x20803214)
UNRELATED_CMD = 0x20800123
STATUS_UNSUCCESSFUL = -1073741823  # 0xC0000001
LAYOUTS = {1104: (1036, 0x2080B213, 18), 976: (908, 0x2080B213, 18), 716: (648, 0x20803213, 17)}
RECORD = [-12500, 0, 0, 0]
PRESERVE = "Could not preserve the current native rail record: "
FOREIGN = "Another driver thread interrupted rail detection. Retry the write."
NO_PROTECT = "Could not inspect the driver rail request. Nothing was written."


def no_packet(status):
    return ("The driver did not emit a recognized rail-control packet. Nothing was written. "
            f"Getter status: {status}. Copy rail diagnostics for this driver.")


def rail_packet(size=1104, *, mask=1):
    """A rail GET request: every word a distinct opaque pattern, then the understood words."""
    params, command, mask_word = LAYOUTS[size]

    def make():
        words = (u32 * (size // 4))(*(0x55660000 + k for k in range(size // 4)))
        words[14], words[15], words[16], words[mask_word] = command, params, 0, mask
        words[mask_word + 1] = 0  # NVAPI does not fill the boost word of a GET
        return words
    return make


def unrelated_packet(size=1104, command=UNRELATED_CMD):
    def make():
        words = (u32 * (size // 4))(*(0x77880000 + k for k in range(size // 4)))
        words[14] = command
        return words
    return make


class RailRig(HookRig):
    """HookRig with a rail-aware driver.

    get_status / get_rm: the GET's NTSTATUS and word 16; set_status / set_rm: the SET's
    response_params: word 15 of the GET response; response_size: the GET rewrites
    PrivateDriverDataSize; complete_976: the 976-byte GET fills its valid mask and record tail
    (exercise_native_write's native_976); valid_mask: word 17 of a 1104-byte GET response
    """

    def __init__(self, steps, *, get_status=0, get_rm=0, set_status=0, set_rm=0, response_params=None,
                 response_size=None, complete_976=True, valid_mask=0xFF, **kw):
        self.get_status, self.get_rm, self.set_status, self.set_rm = get_status, get_rm, set_status, set_rm
        self.response_params, self.response_size = response_params, response_size
        self.complete_976, self.valid_mask = complete_976, valid_mask
        super().__init__(steps, power=False, **kw)

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            status = 0
            try:
                e = NV.GPU._Escape.from_address(pointer)
                size = e.PrivateDriverDataSize
                self.calls.append(("Escape", e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext, size,
                                   ctypes.string_at(e.pPrivateDriverData, size), self.probe(),
                                   threading.current_thread() is threading.main_thread()))
                w = (u32 * (size // 4)).from_address(e.pPrivateDriverData)
                if size >= 68 and w[14] in GETS:
                    w[16] = self.get_rm
                    if size == 1104:
                        w[17] = self.valid_mask  # GET-only header: the SET keeps the request's
                    if size == 976 and self.complete_976:
                        w[17], w[20], w[25], w[26] = 0xFF, 2, 0x11223344, 0xAABBCCDD
                    if self.response_params is not None:
                        w[15] = self.response_params
                    if self.response_size is not None:
                        e.PrivateDriverDataSize = self.response_size
                    status = self.get_status
                elif size >= 68 and w[14] in SETS:
                    w[16] = self.set_rm
                    status = self.set_status
            except BaseException as exc:
                self.errors.append(repr(exc))
            return status
        table[ESCAPE] = escape
        return table

    def state(self):
        return [bytes(packet) for script in self.built for _, (packet, _), _ in script]


def write(records=None, *, boost=25, control_version=0x20AC8, rail_mask=None):
    """The action: (done, status) and the three attributes, on either side."""
    records = {0: RECORD} if records is None else records
    mask = sum(1 << rail for rail in records) if rail_mask is None else rail_mask

    def action(gpu):
        if isinstance(gpu, GPU):
            gpu.volt_rail_limits_supported = Mock(return_value=True)
            gpu._volt_rail_profile = lambda: {"control_version": control_version,
                                              "fields": {0: GPU.VOLT_LIMIT_FIELDS, 1: GPU.VOLT_LIMIT_FIELDS}}
            gpu.read_voltage_boost = Mock(return_value=boost)
            result = GPU._write_rail_records_locked(gpu, records)
        else:
            result = gpu._rail_write_locked(control_version, mask, records, boost)
        return (result, getattr(gpu, "_volt_rail_write_error", None),
                getattr(gpu, "_volt_rail_write_protocol", None),
                list(getattr(gpu, "_volt_rail_observed_packets", [])))
    return action


def escape_log(payload, *, patched=False, main=True):
    return ("Escape", 987, 0, 0, 8, 0, len(payload), bytes(payload), patched, main)


def build(make):
    return bytes(make())


def get_response(request, rig):
    """The GET request bytes after the fake driver answered them."""
    words = (u32 * (len(request) // 4)).from_buffer_copy(request)
    words[16] = rig.get_rm
    if len(request) == 1104:
        words[17] = rig.valid_mask
    if len(request) == 976 and rig.complete_976:
        words[17], words[20], words[25], words[26] = 0xFF, 2, 0x11223344, 0xAABBCCDD
    return bytes(words)


def expected_set(request, rig, records, boost, mask):
    layout = rail_transport.recognize_packet(request, mask)
    boundary = layout.record0 * 4
    current = request[:boundary] + get_response(request, rig)[boundary:]
    return rail_transport.prepare_write(current, mask, records, boost)


def with_status(payload, status):
    words = (u32 * (len(payload) // 4)).from_buffer_copy(payload)
    words[16] = status
    return bytes(words)


def protocol(size, version=0x20AC8):
    get, set_ = (0x20803213, 0x20803214) if size == 716 else (0x2080B213, 0x2080F214)
    return {"get_command": f"0x{get:08X}", "set_command": f"0x{set_:08X}", "packet_size": size,
            "getter_version": f"0x{version:08X}"}


def summary(make):
    return rail_transport.packet_summary(build(make))


class RailSpliceTests(HookCase):
    def happy(self, size, *, version=0x20AC8, records=None, valid_mask=0xFF):
        records = {0: RECORD} if records is None else records
        mask = sum(1 << rail for rail in records)
        rig = RailRig([owner(rail_packet(size, mask=mask))], valid_mask=valid_mask)
        (_, (result, error, proto, seen)), calls, (final,) = self.both(
            rig, write(records, control_version=version))
        request = build(rail_packet(size, mask=mask))
        sent = expected_set(request, rig, records, 25, mask)
        self.assertEqual(result, (True, 0))
        self.assertIsNone(error)
        self.assertEqual(proto, protocol(size, version))
        self.assertEqual(seen, [rail_transport.packet_summary(request)])
        self.assertEqual(calls, [("VoltRailsCtlGet", HANDLE, version, mask), armed(True),
                                 escape_log(request), escape_log(sent), armed(False)])
        self.assertEqual(final, with_status(sent, 0))
        return request, sent

    def test_1104_layout_one_get_then_one_set(self):
        request, sent = self.happy(1104)
        req, out = (u32 * 276).from_buffer_copy(request), (u32 * 276).from_buffer_copy(sent)
        self.assertEqual((req[14], out[14]), (0x2080B213, 0x2080F214))
        self.assertEqual(out[17], req[17])  # the request header, not the GET's valid mask (0xFF)
        self.assertEqual(out[18:20], [1, 25])  # mask and boost
        self.assertEqual(list(ctypes.cast(out, ctypes.POINTER(ctypes.c_int32))[20:25]), [5, *RECORD])
        self.assertEqual(out[27], 1)  # the valid word
        self.assertEqual(out[28:], req[28:])  # rail 1 untouched, opaque tail preserved

    def test_1104_both_rails(self):
        self.happy(1104, records={0: RECORD, 1: [-6250, -1, 7, 800000]})

    def test_976_keeps_the_valid_mask_out_and_the_record_tail_in(self):
        request, sent = self.happy(976)
        req, out = (u32 * 244).from_buffer_copy(request), (u32 * 244).from_buffer_copy(sent)
        self.assertEqual(out[17], req[17])
        self.assertEqual(out[20], 2)  # record type
        self.assertEqual(out[25:27], [0x11223344, 0xAABBCCDD])  # the GET's opaque record tail

    def test_716_layout_whatever_the_getter_version(self):
        for version in (0x20AC8, 0x10AC8):
            with self.subTest(version=hex(version)):
                request, sent = self.happy(716, version=version)
                self.assertEqual(sent[14 * 4:15 * 4], (0x20803214).to_bytes(4, "little"))
        _, sent = self.happy(1104, version=0x10AC8)  # and the version does not choose 716 either
        self.assertEqual(sent[14 * 4:15 * 4], (0x2080F214).to_bytes(4, "little"))

    def test_an_unrelated_escape_first_is_forwarded_unchanged_and_rearms(self):
        steps = [owner(unrelated_packet()), owner(unrelated_packet(64)), owner(unrelated_packet(8196)),
                 owner(rail_packet())]
        rig = RailRig(steps)
        (_, (result, error, _, seen)), calls, final = self.both(rig, write())
        self.assertEqual((result, error), ((True, 0), None))
        # 68 <= size <= 8192 are summarised; the 64- and 8196-byte packets only pass through
        self.assertEqual(seen, [summary(unrelated_packet()), summary(rail_packet())])
        self.assertEqual(calls[1:8], [armed(True), escape_log(build(unrelated_packet())), armed(True),
                                      escape_log(build(unrelated_packet(64))), armed(True),
                                      escape_log(build(unrelated_packet(8196))), armed(True)])
        self.assertEqual(final[:3], [build(unrelated_packet()), build(unrelated_packet(64)),
                                     build(unrelated_packet(8196))])

    def test_at_most_eight_summaries(self):
        steps = [owner(unrelated_packet(command=UNRELATED_CMD + k)) for k in range(10)] + [owner(rail_packet())]
        (_, (result, _, _, seen)), _, _ = self.both(RailRig(steps), write())
        self.assertEqual(result, (True, 0))
        self.assertEqual(seen, [summary(unrelated_packet(command=UNRELATED_CMD + k)) for k in range(8)])

    def test_rearming_stops_at_the_sixteenth_call(self):
        steps = [owner(unrelated_packet(command=UNRELATED_CMD + k)) for k in range(17)] + [owner(rail_packet())]
        (_, (result, error, proto, seen)), calls, _ = self.both(RailRig(steps), write())
        self.assertEqual(result, (False, None))
        self.assertEqual(error, no_packet(0))
        self.assertIsNone(proto)
        self.assertEqual(len(seen), 8)
        states = [entry[1] for entry in calls if entry[0] == "Patched"]
        self.assertEqual(states, [True] * 16 + [False] * 3)  # entry, 15 re-arms, then direct calls
        self.assertEqual(sum(1 for entry in calls if entry[0] == "Escape"), 18)  # the GET went direct

    def test_a_foreign_thread_aborts_without_a_set(self):
        for label, steps in (("foreign GET", [foreign(rail_packet())]),
                             ("foreign then owner", [foreign(unrelated_packet()), owner(rail_packet())])):
            with self.subTest(label):
                (_, (result, error, proto, seen)), calls, _ = self.both(RailRig(steps), write())
                self.assertEqual(result, (False, None))
                self.assertEqual(error, FOREIGN)
                self.assertIsNone(proto)
                self.assertEqual(seen, [])  # a foreign escape is never inspected
                self.assertEqual(calls[3], armed(False))  # and never re-arms
                self.assertFalse(calls[2][-1])  # forwarded from the foreign thread
                escapes = [entry for entry in calls if entry[0] == "Escape"]
                self.assertTrue(all(entry[7][14 * 4:15 * 4] != (0x2080F214).to_bytes(4, "little")
                                    for entry in escapes))

    def test_a_foreign_thread_after_the_set_keeps_the_result_and_reports_itself(self):
        (_, (result, error, proto, _)), calls, _ = self.both(
            RailRig([owner(rail_packet()), foreign_forced(unrelated_packet())]), write())
        self.assertEqual(result, (True, 0))
        self.assertEqual(error, FOREIGN)
        self.assertEqual(proto, protocol(1104))

    def test_never_more_than_one_set(self):
        steps = [owner(rail_packet()), owner(rail_packet()), forced(rail_packet()), owner(rail_packet())]
        (_, (result, error, _, seen)), calls, _ = self.both(RailRig(steps), write())
        self.assertEqual((result, error), ((True, 0), None))
        self.assertEqual(len(seen), 1)  # once done, nothing more is inspected
        sets = [entry for entry in calls if entry[0] == "Escape"
                and entry[7][14 * 4:15 * 4] == (0x2080F214).to_bytes(4, "little")]
        self.assertEqual(len(sets), 1)
        self.assertEqual(sum(1 for entry in calls if entry[0] == "Escape"), 5)


def foreign_forced(make):
    """A foreign thread entering the thunk after the SET (the entry is no longer patched)."""
    return foreign(make)._replace(force=True)


class GetFailureTests(HookCase):
    def refused(self, rig, message, records=None, boost=25):
        (_, (result, error, proto, seen)), calls, _ = self.both(rig, write(records, boost=boost))
        self.assertEqual(result, (False, None))
        self.assertEqual(error, message)
        self.assertIsNone(proto)
        self.assertEqual(len(seen), 1)
        escapes = [entry for entry in calls if entry[0] == "Escape"]
        self.assertEqual(len(escapes), 1)  # the GET only
        self.assertEqual(calls[-1], armed(False))  # an aborted session never re-arms

    def test_get_ntstatus(self):
        self.refused(RailRig([owner(rail_packet())], get_status=STATUS_UNSUCCESSFUL),
                     PRESERVE + "native GET failed (NTSTATUS 0xC0000001, RM 0x00000000)")
        self.refused(RailRig([owner(rail_packet())], get_status=-1),
                     PRESERVE + "native GET failed (NTSTATUS 0xFFFFFFFF, RM 0x00000000)")

    def test_get_rm_status(self):
        self.refused(RailRig([owner(rail_packet())], get_rm=0x57),
                     PRESERVE + "native GET failed (NTSTATUS 0x00000000, RM 0x00000057)")

    def test_response_of_another_layout_or_mask(self):
        self.refused(RailRig([owner(rail_packet())], response_params=904),
                     PRESERVE + "native GET response does not match its request layout and mask")

    def test_response_of_another_size(self):
        self.refused(RailRig([owner(rail_packet())], response_size=1100),
                     PRESERVE + "native GET changed the packet buffer or size")

    def test_prepare_write_refuses_boost_101(self):
        self.refused(RailRig([owner(rail_packet())]), PRESERVE + "boost must be an integer from 0 through 100",
                     boost=101)


class SetFailureTests(HookCase):
    def test_set_ntstatus_and_rm_status_are_reported_not_success(self):
        for kw, status in ((dict(set_status=STATUS_UNSUCCESSFUL), 0xC0000001), (dict(set_status=-1), 0xFFFFFFFF),
                           (dict(set_rm=0x57), 0x57)):
            with self.subTest(**kw):
                (_, (result, error, proto, _)), calls, _ = self.both(RailRig([owner(rail_packet())], **kw), write())
                self.assertEqual(result, (True, status))
                self.assertIsNone(error)
                self.assertEqual(proto, protocol(1104))
                self.assertEqual(sum(1 for entry in calls if entry[0] == "Escape"), 2)


class InstallTests(HookCase):
    def test_virtual_protect_failure_writes_nothing(self):
        (_, (result, error, proto, seen)), calls, (final,) = self.both(
            RailRig([owner(rail_packet())], fail_protect=True), write())
        self.assertEqual((result, error, proto, seen), ((False, None), NO_PROTECT, None, []))
        self.assertEqual(calls, [])
        self.assertEqual(final, build(rail_packet()))

    def test_no_recognized_packet_reports_the_getter_status(self):
        for status in (0, -1, 7):
            with self.subTest(status=status):
                (_, (result, error, proto, seen)), calls, _ = self.both(
                    RailRig([owner(unrelated_packet())], status=status), write())
                self.assertEqual(result, (False, None))
                self.assertEqual(error, no_packet(status))
                self.assertIsNone(proto)
                self.assertEqual(seen, [summary(unrelated_packet())])
                self.assertEqual(calls[-1], armed(True))  # still armed when the getter returned

    def test_a_get_for_another_mask_is_not_recognized(self):
        (_, (result, error, _, _)), _, _ = self.both(RailRig([owner(rail_packet(mask=3))]), write())
        self.assertEqual((result, error), ((False, None), no_packet(0)))

    def test_the_getter_block_carries_version_and_mask(self):
        _, calls, _ = self.both(RailRig([]), write({0: RECORD, 1: RECORD}, control_version=0x10AC8))
        self.assertEqual(calls[0], ("VoltRailsCtlGet", HANDLE, 0x10AC8, 3))

    def test_the_writer_needs_only_the_escape_export(self):
        # Neither side checks the adapter APIs here: nvbackend takes gdi32's D3DKMTEscape directly
        # (never _legacy_clk_gdi), and the core asks only for the escape export (open question 3).
        for name in (ENUM2, "D3DKMTQueryAdapterInfo", "D3DKMTCloseAdapter"):
            with self.subTest(name):
                (_, (result, error, _, _)), _, _ = self.both(RailRig([owner(rail_packet())], missing={name}),
                                                             write())
                self.assertEqual((result, error), ((True, 0), None))


class NativeOnlyTests(HookCase):
    def native_write(self, rig, *args):
        rig.reset()
        gpu = rig.native_gpu()
        try:
            gpu.capabilities()
            rig.calls.clear()
            result = gpu._rail_write_locked(*args)
            attributes = (gpu._volt_rail_write_error, gpu._volt_rail_write_protocol, gpu._volt_rail_observed_packets)
        finally:
            gpu.close()
        self.assertEqual(rig.errors, [])
        self.assertFalse(native.hook_test_state()["installed"])
        return result, attributes

    def test_records_that_do_not_match_the_mask_are_refused_by_prepare_write(self):
        # Not reachable on the Python twin: _write_rail_records_locked derives the mask from the
        # records. The core takes both, and prepare_write's refusal surfaces as Python's would.
        rig = RailRig([owner(rail_packet(mask=1))])
        result, (error, proto, seen) = self.native_write(rig, 0x20AC8, 1, {1: RECORD}, 25)
        self.assertEqual(result, (False, None))
        self.assertEqual(error, PRESERVE + "requested_mask must exactly match the record rails")
        self.assertIsNone(proto)
        self.assertEqual(len([c for c in rig.calls if c[0] == "Escape"]), 1)

    def test_escape_export_missing(self):
        # Recorded divergence: nvbackend reads gdi.D3DKMTEscape unguarded and raises AttributeError
        # (OSError when gdi32 does not load); the core returns (False, None) with the capability
        # detail as the write error and never calls the getter.
        for label, kw, detail in (("D3DKMTEscape missing", dict(missing={ESCAPE}),
                                   "XPDM: no D3DKMT escape path; driver escapes are not possible on this image"),
                                  ("gdi32 not loadable", dict(gdi=False), "gdi32.dll not loadable: ")):
            with self.subTest(label):
                rig = RailRig([owner(rail_packet())], **kw)
                rig.reset()
                gpu = rig.python_gpu()
                expected = AttributeError if ESCAPE in kw.get("missing", ()) else OSError
                with self.assertRaises(expected):
                    rig.run(write())(gpu)
                self.assertEqual(rig.calls, [])
                result, (error, proto, seen) = self.native_write(rig, 0x20AC8, 1, {0: RECORD}, 25)
                self.assertEqual(result, (False, None))
                self.assertTrue(error.startswith(detail), error)
                self.assertEqual((proto, seen, rig.calls), (None, [], []))

    def test_out_of_range_record_values_are_refused_before_the_abi(self):
        # The ABI record carries int32 values, so the binding applies prepare_write's range check
        # itself. Both sides refuse with the same text and issue no SET; the one recorded
        # difference is that Python's refusal happens inside the hook, after the GET, while the
        # binding refuses before any escape is issued.
        for label, values, message in (("above int32", [2 ** 31, 0, 0, 0],
                                        "rail 0 record values must be signed int32 integers"),
                                       ("below int32", [0, -(2 ** 31) - 1, 0, 0],
                                        "rail 0 record values must be signed int32 integers"),
                                       ("bool", [True, 0, 0, 0],
                                        "rail 0 record values must be signed int32 integers")):
            with self.subTest(label):
                rig = RailRig([owner(rail_packet())])
                rig.reset()
                python = rig.run(write({0: values}))(rig.python_gpu())
                self.assertEqual(python[1][:2], ((False, None), PRESERVE + message))
                self.assertEqual(len([c for c in rig.calls if c[0] == "Escape"]), 1)
                rig.reset()
                result, (error, proto, seen) = self.native_write(rig, 0x20AC8, 1, {0: values}, 25)
                self.assertEqual((result, error, proto, seen), ((False, None), PRESERVE + message, None, []))
                self.assertEqual(rig.calls, [])


if __name__ == "__main__":
    unittest.main()
