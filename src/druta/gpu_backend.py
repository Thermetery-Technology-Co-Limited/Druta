# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which backend a Druta process talks to the card through: ``nvbackend.GPU`` or ``native.Gpu``.

This module is the one place a backend object is to be constructed (native/DESIGN-FRONTEND.md,
section (a)). The kind is decided once per process, under a module lock, at the first
:func:`open_gpu` or :func:`enumerate_gpus`, and is then pinned, so no process holds objects of
both kinds.

The request, highest precedence first:

1. the ``kind=`` argument of :func:`open_gpu`;
2. ``--backend X`` or ``--backend=X`` on the command line, which :func:`consume_argv` strips and
   applies by setting ``DRUTA_BACKEND``;
3. the ``DRUTA_BACKEND`` environment variable (``auto``, ``native`` or ``python``);
4. a process started with ``--startup-profile`` (the unattended sign-in apply) asks for ``python``;
5. the default, ``python``.

What each kind does:

- ``python``: ``nvbackend.GPU(slot)``. The native library is never loaded.
- ``native`` (forced): ``native.load()``, then the surface gate must report nothing missing, then
  ``native.Gpu(slot)``. Any failure raises :class:`BackendRefused` with the reason. It never falls
  back: a forced kind that silently changes is useless for diagnosis.
- ``auto``: native only when the library loads, the surface gate reports nothing missing and
  ``native.Gpu(slot)`` constructs. A construction failure counts only when it is a ``ValueError``,
  ``OSError``, ``MemoryError`` or ``NativeUnavailable``; any other exception is a bug and
  propagates, as do ``KeyboardInterrupt`` and the other ``BaseException``\\ s. A native object that
  opens but reports ``available()`` False stays native, and :func:`describe` says how to choose the
  Python backend instead.

When :func:`enumerate_gpus` is the first call, the auto decision's third step is the native
enumeration itself (with the same exception set) rather than a card construction, so that listing
the cards opens no handle.

After the pin, every later call uses the pinned kind and its errors propagate. A later ``kind=``
that is neither the pinned kind nor the request that produced it raises :class:`BackendMismatch`.
A refusal pins nothing: :func:`describe` reports it, and the next call decides again.

Decisions use only load success, export presence, ABI equality, the surface gate and the
construction and availability results. They never use a driver version, a device or subsystem
ID or a VBIOS string. Nothing is written to disk: the choice is process configuration, not
tuning state, and child processes inherit it through the environment.

The surface gate
----------------
:data:`surface_gate` is a module-level callable, ``surface_gate(native_module) -> sequence of
str``, naming the members the front ends need that ``native.Gpu`` lacks; empty means complete.
The front-end surface check (``native_frontend.missing_members()``) does not exist yet, so the
default, :func:`surface_not_provided`, reports the surface as incomplete with a reason saying so:
``auto`` resolves to Python and a forced ``native`` is refused. The increment that adds
``native_frontend`` points the hook at it by replacing the default assignment, for example::

    def _native_frontend_gate(native_module):
        from . import native_frontend
        return native_frontend.missing_members()

    surface_gate = _native_frontend_gate

Tests replace it with ``monkeypatch.setattr(gpu_backend, "surface_gate", ...)``.

Test support
------------
:func:`reset_for_tests` clears the pin, the last refusal and the record of ``--backend``. It exists
only for tests; application code must never call it, because un-pinning a running process is
exactly what the pin forbids.
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass

ENV = "DRUTA_BACKEND"
KINDS = ("auto", "native", "python")

_DEFAULT = "python"  # becomes "auto" only after the hardware sign-off (design, increment G4k)
_STARTUP_PROFILE_FLAG = "--startup-profile"
_FLAG = "--backend"
_UNKNOWN = "DRUTA_BACKEND must be auto, native or python"
_GATE_LIST_LIMIT = 12


class BackendRefused(RuntimeError):
    """A forced ``native`` that cannot be honoured; the message is the reason."""


class BackendMismatch(RuntimeError):
    """A second, different backend kind was asked for in a process whose kind is already pinned."""


@dataclass(frozen=True)
class Choice:
    """The pinned decision.

    kind       "native" or "python": what the process uses.
    requested  "auto", "native" or "python": what was asked for.
    source     where the request came from: "argument", "--backend", "DRUTA_BACKEND",
               "--startup-profile" or "default".
    library    the native library's path when kind is "native", else None.
    abi        the native ABI version when kind is "native", else None.
    reason     why auto chose Python, or why the native object is not usable; None otherwise.
    """

    kind: str
    requested: str
    source: str
    library: str | None
    abi: int | None
    reason: str | None


@dataclass(frozen=True)
class _Refusal:
    requested: str
    source: str
    reason: str


def surface_not_provided(native_module):
    """The surface gate until the front-end surface check exists: the surface counts as incomplete."""
    return ("front-end surface check not provided yet (native_frontend.missing_members)",)


# The surface gate hook: see the module docstring. Replace it, never call native_frontend directly.
surface_gate = surface_not_provided

_lock = threading.Lock()
_choice: Choice | None = None
_refusal: _Refusal | None = None
_argv_value: str | None = None  # the value consume_argv put in the environment, to name its source


def _native_module():
    from . import native
    return native


def _python_module():
    from . import nvbackend
    return nvbackend


def _normalize(value):
    kind = str(value).strip().lower()
    if kind not in KINDS:
        raise ValueError(_UNKNOWN)
    return kind


def consume_argv(argv):
    """Strip every ``--backend X`` / ``--backend=X`` from ``argv`` in place and apply the last one.

    The value goes into ``os.environ[DRUTA_BACKEND]``, so argument parsers that run later and child
    processes see it. Returns ``argv`` itself, so ``consume_argv(sys.argv)`` and
    ``argv = consume_argv(argv)`` both work. A missing or unknown value raises ``ValueError`` and
    leaves both ``argv`` and the environment unchanged.
    """
    global _argv_value
    kept, value, index = [], None, 0
    while index < len(argv):
        item = argv[index]
        if item == _FLAG:
            if index + 1 >= len(argv):
                raise ValueError("--backend needs a value: auto, native or python")
            value = _normalize(argv[index + 1])
            index += 2
            continue
        if isinstance(item, str) and item.startswith(_FLAG + "="):
            text = item[len(_FLAG) + 1:]
            if not text:
                raise ValueError("--backend needs a value: auto, native or python")
            value = _normalize(text)
            index += 1
            continue
        kept.append(item)
        index += 1
    if value is not None:
        argv[:] = kept
        os.environ[ENV] = value
        _argv_value = value
    return argv


def _started_with_startup_profile():
    return _STARTUP_PROFILE_FLAG in sys.argv[1:]


def _request(kind):
    """(requested, source) by the precedence in the module docstring; ValueError on an unknown value."""
    if kind is not None:
        return _normalize(kind), "argument"
    text = os.environ.get(ENV, "").strip()
    if text:
        requested = _normalize(text)
        return requested, ("--backend" if _argv_value is not None and requested == _argv_value else ENV)
    if _started_with_startup_profile():
        return "python", _STARTUP_PROFILE_FLAG
    return _DEFAULT, "default"


def request():
    """``(requested, source)`` for a call with no ``kind=``, by the precedence in the module docstring.

    For callers that must validate the request without constructing or enumerating anything. Raises
    the same ``ValueError`` as :func:`open_gpu` for an unknown ``DRUTA_BACKEND`` value. Pins nothing.
    """
    return _request(None)


def _gate_reason(missing):
    names = [str(name) for name in missing]
    shown = ", ".join(names[:_GATE_LIST_LIMIT])
    if len(names) > _GATE_LIST_LIMIT:
        shown += f" and {len(names) - _GATE_LIST_LIMIT} more"
    return "native surface incomplete: " + shown


def _native_ready():
    """(native module, library, None) when the library loads and the gate passes, else (None, None, reason).

    ``NativeUnavailable`` and ``OSError`` from ``load()`` are reasons (the loader can raise a raw
    ``OSError`` while it examines a candidate path); anything else propagates.
    """
    native = _native_module()
    try:
        library = native.load()
    except native.NativeUnavailable as exc:
        return None, None, str(exc) or "native core not loaded"
    except OSError as exc:
        return None, None, f"native core not loaded: {type(exc).__name__}: {exc}"
    missing = list(surface_gate(native))
    if missing:
        return None, None, _gate_reason(missing)
    return native, library, None


def _open_failures(native):
    return (ValueError, OSError, MemoryError, native.NativeUnavailable)


_UNAVAILABLE = "the native core opened the card but reports it unavailable"


def _native_choice(native, library, requested, source, card):
    """The native Choice; ``card`` is the constructed object, or None when the first call only enumerated."""
    reason = _UNAVAILABLE if card is not None and not card.available() else None
    return Choice("native", requested, source, getattr(library, "path", None),
                  getattr(native, "ABI_VERSION", None), reason)


def _decide(kind, construct, opens_card):
    """Decide and pin under ``_lock``; ``construct(module, which)`` does the first call's work.

    Returns that work's result. ``opens_card`` says the result is a backend object whose
    ``available()`` is recorded in the Choice.
    """
    global _choice, _refusal
    requested, source = _request(kind)
    if requested == "python":
        result = construct(_python_module(), "python")
        _choice, _refusal = Choice("python", requested, source, None, None, None), None
        return result
    native, library, reason = _native_ready()
    if reason is None:
        try:
            result = construct(native, "native")
        except _open_failures(native) as exc:
            if requested == "native":
                _refusal = _Refusal(requested, source, f"native open failed: {type(exc).__name__}: {exc}")
                raise BackendRefused(_refusal.reason) from exc
            reason = f"native open failed: {type(exc).__name__}: {exc}"
        else:
            _choice = _native_choice(native, library, requested, source, result if opens_card else None)
            _refusal = None
            return result
    if requested == "native":
        _refusal = _Refusal(requested, source, reason)
        raise BackendRefused(reason)
    result = construct(_python_module(), "python")
    _choice, _refusal = Choice("python", requested, source, None, None, reason), None
    return result


def _check_pin(pinned, kind):
    if kind is None:
        return
    wanted = _normalize(kind)
    if wanted not in (pinned.kind, pinned.requested):
        raise BackendMismatch(f"this process already uses the {_label(pinned.kind)} backend "
                              f"(requested {pinned.requested} via {pinned.source}); cannot switch to {wanted}")


def _pinned_module(pinned):
    return _native_module() if pinned.kind == "native" else _python_module()


def _construct(module, which, slot):
    return module.Gpu(slot) if which == "native" else module.GPU(slot)


def open_gpu(slot=None, *, kind=None):
    """A backend object for ``slot`` (``None`` for the default card) of the process's kind.

    Returns ``nvbackend.GPU`` or ``native.Gpu``. Raises ``BackendRefused`` for a forced native that
    cannot be honoured, ``BackendMismatch`` for a different kind after the pin, and ``ValueError``
    for an unknown kind.
    """
    pinned = _choice
    if pinned is None:
        with _lock:
            pinned = _choice
            if pinned is None:
                return _decide(kind, lambda module, which: _construct(module, which, slot), True)
    _check_pin(pinned, kind)
    return _construct(_pinned_module(pinned), pinned.kind, slot)


def enumerate_gpus():
    """The pinned kind's ``enumerate_gpus()`` (same keys and order on both); decides the kind first if needed."""
    pinned = _choice
    if pinned is None:
        with _lock:
            pinned = _choice
            if pinned is None:
                return _decide(None, lambda module, which: module.enumerate_gpus(), False)
    return _pinned_module(pinned).enumerate_gpus()


def choice():
    """The pinned :class:`Choice`, or None while nothing is pinned."""
    return _choice


def _label(kind):
    return "native core" if kind == "native" else "Python"


def describe():
    """The one log line about the backend, for the UI log, ``--version`` and About."""
    pinned, refusal = _choice, _refusal
    if pinned is not None:
        via = f"requested {pinned.requested} via {pinned.source}"
        if pinned.kind == "native":
            where = pinned.library or "library path unknown"
            abi = "ABI ?" if pinned.abi is None else f"ABI {pinned.abi}"
            line = f"backend: native core ({where}, {abi}; {via})"
            if pinned.reason:
                line += f"; {pinned.reason}: start with --backend python to use the Python backend"
            return line
        if pinned.reason:
            return f"backend: Python ({via}; {pinned.reason})"
        return f"backend: Python ({via})"
    if refusal is not None:
        return f"backend: native refused (requested {refusal.requested} via {refusal.source}): {refusal.reason}"
    try:
        requested, source = _request(None)
    except ValueError as exc:
        return f"backend: not chosen ({exc})"
    return f"backend: not chosen yet (requested {requested} via {source})"


def reset_for_tests():
    """TEST-ONLY: forget the pin, the last refusal and the ``--backend`` record. Never call from the app."""
    global _choice, _refusal, _argv_value
    with _lock:
        _choice, _refusal, _argv_value = None, None, None
