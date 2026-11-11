# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the native window responsive without submitting Dear PyGui frames.

All window operations run on the attaching UI thread. A blocking hotkey thread
owns Ctrl+Shift+F11 so Dear PyGui's separate message pump cannot consume it.
This module has no GPU/backend dependency and never recreates the viewport.
"""

import ctypes
import math
import os
import sys
import threading


HOTKEY_LABEL = "Ctrl+Shift+F11"
_HOTKEY_ID = 0x4452
_HOTKEY_MODIFIERS = 0x0002 | 0x0004  # MOD_CONTROL | MOD_SHIFT; Vista has no MOD_NOREPEAT.
_HOTKEY_VK = 0x7A
_WM_QUIT = 0x0012
_WM_HOTKEY = 0x0312
_WM_TIMER = 0x0113


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32),
                ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class _Message(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt", _Point),
                ("lPrivate", ctypes.c_uint32)]


class _Windows:
    """Typed Win32 boundary, kept separate from pause/hotkey policy for tests."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Display pause requires the Windows message loop")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
        hwnd, uint, dword = ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32
        msg = ctypes.POINTER(_Message)
        signatures = {
            "EnumThreadWindows": ([dword, self._enum_proc, ctypes.c_ssize_t], ctypes.c_int),
            "GetWindowThreadProcessId": ([hwnd, ctypes.POINTER(dword)], dword),
            "GetWindowTextW": ([hwnd, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([hwnd, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
            "GetClientRect": ([hwnd, ctypes.POINTER(_Rect)], ctypes.c_int),
            "IsWindow": ([hwnd], ctypes.c_int),
            "IsIconic": ([hwnd], ctypes.c_int),
            "ShowWindow": ([hwnd, ctypes.c_int], ctypes.c_int),
            "SetForegroundWindow": ([hwnd], ctypes.c_int),
            "PeekMessageW": ([msg, hwnd, uint, uint, uint], ctypes.c_int),
            "GetMessageW": ([msg, hwnd, uint, uint], ctypes.c_int),
            "TranslateMessage": ([msg], ctypes.c_int),
            "DispatchMessageW": ([msg], ctypes.c_ssize_t),
            "PostThreadMessageW": ([dword, uint, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_int),
            "RegisterHotKey": ([hwnd, ctypes.c_int, uint, uint], ctypes.c_int),
            "UnregisterHotKey": ([hwnd, ctypes.c_int], ctypes.c_int),
            "MsgWaitForMultipleObjectsEx": ([dword, ctypes.c_void_p, dword, dword, dword], dword),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        self.kernel.GetCurrentThreadId.argtypes = []
        self.kernel.GetCurrentThreadId.restype = dword

    def thread_id(self):
        return self.kernel.GetCurrentThreadId()

    def process_id(self):
        return os.getpid()

    def thread_windows(self, thread_id):
        windows = []

        def collect(hwnd, _unused):
            windows.append(hwnd)
            return True

        callback = self._enum_proc(collect)
        ctypes.set_last_error(0)
        if not self.user.EnumThreadWindows(thread_id, callback, 0):
            code = ctypes.get_last_error()
            if code:
                raise ctypes.WinError(code)
        return windows

    def identity(self, hwnd):
        pid = ctypes.c_uint32()
        tid = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title, cls = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(512)
        self.user.GetWindowTextW(hwnd, title, len(title))
        self.user.GetClassNameW(hwnd, cls, len(cls))
        return pid.value, tid, cls.value, title.value

    def valid(self, hwnd):
        return bool(self.user.IsWindow(hwnd))

    def minimized(self, hwnd):
        return bool(self.user.IsIconic(hwnd))

    def client_size(self, hwnd):
        rect = _Rect()
        if self.user.GetClientRect(hwnd, ctypes.byref(rect)):
            return rect.right - rect.left, rect.bottom - rect.top
        return None  # Unknown now; preserve the last valid size and retry next poll.

    def minimize(self, hwnd):
        self.user.ShowWindow(hwnd, 6)  # SW_MINIMIZE

    def restore(self, hwnd):
        self.user.ShowWindow(hwnd, 9)  # SW_RESTORE; retains the existing graphics object.
        self.user.SetForegroundWindow(hwnd)  # Windows may legitimately deny foreground focus.

    def peek(self):
        message = _Message()
        if self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            return message
        return None

    def ensure_queue(self):
        message = _Message()
        self.user.PeekMessageW(ctypes.byref(message), None, 0x0400, 0x0400, 0)

    def get_message(self):
        message = _Message()
        result = self.user.GetMessageW(ctypes.byref(message), None, 0, 0)
        if result == -1:
            raise ctypes.WinError(ctypes.get_last_error())
        return message if result else None

    def dispatch(self, message):
        self.user.TranslateMessage(ctypes.byref(message))
        self.user.DispatchMessageW(ctypes.byref(message))

    def post(self, thread_id, message):
        if not self.user.PostThreadMessageW(thread_id, message, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def register_hotkey(self):
        if not self.user.RegisterHotKey(None, _HOTKEY_ID, _HOTKEY_MODIFIERS, _HOTKEY_VK):
            raise ctypes.WinError(ctypes.get_last_error())

    def unregister_hotkey(self):
        if not self.user.UnregisterHotKey(None, _HOTKEY_ID):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout):
        # MWMO_INPUTAVAILABLE also wakes for messages previously seen by PeekMessage.
        # A frame deadline can be less than 1 ms away. Rounding down would turn
        # that positive wait into repeated nonblocking calls until the deadline.
        result = self.user.MsgWaitForMultipleObjectsEx(
            0, None, max(0, min(1000, math.ceil(timeout * 1000))), 0x04FF, 0x0004)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())


class _ResumeHotkey:
    def __init__(self, native, ui_thread):
        self.native = native
        self.ui_thread = ui_thread
        self.resume = threading.Event()
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.registered = False
        self.error = ""
        self.thread_id = None
        self.thread = threading.Thread(target=self._run, name="Druta-display-hotkey", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(2.0):
            self.error = "Restore hotkey registration timed out; use the taskbar to restore"
            self.close()

    def _run(self):
        try:
            self.native.ensure_queue()
            self.thread_id = self.native.thread_id()
            if self.stopping.is_set():
                return
            self.native.register_hotkey()
            self.registered = True
            self.ready.set()
            while not self.stopping.is_set():
                message = self.native.get_message()
                if message is None or message.message == _WM_QUIT:
                    break
                if (message.message == _WM_HOTKEY and message.wParam == _HOTKEY_ID
                        and message.lParam == (_HOTKEY_VK << 16) | _HOTKEY_MODIFIERS):
                    self.resume.set()
                    # Wake a paused UI's message wait, without operating its HWND here.
                    self.native.post(self.ui_thread, 0)
        except Exception as exc:
            self.error = f"{HOTKEY_LABEL} unavailable: {exc}; restore Druta from the taskbar"
        finally:
            if self.registered:
                try:
                    self.native.unregister_hotkey()
                except Exception as exc:
                    self.error = f"Restore hotkey cleanup failed: {exc}"
                self.registered = False
            self.ready.set()

    def close(self):
        self.stopping.set()
        if self.thread.is_alive() and self.thread_id is not None:
            try:
                self.native.post(self.thread_id, _WM_QUIT)
            except OSError:
                # A thread that exited between is_alive() and PostThreadMessage
                # has already unregistered the shortcut in its finally block.
                if self.thread.is_alive():
                    raise
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
            if self.thread.is_alive():
                raise RuntimeError("Restore hotkey thread did not exit")


class WindowIdle:
    """Pause by minimizing; resume via taskbar or the global restore shortcut.

    attach(), pause(), poll(), wait(), and close() belong to the viewport's UI
    thread. poll() returns whether the viewport is minimized. The caller decides
    whether that pauses drawing/monitoring or keeps an Always refresh override.
    It must also check closed and perform normal application cleanup on termination.

    activity_serial increases for owned input/focus messages consumed by this
    pump and for observed size/minimize transitions. DPG also pumps messages
    during rendering, so callers supplement it with their DPG input callbacks
    and active interaction state. It is an activity hint, not an input log.
    """

    @classmethod
    def attach(cls, title, *, _native=None):
        native = _Windows() if _native is None else _native
        thread_id, process_id = native.thread_id(), native.process_id()
        matches = []
        for hwnd in native.thread_windows(thread_id):
            pid, tid, window_class, window_title = native.identity(hwnd)
            # DPG 2.3.1 registers the initial viewport title as its class name.
            # https://github.com/hoffstadt/DearPyGui/blob/v2.3.1/src/mvViewport_win32.cpp#L320
            if (pid, tid, window_class, window_title) == (process_id, thread_id, title, title):
                matches.append(hwnd)
        if len(matches) != 1:
            raise RuntimeError(f"Cannot identify one owned Druta viewport (found {len(matches)})")
        return cls(native, matches[0], process_id, thread_id, title)

    def __init__(self, native, hwnd, process_id, thread_id, window_class):
        self._native = native
        self._hwnd = hwnd
        self._process_id = process_id
        self._thread_id = thread_id
        self._window_class = window_class
        self.closed = False
        self._released = False
        self.activity_serial = 0
        self._last_client_size = native.client_size(hwnd)
        self._last_minimized = native.minimized(hwnd)
        self._hotkey = _ResumeHotkey(native, thread_id)
        try:
            self._hotkey.start()
        except Exception:
            self._hotkey.close()
            raise

    @property
    def hotkey_available(self):
        return self._hotkey.registered and not self._hotkey.stopping.is_set()

    @property
    def hotkey_error(self):
        return self._hotkey.error

    def _check_thread(self):
        if self._native.thread_id() != self._thread_id:
            raise RuntimeError("Window pause must run on the viewport's UI thread")

    def _owned(self):
        if not self._native.valid(self._hwnd):
            return False
        pid, tid, window_class, _title = self._native.identity(self._hwnd)
        return (pid, tid, window_class) == (self._process_id, self._thread_id, self._window_class)

    @staticmethod
    def _input_message(kind):
        return (0x0100 <= kind <= 0x0109  # Key, character, system-key and Unicode input.
                or 0x0200 <= kind <= 0x020E  # Client mouse buttons, motion and wheels.
                or 0x00A0 <= kind <= 0x00AD  # Non-client mouse input, e.g. title bar.
                or kind in (0x0006, 0x0007, 0x0008, 0x001C,  # Activation/focus.
                            0x010D, 0x010E, 0x010F, 0x0286,  # IME composition/character.
                            0x0215,  # Capture change ends a drag even without button-up.
                            0x02A0, 0x02A1, 0x02A2, 0x02A3))  # Mouse hover/leave.

    def _observe_window_state(self):
        size = self._native.client_size(self._hwnd)
        minimized = self._native.minimized(self._hwnd)
        changed = minimized != self._last_minimized
        if size is not None and size != self._last_client_size:
            self._last_client_size = size
            changed = True
        self._last_minimized = minimized
        if changed:
            self.activity_serial += 1
        return minimized

    def pause(self):
        self._check_thread()
        if self.closed or self._released or not self._owned():
            self.closed = True
            raise RuntimeError("Druta viewport is no longer available")
        self._native.minimize(self._hwnd)
        return self._observe_window_state()

    def poll(self):
        self._check_thread()
        if self.closed or self._released:
            return False
        if not self._owned():
            self.closed = True
            return False
        # Bound work so a flood of posted messages cannot starve active-control
        # maintenance. Remaining messages make wait() return immediately.
        for _ in range(256):
            message = self._native.peek()
            if message is None:
                break
            if message.message == _WM_QUIT:
                self.closed = True
                return False
            if message.hwnd == self._hwnd and self._input_message(message.message):
                self.activity_serial += 1
            # DPG 2.3.1's move/resize timer directly renders in its WndProc.
            # KillTimer on WM_EXITSIZEMOVE does not remove an already queued
            # WM_TIMER. Drop only that exact owned-window, callback-free timer
            # while minimized; other windows/timers and visible drag stay intact.
            if (message.message == _WM_TIMER and message.hwnd == self._hwnd
                    and message.wParam == 1 and message.lParam == 0
                    and self._owned() and self._native.minimized(self._hwnd)):
                continue
            self._native.dispatch(message)
        if not self._owned():
            self.closed = True
            return False
        if self._hotkey.resume.is_set():
            self._hotkey.resume.clear()
            if self._native.minimized(self._hwnd):
                self._native.restore(self._hwnd)
        return self._observe_window_state()

    def wait(self, timeout=0.1):
        self._check_thread()
        if not self.closed and not self._released and not self._hotkey.resume.is_set():
            self._native.wait(timeout)

    def close(self):
        self._check_thread()
        if not self._released:
            self._hotkey.close()
            self._released = True
