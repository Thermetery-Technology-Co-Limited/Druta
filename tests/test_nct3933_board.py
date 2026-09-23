# SPDX-License-Identifier: GPL-3.0-or-later
"""Board-local NCT3933U display calibration and persistence."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from druta import nct3933_board as board
from druta import profiles


def target(uuid="GPU-1", *, port=1, addr7=0x15):
    gpu = SimpleNamespace(static={"uuid": uuid})
    source = {"controller": "NCT3933U", "contract": 1,
              "units": "nominal_microamps", "port": port, "addr7": addr7}
    profile = SimpleNamespace(name="NCT3933U - three current offsets",
                              rail="OUT1/OUT2/OUT3", port=port, addr7=addr7,
                              src=source)
    rail = SimpleNamespace(p=profile, addr7=addr7)
    return gpu, rail


class BoardDisplayTests(unittest.TestCase):
    def test_order_labels_and_signed_meter_mapping(self):
        self.assertEqual(board.MODES, (board.RAW_MODE, board.VOLTAGE_MODE))
        self.assertEqual(board.OUTPUT_ORDER, (3, 1, 2))
        for channel, name, gain in ((3, "GPU", 10),
                                    (1, "Memory", 10),
                                    (2, "PEX-PLL", 66)):
            with self.subTest(channel=channel):
                self.assertEqual(board.output_label(channel, board.RAW_MODE), f"OUT{channel}")
                self.assertEqual(board.output_label(channel, board.VOLTAGE_MODE),
                                 f"{name} offset (OUT{channel})")
                self.assertEqual(board.display_value(-10, channel, board.VOLTAGE_MODE), gain)
                self.assertEqual(board.display_value(10, channel, board.VOLTAGE_MODE), -gain)
                self.assertEqual(board.native_current(gain, channel, board.VOLTAGE_MODE, 0), -10)
                self.assertEqual(board.native_current(-gain, channel, board.VOLTAGE_MODE, 0), 10)
                self.assertEqual(board.display_step(0, channel, board.VOLTAGE_MODE), gain)
                self.assertEqual(board.display_step(1 << (2 * (channel - 1)), channel,
                                                    board.VOLTAGE_MODE), 2 * gain)
        self.assertEqual(board.display_value(-30, 3, board.RAW_MODE), -30)
        self.assertEqual(board.native_current(-30, 3, board.RAW_MODE, 0), -30)

    def test_live_configuration_grid_and_range_checked_before_native_request(self):
        doubled_pll = 1 << 2
        self.assertEqual(board.native_current(132, 2, board.VOLTAGE_MODE, doubled_pll), -20)
        self.assertEqual(board.native_current(-132, 2, board.VOLTAGE_MODE, doubled_pll), 20)
        for request, channel, mode, config in (
                (66, 2, board.VOLTAGE_MODE, doubled_pll),
                (65, 2, board.VOLTAGE_MODE, 0),
                (11, 3, board.VOLTAGE_MODE, 0),
                (1280, 3, board.VOLTAGE_MODE, 0),
                (128, 2, board.VOLTAGE_MODE, 0),
                (10, 1, board.RAW_MODE, 1),
                (True, 1, board.RAW_MODE, 0),
                (10.0, 1, board.VOLTAGE_MODE, 0),
                (-66, 0, board.VOLTAGE_MODE, 0),
                (0, 1, "unknown", 0)):
            with self.subTest(request=request, channel=channel, mode=mode, config=config):
                with self.assertRaises(ValueError):
                    board.native_current(request, channel, mode, config)

    def test_display_is_exact_for_native_nominal_steps(self):
        self.assertEqual(board.display_value(-20, 2, board.VOLTAGE_MODE), 132)
        self.assertEqual(board.display_value(20, 2, board.VOLTAGE_MODE), -132)
        with self.assertRaises(ValueError):
            board.display_value(1, 2, board.VOLTAGE_MODE)


class BoardPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nct-board.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_raw_and_mode_is_exact_adapter_and_route_local(self):
        gpu, rail = target()
        initial_identity = profiles.rail_identity(rail)
        initial_source = dict(rail.p.src)
        self.assertEqual(board.load_mode(gpu, rail, path=self.path), board.RAW_MODE)
        self.assertFalse(self.path.exists())
        self.assertTrue(board.save_mode(gpu, rail, board.VOLTAGE_MODE, path=self.path))
        self.assertEqual(board.load_mode(gpu, rail, path=self.path), board.VOLTAGE_MODE)
        self.assertEqual(board.load_mode(*target("GPU-2"), path=self.path), board.RAW_MODE)
        self.assertEqual(board.load_mode(*target(port=2), path=self.path), board.RAW_MODE)
        self.assertEqual(board.load_mode(*target(addr7=0x14), path=self.path), board.RAW_MODE)
        self.assertEqual(rail.p.src, initial_source)
        self.assertEqual(profiles.rail_identity(rail), initial_identity)
        self.assertTrue(board.save_mode(*target("GPU-2"), board.VOLTAGE_MODE,
                                        path=self.path))
        self.assertEqual(board.load_mode(gpu, rail, path=self.path), board.VOLTAGE_MODE)
        self.assertTrue(board.save_mode(gpu, rail, board.RAW_MODE, path=self.path))
        self.assertEqual(board.load_mode(gpu, rail, path=self.path), board.RAW_MODE)
        self.assertEqual(board.load_mode(*target("GPU-2"), path=self.path), board.VOLTAGE_MODE)

    def test_no_uuid_selection_is_session_only_and_never_reads_or_writes_file(self):
        gpu, rail = target(None)
        self.path.write_text("not JSON", encoding="utf-8")
        self.assertEqual(board.load_mode(gpu, rail, path=self.path), board.RAW_MODE)
        self.assertFalse(board.save_mode(gpu, rail, board.VOLTAGE_MODE, path=self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not JSON")

    def test_bad_saved_config_is_reported_not_silently_replaced(self):
        gpu, rail = target()
        for content in ("{", json.dumps({"version": 2, "modes": {}}),
                        json.dumps({"version": 1, "modes": {"bad": board.VOLTAGE_MODE}}),
                        json.dumps({"version": 1, "modes": {"[\"GPU-1\",\"NCT3933U\",1,21]": "mystery"}})):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    board.load_mode(gpu, rail, path=self.path)
                with self.assertRaises(ValueError):
                    board.save_mode(gpu, rail, board.VOLTAGE_MODE, path=self.path)
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_mismatched_controller_and_route_cannot_select_meter_mapping(self):
        gpu, rail = target()
        rail.p.src["controller"] = "other"
        with self.assertRaises(ValueError):
            board.load_mode(gpu, rail, path=self.path)
        rail.p.src["controller"] = "NCT3933U"
        rail.p.src["addr7"] = 0x14
        with self.assertRaises(ValueError):
            board.save_mode(gpu, rail, board.VOLTAGE_MODE, path=self.path)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
