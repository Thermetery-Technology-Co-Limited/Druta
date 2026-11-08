# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native RM escape transport, increment 2 (the process-wide D3DKMTEscape
hook, the power and legacy-clock captures, and the per-GPU transport cache)
against nvbackend.GPU._capture_current_limit_transport,
_capture_legacy_clk_transport, _rm_power_transport and
_current_limit_transport_layouts.

One fake driver serves both sides. The getters (PowerPolInfo, VoltRailsCtlGet)
are one CFUNCTYPE callback each; they issue a scripted list of escapes the way
the CPU would: through the thunk while the D3DKMTEscape entry is patched, and
straight to the real escape while it is not. "Patched" is bytes(code) on the
Python twin (a create_string_buffer stands in for the code bytes, the patched
ctypes.WINFUNCTYPE hands back the closure) and native.hook_test_state() on the
core (the buffer patcher; the thunk body is reached through
native.hook_test_entry). The fake real escape logs the escape fields, the
packet's header words, whether the hook was patched at the moment of the
forward (restore before forward) and which thread forwarded it. A foreign
thread is a real threading.Thread on both sides (the Python twin's
GetCurrentThreadId is threading.get_ident).
"""
import ctypes
import struct
import threading
from collections import namedtuple
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from druta import native, nvbackend
from tests.test_native_telemetry import u32_callback
from tests.test_native_transport import ENUM2, ESCAPE, KMT, QUERY, CLOSE, REQUIRED, TransportRig
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

NV = nvbackend
u32 = ctypes.c_uint32
GETTER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
POWER_POL_INFO, VOLT_RAILS_CTL_GET = 0x34206D86, 0xA3070DB0
A612, P2612, ARCH_CMD, LEGACY_CMD = 0x2080A612, 0x20802612, 0x20800111, 0x20803213
FIELDS = dict(hAdapter=987, hDevice=0, Type=0, Flags=8, hContext=0)
HANDLE = 0x2000  # the paired NVAPI handle on both sides
POWER_GETTER_CALL = ("PowerPolInfo", HANDLE, 0x100B8)  # _PwrPolInfo: 184 bytes, version 1
LEGACY_GETTER_CALL = ("VoltRailsCtlGet", HANDLE, 0x10AC8, 1)
NO_POLICY_ARCH = 9  # Hopper: a named architecture without a current-limit generation policy


# ---- packets ------------------------------------------------------------------------------------

def power_packet(packet_bytes=54420, parameter_bytes=54352, *, size_word=None, command=A612, marks=(9988, 7766)):
    def make():
        words = (u32 * (packet_bytes // 4))()
        words[2] = packet_bytes if size_word is None else size_word
        words[14], words[15] = command, parameter_bytes
        words[12], words[13] = marks
        return words
    return make


def arch_packet():
    words = (u32 * 33)()
    words[2], words[14], words[15] = 132, ARCH_CMD, 64
    return words


def legacy_packet(*, packet_bytes=716, size_word=716, command=LEGACY_CMD, parameter_bytes=648, record=1,
                  marks=(4455, 6677)):
    def make():
        words = (u32 * (packet_bytes // 4))()
        words[2], words[14], words[15], words[17] = size_word, command, parameter_bytes, record
        words[12], words[13] = marks
        return words
    return make


def header(packet_bytes, parameter_bytes, *, command=A612, marks=(9988, 7766)):
    words = [0] * 17
    words[2], words[14], words[15] = packet_bytes, command, parameter_bytes
    words[12], words[13] = marks
    return words


def legacy_header(marks=(4455, 6677)):
    words = [0] * 17
    words[2], words[14], words[15] = 716, LEGACY_CMD, 648
    words[12], words[13] = marks
    return words


def observed(command, packet_bytes, parameter_bytes):
    return {"command": f"0x{command:08X}", "command_id": command, "packet_bytes": packet_bytes,
            "parameter_bytes": parameter_bytes}


Step = namedtuple("Step", "make foreign force")


def owner(make):
    return Step(make, False, False)


def foreign(make):
    return Step(make, True, False)


def forced(make):
    """Enters the thunk although the entry is not patched (a jump already in flight)."""
    return Step(make, False, True)


ARCH_STEP = owner(arch_packet)


def escape_log(packet_bytes, size_word, command, parameter_bytes, marks=(0, 0), *, patched=False, main=True):
    """The fake real escape's log entry. patched: the hook state when the escape was forwarded."""
    return ("Escape", 987, 0, 0, 8, 0, packet_bytes, size_word, *marks, command, parameter_bytes, patched, main)


ARCH_LOG = escape_log(132, 132, ARCH_CMD, 64)


def power_log(packet_bytes=54420, parameter_bytes=54352, *, size_word=None, marks=(9988, 7766), **kw):
    return escape_log(packet_bytes, packet_bytes if size_word is None else size_word, A612, parameter_bytes, marks,
                      **kw)


def legacy_log(marks=(4455, 6677), **kw):
    return escape_log(716, 716, LEGACY_CMD, 648, marks, **kw)


def armed(value):
    return ("Patched", value)


# ---- the rig ------------------------------------------------------------------------------------

class HookRig(TransportRig):
    """TransportRig plus the two escaping getters and a hook-aware driver model.

    steps: the escapes the getter issues, in order (owner / foreign / forced)
    rounds: instead of steps, one list of steps per getter call (later calls issue nothing)
    status: the getter's return code; arch: NVML's architecture (None: NVML does not answer)
    power / volt: whether PowerPolInfo / VoltRailsCtlGet exist; fail_protect: VirtualProtect fails
    python_nvapi_ok: the Python twin's nvapi.ok (the native side gets False from a pairing refusal)
    """

    def __init__(self, steps=(), *, rounds=None, status=0, arch=7, power=True, volt=True, fail_protect=False,
                 python_nvapi_ok=True, **kw):
        self.rounds = [list(steps)] if rounds is None else [list(r) for r in rounds]
        self.getter_status = status
        self.fail_protect = fail_protect
        self.python_nvapi_ok = python_nvapi_ok
        self.errors = []
        self.probe = lambda: False
        self.enter = None
        self.on_getter = None
        self.after = None
        self.built, self.getter_calls = [], 0
        super().__init__(escape_writes={}, **kw)
        exports = self.nvml.exports
        if arch is None:
            exports.pop("nvmlDeviceGetArchitecture", None)
        else:
            exports["nvmlDeviceGetArchitecture"] = u32_callback(arch)
        interfaces, names = self.nvapi.interfaces, self.names
        if power:
            names["PowerPolInfo"] = interfaces[POWER_POL_INFO] = self._getter("PowerPolInfo", 1)
        if volt:
            names["VoltRailsCtlGet"] = interfaces[VOLT_RAILS_CTL_GET] = self._getter("VoltRailsCtlGet", 2)

    # the driver model

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            try:
                e = NV.GPU._Escape.from_address(pointer)
                w = (u32 * 16).from_address(e.pPrivateDriverData)
                self.calls.append(("Escape", e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext,
                                   e.PrivateDriverDataSize, w[2], w[12], w[13], w[14], w[15], self.probe(),
                                   threading.current_thread() is threading.main_thread()))
            except BaseException as exc:  # an exception would vanish inside the callback
                self.errors.append(repr(exc))
            return 0
        table[ESCAPE] = escape
        return table

    def forward(self, address):
        """The unpatched D3DKMTEscape: the real escape, no thunk."""
        return self._callbacks[ESCAPE](address)

    def _issue(self, step, escape):
        address = ctypes.addressof(escape)
        route = self.enter if step.force or self.probe() else self.forward
        if not step.foreign:
            route(address)
            return
        thread_errors = []

        def run():
            try:
                route(address)
            except BaseException as exc:
                thread_errors.append(repr(exc))
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.errors.extend(thread_errors)

    def _getter(self, name, words):
        @GETTER
        def getter(handle, block):
            try:
                self.calls.append((name, handle, *(u32 * words).from_address(block)))
                if self.on_getter:
                    self.on_getter()
                self.calls.append(armed(self.probe()))
                script = self.built[self.getter_calls] if self.getter_calls < len(self.built) else []
                self.getter_calls += 1
                for step, _, escape in script:
                    self._issue(step, escape)
                    self.calls.append(armed(self.probe()))
            except BaseException as exc:
                self.errors.append(repr(exc))
            return self.getter_status
        return getter

    def reset(self):
        super().reset()
        self.getter_calls = 0
        self.built = []
        for steps in getattr(self, "rounds", ()):
            script = []
            for step in steps:
                packet = step.make()
                escape = NV.GPU._Escape(**FIELDS, pPrivateDriverData=ctypes.addressof(packet),
                                        PrivateDriverDataSize=ctypes.sizeof(packet))
                script.append((step, (packet, bytes(packet)), escape))
            self.built.append(script)

    def state(self):
        # the hook only reads: every packet is byte-for-byte what the getter built
        return [bytes(packet) == original for script in self.built for _, (packet, original), _ in script]

    # the Python twin

    def python_gpu(self):
        gpu = super().python_gpu()
        gpu.nvapi.ok = self.with_nvapi and self.python_nvapi_ok
        gpu.nvapi.PowerPolInfo = self.names.get("PowerPolInfo")
        gpu.nvapi.VoltRailsCtlGet = self.names.get("VoltRailsCtlGet")
        self.code = ctypes.create_string_buffer(b"original bytes")
        self.original_code = bytes(self.code)
        self.callback_storage = ctypes.create_string_buffer(14)
        self.py_target = None
        self.probe = lambda: bytes(self.code) != self.original_code
        self.enter = lambda address: self.py_target(address)

        def protect(pointer, size, flags, previous):
            if self.fail_protect:
                return 0
            previous._obj.value = 0x20
            return 1
        self.kernel = SimpleNamespace(VirtualProtect=Mock(side_effect=protect),
                                      GetCurrentThreadId=Mock(side_effect=threading.get_ident),
                                      GetCurrentProcess=Mock(return_value=42),
                                      FlushInstructionCache=Mock(return_value=1))
        return gpu

    def python_windll(self):
        table = self.gdi_table()

        def load(name, *args, **kwargs):
            if name == "kernel32":
                return self.kernel
            if table is None:
                raise OSError("gdi32.dll not found")
            exports = dict(table)
            if ESCAPE in exports:  # the hook patches the code bytes, never the fake's trampoline
                exports[ESCAPE] = KMT(ctypes.addressof(self.code))
            return SimpleNamespace(**exports)
        return patch.object(NV.ctypes, "WinDLL", create=True, side_effect=load)

    def prototype(self, restype, argtype):
        def bind(target):
            if callable(target):  # the capture closure: the thunk's target
                self.py_target = target
                return ctypes.c_void_p(ctypes.addressof(self.callback_storage))
            if target != ctypes.addressof(self.code):
                self.errors.append(f"forwarded to 0x{target:X}")
            return self.forward  # proto(address)(ptr): the restored real function
        return bind

    # the native core

    def native_gpu(self):
        gpu = super().native_gpu()
        self.probe = lambda: native.hook_test_state()["patched"]
        self.enter = native.hook_test_entry
        return gpu

    def run(self, method):
        """The action for ParityCase.both: method(gpu) as an outcome, never raising."""
        def action(gpu):
            if isinstance(gpu, NV.GPU):
                with self.python_windll(), patch.object(NV.ctypes, "WINFUNCTYPE", new=self.prototype, create=True):
                    return outcome(lambda: method(gpu))
            native.hook_test_fail_protect(self.fail_protect)
            try:
                return outcome(lambda: method(gpu))
            finally:
                native.hook_test_fail_protect(False)
                self.after = native.hook_test_state()
        return action


def outcome(call):
    try:
        return ("ok", call())
    except ValueError as exc:  # CurrentLimitUnsupported is a ValueError
        return ("raised", type(exc).__name__, str(exc), getattr(exc, "fixed", False))


def capture_power(gpu):
    return gpu._capture_current_limit_transport(), dict(getattr(gpu, "_current_limit_observed_transport", {}))


def capture_legacy(gpu):
    return gpu._capture_legacy_clk_transport()


def cache(gpu):
    # the choice is a layout dict on the Python twin and an index on the core: compare whether one is set
    return (getattr(gpu, "_current_limit_transport", None),
            getattr(gpu, "_current_limit_layout_choice", None) is not None, dict(getattr(gpu, "_current_limit_observed_transport", {})))


def seed_choice(gpu):
    # the Python twin keeps the chosen layout dict, the core its index; only None-ness is compared
    gpu._current_limit_layout_choice = {"seeded": True} if isinstance(gpu, NV.GPU) else 0


def transport_then_cache(gpu, *, choice=True, assign=None):
    if assign is not None:
        gpu._current_limit_transport = assign
    if choice:
        seed_choice(gpu)
    return outcome(gpu._rm_power_transport), cache(gpu)


def layouts_then_cache(gpu, *, assign=None):
    if assign is not None:
        gpu._current_limit_transport = assign
    seed_choice(gpu)
    return outcome(gpu._current_limit_transport_layouts), cache(gpu)


class HookCase(ParityCase):
    def tearDown(self):
        native.hook_test_fail_protect(False)
        super().tearDown()
        self.assertFalse(native.hook_test_state()["installed"])

    def both(self, rig, action):
        result = super().both(rig, rig.run(action))
        self.assertEqual(rig.errors, [])
        self.assertIsNotNone(rig.after)
        self.assertFalse(rig.after["installed"])  # uninstalled when the getter returned
        self.assertFalse(rig.after["patched"])
        return result

    def sides(self, rig, action):
        """action on each side, without requiring equal results (documented divergences)."""
        runs = []
        for kind in ("python", "native"):
            rig.reset()
            if kind == "python":
                runs.append(rig.run(action)(rig.python_gpu()))
                continue
            gpu = rig.native_gpu()
            try:
                gpu.capabilities()
                rig.calls.clear()
                runs.append(rig.run(action)(gpu))
            finally:
                gpu.close()
        self.assertEqual(rig.errors, [])
        self.assertEqual([run[0] for run in runs], ["ok", "ok"])  # the actions catch their own errors
        return [run[1] for run in runs]


# ---- the power capture ------------------------------------------------------------------------

class PowerCaptureTests(HookCase):
    def test_architecture_escape_first_then_the_power_get(self):
        for packet_bytes, parameter_bytes in ((54420, 54352), (11748, 11680)):
            with self.subTest(packet_bytes=packet_bytes):
                rig = HookRig([ARCH_STEP, owner(power_packet(packet_bytes, parameter_bytes))])
                (_, (transport, seen)), calls, unchanged = self.both(rig, capture_power)
                self.assertEqual(transport, (header(packet_bytes, parameter_bytes), FIELDS))
                self.assertEqual(seen, observed(A612, packet_bytes, parameter_bytes))
                self.assertEqual(calls, [POWER_GETTER_CALL, armed(True),
                                         ARCH_LOG, armed(True),  # restored for the forward, re-armed after
                                         power_log(packet_bytes, parameter_bytes), armed(False)])
                self.assertEqual(unchanged, [True, True])

    def test_interception_stops_once_the_header_is_found(self):
        rig = HookRig([ARCH_STEP, owner(power_packet()), owner(power_packet(11748, 11680, marks=(1, 2)))])
        (_, (transport, seen)), calls, _ = self.both(rig, capture_power)
        self.assertEqual(transport[0], header(54420, 54352))
        self.assertEqual(seen, observed(A612, 54420, 54352))  # the second GET never reached the thunk
        self.assertEqual(calls[-2:], [power_log(11748, 11680, marks=(1, 2)), armed(False)])

    def test_size_word_that_is_not_the_packet_size_is_not_even_observed(self):
        rig = HookRig([ARCH_STEP, owner(power_packet(size_word=54424))])
        (_, (transport, seen)), calls, unchanged = self.both(rig, capture_power)
        self.assertIsNone(transport)
        self.assertEqual(seen, {})
        self.assertEqual(calls[-2:], [power_log(size_word=54424), armed(True)])  # still armed
        self.assertEqual(unchanged, [True, True])

    def test_self_consistent_size_but_not_the_prologue_is_observed_not_found(self):
        rig = HookRig([ARCH_STEP, owner(power_packet(54424, 54352))])
        (_, (transport, seen)), calls, _ = self.both(rig, capture_power)
        self.assertIsNone(transport)
        self.assertEqual(seen, observed(A612, 54424, 54352))
        self.assertEqual(calls[-1], armed(True))

    def test_the_last_observed_get_wins(self):
        rig = HookRig([owner(power_packet(54424, 54352)), owner(power_packet(3552, 3480, command=P2612))])
        (_, (transport, seen)), _, _ = self.both(rig, capture_power)
        self.assertIsNone(transport)
        self.assertEqual(seen, observed(P2612, 3552, 3480))

    def test_two_found_gets_give_no_transport(self):
        rig = HookRig([owner(power_packet()), forced(power_packet(11748, 11680, marks=(1, 2)))])
        (_, (transport, seen)), calls, _ = self.both(rig, capture_power)
        self.assertIsNone(transport)
        self.assertEqual(seen, observed(A612, 11748, 11680))
        self.assertEqual(calls[-2:], [power_log(11748, 11680, marks=(1, 2)), armed(False)])

    def test_a_foreign_threads_escape_is_forwarded_uninspected_and_rearms(self):
        rig = HookRig([ARCH_STEP, foreign(power_packet())])
        (_, (transport, seen)), calls, unchanged = self.both(rig, capture_power)
        self.assertIsNone(transport)
        self.assertEqual(seen, {})
        self.assertEqual(calls[-2:], [power_log(main=False), armed(True)])
        self.assertEqual(unchanged, [True, True])

    def test_the_owner_still_captures_after_a_foreign_escape(self):
        rig = HookRig([foreign(power_packet(marks=(5, 6))), owner(power_packet())])
        (_, (transport, _)), calls, _ = self.both(rig, capture_power)
        self.assertEqual(transport, (header(54420, 54352), FIELDS))
        self.assertEqual(calls[1:], [armed(True), power_log(marks=(5, 6), main=False), armed(True),
                                     power_log(), armed(False)])

    def test_nonzero_getter_status_keeps_the_observed_record(self):
        rig = HookRig([ARCH_STEP, owner(power_packet())], status=-5)
        (_, (transport, seen)), calls, _ = self.both(rig, capture_power)
        self.assertIsNone(transport)
        self.assertEqual(seen, observed(A612, 54420, 54352))
        self.assertEqual(calls[-1], armed(False))

    def test_virtual_protect_failure_installs_nothing(self):
        rig = HookRig([ARCH_STEP, owner(power_packet())], fail_protect=True)
        (_, (transport, seen)), calls, _ = self.both(rig, capture_power)
        self.assertIsNone(transport)
        self.assertEqual(seen, {})
        self.assertEqual(calls, [])  # the getter never ran

    def test_preconditions_install_no_hook(self):
        cases = {
            "no PowerPolInfo": dict(power=False),
            "no generation policy": dict(arch=NO_POLICY_ARCH),
            "architecture unknown": dict(arch=None),
            "NVAPI disabled by a pairing refusal": dict(nvml_devid=0x1B02, python_nvapi_ok=False),
            "no NVAPI": dict(with_nvapi=False),
            "gdi32 not loadable": dict(gdi=False),
        }
        cases.update({f"gdi32 lacks {name}": dict(missing={name}) for name in REQUIRED})
        for label, kw in cases.items():
            with self.subTest(label):
                rig = HookRig([ARCH_STEP, owner(power_packet())], **kw)
                (_, (transport, seen)), calls, unchanged = self.both(rig, capture_power)
                self.assertIsNone(transport)
                self.assertEqual(seen, {})
                self.assertEqual(calls, [])
                self.assertEqual(unchanged, [True, True])

    def test_an_earlier_observation_is_forgotten_by_the_next_attempt(self):
        rig = HookRig(rounds=[[owner(power_packet(54424, 54352))], []])  # the second call escapes nothing
        (_, (first, second)), calls, _ = self.both(rig, lambda g: (capture_power(g), capture_power(g)))
        self.assertEqual(first, (None, observed(A612, 54424, 54352)))
        self.assertEqual(second, (None, {}))
        self.assertEqual(calls.count(POWER_GETTER_CALL), 2)


class PythonPatchBytesTests(HookCase):
    def test_the_python_patch_is_the_native_x64_thunk(self):
        # The bytes nvbackend writes over D3DKMTEscape are exactly what hook_thunk builds for 8-byte
        # pointers, and the original bytes are back after the capture (restore in finally).
        rig = HookRig([ARCH_STEP, owner(power_packet())])
        rig.reset()
        gpu = rig.python_gpu()
        seen = []
        rig.on_getter = lambda: seen.append(bytes(rig.code)[:14])
        _, (transport, _) = rig.run(capture_power)(gpu)
        self.assertIsNotNone(transport)
        self.assertEqual(seen, [native.hook_thunk(ctypes.addressof(rig.code),
                                                  ctypes.addressof(rig.callback_storage), 8)])
        self.assertEqual(bytes(rig.code), rig.original_code)
        protect = rig.kernel.VirtualProtect.call_args_list
        self.assertEqual([c.args[1:3] for c in protect], [(14, 0x40), (14, 0x20)])


# ---- the legacy capture -----------------------------------------------------------------------

class LegacyCaptureTests(HookCase):
    def test_the_716_byte_get_with_one_record(self):
        rig = HookRig([owner(legacy_packet())])
        (_, transport), calls, unchanged = self.both(rig, capture_legacy)
        self.assertEqual(transport, (legacy_header(), FIELDS))
        self.assertEqual(calls, [LEGACY_GETTER_CALL, armed(True), legacy_log(), armed(False)])
        self.assertEqual(unchanged, [True])

    def test_any_other_shape_is_not_captured(self):
        shapes = {
            "record count 2": legacy_packet(record=2),
            "record count 0": legacy_packet(record=0),
            "720-byte packet": legacy_packet(packet_bytes=720),
            "size word 720": legacy_packet(size_word=720),
            "another command": legacy_packet(command=0x20803214),
            "parameter size 652": legacy_packet(parameter_bytes=652),
        }
        for label, make in shapes.items():
            with self.subTest(label):
                rig = HookRig([owner(make)])
                (_, transport), calls, unchanged = self.both(rig, capture_legacy)
                self.assertIsNone(transport)
                self.assertEqual(calls[-1], armed(False))  # never re-armed
                self.assertEqual(unchanged, [True])

    def test_never_rearmed_so_a_second_get_is_not_inspected(self):
        rig = HookRig([owner(legacy_packet()), owner(legacy_packet(marks=(1, 2)))])
        (_, transport), calls, _ = self.both(rig, capture_legacy)
        self.assertEqual(transport, (legacy_header(), FIELDS))
        self.assertEqual(calls, [LEGACY_GETTER_CALL, armed(True), legacy_log(), armed(False),
                                 legacy_log(marks=(1, 2)), armed(False)])

    def test_an_unrelated_escape_first_defeats_it(self):
        rig = HookRig([ARCH_STEP, owner(legacy_packet())])
        (_, transport), calls, _ = self.both(rig, capture_legacy)
        self.assertIsNone(transport)
        self.assertEqual(calls[1:], [armed(True), ARCH_LOG, armed(False), legacy_log(), armed(False)])

    def test_a_foreign_thread_first_defeats_it(self):
        rig = HookRig([foreign(legacy_packet(marks=(1, 2))), owner(legacy_packet())])
        (_, transport), calls, _ = self.both(rig, capture_legacy)
        self.assertIsNone(transport)
        self.assertEqual(calls[1:], [armed(True), legacy_log(marks=(1, 2), main=False), armed(False),
                                     legacy_log(), armed(False)])

    def test_two_found_gets_give_no_transport(self):
        rig = HookRig([owner(legacy_packet()), forced(legacy_packet(marks=(1, 2)))])
        (_, transport), _, _ = self.both(rig, capture_legacy)
        self.assertIsNone(transport)

    def test_nonzero_getter_status(self):
        rig = HookRig([owner(legacy_packet())], status=-5)
        (_, transport), calls, _ = self.both(rig, capture_legacy)
        self.assertIsNone(transport)
        self.assertEqual(calls[2], legacy_log())

    def test_no_generation_gate(self):
        # Only nvapi.ok and VoltRailsCtlGet gate the legacy capture.
        rig = HookRig([owner(legacy_packet())], arch=NO_POLICY_ARCH, power=False)
        (_, transport), _, _ = self.both(rig, capture_legacy)
        self.assertEqual(transport, (legacy_header(), FIELDS))

    def test_preconditions_install_no_hook(self):
        cases = {
            "no VoltRailsCtlGet": dict(volt=False),
            "NVAPI disabled by a pairing refusal": dict(nvml_devid=0x1B02, python_nvapi_ok=False),
            "VirtualProtect fails": dict(fail_protect=True),
            "gdi32 not loadable": dict(gdi=False),
        }
        cases.update({f"gdi32 lacks {name}": dict(missing={name}) for name in REQUIRED})
        for label, kw in cases.items():
            with self.subTest(label):
                rig = HookRig([owner(legacy_packet())], **kw)
                (_, transport), calls, _ = self.both(rig, capture_legacy)
                self.assertIsNone(transport)
                self.assertEqual(calls, [])


# ---- _rm_power_transport ------------------------------------------------------------------------

UNVALIDATED = ("raised", "CurrentLimitUnsupported", "unvalidated current-policy transport geometry", False)
NOTHING_SEEN = ("raised", "ValueError", "unsupported current-policy transport: no recognized power GET captured",
                False)


class RmPowerTransportTests(HookCase):
    def test_a_fresh_capture_is_cached_and_the_choice_cleared(self):
        rig = HookRig([ARCH_STEP, owner(power_packet())])
        (_, (result, (stored, choice, seen))), calls, _ = self.both(rig, transport_then_cache)
        expected = (header(54420, 54352), FIELDS)
        self.assertEqual(result, ("ok", expected))
        self.assertEqual(stored, expected)
        self.assertFalse(choice)
        self.assertEqual(seen, observed(A612, 54420, 54352))
        self.assertEqual(calls[0], POWER_GETTER_CALL)

    def test_a_cached_transport_is_returned_without_a_capture(self):
        cached = (header(3548, 3480, command=P2612, marks=(1, 2)), dict(FIELDS, hAdapter=55, hContext=3))
        rig = HookRig([ARCH_STEP, owner(power_packet())])
        (_, (result, (stored, choice, _))), calls, _ = self.both(
            rig, lambda g: transport_then_cache(g, assign=cached))
        self.assertEqual(result, ("ok", cached))
        self.assertEqual(stored, cached)
        self.assertTrue(choice)  # a valid cached transport keeps the choice
        self.assertEqual(calls, [])

    def test_an_observed_unknown_geometry_is_unsupported(self):
        rig = HookRig([ARCH_STEP, owner(power_packet(54424, 54352))])
        (_, (result, (stored, choice, _))), _, _ = self.both(rig, transport_then_cache)
        self.assertEqual(result, ("raised", "CurrentLimitUnsupported",
                                  "unsupported current-policy transport: observed 0x2080A612, 54424/54352 bytes",
                                  False))
        self.assertIsNone(stored)
        self.assertFalse(choice)

    def test_an_unknown_self_consistent_get_whose_getter_failed_is_unsupported(self):
        rig = HookRig([owner(power_packet(1068, 1000))], status=-5)
        (_, (result, _)), _, _ = self.both(rig, transport_then_cache)
        self.assertEqual(result[:3], ("raised", "CurrentLimitUnsupported",
                                      "unsupported current-policy transport: observed 0x2080A612, 1068/1000 bytes"))

    def test_a_known_geometry_whose_getter_failed_is_a_failed_read(self):
        rig = HookRig([ARCH_STEP, owner(power_packet())], status=-5)
        (_, (result, (stored, _, seen))), _, _ = self.both(rig, transport_then_cache)
        self.assertEqual(result, ("raised", "ValueError", "unsupported current-policy transport: observed "
                                  "0x2080A612, 54420/54352 bytes, but the capture did not complete", False))
        self.assertIsNone(stored)
        self.assertEqual(seen, observed(A612, 54420, 54352))

    def test_nothing_observed_is_a_failed_read(self):
        for label, kw in (("only the architecture escape", {}), ("no generation policy", dict(arch=NO_POLICY_ARCH)),
                          ("VirtualProtect fails", dict(fail_protect=True))):
            with self.subTest(label):
                rig = HookRig([ARCH_STEP], **kw)
                (_, (result, (stored, choice, seen))), _, _ = self.both(rig, transport_then_cache)
                self.assertEqual(result, NOTHING_SEEN)  # fixed is False on both sides
                self.assertIsNone(stored)
                self.assertFalse(choice)
                self.assertEqual(seen, {})

    def test_a_malformed_assigned_transport_is_dropped(self):
        good = header(54420, 54352)
        bad = {
            "16-word header": good[:16],
            "18-word header": good + [0],
            "architecture command": header(54420, 54352, command=ARCH_CMD),
            "packet not prologue plus parameters": header(54424, 54352),
        }
        for label, words in bad.items():
            with self.subTest(label):
                rig = HookRig([ARCH_STEP, owner(power_packet())])
                (_, (result, (stored, choice, _))), calls, _ = self.both(
                    rig, lambda g: transport_then_cache(g, assign=(words, FIELDS)))
                self.assertEqual(result, UNVALIDATED)
                self.assertIsNone(stored)
                self.assertFalse(choice)
                self.assertEqual(calls, [])  # validated, never recaptured in the same call

    def test_the_next_call_after_a_drop_recaptures(self):
        rig = HookRig([ARCH_STEP, owner(power_packet())])

        def drop_then_retry(gpu):
            first = transport_then_cache(gpu, assign=(header(54424, 54352), FIELDS))
            return first[0], outcome(gpu._rm_power_transport)
        (_, (first, second)), calls, _ = self.both(rig, drop_then_retry)
        self.assertEqual(first, UNVALIDATED)
        self.assertEqual(second, ("ok", (header(54420, 54352), FIELDS)))
        self.assertEqual(calls[0], POWER_GETTER_CALL)


class TransportLayoutsTests(HookCase):
    def test_captured_keys_return_the_layout_and_its_fallbacks(self):
        for command, packet_bytes, parameter_bytes, count in ((A612, 54420, 54352, 3), (P2612, 3548, 3480, 1)):
            with self.subTest(command=hex(command), packet_bytes=packet_bytes):
                key = (command, packet_bytes, parameter_bytes)
                rig = HookRig([ARCH_STEP, owner(power_packet(packet_bytes, parameter_bytes, command=command))])
                (_, (result, (stored, choice, _))), _, _ = self.both(rig, layouts_then_cache)
                expected = (NV.GPU._CURRENT_LIMIT_LAYOUTS[key], *NV.GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS.get(key, ()))
                self.assertEqual(len(expected), count)
                self.assertEqual(result, ("ok", expected))
                self.assertEqual(stored, (header(packet_bytes, parameter_bytes, command=command), FIELDS))
                self.assertFalse(choice)  # a fresh capture clears it; the layouts do not choose

    def test_an_unknown_key_keeps_the_transport_and_clears_the_choice(self):
        unknown = (header(1068, 1000), FIELDS)
        self.assertNotIn((A612, 1068, 1000), NV.GPU._CURRENT_LIMIT_LAYOUTS)
        rig = HookRig([ARCH_STEP, owner(power_packet())])
        (_, (result, (stored, choice, _))), calls, _ = self.both(rig, lambda g: layouts_then_cache(g, assign=unknown))
        self.assertEqual(result, UNVALIDATED)
        self.assertEqual(stored, unknown)
        self.assertFalse(choice)
        self.assertEqual(calls, [])

    def test_an_unknown_captured_key_is_kept_too(self):
        rig = HookRig([owner(power_packet(1068, 1000))])
        (_, (result, (stored, _, seen))), _, _ = self.both(rig, layouts_then_cache)
        self.assertEqual(result, UNVALIDATED)
        self.assertEqual(stored, (header(1068, 1000), FIELDS))
        self.assertEqual(seen, observed(A612, 1068, 1000))

    def test_a_failed_transport_raises_its_own_error(self):
        rig = HookRig([ARCH_STEP])
        (_, (result, _)), _, _ = self.both(rig, layouts_then_cache)
        self.assertEqual(result, NOTHING_SEEN)


class GenerationPolicyTests(HookCase):
    def test_policies_and_profile_support(self):
        def read(gpu):
            return gpu._current_limit_generation_policies(), gpu._current_limit_profile_supported()
        for arch, policies in ((7, NV.GPU.CURRENT_LIMIT_GENERATION_POLICIES[7]), (4, NV.GPU.CURRENT_LIMIT_GENERATION_POLICIES[4]),
                               (NO_POLICY_ARCH, {}), (None, {})):
            with self.subTest(arch=arch):
                (_, result), _, _ = self.both(HookRig(arch=arch), read)
                self.assertEqual(result, (policies, bool(policies)))
        (_, result), _, _ = self.both(HookRig(nvml_devid=0x1B02, python_nvapi_ok=False), read)
        self.assertEqual(result, (NV.GPU.CURRENT_LIMIT_GENERATION_POLICIES[7], False))


# ---- deviation D5 -------------------------------------------------------------------------------

class DeviationD5Tests(HookCase):
    """DESIGN-TRANSPORT.md 3.4, Deviation D5: when no hook could be installed and the escape
    capability is incomplete, the native message appends the capability detail. The class
    (a retryable ValueError, fixed False) is main's on both sides (D4)."""

    def assert_d5(self, rig, suffix):
        python, native_run = self.sides(rig, transport_then_cache)
        self.assertEqual(python[0], NOTHING_SEEN)
        kind, name, message, fixed = native_run[0]
        self.assertEqual((kind, name, fixed), ("raised", "ValueError", False))
        self.assertTrue(message.startswith(NOTHING_SEEN[2] + " ("), message)
        self.assertTrue(message.endswith(")"), message)
        detail = message[len(NOTHING_SEEN[2]) + 2:-1]
        self.assertTrue(detail.startswith(suffix), detail)
        self.assertEqual(python[1], native_run[1])  # the cached attributes agree
        self.assertEqual(rig.calls, [])
        return detail

    def test_missing_export_is_named(self):
        for name in (ENUM2, QUERY, CLOSE):
            with self.subTest(name):
                detail = self.assert_d5(HookRig([ARCH_STEP, owner(power_packet())], missing={name}),
                                        f"gdi32 lacks {name}")
                self.assertEqual(detail, f"gdi32 lacks {name}")

    def test_missing_escape_is_the_xpdm_wording(self):
        self.assert_d5(HookRig([ARCH_STEP, owner(power_packet())], missing={ESCAPE}),
                       "XPDM: no D3DKMT escape path")

    def test_gdi32_not_loadable(self):
        self.assert_d5(HookRig([ARCH_STEP, owner(power_packet())], gdi=False), "gdi32.dll not loadable: ")

    def test_layouts_carry_the_same_detail(self):
        rig = HookRig([ARCH_STEP, owner(power_packet())], missing={ENUM2})
        python, native_run = self.sides(rig, layouts_then_cache)
        self.assertEqual(python[0], NOTHING_SEEN)
        self.assertEqual(native_run[0][2], NOTHING_SEEN[2] + " (gdi32 lacks D3DKMTEnumAdapters2)")

    def test_a_complete_capability_adds_nothing(self):
        # No hook for another reason (no generation policy) with gdi32 complete: identical text.
        rig = HookRig([ARCH_STEP, owner(power_packet())], arch=NO_POLICY_ARCH)
        (_, (result, _)), _, _ = self.both(rig, transport_then_cache)
        self.assertEqual(result, NOTHING_SEEN)

    def test_the_detail_is_attached_only_when_gdi32_was_the_cause(self):
        # A capture that stops earlier (no generation policy, no PowerPolInfo, NVAPI disabled by a
        # pairing refusal) never reaches the gdi32 check, so the native text names neither reason,
        # exactly as Python's does, even though this image also lacks D3DKMTEnumAdapters2.
        for label, kw in (("no generation policy", dict(arch=NO_POLICY_ARCH)), ("no PowerPolInfo", dict(power=False)),
                          ("NVAPI disabled", dict(nvml_devid=0x1B02, python_nvapi_ok=False))):
            with self.subTest(label):
                rig = HookRig([ARCH_STEP], missing={ENUM2}, **kw)
                python, native_run = self.sides(rig, transport_then_cache)
                self.assertEqual(python[0], NOTHING_SEEN)
                self.assertEqual(native_run[0], NOTHING_SEEN)
                self.assertEqual(python[1], native_run[1])
                self.assertEqual(rig.calls, [])


# ---- pure: the thunk bytes ----------------------------------------------------------------------

class ThunkTests(unittest.TestCase):
    def test_x64_is_an_absolute_indirect_jump(self):
        for address, target in ((0x1000, 0x2000), (0x7FFE12345678, 0x00007FF600001000),
                                (0, 0xFFFFFFFFFFFFFFFF), (0xFFFFFFFFFFFFFFF0, 0)):
            with self.subTest(address=hex(address), target=hex(target)):
                self.assertEqual(native.hook_thunk(address, target, 8),
                                 b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", target))

    def test_x86_is_a_rel32_jump_from_the_end_of_the_instruction(self):
        for address, target in ((0x1000, 0x2000), (0x00401000, 0x00001000), (0x10, 0x10),
                                (0xFFFFFFF0, 0x10), (0x10, 0xFFFFFFF0), (0x7FFFFFFF, 0x80000004)):
            with self.subTest(address=hex(address), target=hex(target)):
                self.assertEqual(native.hook_thunk(address, target, 4),
                                 b"\xe9" + struct.pack("<I", (target - (address + 5)) % 2 ** 32))
        self.assertEqual(native.hook_thunk(0xFFFFFFF0, 0x10, 4), b"\xe9\x1b\x00\x00\x00")
        self.assertEqual(native.hook_thunk(0x00401000, 0x00001000, 4), b"\xe9\xfb\xff\xbf\xff")

    def test_other_pointer_widths_are_refused(self):
        for width in (0, 2, 16):
            with self.subTest(width=width), self.assertRaises(ValueError):
                native.hook_thunk(0x1000, 0x2000, width)

    def test_capability_reports_the_overwrite_size(self):
        rig = TransportRig()
        gpu = rig.native_gpu()
        try:
            cap = native.escape_capability()
        finally:
            gpu.close()
            native.set_export_resolver(None)
        self.assertEqual(cap["patch_bytes"], len(native.hook_thunk(0x1000, 0x2000, ctypes.sizeof(ctypes.c_void_p))))


if __name__ == "__main__":
    unittest.main()
