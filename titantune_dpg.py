"""
TitanTune (Dear PyGui edition) - GPU monitor & tuner for the Titan-RTX-on-Strix card.

WHY THIS EXISTS: the original Tk UI dragged in slow motion and stalled the whole
desktop. Root cause, measured: Tk creates a native HWND per widget (~50 on the
control page) and, on WM_ENTERSIZEMOVE, drains its entire idle queue + after()
timers synchronously inside Windows' modal move loop. Dear ImGui renders the
whole UI as GPU geometry inside ONE window - measured 0 child HWNDs and ~120 FPS
while dragging, versus ~50 HWNDs and heavy lag under Tk.

The GPU layer (nvbackend.py) is reused VERBATIM: every NVAPI id, struct layout
and the 15 MHz quantisation law in it was verified against this card, and it is
the last code worth rewriting.

Safety model, carried over from the Tk version:
  * telemetry is read on a background thread; the UI never blocks on the driver
  * every write is behind the "Unlock controls" gate, except the two that only
    ever move toward stock: reset-to-stock, and releasing this app's own clock
    lock on exit (see release_on_exit - the lock is the one write that would
    otherwise outlive the process unseen)
  * footgun knobs (force P-state, TCC, CUDA clocks) are documented in
    README.md, never wired to a button. The per-domain V/F point lock used to
    be on that list and has come off it: it was validated end to end on this
    card (id resolves -> identity write -> single-field read-modify-write, each
    rung checked before the next), so Ctrl+H now drives it. See README.
  * Tk's modal confirmations have no ImGui equivalent. A press-again arm stood
    in for them, but a plan that only appears once the user has already pressed
    is a RECEIPT, not a warning. So the two curve writes - 'Apply to GPU' and
    'Reset curve to stock' - are one click, and the consequence is on screen
    continuously BEFORE the click, in the plan banner above them (see
    update_plan_banner). What pays for the missing second press is
    profiles.autosave(): every write that touches the 103-row VF delta
    table - 'Apply to GPU', 'Reset curve to stock', 'Re-phase', the CORE
    offset Apply (it is the same table), 'Reset all to stock' and a profile
    Load - takes an undo point immediately beforehand, restorable from
    Profiles > Undo last write. Those are the writes whose previous state is
    nowhere on screen; the single-knob applies (memory offset, power limit,
    voltage boost, fan) do not take one, because each moves one number its
    own slider still shows, and the clock lock cannot have one because a
    profile does not record it - Release / Ctrl+H is its undo. A snapshot
    that failed to capture the curve says so and is NOT called an undo
    point (see autosave_before).
    'Reset all to stock' is the ONE exception and still arms on the first
    press - it discards every knob at once (offsets, voltage boost, power
    limit, fan and all 103 deltas), so a stray click there costs a whole tune
    rather than one table.
    De-flatten is not a write at all - where Tk previewed it on a canvas, it
    STAGES onto the working curve, so the plan is visible on the plot and only
    'Apply to GPU' can commit it.
"""
import ctypes
import math
import os
import threading
import time

import dearpygui.dearpygui as dpg

import gpuload
import profiles
import timings
from nvbackend import (GPU, EVENT_REASONS, PERF_DECREASE_BITS, VF_STEP_KHZ,
                       VFP_POINTS, below_cap,
                       PRIV_CONFIRMED, PRIV_DOMAIN_ID, PRIV_LIKELY,
                       PRIV_N_DOMAINS, PRIV_PCIE_GEN, PRIV_UNNAMED)

# ---- palette (ImGui takes 0-255 RGBA) ------------------------------------- #
TEXT = (230, 232, 236)
DIM = (139, 144, 153)
ACCENT = (74, 163, 255)
GOOD = (70, 209, 122)
WARN = (255, 203, 71)
BAD = (255, 92, 92)
IDLE_COL = (58, 63, 75)
VIOLET = (160, 108, 255)


def dpi_scale():
    """Real desktop scale (1.5 at 150%). Read only AFTER declaring DPI
    awareness - Windows reports 96 dpi to unaware processes."""
    for fn in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
               lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            fn()
            break
        except Exception:
            continue
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


class TitanTune:
    def __init__(self):
        self.gpu = GPU()
        self.scale = dpi_scale()
        self.log_lines = []
        self.vf_points = None
        self.vf_work = {}          # idx -> working delta (editor)
        self.vf_orig = {}
        self.vf_by_idx = {}        # idx -> the hardware point, rebuilt by vf_read
        self.vf_sel = None
        self._fitted = False
        self._discard_armed = False
        # 'Reset all to stock' is the only write left that arms: it is the one
        # click with no per-knob undo of its own (see reset_all).
        self._reset_armed = False
        self._drag_idx = None
        # THE record of what this app is holding the card with, and how:
        #   None, or {"kind": LOCK_NVML|LOCK_VF, ...per-mechanism fields}
        # There are now TWO unrelated driver mechanisms behind this (see
        # LOCK_NAME): the Clocks menu's frequency lock and Ctrl+H's V/F point
        # lock. One record, carrying which - because a second source of truth
        # would let an on-screen hold outlive a Release that already dropped
        # it, and because releasing the WRONG mechanism returns OK while the
        # card stays pinned.
        self._clk_lock = None
        self._lockable = None      # cached top-mem-row lockable clock list
        self._hold_t = 0.0         # last accepted Ctrl+H (key auto-repeat)
        self._snap = None
        self._snap_err = None
        self._snap_t = None        # when the last GOOD read landed
        self._stale = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fonts = {}
        self._once = {}            # log-dedup state, keyed per source
        self._ctl_widgets = []     # write widgets greyed out while locked
        self._bar_themes = {}
        self._bar_band = {}
        self._dom_band = {}        # per-domain A-vs-B divergence colour band
        self._dom_shown = set()    # domains whose table row is currently shown
        self._plan_themes = {}     # plan-banner box themes, one per band
        self._plan_band = None
        self._pending_load = None  # (name, why) awaiting a cross-card confirm
        # ---- Timings tab (read-only; see the TIMINGS section) ------------- #
        self._tim_lock = threading.Lock()
        self._tim = None           # latest timings.Snapshot
        self._tim_avail = None     # latest timings.Availability
        self._tim_busy = False     # a capture thread is running
        self._tim_new = False      # there is a result the UI has not drawn
        self._tim_caps = {}        # memory clock -> Snapshot, the comparison
        self._tim_p0_in = False    # card is inside its top memory state now
        self._tim_note = ""        # where the last induced load landed
        self._tim_what = ""        # what the worker is doing, for the button
        self._tim_auto_t = 0.0     # last AUTOMATIC capture (anti-thrash)

    # ---- helpers ---------------------------------------------------------- #
    def s(self, n):
        return int(round(n * self.scale))

    def series_theme(self, marker, size, weight):
        """Marker/line style for one plot series. DPG's default marker radius
        is 4 px, which is nearly invisible on a 4K/150% desktop - and these
        dots are the drag targets, so they have to scale with the DPI."""
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvPlotStyleVar_Marker, marker,
                                    category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_MarkerSize,
                                    self.s(size), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight,
                                    self.s(weight),
                                    category=dpg.mvThemeCat_Plots)
        return th

    def log(self, msg, ok=None):
        tag = "" if ok is None else ("[ok] " if ok else "[!!] ")
        self.log_lines.append(tag + msg)
        del self.log_lines[:-200]
        if dpg.does_item_exist("log"):
            # NEWEST FIRST. A readonly multiline input_text keeps its own scroll
            # offset, DPG has no scroll-to-end (Tk called log.see('end')) and
            # only ~9 rows are visible, so anything appended below the fold
            # would never be read - and this log is the only receipt a write
            # leaves anywhere in the app.
            dpg.set_value("log", "\n".join(reversed(self.log_lines[-40:])))
        # mirror onto the V/F tab: its buttons write to the GPU, and the log
        # lives on the Control tab - without this a refused write is silent
        if dpg.does_item_exist("vf_status"):
            dpg.set_value("vf_status", self.log_lines[-1])
            dpg.configure_item("vf_status",
                               color=BAD if ok is False
                               else (GOOD if ok else DIM))

    def report(self, res):
        ok, msg = res
        self.log(msg, ok)

    def log_once(self, key, msg):
        """Tk's _log_once. A stuck driver re-raises the same error on every
        250 ms tick; without this it writes 4 lines/sec and flushes the write
        receipts - the thing the log exists for - out of the buffer in seconds."""
        if self._once.get(key) != msg:
            self._once[key] = msg
            self.log(msg, False)

    def clear_once(self, key):
        """Re-arm a deduplicated source once it recovers."""
        self._once.pop(key, None)

    def set_stale(self, err, snap_t):
        """Mark the telemetry stale in the header. log_once means a stuck driver
        says so exactly ONCE and then goes quiet, while every panel keeps
        redrawing the last good snapshot - so without this a frozen readout is
        indistinguishable from a live one, and this app exists to be believed
        about clocks and temperatures. The age counter is the cheap part: one
        set_value per tick, and only while the fault lasts."""
        if not dpg.does_item_exist("stale"):
            return
        if not err:
            if self._stale:
                self._stale = False
                dpg.set_value("stale", "")
            return
        self._stale = True
        age = (time.monotonic() - snap_t) if snap_t else None
        dpg.set_value("stale", "   ⚠ TELEMETRY STALE - "
                      + (f"last good read {age:.0f}s ago" if age is not None
                         else "no reading yet"))

    def unlocked(self):
        return dpg.does_item_exist("unlock") and dpg.get_value("unlock")

    def guard(self):
        """True if writes are permitted."""
        if not self.unlocked():
            self.log("locked - tick 'Unlock controls' first", False)
            return False
        return True

    # ---- telemetry thread ------------------------------------------------- #
    def poll_loop(self):
        while not self._stop.is_set():
            try:
                d = self.gpu.read()
                with self._lock:
                    self._snap, self._snap_err = d, None
                    self._snap_t = time.monotonic()
            except Exception as e:
                with self._lock:
                    self._snap_err = str(e)
            self._stop.wait(1.0)

    # ---- fonts ------------------------------------------------------------ #
    def load_fonts(self):
        fdir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        ui = os.path.join(fdir, "segoeui.ttf")
        sb = os.path.join(fdir, "seguisb.ttf")
        mono = os.path.join(fdir, "consola.ttf")
        with dpg.font_registry():
            if os.path.exists(ui):
                self._fonts["ui"] = dpg.add_font(ui, self.s(16))
            if os.path.exists(sb):
                self._fonts["big"] = dpg.add_font(sb, self.s(26))
            if os.path.exists(mono):
                self._fonts["mono"] = dpg.add_font(mono, self.s(14))
        if "ui" in self._fonts:
            dpg.bind_font(self._fonts["ui"])

    def bind(self, tag, name):
        if name in self._fonts and dpg.does_item_exist(tag):
            dpg.bind_item_font(tag, self._fonts[name])

    # ====================================================================== #
    #  MONITOR                                                               #
    # ====================================================================== #
    TILES = [("core", "CORE CLOCK", "MHz", ACCENT),
             ("xbar", "XBAR CLOCK", "MHz", VIOLET),
             ("mem", "MEM CLOCK", "MHz", ACCENT),
             ("edge", "EDGE TEMP", "\u00b0C", GOOD),
             ("hot", "HOTSPOT", "\u00b0C", WARN),
             ("pwr", "POWER", "W", ACCENT),
             ("vcore", "VCORE", "mV", ACCENT)]

    BARS = [("gpu", "GPU", ACCENT), ("board", "Board", VIOLET),
            ("tdp", "TDP used", WARN), ("ugpu", "GPU util", GOOD),
            ("ufb", "FB util", GOOD), ("uvid", "VID util", GOOD),
            ("ubus", "BUS util", GOOD)]

    def build_monitor(self):
        with dpg.tab(label="  Monitor  "):
            with dpg.group(horizontal=True, tag="tile_row"):
                for key, label, unit, col in self.TILES:
                    # no_scrollbar: the tile is sized to its content in
                    # relayout(), so a scrollbar here would only ever be a
                    # rounding artefact on a box with nothing to scroll to.
                    with dpg.child_window(tag=f"tile_{key}", width=self.s(180),
                                          height=self.s(104), border=True,
                                          no_scrollbar=True,
                                          no_scroll_with_mouse=True):
                        dpg.add_text(label, color=DIM)
                        dpg.add_text("--", tag=f"t_{key}", color=col)
                        self.bind(f"t_{key}", "big")
                        dpg.add_text(unit, color=DIM)
                        dpg.add_text("", tag=f"s_{key}", color=DIM, wrap=self.s(165))
            dpg.add_spacer(height=self.s(6))

            # Directly under the tiles on purpose: the CORE CLOCK tile above
            # shows the PROGRAMMED target, and this is the panel that says what
            # the card is measured to be doing instead. Put it at the bottom of
            # the page and the number it qualifies is off screen.
            self.build_domains()
            dpg.add_spacer(height=self.s(6))

            with dpg.group(horizontal=True):
                with dpg.child_window(tag="pan_thr", width=self.s(430),
                                      height=self.s(300)):
                    dpg.add_text("THROTTLE / CLOCKS-EVENT REASONS", color=ACCENT)
                    dpg.add_separator()
                    for _b, name in EVENT_REASONS:
                        dpg.add_text(f"  \u25cf  {name}", tag=f"lamp_{name}",
                                     color=IDLE_COL)
                    dpg.add_spacer(height=self.s(4))
                    dpg.add_text("perf-decrease (NVAPI)", color=DIM)
                    for _b, name in PERF_DECREASE_BITS:
                        dpg.add_text(f"  \u25cf  {name}", tag=f"pd_{name}",
                                     color=IDLE_COL)
                with dpg.child_window(tag="pan_pwr", width=self.s(430),
                                      height=self.s(300)):
                    dpg.add_text("POWER SPLIT & UTILIZATION", color=ACCENT)
                    dpg.add_separator()
                    for key, label, col in self.BARS:
                        dpg.add_text(label, color=DIM)
                        # three themes per bar, built ONCE: the fill escalates
                        # amber at >=80% and red at >=95% like Tk's Bar._paint,
                        # and a theme created per frame would leak items
                        self._bar_themes[key] = {
                            "ok": self.bar_theme(col),
                            "warn": self.bar_theme(WARN),
                            "bad": self.bar_theme(BAD)}
                        self._bar_band[key] = "ok"
                        dpg.add_progress_bar(tag=f"bar_{key}", default_value=0.0,
                                             width=-1, overlay="--")
                        dpg.bind_item_theme(f"bar_{key}",
                                            self._bar_themes[key]["ok"])

            dpg.add_spacer(height=self.s(6))
            with dpg.group(horizontal=True):
                with dpg.child_window(tag="pan_pcie", width=self.s(430),
                                      height=self.s(120)):
                    dpg.add_text("PCIE LINK", color=ACCENT)
                    dpg.add_separator()
                    dpg.add_text("--", tag="pcie", wrap=self.s(400))
                with dpg.child_window(tag="pan_state", width=self.s(430),
                                      height=self.s(120)):
                    dpg.add_text("STATE", color=ACCENT)
                    dpg.add_separator()
                    dpg.add_text("--", tag="state", wrap=self.s(400))

    def bar_theme(self, col):
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvProgressBar):
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, col)
        return th

    # ---- all clock domains ------------------------------------------------ #
    # header / width in UNSCALED px. The unit lives in the CELL, not the
    # header, because the column is not homogeneous: domain 31 is a PCIe link
    # generation and prints "gen 3" where every other row prints MHz.
    DOM_COLS = (("dom", 46), ("domain", 165), ("programmed  A", 145),
                ("measured  B", 145), ("Δ  B-A", 135),
                ("flags", 90), ("srcid", 80))

    # A name we earned is written plainly; a name that is only elimination gets
    # amber and a '?', so nobody goes debugging the wrong domain on our say-so.
    GRADE_COL = {PRIV_CONFIRMED: TEXT, PRIV_LIKELY: WARN, PRIV_UNNAMED: DIM}

    # Divergence bands, in whole 15 MHz clock bins. Inside one bin is the
    # counter jittering; past one bin the card is genuinely not running at the
    # frequency the tiles report.
    DOM_WARN_MHZ = VF_STEP_KHZ / 1000.0
    DOM_BAD_MHZ = 3 * VF_STEP_KHZ / 1000.0
    DOM_BAND_COL = {"ok": DIM, "warn": WARN, "bad": BAD}

    def build_domains(self):
        """Every domain the private getter populates, PROGRAMMED beside
        MEASURED. The tiles show array A - the target the driver programmed,
        always exactly on the 15 MHz grid - and this panel is the only thing in
        the app that says what the card is measured to be doing instead. A
        monitor that only ever quotes the optimistic number of the two is the
        failure mode it exists to close.

        Measured (see nvbackend's header for the full table): settled and under
        load the two agree to within ~3 MHz, or by an exact 15 MHz bin with B
        the HIGHER of the two. The wide readings are a clock change in flight
        (~1-2 s, either sign, hundreds of MHz) or an idle card, where B
        measures a gated clock and sits hundreds of MHz low indefinitely.

        All 32 rows are built once and the unpopulated ones hidden rather than
        created per tick: a domain that only appears at some other pstate then
        shows up IN PLACE instead of renumbering the table under the reader."""
        with dpg.child_window(tag="pan_dom", width=-1, height=self.s(300)):
            dpg.add_text("ALL CLOCK DOMAINS  ·  private NvAPI "
                         "GetAllClocks (0x1BD69F49)", color=ACCENT,
                         tag="dom_title")
            with dpg.tooltip("dom_title"):
                dpg.add_text(
                    "The 288-dword payload is two arrays over the same 32\n"
                    "domains, an exact partition verified over a 192-sample\n"
                    "sweep:  A = 2 dwords per domain at 2*d {freq, flags},\n"
                    "B = 7 dwords per domain at 64+7*d {freq, srcid, 0...}.\n\n"
                    "MEM is the RAW NVAPI figure (half the data rate) - the\n"
                    "MEM CLOCK tile converts it to the true memory clock, so\n"
                    "the two are meant to differ by the GDDR divisor.\n\n"
                    "Measured on this card, GPC, 40 samples per case:\n"
                    "  settled + ~99% load, free boost   A 1950.0 / B 1949.9\n"
                    "  settled + load, locked 1920       within 3 MHz\n"
                    "  settled + load, locked 1350       B 1364.9: one 15 MHz\n"
                    "                                    bin ABOVE A, steady\n"
                    "  ~1-2 s after any clock change     hundreds of MHz,\n"
                    "                                    either sign\n"
                    "  idle, no load                     B sits hundreds low\n"
                    "                                    and never settles -\n"
                    "                                    the clock is gated\n"
                    "                                    and B is its average\n"
                    "So a wide delta means 'mid-change or idle'. A wide one on\n"
                    "a busy card that has been at one clock for seconds is the\n"
                    "case worth reading: there the tiles are optimistic.\n\n"
                    "Domain 31 is not a clock: array A holds the PCIe link\n"
                    "generation. Its array-B word has not been identified,\n"
                    "so it is shown raw rather than dressed up as anything.")
            dpg.add_separator()
            # The legend is on the page, not in the tooltip: a hedged name is
            # only honest if the thing that hedges it is visible without
            # hovering.
            dpg.add_text(
                "A = the target the driver PROGRAMMED (always on the 15 MHz "
                "grid; this is what the tiles above show)   ·   B = a "
                "free-running MEASURED counter   ·   Δ turns amber "
                "past one 15 MHz bin and red past three. Measured: settled and "
                "under load the two agree to within ~3 MHz. Δ is wide for "
                "~1-2 s after any clock change (either sign), and wide "
                "PERMANENTLY on an idle card - with no work the clock gates "
                "and B measures its average, hundreds of MHz low. A steady Δ "
                "on a busy card is the one that counts: there the tiles are "
                "optimistic.\n"
                "Names:  plain = CONFIRMED   ·   amber '?' = LIKELY, by "
                "elimination only - domain 31 is one of these, drawn as 'PCIe "
                "link gen?' because it is a link generation and not a clock at "
                "all   ·   '--' = populated but unidentified, so it stays a "
                "number (3/6/20/22, static here).",
                tag="dom_legend", color=DIM, wrap=self.s(1100))
            dpg.add_text("", tag="dom_err", color=BAD, show=False)
            with dpg.table(tag="dom_table", header_row=True,
                           no_host_extendX=True,
                           policy=dpg.mvTable_SizingFixedFit,
                           borders_innerH=True, borders_innerV=True):
                for label, w in self.DOM_COLS:
                    dpg.add_table_column(label=label, width_fixed=True,
                                         init_width_or_weight=self.s(w))
                for dom in range(PRIV_N_DOMAINS):
                    name, grade, _kind = PRIV_DOMAIN_ID.get(
                        dom, ("", PRIV_UNNAMED, None))
                    with dpg.table_row(tag=f"dom_row_{dom}", show=False):
                        dpg.add_text(f"{dom:>2}", tag=f"dom_{dom}_ix",
                                     color=DIM)
                        # the name and its grade come from a static table, so
                        # they are written ONCE here instead of on every tick
                        dpg.add_text((name + "?") if grade == PRIV_LIKELY
                                     else (name or "--"),
                                     tag=f"dom_{dom}_name",
                                     color=self.GRADE_COL[grade])
                        for col in ("prog", "meas", "delta", "flags", "srcid"):
                            dpg.add_text("--", tag=f"dom_{dom}_{col}",
                                         color=DIM if col == "delta" else TEXT)
                        # monospace: these columns are read by comparing one
                        # row against another, which proportional digits fight
                        for col in ("ix", "prog", "meas", "delta", "flags",
                                    "srcid"):
                            self.bind(f"dom_{dom}_{col}", "mono")

    @staticmethod
    def flag_fmt(v):
        """The capability field. One byte on every domain this card populates,
        but printed full width if anything ever sets more - a flag word
        silently truncated to its low byte would be a lie in hex."""
        return f"0x{v:02X}" if v <= 0xFF else f"0x{v:08X}"

    def dom_band(self, delta_mhz):
        if delta_mhz is None:
            return "ok"
        d = abs(delta_mhz)
        return ("bad" if d >= self.DOM_BAD_MHZ else
                "warn" if d >= self.DOM_WARN_MHZ else "ok")

    def refresh_domains(self, d):
        rows = d.get("clk_domains")
        err = d.get("clk_domains_err")
        if dpg.does_item_exist("dom_err"):
            dpg.configure_item("dom_err", show=bool(err))
            if err:
                dpg.set_value("dom_err", err)
        present = set()
        for r in (rows or []):
            dom = r["domain"]
            present.add(dom)
            if r["kind"] == PRIV_PCIE_GEN:
                # 1/2/3, NOT kHz. Divided by 1000 like every other row it
                # would print as a wholly believable 0.0 MHz domain.
                # Zero is not a generation, and dword 62 does read 0 here (4 of
                # 4 samples in one run) - printing "gen 0" invents a link that
                # negotiated down to nothing. Unknown is '--', like every other
                # value this panel will not vouch for.
                prog = f"gen {r['prog_khz']}" if r["prog_khz"] else "--"
                meas = f"raw {r['meas_khz']}"
                delta = "--"
            else:
                prog = f"{r['prog_mhz']:.1f} MHz"
                meas = f"{r['meas_mhz']:.1f} MHz"
                dv = r["delta_mhz"]
                # +0.0, never "-0.0": the counter's 1-3 Hz jitter puts a static
                # domain a hair under its target, and a minus sign on a
                # zero-to-one-decimal delta reads as a real deficit.
                # (-0.0 + 0.0 is +0.0 in IEEE 754.)
                delta = "--" if dv is None else f"{round(dv, 1) + 0.0:+.1f} MHz"
            dpg.set_value(f"dom_{dom}_prog", prog)
            dpg.set_value(f"dom_{dom}_meas", meas)
            dpg.set_value(f"dom_{dom}_delta", delta)
            dpg.set_value(f"dom_{dom}_flags", self.flag_fmt(r["flags"]))
            dpg.set_value(f"dom_{dom}_srcid", str(r["srcid"]))
            # re-theme only on a band change, same reason as the bars
            band = self.dom_band(r["delta_mhz"])
            if self._dom_band.get(dom) != band:
                self._dom_band[dom] = band
                dpg.configure_item(f"dom_{dom}_delta",
                                   color=self.DOM_BAND_COL[band])
        # row visibility is 32 configure_item calls, so it is only touched when
        # the populated set actually changes - which on this card is never
        if present != self._dom_shown:
            for dom in range(PRIV_N_DOMAINS):
                if dpg.does_item_exist(f"dom_row_{dom}"):
                    dpg.configure_item(f"dom_row_{dom}", show=dom in present)
            self._dom_shown = present

    def text_h(self, txt, font_name, wrap=-1.0):
        """Rendered height of `txt`, or None if the font is not ready yet."""
        f = self._fonts.get(font_name)
        try:
            sz = dpg.get_text_size(txt, wrap_width=wrap, font=f) if f \
                else dpg.get_text_size(txt, wrap_width=wrap)
        except Exception:
            return None
        return sz[1] if sz else None

    def tile_height(self, tw):
        """Measured, not guessed. A tile stacks label / value / unit / subtitle,
        and the subtitle wraps - so at some widths and DPI settings the content
        is taller than a fixed height and DPG grows an inner scrollbar."""
        lh = self.text_h("Ag", "ui") or self.s(19)
        bh = self.text_h("0123", "big") or self.s(31)
        wrap = self.sub_wrap(tw)
        subs = lh
        for key, *_ in self.TILES:
            txt = (dpg.get_value(f"s_{key}") if dpg.does_item_exist(f"s_{key}")
                   else "") or "Ag"
            subs = max(subs, self.text_h(txt, "ui", wrap) or lh)
        # label + unit + value + subtitle, plus 3 item gaps and frame padding
        return int(lh * 2 + bh + subs + self.s(30))

    def sub_wrap(self, tw):
        return max(self.s(80), tw - self.s(26))

    def menu_h(self):
        """Height the viewport menu bar takes out of the client area. DPG draws
        it OVER the top of the primary window instead of insetting it, so the
        tabs really have this much less room than get_viewport_client_height()
        reports - sizing from the raw figure pushes the bottom row under the
        window edge. Measured once DPG will say, guessed from the font before
        the first frame."""
        if not dpg.does_item_exist("menubar"):
            return 0
        try:
            h = dpg.get_item_rect_size("menubar")[1]
        except Exception:
            h = 0
        return int(h) if h else (self.text_h("Ag", "ui") or self.s(19)) + self.s(8)

    def relayout(self, *_a):
        """Size the panels from the CURRENT viewport instead of fixed pixels.
        At 150% DPI the old fixed sizes overflowed and every panel grew its own
        scrollbar while the window had empty space to spare."""
        try:
            W = dpg.get_viewport_client_width()
            H = dpg.get_viewport_client_height()
        except Exception:
            return
        if W < 100 or H < 100:
            return
        mh = self.menu_h()
        if dpg.does_item_exist("menu_pad"):
            dpg.configure_item("menu_pad", height=mh)
        H -= mh
        pad = self.s(10)
        # six tiles across the full width
        tw = max(self.s(120), (W - pad * (len(self.TILES) + 2)) // len(self.TILES))
        tile_h = self.tile_height(tw)
        wrap = self.sub_wrap(tw)
        for key, *_ in self.TILES:
            if dpg.does_item_exist(f"tile_{key}"):
                dpg.configure_item(f"tile_{key}", width=tw, height=tile_h)
            if dpg.does_item_exist(f"s_{key}"):
                dpg.configure_item(f"s_{key}", wrap=wrap)
        # two columns; give the mid row whatever is left after tiles + bottom
        colw = max(self.s(300), (W - pad * 3) // 2)
        # 0.16 was over-generous now that a fourth row competes for the height:
        # PCIE LINK is two lines and STATE four, so 0.12 still clears both and
        # hands the difference to the two panels that are genuinely long.
        bot_h = max(self.s(110), int(H * 0.12))
        # The all-domains table is a full-width row of its own (width=-1, so
        # only its height is managed here). 11 populated rows plus the legend
        # do not fit beside anything, and it is a child_window - past its
        # share it scrolls internally rather than pushing the page.
        dom_h = max(self.s(240), int(H * 0.34))
        mid_h = max(self.s(220), H - tile_h - dom_h - bot_h - self.s(102))
        if dpg.does_item_exist("pan_dom"):
            dpg.configure_item("pan_dom", height=dom_h)
        if dpg.does_item_exist("dom_legend"):
            dpg.configure_item("dom_legend", wrap=W - self.s(40))
        for tag, h in (("pan_thr", mid_h), ("pan_pwr", mid_h),
                       ("pan_pcie", bot_h), ("pan_state", bot_h)):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, width=colw, height=h)
        # control tab: plot and log share the lower half
        if dpg.does_item_exist("vf_plot"):
            dpg.configure_item("vf_plot", height=max(self.s(240),
                                                     int(H * 0.34)))
        if dpg.does_item_exist("log"):
            dpg.configure_item("log", height=max(self.s(90), int(H * 0.13)))
        for tag in ("vf_info", "vf_status", "vf_sel_info", "hold_info"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, wrap=W - self.s(40))
        # timings tab: the header is sized to its CONTENT like the monitor
        # tiles are - it carries the memory clock the whole tab is counted
        # against plus a warning that wraps - and the panels below split what
        # is left. The divergence panel is usually hidden and takes no share.
        wrapw = W - self.s(40)
        for tag in ("tim_ro", "tim_reason", "tim_warn", "tim_sub", "cmp_hint",
                    "cmp_legend", "div_sub", "tim_state", "tim_induce_note"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, wrap=wrapw)
        if dpg.does_item_exist("pan_tim_hdr"):
            hdr_h = self.tim_hdr_h(wrapw)
            dpg.configure_item("pan_tim_hdr", height=hdr_h)
            rest = max(self.s(320), H - hdr_h - self.s(104))
            div_h = (max(self.s(120), int(rest * 0.20))
                     if dpg.is_item_shown("pan_div") else 0)
            tim_h = max(self.s(180), int((rest - div_h) * 0.56))
            cmp_h = max(self.s(150), rest - div_h - tim_h - self.s(30))
            for tag, h in (("pan_tim", tim_h), ("pan_cmp", cmp_h),
                           ("pan_div", div_h)):
                if h and dpg.does_item_exist(tag):
                    dpg.configure_item(tag, height=h)
        self.size_plan_banner()

    def tim_hdr_h(self, wrap):
        """Measured, not guessed - same reason as tile_height(): the warning
        line wraps, and at some widths a fixed height would either clip the
        clock this tab's whole ns column depends on or leave a scrollbar on a
        box with nothing to scroll."""
        lh = self.text_h("Ag", "ui") or self.s(19)
        h = lh * 3 + self.s(28)
        if dpg.does_item_exist("tim_warn") and dpg.is_item_shown("tim_warn"):
            h += self.text_h(dpg.get_value("tim_warn") or "Ag", "ui",
                             wrap) or lh
        return int(h)

    def mem_fmt(self, reported):
        """True memory clock when the type is known, else raw NVAPI figure."""
        if not isinstance(reported, (int, float)):
            return "--", ""
        div = self.gpu.static.get("mem_div")
        gbps = reported * 2 / 1000.0
        mtype = self.gpu.static.get("mem_type", "unknown")
        if div:
            v = reported / div
            return (f"{v:.1f}".rstrip("0").rstrip("."),
                    f"{mtype} \u00b7 {gbps:.2f} Gbps")
        return f"{reported:.0f}", f"{mtype} \u00b7 {gbps:.2f} Gbps (raw)"

    def refresh_monitor(self, d):
        # first: it qualifies the CORE CLOCK tile written just below it, and
        # it rides the same snapshot - no extra driver call (see GPU.read)
        self.refresh_domains(d)
        dpg.set_value("t_core", str(d.get("core", "--")))
        dpg.set_value("s_core", f"P{d.get('pstate','?')}")
        # XBAR: measured to follow core frequency, NOT the voltage rail
        # (at 1800 MHz locked, vcore spanning 912-1069 mV left it at 1725).
        xb, core = d.get("xbar"), d.get("core")
        dpg.set_value("t_xbar", str(xb if xb is not None else "--"))
        dpg.set_value("s_xbar",
                      f"{xb - core:+d} vs core" if (xb and core) else "--")
        mtxt, msub = self.mem_fmt(d.get("mem"))
        dpg.set_value("t_mem", mtxt)
        dpg.set_value("s_mem", msub)
        dpg.set_value("t_edge", str(d.get("temp_edge", "--")))
        hot = d.get("temp_hotspot")
        if hot is not None:
            col = BAD if hot >= 90 else WARN if hot >= 80 else GOOD
            dpg.set_value("t_hot", f"{hot:.0f}")
            dpg.configure_item("t_hot", color=col)
            dpg.set_value("s_hot",
                          f"\u0394 {d.get('temp_delta',0):.0f} \u00b0C over edge")
        pw = d.get("power_w")
        dpg.set_value("t_pwr", f"{pw:.0f}" if pw is not None else "--")
        dpg.set_value("s_pwr", f"limit {d.get('pl_now_mw',0)//1000} W")
        vc = d.get("vcore_mv")
        dpg.set_value("t_vcore", f"{vc:.0f}" if vc is not None else "--")

        em = d.get("event_mask", 0)
        for bit, name in EVENT_REASONS:
            on = bool(em & bit)
            dpg.configure_item(f"lamp_{name}",
                               color=(GOOD if name == "Idle" else BAD) if on
                               else IDLE_COL)
        pdv = d.get("perf_decrease", 0)
        for bit, name in PERF_DECREASE_BITS:
            dpg.configure_item(f"pd_{name}",
                               color=BAD if (pdv & bit) else IDLE_COL)

        # TDP used = actual draw / the limit currently enforced. The old
        # "PL tgt" bar showed the limit SETTING (a constant 123%), which told
        # you nothing about how hard the card is working.
        lim_w = (d.get("pl_now_mw") or 0) / 1000.0
        draw_w = d.get("power_w") or 0.0
        tdp_pct = (draw_w / lim_w * 100.0) if lim_w > 0 else 0.0
        vals = {"gpu": d.get("pwr_gpu_pct", 0), "board": d.get("pwr_board_pct", 0),
                "tdp": tdp_pct, "ugpu": d.get("util_gpu", 0),
                "ufb": d.get("util_fb", 0), "uvid": d.get("util_vid", 0),
                "ubus": d.get("util_bus", 0)}
        for key, v in vals.items():
            dpg.set_value(f"bar_{key}", max(0.0, min(1.0, float(v) / 100.0)))
            # re-theme only on a band change - rebinding every frame is churn
            band = "bad" if v >= 95 else ("warn" if v >= 80 else "ok")
            if self._bar_band.get(key) != band and key in self._bar_themes:
                self._bar_band[key] = band
                dpg.bind_item_theme(f"bar_{key}", self._bar_themes[key][band])
            if key == "tdp":
                dpg.configure_item(
                    f"bar_{key}",
                    overlay=f"{tdp_pct:.0f}%   {draw_w:.0f} / {lim_w:.0f} W")
            else:
                dpg.configure_item(f"bar_{key}", overlay=f"{v:.0f}%")

        errt = d.get("pcie_err_total", 0)
        txt = (f"Gen {d.get('pcie_gen','?')}  x{d.get('pcie_width','?')}\n"
               f"errors: {errt}  (since launch {d.get('pcie_err_since',0)})")
        if errt:
            nz = {k: v for k, v in d.get("pcie_err", {}).items() if v}
            txt += "\n" + "  ".join(f"{k}:{v}" for k, v in nz.items())
        dpg.set_value("pcie", txt)
        dpg.configure_item("pcie", color=GOOD if errt == 0 else BAD)

        fans = d.get("fans", [])
        fantxt = "  ".join(f"fan{i}: {duty}% {rpm or 0}rpm"
                           for i, (duty, rpm) in enumerate(fans)) or "--"
        mscale, munit = self.gpu.mem_offset_scale()
        moff = d.get("mem_off", 0)
        mdisp = int(moff / mscale) if isinstance(moff, int) else 0
        # BOTH lock mechanisms, read back from the driver rather than from this
        # app's own record - so a lock set by another tuner, or left behind by
        # an earlier run, shows up here even though nothing in this session
        # took it. The V/F voltage is the one REQUESTED, not the point held
        # (the driver echoes it back - see nvbackend's VF_LOCK_* block), so it
        # is labelled as a request.
        vfmv = d.get("vf_lock_mv")
        ck = d.get("clk_lock_mhz")
        dpg.set_value("state",
                      f"energy {d.get('energy_j',0):.0f} J\n{fantxt}\n"
                      f"offsets: core {d.get('core_off',0):+d} MHz   "
                      f"mem {mdisp:+d} {munit}\n"
                      f"volt-boost {d.get('vboost_pct','--')}%   "
                      f"VF-locked {d.get('vf_locked_domains') or 'none'}"
                      + (f" (asked {vfmv:.2f} mV)" if vfmv else "")
                      + (f"   clk-locked [{ck[0]}..{ck[1]}] MHz" if ck
                         else "   clk-locked none"))

    # ====================================================================== #
    #  CONTROL                                                               #
    # ====================================================================== #
    # label / slider / Apply / extra, in UNSCALED px. Every knob group builds
    # its table from this one tuple, so Apply is a straight column down the tab
    # instead of landing wherever each row's label happened to end.
    KNOB_COLS = (230, 340, 90, 80)

    def knob_cols(self):
        for w in self.KNOB_COLS:
            dpg.add_table_column(width_fixed=True,
                                 init_width_or_weight=self.s(w))

    def build_control(self):
        st = self.gpu.static
        with dpg.tab(label="  Control  "):
            with dpg.group(horizontal=True):
                # Default ON: the only people running this are vetted internal
                # users, and the extra click bought nothing. Untick to make the
                # app read-only.
                dpg.add_checkbox(label="Unlock controls", tag="unlock",
                                 default_value=True,
                                 callback=lambda s, a, u: self.sync_lock_ui())
                dpg.add_spacer(width=self.s(40))
                # sits with the gate, not inside a knob group: it undoes every
                # group at once (and the curve), so it belongs to the tab
                dpg.add_button(label="Reset all to stock", callback=self.reset_all,
                               width=self.s(200), height=self.s(28))
            dpg.add_text("writes ENABLED - untick for read-only. "
                         "All changes are reversible and reset on reboot",
                         tag="unlock_note", color=DIM)
            dpg.add_separator()

            # OUTSIDE the collapsing groups on purpose: this line is the only
            # in-app confirmation that an applied offset or clock lock actually
            # took effect, so it has to stay on screen whatever is collapsed.
            dpg.add_text("", tag="ctl_clocks", color=TEXT)
            self.bind("ctl_clocks", "mono")
            # Outside the collapsing groups for the same reason as ctl_clocks,
            # and one more: Ctrl+H is a window-wide key, so a hold can be taken
            # and released with the V/F header that owns the feature collapsed.
            # This is then the only thing on screen saying the clock is pinned.
            dpg.add_text("", tag="hold_info", color=GOOD)
            dpg.add_separator()

            with dpg.collapsing_header(label="Clock offsets", default_open=True):
                with dpg.table(header_row=False, no_host_extendX=True,
                               policy=dpg.mvTable_SizingFixedFit):
                    self.knob_cols()
                    core_lo, core_hi = -200, 300
                    if st.get("core_off_range"):
                        core_lo = st["core_off_range"][0]
                        core_hi = st["core_off_range"][1]
                    self.slider_row("core", "Core clock offset (MHz)",
                                    core_lo, core_hi, 0, self.apply_core,
                                    note=f"Apply snaps DOWN to the "
                                         f"{VF_STEP_KHZ//1000} MHz grid, then "
                                         f"shows what was written")

                    mscale, munit = self.gpu.mem_offset_scale()
                    mlo, mhi = -500, 1500
                    if st.get("mem_off_range"):
                        mlo = int(st["mem_off_range"][0] / mscale)
                        mhi = int(st["mem_off_range"][1] / mscale)
                    self.slider_row("mem", f"Memory offset ({munit})",
                                    mlo, mhi, 0, self.apply_mem)

            # Voltage boost is grouped with the limits, not the offsets: it moves
            # no clock at all, it raises a ceiling the arbiter is allowed to
            # reach - the same shape of knob as the power limit.
            with dpg.collapsing_header(label="Limits", default_open=True):
                with dpg.table(header_row=False, no_host_extendX=True,
                               policy=dpg.mvTable_SizingFixedFit):
                    self.knob_cols()
                    pl_lo = st.get("pl_min_mw", 100000) // 1000
                    pl_hi = st.get("pl_max_mw", 320000) // 1000
                    pl_def = st.get("pl_def_mw", 260000) // 1000
                    self.slider_row("pl", "Power limit (W)", pl_lo, pl_hi,
                                    pl_def, self.apply_pl)

                    vb = self.gpu.read_voltage_boost()
                    self.slider_row("volt", "Core voltage boost (%)", 0, 100,
                                    0 if vb is None else max(0, min(100, int(vb))),
                                    self.apply_volt,
                                    note="raises the reliability-voltage ceiling")

                    fan_floor = st.get("fan_min", 30)
                    self.slider_row("fan", "Fan duty (%)", fan_floor, 100,
                                    fan_floor, self.apply_fan,
                                    extra=("Auto", self.fan_auto))

            with dpg.collapsing_header(label="V/F curve editor",
                                       default_open=True):
                self.build_vf()

            dpg.add_separator()
            dpg.add_text("log  (newest line first)", color=DIM)
            dpg.add_input_text(tag="log", multiline=True, readonly=True,
                               width=-1, height=self.s(150))
            self.bind("log", "mono")

    def slider_row(self, key, label, lo, hi, init, cb, note=None, extra=None):
        """One knob = one row of the enclosing knob table (see knob_cols), so
        every Apply lands in the same column even though the labels, the notes
        and the presence of an extra button all differ per row."""
        with dpg.table_row():
            dpg.add_text(label, color=TEXT)
            with dpg.group():
                # clamped: in DPG min_value/max_value only bound the DRAG.
                # Ctrl+click turns a slider into a text field that accepts
                # anything, so without this the UI happily shows 150% voltage
                # boost or a +5000 MHz offset, the backend refuses the write, and
                # the only sign is one log line while the knob keeps displaying a
                # value the card never took.
                dpg.add_slider_int(tag=f"sl_{key}", label="", default_value=init,
                                   min_value=lo, max_value=hi, clamped=True,
                                   width=-1)
                if note:
                    dpg.add_text(note, color=DIM,
                                 wrap=self.s(self.KNOB_COLS[1] - 10))
            # width=-1 fills the cell, which is what makes the buttons one width
            dpg.add_button(label="Apply", tag=f"go_{key}", width=-1,
                           callback=lambda: cb(dpg.get_value(f"sl_{key}")))
            self._ctl_widgets += [f"sl_{key}", f"go_{key}"]
            if extra:
                dpg.add_button(label=extra[0], tag=f"go_{key}_x",
                               width=-1, callback=lambda: extra[1]())
                self._ctl_widgets.append(f"go_{key}_x")

    def sync_lock_ui(self):
        """Grey out every write widget while the gate is clear. Tk kept the same
        list in _ctl_widgets and disabled it from _toggle_lock; DPG disables
        nothing on its own, so without this a locked build looks fully live and
        a refused write shows up only as one line in the log. 'Reset all to
        stock' stays live on purpose - it only ever moves toward stock; 'Reset
        curve to stock' is a 103-row table write that also discards staged
        edits, so it is gated with the rest."""
        on = self.unlocked()
        for tag in self._ctl_widgets:
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=on)
        if dpg.does_item_exist("unlock_note"):
            dpg.set_value("unlock_note",
                          "writes ENABLED - untick for read-only. All changes "
                          "are reversible and reset on reboot" if on else
                          "READ-ONLY - every write control below is disabled "
                          "(curve edits are still staged, not written)")
            dpg.configure_item("unlock_note", color=DIM if on else WARN)

    # ---- write handlers (identical backend calls to the Tk build) ---------- #
    def apply_core(self, v):
        if not self.guard():
            return
        # Snap here, not in the backend: set_clock_offset rounds to the NEAREST
        # bin, so +8 would come back as +15 - clock nobody asked for. Same rule
        # as rephase_deltas and Set-MHz: a request may lose a bin, never gain
        # one. Tk's tk.Scale(resolution=15) made off-grid values unreachable;
        # add_slider_int has no resolution, so the value is snapped on Apply and
        # written back to the slider - the number on screen is the number in the
        # card.
        step = VF_STEP_KHZ // 1000
        mhz = int(math.floor(int(v) / step)) * step
        # ...but never past the driver's own floor. That bound is not a multiple
        # of 15 (-200 snaps DOWN to -210), so at the very bottom of the slider
        # the snap would leave the legal range and set_clock_offset would refuse
        # the write - a dead Apply. The lowest legal bin is the only way out.
        rng = self.gpu.static.get("core_off_range")
        lo = rng[0] if rng else -200
        mhz = max(mhz, int(math.ceil(lo / step)) * step)
        if mhz != int(v):
            dpg.set_value("sl_core", mhz)
        # An undo point, unlike the other sliders: this offset lands in the
        # SAME 103-row delta table as the curve (see nvbackend.set_clock_offset),
        # so one drag and one Apply overwrites a hand-tuned curve that is
        # nowhere on screen. It is a curve write wearing a slider.
        self.autosave_before("core-offset")
        self.report(self.gpu.set_clock_offset(0, mhz))

    def apply_mem(self, v):
        if self.guard():
            self.report(self.gpu.set_clock_offset(2, int(v)))

    def apply_pl(self, v):
        if self.guard():
            self.report(self.gpu.set_power_limit_mw(int(v) * 1000))

    def apply_volt(self, v):
        if self.guard():
            self.report(self.gpu.set_voltage_boost(int(v)))

    def apply_fan(self, v):
        if self.guard():
            self.report(self.gpu.set_fan(int(v)))

    def fan_auto(self):
        if self.guard():
            self.report(self.gpu.reset_fan())

    # ---- the two lock mechanisms ------------------------------------------ #
    # The card can be pinned two completely different ways, and neither driver
    # call reads or clears the other:
    #   LOCK_NVML  nvmlDeviceSetGpuLockedClocks - pins a FREQUENCY range. At
    #              idle it leaves memory in the low state (mem 810 here).
    #   LOCK_VF    the NvAPI per-domain V/F point lock - pins a VOLTAGE, and
    #              holds true P0 (mem 7000) with the card near idle.
    # _clk_lock carries which one is in force, because "release the lock" has
    # to become the right call and a wrong one succeeds silently.
    LOCK_NVML = "nvml"
    LOCK_VF = "vf"
    LOCK_NAME = {LOCK_NVML: "NVML locked clocks (Clocks menu)",
                 LOCK_VF: "V/F point lock (Ctrl+H)"}

    def release_current(self):
        """Drive the release that matches the record, and return its (ok, msg).
        With no record it falls back to the NVML reset: that is the mechanism
        the Clocks-menu Release button names, and it is the one a lock left by
        an earlier run of THIS app would be in. It deliberately does not clear
        an unrecorded V/F lock - that one is almost certainly another tuner's
        (this card was found holding Afterburner's), and taking someone else's
        lock away because a button was nearby is not this app's business."""
        if self._clk_lock and self._clk_lock.get("kind") == self.LOCK_VF:
            return self.gpu.clear_vf_lock()
        return self.gpu.reset_gpu_clocks()

    def handover(self, kind):
        """Release a lock of the OTHER mechanism before taking this one.

        There is exactly one lock record, so a second mechanism taken on top of
        the first would overwrite the record and leave the first one held in
        the driver with nothing on screen naming it - invisible until a reboot.
        Returns False when the old lock could NOT be released, in which case
        the new one must not be taken either: the record has to keep describing
        what the card is really doing."""
        cur = self._clk_lock
        if not cur or cur.get("kind") == kind:
            return True
        ok, m = self.release_current()
        self.log(f"releasing the {self.LOCK_NAME[cur['kind']]} first - {m}", ok)
        if not ok:
            self.log("the new lock was NOT taken: the old one is still held and "
                     "two locks cannot both be tracked", False)
            return False
        self.set_lock_state(None)
        return True

    def apply_lock(self):
        if not self.guard():
            return
        mn, mx = int(dpg.get_value("lock_min")), int(dpg.get_value("lock_max"))
        if not self.handover(self.LOCK_NVML):
            return
        ok, m = self.gpu.lock_gpu_clocks(mn, mx)
        self.report((ok, m))
        if ok:
            self.set_lock_state({"kind": self.LOCK_NVML,
                                 "lo": mn, "hi": mx})

    def release_lock(self):
        """The ONE release path - Ctrl+H routes here too. Sharing the code is
        what makes it impossible for the hold banner to survive a Release (or
        to be dropped while the driver still holds the card, if the release
        fails). It now also picks WHICH driver call to make, from the record."""
        if not self.guard():
            return
        ok, m = self.release_current()
        self.report((ok, m))
        if ok:
            self.set_lock_state(None)

    def release_on_exit(self):
        """Drop whichever lock THIS app is still holding, as the window closes.
        A lock is the one write here that outlives the process: it sits in the
        driver until something resets it or the machine reboots. The NVML one is
        not even readable back on this card (nvmlDeviceGetGpuLockedClocks is
        absent), so leaving it is invisible to the next run and to every other
        tool. Every other knob this app writes is undone by 'Reset all to stock'
        from a later session; this one could not be, so it is undone here.

        Both mechanisms are covered - release_current() picks the call that
        matches the record. A V/F point lock is if anything the more important
        of the two to drop: it is the one that holds the card in true P0, so
        leaving it behind costs idle power for as long as the machine is up.

        Deliberately NOT behind guard(): the unlock gate stops the app making
        writes the user did not ask for, and this only takes back a write the app
        itself made while the gate was open. Untick the gate mid-hold, then quit,
        and the gated version would pin the card with nothing left in the app
        able to see it. A Ctrl+H hold and a Clocks-menu Lock are released alike -
        both are this app's own doing.

        Printed as well as logged: no frame renders after the loop exits, so the
        log widget is written for consistency and never appears on screen."""
        if not self._clk_lock:
            return
        what = self.LOCK_NAME[self._clk_lock["kind"]]
        ok, m = self.release_current()
        note = f"exit: releasing the {what} this app took - {m}"
        print(note)
        self.log(note, ok)
        if ok:
            self.set_lock_state(None)

    def lock_max(self):
        """Pin to the top of the driver's lockable table. Warns when that is
        BELOW what the card is currently boosting to - the lockable list and
        the V/F curve are unrelated mechanisms, so 'max' here can be a
        step down."""
        if not self.guard():
            return
        gmax = self.gpu.static.get("gfx_max")
        if not gmax:
            self.log("no lockable clock range reported by the driver", ok=False)
            return
        with self._lock:          # the poll thread owns _snap
            live = (self._snap or {}).get("core")
        if live and live > gmax:
            self.log(f"note: card is at {live} MHz, above the {gmax} MHz "
                     f"lock ceiling - locking will step it DOWN", ok=False)
        dpg.set_value("lock_min", gmax)
        dpg.set_value("lock_max", gmax)
        if not self.handover(self.LOCK_NVML):
            return
        ok, m = self.gpu.lock_gpu_clocks(gmax, gmax)
        self.report((ok, m))
        if ok:
            self.set_lock_state({"kind": self.LOCK_NVML,
                                 "lo": gmax, "hi": gmax})

    def set_lock_state(self, state):
        """Record what is holding the card now, and redraw both indicators.
        Every path that moves either driver-side lock - Lock, Lock max,
        Release, Ctrl+H, Reset all, exit - ends here, which is what stops a
        stale HOLD banner from claiming a point the card was already released
        from.

        The indicator names the MECHANISM, not just the numbers. Two different
        locks that both read 'locked' would leave the user guessing which
        Release applies, and the wrong one succeeds without doing anything."""
        self._clk_lock = state
        held = state if state and state["kind"] == self.LOCK_VF else None
        # drawn at the voltage the card is really ON, not the one requested:
        # the line is the only place the plot shows the hold, so it has to land
        # on the point the rail settled at (see hold_point)
        if dpg.does_item_exist("vf_holdline"):
            dpg.set_value("vf_holdline", [[held["got_mv"]] if held else []])
        if not dpg.does_item_exist("hold_info"):
            return
        if held:
            exact = held["got_idx"] == held["idx"]
            txt = (f"HOLD  V/F point lock on domain {held['domain']}  •  "
                   + (f"point {held['idx']} @ {held['got_mv']:.2f} mV, "
                      f"{held['got_mhz']:.0f} MHz"
                      if exact else
                      f"asked for point {held['idx']} @ {held['req_mv']:.2f} mV, "
                      f"the card holds the point at or below it: "
                      f"point {held['got_idx']} @ {held['got_mv']:.2f} mV, "
                      f"{held['got_mhz']:.0f} MHz")
                   + "   •   Ctrl+H releases")
        elif state:
            txt = (f"clock locked to [{state['lo']}..{state['hi']}] MHz from the "
                   f"Clocks menu (NVML locked clocks) - no V/F point is held")
        else:
            txt = ""
        dpg.set_value("hold_info", txt)
        dpg.configure_item("hold_info",
                           color=GOOD if (held and held["got_idx"] == held["idx"])
                           else WARN)

    def reset_all(self):
        """The ONE write that keeps its press-again arm, at the user's explicit
        call: a stray click here discards the entire tune at once - both
        offsets, voltage boost, power limit, fan and all 103 deltas - and
        unlike Apply there is no single thing on screen whose consequence a
        banner could state, because it undoes every knob on the tab."""
        if not self._reset_armed:
            self._reset_armed = True
            self.log("this zeroes offsets + voltage boost, restores the default "
                     "power limit, releases the clock lock, returns fans to auto "
                     "and resets the V/F curve - press again to confirm", False)
            return
        self._reset_armed = False
        self.autosave_before("reset-all")
        failed = 0
        # One flag per MECHANISM. reset_all releases both, and the record may
        # only be dropped when the step matching what THIS app holds succeeded
        # - clearing it because the other one worked is how the banner would
        # come down over a card that is still pinned.
        released = {}
        for step in self.gpu.reset_all():
            ok, m = step
            self.log(m, ok)
            failed += (0 if ok else 1)
            # ResetStep names the knob each step moved; the tail steps are
            # conditional, so neither release can be found by position.
            name = getattr(step, "name", None)
            if name == GPU.LOCK_STEP:
                released[self.LOCK_NVML] = ok
            elif name == GPU.VF_LOCK_STEP:
                released[self.LOCK_VF] = ok
        # A failed release that still dropped the banner would leave the driver
        # holding a card nothing on screen names, which is the exact
        # disagreement release_lock refuses to create (see its docstring). The
        # V/F step is only emitted when one was actually held, so a missing
        # entry for the recorded kind means nothing needed releasing.
        kind = (self._clk_lock or {}).get("kind")
        if kind is None or released.get(kind, True):
            self.set_lock_state(None)
        else:
            self.log(f"the {self.LOCK_NAME[kind]} was NOT released - the "
                     f"indicator stays up because the driver is still "
                     f"holding the card", False)
        st = self.gpu.static
        dpg.set_value("sl_core", 0)
        dpg.set_value("sl_mem", 0)
        dpg.set_value("sl_pl", st.get("pl_def_mw", 260000) // 1000)
        vb = self.gpu.read_voltage_boost()
        dpg.set_value("sl_volt", 0 if vb is None else max(0, min(100, vb)))
        dpg.set_value("sl_fan", st.get("fan_min", 30))
        self.log(f"reset incomplete: {failed} step(s) failed" if failed
                 else "reset to stock complete", failed == 0)
        self.vf_read(force=True)

    def refresh_control(self, d):
        c_t = d.get("core_p0max", "?")
        m_t = self.mem_fmt(d.get("mem_p0max"))[0]
        # Vcore is formatted on its own, exactly as Tk did: it needs NVAPI AND a
        # non-zero rail reading, and the app runs fine with NVAPI down. Folding
        # it into the conditional made ONE missing field blank the whole
        # readout - and this line is the only in-app confirmation that an
        # applied offset or clock lock actually took effect.
        vc = d.get("vcore_mv")
        vctxt = f"{vc:.0f}" if vc is not None else "--"
        dpg.set_value("ctl_clocks",
                      f"core {d.get('core','?')} MHz (P0 max {c_t})   "
                      f"VRAM {self.mem_fmt(d.get('mem'))[0]} MHz (P0 max {m_t})\n"
                      f"Vcore {vctxt} mV   "
                      f"volt-boost {d.get('vboost_pct','--')}%"
                      f"   P{d.get('pstate','?')}")

    # ====================================================================== #
    #  V/F CURVE                                                             #
    # ====================================================================== #
    def build_vf(self):
        """Built INSIDE the Control tab (see build_control) - not its own tab."""
        with dpg.group(horizontal=True):
            dpg.add_text("voltage cap (mV)")
            dpg.add_input_float(tag="vcap", default_value=1091.0,
                                width=self.s(130), step=6.25, format="%.2f",
                                min_value=800.0, max_value=1200.0,
                                min_clamped=True, max_clamped=True,
                                callback=self.vcap_changed)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    "Voltage ceiling to tune toward (clamped 800-1200 mV).\n"
                    "VF points are 6.25 mV apart, so the reachable top is\n"
                    "the highest point at or below this value - and editing\n"
                    "this box snaps it DOWN onto that same 6.25 mV grid, so\n"
                    "+/- always lands on a voltage a point really has.")
            dpg.add_button(label="Read curve", callback=lambda: self.vf_read(),
                           width=self.s(110))
            dpg.add_button(label="Re-phase", tag="go_rephase",
                           callback=self.vf_rephase, width=self.s(100))
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    "Puts every point back on ONE 15 MHz phase.\n\n"
                    "The clock is floor((base+delta)/15)*15, so two points\n"
                    "only move together when their deltas share a remainder\n"
                    "mod 15 MHz. A stray point crosses bin boundaries at a\n"
                    "different offset and silently re-creates a flat.\n\n"
                    "Off-phase deltas are rounded DOWN, never up, so a point\n"
                    "can only lose one bin. It is NOT a reset - if every\n"
                    "point already agrees it does nothing.")
            dpg.add_button(label="De-flatten \u2264 cap",
                           callback=self.vf_deflatten, width=self.s(150))
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    "Stages a plan (nothing is written yet):\n"
                    "makes the first point PAST the cap the UNIQUE top, so\n"
                    "the boost arbiter - which runs the LOWEST voltage of\n"
                    "any peak-frequency flat - parks there.\n"
                    "Points BELOW the cap are deliberately left alone.\n"
                    "Press Apply to GPU to write it.")
            dpg.add_button(label="Fit view", callback=self.fit_view,
                           width=self.s(90))
            dpg.add_button(label="Reset curve to stock", tag="go_vfreset",
                           callback=self.vf_reset, width=self.s(170))
        dpg.add_text("--", tag="vf_info", color=DIM, wrap=self.s(1100))
        dpg.add_text("", tag="vf_status", color=WARN, wrap=self.s(1100))

        # no anti_aliased= here: dpg.plot has no such parameter, and DPG's
        # argument parser DROPS unknown keywords instead of raising, so it
        # read as an applied setting while doing nothing. Line smoothing is
        # a per-series style, not a plot flag.
        # Pan on LEFT: it is the button a user reaches for when zoomed in, and
        # a middle-button-only pan made the plot feel frozen. on_plot_click
        # parks pan on an unused button for exactly as long as a dot is held,
        # so the two left-drag gestures never fight over the same press.
        # no_mouse_pos kills DPG's built-in cursor readout - a cursor
        # coordinate answers "where is my mouse", which is not the question
        # being asked while editing; vf_corner below answers "what am I
        # editing" in the same corner.
        with dpg.plot(tag="vf_plot", height=self.s(380), width=-1,
                      no_mouse_pos=True,
                      pan_button=dpg.mvMouseButton_Left):
            dpg.add_plot_legend()
            dpg.add_plot_axis(dpg.mvXAxis, label="mV", tag="vf_x")
            with dpg.plot_axis(dpg.mvYAxis, label="MHz", tag="vf_y"):
                dpg.add_line_series([], [], label="current", tag="vf_cur")
                dpg.add_line_series([], [], label="edited", tag="vf_edit")
                dpg.add_scatter_series([], [], label="selected",
                                       tag="vf_selpt")
                # The cap drives every plan on this tab, so it has to be
                # ON the picture: a mistyped cap that sits down in the
                # low-voltage floor is obvious as a line, invisible as a
                # number in a box.
                dpg.add_inf_line_series([1091.0], label="cap",
                                        tag="vf_capline")
                # A hold changes what the CARD does while leaving the curve
                # untouched, so nothing on this plot would move to show it.
                # Drawn at the held point's voltage, in a different colour from
                # the cap line so the two are never read as one thing.
                dpg.add_inf_line_series([], label="held", tag="vf_holdline")
            # Anchored to a corner of the VIEW, not to a data point, which is
            # why update_vf_corner has to re-place it every frame rather than
            # only on redraw: panning moves the corner, not the curve.
            dpg.add_plot_annotation(tag="vf_corner", label="",
                                    default_value=(0.0, 0.0),
                                    offset=(-self.s(8), -self.s(8)),
                                    show=False)
        # Every V/F point is a drag target, so the dots have to be big
        # enough to aim at: DPG's 4 px default disappears at 150% DPI.
        dpg.bind_item_theme("vf_cur",
                            self.series_theme(dpg.mvPlotMarker_Circle, 5, 2))
        dpg.bind_item_theme("vf_edit",
                            self.series_theme(dpg.mvPlotMarker_Circle, 6, 3))
        dpg.bind_item_theme("vf_selpt",
                            self.series_theme(dpg.mvPlotMarker_Diamond,
                                              12, 3))
        with dpg.theme() as capth:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvPlotCol_Line, WARN,
                                    category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight,
                                    self.s(2), category=dpg.mvThemeCat_Plots)
        dpg.bind_item_theme("vf_capline", capth)
        with dpg.theme() as holdth:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvPlotCol_Line, GOOD,
                                    category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight,
                                    self.s(3), category=dpg.mvThemeCat_Plots)
        dpg.bind_item_theme("vf_holdline", holdth)
        # NOTE: no per-point drag widgets - every dot stays draggable without
        # 103 separate items. The grab is a PIXEL hit test against the marker
        # (see nearest_idx): the Tk-era "nearest by voltage, anywhere on the
        # plot" rule made empty sky a drag handle for whatever point shared
        # that column, which is how a pan attempt became a 300 MHz edit.

        # THE PRE-CLICK WARNING, and the whole reason Apply is one press again.
        # It sits BETWEEN the plot and the buttons because that is the reading
        # path to the click, and it is a filled, bordered, colour-banded box
        # rather than a fourth line of text: vf_info and vf_status are both
        # status lines a few pixels above it, and a status line is exactly what
        # this must not be mistaken for. It describes the NEXT click, not the
        # last one.
        for band, (_fg, bg, border) in self.PLAN_BANDS.items():
            self._plan_themes[band] = self.plan_theme(bg, border)
        # scrollbar deliberately NOT suppressed here (the tiles do suppress
        # theirs): plan_h measures this box, and if it ever measures short the
        # tail must still be reachable rather than silently cut off.
        with dpg.child_window(tag="vf_plan", width=-1, height=self.s(130),
                              border=True, no_scroll_with_mouse=True):
            dpg.add_text("", tag="vf_plan_head", wrap=self.s(1100))
            self.bind("vf_plan_head", "big")
            dpg.add_text("", tag="vf_plan_body", color=TEXT, wrap=self.s(1100))
            dpg.add_text("", tag="vf_plan_reset", color=DIM, wrap=self.s(1100))
        self.update_plan_banner()

        with dpg.group(horizontal=True):
            dpg.add_text("selected")
            # bounded HERE, not only in vf_read: an input_int otherwise
            # carries DPG's default 0..100 unclamped until the first
            # successful read, so a curve that never reads leaves the box
            # accepting indices no VF table has. vf_read narrows this to the
            # points the card actually returned.
            dpg.add_input_int(tag="vf_idx", default_value=0,
                              width=self.s(110),
                              min_value=0, max_value=VFP_POINTS - 1,
                              min_clamped=True, max_clamped=True,
                              callback=lambda: self.vf_select(
                                  dpg.get_value("vf_idx")))
            for lbl, delta in (("-75", -75), ("-15", -15),
                               ("+15", 15), ("+75", 75)):
                # user_data carries the step: DPG passes (sender, app_data,
                # user_data) POSITIONALLY, so a default arg would be
                # clobbered by user_data=None.
                dpg.add_button(label=lbl, width=self.s(58),
                               user_data=delta,
                               callback=lambda s, a, u: self.vf_nudge(u))
            # bounded to the supported clock range for the same reason as
            # lock_min/lock_max: an input_int otherwise carries DPG's
            # default 0..100 and ignores it on entry, so the box could name
            # a frequency no VF point can hold. sync_sel_inputs seeds it
            # with the selected point on every read/select/nudge - DPG
            # clamps user entry only, never set_value.
            dpg.add_input_int(tag="vf_set", default_value=0, step=15,
                              width=self.s(120),
                              min_value=self.gpu.static.get("gfx_min", 300),
                              max_value=self.gpu.static.get("gfx_max", 2160),
                              min_clamped=True, max_clamped=True)
            dpg.add_button(label="Set MHz", width=self.s(90),
                           callback=self.vf_set_freq)
            dpg.add_button(label="Revert edits", width=self.s(120),
                           callback=self.vf_revert)
            dpg.add_button(label="Apply to GPU", tag="go_vfapply",
                           width=self.s(140), callback=self.vf_apply)
        self._ctl_widgets += ["go_rephase", "go_vfapply", "go_vfreset"]
        dpg.add_text("--", tag="vf_sel_info", color=TEXT)
        self.bind("vf_sel_info", "mono")
        dpg.add_text("drag a dot to move it  \u2022  drag anywhere else to "
                     "pan  \u2022  A/D select  \u2022  W/S move "
                     "\u00b115 MHz  \u2022  hold Shift for \u00b145  \u2022  "
                     "Ctrl+H hold the selected point (again to release)",
                     color=DIM)

        # plot-wide mouse + keyboard control. The left button is shared: the
        # plot pans with it, and these handlers steal it for a drag only while
        # the press actually landed on a dot (end_drag hands it back).
        with dpg.handler_registry():
            dpg.add_mouse_click_handler(button=dpg.mvMouseButton_Left,
                                        callback=self.on_plot_click)
            dpg.add_mouse_drag_handler(button=dpg.mvMouseButton_Left,
                                       callback=self.on_plot_drag)
            dpg.add_mouse_release_handler(button=dpg.mvMouseButton_Left,
                                          callback=self.on_plot_release)
            for key, step in ((dpg.mvKey_W, 1), (dpg.mvKey_S, -1)):
                dpg.add_key_press_handler(key, user_data=step,
                                          callback=self.on_key_move)
            for key, step in ((dpg.mvKey_A, -1), (dpg.mvKey_D, 1)):
                dpg.add_key_press_handler(key, user_data=step,
                                          callback=self.on_key_select)
            dpg.add_key_press_handler(dpg.mvKey_H, callback=self.on_key_hold)

    def wf(self, idx):
        """Working (edited) frequency in kHz for a point index."""
        p = self.vf_by_idx[idx]
        return (int(round(p["freq_mhz"] * 1000))
                + (self.vf_work[idx] - self.vf_orig[idx]))

    def vf_read(self, force=False, pts=None):
        """Rebase the editor on the hardware curve. `pts` lets a caller that has
        JUST read the curve hand those points over instead of paying for a
        second NVAPI round trip on the UI thread - and it guarantees the editor
        rebases on exactly the points the post-write prediction check was
        verified against, not on a later read that may have moved."""
        pending = sum(1 for i in self.vf_work
                      if self.vf_work.get(i) != self.vf_orig.get(i))
        if pending and not force:
            # Re-reading rebases the working copy, which would throw the edits
            # away with no undo. Make the user ask twice.
            if not self._discard_armed:
                self._discard_armed = True
                self.log(f"{pending} unapplied edit(s) would be discarded - "
                         f"press Read curve again to confirm, or Apply first",
                         False)
                return
        self._discard_armed = False
        if pts is None:
            pts, err = self.gpu.read_vf_curve()
            if err:
                self.log(err, False)
                return
        self.vf_points = pts
        self.vf_by_idx = {p["idx"]: p for p in pts}
        self.vf_orig = {p["idx"]: p["delta_khz"] for p in pts}
        self.vf_work = dict(self.vf_orig)
        # also reseed when a re-read comes back WITHOUT the selected index: wf()
        # is a bare dict lookup, so a stale selection raises KeyError out of
        # sync_sel_inputs below and aborts vf_read before the redraw, the axis
        # clamp and the success log - DPG swallows that to stderr, so the tab
        # would just freeze mid-update with nothing in the log.
        if self.vf_sel is None or self.vf_sel not in self.vf_by_idx:
            self.vf_sel = pts[len(pts) // 2]["idx"]
        # the index box may only name a point that EXISTS: an input_int carries
        # DPG's default 0..100 bounds (wrong for a 103-point curve) and ignores
        # them on entry, and an index the curve does not have would leave the
        # box showing one point while every nudge drove another
        if dpg.does_item_exist("vf_idx"):
            dpg.configure_item("vf_idx", min_value=min(self.vf_by_idx),
                               max_value=max(self.vf_by_idx),
                               min_clamped=True, max_clamped=True)
        peak, pidx, pmv, _npk = GPU.peak_info(pts)
        cap = dpg.get_value("vcap")
        flats = self.count_flats(pts, cap)
        self.sync_sel_inputs()
        self.vf_redraw()          # rewrites vf_info from the working copy
        # constrain panning, but auto-fit only ONCE: refitting on every read
        # (and after Re-phase / Apply) threw the user's zoom away
        self.clamp_axes(fit=not self._fitted)
        self._fitted = True
        self.log(f"curve read: peak {peak:.0f} MHz, parks idx {pidx} @ "
                 f"{pmv:.2f} mV, {flats} flat run(s) below {cap:.0f} mV", True)

    @staticmethod
    def count_flats(pts, cap):
        """Number of flat runs below the cap - the diagnostic that says whether
        de-flatten has anything to do, and how many bins it can win back.
        Same walk as the Tk build so the number means the same thing."""
        flats, run = 0, 1
        for a, b in zip(pts, pts[1:]):
            if below_cap(b["volt_mv"], cap) and b["freq_mhz"] <= a["freq_mhz"]:
                run += 1
            else:
                flats, run = flats + (1 if run > 1 else 0), 1
        return flats + (1 if run > 1 else 0)

    def fit_view(self):
        self.clamp_axes(fit=True)

    # Y pan/zoom bounds. These are NOT derived from the curve, and that is the
    # whole point: get_plot_mouse_pos - a drag's only source of position - is
    # bounded by the visible axis range, so a ceiling of "highest point + 200"
    # is also a hard CLOCK ceiling, and one that shrinks to whatever the curve
    # happens to be right now. A Titan RTX clears 2300 MHz under LN2, so the
    # headroom is fixed and generous; the constraint still exists only to stop
    # the pan-to-infinity that made the plot impossible to get back.
    # (The write path was never the limit - apply_vf_deltas takes +/-1 GHz.)
    VF_Y_CEIL = 3000.0
    VF_Y_FLOOR = 0.0

    def clamp_axes(self, fit=False):
        """Stop the plot being dragged off to infinity: constrain pan/zoom to a
        little beyond the actual data horizontally, and to LN2-scale headroom
        vertically. Re-run on every redraw so a curve edited ABOVE the fixed
        ceiling carries the ceiling up with it instead of hitting a wall."""
        if not self.vf_points:
            return
        xs = [p["volt_mv"] for p in self.vf_points]
        ys = [self.wf(p["idx"]) / 1000.0 for p in self.vf_points]
        x0, x1 = min(xs) - 25, max(xs) + 25
        y0 = min(self.VF_Y_FLOOR, min(ys) - 120)
        y1 = max(self.VF_Y_CEIL, max(ys) + 200)
        try:
            # Constraints bound how far the view may pan/zoom but still allow
            # zooming; hard set_axis_limits would freeze the view entirely.
            dpg.set_axis_limits_constraints("vf_x", x0, x1)
            dpg.set_axis_limits_constraints("vf_y", y0, y1)
            dpg.set_axis_zoom_constraints("vf_x", 20, x1 - x0)
            dpg.set_axis_zoom_constraints("vf_y", 100, y1 - y0)
            if fit:
                dpg.fit_axis_data("vf_x")
                dpg.fit_axis_data("vf_y")
            self.clear_once("axisclamp")
        except Exception as e:
            # deduplicated now that this runs on every redraw: a drag would
            # otherwise write ~60 identical failures a second into the log
            self.log_once("axisclamp", f"axis clamp: {e}")

    def work_pts(self):
        """The WORKING curve in read_vf_curve() shape, so the planner and every
        summary see exactly what the 'edited' line on the plot shows. Planning
        off a fresh hardware read instead would write a curve the user was never
        shown, and then discard the edits that were."""
        return [{"idx": i, "volt_mv": self.vf_by_idx[i]["volt_mv"],
                 "freq_mhz": self.wf(i) / 1000, "delta_khz": self.vf_work[i]}
                for i in sorted(self.vf_work)]

    def update_vf_info(self):
        """Ceiling / peak / park / flat-run summary of the curve ON SCREEN.
        Recomputed from the WORKING copy on every redraw, the way Tk's editor
        rebuilt it on every _draw: these two numbers decide where the boost
        arbiter parks, and left frozen at the last hardware read they describe
        the curve the staged edits replaced - the user would commit a write
        judging it by a pre-edit park point."""
        if not self.vf_points:
            return
        pts = self.work_pts()
        cap = float(dpg.get_value("vcap"))
        peak, pidx, pmv, npk = GPU.peak_info(pts)
        under = [p for p in pts if below_cap(p["volt_mv"], cap)]
        top = under[-1] if under else None
        pend = sum(1 for i in self.vf_work
                   if self.vf_work[i] != self.vf_orig[i])
        dpg.set_value("vf_info",
                      f"{len(pts)} points   "
                      + (f"top ≤cap: idx {top['idx']} @ "
                         f"{top['volt_mv']:.2f} mV = {top['freq_mhz']:.0f} MHz   "
                         if top else "")
                      + f"peak {peak:.0f} MHz held by {npk} point(s), lowest = "
                        f"idx {pidx} @ {pmv:.2f} mV (where the card parks)   "
                      + f"flat runs below cap: {self.count_flats(pts, cap)}"
                      + (f"   [staged curve, {pend} edit(s) not yet written]"
                         if pend else ""))
        dpg.configure_item("vf_info", color=WARN if pend else DIM)

    VCAP_STEP = 6.25        # the VF table's own voltage spacing

    def vcap_changed(self, sender=None, app_data=None, user_data=None):
        """Snap the cap onto the 6.25 mV VF-point grid. The +/- buttons step by
        6.25 mV from whatever is in the box, and the 1091.0 default is the
        observed rail-lock value, not a point - so unsnapped stepping walks
        1097.25, 1103.50 ... and never names a voltage this curve has.

        DOWNWARD, like every other snap in this app: below_cap() already
        resolves a cap to the highest point at or below it, so flooring makes
        the number in the box the cap that is actually planned against (1091 ->
        1087.50, the very point the Info tab says 1091 lands on) and a typo can
        only ever ask for LESS voltage than typed, never more. The widget's own
        800-1200 clamp still holds: both ends are multiples of the step."""
        v = float(dpg.get_value("vcap"))
        # epsilon: float slop must not drop a value that IS on the grid to the
        # point below it, which would make every keystroke walk the cap down
        snapped = math.floor(v / self.VCAP_STEP + 1e-9) * self.VCAP_STEP
        if abs(snapped - v) > 1e-6:
            dpg.set_value("vcap", snapped)
        self.vf_redraw()

    def vf_redraw(self):
        if dpg.does_item_exist("vf_capline"):
            dpg.set_value("vf_capline", [[float(dpg.get_value("vcap"))]])
        # BEFORE the early return: every path that changes what Apply would
        # write ends here, and the banner is the only thing standing between a
        # staged plan and a single click - it may never be one redraw stale.
        self.update_plan_banner()
        if not self.vf_points:
            return
        xs = [p["volt_mv"] for p in self.vf_points]
        cur = [self.vf_by_idx[p["idx"]]["freq_mhz"] for p in self.vf_points]
        edit = [self.wf(p["idx"]) / 1000.0 for p in self.vf_points]
        dpg.set_value("vf_cur", [xs, cur])
        dpg.set_value("vf_edit", [xs, edit])
        self.update_vf_info()
        # The pan bound is what a drag can reach, so it has to follow the
        # WORKING curve as it is edited, not sit where the last hardware read
        # left it - otherwise the ceiling goes stale the moment editing starts.
        self.clamp_axes()
        if self.vf_sel is not None and self.vf_sel in self.vf_by_idx:
            p = self.vf_by_idx[self.vf_sel]
            dpg.set_value("vf_selpt",
                          [[p["volt_mv"]], [self.wf(self.vf_sel) / 1000.0]])

            pend = sum(1 for i in self.vf_work
                       if self.vf_work[i] != self.vf_orig[i])
            dpg.set_value(
                "vf_sel_info",
                f"idx {self.vf_sel}   {p['volt_mv']:.2f} mV   "
                f"{self.wf(self.vf_sel)/1000:.0f} MHz   "
                f"delta {self.vf_work[self.vf_sel]/1000:+.0f} MHz   |   "
                f"edits pending: {pend}")

    def update_vf_corner(self):
        """Put the SELECTED point in the plot's bottom-right corner, where DPG
        used to print the mouse position. Called per frame, not per redraw: the
        anchor is the corner of the VIEW, so every pan and zoom moves it while
        the curve and the selection stand still."""
        if not dpg.does_item_exist("vf_corner"):
            return
        if (self.vf_sel is None or self.vf_sel not in self.vf_by_idx
                or self.vf_sel not in self.vf_work):
            dpg.configure_item("vf_corner", show=False)
            return
        try:
            x1 = dpg.get_axis_limits("vf_x")[1]
            y0 = dpg.get_axis_limits("vf_y")[0]
        except Exception:
            return
        p = self.vf_by_idx[self.vf_sel]
        txt = (f"idx {self.vf_sel}   {p['volt_mv']:.2f} mV   "
               f"{self.wf(self.vf_sel) / 1000:.0f} MHz   "
               f"delta {self.vf_work[self.vf_sel] / 1000:+.0f}")
        # clamped=True keeps the label inside the plot once it is anchored to
        # the corner, so the offset only has to lift it off the axis lines
        if dpg.get_item_label("vf_corner") != txt:
            dpg.configure_item("vf_corner", label=txt)
        if not dpg.is_item_shown("vf_corner"):
            dpg.configure_item("vf_corner", show=True)
        dpg.set_value("vf_corner", (x1, y0))

    SHIFT_MULT = 3          # hold Shift to move 3 bins at a time

    def typing(self):
        """True while a text/number box has focus, so W/A/S/D typed into an
        input box never also retunes the curve."""
        return any(dpg.does_item_exist(t)
                   and (dpg.is_item_focused(t) or dpg.is_item_active(t))
                   for t in ("vcap", "vf_idx", "vf_set", "lock_min", "lock_max",
                             "log", "info", "prof_name"))

    def plot_units_per_px(self):
        """(mV per pixel, MHz per pixel) for the V/F plot AS CURRENTLY VIEWED,
        or None before the plot has a size. DPG exposes no plot<->pixel
        transform, so derive one: the axis limits are what the plot's rect
        shows. get_item_rect_size covers the axis labels and tick text as well
        as the data area, so the rect is a few percent too BIG and the returned
        units-per-pixel a few percent too SMALL - which makes every distance
        converted with it read a few percent too LARGE. The grab test is
        therefore slightly STRICTER than its nominal radius, never more
        forgiving, and that is the safe direction for a test whose false
        positive is an unintended edit."""
        try:
            w, h = dpg.get_item_rect_size("vf_plot")[:2]
            x0, x1 = dpg.get_axis_limits("vf_x")[:2]
            y0, y1 = dpg.get_axis_limits("vf_y")[:2]
        except Exception:
            return None
        if w <= 0 or h <= 0 or x1 <= x0 or y1 <= y0:
            return None
        return (x1 - x0) / w, (y1 - y0) / h

    # Grab radius, in pixels before DPI scaling. The dot it aims at is drawn at
    # self.s(6), so this forgives a shaky 4K cursor without reaching the
    # next dot along - VF points are 6.25 mV apart, which is far wider than
    # this at any zoom that shows fewer than ~150 points.
    GRAB_PX = 14

    def nearest_idx(self, volt_mv, freq_mhz):
        """The point the cursor is actually ON, or None if it is on none.

        Voltage alone is not a hit test. The columns are 6.25 mV apart while
        the plot is ~1000 MHz tall, so "nearest by mV" matches a click anywhere
        in a column - empty sky included - and the caller then yanks that point
        to the cursor. Two accidental multi-hundred-MHz edits came from exactly
        that. Distance therefore has to be measured in PIXELS: the two axes'
        units are not comparable, and only the pixel view is what the user is
        aiming at."""
        if not self.vf_points:
            return None
        scale = self.plot_units_per_px()
        if scale is None:
            return None
        xu, yu = scale

        def dpx(p):
            return (abs(p["volt_mv"] - volt_mv) / xu,
                    abs(self.wf(p["idx"]) / 1000.0 - freq_mhz) / yu)

        near = min(self.vf_points, key=lambda p: math.hypot(*dpx(p)))
        # a true RADIUS, which is what the tooltip, the shortcut list and the
        # README all promise. `dx <= r and dy <= r` is a SQUARE, and its corners
        # reach 1.41x the stated distance - a grab the words on screen say is a
        # miss.
        return (near["idx"] if math.hypot(*dpx(near)) <= self.s(self.GRAB_PX)
                else None)

    def on_plot_click(self, sender=None, app_data=None, user_data=None):
        """Left-click ON a dot selects it and begins a drag. A press that lands
        anywhere else is left alone for the plot to pan with.

        EVERY exit that does not take a grab clears the drag state first. A new
        press ends whatever the last one held, and a _drag_idx surviving a MISS
        is the accidental-edit bug the pixel hit test was added to prevent,
        reached by another door: click empty sky, then drag anywhere, and the
        previously grabbed point is yanked to the cursor."""
        if not self.vf_points or not dpg.is_item_hovered("vf_plot"):
            self.end_drag()
            return
        try:
            x, y = dpg.get_plot_mouse_pos()[:2]
        except Exception:
            self.end_drag()
            return
        idx = self.nearest_idx(x, y)
        if idx is None:
            self.end_drag()
            return
        self._drag_idx = idx
        # Left now belongs to the pan, so it has to be taken away for the life
        # of the grab or the curve and the view would move together. X2 is a
        # button this app never uses, i.e. a pan that cannot be triggered.
        self.set_pan_button(dpg.mvMouseButton_X2)
        self.vf_select(idx)

    def on_plot_drag(self, sender=None, app_data=None, user_data=None):
        """Move the grabbed dot vertically. Voltage is fixed by the VF table, so
        only Y matters, and it snaps to whole 15 MHz bins."""
        if self._drag_idx is None or not self.vf_points:
            return
        try:
            x, y = dpg.get_plot_mouse_pos()[:2]
        except Exception:
            return
        self.set_work_freq(self._drag_idx, y * 1000.0)
        self.sync_sel_inputs()
        self.vf_redraw()

    def on_plot_release(self, sender=None, app_data=None, user_data=None):
        self.end_drag()

    def set_pan_button(self, button):
        if not dpg.does_item_exist("vf_plot"):
            return
        try:
            dpg.configure_item("vf_plot", pan_button=button)
        except Exception as e:
            self.log_once("panbtn", f"plot pan button: {e}")

    def end_drag(self):
        """The one way out of a dot drag. The pan button is a MODE, not an
        event, so it is restored UNCONDITIONALLY here rather than only when a
        drag was in flight: this handler is registry-wide, so it also catches
        the release that happens outside the plot, and a single missed restore
        would leave the plot permanently unpannable."""
        self._drag_idx = None
        self.set_pan_button(dpg.mvMouseButton_Left)

    def drag_watchdog(self):
        """End a grab whose mouse-up never arrived. This CANNOT live inside
        on_plot_drag, where it used to: DPG's mvMouseDragHandler is
        ImGui::IsMouseDragging(button), which already requires MouseDown[button]
        - so a 'the button is up' test there is false by construction and the
        branch was dead. The case that really strands a grab is focus loss,
        where ImGui clears MouseDown without ever delivering a release; that
        stops the drag handler firing at all, so the check has to run per frame
        instead. Left unfixed, the plot stays permanently unpannable (pan is
        parked on X2 for the life of the grab)."""
        if self._drag_idx is None:
            return
        try:
            down = dpg.is_mouse_button_down(dpg.mvMouseButton_Left)
        except Exception:
            return
        if not down:
            self.end_drag()

    def on_key_move(self, sender=None, app_data=None, user_data=None):
        """W / S nudge the selected dot by one 15 MHz bin (x3 with Shift)."""
        if self.vf_sel is None or not self.vf_points or self.typing():
            return
        mult = self.SHIFT_MULT if dpg.is_key_down(dpg.mvKey_ModShift) else 1
        self.vf_nudge(int(user_data or 1) * (VF_STEP_KHZ // 1000) * mult)

    def on_key_select(self, sender=None, app_data=None, user_data=None):
        """A / D step the selection along the curve (x3 with Shift)."""
        if self.vf_sel is None or not self.vf_points or self.typing():
            return
        mult = self.SHIFT_MULT if dpg.is_key_down(dpg.mvKey_ModShift) else 1
        order = [p["idx"] for p in self.vf_points]
        try:
            pos = order.index(self.vf_sel)
        except ValueError:
            return
        pos = max(0, min(len(order) - 1, pos + int(user_data or 1) * mult))
        self.vf_select(order[pos])

    # ---- hold this point (Ctrl+H) ----------------------------------------- #
    HOLD_REPEAT_S = 0.4

    def on_key_hold(self, sender=None, app_data=None, user_data=None):
        """Ctrl+H. The registry is window-wide, so both guards matter: a bare H
        must not pin the clock, and Ctrl+H typed into a number box must not
        either (same rule as W/A/S/D)."""
        if self.typing() or not dpg.is_key_down(dpg.mvKey_ModCtrl):
            return
        # DPG's key-press handler is ImGui::IsKeyPressed(key) with repeat ON, so
        # a key leaned on auto-repeats ~20x/sec after 275 ms. That is what W/S
        # want; here every fire is a driver write, and the repeat would toggle
        # the clock lock on and off twenty times a second. One deliberate press,
        # one toggle.
        now = time.monotonic()
        if now - self._hold_t < self.HOLD_REPEAT_S:
            return
        self._hold_t = now
        self.hold_toggle()

    def lockable_list(self):
        """Graphics clocks nvmlDeviceSetGpuLockedClocks will accept, from the
        TOP memory-clock row - the row lock_gpu_clocks validates against, since
        static['gfx_min'/'gfx_max'] are read from that same row. Cached: it is
        one NVML enumeration per memory state, it cannot change while the driver
        is loaded, and this runs on the UI thread from a keystroke.

        Empty is not cached (`not`, not `is None`): a driver that failed to
        enumerate would otherwise refuse every hold for the rest of the run."""
        if not self._lockable:
            rows = self.gpu.lockable_clocks_by_mem()
            self._lockable = sorted(max(rows, key=lambda r: r[0])[1]) \
                if rows else []
        return self._lockable

    def snap_lockable(self, mhz):
        """Highest lockable clock at or below `mhz`, or None if there is none.
        DOWN only, like every other snap here: a V/F point's frequency is often
        not IN the lockable table at all (this card's curve reaches 2175 MHz
        against a 2160 MHz table), and a request may lose a bin but must never
        gain clock nobody asked for.

        NOT on the Ctrl+H path any more. The hold moved to the V/F point lock,
        which takes a VOLTAGE and lets the hardware resolve it, so there is
        nothing to snap; this stays as the NVML mechanism's own rule, where the
        lockable table is what the request has to land in. Nothing calls it at
        present - kept rather than deleted because the rule is the correct one
        for that mechanism and re-deriving it later would be re-deriving a
        measurement (see 'Lockable clocks are not a ceiling' in README)."""
        below = [c for c in self.lockable_list() if c <= mhz]
        return max(below) if below else None

    def hold_toggle(self):
        if self._clk_lock and self._clk_lock["kind"] == self.LOCK_VF:
            # straight through the Release button's own handler: one release
            # path means the banner and the driver cannot end up disagreeing
            self.release_lock()
        else:
            self.hold_point()

    def hold_point(self):
        """TitanTune's answer to Afterburner's Ctrl+L curve lock: pin the card
        onto the selected V/F point with the per-domain V/F point lock (NvAPI
        setter 0x39442CFB over the getter 0xE440B867's own buffer).

        This is a VOLTAGE request, not a frequency one, so there is no
        snap-to-a-lockable-clock step here: the hardware resolves the request
        itself, onto the highest point at or below it. The old
        nvmlDeviceSetGpuLockedClocks path is still what the Clocks menu drives,
        but it is the weaker hold - measured on this card, it leaves memory at
        810 MHz on an idle card, where the V/F lock keeps true P0 (mem 7000) at
        ~5% utilisation.

        What the hardware delivers can sit BELOW what was asked for, twice
        over: the request resolves DOWN to a point, and the arbiter then runs
        that point's frequency at the lowest voltage carrying it. So nothing
        here claims the selected point was held - it reads back and reports the
        point the card is really on (see GPU.resolve_vf_point)."""
        if not self.guard():
            return
        if self.vf_sel is None or self.vf_sel not in self.vf_by_idx:
            self.log("hold: no point selected - read the curve first", False)
            return
        idx = self.vf_sel
        p = self.vf_by_idx[idx]
        # the point's own voltage, so the "at or below" rule resolves back onto
        # the point that was picked. Voltage is fixed by the VF table and the
        # editor cannot move it, so unlike the old frequency-based hold this
        # number can never be a staged edit.
        req_mv = p["volt_mv"]
        req_uv = int(round(req_mv * 1000))
        if not self.handover(self.LOCK_VF):
            return
        ok, m = self.gpu.set_vf_lock(req_uv)
        if not ok:
            self.log(m, False)
            return
        # the driver echoes the REQUEST back, so this read-back proves only
        # that the lock is ours and still in force - which is the thing worth
        # proving on a machine where another tuner may be re-asserting its own
        st = self.gpu.read_vf_lock()
        if st is None:
            self.log("hold: the write was accepted but the card now reports no "
                     "V/F lock - something else took it back", False)
            self.set_lock_state(None)
            return
        # Where the card actually ends up is derived from the curve, because
        # the lock struct cannot say (see GPU.resolve_vf_point). Resolved
        # against the curve the PLOT is showing, not a fresh read, so the
        # banner and the picture agree. The point identity that comes out of
        # this is exact either way - point voltages are fixed on the 6.25 mV
        # grid and never move - but the MHz is only as fresh as the last
        # 'Read curve': the driver re-evaluates the curve with temperature, and
        # a cool card was measured a whole 15 MHz bin above a warm one.
        got = GPU.resolve_vf_point(self.vf_points or [], st["volt_mv"])
        if got is None:
            # only reachable if the curve went empty under us - the request is
            # a point's own voltage, so it normally resolves back onto that
            # point at worst. Fall back to naming what was ASKED for, and say
            # that is what the readout now means.
            self.log(f"held at {st['volt_mv']:.2f} mV, but no point on the curve "
                     f"sits at or below that - the readout below names the "
                     f"point requested, not a point read back", False)
            got = p
        self.set_lock_state({"kind": self.LOCK_VF,
                             "idx": idx, "req_mv": req_mv,
                             "domain": st["domain"],
                             "got_idx": got["idx"], "got_mv": got["volt_mv"],
                             "got_mhz": got["freq_mhz"]})
        if got["idx"] != idx:
            self.log(f"asked for point {idx} @ {req_mv:.2f} mV; the card holds "
                     f"point {got['idx']} @ {got['volt_mv']:.2f} mV "
                     f"({got['freq_mhz']:.0f} MHz) - the highest point at or "
                     f"below the request", None)
        if self.vf_work.get(idx) != self.vf_orig.get(idx):
            self.log(f"note: point {idx} has a staged edit that is not in the "
                     f"card yet - the frequency held is the one the card's "
                     f"curve carries at this voltage", None)
        self.log(f"holding point {got['idx']} @ {got['volt_mv']:.2f} mV = "
                 f"{got['freq_mhz']:.0f} MHz via the V/F point lock on domain "
                 f"{st['domain']}. Ctrl+H releases", True)

    def vf_select(self, idx):
        if not self.vf_points:
            return
        if idx not in self.vf_by_idx:
            # An index the curve does not carry must not be left sitting in the
            # box: it would name one point while the nudge buttons and Set MHz
            # drove the one that is really selected. Put the truth back.
            self.sync_sel_inputs()
            self.log_once("vf_idx", f"idx {idx} is not on this curve - "
                                    f"selection stays on idx {self.vf_sel}")
            return
        self.clear_once("vf_idx")
        self.vf_sel = idx
        self.sync_sel_inputs()
        self.vf_redraw()

    def sync_sel_inputs(self):
        """Keep the index box and the Set-MHz box showing the SELECTED point.
        Without this the boxes read 0 while another point is selected, and
        'Set MHz' would drive the point to the bottom of its range."""
        # membership, not just None: wf() indexes vf_by_idx directly, and this
        # runs from inside vf_read, where a KeyError would abort the rebase.
        if self.vf_sel is None or self.vf_sel not in self.vf_by_idx:
            return
        if dpg.does_item_exist("vf_idx"):
            dpg.set_value("vf_idx", self.vf_sel)
        if dpg.does_item_exist("vf_set"):
            dpg.set_value("vf_set", int(round(self.wf(self.vf_sel) / 1000)))

    def set_work_freq(self, idx, target_khz):
        """Only ever move a delta by WHOLE 15 MHz bins: the driver evaluates
        floor((base+delta)/15)*15 and `base` has an unknowable sub-15 remainder,
        so an absolute target lands mid-bin and silently floors."""
        step = VF_STEP_KHZ
        d0 = self.vf_orig[idx]
        lim = GPU.MAX_ABS_DELTA_KHZ
        base_f = int(round(self.vf_by_idx[idx]["freq_mhz"] * 1000))
        # Round half-UP, not Python's round(): round() is half-to-EVEN, so a
        # drag that lands exactly between two bins snaps up or down depending on
        # the parity of the neighbouring bin - the same gesture at two places on
        # the curve moves by different amounts. The plot drag feeds arbitrary
        # floats, so this is live, not theoretical. Same rule as the Tk editor.
        bins = int(math.floor((target_khz - base_f) / step + 0.5))
        if bins > 0:
            bins = min(bins, (lim - d0) // step)
        elif bins < 0:
            bins = max(bins, -((lim + d0) // step))
        self.vf_work[idx] = int(d0 + bins * step)

    def vf_nudge(self, mhz):
        if self.vf_sel is None or not self.vf_points:
            return
        self.set_work_freq(self.vf_sel, self.wf(self.vf_sel) + int(mhz) * 1000)
        self.sync_sel_inputs()
        self.vf_redraw()

    def vf_set_freq(self):
        if self.vf_sel is None or not self.vf_points:
            return
        want_mhz = dpg.get_value("vf_set")
        lo = self.gpu.static.get("gfx_min", 300)
        hi = self.gpu.static.get("gfx_max", 2160)
        if not (lo <= want_mhz <= hi):
            self.log(f"Set MHz: {want_mhz} is outside the supported "
                     f"{lo}-{hi} MHz range", False)
            return
        want = want_mhz * 1000
        base_f = int(round(self.vf_by_idx[self.vf_sel]["freq_mhz"] * 1000))
        bins = int(math.floor((want - base_f) / VF_STEP_KHZ))
        self.set_work_freq(self.vf_sel, base_f + bins * VF_STEP_KHZ)
        # write the LANDED frequency back into the box: the request is floored to
        # a bin, so leaving the asked-for number sitting there would make the
        # box disagree with the point it names
        self.sync_sel_inputs()
        self.vf_redraw()
        self.log(f"idx {self.vf_sel}: asked {want/1000:.0f} -> landed "
                 f"{self.wf(self.vf_sel)/1000:.0f} MHz (15 MHz grid)")

    def vf_revert(self):
        self.vf_work = dict(self.vf_orig)
        # the boxes have to follow the working copy back, or Set-MHz still holds
        # the reverted frequency and one click silently re-applies the edit that
        # was just undone
        self.sync_sel_inputs()
        self.vf_redraw()

    @staticmethod
    def curve_top(pts, cap):
        """(top ≤cap, peak) in MHz. ONE definition of both words for every
        message on this tab. They used to be measured in two places under the
        same name 'ceiling' - the planner reports it at the boundary point, one
        VF point PAST the cap, while Apply measured at/below the cap - so the
        same staged plan printed two different MHz numbers and neither line said
        which it meant."""
        top = max((p["freq_mhz"] for p in pts if below_cap(p["volt_mv"], cap)),
                  default=0.0)
        return top, max((p["freq_mhz"] for p in pts), default=0.0)

    # ---- the plan banner (the warning that arrives BEFORE the click) ------- #
    # (head colour, box fill, border) per band. The FILL is the point: colour
    # alone would just make this a fourth coloured status line on a tab that
    # already has three.
    PLAN_BANDS = {
        "idle": (DIM, (32, 35, 42, 255), (72, 78, 92, 255)),
        "warn": (WARN, (58, 44, 10, 255), WARN),
        "bad": (BAD, (66, 20, 20, 255), BAD),
    }

    def plan_theme(self, bg, border):
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, bg)
                dpg.add_theme_color(dpg.mvThemeCol_Border, border)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, self.s(2))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,
                                    self.s(12), self.s(8))
        return th

    def plan_wrap(self):
        try:
            W = dpg.get_viewport_client_width()
        except Exception:
            W = 0
        # wider inset than the page's own wrap: this text is inside a padded,
        # bordered box, so wrapping it at the page width would clip the tail
        return max(self.s(300), W - self.s(80))

    def plan_h(self):
        """Measured height of the plan banner, the way tile_height measures a
        tile. The text is rebuilt on every edit and it wraps, so a fixed height
        would either clip the peak-lowering warning - the one line in this app
        that must never go unread - or leave a slab of empty colour under a
        single short line."""
        wrap = self.plan_wrap()
        lh = self.text_h("Ag", "ui") or self.s(19)
        # generous: get_text_size can read a hair short, and this box is the one
        # place in the app where a few pixels of over-measure costs nothing and
        # an under-measure hides a warning
        h = self.s(30)          # window padding + border, top and bottom
        for tag, font in (("vf_plan_head", "big"), ("vf_plan_body", "ui"),
                          ("vf_plan_reset", "ui")):
            if not dpg.does_item_exist(tag):
                continue
            fallback = self.s(31) if font == "big" else lh
            txt = dpg.get_value(tag) or "Ag"
            h += (self.text_h(txt, font, wrap) or fallback) + self.s(5)
        return int(h)

    def size_plan_banner(self):
        """Re-wrap and re-measure. Called from relayout (the window changed
        width) AND from update_plan_banner (the text changed): at 4 Hz the
        relayout tick alone would leave a freshly grown warning clipped for a
        quarter second, which is a quarter second in which the box is lying."""
        if not dpg.does_item_exist("vf_plan"):
            return
        wrap = self.plan_wrap()
        for tag in ("vf_plan_head", "vf_plan_body", "vf_plan_reset"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, wrap=wrap)
        dpg.configure_item("vf_plan", height=self.plan_h())

    def apply_plan(self):
        """Exactly what 'Apply to GPU' would write RIGHT NOW, or None when
        nothing is staged. ONE computation with two consumers - the banner that
        has to be true before the click, and the log receipt written at the
        click - so the description the user acted on and the description in the
        log can never be two different plans."""
        if not self.vf_points:
            return None
        changed = {i: self.vf_work[i] for i in self.vf_work
                   if self.vf_work[i] != self.vf_orig[i]}
        if not changed:
            return None
        cap = float(dpg.get_value("vcap"))
        wpts = self.work_pts()
        top, peak = self.curve_top(wpts, cap)
        _hw_top, hw_peak = self.curve_top(self.vf_points, cap)
        _pk, pidx, pmv, npk = GPU.peak_info(wpts)
        # A plan that drags the peak DOWN (a cap that landed in the low-voltage
        # floor levels the whole upper curve onto it) otherwise reads exactly
        # like a raise - it was the case Tk's confirm dialog existed to catch.
        return {
            "changed": changed,
            "lowers_peak": peak < hw_peak,
            "warn": (f"WARNING: this LOWERS the curve's peak from "
                     f"{hw_peak:.0f} to {peak:.0f} MHz. "
                     if peak < hw_peak else ""),
            "text": (f"{len(changed)} edited point(s): top ≤{cap:.0f} mV "
                     f"becomes {top:.0f} MHz, peak {peak:.0f} MHz held by "
                     f"{npk} point(s), the card parks at idx {pidx} @ "
                     f"{pmv:.2f} mV"),
        }

    def update_plan_banner(self):
        """Say what the NEXT click on Apply does, continuously. This is the
        whole exchange for single-click Apply: the summary that used to arrive
        with the first of two presses is now on screen before either."""
        if not dpg.does_item_exist("vf_plan_head"):
            return
        plan = self.apply_plan()
        pending = len(plan["changed"]) if plan else 0
        if plan is None:
            band = "idle"
            head = "APPLY TO GPU  ·  nothing staged"
            body = ("Drag a dot, nudge with W/S, Set MHz or De-flatten, and "
                    "this box says exactly what one click on 'Apply to GPU' "
                    "will write - before the click, not after it.")
        else:
            band = "bad" if plan["lowers_peak"] else "warn"
            head = ("APPLY TO GPU  ·  ONE CLICK LOWERS THE PEAK"
                    if plan["lowers_peak"] else
                    "APPLY TO GPU  ·  ONE CLICK WRITES THIS")
            # The undo-point sentence is the pre-click half of the bargain that
            # bought single-click Apply, so it may only promise what the click
            # really delivers: the snapshot is ATTEMPTED first, and if it comes
            # back without the delta table (or not at all) the status line
            # below says so in red and the write still goes ahead. Promising a
            # restore here that autosave_before then could not take would be
            # the one lie this box cannot afford.
            body = (plan["warn"] + "Writes " + plan["text"]
                    + ".\nAn undo point is taken immediately before the write "
                      "and Profiles > Undo last write puts this state back - "
                      "unless the status line reports that the snapshot came "
                      "back incomplete, in which case the write still happens "
                      "and the curve is NOT recoverable from it. Zeroing the "
                      "table is always available via 'Reset curve to stock' "
                      "or a reboot.")
        reset = (f"'Reset curve to stock' is one click too: it zeroes all "
                 f"{VFP_POINTS} deltas back to the factory curve and discards "
                 f"{pending} staged edit(s).")
        dpg.set_value("vf_plan_head", head)
        dpg.set_value("vf_plan_body", body)
        dpg.set_value("vf_plan_reset", reset)
        # re-theme only on a band change, same reason as the bars and the
        # domain deltas: rebinding a theme every frame is pure churn, and this
        # runs from vf_redraw, i.e. on every frame of a drag
        if self._plan_band != band:
            self._plan_band = band
            dpg.configure_item("vf_plan_head", color=self.PLAN_BANDS[band][0])
            if band in self._plan_themes:
                dpg.bind_item_theme("vf_plan", self._plan_themes[band])
        self.size_plan_banner()

    def vf_apply(self):
        """ONE CLICK. The plan banner directly above this button has been
        saying what the click writes for as long as the edits have existed
        (update_plan_banner), and autosave_before makes the write recoverable -
        which is strictly more than the press-again arm gave, since that only
        described the plan once the user had already committed to pressing."""
        if not self.guard() or not self.vf_points:
            return
        plan = self.apply_plan()
        if plan is None:
            self.log("no edits to apply")
            return
        changed = plan["changed"]
        predicted = {i: self.wf(i) for i in changed}
        self.autosave_before("vf-apply")
        # the log is the only receipt a write leaves anywhere in the app, so
        # the plan goes into it at the moment of commit as well as standing in
        # the banner beforehand - and it lands directly under the undo point
        # that would take it back
        self.log(plan["warn"] + "writing " + plan["text"],
                 False if plan["lowers_peak"] else None)
        ok, m = self.gpu.apply_vf_deltas(changed)
        self.report((ok, m))
        if not ok:
            return
        pts, err = self.gpu.read_vf_curve()
        if err:
            # Re-read failed AFTER a successful write: advance BOTH baselines
            # together, or the frequency anchor goes stale while the delta
            # baseline moves and wf() stops describing the hardware. The curve
            # on screen is now predicted, not measured - say so.
            self.log(f"post-write curve re-read failed: {err} - displayed "
                     f"values are predicted, not measured", False)
            for i, f in predicted.items():
                self.vf_by_idx[i]["freq_mhz"] = f / 1000.0
            self.vf_orig = dict(self.vf_work)
            self.vf_redraw()
            return
        actual = {p["idx"]: int(round(p["freq_mhz"] * 1000)) for p in pts}
        bad = {i for i in predicted if i in actual and actual[i] != predicted[i]}
        if bad:
            i0 = next(iter(bad))
            self.log(f"{len(bad)}/{len(predicted)} point(s) landed off "
                     f"prediction (idx {i0}: predicted "
                     f"{predicted[i0]/1000:.0f}, hardware "
                     f"{actual[i0]/1000:.0f} MHz) - clamped, or another tool "
                     f"is writing this table", False)
        # rebase on the points just read, not on a third NVAPI round trip: two
        # back-to-back curve reads stall the UI thread, and the second one could
        # return something the prediction check above never saw
        self.vf_read(force=True, pts=pts)

    def vf_deflatten(self):
        """Stage the de-flatten plan onto the working copy - PREVIEW only.
        Nothing reaches the GPU until Apply to GPU. This restores the
        look-before-you-write step the Tk build had as a confirm dialog."""
        if not self.vf_points:
            self.log("read the curve first", False)
            return
        pts = self.work_pts()
        cap = dpg.get_value("vcap")
        gmax = self.gpu.static.get("gfx_max")
        # the planner's own before/after pair is dropped on purpose: `before` is
        # measured at/below the cap and `after` at the boundary point one past
        # it, so printing them as one "ceiling" was two numbers under one word.
        # Everything below is measured off the curve, by curve_top, exactly as
        # the Apply confirmation measures it.
        ch, _cb, _ca, meta = GPU.compute_deflatten(
            pts, cap, max_khz=(gmax * 1000 if gmax else None))
        if not ch:
            # Three states land here and only ONE is good news. compute_deflatten
            # returns boundary_idx None when the cap matched no point at all, and
            # unique False when a point below already holds the hardware max -
            # logging either in green reads as "the curve is already optimal"
            # when it means the plan could not be made.
            b = meta.get("boundary_idx")
            if b is None:
                lo = min(p["volt_mv"] for p in pts)
                self.log(f"cap {cap:.0f} mV is below every point on this curve "
                         f"(lowest is {lo:.2f} mV) - nothing matched, no plan",
                         False)
            elif not meta.get("unique", True):
                self.log(f"idx {b} cannot be made the unique top: a point below "
                         f"it already holds the hardware max clock", False)
            else:
                self.log(f"idx {b} is already the unique top at "
                         f"\u2264{cap:.0f} mV - nothing to do", True)
            return
        top_before, peak_before = self.curve_top(pts, cap)
        for idx, _v, _o, _n, nd in ch:
            self.vf_work[idx] = int(nd)
        self.sync_sel_inputs()
        self.vf_redraw()
        top_after, peak_after = self.curve_top(self.work_pts(), cap)
        note = (" (clamped at hw max)" if meta.get("clamped") else "")
        if not meta.get("unique", True):
            # meta['unique'] is False when a point BELOW the boundary already
            # holds the hardware max, i.e. de-flatten cannot make the boundary
            # the sole peak - the case where the operation does not do what its
            # name says. Tk said so in the dialog; it must not be silent here.
            note += (" - a point below is already at the max clock, so "
                     "de-flatten alone cannot lift the top")
        # The top ≤cap says nothing about the points ABOVE the boundary, which
        # are all levelled onto the boundary's new value. A cap that lands in
        # the low-voltage floor therefore reads as a tidy +15 MHz while dragging
        # the whole upper curve down to floor clock - the one plan the Tk dialog
        # existed to catch. Report the PEAK too: if the plan pulls it down, say
        # so, and do not colour it green.
        down = peak_after < peak_before
        self.log(f"staged: idx {meta.get('boundary_idx')} (the boundary point "
                 f"past {cap:.0f} mV) becomes the unique top - peak "
                 f"{peak_before:.0f} -> {peak_after:.0f} MHz, top ≤{cap:.0f} mV "
                 f"{top_before:.0f} -> {top_after:.0f} MHz, {len(ch)} point(s) "
                 f"changed" + note
                 + (f" - WARNING: this pulls the curve's PEAK down from "
                    f"{peak_before:.0f} to {peak_after:.0f} MHz. Check the cap "
                    f"line on the plot; 'Revert edits' drops the plan" if down
                    else " - press Apply to GPU to write"),
                 not down)

    def vf_rephase(self):
        if not self.guard():
            return
        # Re-phase plans off the HARDWARE deltas and writes them, then re-reads
        # with force=True - which would drop staged edits the plan never saw.
        # Refuse instead of arming: unlike Read curve there is no version of
        # this the user could want, since Apply first (or Revert) makes the
        # hardware and the plan agree.
        pending = sum(1 for i in self.vf_work
                      if self.vf_work.get(i) != self.vf_orig.get(i))
        if pending:
            self.log(f"re-phase rewrites the hardware deltas and would discard "
                     f"{pending} staged edit(s) it cannot see - 'Apply to GPU' "
                     f"or 'Revert edits' first", False)
            return
        # The one LOSSY write in this app, so the one that most needs an undo
        # point: off-phase deltas are rounded DOWN onto the common phase and
        # the original remainders are gone. 'Reset curve to stock' zeroes the
        # table, which is not the same thing as putting them back - only a
        # snapshot can.
        self.autosave_before("vf-rephase")
        ok, m = self.gpu.rephase_deltas()
        self.report((ok, m))
        if ok:
            self.vf_read(force=True)

    def vf_reset(self):
        """Zeroing every delta only moves the card toward stock, but it is still
        a full 103-row table write AND it re-reads with force=True, which throws
        staged edits away. ONE CLICK, like Apply: the plan banner above states
        both consequences continuously, and autosave_before makes even the
        discarded edits recoverable. 'Reset all to stock' is the one that still
        arms - it drops every knob at once, not just this table. Behind the
        unlock gate like every other write."""
        if not self.guard():
            return
        pending = sum(1 for i in self.vf_work
                      if self.vf_work.get(i) != self.vf_orig.get(i))
        self.autosave_before("vf-reset")
        self.log(f"zeroing all {VFP_POINTS} V/F deltas back to the factory "
                 f"curve, discarding {pending} staged edit(s)", None)
        ok, m = self.gpu.reset_vf_curve()
        self.report((ok, m))
        if ok:
            self.vf_read(force=True)

    # ====================================================================== #
    #  DEVICE REPORT                                                         #
    # ====================================================================== #
    def lockable_summary(self):
        """The lockable-clock table is per memory clock, not one range. The
        Device tab reports the top-mem row, which hides that; spell it out."""
        rows = self.gpu.lockable_clocks_by_mem()
        if not rows:
            return "        (driver did not enumerate them)"
        return "\n".join(
            f"        mem {m:>5} MHz -> {len(g):>3} clocks, {g[0]}-{g[-1]} MHz"
            for m, g in rows)

    def device_report(self):
        """Everything the RUNNING program knows that a README cannot state:
        per-card, per-driver values read back from NVAPI/NVML at startup. This
        is what belongs in a bug report, so it is built as one pasteable block.

        The hardware explanations that used to live here (quantisation, phase,
        the arbiter rule, the footgun list) moved to README.md. They were
        duplicated prose, and the two copies had already drifted apart."""
        st = self.gpu.static
        cr, mr = st.get("core_off_range"), st.get("mem_off_range")
        return f"""TitanTune - device report

Device : {st.get('name')}
Driver : {st.get('driver')}     VBIOS : {st.get('vbios')}
Memory : {st.get('mem_type')} (id {st.get('mem_type_id','?')}), true-clock divisor {st.get('mem_div')}
Offsets: core {cr} (MHz, 1:1)   mem {mr} (NVML units)   [min, max, applied now]
Power  : {st.get('pl_min_mw','?')}..{st.get('pl_max_mw','?')} mW, default {st.get('pl_def_mw','?')}
Lockable clocks: {st.get('gfx_min','?')}-{st.get('gfx_max','?')} MHz
    nvmlDeviceGetSupportedGraphicsClocks at the TOP memory clock. These are
    the only values SetGpuLockedClocks accepts - NOT a boost ceiling. The
    V/F curve is a separate mechanism (floor((base+delta)/15)*15) and is
    never checked against this list, so the card can and does run above it.
    The list also shrinks with the memory clock on this card:
{self.lockable_summary()}
Backend: {self.gpu.status_line()}

CAUTION
    The core/mem offset sliders and the V/F curve are the SAME delta table,
    and Afterburner writes it too - drive clocks from ONE tool at a time.

See README.md for the clock-quantisation and phase rules, the two-knob
voltage mechanism, what is reversible, and the footguns this tool
deliberately does not put behind a button."""

    def copy_device_report(self):
        try:
            dpg.set_clipboard_text(self.device_report())
            self.log("device report copied to clipboard", ok=True)
        except Exception as e:
            self.log(f"clipboard unavailable: {e}", ok=False)

    # ====================================================================== #
    #  PROFILES                                                              #
    # ====================================================================== #
    def autosave_before(self, action):
        """Undo point taken IMMEDIATELY before a destructive write. This is the
        other half of single-click Apply: the banner makes the click informed,
        this makes it reversible. Returns True only when a snapshot was taken
        AND it is whole.

        WHICH writes call this, and why not the rest. Every write that can
        destroy state you cannot read back off a slider takes one: 'Apply to
        GPU', 'Reset curve to stock', 'Re-phase', the core-offset Apply,
        'Reset all to stock' and a profile Load. All six write the same 103-row
        delta table, whose previous contents appear nowhere on screen - and
        Re-phase is the only genuinely LOSSY write in the app (off-phase deltas
        are rounded down and the original remainders are gone, so not even
        'Reset curve to stock' can recover them).
        The single-knob applies - memory offset, power limit, voltage boost,
        fan - deliberately do NOT: each moves one number that its own slider
        still shows, and putting them in the ring would evict the curve
        snapshots that nothing else can reconstruct. The clock lock does not
        either, and could not: profiles.capture does not record it, so an undo
        point would silently fail to take it back. Release / Ctrl+H does.

        A failed or partial snapshot does NOT block the write - three of the
        callers only ever move the card toward stock, and refusing to let
        someone back out because a JSON file would not open is the wrong
        failure. It says so as an error instead, which reddens the V/F tab's
        status line as well as the log."""
        try:
            name, _path, missing = profiles.autosave(self.gpu, action)
        except Exception as e:
            self.log(f"could NOT save an undo point before {action}: {e} - "
                     f"the write is going ahead unprotected", False)
            return False
        if missing:
            # An incomplete snapshot must not be announced as an undo point.
            # The one field that goes missing is the V/F delta table, i.e.
            # precisely what the write about to happen overwrites: restore()
            # would put every other knob back and leave the curve where the
            # write left it, while the log claimed the state was saved.
            self.log(f"undo point '{name}' is INCOMPLETE: {'; '.join(missing)}."
                     f" 'Undo last write' will NOT put the curve back - the "
                     f"write is going ahead anyway", False)
            return False
        self.log(f"undo point saved as '{name}' - Profiles > Undo last "
                 f"write restores it", None)
        return True

    def save_profile(self):
        """Named snapshot of every knob this tool can write. Saving touches no
        GPU state at all, so unlike load it is not behind guard()."""
        name = (dpg.get_value("prof_name") or "").strip()
        if not name:
            self.log("give the profile a name first", False)
            return
        try:
            state = profiles.capture(self.gpu)
            path = profiles.save(name, state)
        except Exception as e:
            self.log(f"save profile '{name}': {e}", False)
            return
        if dpg.does_item_exist("win_save"):
            dpg.configure_item("win_save", show=False)
        self.log(f"saved '{os.path.basename(path)}' - "
                 f"{profiles.summarize(state)}", True)
        self.refresh_profile_list()

    def open_save_profile(self, sender=None, app_data=None, user_data=None):
        # seeded with a timestamp so the box is never empty: profiles are
        # slugged onto disk by name, so an unnamed one would be "unnamed.json"
        # and the next save would silently overwrite it
        if dpg.does_item_exist("prof_name") and not dpg.get_value("prof_name"):
            dpg.set_value("prof_name", time.strftime("tune-%Y%m%d-%H%M"))
        self.show_win(user_data="win_save")

    def open_profiles(self, sender=None, app_data=None, user_data=None):
        self.refresh_profile_list()
        self.show_win(user_data="win_profiles")

    # header / width in UNSCALED px, same shape as DOM_COLS. 'contents' is the
    # widest because summarize() is what tells one autosave from another - the
    # names are all timestamps.
    PROF_COLS = (("profile", 300), ("saved", 175), ("contents", 470),
                 ("", 90))
    # tag prefix for the per-row Load buttons. They are built and destroyed on
    # every refresh, so they need a predictable name to be pruned OUT of
    # _ctl_widgets again - see refresh_profile_list.
    PROF_LOAD_TAG = "prof_load_"

    def refresh_profile_list(self):
        """Rebuild the rows from disk, newest first (list_profiles sorts them).
        Deleting a table's children takes its COLUMNS with them, so those are
        re-added here rather than only at build time."""
        if not dpg.does_item_exist("prof_table"):
            return
        # a pending cross-card confirmation is keyed to one row; rebuilding the
        # rows out from under it would leave the warning pointing at nothing
        self.set_pending_load(None)
        # the Load buttons about to be destroyed leave the unlock gate with
        # them, or the list would grow by one dead tag per refresh forever
        self._ctl_widgets = [t for t in self._ctl_widgets
                             if not str(t).startswith(self.PROF_LOAD_TAG)]
        dpg.delete_item("prof_table", children_only=True)
        for label, w in self.PROF_COLS:
            dpg.add_table_column(label=label, parent="prof_table",
                                 width_fixed=True,
                                 init_width_or_weight=self.s(w))
        try:
            rows = profiles.list_profiles()
        except Exception as e:
            rows = []
            self.log(f"profile list: {e}", False)
        if not rows:
            with dpg.table_row(parent="prof_table"):
                dpg.add_text("no profiles saved yet", color=DIM)
            return
        for row_i, (name, _path, when, is_auto) in enumerate(rows):
            try:
                state = profiles.load(name)
                summary = profiles.summarize(state)
            except Exception as e:
                state, summary = None, f"unreadable: {e}"
            with dpg.table_row(parent="prof_table"):
                # autosaves are the app's own undo points, not tunes anyone
                # chose to keep, so they are dimmed - a hand-named profile has
                # to stand out in a list that is mostly machine-made
                dpg.add_text(name, color=DIM if is_auto else TEXT,
                             wrap=self.s(self.PROF_COLS[0][1] - 10))
                dpg.add_text(when or "?", color=DIM)
                dpg.add_text(summary, color=TEXT if state else BAD,
                             wrap=self.s(self.PROF_COLS[2][1] - 10))
                if state is None:
                    dpg.add_text("--", color=DIM)
                else:
                    tag = f"{self.PROF_LOAD_TAG}{row_i}"
                    dpg.add_button(label="Load", tag=tag, width=-1,
                                   user_data=name,
                                   callback=lambda s, a, u: self.load_profile(u))
                    self._ctl_widgets.append(tag)
        # the rows were just created, so they are born ignoring the gate - one
        # pass puts every write control, new and old, back in step with it
        self.sync_lock_ui()

    def set_pending_load(self, pend):
        """Show or clear the cross-card warning. It lives in the Profiles
        window and not only in the log, because the log is on another tab and
        this is the one message that has to be read where the button is."""
        self._pending_load = pend
        if not dpg.does_item_exist("prof_warn"):
            return
        dpg.set_value("prof_warn", "" if not pend else
                      f"⚠  '{pend[0]}': {pend[1]}.\nA 103-point V/F table "
                      f"is 103 frequencies measured on ONE piece of silicon - "
                      f"on another die they are a guess. Press Load on that "
                      f"row again to restore it anyway.")
        dpg.configure_item("prof_warn", show=bool(pend))
        # a refusal the user cannot see is just a dead button. This is also
        # reached from the menu's 'Undo last write', where the window may not
        # be open at all, so put the confirmation in front of them.
        if pend and dpg.does_item_exist("win_profiles"):
            dpg.configure_item("win_profiles", show=True)
            dpg.focus_item("win_profiles")

    def load_profile(self, name):
        """Restoring is a destructive write like any other, so it goes through
        the unlock gate, takes its own undo point first, and reports every knob
        restore() moved - a profile that half-applied must not look clean."""
        if not self.guard():
            return
        try:
            state = profiles.load(name)
        except Exception as e:
            self.log(f"load '{name}': {e}", False)
            return
        warn = profiles.device_mismatch(state, self.gpu)
        if warn and (self._pending_load or (None,))[0] != name:
            self.set_pending_load((name, warn))
            self.log(f"'{name}' {warn} - nothing written; confirm it in "
                     f"Profiles > Load profile before this is restored", False)
            return
        self.set_pending_load(None)
        # the action label is bounded: undoing an undo would otherwise compose
        # 'load-autosave-load-autosave-...' into a filename that only grows
        self.autosave_before(f"load-{name}"[:40])
        self.log(f"restoring '{name}' ({state.get('saved_at','?')}): "
                 f"{profiles.summarize(state)}", None)
        for ok, msg in profiles.restore(self.gpu, state):
            self.log(msg, ok)
        self.sync_sliders_from_gpu(state)
        # the delta table is written LAST and wins over the core offset (see
        # profiles.restore) - rebase the editor on what is now in the card
        self.vf_read(force=True)

    def undo_last_write(self):
        """Restore the snapshot taken just before the most recent covered write
        (see autosave_before for which those are). list_profiles() is newest
        first - ordered on a sub-second timestamp, because two autosaves in the
        same second used to tie and be broken by the action label, which could
        hand back the OLDER of the two states. This goes through load_profile,
        i.e. it takes an undo point of its own - undoing an undo is just
        another load, and pressing this twice must not strand anyone."""
        try:
            autos = [r for r in profiles.list_profiles() if r[3]]
        except Exception as e:
            self.log(f"undo: {e}", False)
            return
        if not autos:
            self.log("undo: nothing to undo - a snapshot is written "
                     "immediately before every write that touches the V/F "
                     "delta table (Apply, Reset curve, Re-phase, the core "
                     "offset, Reset all, a profile Load), and none has "
                     "happened yet", False)
            return
        # rows first: a cross-card snapshot bounces into the Profiles window
        # for confirmation (set_pending_load), and an empty table there would
        # leave the user with a warning and no row to confirm it on
        self.refresh_profile_list()
        self.load_profile(autos[0][0])

    def sync_sliders_from_gpu(self, state=None):
        """Put the knobs back in step with the CARD after a restore. Read back
        rather than echoing the profile: a knob whose write the driver refused
        would otherwise leave its slider displaying a value the card never
        took, which is the exact lie the sliders' own clamp exists to stop."""
        try:
            d = self.gpu.read()
        except Exception as e:
            self.log(f"sliders not re-synced: {e} - they may not match the "
                     f"card until the next Apply", False)
            return
        mscale = self.gpu.mem_offset_scale()[0] or 1
        moff = d.get("mem_off")
        for tag, val in (("sl_core", d.get("core_off")),
                         ("sl_mem", int(moff / mscale)
                          if isinstance(moff, int) else None),
                         ("sl_pl", (d.get("pl_now_mw") or 0) // 1000 or None)):
            if val is not None and dpg.does_item_exist(tag):
                dpg.set_value(tag, int(val))
        vb = self.gpu.read_voltage_boost()
        if vb is not None and dpg.does_item_exist("sl_volt"):
            dpg.set_value("sl_volt", max(0, min(100, int(vb))))
        # fan duty ONLY when the profile actually pinned the fans. On the
        # temperature curve the duty is a reading, not a setting, and parking
        # the slider on it would make the next Apply freeze the fans at
        # whatever they happened to be doing (same rule as profiles.restore).
        fans = d.get("fans") or []
        if state and state.get("fan_manual") and fans \
                and dpg.does_item_exist("sl_fan"):
            dpg.set_value("sl_fan", int(fans[0][0]))

    # ====================================================================== #
    #  MENU BAR + TOOL WINDOWS                                               #
    # ====================================================================== #
    def show_win(self, sender=None, app_data=None, user_data=None):
        """Open one of the tool windows. They are built once and hidden, not
        created per click, so a second open restores the size and position the
        user left them at. Focusing is not optional: an already-open window
        sitting behind the main one would make the menu item look dead."""
        tag = user_data
        if not dpg.does_item_exist(tag):
            return
        dpg.configure_item(tag, show=True)
        dpg.focus_item(tag)

    def build_menu_bar(self):
        """dpg.viewport_menu_bar is a TOP-LEVEL container - it belongs to the
        viewport, not to 'root', so it must be built OUTSIDE that window. DPG
        then draws it over the primary window rather than insetting it, which
        is what the menu_pad spacer in run() and menu_h() in relayout() are
        both paying for."""
        st = self.gpu.static
        gmin = st.get("gfx_min", 300)
        gmax = st.get("gfx_max", 2160)
        with dpg.viewport_menu_bar(tag="menubar"):
            with dpg.menu(label="File"):
                dpg.add_menu_item(label="Exit",
                                  callback=lambda s, a, u: dpg.stop_dearpygui())
            with dpg.menu(label="Device"):
                dpg.add_menu_item(label="Device report...", user_data="win_device",
                                  callback=self.show_win)
                dpg.add_menu_item(label="Copy device report",
                                  callback=self.copy_device_report)
            # Up here rather than on the Control tab for the same reason as the
            # clock lock: these are whole-machine actions, not one more knob.
            # 'Undo last write' is the safety net that lets Apply and Reset
            # curve be one click - see autosave_before.
            with dpg.menu(label="Profiles"):
                dpg.add_menu_item(label="Save profile...",
                                  callback=self.open_save_profile)
                dpg.add_menu_item(label="Load profile...",
                                  callback=self.open_profiles)
                dpg.add_separator()
                # in _ctl_widgets like every other write control: this reaches
                # load_profile, which writes the whole delta table. guard()
                # already refuses it while the gate is clear, but a control
                # that stays lit is a control that looks like it will work.
                dpg.add_menu_item(label="Undo last write", tag="mi_undo",
                                  callback=self.undo_last_write)
                self._ctl_widgets.append("mi_undo")
                dpg.add_text("restores the snapshot taken automatically\n"
                             "immediately before the most recent write.\n"
                             "It is a write itself, so it takes one too.",
                             color=DIM)
            # The clock lock lives up here because it was eating the widest row
            # on the Control tab. It is the same widgets with the same tags, so
            # guard() and the unlock gate (_ctl_widgets, below) still cover it.
            # This is the NVML frequency lock. Ctrl+H drives the OTHER
            # mechanism (the V/F point lock) - see set_lock_state and LOCK_NAME
            # for why one record has to say which of the two is in force, and
            # handover() for what happens when this menu is used while a Ctrl+H
            # hold is up.
            with dpg.menu(label="Clocks"):
                dpg.add_text("GPU CLOCK LOCK", color=ACCENT)
                dpg.add_text(f"{gmin}-{gmax} MHz lockable", color=DIM)
                dpg.add_separator()
                # clamped to the supported range for the same reason as the
                # sliders: an input_int carries DPG's default 0..100 bounds and
                # ignores them on entry, so a typo here reaches lock_gpu_clocks
                # and comes back as a refusal in the log
                dpg.add_input_int(tag="lock_min", label="min MHz",
                                  default_value=gmin,
                                  width=self.s(130), step=15,
                                  min_value=gmin, max_value=gmax,
                                  min_clamped=True, max_clamped=True)
                dpg.add_input_int(tag="lock_max", label="max MHz",
                                  default_value=gmax,
                                  width=self.s(130), step=15,
                                  min_value=gmin, max_value=gmax,
                                  min_clamped=True, max_clamped=True)
                dpg.add_spacer(height=self.s(4))
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Lock", tag="go_lock",
                                   callback=self.apply_lock, width=self.s(90))
                    dpg.add_button(label="Release", tag="go_release",
                                   callback=self.release_lock, width=self.s(90))
                    dpg.add_button(label="Lock max", tag="go_lockmax",
                                   callback=self.lock_max, width=self.s(100))
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(
                            f"Pins both ends to {gmax} MHz - the top of the\n"
                            "driver's LOCKABLE table.\n\n"
                            "That table is not a boost ceiling: the V/F curve\n"
                            "reaches clocks above it (it is floor((base+delta)\n"
                            "/15)*15, never checked against this list). So if\n"
                            "the card is already boosting past it, locking max\n"
                            "will LOWER the clock. This is for holding one\n"
                            "frequency steady, not for going fast.")
                dpg.add_text("the result is one line in the Control tab log.\n"
                             "Ctrl+H on the curve editor is a DIFFERENT lock\n"
                             "(V/F point, by voltage); taking one releases the\n"
                             "other, and at idle this one drops memory to 810",
                             color=DIM)
            self._ctl_widgets += ["lock_min", "lock_max", "go_lock",
                                  "go_release", "go_lockmax"]
            with dpg.menu(label="Help"):
                dpg.add_menu_item(label="Keyboard shortcuts",
                                  user_data="win_keys", callback=self.show_win)
                dpg.add_menu_item(label="About", user_data="win_about",
                                  callback=self.show_win)

    # Only the bindings that EXIST in build_vf's handler_registry, and only the
    # gestures on_plot_click / on_plot_drag really implement. A list naming a
    # key nothing implements is worse than no list at all - and so is one
    # describing a gesture the code has since replaced, which is what the
    # Tk-era "nearest by voltage" and "middle-drag pans" rows had become. Both
    # rules bind the same way: anything added to that registry, or any change to
    # which button does what, has to move these rows in the same change.
    VF_KEYS = [
        ("W / S", "move the selected point +/- 15 MHz (one clock bin)"),
        ("A / D", "select the previous / next point along the curve"),
        ("Shift + W/S", "move 3 bins at once (+/- 45 MHz)"),
        ("Shift + A/D", "step the selection 3 points at a time"),
        ("left-click ON a dot",
         "select it. The hit test is in SCREEN PIXELS - the press has to land "
         "within ~14 px of the drawn marker - not 'the nearest point by "
         "voltage', which made empty sky a grab handle for whatever dot shared "
         "that column"),
        ("left-drag ON a dot",
         "move it; snaps to whole 15 MHz bins. Vertical only: voltage is fixed "
         "by the VF table"),
        ("left-drag anywhere else",
         "pan the plot. The left button is shared - a press that misses every "
         "dot leaves it with the pan, and the grab hands it straight back on "
         "release. Middle-drag does nothing"),
        ("scroll wheel", "zoom the plot (pan and zoom are bounded; 'Fit view' "
                         "puts the whole curve back on screen)"),
        ("Ctrl + H", "hold the selected point with the V/F point lock: the "
                     "card is pinned to the point by VOLTAGE, and holds true "
                     "P0 while it is. Press again to release. The hardware "
                     "resolves the request DOWN to the highest point at or "
                     "below it, so the point held can be lower than the one "
                     "selected - the status line always names the point the "
                     "card is really on. Both the hold and its release are "
                     "behind 'Unlock controls'"),
    ]

    def build_tool_windows(self):
        """Everything the menu bar opens. Built hidden, at startup, because the
        device report is a snapshot of what the driver said when the app came up
        - the same text the retired Device tab rendered."""
        # wide on purpose: the report's longest lines (the offset ranges, the
        # per-mem-clock lockable table) are what a bug report needs, and a
        # readonly multiline box clips them rather than wrapping
        with dpg.window(label="Device report", tag="win_device", show=False,
                        width=self.s(1000), height=self.s(600),
                        pos=[self.s(70), self.s(70)]):
            dpg.add_button(label="Copy device report",
                           callback=self.copy_device_report,
                           width=self.s(200))
            # tag "info" follows the text here from the Device tab: typing()
            # names it, so W/S must still not retune the curve behind this box
            dpg.add_input_text(tag="info", multiline=True, readonly=True,
                               default_value=self.device_report(),
                               width=-1, height=-1)
            self.bind("info", "mono")

        with dpg.window(label="Save profile", tag="win_save", show=False,
                        width=self.s(560), height=self.s(210),
                        pos=[self.s(200), self.s(180)]):
            dpg.add_text("Snapshots both offsets, the power limit, the voltage "
                         "boost, the fan POLICY (not just its duty) and all "
                         f"{VFP_POINTS} V/F deltas, as JSON in profiles/ next "
                         "to the app. Saving writes nothing to the GPU.",
                         color=DIM, wrap=self.s(520))
            dpg.add_spacer(height=self.s(6))
            # on_enter as well as the button: this box is a name prompt, and
            # Enter is what a name prompt is expected to take. typing() names
            # it too, so W/A/S/D typed in here do not also retune the curve.
            dpg.add_input_text(tag="prof_name", width=-1, hint="profile name",
                               on_enter=True, callback=self.save_profile)
            dpg.add_text("saved under this name, slugged - an existing profile "
                         "of the same name is overwritten", color=DIM,
                         wrap=self.s(520))
            dpg.add_spacer(height=self.s(6))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", width=self.s(130),
                               callback=self.save_profile)
                dpg.add_button(label="Cancel", width=self.s(130),
                               callback=lambda: dpg.configure_item(
                                   "win_save", show=False))

        with dpg.window(label="Profiles", tag="win_profiles", show=False,
                        width=self.s(1080), height=self.s(560),
                        pos=[self.s(90), self.s(90)]):
            dpg.add_text("Loading is a destructive write: it sets the power "
                         "limit, voltage boost, both offsets and the fan "
                         "policy, then the whole delta table LAST - which wins "
                         "over the core offset, because they are the same "
                         f"{VFP_POINTS} driver rows. An undo point is taken "
                         "first, and every knob reports into the Control tab "
                         "log.", color=WARN, wrap=self.s(1040))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Refresh", width=self.s(130),
                               callback=self.refresh_profile_list)
                dpg.add_button(label="Save profile...", width=self.s(170),
                               callback=self.open_save_profile)
            dpg.add_text("", tag="prof_warn", color=BAD, show=False,
                         wrap=self.s(1040))
            dpg.add_separator()
            # the table lives in a child_window for the same reason pan_dom
            # does: the autosave ring alone is profiles.KEEP_AUTOSAVES rows, so
            # past its share the LIST scrolls rather than pushing Refresh and
            # the warning line off the top of the window
            with dpg.child_window(width=-1, height=-1):
                with dpg.table(tag="prof_table", header_row=True,
                               no_host_extendX=True,
                               policy=dpg.mvTable_SizingFixedFit,
                               borders_innerH=True, borders_innerV=True):
                    for label, w in self.PROF_COLS:
                        dpg.add_table_column(label=label, width_fixed=True,
                                             init_width_or_weight=self.s(w))

        with dpg.window(label="Keyboard shortcuts", tag="win_keys", show=False,
                        width=self.s(620), height=self.s(460),
                        pos=[self.s(140), self.s(120)]):
            dpg.add_text("V/F CURVE EDITOR", color=ACCENT)
            dpg.add_separator()
            with dpg.table(header_row=False, no_host_extendX=True,
                           policy=dpg.mvTable_SizingFixedFit):
                dpg.add_table_column(width_fixed=True,
                                     init_width_or_weight=self.s(130))
                dpg.add_table_column(width_fixed=True,
                                     init_width_or_weight=self.s(430))
                for keys, what in self.VF_KEYS:
                    with dpg.table_row():
                        dpg.add_text(keys, color=ACCENT)
                        # wrapped to the column: a fixed-fit table does not
                        # wrap on its own, so the longer rows would run out
                        # past the window edge instead of onto a second line
                        dpg.add_text(what, color=TEXT, wrap=self.s(420))
            dpg.add_spacer(height=self.s(8))
            dpg.add_text("The key handlers are window-wide, not plot-local, but "
                         "they stand down while a text or number box has focus - "
                         "so W/A/S/D typed into the cap, index or MHz box do not "
                         "also retune the curve.", color=DIM, wrap=self.s(580))

        with dpg.window(label="About TitanTune", tag="win_about", show=False,
                        width=self.s(620), height=self.s(300),
                        pos=[self.s(180), self.s(160)]):
            dpg.add_text("TitanTune", color=ACCENT)
            dpg.add_text("Monitor and tuner for the Titan RTX (TU102) die on an "
                         "ASUS RTX 2080 Ti Strix board.", wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("README.md, shipped beside this app, is the single "
                         "source of truth for the hardware: the 15 MHz clock "
                         "quantisation and phase rules, the two-knob voltage "
                         "mechanism, what is reversible, and the footguns this "
                         "tool deliberately does not put behind a button.",
                         color=DIM, wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("The core/mem offset sliders and the V/F curve are the "
                         "SAME delta table, and Afterburner writes it too - "
                         "drive clocks from ONE tool at a time.", color=WARN,
                         wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text(f"backend: {self.gpu.status_line()}", color=DIM)

    # ====================================================================== #
    #  TIMINGS  -  READ-ONLY                                                 #
    # ====================================================================== #
    # This tab CANNOT write. timings.py talks to nvtune through a whitelist of
    # read-only subcommands and refuses --commit/--force/set/restore/apply/
    # daemon before a process is created; nothing here builds an argv at all.
    # Writing an FBPA timing register can hang the machine and corrupt VRAM,
    # so the refusal is code, not a convention (see timings._check_argv).
    #
    # WHAT THE TAB IS FOR: a timing register holds a CYCLE COUNT, and a cycle
    # count is meaningless without the memory clock it was counted against.
    # The same registers read as garbage at idle and as textbook GDDR6 at P0.
    # So every number here is shown WITH the clock its snapshot was taken at,
    # and the comparison view puts what a field's cycle count did next to what
    # the clock did between the same two states - two ratios agreeing is what
    # proves the decode is real.
    TIM_COLS = (("field", 118), ("register", 88), ("bits", 76),
                ("cycles", 62), ("ns at the capture clock", 250),
                ("what it is", 560))

    TIM_READONLY = ("READ-ONLY. This tab decodes the framebuffer-partition "
                    "memory timing registers through nvtune's read-only "
                    "subcommands. It cannot write one: TitanTune has no code "
                    "path that can build a writing nvtune command line.")

    # verdict -> colour for the comparison table. 'flat' is DIM, not red: a
    # field that does not move with the clock is usually a mode or bus
    # turnaround value counted in cycles by design, not a broken decode.
    CMP_COL = {"tracks": GOOD, "flat": DIM, "partial": WARN, "--": DIM}

    def build_timings(self):
        with dpg.tab(label="  Timings  ", tag="tab_tim"):
            with dpg.group(horizontal=True):
                dpg.add_button(label="Capture", tag="tim_cap",
                               width=self.s(150), callback=self.timings_capture)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(
                        "Reads the timing registers and files the result under\n"
                        "the memory clock it was taken at.\n\n"
                        "Capture at TWO memory states to prove the decode: let\n"
                        "the card idle (the memory clock drops to ~810) and\n"
                        "capture, then put it under load (P0, ~7428) and\n"
                        "capture again. The comparison below then shows each\n"
                        "field's cycle ratio beside the clock ratio.\n\n"
                        "Nothing here writes to the GPU.")
                # INDUCE, never "force": nothing here commands a p-state,
                # because nothing on this card can (see gpuload's header).
                # It runs a workload and reports what the driver decided.
                dpg.add_button(label="Induce P-state (GPU load)",
                               tag="tim_induce", width=self.s(250),
                               callback=self.timings_induce)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(
                        "Runs a CUDA device-to-device memcpy load, waits for\n"
                        "the memory clock to settle, captures WHILE THE LOAD\n"
                        "IS STILL RUNNING, then stops it.\n\n"
                        "This is ENOUGH to read the timings. It reaches\n"
                        "p-state 2 (memory 7228, ~450 GB/s) rather than P0,\n"
                        "and that does not matter: measured on this card the\n"
                        "registers are BIT-IDENTICAL at 7228 and at 7428,\n"
                        "because timings are picked per clock BAND, not per\n"
                        "p-state. No game, no benchmark, no '-cc 1' needed.\n\n"
                        "What IS useless is an idle capture - 405 and 810\n"
                        "program genuinely slacker values.\n\n"
                        "If the card is ALREADY at P0 this skips the load and\n"
                        "captures directly - measured, starting a CUDA context\n"
                        "on a P0 card pulls it DOWN to P2.\n\n"
                        "It is an ordinary workload. It writes no register.")
                dpg.add_button(label="Forget captures", tag="tim_clear",
                               width=self.s(170), callback=self.timings_clear)
                # Armed by default: the memory P-state CANNOT be forced on this
                # card, so waiting for the card to reach P0 on its own is the
                # only way to get a capture that means anything (see
                # timings_p0_watch). Costs nothing while it waits.
                dpg.add_checkbox(label="Auto-capture at P0", tag="tim_auto",
                                 default_value=True,
                                 callback=self.timings_auto_toggled)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(
                        "Watches the memory clock on the telemetry poll and\n"
                        "captures the FIRST time the card reaches its top\n"
                        "memory state, then again on each re-entry after it\n"
                        "has dropped out. One capture per entry.\n\n"
                        "Start a game or a benchmark and come back: a valid\n"
                        "P0 capture will be waiting.\n\n"
                        "This exists because the memory P-state cannot be\n"
                        "pinned on this card - nvmlDeviceSetMemoryLockedClocks\n"
                        "returns NOT_SUPPORTED, and locking the graphics clock\n"
                        "alone only pulls memory to 810. Measured: only a\n"
                        "sustained 3D load reaches the top state.\n\n"
                        "It reads. It never writes.")
                dpg.add_text("", tag="tim_busy", color=ACCENT)
            # THE headline. A capture taken at idle is the same class of error
            # as a capture taken across a reclock - an authoritative-looking
            # number measured against the wrong state - so it gets the same
            # loudness, at the top, before the table anyone would read.
            dpg.add_text("", tag="tim_state", color=WARN, wrap=self.s(1100))
            # where the last induced load actually landed, and what lever is
            # left if it landed short
            dpg.add_text("", tag="tim_induce_note", color=WARN, show=False,
                         wrap=self.s(1100))
            dpg.add_text(self.TIM_READONLY, tag="tim_ro", color=DIM,
                         wrap=self.s(1100))
            # the specific degradation: which of the two halves is missing
            dpg.add_text("", tag="tim_reason", color=WARN, show=False,
                         wrap=self.s(1100))

            # ---- header: the identity, and THE clock this decode is against #
            with dpg.child_window(tag="pan_tim_hdr", width=-1,
                                  height=self.s(104), border=True,
                                  no_scrollbar=True, no_scroll_with_mouse=True):
                dpg.add_text("--", tag="tim_ident", color=ACCENT)
                dpg.add_text("--", tag="tim_clock")
                dpg.add_text("", tag="tim_warn", color=BAD, show=False,
                             wrap=self.s(1100))
                dpg.add_text("", tag="tim_words", color=DIM)
                for t in ("tim_ident", "tim_clock", "tim_words"):
                    self.bind(t, "mono")

            dpg.add_spacer(height=self.s(4))
            with dpg.child_window(tag="pan_tim", width=-1, height=self.s(360)):
                dpg.add_text("DECODED TIMINGS  ·  broadcast aperture",
                             tag="tim_title", color=ACCENT)
                dpg.add_text("", tag="tim_sub", color=DIM, wrap=self.s(1100))
                dpg.add_separator()
                with dpg.table(tag="tim_table", header_row=True,
                               no_host_extendX=True,
                               policy=dpg.mvTable_SizingFixedFit,
                               borders_innerH=True, borders_innerV=True):
                    self.tim_columns()

            dpg.add_spacer(height=self.s(4))
            with dpg.child_window(tag="pan_cmp", width=-1, height=self.s(300)):
                dpg.add_text("CAPTURES COMPARED  ·  cycle ratio vs clock "
                             "ratio", color=ACCENT)
                dpg.add_text("", tag="cmp_hint", color=DIM, wrap=self.s(1100))
                dpg.add_text("", tag="cmp_legend", color=TEXT,
                             wrap=self.s(1100), show=False)
                dpg.add_separator()
                dpg.add_table(tag="cmp_table", header_row=True,
                              no_host_extendX=True,
                              policy=dpg.mvTable_SizingFixedFit,
                              borders_innerH=True, borders_innerV=True)

            dpg.add_spacer(height=self.s(4))
            # Hidden while the partitions agree, which is the normal case on
            # this card - six identical copies of the same table would bury the
            # one line that actually matters.
            with dpg.child_window(tag="pan_div", width=-1, height=self.s(150),
                                  show=False):
                dpg.add_text("PARTITION DIVERGENCE", color=BAD)
                dpg.add_text("", tag="div_sub", color=DIM, wrap=self.s(1100))
                dpg.add_separator()
                dpg.add_table(tag="div_table", header_row=True,
                              no_host_extendX=True,
                              policy=dpg.mvTable_SizingFixedFit,
                              borders_innerH=True, borders_innerV=True)

    def tim_columns(self):
        # parent named explicitly: these columns are re-created on every
        # redraw, when there is no container stack to deduce a parent from
        for label, w in self.TIM_COLS:
            dpg.add_table_column(label=label, parent="tim_table",
                                 width_fixed=True,
                                 init_width_or_weight=self.s(w))

    # ---- the worker ------------------------------------------------------- #
    def timings_capture(self, sender=None, app_data=None, user_data=None):
        """Take one snapshot OFF the UI thread. nvtune is a subprocess and
        gpu.read() is a driver round trip; done inline they would stall the
        render loop exactly the way the Tk build stalled the desktop."""
        with self._tim_lock:
            if self._tim_busy:
                return
            self._tim_busy = True
            self._tim_what = "reading…"
        threading.Thread(target=self._timings_worker, daemon=True,
                         name="titantune-timings").start()

    def _timings_worker(self):
        av = timings.available()
        snap = timings.snapshot(self.gpu) if av.ok else None
        with self._tim_lock:
            self._tim_avail = av
            # a plain capture is not the induced one: its landing note would
            # be describing a load that is no longer running
            self._tim_note = ""
            self._tim_what = ""
            if snap is not None:
                self._tim = snap
                # Only a snapshot whose clock HELD STILL is filed as a capture:
                # the captures are keyed by memory clock, and one taken across
                # a reclock has no single clock to be keyed by.
                if snap.ok and snap.mem_stable and snap.key:
                    self._tim_caps[snap.key] = snap
            self._tim_busy = False
            self._tim_new = True

    def timings_clear(self, sender=None, app_data=None, user_data=None):
        with self._tim_lock:
            self._tim_caps.clear()
            self._tim_new = True
        # an explicit "start over" also drops the anti-thrash floor, so the
        # next time the card reaches P0 it is captured immediately
        self._tim_auto_t = 0.0

    def timings_auto_toggled(self, sender=None, app_data=None,
                             user_data=None):
        """Re-arm on every toggle. Ticking the box while the card is ALREADY
        at P0 should capture now, not wait for the next time it drops out and
        climbs back - that wait could be the rest of the session."""
        self._tim_p0_in = False
        self.log("timings: auto-capture at P0 "
                 + ("armed" if app_data else "off"))

    # ---- waiting for P0, because it cannot be commanded -------------------- #
    def mem_states(self):
        """Every memory clock the driver enumerates. On this card
        [405, 810, 5001, 6801, 7001] - and the card reaches 7428 under load,
        because the memory offset rides on top of the top state."""
        return sorted(self.gpu.static.get("mem_clocks") or [])

    def mem_top(self):
        st = self.mem_states()
        return st[-1] if st else None

    def is_p0(self, mem, pstate):
        """The ONE P0 test in the UI, so the status line, the auto-capture
        watcher and the snapshot cannot disagree.

        The p-state decides, not the clock: a CUDA load sits at memory 7228,
        ABOVE the top enumerated 7001, and is still P2. A clock-only test calls
        that P0 and it is not."""
        top = self.mem_top()
        if not top or mem is None:
            return False
        if pstate is not None:
            return pstate == 0 and mem >= top
        return mem >= top

    # ---- induce a load, capture during it --------------------------------- #
    def timings_induce(self, sender=None, app_data=None, user_data=None):
        """Run a GPU load and capture while it runs. Off the UI thread: it
        holds the card busy for seconds."""
        with self._tim_lock:
            if self._tim_busy:
                return
            self._tim_busy = True
            self._tim_what = "inducing GPU load…"
        threading.Thread(target=self._induce_worker, daemon=True,
                         name="titantune-induce").start()

    def _induce_worker(self):
        note, snap = "", None
        try:
            av = timings.available()
            if not av.ok:
                note = av.reason
            else:
                mem, ps = None, None
                try:
                    d = self.gpu.read()
                    mem, ps = d.get("mem"), d.get("pstate")
                except Exception:
                    pass
                if self.is_p0(mem, ps):
                    # MEASURED: creating a CUDA context on a card already at P0
                    # drops it to P2 (7428/P0 -> 7228/P2). Running the load
                    # here would destroy the very state we came for.
                    snap = timings.snapshot(self.gpu)
                    note = (f"The card was ALREADY at P0 (memory {mem}, "
                            f"p-state {ps}), so no load was started and the "
                            f"capture was taken directly. Measured: opening a "
                            f"CUDA context on a card at P0 pulls it DOWN to "
                            f"P2, so inducing here would have cost you the "
                            f"state you already had.")
                else:
                    ok, why = gpuload.available()
                    if not ok:
                        note = why
                    else:
                        res = gpuload.induce(
                            self.gpu,
                            on_settled=lambda: timings.snapshot(self.gpu))
                        snap = res.get("result")
                        note = self.induce_note(res, snap)
        except Exception as e:
            note = f"induce failed: {type(e).__name__}: {e}"
        with self._tim_lock:
            self._tim_avail = timings.available()
            if snap is not None:
                self._tim = snap
                if snap.ok and snap.mem_stable and snap.key:
                    self._tim_caps[snap.key] = snap
            self._tim_note = note
            self._tim_busy = False
            self._tim_what = ""
            self._tim_new = True
        if note:
            self.log("timings: " + note.replace("\n", " ")[:180],
                     True if (snap is not None and snap.ok) else False)

    def induce_note(self, res, snap):
        """Say plainly where the load landed.

        A P2 landing is a SUCCESS and is written as one. The registers are
        bit-identical to P0's on this card, so describing P2 as 'short' would
        send the reader chasing a graphics load and a compute-cap flag for
        data they already have - a false shortfall, which is the same kind of
        misinformation as a false authority."""
        if res.get("error"):
            return f"the GPU load did not run: {res['error']}"
        st = res.get("stats") or {}
        how = (f"CUDA memcpy load: {st.get('buf_mib', '?')} MiB x2 out of "
               f"{st.get('free_mib', '?')} MiB free, "
               f"{st.get('gbps_traffic', 0):.0f} GB/s of memory traffic"
               if st else "CUDA memcpy load")
        mem, ps = res.get("mem"), res.get("pstate")
        if snap is not None and snap.ok:
            mem, ps = snap.mem_nvml, snap.pstate
        if snap is not None and snap.at_p0:
            return f"The load induced P0 (memory {mem}). {how}."
        if snap is not None and snap.perf_band:
            return (
                f"The load induced P-STATE {ps} (memory {mem}) — the TOP "
                f"CLOCK BAND, and a fully valid reading. {how}.\n"
                f"This is the same timing data a 3D load would give you: "
                f"measured on this card, CONFIG0..CONFIG5 and TIMING22 are "
                f"BIT-IDENTICAL at 7228 (P2) and 7428 (P0), because timings "
                f"are selected per clock BAND and the 50 MHz of true clock "
                f"between the two does not cross a band boundary. Nothing "
                f"further is needed to read them — no game, no benchmark, no "
                f"compute-cap change.\n"
                f"That identity is for READING. A throughput benchmark would "
                f"still have to run in the state it claims to describe.")
        return (f"The load reached memory {mem}, p-state {ps} — below the top "
                f"clock band, so these are not timings worth reading. {how}. "
                f"Try again, or run any 3D workload with 'Auto-capture at P0' "
                f"armed.")

    # An idle card does not sit still at P0. MEASURED here: it bounces
    # 5000/P3 -> 7428/P0 -> 5000/P3 every 3-4 seconds with nothing running, so
    # "one capture per entry" on its own produced four captures in ten seconds.
    AUTO_MIN_GAP = 30.0

    def auto_wanted(self, mem):
        """Is another AUTOMATIC capture worth taking? (The Capture button is
        never gated by this - an explicit press always reads.)

        Auto-capture exists to leave a valid sample waiting for someone who
        walked away. Once one exists for that memory state there is nothing
        further to learn from re-entering it, so a re-entry only captures a
        state not already held - with a time floor as a backstop against a
        clock that lands a few units differently each time."""
        with self._tim_lock:
            have = self._tim_caps.get(mem)
            last = self._tim_auto_t
        if have is not None and have.perf_band:
            return False
        return (time.monotonic() - last) >= self.AUTO_MIN_GAP

    def timings_p0_watch(self, mem, pstate, busy):
        """Edge-trigger a capture when the card ENTERS its top memory state.

        The tab cannot FORCE a memory p-state - nothing on this card can:
        nvmlDeviceSetMemoryLockedClocks answers NOT_SUPPORTED and nvidia-smi
        -lmc fails identically. It can only INDUCE one and be ready when the
        driver decides. This is the ready-and-waiting half; 'Induce P-state'
        is the other.

        Edge-triggered with hysteresis, and the exit threshold is the NEXT
        enumerated state down (6801 here), not the entry one: a clock wobbling
        either side of the top would otherwise re-fire every tick, and this
        spawns a process each time."""
        top = self.mem_top()
        if not top or mem is None:
            return
        if self.is_p0(mem, pstate):
            if not self._tim_p0_in:
                self._tim_p0_in = True
                if (dpg.get_value("tim_auto") and not busy
                        and self.auto_wanted(mem)):
                    self._tim_auto_t = time.monotonic()
                    # a receipt: the point of arming this is to walk away, so
                    # the log has to say it happened while nobody was looking
                    self.log(f"timings: memory reached {mem} (P0, top "
                             f"enumerated {top}) - auto-capturing", True)
                    self.timings_capture()
            return
        states = self.mem_states()
        exit_below = states[-2] if len(states) > 1 else top
        if self._tim_p0_in and mem < exit_below:
            self._tim_p0_in = False

    # ---- drawing ---------------------------------------------------------- #
    def refresh_timings(self, d):
        """Called on the 4 Hz panel tick like every other panel. Cheap unless
        a capture landed: the tables are rebuilt only when there is new data,
        never per tick."""
        if not dpg.does_item_exist("tim_cap"):
            return
        with self._tim_lock:
            new, snap, av = self._tim_new, self._tim, self._tim_avail
            busy, caps = self._tim_busy, dict(self._tim_caps)
            note, what = self._tim_note, self._tim_what
            self._tim_new = False
        for tag in ("tim_cap", "tim_induce", "tim_clear"):
            dpg.configure_item(tag, enabled=not busy)
        dpg.set_value("tim_busy", f"  {what}" if busy else "")
        # The live clock, so the user can see WHEN a state worth capturing is
        # available - and, when it is not, that the tab is waiting for one.
        mem, ps = d.get("mem"), d.get("pstate")
        if mem is not None and not busy:
            div = self.gpu.static.get("mem_div")
            true = f" ({mem / div:.0f} MHz true)" if div else ""
            top = self.mem_top()
            at_p0 = self.is_p0(mem, ps)
            have = "captured" if mem in caps else "not captured"
            wait = ("" if at_p0 or not dpg.get_value("tim_auto")
                    else f" · waiting for P0 (≥{top}, p-state 0)" if top else "")
            dpg.set_value("tim_busy", f"  card is at {mem}{true} · P{ps} · "
                          f"{'P0' if at_p0 else 'below P0'} · {have}{wait}")
            dpg.configure_item("tim_busy", color=GOOD if at_p0 else DIM)
        # armed or not, the edge is tracked so that arming mid-P0 still fires
        self.timings_p0_watch(mem, ps, busy)
        if new:
            try:
                self.draw_timings(snap, av, caps, note)
            except Exception as e:
                self.log_once("timings", f"timings panel: {e}")

    def draw_timings(self, snap, av, caps, note=""):
        # where the last induced load landed, and the remedy if it fell short
        dpg.configure_item("tim_induce_note", show=bool(note))
        if note:
            dpg.set_value("tim_induce_note", note)
        # ---- availability, named specifically ----------------------------- #
        bad = (av is not None and not av.ok)
        dpg.configure_item("tim_reason", show=bad)
        if bad:
            dpg.set_value("tim_reason", av.reason + "\n\nThe tab is read-only "
                          "either way - nothing here can write a timing "
                          "register.")
        if snap is None or not snap.ok:
            dpg.set_value("tim_state", "")
            dpg.set_value("tim_ident", "no snapshot")
            dpg.set_value("tim_clock", (snap.error if snap else
                                        "nvtune has not been read yet"))
            dpg.configure_item("tim_warn", show=False)
            dpg.set_value("tim_words", "")
            dpg.delete_item("tim_table", children_only=True)
            self.tim_columns()
            # the captures still have to be redrawn: 'Forget captures' lands
            # here whenever the most recent snapshot failed, and a comparison
            # left standing over captures that no longer exist is a lie
            self.draw_comparison(caps)
            return

        # ---- WHICH MEMORY STATE, before anything else --------------------- #
        # Red, first, and in full. The startup capture almost always lands at
        # idle, and an idle capture presented calmly under a confident ns table
        # is exactly the mistake this tab exists to stop people making.
        # The loud case is an IDLE capture. A P2 capture is bit-identical to a
        # P0 one on this card, so calling it second-rate would be its own
        # false-authority error - the exact thing this banner exists to stop.
        band = snap.perf_band
        dpg.set_value("tim_state", ("✔ " if band else "⚠ ")
                      + snap.state_headline)
        dpg.configure_item("tim_state",
                           color=GOOD if band else (WARN if band is None
                                                    else BAD))
        dpg.set_value("tim_title", (
            f"DECODED TIMINGS  ·  broadcast aperture  ·  "
            f"{snap.state_tag}, top clock band" if band else
            "DECODED TIMINGS  ·  IDLE STATE — NOT PERFORMANCE-RELEVANT"))
        dpg.configure_item("tim_title", color=ACCENT if band else BAD)

        # ---- header ------------------------------------------------------- #
        dpg.set_value("tim_ident",
                      f"{snap.codename}  ·  PCI {snap.pci_id}  ·  "
                      f"slot {snap.slot}  ·  FBPA aperture {snap.aperture}"
                      f"  ·  BOOT_0 {snap.boot0}")
        true = snap.mem_true_mhz
        clk = (f"sampled at memory clock {snap.mem_nvml} reported"
               + (f"  =  {true:.0f} MHz true ({snap.mem_type})" if true else "")
               + f"   ·   {time.strftime('%H:%M:%S', time.localtime(snap.wall))}"
               f"   ·   {len(snap.scopes)} partitions")
        dpg.set_value("tim_clock", clk)
        dpg.configure_item("tim_clock",
                           color=TEXT if snap.ns_trustworthy else WARN)
        warn = "\n".join("⚠ " + w for w in snap.warnings)
        dpg.configure_item("tim_warn", show=bool(warn))
        dpg.set_value("tim_warn", warn)
        dpg.set_value("tim_words", "   ".join(
            f"{r} {v:#010X}" for r, v in snap.registers["broadcast"].items()))

        # ---- the decode table --------------------------------------------- #
        n_ns = sum(1 for r in snap.readings if r.ns is not None)
        dpg.set_value("tim_sub", (
            f"{len(snap.readings)} fields, bit ranges parsed from `nvtune "
            f"fields` at runtime so the decode cannot drift from the installed "
            f"tool. {n_ns} convert to nanoseconds"
            + ("" if snap.ns_trustworthy else
               " - but NOT shown, because the clock moved during this capture")
            + ". A cycle count is only a time when you know the clock it was "
            "counted against; that clock is on the line above."))
        dpg.delete_item("tim_table", children_only=True)
        self.tim_columns()
        for r in snap.readings:
            f = r.field
            with dpg.table_row(parent="tim_table"):
                dpg.add_text(f.name, color=WARN if f.inferred else TEXT)
                if f.inferred:
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text("register offset is INFERRED, not "
                                     "established:\n"
                                     + timings.field_table().note_text(
                                         f.register), wrap=self.s(520))
                dpg.add_text(f.register, color=DIM)
                dpg.add_text(f.bits, color=DIM)
                self.bind(dpg.last_item(), "mono")
                dpg.add_text("--" if r.cycles is None else str(r.cycles))
                self.bind(dpg.last_item(), "mono")
                self.tim_ns_cell(r, snap)
                desc = f.description + (" [structural]" if f.structural else "")
                dpg.add_text(desc, color=DIM if f.structural else TEXT,
                             wrap=self.s(self.TIM_COLS[-1][1] - 14))
        self.draw_comparison(caps)
        self.draw_divergence(snap)

    def tim_ns_cell(self, r, snap):
        """The one cell this feature exists to get right: a number ONLY when
        the field converts and the clock it would be converted against held
        still. Everything else says which of those failed, in the cell."""
        if not snap.ns_trustworthy:
            # measured: a capture straddling an 810 -> 7428 reclock turned
            # RC's 42 ns into 385 ns. That number must not reach the screen.
            dpg.add_text("clock moved — no ns", color=BAD)
            self.tim_cell_tip("The memory clock changed while these registers "
                              "were being read, so there is no single clock to "
                              "count them against. Capture again once it "
                              "settles.")
            return
        if r.ns is not None:
            # amber only for an IDLE capture: the figure is a true statement
            # about a state nobody runs work in, and it should not read like
            # the answer to "what are my timings". A P2 capture gets plain
            # text - it IS the answer, bit-identical to P0's.
            dpg.add_text(f"{r.ns:8.2f} ns",
                         color=TEXT if snap.perf_band else WARN)
            self.bind(dpg.last_item(), "mono")
            return
        # RFC and WL land here, and they are never given a number
        dpg.add_text("encodes differently — not ns",
                     color=WARN if r.field.ns_unreliable else DIM)
        self.tim_cell_tip(r.ns_refusal or "no nanosecond conversion")

    def tim_cell_tip(self, text):
        with dpg.tooltip(dpg.last_item()):
            dpg.add_text(text, wrap=self.s(460))

    # ---- the payload: two ratios, side by side ---------------------------- #
    def draw_comparison(self, caps):
        """For each field, its cycle count at every captured memory state and
        the ratio between them, NEXT TO the memory-clock ratio over the same
        states. If a field is a cycle count of a fixed physical time, the two
        must agree - that agreement is the whole proof, so it is drawn as two
        numbers in the same cell rather than left for the reader to divide."""
        base, cratios, rows = timings.compare(caps.values())
        dpg.delete_item("cmp_table", children_only=True)
        if len(caps) < 2:
            dpg.configure_item("cmp_legend", show=False)
            dpg.set_value("cmp_hint", (
                f"{len(caps)} capture{'' if len(caps) == 1 else 's'}. Two are "
                "needed, at DIFFERENT memory "
                "clocks. Let the card idle until the memory clock drops "
                "(~810 reported / ~203 MHz true) and press Capture, then load "
                "it to P0 (~7428 / ~1857 MHz) and press Capture again. The "
                "same registers decode to different cycle counts at the two "
                "states, and this table is where that stops looking like "
                "noise and starts being the proof."))
            return
        snaps = sorted((s for s in caps.values() if s.ok and s.mem_nvml),
                       key=lambda s: s.mem_nvml)
        dpg.set_value("cmp_hint", (
            f"Baseline is the slowest capture. Each column is one captured "
            f"memory state: the cycle count, then × its ratio to the "
            f"baseline. The column heading carries what the CLOCK did over the "
            f"same two states. A field that is a cycle count of a fixed time "
            f"has to move by the clock ratio - the two numbers agreeing is the "
            f"decode being right. Cycle counts are integers, so small ones "
            f"round hard and their ratios are coarse; that is rounding, not "
            f"disagreement."))
        dpg.configure_item("cmp_legend", show=True)
        # every capture labelled with the state it was taken at: a cross-state
        # ratio is what verified the decode, so the idle captures EARN their
        # place here - but not one of them may be mistaken for the reading that
        # describes how the card performs
        dpg.set_value("cmp_legend", "   ".join(
            f"[{s.mem_nvml} reported = "
            + (f"{s.mem_true_mhz:.0f} MHz true" if s.mem_true_mhz else "? MHz")
            + (f", P{s.pstate}" if s.pstate is not None else "")
            + (" — top band, PERFORMANCE-RELEVANT" if s.perf_band
               else " — idle, read-only evidence")
            + f", clock ×{c:.2f}]"
            for s, c in zip(snaps, cratios)))

        dpg.add_table_column(label="field", parent="cmp_table",
                             width_fixed=True, init_width_or_weight=self.s(118))
        dpg.add_table_column(label="register", parent="cmp_table",
                             width_fixed=True, init_width_or_weight=self.s(88))
        for s, c in zip(snaps, cratios):
            tag = s.state_tag
            lab = (f"{s.mem_nvml} {tag}" if c == 1.0
                   else f"{s.mem_nvml} {tag}  (clock ×{c:.2f})")
            dpg.add_table_column(label=lab, parent="cmp_table",
                                 width_fixed=True,
                                 init_width_or_weight=self.s(168))
        dpg.add_table_column(label="verdict", parent="cmp_table",
                             width_fixed=True, init_width_or_weight=self.s(90))
        dpg.add_table_column(label="", parent="cmp_table", width_fixed=True,
                             init_width_or_weight=self.s(430))

        tally = {}
        for row in rows:
            tally[row.verdict] = tally.get(row.verdict, 0) + 1
            with dpg.table_row(parent="cmp_table"):
                dpg.add_text(row.name)
                dpg.add_text(row.register, color=DIM)
                for i, (cyc, rat) in enumerate(zip(row.cycles, row.ratios)):
                    if cyc is None:
                        dpg.add_text("--", color=DIM)
                    elif i == 0:
                        dpg.add_text(f"{cyc:>5}", color=TEXT)
                    else:
                        # measured ratio sits directly under the clock ratio in
                        # the heading; that vertical pairing IS the comparison
                        dpg.add_text(f"{cyc:>5}   ×{rat:5.2f}",
                                     color=self.CMP_COL.get(row.verdict, TEXT))
                    self.bind(dpg.last_item(), "mono")
                dpg.add_text(row.verdict,
                             color=self.CMP_COL.get(row.verdict, TEXT))
                dpg.add_text(row.note, color=DIM, wrap=self.s(416))
        n = tally.get("tracks", 0)
        dpg.set_value("cmp_hint", dpg.get_value("cmp_hint") + (
            f"\n{n} of {len(rows)} fields moved by the memory-clock ratio "
            f"(within the rounding a whole-cycle count carries)"
            f"   ·   {tally.get('flat', 0)} flat"
            f"   ·   {tally.get('partial', 0)} partial"
            f"   ·   {tally.get('--', 0)} no data."))

    def draw_divergence(self, snap):
        """Per-FBPA differences, and NOTHING when there are none. All six
        partitions carry identical words on this card, so the quiet case is one
        line in the table above, not six more tables."""
        dpg.delete_item("div_table", children_only=True)
        if not snap.divergence:
            dpg.configure_item("pan_div", show=False)
            dpg.set_value("tim_sub", dpg.get_value("tim_sub") +
                          f"\nAll {len(snap.scopes)} partitions "
                          f"({', '.join(snap.scopes)}) match the broadcast "
                          f"aperture exactly, so they are not drawn again.")
            return
        dpg.configure_item("pan_div", show=True)
        dpg.set_value("div_sub",
                      f"{len(snap.divergence)} field(s) differ from the "
                      f"broadcast aperture. The broadcast window writes all "
                      f"partitions at once, so a partition reading back "
                      f"differently means one of them is not taking the same "
                      f"timings - worth knowing before trusting the table "
                      f"above as 'the' memory timing.")
        for label, w in (("partition", 110), ("field", 118), ("register", 88),
                         ("broadcast", 90), ("this partition", 110)):
            dpg.add_table_column(label=label, parent="div_table",
                                 width_fixed=True,
                                 init_width_or_weight=self.s(w))
        for dv in snap.divergence:
            with dpg.table_row(parent="div_table"):
                dpg.add_text(dv["scope"], color=WARN)
                dpg.add_text(dv["field"])
                dpg.add_text(dv["register"], color=DIM)
                dpg.add_text(str(dv["broadcast"]))
                self.bind(dpg.last_item(), "mono")
                dpg.add_text(str(dv["value"]), color=BAD)
                self.bind(dpg.last_item(), "mono")

    # ====================================================================== #
    #  main                                                                  #
    # ====================================================================== #
    def run(self):
        if not self.gpu.available():
            print("No GPU backend:", self.gpu.status_line())
            return
        dpg.create_context()
        # Taller than the old 860: the Monitor tab gained the all-domains
        # table, and relayout() only divides up whatever the viewport gives it
        # - at the old height the new panel could show barely half its rows
        # without starving the throttle lamps beside it. Clamped to the real
        # display so a screen smaller than this 4K one still gets a window that
        # fits on it (dpi_scale() has already declared DPI awareness, so
        # GetSystemMetrics reports physical pixels).
        vh = self.s(1120)
        try:
            screen_h = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
        except Exception:
            screen_h = 0
        if screen_h:
            # the s(600) floor is applied INSIDE the screen bound, not after
            # it: max() taken last hands any display shorter than s(670) a
            # window taller than itself, which is the one thing this clamp
            # exists to prevent.
            vh = min(vh, max(self.s(600), screen_h - self.s(70)), screen_h)
        dpg.create_viewport(title="TitanTune", width=self.s(1180), height=vh)
        self.load_fonts()

        st = self.gpu.static
        self.build_menu_bar()             # viewport-owned, so NOT inside 'root'
        with dpg.window(tag="root"):
            # DPG draws the viewport menu bar OVER the primary window instead of
            # insetting it, so without this pad the title row is half-hidden
            # behind File/Device/Profiles/Clocks/Help. relayout() keeps it in
            # step with the bar's measured height.
            dpg.add_spacer(tag="menu_pad", height=self.menu_h())
            with dpg.group(horizontal=True):
                dpg.add_text("TitanTune", tag="hdr", color=ACCENT)
                self.bind("hdr", "big")
                dpg.add_text(f"   {st.get('name')}  \u2022  driver "
                             f"{st.get('driver')}  \u2022  vbios "
                             f"{st.get('vbios')}", color=DIM)
                dpg.add_text("   admin" if st.get("admin")
                             else "   NOT admin (lock/fan/PL need admin)",
                             color=GOOD if st.get("admin") else WARN)
                dpg.add_text("", tag="stale", color=BAD)
            with dpg.tab_bar():
                self.build_monitor()
                self.build_control()      # the V/F editor lives inside this tab
                self.build_timings()      # read-only; see the TIMINGS section
        self.build_tool_windows()         # hidden until the menu bar asks

        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("root", True)
        dpg.set_viewport_resize_callback(self.relayout)
        self.relayout()

        threading.Thread(target=self.poll_loop, daemon=True,
                         name="titantune-poll").start()
        self.sync_lock_ui()          # the gate must LOOK like whatever it is
        self.log(f"backend: {self.gpu.status_line()}")
        self.vf_read()
        # One read-only timing capture at startup, on its own thread, so the
        # Timings tab has something in it the first time it is opened instead
        # of an empty table and a button. It cannot write and it cannot block:
        # worst case the tab says why nvtune or its driver is unavailable.
        self.timings_capture()

        last = 0.0
        # try/finally, not a bare loop: the tail below is what stops this app
        # leaving the card pinned, and the lock is the ONE write that outlives
        # the process unread. Anything the loop raises - a DPG call on a
        # deleted item, a driver hiccup inside relayout - must not be able to
        # skip it. Per-call guards inside the loop keep the app alive; this
        # keeps the promise even when one of them is missing.
        try:
            while dpg.is_dearpygui_running():
                now = time.perf_counter()
                if now - last >= 0.25:
                    last = now
                    self.relayout()
                    with self._lock:
                        d, err, snap_t = (self._snap, self._snap_err,
                                          self._snap_t)
                    if err:
                        self.log_once("read", f"read error: {err}")
                    else:
                        self.clear_once("read")
                    self.set_stale(err, snap_t)
                    # Panels refresh even while a read is failing (Tk did the
                    # same: the snapshot is simply the last good one), and each
                    # panel gets its OWN try/except - sharing one meant a
                    # Monitor glitch also silenced the Control tab's live clock
                    # readout, the only confirmation that an applied offset
                    # took effect.
                    if d:
                        for name, fn in (("monitor", self.refresh_monitor),
                                         ("control", self.refresh_control),
                                         ("timings", self.refresh_timings)):
                            try:
                                fn(d)
                                self.clear_once(name)
                            except Exception as e:
                                self.log_once(name, f"{name} panel: {e}")
                # per FRAME, not on the 4 Hz panel tick: this readout is pinned
                # to a corner of the plot's view, and at 4 Hz it would visibly
                # lag behind the user's own pan. Guarded like the panels above
                # rather than left bare: it ran unprotected directly beneath
                # two deliberate try/excepts, and the drag watchdog now shares
                # the same tick.
                try:
                    self.drag_watchdog()
                    self.update_vf_corner()
                    self.clear_once("corner")
                except Exception as e:
                    self.log_once("corner", f"plot corner: {e}")
                dpg.render_dearpygui_frame()
        finally:
            self._stop.set()
            # before destroy_context: the release goes through set_lock_state,
            # and the DPG items it writes only exist while the context does
            self.release_on_exit()
            dpg.destroy_context()


if __name__ == "__main__":
    TitanTune().run()
