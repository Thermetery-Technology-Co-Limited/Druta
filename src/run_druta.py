# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone frozen entrypoint with the src package first on the import path."""
from druta.druta import main

if __name__ == "__main__":
    raise SystemExit(main())
