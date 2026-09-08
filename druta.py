# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve source launches while forwarding imports to the src package."""
from pathlib import Path as _Path
import sys as _sys

_package_dir = _Path(__file__).resolve().parent / "src" / "druta"
if __name__ == "__main__":
    _sys.path.insert(0, str(_package_dir.parent))
    from druta.druta import main as _main
    raise SystemExit(_main())
else:
    import importlib.util as _import_util
    _spec = _import_util.spec_from_file_location(
        __name__, _package_dir / "__init__.py",
        submodule_search_locations=[str(_package_dir)])
    if _spec is None or _spec.loader is None:
        raise ImportError("Cannot locate the Druta source package")
    # Preserve the existing module object, including on importlib.reload, so
    # previously imported submodules keep the same parent package identity.
    _module = _sys.modules[__name__]
    _module.__file__ = _spec.origin
    _module.__cached__ = _spec.cached
    _module.__loader__ = _spec.loader
    _module.__spec__ = _spec
    _module.__package__ = __name__
    _module.__path__ = list(_spec.submodule_search_locations)
    _spec.loader.exec_module(_module)
