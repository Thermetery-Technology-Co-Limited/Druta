# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from druta import ncp4206 as n
from druta import profiles
from tests.test_tune_profiles import hardware

class NCPTests(unittest.TestCase):
    def rail(self):
        r=n.NCP4206(SimpleNamespace(ok=True, selected={'devid':0x1184,'subsys':0x1033196e}), architecture=2)
        r.regs={0x99:65,0x9a:12952,0x9b:1,0x20:32,0x21:0,0xd2:0x72,0xd3:0x72,0xdd:3,0xd7:47726}
        r.read=lambda reg,width:r.regs.get(reg)
        r.calls=[]
        def write(reg,value,width):r.calls.append((reg,value,width));r.regs[reg]=value;return True
        r._raw_write=write
        return r

    def test_mode_bounds_and_no_wrap(self):
        r=self.rail()
        for value in (1282,1350,2000,float('nan'),float('inf')):
            self.assertFalse(r.set_voltage_mv(value,acknowledged=True)[0])
        self.assertFalse(r.calls)
        self.assertTrue(r.set_voltage_mv(1281,acknowledged=True)[0])
        self.assertEqual(n.decode_vid(r.regs[0x21]),1275)
        r.xoc=True
        self.assertTrue(r.plan(1350)[0])
        self.assertFalse(r.plan(2000)[0])
        self.assertFalse(r.plan(2001)[0])
        for value in (0,1,199,255):self.assertIsNone(n.decode_vid(value))

    def test_command_precedes_enable_and_auto_precedes_off_code(self):
        r=self.rail();before=dict(r.regs)
        self.assertTrue(r.set_voltage_mv(1250,acknowledged=True)[0])
        self.assertEqual([x[0] for x in r.calls],[0x21,0xd2,0xd3])
        self.assertEqual(r.regs[0xd2],0x7a);self.assertEqual(r.regs[0xdd],3)
        r.calls=[];self.assertTrue(r.reset()[0])
        self.assertEqual([x[0] for x in r.calls],[0xd2,0xd3,0x21])
        self.assertEqual(r.regs,before)

    def test_partial_failure_restores_original_mode_and_command(self):
        r=self.rail();before=dict(r.regs);write=r._raw_write
        def fail_once(reg,value,width):
            if reg==0xd3 and value&8:return False
            return write(reg,value,width)
        r._raw_write=fail_once
        self.assertFalse(r.set_voltage_mv(1250,acknowledged=True)[0])
        self.assertEqual(r.regs,before)

    def test_bad_identity_and_mixed_modes_refuse_without_write(self):
        r=self.rail();r.regs[0x9a]=0
        self.assertFalse(r.set_voltage_mv(1250,acknowledged=True)[0]);self.assertFalse(r.calls)
        r=self.rail();r.regs[0xd2]|=8
        self.assertFalse(r.set_voltage_mv(1250,acknowledged=True)[0]);self.assertFalse(r.calls)

    def test_profiles_restore_absolute_target_and_auto_without_touching_calibration(self):
        gpu,_,calls=hardware();r=self.rail()
        auto=profiles.capture(gpu,r)
        self.assertIn('control',auto['i2c']);self.assertNotIn('offset_mv',auto['i2c'])
        self.assertIn('Auto (GPU VID)',profiles.summarize(auto))
        self.assertIn('Kepler',profiles.summarize(auto))
        self.assertNotIn('GTX 770',profiles.summarize(auto))
        self.assertTrue(r.set_voltage_mv(1250,acknowledged=True)[0])
        target=profiles.capture(gpu,r)
        self.assertIn('1250 mV',profiles.summarize(target))
        self.assertTrue(all(ok for ok,msg in profiles.restore(gpu,auto,rail=r,i2c_verified=True)))
        self.assertFalse(r.capture_control()['enabled'])
        self.assertTrue(all(ok for ok,msg in profiles.restore(gpu,target,rail=r,i2c_verified=True)))
        self.assertEqual(r.regs[0xdd],3)
        target['i2c']['control']['command']=n.encode_vid(1350);target['xoc']=False
        self.assertIsNotNone(profiles.preflight(gpu,target,r))
        target['xoc']=True;self.assertIsNone(profiles.preflight(gpu,target,r))
        target['i2c']['control']['command']=0
        self.assertIsNotNone(profiles.preflight(gpu,target,r))

if __name__=='__main__':unittest.main()
