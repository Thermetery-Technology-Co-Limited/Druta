# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Combined-prefix storage, target binding and uncertain completion contracts."""
import ctypes
import unittest

from druta import nvbackend, railctl
from druta.controllers import up9512r as u


class CombinedBus:
    """Model separate register storage and request acknowledgments."""

    def __init__(self, *, architecture=0x190, port=2, address=0x25):
        self.ok = True
        self.gpu = ctypes.c_void_p(123)
        self.selected = {'slot': '0000:03:00.0', 'devid': 0x2782, 'subsys': 0x98765432}
        self.architecture, self.port, self.address = architecture, port, address
        self.regs = {0x27: 0, 0x28: 0x2B, 0x0A: 0, 0x0B: 0, 0x0C: 0x0B,
                     0x2A: 0x2C, 0x39: 0x94, 0x2C: 11, 0x2D: 90}
        self.arch_calls, self.reads, self.combined, self.writes = [], [], [], []
        self.arch_hook = self.read_hook = self.combined_hook = None
        self.missing = set()

    def GetArchInfo(self, gpu, pointer):
        assert gpu.value == self.gpu.value
        info = ctypes.cast(pointer, ctypes.POINTER(nvbackend._GpuArchInfo)).contents
        self.arch_calls.append(info.version >> 16)
        if self.arch_hook is not None:
            result = self.arch_hook(info)
            if result is not None:
                return result
        info.architecture = self.architecture
        return 0

    def _i(self, api, *args):
        if api in self.missing:
            return None
        return {railctl.I2C_READ_EX: self.read, railctl.I2C_WRITE_EX: self.write}[api]

    def packet(self, gpu, pointer, extra):
        assert gpu.value == self.gpu.value
        p = ctypes.cast(pointer, ctypes.POINTER(railctl._V3)).contents
        flags = ctypes.cast(extra, ctypes.POINTER(railctl.u32 * 2)).contents
        assert p.version == railctl.VER3 and p.cbSize == 1
        assert p.displayMask == 0 and p.bIsDDCPort == 0
        assert p.i2cDevAddress == self.address << 1 and p.portId == self.port
        assert p.bIsPortIdSet == 1 and p.i2cSpeed == 0xFFFF and p.i2cSpeedKhz == 0
        assert list(flags) == [0, 0]
        return p, flags

    def read(self, gpu, pointer, extra):
        p, flags = self.packet(gpu, pointer, extra)
        reg = int(p.pbI2cRegAddress[0])
        if p.regAddrSize == 2:
            value = int(p.pbI2cRegAddress[1])
            self.combined.append((reg, value, int(p.pbData[0])))
            if self.combined_hook is not None:
                result = self.combined_hook(reg, value, p, flags)
                if result is not None:
                    return result
            self.regs[reg] = value
            p.pbData[0] = value
            return 0
        assert p.regAddrSize == 1
        self.reads.append(reg)
        if self.read_hook is not None:
            result = self.read_hook(reg, p, flags)
            if result is not None:
                return result
        p.pbData[0] = self.regs[reg]
        return 0

    def write(self, gpu, pointer, extra):
        p, _ = self.packet(gpu, pointer, extra)
        assert p.regAddrSize == 1
        reg, value = int(p.pbI2cRegAddress[0]), int(p.pbData[0])
        self.writes.append((reg, value))
        self.regs[reg] = value
        return 0


class UP9512RCombinedTests(unittest.TestCase):
    def make(self, **kwargs):
        bus = CombinedBus(**kwargs)
        return bus, u.UP9512R(bus, port=bus.port, addr7=bus.address)

    def controls(self, bus):
        return tuple(bus.regs[r] for r in u.CONTROL_REGISTERS)

    def test_ada_live_v2_and_v1_layouts_select_combined_on_alternate_gpu_and_bus(self):
        for revision in (2, 1):
            for port, address in ((0, 0x08), (5, 0x35), (7, 0x77)):
                with self.subTest(revision=revision, port=port, address=address):
                    bus, rail = self.make(port=port, address=address)
                    bus.arch_hook = lambda info: -9 if info.version >> 16 > revision else None
                    self.assertTrue(rail._raw_write(0x0A, 0x12))
                    self.assertEqual(bus.regs[0x0A], 0x12)
                    self.assertEqual(bus.arch_calls, [2] if revision == 2 else [2, 1])
                    self.assertEqual([(r, v) for r, v, _ in bus.combined], [(0x0A, 0x12)])
                    self.assertEqual(bus.writes, [])

    def test_non_ada_architectures_and_missing_arch_export_keep_conventional_transport(self):
        for architecture in (0x140, 0x160, 0x170, 0x1B0, None):
            with self.subTest(architecture=architecture):
                bus, rail = self.make(architecture=architecture)
                if architecture is None:
                    bus.GetArchInfo = None
                self.assertTrue(rail._raw_write(0x0B, 0x23))
                self.assertEqual(bus.writes, [(0x0B, 0x23)])
                self.assertEqual(bus.combined, [])

    def test_architecture_transient_failure_is_retryable_without_a_write_or_v1_fallback(self):
        bus, rail = self.make()
        bus.arch_hook = lambda info: -1
        with self.assertRaisesRegex(ValueError, 'architecture query failed'):
            rail._raw_write(0x0A, 1)
        self.assertEqual(bus.arch_calls, [2])
        self.assertEqual(bus.combined + bus.writes, [])
        bus.arch_hook = None
        self.assertTrue(rail._raw_write(0x0A, 1))
        self.assertEqual(bus.arch_calls, [2, 2])
        self.assertEqual(len(bus.combined), 1)

    def test_unavailable_architecture_layout_or_bad_response_blocks_dispatch(self):
        def changed_version(info):
            info.architecture = 0x190
            info.version ^= 1
            return 0
        hooks = {
            'unsupported layouts': lambda info: -9,
            'zero architecture': lambda info: (setattr(info, 'architecture', 0) or 0),
            'changed version': changed_version,
        }
        for name, hook in hooks.items():
            with self.subTest(name=name):
                bus, rail = self.make()
                bus.arch_hook = hook
                with self.assertRaises(ValueError):
                    rail._raw_write(0x0A, 1)
                self.assertEqual(bus.combined + bus.writes, [])

    def test_target_rebinding_during_architecture_query_blocks_dispatch(self):
        bus, rail = self.make()
        def change_target(info):
            info.architecture = 0x190
            bus.selected['slot'] = '0000:04:00.0'
            return 0
        bus.arch_hook = change_target
        with self.assertRaises(ValueError):
            rail._raw_write(0x0A, 1)
        self.assertEqual(bus.combined + bus.writes, [])

    def test_missing_combined_export_never_uses_available_write_export(self):
        bus, rail = self.make()
        bus.missing.add(railctl.I2C_READ_EX)
        self.assertFalse(rail._raw_write(0x0A, 1))
        self.assertEqual(bus.combined + bus.writes, [])
        bus.missing.clear()
        self.assertTrue(rail._raw_write(0x0A, 1))
        self.assertEqual(bus.writes, [])

    def test_failed_status_untouched_output_or_exception_has_one_dispatch_and_no_fallback(self):
        def raises(reg, value, packet, flags):
            raise RuntimeError('uncertain completion')
        for name, hook in (('failed status', lambda *args: -1),
                           ('untouched output', lambda *args: 0), ('exception', raises)):
            with self.subTest(name=name):
                bus, rail = self.make()
                bus.combined_hook = hook
                if name == 'exception':
                    with self.assertRaisesRegex(RuntimeError, 'uncertain completion'):
                        rail._raw_write(0x0A, 1)
                else:
                    self.assertFalse(rail._raw_write(0x0A, 1))
                self.assertEqual(len(bus.combined), 1)
                self.assertEqual(bus.writes, [])

    def test_a5_and_complement_values_need_no_sentinel_retry(self):
        bus, rail = self.make()
        for value in (0xA5, 0x5A, 0, 0xFF):
            self.assertTrue(rail._raw_write(0x0A, value))
            self.assertEqual(bus.regs[0x0A], value)
        self.assertEqual(len(bus.combined), 4)
        self.assertTrue(all(value != sentinel for _, value, sentinel in bus.combined))
        self.assertEqual(bus.writes, [])

    def test_corrupted_prefix_pointer_geometry_or_extra_flags_rejects_success_status(self):
        mutations = {
            'prefix': lambda p, f: p.pbI2cRegAddress.__setitem__(1, 0x33),
            'register pointer': lambda p, f: setattr(p, 'pbI2cRegAddress', ctypes.POINTER(railctl.u8)()),
            'data pointer': lambda p, f: setattr(p, 'pbData', ctypes.POINTER(railctl.u8)()),
            'data size': lambda p, f: setattr(p, 'cbSize', 0),
            'prefix size': lambda p, f: setattr(p, 'regAddrSize', 1),
            'address': lambda p, f: setattr(p, 'i2cDevAddress', 0x4C),
            'port': lambda p, f: setattr(p, 'portId', 3),
            'version': lambda p, f: setattr(p, 'version', 0),
            'extra flags': lambda p, f: f.__setitem__(0, 1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                bus, rail = self.make()
                def corrupt(reg, value, packet, flags):
                    packet.pbData[0] = value
                    mutate(packet, flags)
                    return 0
                bus.combined_hook = corrupt
                self.assertFalse(rail._raw_write(0x0A, 1))
                self.assertEqual(len(bus.combined), 1)
                self.assertEqual(bus.writes, [])

    def test_selected_target_change_before_or_during_dispatch_invalidates_write(self):
        for during in (False, True):
            with self.subTest(during=during):
                bus, rail = self.make()
                if during:
                    def change(reg, value, packet, flags):
                        packet.pbData[0] = value
                        bus.selected['subsys'] += 1
                        return 0
                    bus.combined_hook = change
                else:
                    bus.selected['subsys'] += 1
                self.assertFalse(rail._raw_write(0x0A, 1))
                self.assertEqual(len(bus.combined), int(during))
                self.assertEqual(bus.writes, [])

    def test_independent_reads_reject_success_with_mutated_geometry_or_flags(self):
        for mutate in (lambda p, f: setattr(p, 'regAddrSize', 2),
                       lambda p, f: p.pbI2cRegAddress.__setitem__(0, 0x28),
                       lambda p, f: setattr(p, 'i2cDevAddress', 0x4C),
                       lambda p, f: setattr(p, 'pbData', ctypes.POINTER(railctl.u8)()),
                       lambda p, f: f.__setitem__(1, 1)):
            bus, rail = self.make()
            def corrupt(reg, packet, flags):
                packet.pbData[0] = 0
                mutate(packet, flags)
                return 0
            bus.read_hook = corrupt
            self.assertIsNone(rail.read(0x27))
            self.assertEqual(bus.reads, [0x27])
            self.assertEqual(bus.combined + bus.writes, [])

    def test_transaction_requires_independent_storage_not_just_combined_echo(self):
        bus, rail = self.make()
        before = self.controls(bus)
        def echo_without_commit(reg, value, packet, flags):
            packet.pbData[0] = value
            return 0
        bus.combined_hook = echo_without_commit
        ok, message = rail.set_offset_mv(10, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn('readback', message)
        self.assertEqual(self.controls(bus), before)
        self.assertEqual([(r, v) for r, v, _ in bus.combined], [(0x0A, 0x11)])
        self.assertEqual(bus.writes, [])

    def test_apply_and_reset_use_independent_readback_enable_last_disable_first(self):
        bus, rail = self.make()
        before = self.controls(bus)
        self.assertTrue(rail.set_offset_mv(20, acknowledged=True)[0])
        self.assertEqual(self.controls(bus), (0x22, 0x22, 0x2B, 0x6C))
        self.assertEqual([(r, v) for r, v, _ in bus.combined],
                         [(0x0A, 0x22), (0x0B, 0x22), (0x0C, 0x2B), (0x2A, 0x6C)])
        self.assertTrue(all(r in bus.reads for r in u.CONTROL_REGISTERS))
        bus.combined.clear()
        self.assertTrue(rail.reset()[0])
        self.assertEqual(self.controls(bus), before)
        self.assertEqual(bus.combined[0][:2], (0x2A, 0x2C))
        self.assertEqual(bus.writes, [])

    def test_uncertain_committed_write_restores_with_the_selected_route_even_if_arch_query_breaks(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                bus, rail = self.make()
                before = self.controls(bus)
                def fault(reg, value, packet, flags):
                    if len(bus.combined) == 1:
                        bus.regs[reg] = value
                        bus.arch_hook = lambda info: -1
                        if raises:
                            raise RuntimeError('uncertain committed write')
                        return -1
                bus.combined_hook = fault
                ok, message = rail.set_offset_mv(10, acknowledged=True)
                self.assertFalse(ok)
                self.assertIn('exact entry control restored', message)
                self.assertEqual(self.controls(bus), before)
                self.assertEqual([(r, v) for r, v, _ in bus.combined], [(0x0A, 0x11), (0x0A, 0)])
                self.assertEqual(bus.arch_calls, [2])
                self.assertEqual(bus.writes, [])


if __name__ == '__main__':
    unittest.main()
