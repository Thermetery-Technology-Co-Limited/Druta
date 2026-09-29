# Druta - the measured clock beside the programmed one.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Whether this card's second private clock array (B) may be shown as a
measured clock, and how far it runs from the programmed one (A).

The Monitor tiles quote array A, the programmed target. On one TITAN RTX, B
behaved as a physical counter: it never landed on the programming grid,
jittered, refreshed about once a second, and read 1950 MHz under load while A,
NVML and every tile read 1965. On GK104/GM107 the two arrays were identical in
every tested state, so B proves nothing there. No card or generation list
decides which it is: B earns "measured" on the card in front of us, from its own
readings, and a reading that copies A, stops moving or has not settled is never
judged.

Pure: no DearPyGui, no driver. The constants are statistical POLICY - how many
readings make evidence, how much load makes a reading comparable - chosen, not
measured."""

import statistics
from collections import deque
from dataclasses import dataclass

LOAD_GATE_PCT = 90      # a chosen threshold: judged only at this load or more ...
LOAD_EXIT_PCT = 85      # ... and until the load drops below this (no flicker at 89/91)
WINDOW_READS = 5        # readings before a verdict, and the span checked for a transient
MEDIAN_READS = 15       # readings under one A that the judged median spans at most
FRESH_NEEDED = 2        # B refreshes since A last changed before a reading counts
EVIDENCE_EVENTS = 2     # turns of B on its own, while A holds, that earn "measured"
DECIDE_READS = 5        # readings before "checking" becomes a verdict about B
MIRROR_RUN = 5          # exact B == A readings, across a change of A, that withdraw it
STUCK_READS = 5         # readings with B unchanged while A held: B is not refreshing
MOVING_READS = 10       # A changed twice within this many readings: the target is moving
RECENT_A = 16           # A values remembered, so a lagged copy of A is no evidence
READ_GAP_S = 3.0        # a longer gap between readings breaks continuity
GAP_TOL_MHZ = 0.5       # a gap this close to a band edge counts as reaching it

MEASURED, CHECKING, MIRROR, SAME, UNPROVEN = (
    "measured", "checking", "mirror", "same", "unproven")


def _usable(bin_mhz):
    return isinstance(bin_mhz, (int, float)) and bin_mhz == bin_mhz and bin_mhz > 0


def gap_band(gap_mhz, bin_mhz):
    """'ok', 'warn' (one clock bin or more) or 'bad' (three or more), either
    sign, with GAP_TOL_MHZ of tolerance: a counter reading one bin off lands a
    hair short of it (TU102 at a 1350 lock read +14.9 on a 15 MHz grid). None
    when either figure is unusable - no band is claimed without a bin."""
    if gap_mhz is None or not _usable(bin_mhz):
        return None
    g = abs(gap_mhz)
    if g >= 3 * bin_mhz - GAP_TOL_MHZ:
        return "bad"
    if g >= bin_mhz - GAP_TOL_MHZ:
        return "warn"
    return "ok"


def median_hold(deltas):
    """How far past a band edge the median of `deltas` must fall before a
    band once reached is released: the tolerance plus three standard errors
    of that median, estimated from the readings' own scatter (1.86 x MAD /
    sqrt(n) for a median). A policy, not a measurement: a counter jittering
    +-2.2 MHz adds about 1.6 MHz to the tolerance over 15 readings, a quiet
    one about nothing, and one outlier does not widen it."""
    deltas = [d for d in deltas if d is not None]
    if not deltas:
        return GAP_TOL_MHZ
    median = statistics.median(deltas)
    mad = statistics.median(abs(d - median) for d in deltas)
    return GAP_TOL_MHZ + 3 * 1.86 * mad / len(deltas) ** 0.5


def window_verdict(deltas, bin_mhz, previous=None):
    """One verdict for a window of gaps, oldest first.

    'varying' when the last WINDOW_READS of them spread over more than one
    bin (a transient or an outlier); otherwise the band of the MEDIAN of them
    all, with hysteresis against `previous`: a band once reached is kept until
    the median has fallen past its edge by median_hold(), so a gap that sits
    on an edge does not flicker between colours. None without a usable bin."""
    deltas = [d for d in deltas if d is not None]
    if not deltas or not _usable(bin_mhz):
        return None
    recent = deltas[-WINDOW_READS:]
    if max(recent) - min(recent) > bin_mhz:
        return "varying"
    median = statistics.median(deltas)
    band = gap_band(median, bin_mhz)
    g = abs(median)
    hold = median_hold(deltas)
    if previous == "bad" and band == "warn" and g >= 3 * bin_mhz - GAP_TOL_MHZ - hold:
        band = "bad"
    elif previous in ("warn", "bad") and band == "ok" and g >= bin_mhz - GAP_TOL_MHZ - hold:
        band = "warn"
    return band


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
    held: bool              # the verdict is a band kept by hysteresis, not the median's own
    window: tuple
    window_util: object


class _Evidence:
    """What one domain's B has shown, as seen through one tile."""

    def __init__(self):
        self.samples = 0
        self.events = 0
        self.fresh = 0
        self.still = 0              # readings with B unchanged while A held
        self.direction = 0          # sign of B's last move while A held
        self.mirror_run = 0
        self.mirror_as = set()      # A values seen during the current B == A run
        self.prev = None            # (a_khz, b_khz) of the previous reading
        self.recent_a = deque(maxlen=RECENT_A)

    def break_continuity(self):
        self.prev, self.fresh, self.still, self.direction = None, 0, 0, 0


class _Key:
    """Per-tile continuity: the domain it follows, its window, the load."""

    def __init__(self):
        self.dom = None
        self.window = []
        self.loaded = False
        self.band = None            # the last judged colour band, for hysteresis
        self.last_t = None
        self.last = None
        self.n = 0
        self.a_changes = deque(maxlen=4)    # reading numbers at which A changed

    def break_continuity(self):
        self.window, self.loaded, self.band = [], False, None


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
        k.break_continuity()
        ev = self._evidence.get((key, k.dom))
        if ev is not None:
            ev.break_continuity()

    def feed(self, key, dom, name, a_khz, b_khz, scale, util, paired, now, bin_mhz,
             graded=True):
        """One reading of this tile's row. Returns a Reading, or None when
        either array word is zero (a zero word is never evidence)."""
        if not (type(a_khz) is int and type(b_khz) is int and a_khz > 0 and b_khz > 0):
            self.miss(key)
            return None
        scale = scale if type(scale) is int and scale > 0 else 1
        util = util if isinstance(util, (int, float)) and not isinstance(util, bool) else None
        k = self._key(key)
        ev = self._evidence.get((key, dom))
        if ev is None:
            ev = self._evidence[(key, dom)] = _Evidence()
        if k.dom != dom:
            k.dom, k.last_t = dom, None
            k.break_continuity()
            k.a_changes.clear()
            ev.break_continuity()       # an earlier visit's continuity is stale
        if k.last_t is not None and now - k.last_t > READ_GAP_S:
            ev.break_continuity()
            k.break_continuity()
        k.last_t = now
        k.n += 1

        ev.samples += 1
        if b_khz == a_khz:
            ev.mirror_run += 1
            ev.mirror_as.add(a_khz)
        else:
            ev.mirror_run, ev.mirror_as = 0, set()
        if ev.prev is None:
            ev.fresh = ev.still = 0
        else:
            prev_a, prev_b = ev.prev
            if a_khz != prev_a:
                ev.fresh = ev.still = ev.direction = 0
                k.a_changes.append(k.n)
            elif b_khz != prev_b:
                ev.fresh += 1
                ev.still = 0
                direction = 1 if b_khz > prev_b else -1
                # B moved on its own while A held still, to a value A never
                # reported, and turned back from its previous move: a counter
                # jitters both ways, while a copy of A - lagging, offset or
                # smoothed - only ever chases A one way after it changes
                if (ev.direction and direction != ev.direction
                        and b_khz != a_khz and b_khz not in ev.recent_a):
                    ev.events += 1
                ev.direction = direction
            else:
                ev.still += 1
        if not ev.recent_a or ev.recent_a[-1] != a_khz:
            ev.recent_a.append(a_khz)
        ev.prev = (a_khz, b_khz)

        if ev.mirror_run >= MIRROR_RUN and len(ev.mirror_as) >= 2:
            trust = MIRROR          # B followed A exactly through a change of A
        elif ev.events >= EVIDENCE_EVENTS:
            trust = MEASURED
        elif ev.mirror_run >= DECIDE_READS:
            trust = SAME            # B equals A, and A has not changed to tell
        elif ev.samples < DECIDE_READS:
            trust = CHECKING
        else:
            trust = UNPROVEN

        loaded = util is not None and (util >= LOAD_GATE_PCT
                                       or (k.loaded and util >= LOAD_EXIT_PCT))
        moving = sum(1 for n in k.a_changes if k.n - n < MOVING_READS) >= 2
        delta = (b_khz - a_khz) / 1000.0 / scale
        if trust != MEASURED:
            state = trust
        elif not graded:
            state = "ungraded"
        elif util is None:
            state = "load unread"
        elif not loaded:
            state = "light"
        elif not k.loaded:
            state = "load start"
        elif not paired:
            state = "unpaired"
        elif ev.still >= STUCK_READS:
            state = "stuck"         # a counter that stopped refreshing
        elif ev.fresh < FRESH_NEEDED:
            state = "moving" if moving else "settling"
        else:
            state = "window"
        if state == "window":
            k.window = (k.window + [(a_khz, delta, util)])[-MEDIAN_READS:]
            state = ("judged" if len(k.window) >= WINDOW_READS
                     else "moving" if moving else "settling")
        else:
            k.window, k.band = [], None
        k.loaded = loaded

        deltas = tuple(d for _a, d, _u in k.window)
        utils = [u for _a, _d, u in k.window]
        verdict, held = None, False
        if state == "judged":
            verdict = window_verdict(deltas, bin_mhz, previous=k.band)
            if verdict in ("ok", "warn", "bad"):
                held = verdict != window_verdict(deltas, bin_mhz)
                k.band = verdict        # kept through a 'varying' window
        reading = Reading(
            key=key, dom=dom, name=name or "", scale=scale, trust=trust,
            events=ev.events, samples=ev.samples, mirror_run=ev.mirror_run,
            fresh=ev.fresh, a_khz=a_khz, b_khz=b_khz,
            a_mhz=a_khz / 1000.0 / scale, b_mhz=b_khz / 1000.0 / scale,
            delta_mhz=delta, util=util, paired=bool(paired), state=state,
            verdict=verdict, held=held,
            window=deltas if state == "judged" else (),
            window_util=(min(utils), max(utils)) if state == "judged" and utils else None)
        k.last = reading
        return reading
