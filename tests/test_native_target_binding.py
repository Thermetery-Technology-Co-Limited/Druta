# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native paired target binding (increment A1) against its Python originals.

native.Gpu._target_binding / _client_power_binding / _target_binding_reset /
nvapi_interface_present run dc_gpu_target_bind, dc_gpu_target_reset and
dc_nvapi_interface_present. The originals are
perf_diagnostics.PerfDiagnosticsReader._binding and
client_power_runtime.ClientPowerController._binding, run here on an
nvbackend.GPU twin (GPU.__new__, so slot() and arch() are the real methods).

One set of fake driver callbacks serves both sides. The twin's identity
records (nvapi.selected, nvapi.gpu, nvml.selected, pairing_error) are copied
from what the native handle exposes, so both sides judge one identity; the
pairing itself is covered by tests/test_native_identity.py.

Not reachable through the public fakes, and so covered only by
tests/test_perf_diagnostics.py and tests/test_client_power_runtime.py: an NVAPI
handle that changes without its target binding, a slot that differs only in
PCI function, NVML devid/subsys disagreeing with NVAPI after a successful
pairing, the sticky "GPU binding changed" refusal, and the client-power
"complete paired PCI identity" refusal (native devid is a u32 shifted right by
16 and subsys a u32, so both always fit)."""
import ctypes
import threading
from types import SimpleNamespace
import unittest

from druta import native, nvbackend
from druta.client_power_runtime import ClientPowerController, ClientPowerError
from druta.perf_diagnostics import LAYOUTS, PerfDiagnosticError, PerfDiagnosticsReader
from tests.test_native_identity import FakeNvapi, FakeNvml, TITAN, install, write_u32

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend
GET_ARCH_INFO = 0xD8265D24
PERF_IDS = tuple(i for layout in LAYOUTS.values() for i in (layout.info_id, layout.control_id))
EXTRA_ID = 0x57B5A5DF


@CDECL(ctypes.c_int)
def _unused():
    return 0


class Card:
    """Fake driver state for one card. nvmlDeviceGetArchitecture answers from
    self.arch (a value, or an int status when self.arch_status is nonzero);
    NvAPI_GPU_GetArchInfo answers self.nvapi_arch when that is set."""

    def __init__(self, *, nvml_device=TITAN, nvapi_gpu=TITAN, with_nvml=True, with_nvapi=True, nvapi_init=0,
                 arch=7, arch_status=0, arch_export=True, nvapi_arch=None, interfaces=(PERF_IDS + (EXTRA_ID,))):
        self.arch, self.arch_status, self.nvapi_arch = arch, arch_status, nvapi_arch
        self.with_nvml, self.with_nvapi = with_nvml, with_nvapi
        self.nvml = FakeNvml([nvml_device])
        self.nvapi = FakeNvapi([nvapi_gpu], init_status=nvapi_init)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def architecture(device, pointer):
            write_u32(pointer, self.arch)
            return self.arch_status
        if arch_export:
            self.nvml.exports["nvmlDeviceGetArchitecture"] = architecture

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def arch_info(handle, pointer):
            info = NV._GpuArchInfo.from_address(pointer)
            if info.version != NV.NvAPI.ver(NV._GpuArchInfo, 2):
                return -9
            info.architecture = self.nvapi_arch
            return 0
        if nvapi_arch is not None:
            self.nvapi.interfaces[GET_ARCH_INFO] = arch_info
        for identifier in interfaces:
            self.nvapi.interfaces[identifier] = _unused

    def native_gpu(self):
        install(self.nvml if self.with_nvml else None, self.nvapi if self.with_nvapi else None)
        return native.Gpu()

    def python_twin(self, opened):
        """An nvbackend.GPU carrying the identity the native handle `opened` exposes."""
        exports = self.nvml.exports

        class Dll:
            def __getattr__(self, name):
                try:
                    return exports[name]
                except KeyError:
                    raise AttributeError(name)

        gpu = NV.GPU.__new__(NV.GPU)
        gpu._lock = threading.RLock()
        selected = opened.nvapi.selected
        api = SimpleNamespace(ok=opened.nvapi.ok, err_detail=opened.nvapi.err_detail, ver=NV.NvAPI.ver,
                              gpu=opened.nvapi.gpu, selected=dict(selected) if selected else None)
        if self.with_nvapi:
            binder = SimpleNamespace(_qi=self.nvapi.query)
            api._i = lambda ident, *argtypes: NV.NvAPI._i(binder, ident, *argtypes)  # the real binder
        else:
            api._i = lambda *a: None
        api.GetArchInfo = api._i(GET_ARCH_INFO, NV.PTR, NV.PTR)  # as NvAPI.__init__ binds it
        nvml = NV.Nvml.__new__(NV.Nvml)
        other = opened.nvml.selected
        nvml.ok, nvml.err_detail = opened.nvml.ok, opened.nvml.err_detail
        nvml.dll = Dll()
        nvml.dev = 0x1000 + other["nvml_index"] if other else None
        nvml.selected = {k: other[k] for k in ("slot", "devid", "subsys")} if other else None
        gpu.nvapi, gpu.nvml = api, nvml
        gpu.pairing_error = opened.pairing_error
        gpu.requested_slot = opened.requested_slot
        gpu.static = {}
        return gpu


def outcome(call):
    try:
        return ("ok", call())
    except (PerfDiagnosticError, ClientPowerError) as exc:
        return (type(exc).__name__, exc.kind, str(exc))


def perf(gpu, reader):
    """(slot, architecture) of a PERF binding, asserting the api element is gpu.nvapi."""
    def call():
        api, slot, architecture = reader()
        assert api is gpu.nvapi
        return slot, architecture
    return outcome(call)


class TargetBindingParity(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, card):
        """Both bindings on each side from one fake state: {"perf": ..., "client": ...} per side."""
        opened = card.native_gpu()
        try:
            twin = card.python_twin(opened)
            python = {"perf": perf(twin, PerfDiagnosticsReader(twin)._binding),
                      "client": outcome(ClientPowerController(twin)._binding)}
            core = {"perf": perf(opened, opened._target_binding),
                    "client": outcome(opened._client_power_binding)}
        finally:
            opened.close()
        self.assertEqual(python, core, "Python and native bindings differ")
        return python

    # ---- 1. happy path ------------------------------------------------------------------------- #

    def test_paired_card_binds_on_both_paths(self):
        result = self.both(Card())
        self.assertEqual(result["perf"], ("ok", ("0000:01:00.0", 7)))
        self.assertEqual(result["client"], ("ok", {"slot": "0000:01:00.0", "architecture": 7,
                                                   "device_id": 0x1E02, "subsystem_id": 0x12A010DE}))

    def test_binding_is_repeatable_and_survives_a_fresh_reader(self):
        card = Card()
        opened = card.native_gpu()
        try:
            twin = card.python_twin(opened)
            reader = PerfDiagnosticsReader(twin)
            python = [perf(twin, reader._binding), perf(twin, reader._binding),
                      perf(twin, PerfDiagnosticsReader(twin)._binding)]
            core = [perf(opened, opened._target_binding), perf(opened, opened._target_binding)]
            opened._target_binding_reset()
            core.append(perf(opened, opened._target_binding))
            controller = ClientPowerController(twin)
            python += [outcome(controller._binding), outcome(controller._binding)]
            core += [outcome(opened._client_power_binding), outcome(opened._client_power_binding)]
        finally:
            opened.close()
        self.assertEqual(python, core)
        self.assertEqual(core[:3], [("ok", ("0000:01:00.0", 7))] * 3)
        self.assertEqual(core[3], core[4])

    # ---- 2-4, 6. identity and availability refusals -------------------------------------------- #

    def test_pairing_mismatch_is_an_identity_refusal_with_the_pairing_message(self):
        card = Card(nvapi_gpu=dict(TITAN, devid=0x1B02, subsys=0x119A10DE))
        result = self.both(card)
        message = ("NVAPI and NVML disagree about the card at 0000:01:00.0: NVAPI says devid 1B02/subsys 119A10DE, "
                   "NVML says devid 1E02/subsys 12A010DE. NVAPI disabled rather than risk writing the wrong card.")
        self.assertEqual(result["perf"], ("PerfDiagnosticError", "identity", message))
        self.assertEqual(result["client"], ("ClientPowerError", "identity", message))

    def test_missing_nvapi_is_transient(self):
        unavailable = "A live PCI-paired NVAPI handle is unavailable"
        for card in (Card(with_nvapi=False), Card(nvapi_init=-1)):
            with self.subTest(with_nvapi=card.with_nvapi):
                result = self.both(card)
                self.assertEqual(result["perf"], ("PerfDiagnosticError", "transient", unavailable))
                self.assertEqual(result["client"], ("ClientPowerError", "transient", unavailable))

    def test_nonzero_pci_domain_is_refused(self):
        # NVML says 0001:01:00.0 and NVAPI 0000:01:00.0; they pair because nvbackend.same_slot compares
        # bus and device only. The binding still refuses any domain other than 0. That refusal is ported
        # verbatim and flagged for the user (header comment of native/src/backend/target.h): it blocks
        # client power, PERF and clock-BIOS on such hosts.
        card = Card(nvml_device=dict(TITAN, domain=1))
        opened = card.native_gpu()
        try:
            self.assertEqual((opened.slot(), opened.pairing_error), ("0001:01:00.0", ""))
        finally:
            opened.close()
        result = self.both(card)
        refusal = "NVAPI handle does not identify the selected PCI address"
        self.assertEqual(result["perf"], ("PerfDiagnosticError", "identity", refusal))
        self.assertEqual(result["client"], ("ClientPowerError", "identity", refusal))

    def test_nvml_without_pci_identity_is_refused_as_a_different_address(self):
        # NVML selected a device but read no PCI info: its record exists with slot "", which parses to
        # nothing, so both sides refuse instead of skipping the NVML cross-check.
        card = Card(nvml_device=dict(TITAN, pci_status=3))
        result = self.both(card)
        refusal = "NVML and NVAPI identify different PCI addresses"
        self.assertEqual(result["perf"], ("PerfDiagnosticError", "identity", refusal))
        self.assertEqual(result["client"], ("ClientPowerError", "identity", refusal))

    def test_without_nvml_the_architecture_comes_from_nvapi_or_nothing(self):
        withheld = self.both(Card(with_nvml=False))
        self.assertEqual(withheld["perf"], ("PerfDiagnosticError", "transient", "GPU architecture could not be read"))
        self.assertEqual(withheld["client"], ("ClientPowerError", "transient", "GPU architecture could not be read"))
        served = self.both(Card(with_nvml=False, nvapi_arch=0x160))  # NV_GPU_ARCHITECTURE_TU100 -> Turing
        self.assertEqual(served["perf"], ("ok", ("0000:01:00.0", 6)))
        self.assertEqual(served["client"][1]["architecture"], 6)
        unlisted = self.both(Card(with_nvml=False, nvapi_arch=0x999))
        self.assertEqual(unlisted["perf"], ("PerfDiagnosticError", "transient", "GPU architecture could not be read"))

    # ---- 7. architecture matrix ---------------------------------------------------------------- #

    def test_architecture_matrix(self):
        unsupported = ("unsupported", "Modern PERF groups require a Pascal-or-later architecture")
        unread = ("transient", "GPU architecture could not be read")
        cases = [
            (dict(arch=1), unsupported), (dict(arch=2), unsupported), (dict(arch=3), unsupported),
            (dict(arch=4), 4), (dict(arch=9), 9), (dict(arch=10), 10),
            (dict(arch=99), unread), (dict(arch=0), unread), (dict(arch=7, arch_status=3), unread),
            (dict(arch_export=False), unread),
        ]
        for options, expected in cases:
            with self.subTest(**options):
                result = self.both(Card(**options))
                if isinstance(expected, tuple):
                    self.assertEqual(result["perf"], ("PerfDiagnosticError", *expected))
                    self.assertEqual(result["client"], ("ClientPowerError", *expected))
                else:
                    self.assertEqual(result["perf"], ("ok", ("0000:01:00.0", expected)))

    def test_an_unrecognised_architecture_read_does_not_stick(self):
        card = Card(arch=99)
        opened = card.native_gpu()
        try:
            twin = card.python_twin(opened)
            reader = PerfDiagnosticsReader(twin)
            python, core = [perf(twin, reader._binding)], [perf(opened, opened._target_binding)]
            card.arch = 7
            python.append(perf(twin, reader._binding))
            core.append(perf(opened, opened._target_binding))
            card.arch = 3  # a recognised architecture is cached: a later read is not taken
            python.append(perf(twin, reader._binding))
            core.append(perf(opened, opened._target_binding))
        finally:
            opened.close()
        self.assertEqual(python, core)
        self.assertEqual(core, [("PerfDiagnosticError", "transient", "GPU architecture could not be read"),
                                ("ok", ("0000:01:00.0", 7)), ("ok", ("0000:01:00.0", 7))])

    # ---- 8. interface presence ----------------------------------------------------------------- #

    def test_interface_presence_matches_the_python_binder(self):
        ids = PERF_IDS + (EXTRA_ID,)
        for present in (ids, ids[::2], ()):
            card = Card(interfaces=present)
            opened = card.native_gpu()
            try:
                twin = card.python_twin(opened)
                python = {i: twin.nvapi._i(i, ctypes.c_void_p, ctypes.c_void_p) is not None for i in ids}
                core = {i: opened.nvapi_interface_present(i) for i in ids}
            finally:
                opened.close()
            with self.subTest(present=[hex(i) for i in present]):
                self.assertEqual(python, core)
                self.assertEqual(core, {i: i in present for i in ids})

    def test_interface_presence_without_nvapi(self):
        card = Card(with_nvapi=False)
        opened = card.native_gpu()
        try:
            twin = card.python_twin(opened)
            for ident in PERF_IDS + (EXTRA_ID,):
                self.assertFalse(opened.nvapi_interface_present(ident))
                self.assertIsNone(twin.nvapi._i(ident, ctypes.c_void_p, ctypes.c_void_p))
        finally:
            opened.close()


if __name__ == "__main__":
    unittest.main()
