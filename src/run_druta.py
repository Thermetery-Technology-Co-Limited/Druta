# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone frozen entrypoint with the src package first on the import path."""
import sys

from druta import diagnostics

if __name__ == "__main__":
    # Before the UI import: a failed import or native crash while loading
    # Dear PyGui, Direct3D or a driver library must still leave a log.
    if sys.argv[1:2] == ["--self-check"]:
        raise SystemExit(diagnostics.cli(sys.argv[2:]))
    diagnostics.start_session_log()
    diagnostics.stage("importing the application")
    from druta.druta import main
    diagnostics.stage("application imported")
    code = main()
    diagnostics.stop_session_log(f"exit {code}")
    raise SystemExit(code)
