# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Survey presence must not depend on every controller implementing PAGE."""

import unittest
from unittest.mock import Mock, call

from tools.i2c_discover import PROBES, responds


class I2cDiscoverTests(unittest.TestCase):
    def test_page_responder_needs_no_fallback(self):
        read = Mock(return_value=b"\x00")
        self.assertTrue(responds(read, 2, 0x20))
        read.assert_called_once_with(2, 0x20, 0, 1)

    def test_ncp_without_page_is_found_by_byte_manufacturer_id(self):
        read = Mock(side_effect=[None, b"A"])
        self.assertTrue(responds(read, 2, 0x20))
        self.assertEqual(read.call_args_list,
                         [call(2, 0x20, 0, 1), call(2, 0x20, 0x99, 1)])

    def test_read_vout_is_the_last_word_sized_fallback(self):
        read = Mock(side_effect=[None, None, b"\x00\x04"])
        self.assertTrue(responds(read, 6, 0x20))
        self.assertEqual(read.call_args_list[-1], call(6, 0x20, 0x8B, 2))

    def test_no_response_checks_exactly_three_commands(self):
        read = Mock(return_value=None)
        self.assertFalse(responds(read, 5, 0x4F))
        self.assertEqual(read.call_args_list, [call(5, 0x4F, 0, 1),
                                              call(5, 0x4F, 0x99, 1),
                                              call(5, 0x4F, 0x8B, 2)])

    def test_controller_identity_uses_byte_word_byte_widths(self):
        widths = {reg: width for reg, width, _ in PROBES}
        self.assertEqual([widths[reg] for reg in (0x99, 0x9A, 0x9B)], [1, 2, 1])


if __name__ == "__main__":
    unittest.main()
