# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Windows helpers shared by the modern and Windows 7 application builds."""
import ctypes


def dpi_scale(_dll_loader=None):
    """Return desktop scaling after enabling system DPI awareness.

    GetDpiForSystem arrived in Windows 10. Windows 7 instead exposes the
    system DPI through a desktop DC's LOGPIXELSX. Explicit handle signatures
    matter here: ctypes' default integer return type truncates 64-bit HDCs.
    """
    try:
        load = _dll_loader or ctypes.WinDLL
        user32 = load("user32", use_last_error=True)
    except (OSError, AttributeError):
        return 1.0

    try:
        aware = load("shcore", use_last_error=True).SetProcessDpiAwareness
        aware.argtypes = [ctypes.c_int]
        aware.restype = ctypes.c_long
        # E_ACCESSDENIED means a manifest or earlier call already set this.
        if aware(1) not in (0, -2147024891):
            raise OSError("SetProcessDpiAwareness failed")
    except (OSError, AttributeError):
        try:
            aware = user32.SetProcessDPIAware
            aware.argtypes = []
            aware.restype = ctypes.c_int
            aware()
        except (OSError, AttributeError):
            pass

    try:
        get_dpi = user32.GetDpiForSystem
        get_dpi.argtypes = []
        get_dpi.restype = ctypes.c_uint
        dpi = get_dpi()
        if dpi > 0:
            return dpi / 96.0
    except (OSError, AttributeError):
        pass

    try:
        get_dc, release_dc = user32.GetDC, user32.ReleaseDC
        get_dc.argtypes = [ctypes.c_void_p]
        get_dc.restype = ctypes.c_void_p
        release_dc.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        release_dc.restype = ctypes.c_int
        get_caps = load("gdi32", use_last_error=True).GetDeviceCaps
        get_caps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_caps.restype = ctypes.c_int
        dc = get_dc(None)
        if dc:
            try:
                dpi = get_caps(dc, 88)  # LOGPIXELSX
                if dpi > 0:
                    return dpi / 96.0
            finally:
                release_dc(None, dc)
    except (OSError, AttributeError):
        pass
    return 1.0
