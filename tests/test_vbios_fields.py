# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime-image parsing: portable geometry and exact selected-byte preservation."""

import hashlib
import struct
import unittest

from druta import vbios_fields as vf


def fixture(device=0x1234, power_at=0x3800, captured=0x5000):
    """Synthetic, incomplete archival tail; never a real or padded GPU ROM."""
    blob = bytearray(captured)

    def image(start, size, kind, magic=b"PCIR", header_delta=0x80):
        blob[start : start + 2] = b"\x55\xaa" if magic == b"PCIR" else b"VN"
        struct.pack_into("<H", blob, start + 24, header_delta)
        at = start + header_delta
        blob[at : at + 4] = magic
        struct.pack_into("<HHHH", blob, at + 4, 0x10DE, device, 0, 24)
        struct.pack_into("<H", blob, at + 16, size // 512)
        blob[at + 20] = kind
        blob[at + 21] = 128

    image(0, 0x1000, 0)
    image(0x1000, 0x1000, 3)
    image(0x2000, 0x1000, 0xE0, header_delta=0x700)
    image(0x3000, 0x5000, 0xE0, b"NPDS")
    bit = 0x100
    blob[bit : bit + 6] = b"\xff\xb8BIT\x00"
    blob[bit + 8 : bit + 11] = bytes((12, 6, 2))
    blob[bit + 11] = (-sum(blob[bit : bit + 11])) & 255
    struct.pack_into("<BBHH", blob, bit + 12, ord("P"), 2, 0x130, 0x200)
    struct.pack_into("<BBHH", blob, bit + 18, ord("i"), 2, 5, 0x360)
    struct.pack_into("<I", blob, 0x200 + 0x2C, power_at - 0x1000)
    struct.pack_into("<IB", blob, 0x360, 0x12345678, 0x9A)
    directory = 0x2200
    struct.pack_into("<IIII", blob, directory, 3, 16, 3, device)
    base = directory + 16 + 3 * 60
    size = 0x38 + 20 * 0x66
    for i, (oid, start, length) in enumerate(((0xFB, base, 8), (0xBE, 0x2700, 24), (0x13, power_at, size))):
        struct.pack_into("<III", blob, directory + 16 + i * 60, oid, start - base, length)
    blob[power_at : power_at + 4] = bytes((0x40, 0x38, 0x66, 20))
    for i in range(18):
        row = power_at + 0x38 + 0x66 * i
        blob[row] = 12
        struct.pack_into("<I", blob, row + 1, 1 if i == 13 else 0)
        blob[row + 5] = i
        struct.pack_into("<III", blob, row + 13, 100000, 360000, 450000)
    return bytes(blob)


def apply(blob, plan):
    out = bytearray(blob)
    for edit in plan["patches"]:
        at = edit["offset"]
        before, after = bytes.fromhex(edit["before"]), bytes.fromhex(edit["after"])
        assert blob[at : at + len(before)] == before
        assert len(before) == len(after)
        out[at : at + len(after)] = after
    return bytes(out)


def v30_power_fixture(header=0x2C, stride=0x4B, *, kind=1, extension=0, channel=5, unit=0):
    blob = bytearray(fixture())
    blob[0x2000:0x2002] = b"\x00\x00"
    start = 0x3800
    blob[start : start + header + stride] = bytes(header + stride)
    blob[start : start + 4] = bytes((0x30, header, stride, 1))
    row = start + header
    blob[row] = kind | (unit << 4)
    blob[row + 1] = channel
    struct.pack_into("<III", blob, row + 2, 100000, 310000, 380000)
    blob[row + 0x42] = extension
    return bytes(blob)


def extend(blob, tables):
    """Add bounded tables to the synthetic metadata directory (P pointers)."""
    b = bytearray(blob)
    count = struct.unpack_from("<I", b, 0x2208)[0]
    base = 0x2210 + count * 60
    entries = []
    for i in range(count):
        oid, off, size = struct.unpack_from("<III", b, 0x2210 + 60 * i)
        entries.append((oid, base + off, size))
    for ptr, oid, offset, data in tables:
        if offset + len(data) > len(b):
            b.extend(bytes(offset + len(data) - len(b)))
        b[offset : offset + len(data)] = data
        struct.pack_into("<I", b, 0x200 + ptr, offset - 0x1000 if offset >= 0x1000 else offset)
        if oid is not None:
            entries.append((oid, offset, len(data)))
    base = 0x2210 + len(entries) * 60
    entries[0] = (0xFB, base, 8)
    struct.pack_into("<I", b, 0x2208, len(entries))
    for i, (oid, offset, size) in enumerate(entries):
        struct.pack_into("<III", b, 0x2210 + 60 * i, oid, offset - base, size)
    return bytes(b)


def perf_fixture():
    controllers = bytearray(6 + 57 * 3)
    controllers[:4] = bytes((0x10, 6, 57, 3))
    for i, domain in enumerate((0, 2, 4)):
        row = 6 + 57 * i
        controllers[row] = 1
        struct.pack_into("<II", controllers, row + 3, 1024, 3072)
        controllers[row + 19 : row + 22] = bytes((domain, 1, 4))
    policies = bytearray(15)
    policies[:4] = bytes((0x10, 5, 10, 1))
    policies[5] = 0x81
    struct.pack_into("<I", policies, 11, 7)
    return extend(fixture(), [(0xA8, 0x60, 0x4500, controllers), (0xAC, 0x61, 0x4600, policies)])


def voltage_fixture():
    rail = bytearray(b"\xff" * (5 + 65))
    rail[:5] = bytes((0x30, 5, 65, 1, 0))
    rail[5:7] = bytes((1, 1))
    for offset, idx in ((13, 0), (15, 1), (17, 2), (33, 3), (19, 4)):
        struct.pack_into("<H", rail, 5 + offset, idx)
    base = 14 + 29 * 255
    vfe = bytearray(base + 25 * 6 + 5 * 16)
    vfe[:6] = bytes((0x30, 14, 29, 255, 25, 6))
    vfe[11] = 5
    struct.pack_into("<H", vfe, 12, 16)
    for i, value in enumerate((1000000, 20000, 1200000, 900000, 750000, -100000)):
        at = base + 25 * i
        vfe[at : at + 3] = bytes((1, 0, 255))
        struct.pack_into("<5f", vfe, at + 3, value, value, value, 0, 0)
        vfe[at + 23 : at + 25] = bytes((2, 255))
    at = base
    vfe[at] = 7
    vfe[at + 2] = 5
    vfe[at + 24] = 0
    struct.pack_into("<ff", vfe, at + 3, 900000, 1120000)
    struct.pack_into("<I", vfe, at + 11, 2 << 16)
    term = base + 25 * 6
    for j, (kind, value) in enumerate(((2, 0), (2, 1), (1, 1000000), *((1, 0),) * 5)):
        vfe[term + 5 * j] = kind
        struct.pack_into("<f" if kind == 1 else "<I", vfe, term + 5 * j + 1, value)
    # ALT is an independent constant head whose next equation is REL.
    vfe[base + 25 + 2] = vfe[base + 25 + 24] = 0
    return extend(fixture(), [(0x68, None, 0x800, rail), (0x20, 0x14, 0x5000, vfe)])


def polynomial_reference_fixture(target=1, coefficient=False):
    b = bytearray(voltage_fixture())
    base = 0x5000 + 14 + 29 * 255
    at = base + 3 * 25  # Existing INFO_VMIN selector; its output is read-only.
    b[at] = 7
    struct.pack_into("<I", b, at + 11, 8 | (1 << 16))
    for j in range(8, 12):
        term = base + 6 * 25 + j * 5
        b[term] = 1
        struct.pack_into("<f", b, term + 1, 1 if j == 9 else 0)
    term = base + 6 * 25 + (9 if coefficient else 8) * 5
    b[term] = 3
    struct.pack_into("<H", b, term + 1, target)
    return bytes(b)


def voltage_clock_fixture(target=0, kind=1, version=0x50):
    size = 23 if version == 0x50 else 15
    table = bytearray(8 + (size + 2 * 4 + 3) * 2)
    table[:8] = bytes((version, 8, size, 2, 2, 4, 1, 3))
    for i, root in enumerate((target, 65535)):
        at = 8 + i * (size + 11)
        table[at] = kind if i == 0 else 0
        if version == 0x50:
            struct.pack_into("<H", table, at + 3, root)
        else:
            table[at + 3], table[at + 13] = root & 255, root >> 8
    b = bytearray(extend(voltage_fixture(), [(0x124, 4, 0x7400, table)]))
    b[0x10A] = 3
    b[0x10B] = 0
    b[0x10B] = (-sum(b[0x100:0x10C])) & 255
    struct.pack_into("<BBHH", b, 0x118, ord("C"), 2, 0x40, 0x380)
    struct.pack_into("<I", b, 0x38C, 0x6400)
    return bytes(b)


def fan_fixture():
    cooler = bytearray(9 + 26 * 4)
    cooler[:9] = bytes((0x10, 9, 26, 4, 0, 1, 255, 255, 0))
    policy = bytearray(4 + 51 * 11)
    policy[:4] = bytes((0x20, 4, 51, 11))
    mapping = bytearray(4 + 5 * 4)
    mapping[:4] = bytes((0x10, 4, 5, 4))
    control = bytearray(4 + 13 * 4)
    control[:4] = bytes((0x10, 4, 13, 4))
    speed = bytearray(4 + 6 * 4)
    speed[:4] = bytes((0x10, 4, 6, 4))
    for i in (0, 1):
        row = 9 + 26 * i
        cooler[row] = 1
        cooler[row + 2 : row + 4] = bytes((30, 100))
        struct.pack_into("<H", cooler, row + 11, 27000)
        struct.pack_into("<HH", cooler, row + 14, 1000, 3000)
        mapping[4 + 5 * i : 6 + 5 * i] = bytes((1, i))
        control[4 + 13 * i] = 1
        control[6 + 13 * i] = i
        control[9 + 13 * i] = 0x30 | 1 << i
        speed[4 + 6 * i : 6 + 6 * i] = bytes((1, i))
        struct.pack_into("<H", speed, 6 + 6 * i, 1500)
    for i in (0, 1, 4, 5):
        p = 4 + 51 * i
        policy[p : p + 2] = bytes((1, 1 if i < 4 else 0))
        policy[p + 14 : p + 17] = bytes((30, 70, 100))
        for j in range(3):
            struct.pack_into("<HH", policy, p + 18 + 4 * j, (40 + 20 * j) * 32, 1000 + 1000 * j)
        struct.pack_into("<HH", policy, p + 30, 30 * 32, 35 * 32)
        policy[p + 50] = 0x17 if i < 4 else 0x07
    return extend(
        fixture(),
        [
            (0x58, 0x1D, 0x4400, cooler),
            (0x5C, 0x1E, 0x4500, policy),
            (0x64, 0x20, 0x4800, mapping),
            (0xDC, 0x80, 0x4900, control),
            (0xE0, 0x83, 0x4A00, speed),
        ],
    )


def factory_fixture():
    clocks = bytearray(13 + 21 * 9)
    clocks[:6] = bytes((0x50, 13, 15, 9, 3, 2))
    descriptors = (0x43, 0x84, 0x182, 0xC5, 0x106, 0x147, 0x8, 0x200, 0x1C1)
    for i, descriptor in enumerate(descriptors):
        row = 13 + 21 * i
        clocks[row] = 2
        struct.pack_into("<hh", clocks, row + 3, -1000, 1000)
        struct.pack_into("<H", clocks, row + 9, descriptor)
    factory = bytearray(27)
    factory[:7] = bytes((0x10, 7, 5, 2, 5, 2, 0))
    struct.pack_into("<Bi", factory, 7, 1, 75000)
    struct.pack_into("<BI", factory, 22, 1, 2760000)
    b = bytearray(extend(fixture(), [(0x98, 0x26, 0x4D00, factory), (0x90, 3, 0x4E00, clocks)]))
    b[0x10A] = 3
    b[0x10B] = 0
    b[0x10B] = (-sum(b[0x100:0x10C])) & 255
    struct.pack_into("<BBHH", b, 0x118, ord("C"), 2, 0x40, 0x380)
    struct.pack_into("<I", b, 0x388, 0x3E00)
    return bytes(b)


class RuntimeVbiosFields(unittest.TestCase):
    def test_partial_archive_is_not_padded(self):
        blob = fixture()
        report = vf.inspect(blob)
        self.assertEqual(report["byte_count"], len(blob))
        self.assertFalse(report["images"][-1]["complete"])
        self.assertEqual(report["identity"]["vbios_version_raw"], 0x12345678)
        self.assertEqual(report["identity"]["oem_revision"], 0x9A)
        field = next(f for f in report["fields"] if f["key"] == "power.2.maximum")
        self.assertEqual(field["offsets"], [0x3800 + 0x38 + 2 * 0x66 + 21])
        self.assertEqual(field["current"], 450000)

    def test_identity_and_table_offsets_are_not_allowlists(self):
        for device, offset in ((0x0001, 0x3900), (0xDEAD, 0x3A00)):
            blob = fixture(device, offset)
            p = vf.plan(blob, {"power.2.maximum": 999000})
            self.assertEqual(vf.inspect(apply(blob, p))["identity"]["pci_device"], device)
            self.assertEqual(p["patches"][0]["offset"], offset + 0x38 + 2 * 0x66 + 21)

    def test_runtime_class_binding_comes_from_v40_parser_on_alternate_board(self):
        blob = bytearray(fixture(0xBEEF, 0x4000))
        mapping = {
            1: 0,
            2: 3,
            3: 4,
            4: 27,
            5: 28,
            6: 17,
            7: 18,
            8: 19,
            9: 20,
            10: 31,
            11: 30,
            12: 33,
            13: 32,
            14: 34,
            15: 36,
            16: 35,
        }
        for i, kind in enumerate((*mapping, 17)):
            blob[0x4000 + 0x38 + 0x66 * i] = kind
        fields = {f["key"]: f for f in vf.inspect(blob)["fields"]}
        for i, (kind, runtime) in enumerate(mapping.items()):
            binding = fields[f"power.{i}.maximum"]["runtime_binding"]
            self.assertEqual((binding["vbios_type"], binding["runtime_type"]), (kind, runtime))
            self.assertTrue(binding["class_evidence"].endswith("power-class-mapping.json"))
        unknown = fields["power.16.maximum"]
        self.assertIsNone(unknown["runtime_binding"]["runtime_type"])
        self.assertEqual(unknown["capability"], "read-only")
        plan = vf.plan(blob, {"power.11.maximum": 999000})
        self.assertEqual(vf.inspect(apply(blob, plan))["identity"]["pci_device"], 0xBEEF)

    def test_legacy_power_classes_are_not_assigned_v40_mapping(self):
        blob = bytearray(fixture())
        blob[0x2000:0x2002] = b"\x00\x00"  # No V3 metadata image.
        blob[0x3800:0x3804] = bytes((0x30, 0x2C, 0x4B, 1))
        row = 0x3800 + 0x2C
        blob[row : row + 2] = bytes((12, 5))
        struct.pack_into("<III", blob, row + 2, 100000, 360000, 450000)
        field = next(f for f in vf.inspect(blob)["fields"] if f["key"] == "power.0.maximum")
        self.assertIsNone(field["runtime_binding"]["runtime_type"])
        self.assertIsNone(field["runtime_binding"]["class_evidence"])

    def test_minimal_edits_preserve_every_other_byte(self):
        blob = fixture()
        p = vf.plan(blob, {"power.2.maximum": 999000, "power.7.maximum": 999000, "power.8.maximum": 999000})
        result = apply(blob, p)
        self.assertEqual(p["expected_sha256"], hashlib.sha256(result).hexdigest())
        self.assertEqual(p["changed_byte_count"], 9)
        allowed = {a for i in (2, 7, 8) for a in range(0x3800 + 0x38 + i * 0x66 + 21, 0x3800 + 0x38 + i * 0x66 + 25)}
        self.assertTrue(all(i in allowed for i, (a, b) in enumerate(zip(blob, result, strict=True)) if a != b))
        self.assertEqual(len(blob), len(result))

    def test_roundtrip_restoration_is_exact(self):
        blob = fixture()
        raised = apply(blob, vf.plan(blob, {"power.2.maximum": 999000}))
        self.assertEqual(apply(raised, vf.plan(raised, {"power.2.maximum": 450000})), blob)

    def test_unit_flag_does_not_destroy_policy_flags(self):
        blob = bytearray(fixture())
        row = 0x3800 + 0x38 + 2 * 0x66
        struct.pack_into("<I", blob, row + 1, 0x148)
        original = bytes(blob)
        report = vf.inspect(blob)
        f = next(f for f in report["fields"] if f["key"] == "power.2.maximum")
        self.assertEqual(f["units"], "mW")
        result = apply(original, vf.plan(original, {"power.2.maximum": 999000}))
        self.assertEqual(result[row + 1 : row + 5], original[row + 1 : row + 5])
        ma = next(f for f in report["fields"] if f["key"] == "power.13.maximum")
        self.assertEqual(ma["units"], "mA")

    def test_linked_current_follows_structural_reference_not_fingerprint(self):
        b = bytearray(fixture(device=0xDEAD))
        source = 0x3800 + 0x38 + 7 * 0x66
        target = 0x3800 + 0x38 + 9 * 0x66
        relation_base = 0x3800 + 0x38 + 20 * 0x66
        b[0x3804:0x3806] = bytes((18, 3))
        struct.pack_into("<I", b, 0x2210 + 2 * 60 + 8, 0x38 + 20 * 0x66 + 18 * 3)
        struct.pack_into("<BBH", b, relation_base + 2 * 18, 1, 9, 4096)
        b[source] = 13
        struct.pack_into("<H", b, source + 33, 2)
        b[source + 80] = 0xA7  # unrelated bytes intentionally differ from any board corpus
        struct.pack_into("<I", b, source + 61, 45000)
        b[target] = 11
        struct.pack_into("<I", b, target + 1, 1)
        struct.pack_into("<III", b, target + 13, 1, 45000, 45000)
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        self.assertEqual(fields["power.7.linked_current"]["capability"], "editable")
        self.assertEqual(fields["power.7.linked_current"]["linked_relationship"], 2)
        self.assertEqual(fields["power.7.linked_current"]["linked_policy"], 9)
        self.assertEqual(fields["power.7.linked_current"]["relationship_binding"]["offset"], relation_base + 36)
        with self.assertRaises(ValueError):
            vf.plan(b, {"power.7.linked_current": 46000})
        result = apply(b, vf.plan(b, {"power.7.linked_current": 46000, "power.9.maximum": 46000}))
        self.assertEqual(struct.unpack_from("<I", result, source + 61)[0], 46000)
        self.assertEqual(result[source + 80], 0xA7)
        for offset, replacement in (
            (source + 33, b"\x02\x01"),
            (relation_base + 36, b"\x02"),
            (relation_base + 37, b"\xff"),
            (0x3804, b"\x03"),
        ):
            damaged = bytearray(b)
            damaged[offset : offset + len(replacement)] = replacement
            invalid = {f["key"]: f for f in vf.inspect(damaged)["fields"]}
            self.assertEqual(invalid["power.7.linked_current"]["capability"], "read-only")
            self.assertEqual(invalid["power.2.maximum"]["capability"], "editable")

    def test_incomplete_selected_table_has_no_write_capability(self):
        blob = fixture()[:0x4000]
        report = vf.inspect(blob)
        self.assertFalse(any(f["key"].startswith("power.") for f in report["fields"]))
        with self.assertRaises(ValueError):
            vf.plan(blob, {"power.2.maximum": 999000})

    def test_owner_and_unknown_layout_rejected_independently(self):
        for offset, value in ((0x2200 + 16 + 2 * 60, 0x14), (0x3800, 0x41)):
            blob = bytearray(fixture())
            blob[offset] = value
            report = vf.inspect(blob)
            self.assertTrue(any(d["group"] == "power" for d in report["diagnostics"]))
            with self.assertRaises(ValueError):
                vf.plan(blob, {"power.2.maximum": 999000})

    def test_invalid_bit_checksum_rejected_before_any_fields(self):
        blob = bytearray(fixture())
        blob[0x107] ^= 1
        with self.assertRaises(vf.FormatError):
            vf.inspect(blob)

    def test_wrong_types_nonfinite_fraction_and_order_rejected(self):
        for value in (True, "999000", float("nan"), float("inf"), 999000.1, -1, 2**32):
            with self.subTest(value=value), self.assertRaises(ValueError):
                vf.plan(fixture(), {"power.2.maximum": value})
        with self.assertRaises(ValueError):
            vf.plan(fixture(), {"power.2.maximum": 100001})
        plan = vf.plan(fixture(), {"power.2.minimum": 100000, "power.2.default": 100000, "power.2.maximum": 100001})
        self.assertTrue(plan["preservation_verified"])

    def test_noop_is_no_patch(self):
        p = vf.plan(fixture(), {"power.2.maximum": 450000})
        self.assertEqual(p["patches"], [])
        self.assertEqual(p["expected_sha256"], p["baseline_sha256"])

    def test_perf_relationships_and_policy_dependency(self):
        b = perf_fixture()
        for changes in (
            {"perf_cf.0.target": 0.75},
            {"perf_cf.0.jump": 1.1},
            {"perf_cf.0.hysteresis_up": 0},
            {"perf_cf.0.target": 0.5, "perf_cf_policy.0.enabled": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                vf.plan(b, changes)
        result = apply(b, vf.plan(b, {"perf_cf.0.target": 0.5, "perf_cf.0.jump": 0.875}))
        rows = {f["key"]: f for f in vf.inspect(result)["fields"]}
        self.assertEqual(rows["perf_cf.0.target"]["current"], 0.5)
        self.assertEqual(rows["perf_cf.0.jump"]["current"], 0.875)

    def test_wrong_perf_domain_and_mask_cannot_enable_control(self):
        for offset, value in ((0x4500 + 6 + 19, 6), (0x4600 + 11, 3)):
            b = bytearray(perf_fixture())
            b[offset] = value
            fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
            self.assertEqual(fields["perf_cf.0.target"]["capability"], "read-only")

    def test_voltage_vmin_preserves_negative_chain_as_metadata_not_active_limit(self):
        b = voltage_fixture()
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        self.assertEqual(fields["voltage.0.VMIN"]["rel_lowest_uv"], 800000)
        self.assertEqual(fields["voltage.0.VMIN"]["maximum"], 2147483647)
        self.assertEqual(fields["voltage.0.INFO_VMIN.maximum"]["capability"], "read-only")
        # The runtime contract, not a conservative polynomial bound, checks
        # whether the actual selected adapter permits this effective VMIN.
        raised = apply(b, vf.plan(b, {"voltage.0.VMIN": 850000}))
        self.assertEqual(
            next(f for f in vf.inspect(raised)["fields"] if f["key"] == "voltage.0.VMIN")["capability"], "editable"
        )
        with self.assertRaises(ValueError):
            vf.plan(b, {"voltage.0.VMIN": 0})
        p = vf.plan(b, {"voltage.0.VMIN": 775000})
        result = apply(b, p)
        field = fields["voltage.0.VMIN"]
        for offset in field["offsets"]:
            self.assertEqual(struct.unpack_from("<f", result, offset)[0], 775000)
        self.assertEqual(vf.inspect(result)["selector"], vf.inspect(b)["selector"])

    def test_voltage_cycle_removes_vmin_capability(self):
        b = bytearray(voltage_fixture())
        at = 0x5000 + 14 + 29 * 255 + 25 * 5
        b[at + 2] = 0
        b[at + 24] = 0
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        self.assertEqual(fields["voltage.0.VMIN"]["capability"], "read-only")

    def test_voltage_compound_constants_change_three_floats_only_and_restore(self):
        b = voltage_fixture()
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        for key, value in (
            ("voltage.0.ALT.addend", 15000),
            ("voltage.0.ALT.addend", 0),
            ("voltage.0.OV.constant", 1195000),
        ):
            with self.subTest(key=key, value=value):
                f = fields[key]
                self.assertEqual(f["capability"], "editable")
                result = apply(b, vf.plan(b, {key: value}))
                expected = bytearray(b)
                for offset in f["offsets"]:
                    struct.pack_into("<f", expected, offset, value)
                self.assertEqual(result, expected)
                self.assertEqual(apply(result, vf.plan(result, {key: f["current"]})), b)
                self.assertEqual(f["next_equation"], 0 if key.endswith("addend") else 65535)
                self.assertEqual(f["variable_index"], 0)
                self.assertEqual(f["output_type"], 2)
                self.assertEqual(f["equation_offset"] + 3, f["offsets"][0])
                self.assertTrue(f["sharing_graph_complete"])
        self.assertEqual(fields["voltage.0.ALT.maximum"]["capability"], "read-only")
        self.assertEqual(fields["voltage.0.OV.maximum"]["capability"], "read-only")
        self.assertEqual(fields["voltage.0.REL.maximum"]["capability"], "editable")

    def test_voltage_compound_input_units_and_constancy(self):
        for key in ("voltage.0.ALT.addend", "voltage.0.OV.constant"):
            for value in (True, -1, 1.5, "15000", float("nan"), float("inf"), 2147483648):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    vf.plan(voltage_fixture(), {key: value})
        with self.assertRaises(ValueError):
            vf.plan(voltage_fixture(), {"voltage.0.OV.constant": 0})
        base = 0x5000 + 14 + 29 * 255
        for offset, value in ((3, 19000), (7, 21000), (11, 19000), (15, 1), (19, 1)):
            b = bytearray(voltage_fixture())
            struct.pack_into("<f", b, base + 25 + offset, value)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                vf.plan(b, {"voltage.0.ALT.addend": 15000})

    def test_voltage_constant_head_must_have_expected_next_and_unique_selector(self):
        base = 0x5000 + 14 + 29 * 255
        for role, row, next_index in (("ALT.addend", 1, 65535), ("ALT.addend", 1, 2), ("OV.constant", 2, 0)):
            b = bytearray(voltage_fixture())
            b[base + row * 25 + 2] = next_index & 255
            b[base + row * 25 + 24] = next_index >> 8
            with self.subTest(role=role, next_index=next_index), self.assertRaises(ValueError):
                vf.plan(b, {f"voltage.0.{role}": 15000})
        b = bytearray(voltage_fixture())
        struct.pack_into("<H", b, 0x800 + 5 + 21, 1)
        self.assertEqual(
            next(f for f in vf.inspect(b)["fields"] if f["key"] == "voltage.0.ALT.addend")["capability"], "read-only"
        )

    def test_polynomial_variable_and_coefficient_equation_terms_count_as_sharing(self):
        for coefficient in (False, True):
            for target, key in ((1, "voltage.0.ALT.addend"), (2, "voltage.0.OV.constant"), (4, "voltage.0.VMIN")):
                b = polynomial_reference_fixture(target, coefficient)
                fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
                with self.subTest(coefficient=coefficient, target=target):
                    self.assertTrue(fields[key]["sharing_graph_complete"])
                    self.assertEqual(fields[key]["referenced_by"], [3])
                    self.assertEqual(fields[key]["capability"], "read-only")
                    with self.assertRaises(ValueError):
                        vf.plan(b, {key: 15000})

    def test_polynomial_dependency_on_rel_does_not_alias_independent_alt_head(self):
        fields = {f["key"]: f for f in vf.inspect(polynomial_reference_fixture(0))["fields"]}
        self.assertEqual(fields["voltage.0.REL.maximum"]["referenced_by"], [1, 3])
        self.assertEqual(fields["voltage.0.ALT.addend"]["referenced_by"], [])
        self.assertEqual(fields["voltage.0.ALT.addend"]["capability"], "editable")

    def test_unknown_truncated_or_cyclic_polynomial_graph_keeps_fields_read_only(self):
        base = 0x5000 + 14 + 29 * 255
        term = base + 6 * 25
        mutations = (
            (0x5000 + 11, b"\x06"),
            (0x5000 + 12, b"\x00\x00"),
            (0x5000 + 12, b"\xff\xff"),
            (base + 13, b"\x00"),
            (base + 11, b"\x0f\x00"),
            (base + 3 * 25, b"\x08"),
            (term, b"\x04"),
            (term, b"\x02\xff"),
            (term + 10, b"\x01\x00\x00\x80\x7f"),
            (term, b"\x03\xff\xff"),
            (term, b"\x03\x06\x00"),
            (term, b"\x03\x00\x00"),
        )
        for offset, replacement in mutations:
            b = bytearray(voltage_fixture())
            b[offset : offset + len(replacement)] = replacement
            fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
            with self.subTest(offset=offset, replacement=replacement):
                for key in ("voltage.0.ALT.addend", "voltage.0.OV.constant", "voltage.0.VMIN", "voltage.0.REL.maximum"):
                    self.assertFalse(fields[key]["sharing_graph_complete"])
                    self.assertEqual(fields[key]["capability"], "read-only")
                self.assertEqual(fields["power.2.maximum"]["capability"], "editable")

    def test_polynomial_high_index_reference_and_opaque_term_bytes(self):
        # Bytes2..4 of a type2 variable descriptor are not its index. Keep
        # them opaque; they may contain unrelated residue in real images.
        b = bytearray(voltage_fixture())
        term = 0x5000 + 14 + 29 * 255 + 6 * 25
        b[term + 2 : term + 5] = b"\xff\x80\x04"
        result = apply(b, vf.plan(b, {"voltage.0.ALT.addend": 15000}))
        self.assertEqual(result[term : term + 5], b[term : term + 5])
        b[term] = 3
        struct.pack_into("<H", b, term + 1, 0x0101)
        field = next(f for f in vf.inspect(b)["fields"] if f["key"] == "voltage.0.ALT.addend")
        self.assertFalse(field["sharing_graph_complete"])

    def test_clock_direct_roots_make_constants_shared(self):
        for version in (0x40, 0x50):
            for kind in (1, 3):
                for target, key in ((1, "voltage.0.ALT.addend"), (2, "voltage.0.OV.constant"), (4, "voltage.0.VMIN")):
                    b = voltage_clock_fixture(target, kind, version)
                    field = next(f for f in vf.inspect(b)["fields"] if f["key"] == key)
                    with self.subTest(version=version, kind=kind, target=target):
                        self.assertTrue(field["sharing_graph_complete"])
                        self.assertTrue(field["shared"])
                        self.assertEqual(field["clock_referenced_by"], [{"entry": 0, "kind": kind, "offset": 0x740B}])
                        self.assertEqual(field["capability"], "read-only")
                        with self.assertRaises(ValueError):
                            vf.plan(b, {key: 15000})

    def test_clock_rel_root_and_optional_absence_preserve_independent_constants(self):
        empty = bytearray(voltage_clock_fixture())
        struct.pack_into("<I", empty, 0x38C, 0)
        for b in (voltage_fixture(), voltage_clock_fixture(), bytes(empty)):
            fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
            for key in ("voltage.0.ALT.addend", "voltage.0.OV.constant"):
                self.assertTrue(fields[key]["external_sharing_complete"])
                self.assertEqual(fields[key]["clock_referenced_by"], [])
                self.assertEqual(fields[key]["capability"], "editable")
            result = apply(b, vf.plan(b, {"voltage.0.ALT.addend": 15000}))
            self.assertEqual(result[0x7400:], b[0x7400:])

    def test_unknown_present_clock_geometry_blocks_constants_only(self):
        mutations = (
            (0x7400, b"\x60"),
            (0x7401, b"\x09"),
            (0x7402, b"\x16"),
            (0x7403, b"\xff"),
            (0x7405, b"\x00"),
            (0x7408, b"\x02"),
            (0x740B, b"\x06\x00"),
            (0x740B, b"\x01\x01"),
            (0x119, b"\x03"),
            (0x11A, b"\x0c\x00"),
        )
        for offset, replacement in mutations:
            b = bytearray(voltage_clock_fixture())
            b[offset : offset + len(replacement)] = replacement
            fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
            with self.subTest(offset=offset, replacement=replacement):
                for key in ("voltage.0.ALT.addend", "voltage.0.OV.constant"):
                    self.assertFalse(fields[key]["external_sharing_complete"])
                    self.assertFalse(fields[key]["sharing_graph_complete"])
                    self.assertEqual(fields[key]["capability"], "read-only")
                    self.assertIsNotNone(fields[key]["external_sharing_error"])
                self.assertEqual(fields["power.2.maximum"]["capability"], "editable")
                self.assertEqual(fields["voltage.0.REL.maximum"]["capability"], "read-only")

    def test_rel_clamp_binding_and_complete_affected_roots(self):
        b = voltage_fixture()
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        field = fields["voltage.0.REL.maximum"]
        self.assertEqual(field["capability"], "editable")
        binding = field["upper_clamp_binding"]
        self.assertEqual(
            {k: binding[k] for k in ("index", "lower_uv", "before_uv", "next_index", "variable", "output_type")},
            {"index": 0, "lower_uv": 900000, "before_uv": 1120000, "next_index": 5, "variable": 0, "output_type": 2},
        )
        self.assertEqual(binding["variables"], [{"type": 2, "value": 0}, {"type": 2, "value": 1}])
        self.assertEqual(
            binding["coefficients"],
            [
                {"type": 1, "value": struct.unpack("<I", struct.pack("<f", 1000000))[0]},
                *[{"type": 1, "value": 0} for _ in range(5)],
            ],
        )
        self.assertEqual(field["dependency_indices"], [0, 5])
        self.assertEqual(field["affected_equation_indices"], [0, 1])
        self.assertEqual(
            field["affected_rail_roots"],
            [
                {
                    "rail_index": 0,
                    "rail_identity": 1,
                    "role": "REL",
                    "selector_offset": 0x812,
                    "equation": 0,
                    "dependency_indices": [0, 5],
                },
                {
                    "rail_index": 0,
                    "rail_identity": 1,
                    "role": "ALT",
                    "selector_offset": 0x814,
                    "equation": 1,
                    "dependency_indices": [0, 1, 5],
                },
            ],
        )
        self.assertEqual(field["affected_clock_roots"], [])
        self.assertEqual(field["offsets"], [field["equation_offset"] + 7])
        changed = apply(b, vf.plan(b, {field["key"]: 1100000}))
        expected = bytearray(b)
        struct.pack_into("<f", expected, field["offsets"][0], 1100000)
        self.assertEqual(changed, expected)
        self.assertEqual(apply(changed, vf.plan(changed, {field["key"]: field["current"]})), b)

    def test_rel_shared_roots_and_unsupported_peers_are_reported_independently(self):
        b = bytearray(polynomial_reference_fixture(0))
        # An extra selector consumes REL directly. Its semantics remain unnamed.
        struct.pack_into("<H", b, 0x800 + 5 + 21, 0)
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        field = fields["voltage.0.REL.maximum"]
        self.assertEqual(field["capability"], "editable")
        self.assertTrue(field["shared"])
        self.assertEqual(field["affected_equation_indices"], [0, 1, 3])
        self.assertEqual([r["role"] for r in field["affected_rail_roots"]], ["REL", "ALT", "unnamed", "INFO_VMIN"])
        self.assertEqual(fields["voltage.0.INFO_VMIN.maximum"]["capability"], "read-only")
        clock = {f["key"]: f for f in vf.inspect(voltage_clock_fixture(0))["fields"]}[field["key"]]
        self.assertEqual(clock["capability"], "editable")
        self.assertEqual(
            clock["affected_clock_roots"],
            [{"equation": 0, "entry": 0, "kind": 1, "offset": 0x740B, "dependency_indices": [0, 5]}],
        )

    def test_rel_upper_binding_requires_type7_complete_graph_and_whole_units(self):
        base = 0x5000 + 14 + 29 * 255
        for offset, replacement in (
            (base, b"\x01"),
            (base + 23, b"\x01"),
            (base + 7, struct.pack("<f", 1120000.5)),
            (base + 3, struct.pack("<f", 900000.5)),
            (0x5000 + 11, b"\x06"),
        ):
            b = bytearray(voltage_fixture())
            b[offset : offset + len(replacement)] = replacement
            fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
            with self.subTest(offset=offset, replacement=replacement):
                self.assertIsNone(fields["voltage.0.REL.maximum"]["upper_clamp_binding"])
                self.assertEqual(fields["voltage.0.REL.maximum"]["capability"], "read-only")
                self.assertEqual(fields["power.2.maximum"]["capability"], "editable")
        with self.assertRaises(ValueError):
            vf.plan(voltage_fixture(), {"voltage.0.REL.maximum": 1000000.5})

    def test_rel_native_term_metadata_preserves_opaque_bits_and_equation_references(self):
        b = bytearray(polynomial_reference_fixture(0, coefficient=True))
        base = 0x5000 + 14 + 29 * 255
        # The selected REL's first variable now references a distinct active
        # equation with no return path; its upper bits are opaque ROM residue.
        at = base + 6 * 25
        b[at : at + 5] = b"\x03\x04\x00\xa5\xbb"
        b[at + 5 : at + 10] = b"\x02\x01\xdd\xee\xff"
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        field = fields["voltage.0.REL.maximum"]
        self.assertEqual(field["upper_clamp_binding"]["variables"], [{"type": 3, "value": 4}, {"type": 2, "value": 1}])
        self.assertEqual(field["dependency_indices"], [0, 4, 5])
        result = apply(b, vf.plan(b, {field["key"]: 1100000}))
        self.assertEqual(result[at : at + 10], b[at : at + 10])

    def test_rel_value_initial_upper_seed_and_explicit_selection_metadata(self):
        fields = {f["key"]: f for f in vf.inspect(voltage_fixture())["fields"]}
        field = fields["voltage.0.REL.value"]
        upper = fields["voltage.0.REL.maximum"]
        self.assertEqual(field["label"], "NVVDD fixed REL equation bounds")
        self.assertEqual(field["current"], 1120000)
        self.assertEqual(field["raw"], 1120000)
        self.assertEqual(field["raw_values"], [900000, 1120000])
        self.assertEqual(field["capability"], "editable")
        self.assertTrue(field["explicit_selection"])
        self.assertEqual((field["minimum"], field["maximum"]), (1, 2147483647))
        self.assertEqual(field["offsets"], [field["equation_offset"] + 3, field["equation_offset"] + 7])
        for key in (
            "upper_clamp_binding",
            "sharing_graph_complete",
            "affected_rail_roots",
            "affected_clock_roots",
            "dependency_indices",
            "equation",
            "next_equation",
            "variable_index",
            "output_type",
            "whole_uv",
        ):
            self.assertEqual(field[key], upper[key])
        self.assertNotIn("explicit_selection", upper)
        self.assertIn("stored upper bound", field["detail"])
        self.assertIn("Linked tails", field["detail"])
        self.assertIn("rail offsets and device rounding", field["detail"])

    def test_rel_value_changes_exactly_both_bound_words_and_preserves_chain_and_terms(self):
        b = voltage_fixture()
        key = "voltage.0.REL.value"
        field = next(f for f in vf.inspect(b)["fields"] if f["key"] == key)
        p = vf.plan(b, {key: 1035000})
        result = apply(b, p)
        expected = bytearray(b)
        for offset in field["offsets"]:
            struct.pack_into("<f", expected, offset, 1035000)
        self.assertEqual(result, expected)
        self.assertTrue(p["preservation_verified"])
        self.assertEqual(p["expected_sha256"], hashlib.sha256(expected).hexdigest())
        after = next(f for f in vf.inspect(result)["fields"] if f["key"] == key)
        self.assertEqual(after["raw_values"], [1035000, 1035000])
        self.assertEqual(after["upper_clamp_binding"]["variables"], field["upper_clamp_binding"]["variables"])
        self.assertEqual(after["upper_clamp_binding"]["coefficients"], field["upper_clamp_binding"]["coefficients"])
        self.assertEqual(after["next_equation"], 5)
        self.assertEqual(after["dependency_indices"], field["dependency_indices"])
        self.assertEqual(after["capability"], "editable")

    def test_rel_value_equal_to_old_upper_still_changes_lower_only(self):
        b = voltage_fixture()
        key = "voltage.0.REL.value"
        field = next(f for f in vf.inspect(b)["fields"] if f["key"] == key)
        p = vf.plan(b, {key: field["current"]})
        result = apply(b, p)
        expected = bytearray(b)
        struct.pack_into("<f", expected, field["offsets"][0], field["current"])
        self.assertGreater(p["changed_byte_count"], 0)
        self.assertEqual(result, expected)
        again = vf.plan(result, {key: field["current"]})
        self.assertEqual(again["patches"], [])
        self.assertEqual(again["changed_byte_count"], 0)

    def test_rel_value_upper_field_remains_independent_and_overlap_is_checked(self):
        b = voltage_fixture()
        key = "voltage.0.REL.value"
        upper = "voltage.0.REL.maximum"
        field = next(f for f in vf.inspect(b)["fields"] if f["key"] == key)
        changed = apply(b, vf.plan(b, {upper: 1100000}))
        self.assertEqual(struct.unpack_from("<f", changed, field["offsets"][0])[0], 900000)
        self.assertEqual(
            apply(b, vf.plan(b, {key: 1035000, upper: 1035000})),
            apply(b, vf.plan(b, {key: 1035000})),
        )
        for changes in ({key: 1035000, upper: 1040000}, {upper: 1040000, key: 1035000}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "conflicting overlapping"):
                vf.plan(b, changes)

    def test_rel_value_rejects_nonwhole_or_inexact_float32_input(self):
        for value in (True, "1035000", 0, -1, 1000000.5, 16777217, 2147483647, 2147483648, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                vf.plan(voltage_fixture(), {"voltage.0.REL.value": value})

    def test_rel_value_alternate_adapter_and_selector_identity(self):
        b = bytearray(voltage_fixture())
        for header in (0x80, 0x1080, 0x2700, 0x3080):
            struct.pack_into("<H", b, header + 6, 0xBEEF)
        struct.pack_into("<I", b, 0x220C, 0xBEEF)
        struct.pack_into("<IB", b, 0x360, 0x87654321, 0x73)
        plan = vf.plan(b, {"voltage.0.REL.value": 1045000})
        result = apply(b, plan)
        self.assertEqual(vf.inspect(result)["identity"], vf.inspect(b)["identity"])
        self.assertEqual(vf.inspect(result)["identity"]["pci_device"], 0xBEEF)
        self.assertEqual(plan["fields"][0]["current"], 1045000)

    def test_rel_value_two_rails_and_unsupported_peer_preserve_partial_capability(self):
        b = bytearray(voltage_fixture())
        b[0x803] = 2
        b[0x846:0x887] = b[0x805:0x846]
        b[0x847] = 2
        # Second rail initially shares the recognized root, then references a
        # different active type1 root. Only its own fixed-value field is lost.
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        self.assertEqual(fields["voltage.1.REL.value"]["label"], "MSVDD fixed REL equation bounds")
        self.assertEqual(fields["voltage.1.REL.value"]["capability"], "editable")
        result = apply(b, vf.plan(b, {"voltage.1.REL.value": 1045000}))
        self.assertEqual(
            next(f for f in vf.inspect(result)["fields"] if f["key"] == "voltage.1.REL.value")["current"], 1045000
        )
        struct.pack_into("<H", b, 0x846 + 13, 3)
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        self.assertEqual(fields["voltage.0.REL.value"]["capability"], "editable")
        self.assertEqual(fields["voltage.1.REL.value"]["capability"], "read-only")
        self.assertIsNone(fields["voltage.1.REL.value"]["upper_clamp_binding"])
        vf.plan(b, {"voltage.0.REL.value": 1045000})

    def test_rel_value_requires_supported_type_and_complete_graph(self):
        base = 0x5000 + 14 + 29 * 255
        for offset, replacement in (
            (base, b"\x01"),
            (base + 23, b"\x01"),
            (base + 7, struct.pack("<f", 1120000.5)),
            (base + 3, struct.pack("<f", 900000.5)),
            (base + 3, struct.pack("<f", float("nan"))),
            (base + 7, struct.pack("<f", float("inf"))),
            (0x5000 + 11, b"\x06"),
            (base + 6 * 25, b"\x03\x00\x00"),
        ):
            b = bytearray(voltage_fixture())
            b[offset : offset + len(replacement)] = replacement
            fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
            with self.subTest(offset=offset, replacement=replacement):
                self.assertEqual(fields["voltage.0.REL.value"]["capability"], "read-only")
                self.assertIsNone(fields["voltage.0.REL.value"]["upper_clamp_binding"])
                self.assertEqual(fields["power.2.maximum"]["capability"], "editable")
                with self.assertRaises(ValueError):
                    vf.plan(b, {"voltage.0.REL.value": 1045000})

    def test_rel_value_affected_roots_include_read_only_consumers_without_guessing_policy(self):
        b = polynomial_reference_fixture(0)
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        field = fields["voltage.0.REL.value"]
        self.assertEqual(field["capability"], "editable")
        self.assertEqual([r["role"] for r in field["affected_rail_roots"]], ["REL", "ALT", "INFO_VMIN"])
        self.assertEqual(fields["voltage.0.INFO_VMIN.maximum"]["capability"], "read-only")
        clock = next(f for f in vf.inspect(voltage_clock_fixture(0))["fields"] if f["key"] == field["key"])
        self.assertEqual(clock["capability"], "editable")
        self.assertEqual(
            clock["affected_clock_roots"],
            [{"equation": 0, "entry": 0, "kind": 1, "offset": 0x740B, "dependency_indices": [0, 5]}],
        )
        unknown = bytearray(voltage_clock_fixture(0))
        unknown[0x7400] = 0x60
        fields = {f["key"]: f for f in vf.inspect(unknown)["fields"]}
        self.assertEqual(fields[field["key"]]["capability"], "read-only")
        self.assertEqual(fields["power.2.maximum"]["capability"], "editable")

    def test_thermal_compound_field_preserves_other_policies(self):
        table = bytearray(12 + 48 * 2)
        table[:4] = bytes((0x20, 12, 48, 2))
        table[12:14] = bytes((1, 1))
        struct.pack_into("<3h", table, 14, 80 * 32, 80 * 32, 80 * 32)
        table[60] = 2
        struct.pack_into("<3h", table, 62, 90 * 32, 91 * 32, 92 * 32)
        b = extend(fixture(), [(0x50, 0x1B, 0x4500, table)])
        with self.assertRaises(ValueError):
            vf.plan(b, {"thermal.1.temperature0": 80})
        result = apply(b, vf.plan(b, {"thermal.operating.maximum": 75}))
        self.assertEqual(struct.unpack_from("<3h", result, 0x4500 + 14), (75 * 32,) * 3)
        self.assertEqual(result[0x4500 + 60 : 0x4500 + 108], b[0x4500 + 60 : 0x4500 + 108])

    def test_lpwr_does_not_invent_support_for_disabled_entries(self):
        table = bytearray(10 + 5 * 2)
        table[:4] = bytes((0x10, 10, 5, 2))
        struct.pack_into("<I", table, 10, 0xF1)
        struct.pack_into("<I", table, 15, 0xF0)
        b = extend(fixture(), [(0x80, 0x39, 0x4500, table)])
        with self.assertRaises(ValueError):
            vf.plan(b, {"lpwr.gr_pg.1.enabled": True})
        result = apply(b, vf.plan(b, {"lpwr.gr_pg.0.enabled": False}))
        self.assertEqual(struct.unpack_from("<I", result, 0x4500 + 10)[0], 0xF0)
        self.assertEqual(result[0x4500 + 15 : 0x4500 + 20], b[0x4500 + 15 : 0x4500 + 20])

    def test_fan_curve_must_fit_own_and_shared_limits(self):
        b = fan_fixture()
        for changes in (
            {"fan.0.rpm_maximum": 2500},
            {"fan_curve.4.2.rpm": 3100},
            {"fan_curve.0.1.temperature": 40},
            {"fan.0.rpm_minimum": 1600},
            {"fan_stop.0.low": 35},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                vf.plan(b, changes)
        changes = {"fan.0.rpm_maximum": 2500, **{f"fan_curve.{i}.2.rpm": 2500 for i in (0, 4, 5)}}
        result = apply(b, vf.plan(b, changes))
        self.assertEqual(struct.unpack_from("<H", result, 0x4400 + 9 + 16)[0], 2500)

    def test_fan_role_map_comes_from_cooler_header(self):
        b = bytearray(fan_fixture())
        b[0x4404:0x4408] = bytes((1, 0, 255, 255))
        fields = {field["key"]: field for field in vf.inspect(bytes(b))["fields"]}
        expected = {"1": 1, "2": 0, "3": None, "4": None}
        for index in (0, 1):
            for role in ("level_minimum", "level_maximum", "rpm_minimum", "rpm_maximum", "pwm_hz"):
                self.assertEqual(fields[f"fan.{index}.{role}"]["client_id_to_index"], expected)

    def test_clock_binding_retains_actual_geometry_ordinal_and_hal_selector(self):
        b = bytearray(factory_fixture())
        b[0x4E00 + 6] = 11
        fields = {field["key"]: field for field in vf.inspect(bytes(b))["fields"]}
        for index in range(9):
            expected = {
                "schema": 1,
                "table_version": 0x50,
                "bios_row_type": 2,
                "bios_index": index,
                "bios_hal_selector": 11,
                "table_offset": 0x4E00,
                "header_size": 13,
                "entry_size": 15,
                "entry_count": 9,
                "subentry_count": 3,
                "subentry_size": 2,
            }
            for role, delta in (("minimum", 3), ("maximum", 5)):
                field = fields[f"clock.{index}.{role}"]
                self.assertEqual(field["clock_binding"], expected)
                self.assertEqual(field["clock_table_version"], 0x50)
                self.assertEqual(field["offsets"], [0x4E00 + 13 + index * 21 + delta])

    def test_global_zero_rpm_exposes_every_edited_policy(self):
        fields = {field["key"]: field for field in vf.inspect(fan_fixture())["fields"]}
        field = fields["fan.zero_rpm"]
        self.assertEqual(field["policy_indices"], [0, 1, 4, 5])
        self.assertEqual(field["offsets"], [0x4500 + 4 + i * 51 + 50 for i in (0, 1, 4, 5)])

    def test_fan_policy_binding_preserves_raw_metadata_and_explicit_routes(self):
        b = bytearray(fan_fixture())
        policy = 0x4500 + 4
        struct.pack_into("<H", b, policy + 44, 4321)
        b[policy + 46] = 7
        struct.pack_into("<h", b, policy + 47, -33)
        b[policy + 50] = 0x9F
        route = 0x4900 + 4
        struct.pack_into("<H", b, route + 3, 217)
        struct.pack_into("<I", b, route + 9, 2)
        binding = vf.inspect(bytes(b))["fan_policy_binding"]
        self.assertEqual(binding["version"], 32)
        self.assertEqual([p["index"] for p in binding["policies"]], [0, 1, 4, 5])
        self.assertEqual(
            binding["policies"][0],
            {
                "index": 0,
                "selector_raw": 1,
                "flags": 0x9F,
                "thermal_offset_q8": -264,
                "stop_word_44": 4321,
                "stop_selector_46": 7,
            },
        )
        self.assertEqual(
            binding["routes"][0],
            {
                "index": 0,
                "kind": 1,
                "cooler_index": 0,
                "word": 217,
                "policy_mask_a": 49,
                "policy_mask_b": 2,
            },
        )
        self.assertEqual(binding["client_id_to_index"], {"1": 0, "2": 1, "3": None, "4": None})

    def test_fan_policy_binding_absent_when_related_tables_are_inconsistent(self):
        b = bytearray(fan_fixture())
        b[0x4900 + 4 + 2] = 1
        report = vf.inspect(bytes(b))
        self.assertIsNone(report["fan_policy_binding"])
        self.assertTrue(any(d["group"] == "fans" for d in report["diagnostics"]))
        self.assertIsNone(vf.inspect(fixture())["fan_policy_binding"])

    def test_zero_rpm_bitmask_preserves_each_distinct_flag(self):
        b = fan_fixture()
        result = apply(b, vf.plan(b, {"fan.zero_rpm": False}))
        for i in (0, 1, 4, 5):
            at = 0x4500 + 4 + 51 * i + 50
            self.assertEqual(result[at], b[at] & ~6)
        self.assertEqual(apply(result, vf.plan(result, {"fan.zero_rpm": True})), b)

    def test_factory_offset_controls_flag_and_preserves_rated_boost(self):
        b = factory_fixture()
        for value in (-1, 0, 1):
            p = vf.plan(b, {"factory.0.offset": value})
            result = apply(b, p)
            self.assertEqual(struct.unpack_from("<Bi", result, 0x4D00 + 7), (int(value != 0), value * 1000))
            self.assertEqual(result[0x4D00 + 17 : 0x4D00 + 27], b[0x4D00 + 17 : 0x4D00 + 27])
            self.assertEqual(apply(result, vf.plan(result, {"factory.0.offset": 75})), b)
        with self.assertRaises(ValueError):
            vf.plan(b, {"factory.0.offset": 0.5})
        with self.assertRaises(ValueError):
            vf.plan(b, {"factory.3.value": 3000000})

    def test_rated_clock_enables_only_selected_unsigned_record(self):
        b = factory_fixture()
        fields = {f["key"]: f for f in vf.inspect(b)["fields"]}
        self.assertEqual(fields["factory.2.rated"]["current"], 0)
        self.assertFalse(fields["factory.2.rated"]["factory_enabled"])
        for record in (2, 3):
            for value in (1, 2501, 65535):
                key = f"factory.{record}.rated"
                result = apply(b, vf.plan(b, {key: value}))
                at = 0x4D00 + 7 + record * 5
                expected = bytearray(b)
                struct.pack_into("<BI", expected, at, 1, value * 1000)
                self.assertEqual(result, expected)
                field = next(f for f in vf.inspect(result)["fields"] if f["key"] == key)
                self.assertEqual(field["current"], value)
                self.assertTrue(field["factory_enabled"])
                self.assertEqual(field["factory_binding"]["clock_binding"], fields["clock.0.minimum"]["clock_binding"])
            for value in (0, -1, 65536, 1.5, True, "2500", float("nan"), float("inf")):
                with self.subTest(record=record, value=value), self.assertRaises(ValueError):
                    vf.plan(b, {f"factory.{record}.rated": value})

    def test_rated_clock_unknown_flag_fractional_or_truncated_layout_is_read_only(self):
        for flag, khz in ((2, 2500000), (1, 2500500), (1, 65536000), (1, 0)):
            b = bytearray(factory_fixture())
            struct.pack_into("<BI", b, 0x4D00 + 17, flag, khz)
            field = next(f for f in vf.inspect(b)["fields"] if f["key"] == "factory.2.rated")
            self.assertEqual(field["capability"], "read-only")
            with self.assertRaises(ValueError):
                vf.plan(b, {"factory.2.rated": 2500})


if __name__ == "__main__":
    unittest.main()


def test_v30_class_mapping_is_version_scoped_and_uses_full_encoded_class():
    for header, stride in ((0x2C, 0x4B), (0x32, 0x53), (0x34, 0x5B)):
        parsed = vf.inspect(v30_power_fixture(header, stride))
        field = next(f for f in parsed["fields"] if f["key"] == "power.0.maximum")
        assert field["policy_type"] == 1 and field["runtime_binding"]["runtime_type"] == 0
        assert field["runtime_binding"]["table_version"] == 0x30
        unknown = vf.inspect(v30_power_fixture(header, stride, extension=1))
        field = next(f for f in unknown["fields"] if f["key"] == "power.0.maximum")
        assert field["policy_type"] == 0x11 and field["runtime_binding"]["runtime_type"] is None
        assert field["capability"] == "read-only"
        assert vf.power_runtime_type(0x40, 1) == 0
        assert vf.power_runtime_type(0x30, 2) is None
