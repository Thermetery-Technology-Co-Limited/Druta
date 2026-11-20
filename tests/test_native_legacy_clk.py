# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native legacy frequency-lock readback, increment 3 of the RM escape
transport (nvbackend.GPU._legacy_clk_limit_records and the Turing fallback
_read_legacy_clk_lock inside read_clk_lock / read_clk_lock_status), against
its Python twin.

This is where the hook (the VoltRailsCtlGet capture of the 716-byte escape
header) and the raw escape (RM GET 0x20802077 on the adapter at the paired
slot) meet, so one rig combines HookRig (the getter and the hook-aware driver
model of test_native_rm_transport) with LockRig (the BoostLock table of
test_native_vflock). The fake D3DKMTEscape answers 0x20802077 by reading
words 19-20 of the 21-word packet as the records address, writing the two
82-word records there and replying in words 16-17. Every case compares the
result, the cached transport, and the complete call log (BoostLock, the
getter, every gdi call with the packet header words, and the record buffer
the driver was handed)."""
import ctypes
import threading
from collections import namedtuple
import unittest

from druta import native, nvbackend
from tests.test_native_rm_transport import (FIELDS, LEGACY_GETTER_CALL, HookCase, HookRig, armed, foreign,
                                            legacy_header, legacy_packet, owner)
from tests.test_native_transport import ESCAPE, KMT, closes, queries
from tests.test_native_vflock import LockRig, entry

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

NV = nvbackend
u32 = ctypes.c_uint32
RECORDS_GET = 0x20802077
TURING = NV.GPU.ARCH_TURING
STATUS_UNSUCCESSFUL = -1073741823  # 0xC0000001 as a signed NTSTATUS
AMBIGUOUS = "legacy frequency-lock state is unreadable or ambiguous"

Reply = namedtuple("Reply", "status rm count records")


def records(low=1200, high=1500, *, mode=2):
    """test_legacy_frequency_lock.records: the two RM records the driver returns."""
    rows = [[0] * 82 for _ in range(2)]
    for row, ident, mhz in zip(rows, (0x4C, 0x4B), (low, high)):
        row[0], row[1], row[3], row[4] = ident, mode, mhz * 1000, 1
    return rows


def ok(rows=None):
    return Reply(0, 0, 2, records() if rows is None else rows)


class EscapeAt(KMT):
    """gdi32's D3DKMTEscape for the Python twin: its address is the hook's code buffer (what the
    capture patches) and calling it, as _legacy_clk_escape does, reaches the rig's real escape."""
    _flags_, _restype_, _argtypes_ = KMT._flags_, KMT._restype_, KMT._argtypes_

    def __call__(self, argument):
        return self.rig.forward(ctypes.addressof(argument._obj))


class LegacyRig(HookRig, LockRig):
    """HookRig (VoltRailsCtlGet emitting the 716-byte legacy escape through the hook) plus
    LockRig (the BoostLock table, mode 0 by default) on a Turing card.

    replies: what each 0x20802077 escape answers, in order; then `default`
    """

    def __init__(self, *, replies=(), default=None, steps=None, arch=TURING, table=None, **kw):
        self.reply_script = list(replies)
        self.default_reply = ok() if default is None else default
        self.replies = []
        self.boost_table = table
        rounds = kw.pop("rounds", None)
        if rounds is None:
            rounds = [[owner(legacy_packet())] if steps is None else steps]
        super().__init__(rounds=rounds, arch=arch, **kw)

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            status = 0
            try:
                e = NV.GPU._Escape.from_address(pointer)
                n = e.PrivateDriverDataSize // 4
                w = (u32 * n).from_address(e.pPrivateDriverData)
                head, buffer = tuple(w[:min(n, 19)]), None
                if n == 21 and w[14] == RECORDS_GET:
                    target = ((u32 * 82) * 2).from_address(w[19] | w[20] << 32)
                    # fresh storage: the two identifiers and nothing else
                    buffer = (target[0][0], target[1][0], not any(v for row in target for v in row[1:]))
                    reply = self.replies.pop(0) if self.replies else self.default_reply
                    if reply.records is not None:
                        for row, values in zip(target, reply.records):
                            row[:] = values
                    w[16], w[17], status = reply.rm, reply.count, reply.status
                self.calls.append(("Escape", e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext,
                                   e.PrivateDriverDataSize, head, buffer, self.probe(),
                                   threading.current_thread() is threading.main_thread()))
            except BaseException as exc:
                self.errors.append(repr(exc))
            return status
        table[ESCAPE] = escape
        return table

    def reset(self):
        HookRig.reset(self)
        LockRig.reset(self)
        self.replies = list(getattr(self, "reply_script", ()))
        if getattr(self, "boost_table", None) is not None:
            self.table = [dict(row) for row in self.boost_table]

    def state(self):
        return HookRig.state(self)

    def python_windll(self):
        table = self.gdi_table()
        manager = super().python_windll()
        if table is None or ESCAPE not in table:
            return manager
        loader = manager.kwargs["side_effect"]

        def load(name, *args, **kwargs):
            dll = loader(name, *args, **kwargs)
            if name != "kernel32":
                dll.D3DKMTEscape = EscapeAt(ctypes.addressof(self.code))
                dll.D3DKMTEscape.rig = self
            return dll
        manager.kwargs["side_effect"] = load
        return manager


# ---- expected log entries -------------------------------------------------------------------------

def capture_log(marks=(4455, 6677)):
    words = [0] * 19
    words[2], words[14], words[15], words[17] = 716, 0x20803213, 648, 1
    words[12], words[13] = marks
    return ("Escape", 987, 0, 0, 8, 0, 716, tuple(words), None, False, True)


def capture(marks=(4455, 6677)):
    return [LEGACY_GETTER_CALL, armed(True), capture_log(marks), armed(False)]


def records_get(header=None, fields=FIELDS, handle=111):
    words = list(legacy_header() if header is None else header) + [2, 0]
    words[2], words[14], words[15], words[16] = 84, RECORDS_GET, 16, 0
    return ("Escape", handle, fields["hDevice"], fields["Type"], fields["Flags"], fields["hContext"], 84,
            tuple(words), (0x4C, 0x4B, True), False, True)


def read(header=None, fields=FIELDS, handle=111, adapters=(111, 222, 333)):
    return ([("EnumAdapters2",)] + queries(*adapters) + [records_get(header, fields, handle)]
            + closes(*adapters))


BOOST = [("BoostLock",)]


def clk(gpu):
    return gpu.read_clk_lock(), getattr(gpu, "_legacy_clk_transport", None)


def clk_status(gpu):
    return gpu.read_clk_lock_status(), getattr(gpu, "_legacy_clk_transport", None)


def raw_records(gpu):
    return gpu._legacy_clk_limit_records(), getattr(gpu, "_legacy_clk_transport", None)


TRANSPORT = (legacy_header(), FIELDS)


class LegacyRecordsTests(HookCase):
    def test_records_through_a_fresh_capture(self):
        rig = LegacyRig()
        (_, (rows, transport)), calls, unchanged = self.both(rig, raw_records)
        self.assertEqual(rows, records())
        self.assertEqual(transport, TRANSPORT)
        self.assertEqual(calls, capture() + read())
        self.assertEqual(unchanged, [True])  # the hook never touched the getter's packet

    def test_read_clk_lock_on_turing_with_mode_zero_boost_rows(self):
        rig = LegacyRig()
        (_, (value, transport)), calls, _ = self.both(rig, clk)
        self.assertEqual(value, (1200, 1500))
        self.assertEqual(transport, TRANSPORT)
        self.assertEqual(calls, BOOST + capture() + read())

    def test_the_transport_is_cached(self):
        rig = LegacyRig(replies=[ok(), ok(records(1350, 1650))])
        (_, (first, second)), calls, _ = self.both(rig, lambda g: (clk(g), clk(g)))
        self.assertEqual(first, ((1200, 1500), TRANSPORT))
        self.assertEqual(second, ((1350, 1650), TRANSPORT))
        self.assertEqual(calls, BOOST + capture() + read() + BOOST + read())  # no second getter call

    def test_captured_fields_pass_through_and_the_adapter_is_the_one_at_the_slot(self):
        rig = LegacyRig(bus=2)
        (_, (value, _)), calls, _ = self.both(rig, clk)
        self.assertEqual(value, (1200, 1500))
        self.assertEqual(calls[-4], records_get(handle=222))

    def test_an_assigned_transport_is_used_without_a_capture(self):
        header = [0] * 17
        header[12:14] = [123, 456]
        fields = dict(hAdapter=7, hDevice=8, Type=0x22, Flags=0x33, hContext=0x44)

        def action(gpu):
            gpu._legacy_clk_transport = (header, fields)
            return clk(gpu)
        rig = LegacyRig()
        (_, (value, transport)), calls, _ = self.both(rig, action)
        self.assertEqual(value, (1200, 1500))
        self.assertEqual(transport, (header, fields))
        self.assertEqual(calls, BOOST + read(header, fields))  # hAdapter 7 replaced by 111

    def test_rejected_reads_drop_the_transport(self):
        cases = {"NTSTATUS": Reply(STATUS_UNSUCCESSFUL, 0, 2, records()),
                 "RM status": Reply(0, 0x1F, 2, records()),
                 "record count 1": Reply(0, 0, 1, records()),
                 "record count 0": Reply(0, 0, 0, None)}
        for label, reply in cases.items():
            with self.subTest(label):
                rig = LegacyRig(replies=[reply])
                (_, (rows, transport)), calls, _ = self.both(rig, raw_records)
                self.assertIsNone(rows)
                self.assertIsNone(transport)
                self.assertEqual(calls, capture() + read())

    def test_the_next_read_recaptures(self):
        rig = LegacyRig(replies=[Reply(STATUS_UNSUCCESSFUL, 0, 2, None)],
                        rounds=[[owner(legacy_packet())], [owner(legacy_packet(marks=(1, 2)))]])
        (_, (first, second)), calls, _ = self.both(rig, lambda g: (clk(g), clk(g)))
        self.assertEqual(first, (None, None))
        self.assertEqual(second, ((1200, 1500), (legacy_header((1, 2)), FIELDS)))
        self.assertEqual(calls, BOOST + capture() + read() + BOOST + capture((1, 2)) + read(legacy_header((1, 2))))

    def test_a_failed_capture_issues_no_records_get(self):
        for label, kw in (("foreign thread first", dict(steps=[foreign(legacy_packet()), owner(legacy_packet())])),
                          ("getter status", dict(status=-5)), ("VirtualProtect fails", dict(fail_protect=True)),
                          ("no VoltRailsCtlGet", dict(volt=False))):
            with self.subTest(label):
                rig = LegacyRig(**kw)
                (_, (value, transport)), calls, _ = self.both(rig, clk)
                self.assertEqual((value, transport), (None, None))
                self.assertNotIn(("EnumAdapters2",), calls)


class MalformedRecordTests(HookCase):
    def test_disabled_or_malformed_records_do_not_invent_a_lock(self):
        # test_legacy_frequency_lock's samples, delivered by the driver. Its [] sample (fewer than
        # two records) cannot come back from the escape: the reader always owns two 82-word rows.
        samples = {"rejected": Reply(STATUS_UNSUCCESSFUL, 0, 2, None),
                   "low above high": ok(records(1500, 1200))}
        for index, value in ((0, 0), (1, 0), (1, 3), (3, 0), (3, 0xFFFFFFFF), (4, 0)):
            rows = records()
            rows[0][index] = value
            samples[f"word {index} = {value:#x}"] = ok(rows)
        for label, reply in samples.items():
            with self.subTest(label):
                rig = LegacyRig(replies=[reply])
                (_, (value, _)), calls, _ = self.both(rig, clk)
                self.assertIsNone(value)
                self.assertEqual(calls, BOOST + capture() + read())

    def test_the_second_record_is_checked_too(self):
        for index, value in ((0, 0x4C), (1, 2 + 1), (3, 10_000_001), (4, 2)):
            rows = records()
            rows[1][index] = value
            with self.subTest(index=index, value=value):
                (_, (result, _)), _, _ = self.both(LegacyRig(replies=[ok(rows)]), clk)
                self.assertIsNone(result)

    def test_bounds_are_inclusive_at_ten_gigahertz(self):
        rows = records()
        rows[0][3] = rows[1][3] = 10_000_000
        (_, (result, _)), _, _ = self.both(LegacyRig(replies=[ok(rows)]), clk)
        self.assertEqual(result, (10000, 10000))


class GenerationTests(HookCase):
    def test_non_turing_architectures_never_read(self):
        for arch in (NV.GPU.ARCH_PASCAL, NV.GPU.ARCH_AMPERE, NV.GPU.ARCH_ADA, None):
            with self.subTest(arch=arch):
                rig = LegacyRig(arch=arch)
                (_, (value, transport)), calls, _ = self.both(rig, clk)
                self.assertEqual((value, transport), (None, None))
                self.assertEqual(calls, BOOST)

    def test_nvapi_disabled_never_reads(self):
        rig = LegacyRig(nvml_devid=0x1B02, python_nvapi_ok=False)
        (_, (value, _)), calls, _ = self.both(rig, clk)
        self.assertIsNone(value)
        self.assertEqual(calls, [])


class ClkLockStatusTests(HookCase):
    def test_mode_two_records_are_the_lock(self):
        rig = LegacyRig()
        (_, (status, _)), calls, _ = self.both(rig, clk_status)
        self.assertEqual(status, ((1200, 1500), None))
        self.assertEqual(calls, BOOST + capture() + read())

    def test_unreadable_records_are_ambiguous(self):
        rig = LegacyRig(default=Reply(STATUS_UNSUCCESSFUL, 0, 2, None),
                        rounds=[[owner(legacy_packet())], [owner(legacy_packet(marks=(1, 2)))]])
        (_, (status, transport)), calls, _ = self.both(rig, clk_status)
        self.assertEqual(status, (None, AMBIGUOUS))
        self.assertIsNone(transport)
        # the lock read drops the transport; the Turing check captures and reads again
        self.assertEqual(calls, BOOST + capture() + read() + capture((1, 2)) + read(legacy_header((1, 2))))

    def test_no_transport_at_all_is_ambiguous(self):
        (_, (status, _)), calls, _ = self.both(LegacyRig(volt=False), clk_status)
        self.assertEqual(status, (None, AMBIGUOUS))
        self.assertEqual(calls, BOOST)

    def test_mode_zero_records_are_unlocked(self):
        rig = LegacyRig(default=ok(records(mode=0)))
        (_, (status, transport)), calls, _ = self.both(rig, clk_status)
        self.assertEqual(status, (None, None))
        self.assertEqual(transport, TRANSPORT)
        self.assertEqual(calls, BOOST + capture() + read() + read())  # one capture, two reads

    def test_malformed_mode_zero_records_are_ambiguous(self):
        for index, value in ((0, 0x4C), (1, 3), (4, 0)):
            rows = records(mode=0)
            rows[1][index] = value
            with self.subTest(index=index):
                (_, (status, _)), _, _ = self.both(LegacyRig(default=ok(rows)), clk_status)
                self.assertEqual(status, (None, AMBIGUOUS))

    def test_other_generations_are_unlocked_without_a_read(self):
        (_, (status, _)), calls, _ = self.both(LegacyRig(arch=NV.GPU.ARCH_AMPERE), clk_status)
        self.assertEqual(status, (None, None))
        self.assertEqual(calls, BOOST)

    def test_modern_mode_two_rows_take_precedence(self):
        table = [entry(d) for d in range(7)]
        table[0], table[1] = entry(0, 2, 1500000), entry(1, 2, 1200000)
        rig = LegacyRig(table=table)
        (_, (status, _)), calls, _ = self.both(rig, clk_status)
        self.assertEqual(status, ((1200, 1500), None))
        self.assertEqual(calls, BOOST)
        (_, (value, _)), calls, _ = self.both(rig, clk)
        self.assertEqual(value, (1200, 1500))
        self.assertEqual(calls, BOOST)


if __name__ == "__main__":
    unittest.main()
