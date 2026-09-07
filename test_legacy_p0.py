# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from nvbackend import GPU


def card():
 g=GPU.__new__(GPU);g._lock=threading.RLock();g._legacy_p0_owned=False
 g.arch=Mock(return_value=3);g.static={'driver':'472.12','vbios':'82.07.32.00.6a'}
 g.nvapi=SimpleNamespace(ok=True,gpu=512,selected={'devid':0x1382,'subsys':0x6893103c},ForcePstate=Mock(return_value=0))
 g.read=Mock(return_value={'pstate':0,'core':540,'mem':900,'mem_p0max':900})
 return g

class LegacyP0Backend(unittest.TestCase):
 def test_gtx690_exact_profile_and_per_gpu_force_release(self):
  for handle in (69004,69005):
   g=card();g.arch.return_value=2;g.nvapi.gpu=handle
   g.nvapi.selected={'devid':0x1188,'subsys':0x84061043}
   g.static={'driver':'472.12','vbios':'80.04.1E.00.18'}
   g.read.return_value={'pstate':0,'core':705,'mem':3004,'mem_p0max':3004}
   self.assertEqual(g.legacy_p0_profile()['held_core_mhz'],705)
   with patch('nvbackend.time.sleep'):self.assertTrue(g.hold_legacy_p0()[0])
   self.assertTrue(g.release_legacy_p0()[0])
   self.assertEqual([c.args[0] for c in g.nvapi.ForcePstate.call_args_list],[handle,handle])
   self.assertEqual([[v.value for v in c.args[1:]] for c in g.nvapi.ForcePstate.call_args_list],[[0,2],[16,2]])
 def test_gtx690_nearby_boards_and_other_driver_remain_unverified(self):
  for field,value in [('driver','473.81'),('vbios','80.04.1e.00.19'),('subsys',0x84081043),('devid',0x1184),('arch',3)]:
   g=card();g.arch.return_value=2
   g.nvapi.selected={'devid':0x1188,'subsys':0x84061043}
   g.static={'driver':'472.12','vbios':'80.04.1e.00.18'}
   if field=='arch':g.arch.return_value=value
   elif field in g.static:g.static[field]=value
   else:g.nvapi.selected[field]=value
   self.assertIsNone(g.legacy_p0_profile());self.assertFalse(g.hold_legacy_p0()[0])
   g.nvapi.ForcePstate.assert_not_called()
 def test_gtx745_profile_retains_its_own_measurements(self):
  g=card();self.assertEqual(g.legacy_p0_profile()['name'],'GTX 745')
  self.assertEqual(g.legacy_p0_profile()['held_core_mhz'],540)
  self.assertEqual(g.legacy_p0_profile()['boost_core_mhz'],1072)
 def test_only_validated_board_driver_and_firmware_can_write(self):
  for field,value in [('driver','580.97'),('vbios','other'),('vbios',None),('subsys',0),('devid',0x1184)]:
   g=card()
   if field in g.static:g.static[field]=value
   else:g.nvapi.selected[field]=value
   self.assertFalse(g.legacy_p0_supported());self.assertFalse(g.hold_legacy_p0()[0])
   g.nvapi.ForcePstate.assert_not_called()
  g=card();g.pairing_error='wrong PCI card';self.assertFalse(g.legacy_p0_supported())
 def test_verified_hold_and_automatic_release(self):
  g=card()
  with patch('nvbackend.time.sleep'):
   self.assertTrue(g.hold_legacy_p0()[0])
  self.assertTrue(g.legacy_p0_owned());self.assertEqual(g.read.call_count,3)
  call=g.nvapi.ForcePstate.call_args.args;self.assertEqual([v.value for v in call[1:]],[0,2])
  self.assertTrue(g.release_legacy_p0()[0]);self.assertFalse(g.legacy_p0_owned())
  call=g.nvapi.ForcePstate.call_args.args;self.assertEqual([v.value for v in call[1:]],[16,2])
 def test_p0_without_top_memory_fails_and_releases(self):
  g=card();g.read.return_value={'pstate':0,'mem':405,'mem_p0max':900}
  with patch('nvbackend.time.sleep'):self.assertFalse(g.hold_legacy_p0()[0])
  self.assertFalse(g.legacy_p0_owned());self.assertEqual(g.nvapi.ForcePstate.call_count,2)
 def test_verification_read_exception_releases(self):
  g=card();g.read.side_effect=RuntimeError('read failed')
  self.assertFalse(g.hold_legacy_p0()[0]);self.assertFalse(g.legacy_p0_owned())
 def test_failed_rollback_retains_ownership_for_retry(self):
  g=card();g.nvapi.ForcePstate.side_effect=[0,-1,0];g.read.side_effect=RuntimeError('read failed')
  ok,msg=g.hold_legacy_p0();self.assertFalse(ok);self.assertIn('RELEASE FAILED',msg)
  self.assertTrue(g.legacy_p0_owned());self.assertTrue(g.release_legacy_p0()[0]);self.assertFalse(g.legacy_p0_owned())
 def test_failed_set_never_claims_a_hold(self):
  g=card();g.nvapi.ForcePstate.return_value=-1
  self.assertFalse(g.hold_legacy_p0()[0]);self.assertFalse(g.legacy_p0_owned());g.read.assert_not_called()
 def test_release_does_not_touch_another_sessions_hold(self):
  g=card();self.assertTrue(g.release_legacy_p0()[0]);g.nvapi.ForcePstate.assert_not_called()
 def test_reset_releases_only_owned_p0_and_preserves_failure(self):
  from test_pascal_reset import reset_gpu
  for owned,rc in [(True,0),(True,-1),(False,0)]:
   g=reset_gpu();g._legacy_p0_owned=owned;g.nvapi.ForcePstate=Mock(return_value=rc);g.nvapi.gpu=512
   steps={s.name:tuple(s) for s in g.reset_all()}
   self.assertEqual(GPU.P0_LOCK_STEP in steps,owned)
   if owned:self.assertEqual(steps[GPU.P0_LOCK_STEP][0],rc==0)
   self.assertEqual(g.legacy_p0_owned(),owned and rc!=0)
 def test_unmeasured_private_clock_domains_remain_unknown(self):
  g=card()
  for arch in (None,2,3,6,10):
   g.arch.return_value=arch
   self.assertIsNone(g.clkdom_delta_inert(19))
  g.arch.return_value=4
  self.assertTrue(g.clkdom_delta_inert(1));self.assertFalse(g.clkdom_delta_inert(2))

if __name__=='__main__':unittest.main()
