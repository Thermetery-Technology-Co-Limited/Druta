# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native client-power controller, increment A4 (the client group choice, the metadata capture
and the bounded stable read) against client_power_runtime.ClientPowerController._client_group,
_metadata_once, _stable_locked and read_metadata.

native.Gpu._cp_read_metadata runs dc_cpc_read_metadata. The Python original runs the real
ClientPowerController(twin).read_metadata() on the nvbackend.GPU twin of the A3 rig
(tests/test_native_client_power_controller.py, a Card from tests/test_native_target_binding.py).

The driver behind both sides is one of the Python tests' own wire models, used as the RESPONDER of
the A3 rig's fake gdi32 escape: the escape callback wraps the packet as a c_uint32 array of the
packet's length and calls fake._legacy_clk_escape(view, fields) with the escape's own fields. The
fake's transport header and fields are stored as the fixture transport on both sides (the adapter
the rig enumerates at the card's slot carries the fake's hAdapter), so both sides send the fake the
exact packets it expects. Each side gets a fresh fake (its counts and scripted faults start over).
Every case compares the metadata dict (key order included, nested dicts too) or the exception (kind,
message, rm_status), the gdi call log (the escape fields and packet bytes), the controller state, the
transport cache and the fake's own command log.

The models: FakeGpu (modern, tests/test_client_power_runtime.py), ModernCoupledGpu (modern buffers
with a v30 definition, tests/test_client_power_driver_compat.py), LegacyGpu
(tests/test_client_power_legacy.py), TuringGpu with the 228-byte record (policy124) and the
216-byte record (INFO216, tests/test_client_power_turing.py), Rm26Gpu and ExtendedRm26Gpu
(tests/test_client_power_rm26*.py) and, for the A612 compact group no Python test models, the
CompactGpu below.

A transient failure resets the transport cache before the retry, so the retry needs the transport
captured again. Where a case needs that capture to succeed, the Card carries a PowerPolInfo getter
that issues the fake's own power GET through the hook (native.hook_test_entry on the core; on the
twin the real _capture_current_limit_transport runs against a patched kernel32 / WINFUNCTYPE and a
code buffer, as tests/test_native_rm_transport.py does). Without it the capture finds nothing on
either side, which is the transient "no recognized power GET captured".

Not reachable natively or through these fakes, and so covered only by the Python tests:
  - the target identity changing during a capture ("Client-power target changed during metadata
    capture") and the sticky binding refusal (the fake identity cannot change while one core call
    runs). The transport key changing on the retry's fresh capture is reachable, and covered;
  - "Power-role INFO carries no usable policy definition": the role size comes from a fixed
    table, and every table's role size leaves a stride of at least 20 that is a multiple of 4;
  - an IndexError from a client row past the end of INFO (every table's rows lie inside it);
  - "No understood client-power group was accepted" (the last fallback always returns or raises).
Not reachable at all: dc_cpc_read_metadata's DC_E_BUFFER (every size group's payloads fit
dc_cpc_metadata); AbiTests checks the binding's answer to it with a stubbed export.
"""
import ctypes
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from druta import client_power_runtime as cp
from druta import native, nvbackend
from tests.test_client_power_driver_compat import ModernCoupledGpu
from tests.test_client_power_legacy import LegacyGpu
from tests.test_client_power_rm26 import Rm26Gpu
from tests.test_client_power_rm26_extended import ExtendedRm26Gpu
from tests.test_client_power_runtime import FakeGpu, Lock, put
from tests.test_client_power_turing import TuringGpu
from tests.test_native_client_power_controller import NativeSide, PythonSide, Rig, outcome
from tests.test_native_transport import ESCAPE, KMT, install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

NV = nvbackend
u32 = ctypes.c_uint32
GETTER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
POWER_POL_INFO = 0x34206D86
TRANSPORT_COMMANDS = (0x2080A612, 0x20802612)
FIELD_NAMES = ("hAdapter", "hDevice", "Type", "Flags", "hContext")


# ---- the A612 compact group (A612_COMPACT_SIZES), which no Python test models ----------------------

class CompactGpu:
    """The A612 command family with the compact record layout: client rows of 28 bytes at 4, a
    128-byte native INFO record at 60. Every other size is refused with RM 0x1F, as a driver does."""

    sizes = {cp.INFO: 900, cp.ROLE_INFO: 132, cp.NATIVE_INFO: 5392}

    def __init__(self, *, client=11, native=21, architecture=7):
        self._lock = Lock()
        self.architecture = architecture
        self.client, self.native = client, native
        self.calls, self.counts, self.faults = [], {}, {}
        self.info = bytearray((i * 17 + 37) % 256 for i in range(900))
        put(self.info, 0, (1 << client) | (1 << 3))
        self.info[4 + 3 * 28:4 + 3 * 28 + 4] = b"\x99\x55\x77\x09"
        at = 4 + client * 28
        self.info[at:at + 4] = b"\x57\x57\x01\x00"
        put(self.info, at + 4, 123000)
        put(self.info, at + 8, 321000)
        put(self.info, at + 12, 550000)
        self.native_info = bytearray((i * 13 + 43) % 256 for i in range(5392))
        put(self.native_info, 4, (1 << native) | (1 << 6))
        for index, kind, units, bounds in ((native, 10, 0, (123000, 321000, 550000)), (6, 11, 1, (1, 2, 3))):
            at = 60 + index * 128
            self.native_info[at:at + 7] = bytes([0x30, 0x17, 0x28, 0x39, kind, 9, units])
            for k, value in enumerate(bounds):
                put(self.native_info, at + 8 + 4 * k, value)
        header = list(range(17))
        header[0:5] = [0x4E564441, 0x10002, 54420, 0x4E562A2A, 0x0500002B]
        header[5] = header[11] = header[16] = 0
        header[14], header[15] = 0x2080A612, 54352
        self._current_limit_transport = (header, {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0})

    def _rm_power_transport(self):
        return self._current_limit_transport

    def _legacy_clk_escape(self, packet, fields):
        assert self._lock.held and fields == self._current_limit_transport[1]
        command, size = int(packet[14]), int(packet[15])
        assert len(packet) * 4 == packet[2] == 68 + size
        request = bytes(packet)[68:]
        self.calls.append((command, request))
        key = (command, size)
        self.counts[key] = self.counts.get(key, 0) + 1
        event = self.faults.get((command, size, self.counts[key]), {})
        rejected = self.sizes.get(command) != size
        result = bytearray(request)
        if not rejected:
            if command == cp.INFO:
                assert not any(request)
                result[:] = self.info
            elif command == cp.ROLE_INFO:
                assert request == b"\x01" + bytes(131)
                at = 60 + self.native * 128
                result[4:] = self.native_info[at:at + 128]
            elif command == cp.NATIVE_INFO:
                assert not any(request)
                result[:] = self.native_info
        ctypes.memmove(ctypes.addressof(packet) + 68, bytes(result), size)
        packet[5], packet[11], packet[16] = 1, 1, event.get("rm", 0x1F if rejected else 0)
        return 0


# ---- the rig --------------------------------------------------------------------------------------

def held(fake, call):
    """call() with the fake's lock held (its methods assert the caller holds the GPU lock)."""
    lock = fake._lock
    previous, lock.held = lock.held, True
    try:
        return call()
    finally:
        lock.held = previous


class MetaRig(Rig):
    """The A3 rig with a wire model as the escape's responder.

    factory: builds a fresh fake (per side); configure(fake): scripts its faults or mutates its
    buffers. accepted: {command: sizes} the fake is asked about; any other size is refused by the
    rig with RM 0x1F (for models that assert their own sizes). recapture: the Card carries a
    PowerPolInfo getter that issues the fake's power GET, so a reset transport is captured again.
    """

    def __init__(self, factory, *, configure=None, accepted=None, recapture=False, arch=None):
        self.factory, self.configure, self.accepted = factory, configure, accepted
        sample = factory()
        self.header, self.fields = held(sample, sample._rm_power_transport)
        self.header, self.fields = list(self.header), dict(self.fields)
        self.errors, self.fake = [], None
        self.capturing, self.code, self.py_target, self.probe, self.enter = False, None, None, None, None
        super().__init__(arch=sample.architecture if arch is None else arch,
                         adapters=((self.fields["hAdapter"], 1, 0, 0), (222, 2, 0, 0)))
        self.recapture = recapture
        if recapture:
            self.getter = self._getter()
            self.card.nvapi.interfaces[POWER_POL_INFO] = self.getter

    def fresh(self):
        self.fake = self.factory()
        if self.configure:
            self.configure(self.fake)
        self.errors = []

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            try:
                e = NV.GPU._Escape.from_address(pointer)
                size = e.PrivateDriverDataSize
                fields = {name: getattr(e, name) for name in FIELD_NAMES}
                self.calls.append(("Escape", *fields.values(), size, ctypes.string_at(e.pPrivateDriverData, size)))
                return self.respond(e.pPrivateDriverData, size, fields)
            except BaseException as exc:  # an exception would vanish inside the callback
                self.errors.append(repr(exc))
                return -1
        table[ESCAPE] = escape
        return table

    def respond(self, address, size, fields):
        view = (u32 * (size // 4)).from_address(address)
        if view[14] in TRANSPORT_COMMANDS:  # the captured power GET itself: the driver answers it
            return 0
        if self.accepted is not None and view[15] not in self.accepted.get(view[14], ()):
            view[5], view[11], view[16] = 1, 1, 0x1F
            return 0
        return held(self.fake, lambda: self.fake._legacy_clk_escape(view, fields))

    # the PowerPolInfo getter: the driver's own power GET, through the hook while it is patched

    def _getter(self):
        @GETTER
        def getter(handle, block):
            try:
                words = (u32 * (self.header[2] // 4))()
                words[:17] = self.header
                escape = NV.GPU._Escape(**self.fields, pPrivateDriverData=ctypes.addressof(words),
                                        PrivateDriverDataSize=self.header[2])
                address = ctypes.addressof(escape)
                if self.probe():
                    self.enter(address)
                else:
                    self._callbacks[ESCAPE](address)
            except BaseException as exc:
                self.errors.append(repr(exc))
            return 0
        return getter

    # the Python twin's capture: kernel32, WINFUNCTYPE and a code buffer for D3DKMTEscape

    def python_patches(self):
        self.code = ctypes.create_string_buffer(b"original bytes")
        original = bytes(self.code)
        storage = ctypes.create_string_buffer(14)
        self.probe = lambda: bytes(self.code) != original
        self.enter = lambda address: self.py_target(address)
        protects = []

        def protect(pointer, size, flags, previous):
            protects.append(flags)
            if len(protects) % 2 == 0:  # the capture restores the old protection when it is done
                self.capturing = False
            previous._obj.value = 0x20
            return 1
        kernel = SimpleNamespace(VirtualProtect=Mock(side_effect=protect),
                                 GetCurrentThreadId=Mock(side_effect=threading.get_ident),
                                 GetCurrentProcess=Mock(return_value=42),
                                 FlushInstructionCache=Mock(return_value=1))
        rig = self

        class Gdi:
            def __getattr__(self, name):
                if name == ESCAPE and rig.capturing:
                    rig.patched_escape = KMT(ctypes.addressof(rig.code))
                    return rig.patched_escape
                try:
                    return rig.gdi()[name]
                except KeyError:
                    raise AttributeError(name) from None

        def load(name, *args, **kwargs):
            if name == "kernel32":
                self.capturing = True
                return kernel
            return Gdi()

        def prototype(restype, argtype):
            def bind(target):
                if callable(target):  # the capture closure: the thunk's target
                    self.py_target = target
                    return ctypes.c_void_p(ctypes.addressof(storage))
                return self._callbacks[ESCAPE]  # the restored real function
            return bind
        return (patch.object(NV.ctypes, "WinDLL", create=True, side_effect=load),
                patch.object(NV.ctypes, "WINFUNCTYPE", new=prototype, create=True))


class MetaPython(PythonSide):
    def read(self):
        return self.controller.read_metadata()


class MetaNative(NativeSide):
    def read(self):
        return self.gpu._cp_read_metadata()


class MetadataCase(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)
        native.hook_test_fail_protect(False)
        self.assertFalse(native.hook_test_state()["installed"])

    def both(self, rig, action=None, *, choice=True):
        """action(side) (default: store the fake's transport, then one read_metadata) on the Python
        original, then on the core, each with a fresh fake. Returns what action returned, the gdi
        call log, the controller state, the transport cache and the fake's own command log."""
        def default(side):
            store(side, rig, choice)
            return outcome(side.read)
        action = action or default
        runs = []
        for kind in ("python", "native"):
            rig.fresh()
            rig.calls = []
            install(rig.card.nvml, rig.card.nvapi, rig.gdi())
            opened = native.Gpu()
            try:
                if kind == "python":
                    twin = rig.card.python_twin(opened)
                    if rig.recapture:
                        twin.nvapi.PowerPolInfo = rig.getter
                    side = MetaPython(twin)
                    windll, winfunctype = rig.python_patches()
                    with windll, winfunctype:
                        result = action(side)
                else:
                    rig.probe = lambda: native.hook_test_state()["patched"]
                    rig.enter = native.hook_test_entry
                    side = MetaNative(opened)
                    result = action(side)
                runs.append((result, list(rig.calls), side.state(), side.cache(), list(rig.fake.calls)))
            finally:
                opened.close()
            self.assertEqual(rig.errors, [], f"the {kind} run raised inside the fake driver")
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        # dict equality ignores insertion order; the metadata dict promises Python's, nested keys included
        self.assertEqual(ordered(runs[0][0]), ordered(runs[1][0]), "Python and native key orders differ")
        return runs[0]


def ordered(value):
    """value with every dict turned into its (key, value) list, so == also compares key order."""
    if isinstance(value, dict):
        return [(key, ordered(item)) for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        return type(value)(ordered(item) for item in value)
    return value


def store(side, rig, choice=True):
    side.gpu._current_limit_transport = (list(rig.header), dict(rig.fields))
    if isinstance(side, MetaPython):
        side.gpu._current_limit_layout_choice = {"seeded": True} if choice else None
    else:
        side.gpu._current_limit_layout_choice = 0 if choice else None


def commands(fake_calls):
    """(command, size) of each escape the fake answered."""
    return [(command, len(request)) for command, request in fake_calls]


def legacy_accepted():
    accepted = {command: {cp.LEGACY_SIZES[command]} for command in cp.LEGACY_SIZES}
    accepted[cp.INFO].add(cp.SIZES[cp.INFO])  # LegacyGpu refuses the modern INFO itself
    return accepted


MODERN = {command: {cp.SIZES[command]} for command in cp.SIZES}


def state(**changes):
    base = {"sizes": "SIZES", "legacy": False, "native_policy124": False, "role_info216": False, "rm26": False,
            "rm26_records": False, "rm26_extended": False, "transport_key": None}
    return dict(base, **changes)


def rm_error(command, rm):
    kind = "unsupported" if rm in (0x1F, 0x3A, 0x56, 0x40) else "transient"
    return ("ClientPowerError", kind, f"Client-power command {command:#x} rejected (RM {rm:#x})", rm)


A612_KEY = (0x2080A612, 54420, 54352)
TURING_KEY = (0x2080A612, 11748, 11680)


# ---- the happy path on every model --------------------------------------------------------------

class HappyPathTests(MetadataCase):
    def check(self, rig, *, binding_extra, expected_state, client, native_index, limits):
        result, calls, final, cache, fake_calls = self.both(rig)
        self.assertEqual(result[0], "ok", result)
        metadata = result[1]
        self.assertEqual(list(metadata), ["schema", "stable_reads", "identity", "binding", "info_hex",
                                          "role_info_hex", "native_info_hex"])
        self.assertEqual((metadata["schema"], metadata["stable_reads"]), (1, 2))
        self.assertEqual(metadata["identity"], {"slot": "0000:01:00.0", "architecture": rig.card.arch,
                                                "device_id": 0x1E02, "subsystem_id": 0x12A010DE})
        binding = {"client_index": client, "native_index": native_index, "semantic_role": 1, "client": 0xFE,
                   "units": "mW", "minimum_mw": limits[0], "rated_mw": limits[1], "maximum_mw": limits[2],
                   **binding_extra}
        self.assertEqual(metadata["binding"], binding)
        self.assertEqual(list(metadata["binding"]), list(binding))  # Python's insertion order
        self.assertEqual(final, expected_state)
        self.assertEqual(cache, ((rig.header, rig.fields), True))
        return metadata, fake_calls

    def test_modern(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)
        metadata, fake_calls = self.check(rig, binding_extra={}, expected_state=state(transport_key=A612_KEY),
                                          client=37, native_index=31, limits=(200000, 321000, 550000))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.ROLE_INFO, 0x108), (cp.NATIVE_INFO, 20512)] * 2)
        self.assertEqual(bytes.fromhex(metadata["role_info_hex"])[4], 0x40)

    def test_modern_with_a_v30_definition(self):
        rig = MetaRig(ModernCoupledGpu, accepted=MODERN)
        self.check(rig, binding_extra={}, expected_state=state(transport_key=A612_KEY), client=63, native_index=29,
                   limits=(200000, 321000, 550000))

    def test_legacy(self):
        rig = MetaRig(LegacyGpu, accepted=legacy_accepted())
        _, fake_calls = self.check(rig, binding_extra={"layout": "legacy32"},
                                   expected_state=state(sizes="LEGACY_SIZES", legacy=True, transport_key=A612_KEY),
                                   client=3, native_index=19, limits=(200000, 321000, 550000))
        # the modern INFO once, refused; the legacy group from then on, never re-probed
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.ROLE_INFO, 232),
                                                (cp.NATIVE_INFO, 8632), (cp.INFO, 1160), (cp.ROLE_INFO, 232),
                                                (cp.NATIVE_INFO, 8632)])

    def test_turing_policy124(self):
        rig = MetaRig(TuringGpu)
        _, fake_calls = self.check(rig, binding_extra={"layout": "legacy32", "native_layout": "policy124"},
                                   expected_state=state(sizes="TURING_SIZES", legacy=True, native_policy124=True,
                                                        transport_key=TURING_KEY),
                                   client=5, native_index=17, limits=(180000, 321000, 550000))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.ROLE_INFO, 232),
                                                (cp.NATIVE_INFO, 8632), (cp.NATIVE_INFO, 8620),
                                                (cp.INFO, 1160), (cp.ROLE_INFO, 232), (cp.NATIVE_INFO, 8620)])

    def test_info216_records_the_stride(self):
        rig = MetaRig(lambda: TuringGpu(record=216))
        _, fake_calls = self.check(rig, binding_extra={"layout": "legacy32", "native_layout": "policy124",
                                                       "native_info_stride": 216},
                                   expected_state=state(sizes="INFO216_SIZES", legacy=True, role_info216=True,
                                                        transport_key=TURING_KEY),
                                   client=5, native_index=17, limits=(180000, 321000, 550000))
        # the role GET refused for its size once; INFO216 then holds, and TURING is never tried
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.ROLE_INFO, 232),
                                                (cp.ROLE_INFO, 220), (cp.NATIVE_INFO, 8236),
                                                (cp.INFO, 1160), (cp.ROLE_INFO, 220), (cp.NATIVE_INFO, 8236)])

    def test_rm26(self):
        rig = MetaRig(Rm26Gpu)
        self.check(rig, binding_extra={"layout": "rm26"},
                   expected_state=state(sizes="RM26_SIZES", rm26=True, rm26_records=True,
                                        transport_key=(0x20802612, 2524, 2456)),
                   client=11, native_index=31, limits=(123000, 321000, 550000))

    def test_rm26_extended(self):
        rig = MetaRig(ExtendedRm26Gpu)
        self.check(rig, binding_extra={"layout": "rm26", "native_layout": "rm26_extended"},
                   expected_state=state(sizes="RM26_EXTENDED_SIZES", rm26=True, rm26_records=True,
                                        rm26_extended=True, transport_key=(0x20802612, 3548, 3480)),
                   client=11, native_index=31, limits=(123000, 321000, 550000))

    def test_a612_compact_records_the_stride(self):
        rig = MetaRig(CompactGpu)
        _, fake_calls = self.check(rig, binding_extra={"layout": "rm26", "native_layout": "rm26_extended",
                                                       "native_info_stride": 128},
                                   expected_state=state(sizes="A612_COMPACT_SIZES", rm26_records=True,
                                                        rm26_extended=True, transport_key=A612_KEY),
                                   client=11, native_index=21, limits=(123000, 321000, 550000))
        # modern refused, legacy refused, compact accepted
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.INFO, 900), (cp.ROLE_INFO, 132),
                                                (cp.NATIVE_INFO, 5392), (cp.INFO, 900), (cp.ROLE_INFO, 132),
                                                (cp.NATIVE_INFO, 5392)])

    def test_escape_packets_carry_the_fake_transport(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)
        _, calls, *_ = self.both(rig)
        escapes = [c for c in calls if c[0] == "Escape"]
        self.assertEqual(len(escapes), 6)
        for entry in escapes:
            self.assertEqual(entry[1:6], tuple(rig.fields.values()))
            words = (u32 * 17).from_buffer_copy(entry[7][:68])
            self.assertEqual([words[i] for i in (0, 1, 3, 4)], [rig.header[i] for i in (0, 1, 3, 4)])


# ---- the client group fallbacks -----------------------------------------------------------------

class ClientGroupTests(MetadataCase):
    def test_a_transient_during_a_fallback_restores_the_previous_sizes(self):
        def configure(fake):
            fake.faults[(cp.INFO, 2)] = {"rm": 0x99}  # the legacy probe
        rig = MetaRig(LegacyGpu, configure=configure, accepted=legacy_accepted())
        result, _, final, cache, fake_calls = self.both(rig)
        # the retry finds no transport to capture once the cache is reset
        self.assertEqual(result, ("ClientPowerError", "transient",
                                  "unsupported current-policy transport: no recognized power GET captured", None))
        self.assertEqual(final, state(transport_key=A612_KEY))  # no group half-selected
        self.assertEqual(cache, (None, False))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160)])

    def test_the_last_fallback_refused_is_raised(self):
        def configure(fake):
            fake.faults[(cp.INFO, 2)] = {"rm": 0x1F}  # legacy refused; compact refused by the rig
        rig = MetaRig(LegacyGpu, configure=configure, accepted=legacy_accepted())
        result, calls, final, cache, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.INFO, 0x1F))
        self.assertEqual(final, state(transport_key=A612_KEY))
        self.assertEqual(cache, ((rig.header, rig.fields), True))  # not transient: no reset
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160)])
        self.assertEqual([len(c[7]) - 68 for c in calls if c[0] == "Escape"], [0x924, 1160, 900])

    def test_a_definite_0x3a_refusal_also_falls_back(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1)] = {"rm": 0x3A}
        rig = MetaRig(LegacyGpu, configure=configure, accepted=legacy_accepted())
        result, *_ = self.both(rig)
        self.assertEqual(result[0], "ok")
        self.assertEqual(result[1]["binding"]["layout"], "legacy32")

    def test_a_generation_outside_the_legacy_list_raises_the_refusal(self):
        rig = MetaRig(lambda: LegacyGpu(architecture=8), accepted=legacy_accepted())
        result, _, final, _, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.INFO, 0x1F))
        self.assertEqual(final, state(transport_key=A612_KEY))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924)])

    def test_only_pascal_turing_and_ampere_fall_back(self):
        # every generation the binding accepts (4 through 10): Volta (5) is the gap inside the list
        for arch in range(4, 11):
            with self.subTest(arch=arch):
                rig = MetaRig(lambda arch=arch: LegacyGpu(architecture=arch), accepted=legacy_accepted())
                result, _, final, _, fake_calls = self.both(rig)
                if arch in (4, 6, 7):
                    self.assertEqual(result[0], "ok")
                    self.assertEqual(result[1]["binding"]["layout"], "legacy32")
                    self.assertEqual(final, state(sizes="LEGACY_SIZES", legacy=True, transport_key=A612_KEY))
                else:
                    self.assertEqual(result, rm_error(cp.INFO, 0x1F))
                    self.assertEqual(final, state(transport_key=A612_KEY))
                    self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924)])

    def test_a_0x3a_refusal_at_the_legacy_probe_moves_on_to_compact(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1160, 1)] = {"rm": 0x3A}
        rig = MetaRig(CompactGpu, configure=configure)
        result, _, final, _, fake_calls = self.both(rig)
        self.assertEqual(result[0], "ok")
        self.assertEqual(final, state(sizes="A612_COMPACT_SIZES", rm26_records=True, rm26_extended=True,
                                      transport_key=A612_KEY))
        self.assertEqual(commands(fake_calls)[:3], [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.INFO, 900)])

    def test_a_transient_at_the_compact_probe_restores_the_previous_sizes(self):
        def configure(fake):
            fake.faults[(cp.INFO, 900, 1)] = {"rm": 0x99}
        rig = MetaRig(CompactGpu, configure=configure)
        result, _, final, cache, fake_calls = self.both(rig)
        self.assertEqual(result, ("ClientPowerError", "transient",
                                  "unsupported current-policy transport: no recognized power GET captured", None))
        self.assertEqual(final, state(transport_key=A612_KEY))  # no group half-selected
        self.assertEqual(cache, (None, False))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.INFO, 900)])

    def test_the_retry_after_a_transient_fallback_probe_starts_again_from_the_modern_group(self):
        def compact(fake):
            fake.faults[(cp.INFO, 900, 1)] = {"rm": 0x99}

        def legacy(fake):
            fake.faults[(cp.INFO, 2)] = {"rm": 0x99}
        cases = ((CompactGpu, compact, None, (cp.INFO, 900), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.INFO, 900)]),
                 (LegacyGpu, legacy, legacy_accepted(), (cp.INFO, 1160), [(cp.INFO, 0x924), (cp.INFO, 1160)]))
        for factory, configure, accepted, chosen, probes in cases:
            with self.subTest(factory.__name__):
                rig = MetaRig(factory, configure=configure, accepted=accepted, recapture=True)
                result, _, _, cache, fake_calls = self.both(rig)
                self.assertEqual(result[0], "ok")
                self.assertEqual(cache, ((rig.header, rig.fields), False))
                # the failed probe, then the whole probe order again, then the chosen group only
                self.assertEqual(commands(fake_calls)[:2 * len(probes)], probes * 2)
                self.assertEqual(commands(fake_calls)[2 * len(probes) + 2], chosen)

    def test_rm26_records_are_never_probed_again(self):
        def rm26(fake):
            fake.faults[(0x20802630, 1)] = {"rm": 0x1F}  # the 26xx INFO, on a generation of the legacy list
        rig = MetaRig(Rm26Gpu, configure=rm26)
        result, _, final, _, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(0x20802630, 0x1F))  # the wire command
        self.assertEqual(final, state(sizes="RM26_SIZES", rm26=True, rm26_records=True,
                                      transport_key=(0x20802612, 2524, 2456)))
        self.assertEqual(commands(fake_calls), [(0x20802630, 644)])

        def compact(fake):
            fake.faults[(cp.INFO, 900, 3)] = {"rm": 0x1F}  # the compact INFO of the next read
        rig = MetaRig(CompactGpu, configure=compact)

        def action(side):
            store(side, rig)
            return outcome(side.read), outcome(side.read)
        (first, second), calls, final, _, fake_calls = self.both(rig, action)
        self.assertEqual(first[0], "ok")
        self.assertEqual(second, rm_error(cp.INFO, 0x1F))
        self.assertEqual(final, state(sizes="A612_COMPACT_SIZES", rm26_records=True, rm26_extended=True,
                                      transport_key=A612_KEY))
        self.assertEqual(commands(fake_calls)[-1], (cp.INFO, 900))
        self.assertEqual(len(fake_calls), 9)

    def test_a_transient_modern_refusal_does_not_fall_back(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1)] = {"rm": 0x99}
            fake.faults[(cp.INFO, 2)] = {"rm": 0x99}
        rig = MetaRig(LegacyGpu, configure=configure, accepted=legacy_accepted(), recapture=True)
        result, _, final, _, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.INFO, 0x99))
        self.assertEqual(final, state(transport_key=A612_KEY))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 0x924)])

    def test_a_chosen_group_is_never_probed_again(self):
        def configure(fake):
            fake.faults[(cp.INFO, 4)] = {"rm": 0x1F}  # the legacy INFO of the next read
        rig = MetaRig(LegacyGpu, configure=configure, accepted=legacy_accepted())

        def action(side):
            store(side, rig)
            return outcome(side.read), outcome(side.read)
        (first, second), _, final, _, fake_calls = self.both(rig, action)
        self.assertEqual(first[0], "ok")
        self.assertEqual(second, rm_error(cp.INFO, 0x1F))
        self.assertEqual(final, state(sizes="LEGACY_SIZES", legacy=True, transport_key=A612_KEY))
        self.assertEqual(commands(fake_calls)[-1], (cp.INFO, 1160))
        self.assertEqual(len(fake_calls), 8)

    def test_a_second_read_reuses_the_selected_groups(self):
        rig = MetaRig(lambda: TuringGpu(record=216))

        def action(side):
            store(side, rig)
            return outcome(side.read), outcome(side.read)
        (first, second), _, final, _, fake_calls = self.both(rig, action)
        self.assertEqual(first, second)
        self.assertEqual(final, state(sizes="INFO216_SIZES", legacy=True, role_info216=True, transport_key=TURING_KEY))
        self.assertEqual(commands(fake_calls)[8:], [(cp.INFO, 1160), (cp.ROLE_INFO, 220), (cp.NATIVE_INFO, 8236)] * 2)


# ---- the ROLE_INFO and NATIVE_INFO retries -------------------------------------------------------

class RetryTests(MetadataCase):
    def test_info216_is_tried_once(self):
        def configure(fake):
            fake.faults[(cp.ROLE_INFO, 220, 1)] = {"rm": 0x1F}
        rig = MetaRig(lambda: TuringGpu(record=216), configure=configure)
        result, _, final, _, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.ROLE_INFO, 0x1F))
        self.assertEqual(final, state(sizes="INFO216_SIZES", legacy=True, role_info216=True, transport_key=TURING_KEY))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.ROLE_INFO, 232),
                                                (cp.ROLE_INFO, 220)])

    def test_a_0x3a_role_refusal_is_not_retried(self):
        def configure(fake):
            fake.faults[(cp.ROLE_INFO, 232, 1)] = {"rm": 0x3A}
        rig = MetaRig(TuringGpu, configure=configure)
        result, _, final, *_ = self.both(rig)
        self.assertEqual(result, rm_error(cp.ROLE_INFO, 0x3A))
        self.assertEqual(final, state(sizes="LEGACY_SIZES", legacy=True, transport_key=TURING_KEY))

    def test_the_modern_group_does_not_retry_the_role(self):
        def configure(fake):
            fake.faults[(cp.ROLE_INFO, 1)] = {"rm": 0x1F}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
        result, _, final, *_ = self.both(rig)
        self.assertEqual(result, rm_error(cp.ROLE_INFO, 0x1F))
        self.assertEqual(final, state(transport_key=A612_KEY))

    def test_turing_is_not_tried_after_info216(self):
        def configure(fake):
            fake.faults[(cp.NATIVE_INFO, 8236, 1)] = {"rm": 0x1F}
        rig = MetaRig(lambda: TuringGpu(record=216), configure=configure)
        result, _, final, _, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.NATIVE_INFO, 0x1F))
        self.assertEqual(final, state(sizes="INFO216_SIZES", legacy=True, role_info216=True, transport_key=TURING_KEY))
        self.assertEqual(commands(fake_calls)[-1], (cp.NATIVE_INFO, 8236))

    def test_turing_is_tried_once(self):
        def configure(fake):
            fake.faults[(cp.NATIVE_INFO, 8620, 1)] = {"rm": 0x1F}
        rig = MetaRig(TuringGpu, configure=configure)
        result, _, final, _, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.NATIVE_INFO, 0x1F))
        self.assertEqual(final, state(sizes="TURING_SIZES", legacy=True, native_policy124=True,
                                      transport_key=TURING_KEY))
        self.assertEqual(commands(fake_calls)[-2:], [(cp.NATIVE_INFO, 8632), (cp.NATIVE_INFO, 8620)])

    def test_only_a_0x1f_native_refusal_tries_turing(self):
        for rm in (0x3A, 0x99):
            with self.subTest(rm=hex(rm)):
                def configure(fake, rm=rm):
                    fake.faults[(cp.NATIVE_INFO, 8632, 1)] = {"rm": rm}
                rig = MetaRig(TuringGpu, configure=configure)
                result, _, final, _, fake_calls = self.both(rig)
                if rm == 0x3A:
                    self.assertEqual(result, rm_error(cp.NATIVE_INFO, 0x3A))
                else:  # transient: the retry finds no transport to capture
                    self.assertEqual(result, ("ClientPowerError", "transient", "unsupported current-policy "
                                              "transport: no recognized power GET captured", None))
                self.assertEqual(final, state(sizes="LEGACY_SIZES", legacy=True, transport_key=TURING_KEY))
                self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 1160), (cp.ROLE_INFO, 232),
                                                        (cp.NATIVE_INFO, 8632)])

    def test_a_selected_retry_group_is_not_retried_on_a_later_read(self):
        # The third GET of the retried size is the first capture of the second read.
        cases = ((lambda: TuringGpu(record=216), (cp.ROLE_INFO, 220),
                  state(sizes="INFO216_SIZES", legacy=True, role_info216=True, transport_key=TURING_KEY)),
                 (TuringGpu, (cp.NATIVE_INFO, 8620),
                  state(sizes="TURING_SIZES", legacy=True, native_policy124=True, transport_key=TURING_KEY)))
        for factory, (command, size), expected in cases:
            with self.subTest(command=hex(command)):
                def configure(fake, command=command, size=size):
                    fake.faults[(command, size, 3)] = {"rm": 0x1F}
                rig = MetaRig(factory, configure=configure)

                def action(side, rig=rig):
                    store(side, rig)
                    return outcome(side.read), outcome(side.read)
                (first, second), _, final, _, fake_calls = self.both(rig, action)
                self.assertEqual(first[0], "ok")
                self.assertEqual(second, rm_error(command, 0x1F))
                self.assertEqual(final, expected)
                self.assertEqual(commands(fake_calls)[-1], (command, size))
                self.assertEqual(commands(fake_calls).count((command, size)), 3)

    def test_a_transient_on_the_retried_get_keeps_the_group_for_the_stable_retry(self):
        cases = ((lambda: TuringGpu(record=216), (cp.ROLE_INFO, 220),
                  [(cp.INFO, 1160), (cp.ROLE_INFO, 220), (cp.NATIVE_INFO, 8236)],
                  state(sizes="INFO216_SIZES", legacy=True, role_info216=True, transport_key=TURING_KEY)),
                 (TuringGpu, (cp.NATIVE_INFO, 8620),
                  [(cp.INFO, 1160), (cp.ROLE_INFO, 232), (cp.NATIVE_INFO, 8620)],
                  state(sizes="TURING_SIZES", legacy=True, native_policy124=True, transport_key=TURING_KEY)))
        for factory, (command, size), capture, expected in cases:
            with self.subTest(command=hex(command)):
                def configure(fake, command=command, size=size):
                    fake.faults[(command, size, 1)] = {"rm": 0x99}
                rig = MetaRig(factory, configure=configure, recapture=True)
                result, _, final, _, fake_calls = self.both(rig)
                self.assertEqual(result[0], "ok")
                self.assertEqual(final, expected)
                # the retry goes straight to the selected group: no 232 role or 8632 native probe again
                sent = commands(fake_calls)
                failed = sent.index((command, size))
                self.assertEqual(sent[failed + 1:], capture * 2)

    def test_the_modern_group_does_not_retry_native_info(self):
        def configure(fake):
            fake.faults[(cp.NATIVE_INFO, 1)] = {"rm": 0x1F}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
        result, *_ = self.both(rig)
        self.assertEqual(result, rm_error(cp.NATIVE_INFO, 0x1F))


# ---- every _metadata_once refusal ----------------------------------------------------------------

ROW = cp.INFO_BASE + 37 * cp.INFO_STRIDE  # FakeGpu's client row
NATIVE_ROW = cp.NATIVE_INFO_BASE + 31 * cp.NATIVE_INFO_STRIDE  # FakeGpu's native policy


def mutate_info(offset, value):
    def configure(fake):
        fake.info[offset] = value
    return configure


def mutate_native(offset, value):
    def configure(fake):
        fake.native_info[NATIVE_ROW + offset] = value
    return configure


def duplicate_native(fake):
    fake.native_mask |= 1 << 5
    put(fake.native_info, 4, fake.native_mask)
    at = cp.NATIVE_INFO_BASE + 5 * cp.NATIVE_INFO_STRIDE
    fake.native_info[at:at + cp.NATIVE_INFO_STRIDE] = fake.native_info[NATIVE_ROW:NATIVE_ROW + cp.NATIVE_INFO_STRIDE]


def second_role1(fake):
    fake.info[4] |= 1 << 2
    fake.info[cp.INFO_BASE + 2 * cp.INFO_STRIDE + 2] = 1


def changed_selector(fake):
    fake.faults[(cp.ROLE_INFO, 1)] = {"payload": lambda raw: raw.__setitem__(0, 2)}


def unbound_definition(fake):
    fake.faults[(cp.ROLE_INFO, 1)] = {"payload": lambda raw: raw.__setitem__(100, raw[100] ^ 0xFF)}


def inconsistent_limits(fake):
    put(fake.info, ROW + 4, 321001)


def native_limits(fake):
    put(fake.native_info, NATIVE_ROW + 16, 550001)


def native_word(offset, value):
    def configure(fake):
        put(fake.native_info, NATIVE_ROW + offset, value)
    return configure


def selector(index, value):
    def configure(fake):
        fake.faults[(cp.ROLE_INFO, 1)] = {"payload": lambda raw: raw.__setitem__(index, value)}
    return configure


MALFORMED, UNSUPPORTED = "malformed", "unsupported"
METADATA_REFUSALS = [
    ("mask beyond the row capacity", mutate_info(4 + 8, 1), MALFORMED,
     "Client-power inventory exceeds the understood row capacity"),
    ("mask beyond the row capacity, last byte", mutate_info(cp.INFO_BASE - 1, 0x80), MALFORMED,
     "Client-power inventory exceeds the understood row capacity"),
    ("no role1 row", mutate_info(ROW + 2, 2), UNSUPPORTED, "No unique role1 client-power policy is available"),
    ("two role1 rows", second_role1, UNSUPPORTED, "No unique role1 client-power policy is available"),
    ("role1 row not in the mask", mutate_info(4 + 37 // 8, 0), UNSUPPORTED,
     "No unique role1 client-power policy is available"),
    ("discriminants", mutate_info(ROW + 1, 84), MALFORMED, "Selected client INFO type discriminants disagree"),
    ("units", mutate_info(ROW + 3, 1), UNSUPPORTED, "Selected client-power policy does not report milliwatts"),
    ("limits", inconsistent_limits, MALFORMED, "Client-power ordinary limits are inconsistent"),
    ("limits, rated above the maximum", lambda fake: put(fake.info, ROW + 8, 600000), MALFORMED,
     "Client-power ordinary limits are inconsistent"),
    ("selector", changed_selector, MALFORMED, "Power-role INFO changed its semantic selector"),
    ("selector byte 1", selector(1, 1), MALFORMED, "Power-role INFO changed its semantic selector"),
    ("selector byte 2", selector(2, 0x80), MALFORMED, "Power-role INFO changed its semantic selector"),
    ("selector byte 3", selector(3, 1), MALFORMED, "Power-role INFO changed its semantic selector"),
    ("no native match", unbound_definition, MALFORMED, "Role1 INFO does not uniquely bind a current native policy"),
    ("two native matches", duplicate_native, MALFORMED, "Role1 INFO does not uniquely bind a current native policy"),
    ("native policy not in the mask", lambda fake: put(fake.native_info, 4, 1 << 2), MALFORMED,
     "Role1 INFO does not uniquely bind a current native policy"),
    ("table version", mutate_native(0, 0x41), MALFORMED, "Client INFO differs from the bound native policy definition"),
    ("native units", mutate_native(6, 1), MALFORMED, "Client INFO differs from the bound native policy definition"),
    ("native limits", native_limits, MALFORMED, "Client INFO differs from the bound native policy definition"),
    ("native minimum", native_word(8, 199999), MALFORMED,
     "Client INFO differs from the bound native policy definition"),
    ("native rated", native_word(12, 321001), MALFORMED,
     "Client INFO differs from the bound native policy definition"),
]


class MetadataRefusalTests(MetadataCase):
    def test_refusals(self):
        for name, configure, kind, message in METADATA_REFUSALS:
            with self.subTest(name):
                rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
                result, _, final, cache, _ = self.both(rig)
                self.assertEqual(result, ("ClientPowerError", kind, message, None))
                self.assertEqual(final, state(transport_key=A612_KEY))
                self.assertEqual(cache, ((rig.header, rig.fields), True))  # not transient: no reset

    def test_a_v30_definition_is_accepted_on_the_modern_group(self):
        rig = MetaRig(FakeGpu, configure=mutate_native(0, 0x30), accepted=MODERN)
        result, *_ = self.both(rig)
        self.assertEqual(result[0], "ok")

    def test_the_legacy_group_refuses_a_v40_definition(self):
        def configure(fake):
            fake.native_info[88 + 19 * 228] = 0x40
        rig = MetaRig(LegacyGpu, configure=configure, accepted=legacy_accepted())
        result, _, final, *_ = self.both(rig)
        self.assertEqual(result, ("ClientPowerError", MALFORMED,
                                  "Client INFO differs from the bound native policy definition", None))
        self.assertEqual(final, state(sizes="LEGACY_SIZES", legacy=True, transport_key=A612_KEY))

    def test_rm26_records_refuse_a_v40_definition(self):
        def configure(fake):
            fake.native_info[52 + 31 * 116] = 0x40
        result, *_ = self.both(MetaRig(Rm26Gpu, configure=configure))
        self.assertEqual(result, ("ClientPowerError", MALFORMED,
                                  "Client INFO differs from the bound native policy definition", None))

    def test_the_v30_only_groups_refuse_a_v40_definition(self):
        # TURING and INFO216 carry _legacy; A612_COMPACT carries _rm26_records on the A612 family
        cases = ((CompactGpu, 60 + 21 * 128,
                  state(sizes="A612_COMPACT_SIZES", rm26_records=True, rm26_extended=True, transport_key=A612_KEY)),
                 (TuringGpu, 88 + 17 * 228,
                  state(sizes="TURING_SIZES", legacy=True, native_policy124=True, transport_key=TURING_KEY)),
                 (lambda: TuringGpu(record=216), 88 + 17 * 216,
                  state(sizes="INFO216_SIZES", legacy=True, role_info216=True, transport_key=TURING_KEY)))
        for factory, at, expected in cases:
            with self.subTest(expected["sizes"]):
                def configure(fake, at=at):
                    fake.native_info[at] = 0x40
                result, _, final, *_ = self.both(MetaRig(factory, configure=configure))
                self.assertEqual(result, ("ClientPowerError", MALFORMED,
                                          "Client INFO differs from the bound native policy definition", None))
                self.assertEqual(final, expected)

    def test_the_last_row_of_a_u32_mask_is_addressable(self):
        # 32 rows: the role1 row (and, where the model allows it, the native policy) at index 31, the
        # row ending exactly at the end of INFO
        cases = (("rm26", lambda: Rm26Gpu(client=31, native=31), None, 31),
                 ("legacy", lambda: LegacyGpu(client_index=31, native_index=31), legacy_accepted(), 31),
                 ("turing", lambda: TuringGpu(client_index=31, native_index=31), None, 31),
                 ("compact", lambda: CompactGpu(client=31, native=31), None, 31))
        for name, factory, accepted, native_index in cases:
            with self.subTest(name):
                result, *_ = self.both(MetaRig(factory, accepted=accepted))
                self.assertEqual(result[0], "ok", result)
                self.assertEqual((result[1]["binding"]["client_index"], result[1]["binding"]["native_index"]),
                                 (31, native_index))


# ---- _stable_locked ------------------------------------------------------------------------------

class StableReadTests(MetadataCase):
    def test_two_differing_captures_are_unstable(self):
        def configure(fake):
            fake.faults[(cp.NATIVE_INFO, 2)] = {"payload": lambda raw: raw.__setitem__(len(raw) - 1, 0x55)}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
        result, _, final, cache, fake_calls = self.both(rig)
        self.assertEqual(result, ("ClientPowerError", "unstable",
                                  "Client-power definitions or requests changed between fresh reads", None))
        self.assertEqual(len(fake_calls), 6)
        self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_a_difference_in_info_alone_is_unstable(self):
        # the last INFO byte lies in row 63, which is not the bound row: only info_hex differs
        def configure(fake):
            fake.faults[(cp.INFO, 2)] = {"payload": lambda raw: raw.__setitem__(len(raw) - 1, raw[-1] ^ 0xFF)}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
        result, _, _, cache, fake_calls = self.both(rig)
        self.assertEqual(result, ("ClientPowerError", "unstable",
                                  "Client-power definitions or requests changed between fresh reads", None))
        self.assertEqual(len(fake_calls), 6)
        self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_a_changed_transport_on_the_retry_is_an_identity_failure(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1)] = {"rm": 0x99}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
        stored = list(rig.header)

        def action(side):
            store(side, rig)
            rig.header[2], rig.header[15] = 11748, 11680  # the driver's power GET now has another size
            try:
                return outcome(side.read)
            finally:
                rig.header[:] = stored
        result, _, final, cache, fake_calls = self.both(rig, action)
        self.assertEqual(result, ("ClientPowerError", "identity",
                                  "Client-power transport changed; create a fresh controller", None))
        self.assertEqual(final, state(transport_key=A612_KEY))
        changed = list(stored)
        changed[2], changed[15] = 11748, 11680
        # captured again once and kept: an identity failure is neither retried nor followed by a reset
        self.assertEqual(cache, ((changed, rig.fields), False))
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924)])

    def test_a_changed_binding_between_captures_is_unstable(self):
        def configure(fake):
            def mutate(f):
                put(f.info, ROW + 4, 199999)
                put(f.native_info, NATIVE_ROW + 8, 199999)
            fake.faults[(cp.NATIVE_INFO, 1)] = {"mutate": mutate}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
        result, *_ = self.both(rig)
        self.assertEqual(result[:2], ("ClientPowerError", "unstable"))

    def test_a_transient_first_capture_resets_the_transport_and_recovers(self):
        for command in (cp.INFO, cp.ROLE_INFO, cp.NATIVE_INFO):
            with self.subTest(command=hex(command)):
                def configure(fake, command=command):
                    fake.faults[(command, 1)] = {"rm": 0x99}
                rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
                result, calls, final, cache, fake_calls = self.both(rig, choice=True)
                self.assertEqual(result[0], "ok")
                self.assertEqual(result[1]["stable_reads"], 2)
                self.assertEqual(final, state(transport_key=A612_KEY))
                # captured again: the header and fields, and no layout choice after the reset
                self.assertEqual(cache, ((rig.header, rig.fields), False))
                power_gets = [c for c in calls if c[0] == "Escape" and c[7][56:60] == bytes(u32(0x2080A612))]
                self.assertEqual(len(power_gets), 1)

    def test_a_transient_second_capture_retries_a_fresh_pair(self):
        def configure(fake):
            fake.faults[(cp.ROLE_INFO, 2)] = {"rm": 0x99}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
        result, _, _, _, fake_calls = self.both(rig)
        self.assertEqual(result[0], "ok")
        self.assertEqual(commands(fake_calls).count((cp.INFO, 0x924)), 4)

    def test_two_transients_raise_the_second(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1)] = {"rm": 0x99}
            fake.faults[(cp.NATIVE_INFO, 1)] = {"rm": 0x98}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
        result, _, _, cache, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.NATIVE_INFO, 0x98))
        self.assertEqual(cache, (None, False))  # reset after the second transient as well
        self.assertEqual(commands(fake_calls), [(cp.INFO, 0x924), (cp.INFO, 0x924), (cp.ROLE_INFO, 0x108),
                                                (cp.NATIVE_INFO, 20512)])

    def test_a_transient_retry_without_a_capturable_transport(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1)] = {"rm": 0x99}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
        result, _, final, cache, _ = self.both(rig)
        self.assertEqual(result, ("ClientPowerError", "transient",
                                  "unsupported current-policy transport: no recognized power GET captured", None))
        self.assertEqual(final, state(transport_key=A612_KEY))
        self.assertEqual(cache, (None, False))

    def test_an_escape_failure_is_transient_and_retried(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1)] = {"completion": 0}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
        result, *_ = self.both(rig)
        self.assertEqual(result[0], "ok")

    def test_a_non_transient_failure_is_raised_at_once(self):
        def configure(fake):
            fake.faults[(cp.ROLE_INFO, 2)] = {"rm": 0x56}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
        result, _, _, cache, fake_calls = self.both(rig)
        self.assertEqual(result, rm_error(cp.ROLE_INFO, 0x56))
        self.assertEqual(cache, ((rig.header, rig.fields), True))
        self.assertEqual(len(fake_calls), 5)

    def test_a_malformed_envelope_is_not_retried(self):
        def configure(fake):
            fake.faults[(cp.INFO, 1)] = {"header": (12, 7)}
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
        result, _, _, cache, _ = self.both(rig)
        self.assertEqual(result, ("ClientPowerError", "malformed",
                                  "Client-power response changed its transport envelope", None))
        self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_without_a_transport_the_read_fails_twice_and_stays_transient(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)

        def action(side):
            return outcome(side.read)  # nothing stored, nothing capturable
        result, calls, final, cache, fake_calls = self.both(rig, action)
        self.assertEqual(result, ("ClientPowerError", "transient",
                                  "unsupported current-policy transport: no recognized power GET captured", None))
        self.assertEqual((calls, fake_calls, final, cache), ([], [], state(), (None, False)))

    def test_rm26_recovers_through_a_fresh_capture(self):
        def configure(fake):
            fake.faults[(0x20802618, 1)] = {"rm": 0x99}
        rig = MetaRig(Rm26Gpu, configure=configure, recapture=True)
        result, _, final, cache, _ = self.both(rig)
        self.assertEqual(result[0], "ok")
        self.assertEqual(result[1]["binding"]["layout"], "rm26")
        self.assertEqual(final, state(sizes="RM26_SIZES", rm26=True, rm26_records=True,
                                      transport_key=(0x20802612, 2524, 2456)))
        self.assertEqual(cache, ((rig.header, rig.fields), False))


# ---- the C ABI ----------------------------------------------------------------------------------

def _text_bytes(field):
    return bytes(field).split(b"\0", 1)[0]


class AbiTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def test_arguments(self):
        lib = native.load()
        rig = MetaRig(FakeGpu, accepted=MODERN)
        rig.fresh()
        install(rig.card.nvml, rig.card.nvapi, rig.gdi())
        gpu = native.Gpu()
        rig.calls = []
        try:
            before = gpu._cp_state()
            m, error = native.Gpu._CpcMetadata(), native._Error()
            ctypes.memset(ctypes.addressof(m), 0xA5, ctypes.sizeof(m))
            m.size, error.size, error.kind = ctypes.sizeof(m), ctypes.sizeof(error), 99
            sentinel = bytes(m)[4:]

            def untouched(kind=99):
                self.assertEqual(error.kind, kind)
                self.assertEqual(bytes(m)[4:], sentinel)
            self.assertEqual(lib.dc_cpc_read_metadata(None, ctypes.addressof(m), ctypes.byref(error)),
                             native.DC_E_ARGUMENT)
            untouched()
            self.assertEqual(lib.dc_cpc_read_metadata(gpu._handle, None, ctypes.byref(error)), native.DC_E_ARGUMENT)
            untouched()
            m.size = ctypes.sizeof(m) - 1
            self.assertEqual(lib.dc_cpc_read_metadata(gpu._handle, ctypes.addressof(m), ctypes.byref(error)),
                             native.DC_E_ARGUMENT)
            untouched()
            m.size, error.size = ctypes.sizeof(m), 0
            self.assertEqual(lib.dc_cpc_read_metadata(gpu._handle, ctypes.addressof(m), ctypes.byref(error)),
                             native.DC_E_ARGUMENT)
            untouched()
            self.assertEqual(rig.calls, [])
            self.assertEqual(gpu._cp_state(), before)
            self.assertEqual(gpu._current_limit_transport, None)
            # A NULL error is allowed: the failure (no transport) is still DC_E_FAILED, *out untouched.
            self.assertEqual(lib.dc_cpc_read_metadata(gpu._handle, ctypes.addressof(m), None), native.DC_E_FAILED)
            self.assertEqual(bytes(m)[4:], sentinel)
            # The success path fills the whole struct over the 0xA5 fill (absent keys and padding zeroed)
            # and clears *error.
            gpu._current_limit_transport = (list(rig.header), dict(rig.fields))
            error.size, error.kind = ctypes.sizeof(error), 99
            self.assertEqual(lib.dc_cpc_read_metadata(gpu._handle, ctypes.addressof(m), ctypes.byref(error)),
                             native.DC_OK)
            self.assertEqual((m.schema, m.stable_reads, m.slot, m.units, m.layout, m.native_layout,
                              m.has_native_info_stride, m.native_info_stride),
                             (1, 2, b"0000:01:00.0", b"mW", 0, 0, 0, 0))
            self.assertEqual((m.info_size, m.role_info_size, m.native_info_size), (0x924, 0x108, 20512))
            raw, fields = bytes(m), native.Gpu._CpcMetadata
            self.assertEqual(raw[fields.slot.offset:fields.slot.offset + 24], b"0000:01:00.0".ljust(24, b"\0"))
            self.assertEqual(raw[fields.units.offset:fields.units.offset + 4], b"mW\0\0")
            self.assertEqual(error.kind, 0)
            self.assertEqual(_text_bytes(error.message), b"")
        finally:
            gpu.close()

    def test_a_buffer_refusal_is_a_client_power_error(self):
        # Unreachable in the core (every group fits dc_cpc_metadata); the binding's answer to it.
        gpu = native.Gpu.__new__(native.Gpu)
        gpu._handle = None
        stub = SimpleNamespace(dc_cpc_read_metadata=Mock(return_value=native.DC_E_BUFFER))
        with patch.object(native, "load", return_value=stub):
            with self.assertRaises(cp.ClientPowerError) as caught:
                gpu._cp_read_metadata()
        self.assertEqual(caught.exception.kind, "malformed")
        self.assertIn("DC_E_BUFFER", str(caught.exception))
        self.assertIsNone(caught.exception.rm_status)

    def test_the_identity_slot_is_the_adapter_s_canonical_slot(self):
        # The handle opened with a requested slot that does not fit dc_cpc_metadata.slot (surrounding
        # whitespace, the 8-digit NVML domain): the identity is still the adapter's own 12-character
        # format_slot text, which is what Python's GPU.slot() returns too.
        rig = MetaRig(FakeGpu, accepted=MODERN)
        rig.fresh()
        install(rig.card.nvml, rig.card.nvapi, rig.gdi())
        requested = "    00000000:01:00.0        "
        self.assertGreaterEqual(len(requested), 24)
        gpu = native.Gpu(requested)
        try:
            gpu._current_limit_transport = (list(rig.header), dict(rig.fields))
            metadata = gpu._cp_read_metadata()
            self.assertEqual(metadata["identity"]["slot"], "0000:01:00.0")
            self.assertEqual(metadata["identity"]["slot"], gpu.slot())
        finally:
            gpu.close()

    def test_the_payload_arrays_hold_every_group(self):
        m = native.Gpu._CpcMetadata
        for name in ("SIZES", "LEGACY_SIZES", "TURING_SIZES", "INFO216_SIZES", "RM26_SIZES", "RM26_EXTENDED_SIZES",
                     "A612_COMPACT_SIZES"):
            sizes = getattr(cp, name)
            self.assertLessEqual(sizes[cp.INFO], m.info.size)
            self.assertLessEqual(sizes[cp.ROLE_INFO], m.role_info.size)
            self.assertLessEqual(sizes[cp.NATIVE_INFO], m.native_info.size)
        self.assertEqual((m.info.size, m.role_info.size, m.native_info.size),
                         (cp.SIZES[cp.INFO], cp.SIZES[cp.ROLE_INFO], cp.SIZES[cp.NATIVE_INFO]))


if __name__ == "__main__":
    unittest.main()
