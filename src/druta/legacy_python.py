# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Language shims for the Windows 7 and Vista builds' CPython 3.8 runtime.

CPython 3.9 and newer import Windows 8+ API sets, so the legacy bundles stay
on 3.8 while main targets current CPython. Only behavior that cannot be
written portably at each call site without diverging from main belongs here;
``tools/py38_compat.py`` reports everything else for a source fix.
"""
import builtins
import sys

_builtin_zip = builtins.zip


class _StrictZip:
    """``zip`` with Python 3.10's ``strict`` keyword.

    ``strict=True`` raises ValueError when the iterables have different
    lengths, with the same message shape as CPython 3.10.
    """

    def __init__(self, *iterables, strict=False):
        self._iterators = [iter(iterable) for iterable in iterables]
        self._strict = bool(strict)
        self._done = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._done or not self._iterators:
            raise StopIteration
        items = []
        for position, iterator in enumerate(self._iterators):
            try:
                items.append(next(iterator))
            except StopIteration:
                self._done = True
                if self._strict:
                    if position:
                        plural = " " if position == 1 else "s 1-"
                        raise ValueError(f"zip() argument {position + 1} is shorter "
                                         f"than argument{plural}{position}") from None
                    for later, rest in enumerate(self._iterators[1:], start=2):
                        try:
                            next(rest)
                        except StopIteration:
                            continue
                        plural = " " if later == 2 else "s 1-"
                        raise ValueError(f"zip() argument {later} is longer "
                                         f"than argument{plural}{later - 1}") from None
                raise
        return tuple(items)


def install(version_info=None):
    """Install the shims needed by this interpreter; return their names."""
    version_info = sys.version_info if version_info is None else version_info
    installed = []
    if tuple(version_info[:2]) < (3, 10) and builtins.zip is _builtin_zip:
        builtins.zip = _StrictZip
        installed.append("zip(strict=)")
    return installed


def add_note(exc, note):
    """``exc.add_note(note)`` with a Python < 3.11 fallback.

    Cancellation handlers attach rollback details to a re-raised
    KeyboardInterrupt or SystemExit. On 3.8 the missing method would raise
    AttributeError inside the handler and replace the user's cancellation.
    The fallback keeps PEP 678's ``__notes__`` list for traceback readers.
    """
    method = getattr(exc, "add_note", None)
    if method is not None:
        method(note)
        return
    notes = getattr(exc, "__notes__", None)
    if not isinstance(notes, list):
        notes = []
        exc.__notes__ = notes
    notes.append(str(note))
