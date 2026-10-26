# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure three-way undo of a journaled registry transaction.

The transaction owns only names it changed, including creation/deletion. Values
on those names may be in either recorded state after an interrupted sequence of
writes. An unrelated writer's changes are retained. Conflicting owned values
require an explicit resolution; this function never guesses or mutates inputs.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping


class RecoveryConflict(ValueError):
    """At least one transaction-owned name has a third, unrecognized value."""

    def __init__(self, conflicts):
        self.conflicts = tuple(conflicts)
        super().__init__("Registry recovery conflicts with outside changes to: " + ", ".join(self.conflicts))


_MISSING = object()


def _names(values, label):
    if not isinstance(values, Mapping):
        raise ValueError(f"{label} registry state must be a mapping")
    result = {}
    for name in values:
        if not isinstance(name, str):
            raise ValueError(f"{label} registry state contains a non-string value name")
        key = name.casefold()
        if key in result:
            raise ValueError(f"{label} registry state contains duplicate case-insensitive name {name}")
        result[key] = name
    return result


def restore_state(before, after, current):
    """Return current with the transaction's changed names restored to before.

    `before`, `after`, and `current` map registry value names to encoded values
    (normally {"type": REG_*, "value": ...}). Missing keys represent absence.
    Values must compare exactly as captured; their encoding is not normalized.
    Windows value names compare case-insensitively. Existing current spelling
    is retained so a consumer cannot accidentally delete a newly restored name
    through a differently-cased alias.
    """
    before_names = _names(before, "Before")
    after_names = _names(after, "After")
    current_names = _names(current, "Current")
    changes, conflicts = [], []
    for key in sorted(before_names.keys() | after_names.keys()):
        old_name, new_name = before_names.get(key), after_names.get(key)
        old = before[old_name] if old_name is not None else _MISSING
        new = after[new_name] if new_name is not None else _MISSING
        if old == new:
            continue
        actual_name = current_names.get(key)
        actual = current[actual_name] if actual_name is not None else _MISSING
        if actual != old and actual != new:
            conflicts.append(actual_name or old_name or new_name)
            continue
        changes.append((actual_name, old_name, old))
    if conflicts:
        raise RecoveryConflict(conflicts)
    restored = copy.deepcopy(dict(current))
    for actual_name, old_name, old in changes:
        if old is _MISSING:
            if actual_name is not None:
                del restored[actual_name]
        else:
            restored[actual_name if actual_name is not None else old_name] = copy.deepcopy(old)
    return restored
