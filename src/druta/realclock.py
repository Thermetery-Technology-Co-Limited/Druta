# Druta - the measured clock beside the programmed one.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Whether this card's second private clock array (B) may be shown as a
measured clock, and how far it runs from the programmed one (A).

The Monitor tiles quote array A, the programmed target - what GPU-Z and NVML
show. On one TITAN RTX, B was established as a physical counter (it never lands
on the programming grid, jitters, refreshes about once a second, and read
1950 MHz under load while A, NVML and every tile read 1965). On GK104/GM107 the
two arrays were identical in every tested state, so B proves nothing there. No
card or generation list decides which it is: B earns "measured" on the card in
front of us, from its own readings, and loses it again if it starts copying A.

Pure: no DearPyGui, no driver. The constants are statistical policy (how many
readings make evidence, how much load makes a reading comparable), not hardware
values."""

from collections import deque
from dataclasses import dataclass

LOAD_GATE_PCT = 90      # below this the clock gates between bursts and B reads low
WINDOW_READS = 5        # readings in one judged window
FRESH_NEEDED = 2        # B refreshes since A last changed before a reading counts
EVIDENCE_EVENTS = 2     # independent B changes that earn "measured"
DECIDE_READS = 5        # readings before "checking" becomes "unproven"
MIRROR_RUN = 5          # exact B == A readings, across a change of A, that withdraw it
RECENT_A = 16           # A values remembered, so a lagged copy of A is no evidence
READ_GAP_S = 3.0        # a longer gap between readings breaks continuity
GAP_TOL_MHZ = 0.5       # a gap this close to a band edge counts as reaching it

MEASURED, CHECKING, MIRROR, UNPROVEN = "measured", "checking", "mirror", "unproven"


def gap_band(gap_mhz, bin_mhz):
    """'ok', 'warn' (one clock bin or more) or 'bad' (three or more), either
    sign, with GAP_TOL_MHZ of tolerance: a counter reading one bin off lands a
    hair short of it (TU102 at a 1350 lock read +14.9 on a 15 MHz grid)."""
    if gap_mhz is None or not bin_mhz or bin_mhz <= 0:
        return "ok"
    g = abs(gap_mhz)
    if g >= 3 * bin_mhz - GAP_TOL_MHZ:
        return "bad"
    if g >= bin_mhz - GAP_TOL_MHZ:
        return "warn"
    return "ok"


def window_verdict(deltas, bin_mhz):
    """One verdict for a window of gaps: 'ok' when every one is inside a bin;
    the band of the SMALLEST gap when every gap is past a bin and on the same
    side; otherwise 'varying' - a window that straddles a band edge is not
    coloured, so a jittering counter does not flicker between colours."""
    deltas = [d for d in deltas if d is not None]
    if not deltas:
        return None
    mags = [abs(d) for d in deltas]
    if gap_band(max(mags), bin_mhz) == "ok":
        return "ok"
    low = gap_band(min(mags), bin_mhz)
    if low != "ok" and (all(d < 0 for d in deltas) or all(d > 0 for d in deltas)):
        return low
    return "varying"


@dataclass(frozen=True)
class Reading:
    """One tile's view of one reading. a/b/delta_mhz are in the TILE's units
    (a 2CLK row divided by its scale)."""
    key: str
    dom: int
    name: str
    scale: int
    trust: str
    events: int
    samples: int
    mirror_run: int
    fresh: int
    a_khz: int
    b_khz: int
    a_mhz: float
    b_mhz: float
    delta_mhz: float
    util: object
    paired: bool
    state: str
    verdict: object
    window: tuple
    window_util: object


class _Evidence:
    """What one domain's B has shown, as seen through one tile."""

    def __init__(self):
        self.samples = 0
        self.events = 0
        self.fresh = 0
        self.mirror_run = 0
        self.mirror_as = set()      # A values seen during the current B == A run
        self.prev = None            # (a_khz, b_khz) of the previous reading
        self.recent_a = deque(maxlen=RECENT_A)


class _Key:
    """Per-tile continuity: the domain it follows, its window, the last load."""

    def __init__(self):
        self.dom = None
        self.window = []
        self.prev_util = None
        self.last_t = None
        self.last = None


class ClockEvidence:
    """Readings fed one per DISTINCT snapshot (the caller must not feed a
    snapshot it has already fed). Evidence and window are keyed by tile and
    domain, so two tiles never share a counter."""

    def __init__(self):
        self._evidence = {}
        self._keys = {}

    def _key(self, key):
        k = self._keys.get(key)
        if k is None:
            k = self._keys[key] = _Key()
        return k

    def last(self, key):
        k = self._keys.get(key)
        return k.last if k else None

    def miss(self, key):
        """No usable reading for this tile now: forget the last one and break
        continuity, but keep what B has shown so far."""
        k = self._key(key)
        k.last = None
        k.window = []
        k.prev_util = None
        ev = self._evidence.get((key, k.dom))
        if ev is not None:
            ev.prev = None
            ev.fresh = 0

    def feed(self, key, dom, name, a_khz, b_khz, scale, util, paired, now, bin_mhz,
             graded=True):
        """One reading of this tile's row. Returns a Reading, or None when
        either array word is zero (a zero word is never evidence)."""
        if not (isinstance(a_khz, int) and isinstance(b_khz, int) and a_khz > 0 and b_khz > 0):
            self.miss(key)
            return None
        scale = scale if isinstance(scale, int) and scale > 0 else 1
        k = self._key(key)
        if k.dom != dom:
            k.dom, k.window, k.prev_util, k.last_t = dom, [], None, None
        ev = self._evidence.get((key, dom))
        if ev is None:
            ev = self._evidence[(key, dom)] = _Evidence()
        if k.last_t is not None and now - k.last_t > READ_GAP_S:
            ev.prev, ev.fresh, k.window, k.prev_util = None, 0, [], None
        k.last_t = now

        ev.samples += 1
        if b_khz == a_khz:
            ev.mirror_run += 1
            ev.mirror_as.add(a_khz)
        else:
            ev.mirror_run, ev.mirror_as = 0, set()
        if ev.prev is None:
            ev.fresh = 0
        else:
            prev_a, prev_b = ev.prev
            if a_khz == prev_a and b_khz != prev_b:
                ev.fresh += 1
                # B moved on its own while A held still, to a value A never
                # reported: not a copy of A, not a copy lagging one read
                if b_khz != a_khz and b_khz not in ev.recent_a:
                    ev.events += 1
            elif a_khz != prev_a:
                ev.fresh = 0
        if not ev.recent_a or ev.recent_a[-1] != a_khz:
            ev.recent_a.append(a_khz)
        ev.prev = (a_khz, b_khz)

        if ev.mirror_run >= MIRROR_RUN and len(ev.mirror_as) >= 2:
            trust = MIRROR          # B followed A exactly through a change of A
        elif ev.events >= EVIDENCE_EVENTS:
            trust = MEASURED
        elif ev.samples < DECIDE_READS:
            trust = CHECKING
        else:
            trust = UNPROVEN

        delta = (b_khz - a_khz) / 1000.0 / scale
        if trust != MEASURED:
            state = trust
        elif not graded:
            state = "ungraded"
        elif util is None:
            state = "load unread"
        elif util < LOAD_GATE_PCT:
            state = "light"
        elif k.prev_util is None or k.prev_util < LOAD_GATE_PCT:
            state = "load start"
        elif not paired:
            state = "unpaired"
        elif ev.fresh < FRESH_NEEDED:
            state = "settling"
        else:
            state = "window"
        if state == "window":
            k.window = (k.window + [(a_khz, delta, util)])[-WINDOW_READS:]
            state = "judged" if len(k.window) >= WINDOW_READS else "settling"
        else:
            k.window = []
        k.prev_util = util

        deltas = tuple(d for _a, d, _u in k.window)
        utils = [u for _a, _d, u in k.window]
        reading = Reading(
            key=key, dom=dom, name=name or "", scale=scale, trust=trust,
            events=ev.events, samples=ev.samples, mirror_run=ev.mirror_run,
            fresh=ev.fresh, a_khz=a_khz, b_khz=b_khz,
            a_mhz=a_khz / 1000.0 / scale, b_mhz=b_khz / 1000.0 / scale,
            delta_mhz=delta, util=util, paired=bool(paired), state=state,
            verdict=window_verdict(deltas, bin_mhz) if state == "judged" else None,
            window=deltas if state == "judged" else (),
            window_util=(min(utils), max(utils)) if state == "judged" and utils else None)
        k.last = reading
        return reading
