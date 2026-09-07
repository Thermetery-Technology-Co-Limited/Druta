# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from nvbackend import GPU, MEM_TYPES

class MaxwellTests(unittest.TestCase):
 def card(self,device=0x1382):
  g=GPU.__new__(GPU);g.arch=Mock(return_value=GPU.ARCH_MAXWELL)
  g.nvapi=SimpleNamespace(selected={'devid':device},ok=True,ClkDomCtlGet=Mock(),ClkDomCtlSet=Mock())
  return g
 def test_ddr3_clock_and_offset_units(self):
  g=self.card();name,div=MEM_TYPES[7];g.static={'mem_type':name,'mem_div':div}
  self.assertEqual(name,'DDR3');self.assertEqual(900/div,900)
  self.assertEqual(g.mem_offset_scale(),(2,'MHz true'))
  self.assertEqual(10*g.mem_offset_scale()[0],20)
 def test_gtx745_has_no_curve_or_private_voltage_write_path(self):
  g=self.card();g._clkdom_layout_cache=object();g._vfp_layout_cache=object()
  self.assertTrue(g.is_gtx745());self.assertFalse(g.vf_curve_applicable())
  self.assertIsNone(g.vfp_layout());self.assertIsNone(g.clkdom_layout())
  self.assertIsNone(g.read_rail_offset_mv());self.assertEqual(g.clkdom_controls_for_ui(),[])
  self.assertFalse(g.set_rail_offset_mv(12.5)[0]);g.nvapi.ClkDomCtlSet.assert_not_called()
 def test_gtx745_results_do_not_claim_other_maxwell_boards(self):
  g=self.card(0x13C2)
  self.assertFalse(g.is_gtx745());self.assertTrue(g.vf_curve_applicable())

if __name__=='__main__':unittest.main()
