# Druta - names the user gives the power policies, remembered per card.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""The driver's policy table has numbers, channels and record types, not names,
and which rail a channel is differs per board. The user names them beside the
sliders. Names are kept per card (by UUID) so a fresh session or a new profile
starts with them, and profiles carry their own copy (profiles.capture).

Each name keeps the channel and record type it was given against, and is only
shown for a live policy that still has both: a VBIOS flash that renumbers the
table must not put one limit's name on another."""

import json
import os
from pathlib import Path

NAME_MAX = 80
# The note a channel can be given from the list, and how many 12 V wires share
# a connector's current: the per-wire colour bands in the UI divide its reading
# by this, assuming an even split. None: not a cable - the slot's current comes
# through board pins and is judged as a whole (the UI's slot bands). Any other
# note is free text.
CONNECTORS = {"PCIE 8pin": 3, "PCIE": None, "EPS 8pin": 4, "12VHPWR/12V-2x6": 6}
OTHER = "others"


def store_path(root=None):
    base = Path(root) if root else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Druta"
    return base / "power-policy-names.json"


def _read(root=None, strict=False):
    """The whole store; {} when there is none. strict raises for a store that
    exists but cannot be read or parsed, so a save does not replace it."""
    path = store_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError("the name store is not a mapping")
        return {}
    return data


def clean(names):
    """{policy: {"name", "channel", "type"}} with anything malformed dropped."""
    out = {}
    for key, entry in (names or {}).items():
        try:
            policy = int(key)
        except (TypeError, ValueError):
            continue
        if (0 <= policy < 32 and isinstance(entry, dict) and isinstance(entry.get("name"), str)
                and entry["name"].strip() and type(entry.get("channel")) is int
                and type(entry.get("type")) is int):
            out[policy] = {"name": entry["name"].strip()[:NAME_MAX],
                           "channel": entry["channel"], "type": entry["type"]}
    return out


def wires(note):
    """12 V wires sharing the current of a channel with this note, or None."""
    return CONNECTORS.get(note)


def load(uuid, root=None):
    """This card's names, or {} when there are none or the store is unreadable."""
    if not uuid:
        return {}
    card = _read(root).get(str(uuid))
    return clean(card) if isinstance(card, dict) else {}


def save(uuid, names, rows=None, root=None):
    """Store this card's names. (ok, message); the write is atomic.

    rows: the live table the names were edited against. A stored name the
    live table does not show - its policy is gone, or its channel or type
    differ - was not visible to edit, so it is kept rather than dropped.
    A store that exists but cannot be read is set aside, not replaced."""
    if not uuid:
        return False, "this card reports no UUID to remember names by"
    path = store_path(root)
    note = ""
    try:
        data = _read(root, strict=True)
    except (OSError, ValueError) as exc:
        aside = path.with_name(f"{path.name}.unreadable-{os.getpid()}")
        try:
            os.replace(path, aside)
        except OSError as move_error:
            return False, f"power-policy names not saved: the store is unreadable ({exc}) " \
                          f"and could not be set aside ({move_error})"
        data, note = {}, f"; the unreadable store was kept as {aside.name}"
    live = {row["policy"]: row for row in rows or ()}
    stored = clean(data.get(str(uuid)) if isinstance(data.get(str(uuid)), dict) else {})
    hidden = {p: e for p, e in stored.items()
              if rows is not None and not (p in live and live[p]["channel"] == e["channel"]
                                           and live[p]["type"] == e["type"])}
    data[str(uuid)] = {str(p): e for p, e in sorted({**hidden, **clean(names)}.items())}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return False, f"power-policy names not saved: {exc}"
    return True, "power-policy names saved" + note


def matching(names, rows):
    """The names whose channel and record type still match the live policy."""
    live = {row["policy"]: row for row in rows}
    return {p: e for p, e in clean(names).items()
            if p in live and live[p]["channel"] == e["channel"] and live[p]["type"] == e["type"]}
