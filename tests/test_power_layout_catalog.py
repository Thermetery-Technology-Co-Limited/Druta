# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import subprocess
import sys
import unittest
from pathlib import Path

from druta import power_layout_catalog
from druta.nvbackend import GPU


class PowerLayoutCatalog(unittest.TestCase):
    def test_python_backend_uses_the_shared_tables(self):
        self.assertIs(GPU._CURRENT_LIMIT_LAYOUTS, power_layout_catalog.CURRENT_LIMIT_LAYOUTS)
        self.assertIs(GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS, power_layout_catalog.CURRENT_LIMIT_LAYOUT_FALLBACKS)

    def test_checked_in_native_layouts_match_catalog(self):
        tool = Path(__file__).resolve().parents[1] / "native/tools/generate_power_layouts.py"
        subprocess.run([sys.executable, str(tool), "--check"], check=True)
