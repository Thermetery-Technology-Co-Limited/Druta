# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""A remembered I2C route is only a hint for a fresh, scoped read-only probe."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import i2c_cache, profiles


def gpu(uuid="GPU-A"):
    return SimpleNamespace(static={"uuid": uuid}, nvapi=object(), arch=Mock(return_value=5))


def rail(*, controller="NCP4206", port=2, addr7=0x20, source=None,
         read_only=False, control=True):
    recipe = source if source is not None else {"profile": {"regulator": controller},
                                                "bus": {"port": port, "addr7": addr7}}
    p = SimpleNamespace(regulator=controller, name=f"{controller} voltage",
                        rail="NVVDD", port=port, src=recipe,
                        read_only=read_only)
    candidate = SimpleNamespace(p=p, addr7=addr7,
                                present=Mock(return_value=True),
                                telemetry=Mock(return_value={"offset_mv": 0,
                                                             "vout_mv": 1000}),
                                reset=Mock(), set_offset_mv=Mock())
    if control:
        candidate.capture_control = Mock(return_value={"kind": "absolute_vid",
                                                       "enabled": False, "command": 32})
    return candidate


class I2CRouteCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "i2c-routes.json"
        self.gpu = gpu()
        self.selected = rail()

    def saved(self):
        self.assertTrue(i2c_cache.remember(self.gpu, self.selected, path=self.path))
        return i2c_cache.load_route(self.gpu, path=self.path)

    def test_remember_only_selected_route_for_uuid_without_runtime_state(self):
        route = self.saved()
        self.assertEqual(route, {"uuid": "GPU-A", "controller": "NCP4206",
                                 "identity": profiles.rail_identity(self.selected)})
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document, {"version": 1, "routes": {"GPU-A": route}})
        self.assertNotIn("output", self.path.read_text(encoding="utf-8"))
        self.assertIsNone(i2c_cache.load_route(gpu("GPU-B"), path=self.path))
        other = rail(port=4)
        self.assertTrue(i2c_cache.remember(gpu("GPU-B"), other, path=self.path))
        self.assertEqual(i2c_cache.load_route(self.gpu, path=self.path), route)
        replacement = rail(port=3)
        self.assertTrue(i2c_cache.remember(self.gpu, replacement, path=self.path))
        self.assertEqual(i2c_cache.load_route(self.gpu, path=self.path)["identity"]["port"], 3)
        self.assertEqual(len(json.loads(self.path.read_text())["routes"]), 2)

    def test_no_uuid_or_no_file_never_creates_or_loads_cache(self):
        self.assertIsNone(i2c_cache.load_route(self.gpu, path=self.path))
        for selected in (None, SimpleNamespace(), gpu(None), gpu("")):
            self.assertIsNone(i2c_cache.load_route(selected, path=self.path))
            self.assertFalse(i2c_cache.remember(selected, self.selected, path=self.path))
        self.assertFalse(self.path.exists())

    def test_corrupt_or_wrong_schema_file_is_not_silently_ignored(self):
        for payload in ("{", "[]", '{}', '{"version":2,"routes":{}}',
                        '{"version":1,"routes":{"GPU-A":{}}}',
                        '{"version":1,"routes":{},"negative":[]}'):
            with self.subTest(payload=payload):
                self.path.write_text(payload, encoding="utf-8")
                with self.assertRaises((ValueError, OSError)):
                    i2c_cache.load_route(self.gpu, path=self.path)

    @patch("druta.i2c_cache.railctl.discover")
    def test_reconnect_is_fresh_scoped_probe_and_readable_control(self, discover):
        route = self.saved()
        live = rail()
        discover.return_value = [live]
        progress = Mock()
        cancelled = Mock(return_value=False)
        hits = i2c_cache.reconnect(self.gpu, route, controller="NCP4206",
                                   progress=progress, cancelled=cancelled)
        self.assertEqual(hits, [live])
        self.assertIs(hits[0], live)
        discover.assert_called_once_with(
            self.gpu.nvapi, architecture=5, controller="NCP4206",
            routes=[(2, 0x20)], progress=progress, cancelled=cancelled)
        live.present.assert_called_once()
        live.capture_control.assert_called_once()
        live.reset.assert_not_called()
        live.set_offset_mv.assert_not_called()
        self.assertEqual(i2c_cache.load_route(self.gpu, path=self.path), route)

    @patch("druta.i2c_cache.railctl.discover")
    def test_other_gpu_or_family_never_uses_cached_controller(self, discover):
        route = self.saved()
        self.assertEqual(i2c_cache.reconnect(gpu("GPU-B"), route), [])
        self.assertEqual(i2c_cache.reconnect(self.gpu, route,
                                             controller="Nuvoton NCT3933U"), [])
        self.assertEqual(i2c_cache.reconnect(None, route), [])
        discover.assert_not_called()

    @patch("druta.i2c_cache.railctl.discover")
    def test_changed_recipe_route_or_controller_identity_is_rejected(self, discover):
        route = self.saved()
        changed = [rail(source={"edited": True}), rail(port=3), rail(addr7=0x21),
                   rail(controller="Nuvoton NCT3933U")]
        discover.return_value = changed
        self.assertEqual(i2c_cache.reconnect(self.gpu, route), [])
        for candidate in changed:
            candidate.reset.assert_not_called()
            candidate.set_offset_mv.assert_not_called()

    @patch("druta.i2c_cache.railctl.discover")
    def test_cancel_or_failed_read_returns_no_hit_without_deleting_hint(self, discover):
        route = self.saved()
        self.assertEqual(i2c_cache.reconnect(self.gpu, route,
                                             cancelled=lambda: True), [])
        discover.assert_not_called()
        discover.return_value = [rail()]
        self.assertEqual(i2c_cache.reconnect(
            self.gpu, route, cancelled=Mock(side_effect=[False, True])), [])
        unreadable = rail()
        unreadable.capture_control.side_effect = OSError("transient bus read")
        discover.return_value = [unreadable]
        self.assertEqual(i2c_cache.reconnect(self.gpu, route), [])
        self.assertEqual(i2c_cache.load_route(self.gpu, path=self.path), route)
        discover.side_effect = OSError("temporary bus failure")
        self.assertEqual(i2c_cache.reconnect(self.gpu, route), [])
        self.assertEqual(i2c_cache.load_route(self.gpu, path=self.path), route)

    @patch("druta.i2c_cache.railctl.discover")
    def test_generic_readability_requires_offset_only_when_writable(self, discover):
        writable = rail(control=False)
        writable.telemetry.return_value = {"vout_mv": 1000, "offset_mv": None}
        discover.return_value = [writable]
        route = self.saved()
        self.assertEqual(i2c_cache.reconnect(self.gpu, route), [])
        writable.telemetry.return_value["offset_mv"] = 0
        self.assertEqual(i2c_cache.reconnect(self.gpu, route), [writable])
        readonly = rail(read_only=True, control=False)
        readonly.telemetry.return_value = {"offset_mv": None, "vout_mv": None,
                                           "temperature_c": 42.5}
        discover.return_value = [readonly]
        self.assertEqual(i2c_cache.reconnect(self.gpu, route), [readonly])
        readonly.telemetry.return_value["temperature_c"] = None
        self.assertEqual(i2c_cache.reconnect(self.gpu, route), [])


if __name__ == "__main__":
    unittest.main()
