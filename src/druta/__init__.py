# Druta - GPU monitor and tuner for NVIDIA cards
# Copyright (C) 2026 Thermetery Technology Co Limited
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Druta - GPU monitor and tuner for NVIDIA cards.
"""

__version__ = "1.3.0"
__author__ = "Thermetery Technology Co Limited"
__license__ = "GPL-3.0-or-later"

# Don't import .druta at package level to avoid importing dearpygui
# Users should import druta.druta or druta.nvbackend directly

__all__ = ["__version__"]
