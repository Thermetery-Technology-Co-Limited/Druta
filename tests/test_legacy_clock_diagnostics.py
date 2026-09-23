# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Suppressed offset controls still provide raw, read-only decoding evidence."""
import ctypes
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from druta.nvbackend import GPU, CLKDOM_VERSION, u32

class LegacyDiagnosticTests(unittest.TestCase):
    def gpu(self, status=0, extra=None):
        gpu=GPU.__new__(GPU)
        gpu._lock=threading.RLock()
        gpu.static={'name':'legacy test'}
        gpu.nvapi=SimpleNamespace(ok=True,ClkDomCtlGet=object(),ClkDomCtlSet=Mock(),ClkMeasureFreq=None)
        gpu.clkdom_is_blackwell=lambda:False
        gpu.clkdom_ok=lambda:True
        gpu.clkdom_domains=lambda:[0,8]
        gpu.clkdom_layout=lambda:None
        gpu.read_clock_domains=lambda:([{'domain':15,'khz':2402000}],None)
        calls=[]
        def get(mask):
            calls.append(mask)
            buf=(ctypes.c_ubyte*65536)(); p=ctypes.cast(buf,ctypes.POINTER(u32))
            p[0],p[2]=CLKDOM_VERSION,mask
            if status==0:
                p[3]=0x1010000
                if extra is not None:p[extra//4]=123
            return status,buf
        gpu._clkdom_get=get
        return gpu,calls

    def test_header_only_reply_does_not_invent_decoded_fields(self):
        gpu,calls=self.gpu()
        result=gpu.clkdom_debug_report()
        self.assertEqual(calls,[0,1,256])
        self.assertIsNone(result['layout'])
        self.assertEqual(result['entries'],{})
        self.assertTrue(all(r['changed_dwords']=={'0xC':0x1010000} for r in result['raw_queries']))
        self.assertEqual(result['private_clock_domains'][0]['domain'],15)
        gpu.nvapi.ClkDomCtlSet.assert_not_called()

    def test_unknown_data_is_retained_at_raw_offset(self):
        gpu,_=self.gpu(extra=0x888)
        result=gpu.clkdom_debug_report()
        self.assertEqual(result['raw_queries'][1]['changed_dwords']['0x888'],123)
        self.assertEqual(result['entries'],{})
        gpu.nvapi.ClkDomCtlSet.assert_not_called()

    def test_failed_getter_keeps_status_without_support_claim(self):
        gpu,_=self.gpu(status=-104)
        result=gpu.clkdom_debug_report()
        self.assertTrue(all(r['status']==-104 and not r['changed_dwords'] for r in result['raw_queries']))
        self.assertIn('suppressed',result['error'])
        gpu.nvapi.ClkDomCtlSet.assert_not_called()

if __name__=='__main__':unittest.main()
