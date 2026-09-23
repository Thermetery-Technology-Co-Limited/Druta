# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Application assets and existing user data across source and packaged runs."""
import os
from pathlib import Path
import sys


def source_root() -> Path | None:
    """Find an editable/exported source tree from this module, never the CWD."""
    if getattr(sys, "frozen", False):
        return None
    package = Path(__file__).resolve().parent
    if package.name != "druta" or package.parent.name != "src":
        return None
    root = package.parent.parent
    if (root / "pyproject.toml").is_file() and (package / "__init__.py").is_file():
        return root
    return None


def app_dir() -> Path:
    """Directory for a portable nvtune beside the source tree or executable."""
    return source_root() or Path(sys.executable).resolve().parent


def packaged_data_dir() -> Path:
    """Wheel assets copied from the canonical source-root files at build time."""
    return Path(__file__).resolve().parent / "_data"


def resource_path(name: str) -> Path:
    """Locate licenses and I2C recipes in a checkout, bundle, or installed wheel."""
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            return Path(bundle) / name
    return (source_root() or packaged_data_dir()) / name


def profile_dir() -> Path:
    """Keep old source/frozen profiles; installed wheels use per-user storage.

    Merely resolving a directory creates or migrates nothing. PyInstaller used
    to place profiles.py directly under _MEIPASS, so its existing profiles and
    undo snapshots must remain there after the module moves into the package.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", None) or app_dir()) / "profiles"
    root = source_root()
    if root is not None:
        return root / "profiles"
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return local / "Thermetery" / "Druta" / "profiles"
