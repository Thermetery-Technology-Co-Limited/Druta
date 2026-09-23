# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Current-DAC profiles preserve raw bytes and reject the wrong controller."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import profiles
from tests.test_tune_profiles import hardware


class CurrentDacRail:
    current_dac = True
    absolute_voltage = False

    def __init__(self):
        self.p = SimpleNamespace(
            src={"controller": "NCT3933", "outputs": [0, 1, 2]},
            name="NCT3933 current DAC", rail="NVVDD", port=1,
            read_only=False)
        self.addr7 = 0x60
        self.outputs = [0x00, 0x80, 0xFF]
        self.configuration = 0xA5
        self.writes = []
        self.present = Mock(return_value=True)
        self.telemetry = Mock(side_effect=AssertionError("no voltage telemetry"))

    def capture_control(self):
        return {"outputs": list(self.outputs), "configuration": self.configuration}

    def validate_control(self, state, xoc=None):
        if xoc is not None:
            raise ValueError("current DAC is not an mV/XOC request")
        if not isinstance(state, dict) or set(state) != {"outputs", "configuration"}:
            raise ValueError("invalid current DAC control")
        outputs = state["outputs"]
        if (not isinstance(outputs, list) or len(outputs) != 3
                or any(type(value) is not int or not 0 <= value <= 255
                       for value in outputs)):
            raise ValueError("invalid current DAC outputs")
        config = state["configuration"]
        if type(config) is not int or not 0 <= config <= 255:
            raise ValueError("invalid current DAC configuration")
        if config != self.configuration:
            raise ValueError("current DAC configuration changed")

    def restore_control(self, state, *, recovery=False):
        self.validate_control(state)
        self.outputs = list(state["outputs"])
        self.writes.append(list(self.outputs))
        return True, "current DAC outputs restored"


class CurrentDacProfileTests(unittest.TestCase):
    def setUp(self):
        self.gpu, _, self.gpu_writes = hardware()
        self.rail = CurrentDacRail()
        captured = profiles.capture(self.gpu, self.rail)
        self.state = {"schema": profiles.SCHEMA,
                      "device": captured["device"],
                      "i2c": captured["i2c"]}

    def assert_no_write(self, state, rail=None):
        rail = self.rail if rail is None else rail
        error = profiles.preflight(self.gpu, state, rail, apply_curve=False)
        self.assertIsInstance(error, str)
        result = profiles.restore(self.gpu, state, apply_curve=False,
                                  rail=rail, i2c_verified=True)
        self.assertEqual(len(result), 1, result)
        self.assertFalse(result[0][0])
        self.assertEqual(self.rail.writes, [])
        self.assertEqual(self.gpu_writes, [])
        return error

    def test_raw_three_output_round_trip_preserves_sign_bit_and_configuration(self):
        i2c = self.state["i2c"]
        self.assertEqual(i2c["format"], profiles.CURRENT_DAC_FORMAT)
        self.assertEqual(i2c["current_dac_control"],
                         {"outputs": [0x00, 0x80, 0xFF], "configuration": 0xA5})
        self.assertNotIn("control", i2c)
        self.assertNotIn("offset_mv", i2c)
        self.assertFalse(self.rail.telemetry.called)
        saved = json.loads(json.dumps(self.state))
        self.rail.outputs = [5, 6, 7]
        self.assertIsNone(profiles.preflight(self.gpu, saved, self.rail,
                                             apply_curve=False))
        results = profiles.restore(self.gpu, saved, apply_curve=False,
                                   rail=self.rail, i2c_verified=True)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertEqual(self.rail.outputs, [0x00, 0x80, 0xFF])
        self.assertEqual(self.rail.configuration, 0xA5)
        self.assertEqual(self.rail.writes, [[0x00, 0x80, 0xFF]])
        summary = profiles.summarize(saved)
        self.assertIn("current DAC raw 0x00/0x80/0xFF", summary)
        self.assertIn("config 0xA5", summary)
        self.assertNotIn("mV", summary)

    def test_invalid_saved_shape_and_changed_configuration_block_all_writes(self):
        for mutation in (
                lambda s: s["i2c"]["current_dac_control"].update(outputs=[0, 128]),
                lambda s: s["i2c"]["current_dac_control"].update(outputs=[0, 128, 256]),
                lambda s: s["i2c"]["current_dac_control"].update(outputs=[0, True, 255]),
                lambda s: s["i2c"]["current_dac_control"].update(outputs=[0, -1, 255]),
                lambda s: s["i2c"]["current_dac_control"].update(configuration=0xA4),
                lambda s: s["i2c"].update(control={"command": 0}),
                lambda s: s["i2c"].pop("format"),
                lambda s: s["i2c"].pop("current_dac_control")):
            with self.subTest(mutation=mutation):
                state = copy.deepcopy(self.state)
                mutation(state)
                self.assert_no_write(state)

    def test_wrong_controller_gpu_or_unverified_session_blocks_restore(self):
        for key in ("profile", "sha256", "port", "addr7", "rail"):
            with self.subTest(key=key):
                state = copy.deepcopy(self.state)
                state["i2c"][key] = "other"
                self.assertIn("regulator", self.assert_no_write(state))
        state = copy.deepcopy(self.state)
        state["device"]["uuid"] = "another GPU"
        self.assertIn("uuid", self.assert_no_write(state))
        other = CurrentDacRail()
        other.current_dac = False
        other.absolute_voltage = True
        self.assertIn("current DAC", self.assert_no_write(self.state, other))
        other.writes = []
        legacy = copy.deepcopy(self.state)
        legacy["i2c"].pop("format")
        legacy["i2c"].pop("current_dac_control")
        legacy["i2c"]["control"] = {"command": 0, "enabled": False}
        self.assertIn("current DAC", self.assert_no_write(legacy))
        result = profiles.restore(self.gpu, self.state, apply_curve=False,
                                  rail=self.rail, i2c_verified=False)
        self.assertFalse(result[0][0])
        self.assertIn("verified", result[0][1])
        self.assertEqual(self.rail.writes, [])

    def test_capture_failure_is_visible_in_incomplete_snapshot(self):
        self.rail.capture_control = Mock(side_effect=OSError("transient I2C read failure"))
        state = profiles.capture(self.gpu, self.rail)
        self.assertIsNone(state["i2c"])
        self.assertTrue(any("I2C current DAC control NOT captured" in item
                            for item in profiles.incomplete(state)))
        self.assertIn("INCOMPLETE", profiles.summarize(state))

    def test_live_configuration_or_presence_change_blocks_replay(self):
        self.rail.configuration = 0xA4
        self.assertIn("configuration changed", self.assert_no_write(self.state))
        self.rail.configuration = 0xA5
        self.rail.present.return_value = False
        self.assertIn("not available", self.assert_no_write(self.state))
        self.rail.present.return_value = True
        self.rail.p.read_only = True
        self.assertIn("not available", self.assert_no_write(self.state))


if __name__ == "__main__":
    unittest.main()
