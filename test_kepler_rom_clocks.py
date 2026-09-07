import struct
import unittest

from tools.decode_kepler_clocks import DecodeError, decode


def rom_fixture(device=0x1188, states=None):
    b = bytearray(1024)
    b[:3] = b"\x55\xaa\x02"
    struct.pack_into("<H", b, 0x18, 0x30)
    b[0x30:0x34] = b"PCIR"
    struct.pack_into("<HH", b, 0x34, 0x10DE, device)
    b[0x60:0x6c] = b"\xff\xb8BIT\x00\x00\x01\x0c\x06\x01\x00"
    b[0x6b] = -sum(b[0x60:0x6c]) & 255
    b[0x6c:0x72] = b"P\x02\x04\x00\x80\x00"
    struct.pack_into("<I", b, 0x80, 0xA0)
    if states is None:
        states = [(15, (1411, 1481, 1411, 3004, 1481, 1080, 540, 324, 540), 0x8000)]
    b[0xA0:0xA6] = bytes((0x40, 6, 1, 4, 9, len(states)))
    for state_index, (raw_state, clocks, flags) in enumerate(states):
        at = 0xA6 + state_index * 37
        b[at] = raw_state
        for i, mhz in enumerate(clocks):
            struct.pack_into("<H", b, at + 1 + i * 4, mhz | flags)
    return checksum(b)


def checksum(b):
    b[-1] = 0
    b[-1] = -sum(b) & 255
    return b


def nvgi(rom, base):
    header = bytearray(base)
    header[:4] = b"NVGI"
    struct.pack_into("<I", header, 0x14, base)
    return header + rom


class KeplerRomClocksTest(unittest.TestCase):
    def test_plain_rom_has_separate_bios_indices_and_masked_frequencies(self):
        result = decode(rom_fixture())
        self.assertEqual(result["states"][0]["pstate"], 0)
        clocks = result["states"][0]["clocks"]
        self.assertEqual([c["label"] for c in clocks], ["GPC", "XBAR", "L2C", "DDR", "SYS", "HUB", "MSD", "PWR", "DISP"])
        self.assertEqual(clocks[3]["stored_mhz"], 3004)
        self.assertEqual(clocks[3]["flags"], 0x8000)
        self.assertNotIn("nvapi_domain", clocks[3])

    def test_nvgi_offsets_are_relative_to_embedded_rom(self):
        wrapped = nvgi(rom_fixture(), 0x400)
        result = decode(wrapped)
        self.assertEqual(result["performance_table_offset"], 0x4A0)
        self.assertEqual(result["states"][0]["clocks"][0]["file_offset"], 0x4A7)

    def test_gm107_nvgi_uses_declared_image_and_preserves_disabled_records(self):
        p8 = (810, 810, 810, 405, 810, 648, 405, 324, 405)
        p0 = (1080, 1165, 1130, 900, 1120, 1080, 540, 324, 648)
        rom = rom_fixture(0x1382, [(7, p8, 0), (255, (0,) * 9, 0),
                                 (255, (0,) * 9, 0), (15, p0, 0x4000)])
        wrapped = nvgi(rom, 0x600)
        wrapped[0x400:0x402] = b"\x55\xaa"  # Not the declared PCI image.
        result = decode(wrapped)
        self.assertEqual(result["rom_base"], 0x600)
        self.assertEqual(result["performance_table_offset"], 0x6A0)
        self.assertEqual(result["pci_device"], 0x1382)
        self.assertEqual([s["pstate"] for s in result["states"]], [8, None, None, 0])
        self.assertEqual([s["disabled"] for s in result["states"]], [False, True, True, False])
        self.assertEqual([c["stored_mhz"] for c in result["states"][3]["clocks"]], list(p0))
        self.assertEqual(result["states"][3]["clocks"][1]["flags"], 0x4000)

    def test_nvgi_invalid_image_offsets_are_rejected(self):
        for offset in (0, 0x401, 0x800, 0xFFFFFF00):
            with self.subTest(offset=offset):
                wrapped = nvgi(rom_fixture(), 0x400)
                struct.pack_into("<I", wrapped, 0x14, offset)
                with self.assertRaises(DecodeError):
                    decode(wrapped)

    def test_nvgi_truncated_header_is_rejected(self):
        with self.assertRaises(DecodeError):
            decode(b"NVGI")

    def test_truncated_rom_is_rejected(self):
        with self.assertRaises(DecodeError):
            decode(rom_fixture()[:-1])

    def test_bad_checksum_is_rejected(self):
        b = rom_fixture()
        b[-1] ^= 1
        with self.assertRaises(DecodeError):
            decode(b)

    def test_out_of_image_pointer_is_rejected(self):
        b = rom_fixture()
        struct.pack_into("<I", b, 0x80, 0xFFFFFFF0)
        with self.assertRaises(DecodeError):
            decode(checksum(b))

    def test_unknown_layout_is_rejected(self):
        b = rom_fixture()
        b[0xA4] = 8
        with self.assertRaises(DecodeError):
            decode(checksum(b))

    def test_incomplete_clock_records_are_rejected(self):
        b = rom_fixture()
        b[0xA5] = 255
        with self.assertRaises(DecodeError):
            decode(checksum(b))

    def test_duplicate_bit_directory_is_rejected(self):
        b = rom_fixture()
        b[0x180:0x186] = b"\xff\xb8BIT\x00"
        with self.assertRaises(DecodeError):
            decode(checksum(b))


if __name__ == "__main__":
    unittest.main()
