# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from druta.nvbackend import GPU, MEM_TYPES
from tests.test_vfp_read import fake_gpu

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
 def test_maxwell_private_voltage_still_requires_validated_layout(self):
  g=self.card();g._clkdom_layout_cache=object()
  self.assertIsNone(g.clkdom_layout())
  self.assertIsNone(g.read_rail_offset_mv());self.assertEqual(g.clkdom_controls_for_ui(),[])
  self.assertFalse(g.set_rail_offset_mv(12.5)[0]);g.nvapi.ClkDomCtlSet.assert_not_called()
 def test_maxwell_curve_getter_and_layout_determine_availability_without_device_blacklist(self):
  for device in (0x1382,0x13C2):
   g=fake_gpu('turing');g.arch=Mock(return_value=GPU.ARCH_MAXWELL)
   g.nvapi.selected={'devid':device}
   self.assertTrue(g.vf_curve_applicable())
   points,error=g.read_vf_curve()
   self.assertIsNone(error);self.assertEqual(len(points),128)
   self.assertEqual(g.vfp_layout().n_gpu,128)
 def test_maxwell_failed_or_incomplete_curve_getter_cannot_authorize_write(self):
  for failure in ('rejected','incomplete','missing'):
   g=fake_gpu('turing');g.arch=Mock(return_value=GPU.ARCH_MAXWELL)
   g.nvapi.selected={'devid':0x1382};g.nvapi.BoostTableSet=Mock(return_value=0)
   if failure=='rejected':g.nvapi.VfpCurve=Mock(return_value=-1)
   elif failure=='incomplete':g.nvapi.incomplete=True
   else:g.nvapi.VfpCurve=None
   self.assertTrue(g.vf_curve_applicable())
   points,error=g.read_vf_curve()
   self.assertIsNone(points);self.assertTrue(error)
   self.assertFalse(g.apply_vf_deltas({0:15000})[0])
   g.nvapi.BoostTableSet.assert_not_called()

if __name__=='__main__':unittest.main()
