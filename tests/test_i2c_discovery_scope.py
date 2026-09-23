# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit controller/route scopes preserve read-only discovery boundaries."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import railctl
from druta.controllers import mp2888
from druta import mp29816


class ScopedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.nvapi = SimpleNamespace(ok=True, selected={"devid": 1, "subsys": 2})
        self.mp = SimpleNamespace(regulator="MPS MP2888A")
        self.mp2 = SimpleNamespace(regulator="MPS MP29816")
        self.generic = SimpleNamespace(regulator="Generic board regulator", port=4,
                                       addrs=[0x21, 0x22],
                                       candidate_for=Mock(return_value=True))
        patcher = patch("druta.railctl.load_profiles",
                        return_value=[self.mp, self.mp2, self.generic])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_controller_list_deduplicates_loaded_recipe_names(self):
        with patch("druta.railctl.load_profiles",
                   return_value=[self.mp, self.mp, self.mp2, self.generic]):
            self.assertEqual(railctl.controller_names(),
                             ["Nuvoton NCT3933U", "NCP4206", "MPS MP2888A",
                              "MPS MP29816", "Generic board regulator"])

    def test_nct_scope_visits_only_exact_pairs_and_keeps_all_hits(self):
        seen, events = [], []

        def make_dac(nvapi, *, port, addr7):
            seen.append((port, addr7))
            return SimpleNamespace(present=lambda: True,
                                   telemetry=lambda: {"outputs": [0, 0, 0]})

        with patch("druta.controllers.nct3933.NCT3933U", side_effect=make_dac), \
                patch("druta.controllers.ncp4206.NCP4206") as ncp, \
                patch("druta.controllers.mp2888.discover") as mp, \
                patch("druta.mp29816.discover") as mp2, \
                patch("druta.railctl.Rail") as generic:
            hits = railctl.discover(self.nvapi, controller="Nuvoton NCT3933U",
                                    routes=[(2, 0x15), (1, 0x15), (1, 0x15),
                                            (1, 0x16)],
                                    progress=lambda *e: events.append(e))
        self.assertEqual(len(hits), 2)
        self.assertEqual(seen, [(1, 0x15), (2, 0x15)])
        self.assertEqual([e[:2] for e in events], [(0, 2), (1, 2), (2, 2)])
        ncp.assert_not_called()
        mp.assert_not_called()
        mp2.assert_not_called()
        generic.assert_not_called()
        self.generic.candidate_for.assert_not_called()

    def test_ncp_scope_and_generic_scope_do_not_cross_probe(self):
        seen, events = [], []

        def make_ncp(nvapi, *, architecture, port, addr7):
            seen.append((port, addr7))
            return SimpleNamespace(present=lambda: True)

        with patch("druta.controllers.ncp4206.NCP4206", side_effect=make_ncp), \
                patch("druta.controllers.nct3933.NCT3933U") as nct, \
                patch("druta.controllers.mp2888.discover") as mp, \
                patch("druta.mp29816.discover") as mp2:
            hits = railctl.discover(self.nvapi, controller="NCP4206",
                                    routes=[(3, 0x20)],
                                    progress=lambda *e: events.append(e))
        self.assertEqual(len(hits), 1)
        self.assertEqual(seen, [(3, 0x20)])
        self.assertEqual([e[:2] for e in events], [(0, 1), (1, 1)])
        nct.assert_not_called()
        mp.assert_not_called()
        mp2.assert_not_called()
        self.generic.candidate_for.assert_not_called()

        self.generic.candidate_for.reset_mock()
        events.clear()
        with patch("druta.railctl.Rail") as rail, \
                patch("druta.controllers.nct3933.NCT3933U") as nct, \
                patch("druta.controllers.ncp4206.NCP4206") as ncp:
            rail.return_value.present.return_value = True
            hits = railctl.discover(self.nvapi, controller=self.generic.regulator,
                                    routes=[(4, 0x22), (5, 0x21)],
                                    progress=lambda *e: events.append(e))
        self.assertEqual(len(hits), 1)
        rail.assert_called_once_with(self.generic, self.nvapi, addr7=0x22)
        self.generic.candidate_for.assert_called_once_with(1, 2)
        self.assertEqual([e[:2] for e in events], [(0, 1), (1, 1)])
        nct.assert_not_called()
        ncp.assert_not_called()

    def test_mps_families_get_exact_route_scope_and_progress(self):
        for name, module in (("MPS MP2888A", "druta.controllers.mp2888"),
                             ("MPS MP29816", "druta.mp29816")):
            with self.subTest(name=name):
                events = []

                def scan(nvapi, profile, log=None, *, progress=None, cancelled=None,
                         routes=None):
                    self.assertEqual(routes, {(2, 0x30)})
                    progress("matched route")
                    return [SimpleNamespace(p=SimpleNamespace(name=name, port=2),
                                            addr7=0x30)]

                with patch(module + ".discover", side_effect=scan) as selected, \
                        patch("druta.controllers.nct3933.NCT3933U") as nct, \
                        patch("druta.controllers.ncp4206.NCP4206") as ncp:
                    hits = railctl.discover(self.nvapi, controller=name,
                                            routes=[(2, 0x30)],
                                            progress=lambda *e: events.append(e))
                self.assertEqual(len(hits), 1)
                selected.assert_called_once()
                self.assertEqual([e[:2] for e in events], [(0, 1), (1, 1)])
                nct.assert_not_called()
                ncp.assert_not_called()

    def test_invalid_scope_fails_before_any_probe_and_cancel_stops_at_boundary(self):
        for kwargs in ({"controller": "NCT3933U"},
                       {"controller": 1}, {"routes": [(8, 0x15)]},
                       {"routes": [(1, 0x80)]}, {"routes": [(True, 0x15)]},
                       {"routes": [[1, 0x15]]}):
            with self.subTest(kwargs=kwargs), \
                    patch("druta.controllers.nct3933.NCT3933U") as nct, \
                    patch("druta.controllers.ncp4206.NCP4206") as ncp:
                with self.assertRaises(ValueError):
                    railctl.discover(self.nvapi, **kwargs)
                nct.assert_not_called()
                ncp.assert_not_called()
        events = []
        with patch("druta.controllers.nct3933.NCT3933U") as nct:
            nct.return_value.present.return_value = False
            railctl.discover(self.nvapi, controller="Nuvoton NCT3933U",
                             routes=[(0, 0x10), (1, 0x10)],
                             progress=lambda *e: events.append(e),
                             cancelled=lambda: len(events) >= 2)
        nct.assert_called_once()
        self.assertEqual([e[:2] for e in events], [(0, 2), (1, 2)])


class DirectMPScopeTests(unittest.TestCase):
    def test_mp2888_only_constructs_requested_route(self):
        api = SimpleNamespace(ok=True)
        with patch.object(mp2888, "MP2888Candidate") as candidate:
            candidate.return_value.present.return_value = False
            events = []
            self.assertEqual(mp2888.discover(api, SimpleNamespace(),
                                              routes=[(3, 0x20)],
                                              progress=events.append), [])
        candidate.assert_called_once_with(unittest.mock.ANY, api, 3, 0x20)
        self.assertEqual(events, ["MP2888A port 3, 0x20"])

    def test_mp29816_only_reads_requested_route(self):
        api = SimpleNamespace(ok=True)
        profile = SimpleNamespace()
        with patch.object(mp29816, "Rail") as probe:
            probe.return_value.read.return_value = None
            events = []
            self.assertEqual(mp29816.discover(api, profile,
                                               routes=[(7, 0x30)],
                                               progress=events.append), [])
        probe.assert_called_once_with(profile, api, 0x30)
        self.assertEqual(probe.return_value.read.call_args.args, (0xAD, 5))
        self.assertEqual(probe.return_value.p.port, 7)
        self.assertEqual(events, ["MP29816 port 7, 0x30"])


if __name__ == "__main__":
    unittest.main()
