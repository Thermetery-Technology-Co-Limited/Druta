# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shunt-mod corrected power, and the arithmetic behind it.

WHAT A SHUNT MOD DOES. A board measures rail current as the voltage across a
small sense resistor: V = I x R. The controller divides that voltage by the
resistance it was BUILT to expect, so it reports I_reported = V / R_nominal.
Lower the real resistance and the same current makes a smaller voltage, so the
card believes it is drawing less than it is - which is the point, because the
power limit is enforced on the believed number.

    R_effective = R_nominal / m   =>   the card under-reports by m
    real power  = reported power x m

STACKING IS PARALLEL, NOT SERIES. A resistor soldered on top of an existing
shunt bridges the same two pads, so the two are in PARALLEL and the resistance
FALLS: two equal 5 mOhm parts give 2.5 mOhm, i.e. m = 2. (In series the
resistance would rise and the card would over-report, which is the opposite of
what a shunt mod is for.) This module therefore takes the ORIGINAL and the
EFFECTIVE resistance and derives m from them, which is true however the change
was made - stacked, swapped, or several stacked at once.

WHAT THE DRIVER WILL NOT TELL US, and why it shapes everything here. NVML and
NVAPI report ONE total board figure plus a GPU/board split in percent. Probed
on a Titan RTX: NvAPI's power topology returns two domains (GPU, board) as
per-mille of the limit, NVML's POWER_AVERAGE answers only for scope 0/1 and
POWER_INSTANT is unsupported. There is no per-connector telemetry. So:

  * UNIFORM mod - every rail ends at the same multiplier - is EXACT. The whole
    board figure is scaled by one number, and how the load happens to divide
    between the slot and the connectors does not matter.

  * MIXED mod - rails at different multipliers - cannot be computed exactly,
    because it needs the per-rail split the driver does not expose. It is
    ESTIMATED here by weighting each rail by its rated capacity, and every
    caller is told which of the two it is looking at (see Correction.exact).

Refusing to show a mixed result at all was the alternative. Weighting by
capacity and labelling it an estimate is more useful and no less honest, as
long as the label never comes off - which is why `exact` is part of the return
value rather than a footnote in the UI.
"""
import json
import os

# Rated capacity per rail, in watts. Used ONLY to weight a mixed-multiplier
# estimate; a uniform mod never touches these. The PCIe slot figure is the
# 12 V allowance (66 W) rather than the 75 W headline, which includes 3.3 V
# the connectors do not carry.
RAIL_KINDS = {
    "slot":  ("PCIe slot", 66.0),
    "pin6":  ("6-pin", 75.0),
    "pin8":  ("8-pin", 150.0),
}
DEFAULT_MOHM = 5.0

# What a card gets before anyone says otherwise: the slot plus two 8-pins. It
# is a DEFAULT, not a detection - nothing in NVML or NVAPI enumerates the
# connectors, so this is the common layout and nothing more.
DEFAULT_RAILS = [
    {"kind": "slot", "orig": DEFAULT_MOHM, "mod": DEFAULT_MOHM},
    {"kind": "pin8", "orig": DEFAULT_MOHM, "mod": DEFAULT_MOHM},
    {"kind": "pin8", "orig": DEFAULT_MOHM, "mod": DEFAULT_MOHM},
]


def config_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Thermetery", "Druta", "shuntmod.json")


class Correction:
    """The answer, with its own provenance attached.

    `exact` is not decoration. A uniform mod scales one measured number and is
    as good as the driver's own reading; a mixed one rests on an assumed split
    between rails. A caller that prints the watts without printing which of
    those it has is publishing an estimate as a measurement, which is the
    failure this whole project is organised against."""

    def __init__(self, factor=1.0, exact=True, active=False, why=""):
        self.factor = factor
        self.exact = exact
        self.active = active        # is any rail actually modified?
        self.why = why

    def apply(self, watts):
        if watts is None:
            return None
        return watts * self.factor

    def __repr__(self):
        return (f"<Correction x{self.factor:.4g} "
                f"{'exact' if self.exact else 'estimated'} "
                f"{'active' if self.active else 'inactive'}>")


def rail_multiplier(rail):
    """R_original / R_effective for one rail, or 1.0 if it makes no sense.

    Guards rather than raises: these numbers come from a text box, and a
    half-typed "0." must not take the monitor down mid-keystroke."""
    try:
        orig = float(rail.get("orig") or 0.0)
        mod = float(rail.get("mod") or 0.0)
    except (TypeError, ValueError):
        return 1.0
    if orig <= 0.0 or mod <= 0.0:
        return 1.0
    return orig / mod


def correction(rails):
    """Fold a rail list into one multiplier for the board's total power."""
    rails = [r for r in (rails or []) if r.get("kind") in RAIL_KINDS]
    if not rails:
        return Correction(1.0, True, False, "no rails configured")

    mults = [rail_multiplier(r) for r in rails]
    active = any(abs(m - 1.0) > 1e-9 for m in mults)
    if not active:
        return Correction(1.0, True, False, "no rail is modified")

    first = mults[0]
    if all(abs(m - first) <= 1e-9 for m in mults):
        # EXACT. One multiplier over the whole board, so the split between
        # rails is irrelevant - which is the only reason this case can be
        # honest without per-rail telemetry.
        return Correction(first, True, True,
                          f"every rail x{first:.4g}")

    # MIXED. Needs the per-rail power the driver does not report, so weight by
    # what each rail is RATED to carry and say plainly that it is an estimate.
    caps = [RAIL_KINDS[r["kind"]][1] for r in rails]
    total_cap = sum(caps) or 1.0
    factor = sum(c * m for c, m in zip(caps, mults)) / total_cap
    return Correction(
        factor, False, True,
        "rails differ, so this is weighted by rated capacity "
        f"({'+'.join(f'{c:.0f}W' for c in caps)}) - the driver reports no "
        f"per-rail power to weight it by measurement")


def describe(rails):
    """One line per rail, for the UI to print without recomputing anything."""
    out = []
    for r in rails or []:
        label = RAIL_KINDS.get(r.get("kind"), ("?", 0.0))[0]
        m = rail_multiplier(r)
        out.append(f"{label}: {r.get('orig')} -> {r.get('mod')} mOhm  "
                   f"= x{m:.4g}")
    return out


def load():
    """Saved rails, or the defaults. Never raises - a corrupt file costs the
    saved layout, not the monitor."""
    try:
        with open(config_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        rails = data.get("rails")
        if isinstance(rails, list) and rails:
            return [r for r in rails if r.get("kind") in RAIL_KINDS]
    except (OSError, ValueError, AttributeError):
        pass
    return [dict(r) for r in DEFAULT_RAILS]


def save(rails):
    """(ok, message). Written through a temp file so an interrupted write
    cannot leave a truncated config where a good one was."""
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rails": rails}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        return False, f"could not save the shunt config: {e}"
    return True, f"shunt config saved ({len(rails)} rail(s))"
