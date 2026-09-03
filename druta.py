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

"""
Druta (Dear PyGui edition) - GPU monitor & tuner for NVIDIA cards.

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
    profiles.autosave(): every write that touches the VF delta
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
    limit, fan and every delta), so a stray click there costs a whole tune
    rather than one table.
    De-flatten is not a write at all - where Tk previewed it on a canvas, it
    STAGES onto the working curve, so the plan is visible on the plot and only
    'Apply to GPU' can commit it. The same is true of the other two planners,
    which are opposites of each other: 'Ramp <= cap' (vf_ramp) makes a band
    strictly increasing so a THROTTLING card has fine steps to descend
    through, and 'Hard de-flatten' (vf_hard_deflatten) makes everything above
    a floor completely FLAT so the arbiter parks at that floor and the power
    estimator is deceived into not throttling at all. Both push a note into
    the plan banner, because the generic sentence describes every plan in
    terms that read as good news - top, peak, park - and cannot say "this
    drops the floor 120 MHz", "the driver will drag 16 points below the floor
    up with it" or "this crashes any card without an external voltage mod".
    Hard de-flatten is additionally gated on an explicit acknowledgement
    checkbox that no tooltip can substitute for.
"""
import ctypes
import math
import os
import subprocess
import sys
import threading
import time

import dearpygui.dearpygui as dpg

import gpuload
import profiles
import shuntmod
import timings
import timingwrite
from nvbackend import (GPU, EVENT_REASONS, PERF_DECREASE_BITS, VF_STEP_KHZ,
                       VFP_POINTS, below_cap, enumerate_gpus, is_admin,
                       same_slot,
                       PRIV_CONFIRMED, PRIV_DOMAIN_ID, PRIV_LIKELY,
                       PRIV_N_DOMAINS, PRIV_PCIE_GEN, PRIV_UNNAMED,
                       PRIV_UNPOPULATED)

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


class Druta:
    def __init__(self, slot=None):
        # ONE card per process, chosen here and never re-targeted afterwards.
        #
        # Switching cards relaunches (see switch_gpu) instead of re-pointing a
        # live GPU, because too much of this window is a per-card measurement
        # baked in at build time to be re-derived safely: the clock sliders take
        # their range from gfx_min/gfx_max (2160 MHz on the Titan RTX, 1911 on
        # the Xp), the V/F editor is sized by the probed table (128 entries vs
        # 84), every nudge is a clock bin (15 MHz vs 12.657), and the domain
        # table's names were earned by correlation against THIS card. A live
        # switch would have to find and redo all of it, and the failure mode of
        # missing one is a control that silently means something else than it
        # says - which is the exact class of bug this whole change exists to
        # remove. A process boundary makes that unmissable instead of careful.
        self.gpu = GPU(slot)
        self.gpu_list = enumerate_gpus()
        # Shunt-mod correction. Held as (rails, folded correction) so the 4 Hz
        # tile update multiplies by a number rather than re-folding the rail
        # list on every frame.
        self.shunt_rails = shuntmod.load()
        self.shunt = shuntmod.correction(self.shunt_rails)
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
        # slot awaiting a second click in the Card menu, or None (see switch_gpu)
        self._switch_armed = None
        # Bumped by every card switch. A worker can be several seconds inside
        # timings.snapshot() or a CUDA induce when the card changes underneath
        # it; it captured self.gpu when it started, so its result describes the
        # card it STARTED on. Each worker stamps this counter on entry and
        # drops its result if the stamp is stale, which is the difference
        # between a discarded capture and one card's registers filed under
        # another card's tab.
        self._gpu_gen = 0
        # ids the last build created outside the window tree - themes and
        # handler registries, which no delete of the tree can reach. See
        # build_ui.
        self._orphans = []
        # True only while the item tree is being torn down and rebuilt. log()
        # honours it: a worker thread writing to "log" during the window where
        # the tab holding it does not exist would be writing to a dead tag.
        self._rebuilding = False
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
        self._undo_t = 0.0         # last accepted Ctrl+Z/Y (same reason)
        self._undo = []            # [(label, vf_work snapshot)], oldest first
        self._redo = []            # the branch Ctrl+Z walked back out of
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
        self._dom_name = {}        # per-domain (label, grade) actually drawn
        self._dom_shown = set()    # domains whose table row is currently shown
        self._plan_themes = {}     # plan-banner box themes, one per band
        self._plan_band = None
        # What a staged RAMP or HARD DE-FLATTEN is, in the plan banner's own
        # words, or None. apply_plan() describes any staged edit generically -
        # point count, top ≤cap, peak, park - and none of those words can say
        # "this plan lowers the floor by 120 MHz", "the driver will drag 16
        # points below the floor up with it" or "without the voltage mod this
        # crashes". The note is cleared wherever the working copy is reset
        # (vf_read, vf_revert), so it can never outlive the edits it describes.
        self._plan_note = None     # None, or {"text": str, "hard": bool}
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
        self._tim_ft = None        # timings.FieldTable, once nvtune has answered
        self._tw_pending = {}      # staged timing writes, field -> new value
        self._tw_base = {}         # field -> cycle count actually in the reg
        self._tw_themes = {}       # cached text-colour themes for the cells
        self._tw_btn = None        # colour band the Apply button is wearing

    # ---- helpers ---------------------------------------------------------- #
    def s(self, n):
        return int(round(n * self.scale))

    def step_khz(self):
        """This card's core-clock grid in kHz - 15000 on TU102, 12657 on GP102.

        Everything in the editor that moves a point by 'one bin' has to ask,
        because a bin is a per-card quantity. VF_STEP_KHZ is only the fallback
        the backend uses when the driver's table cannot be measured."""
        gpu = getattr(self, "gpu", None)
        return gpu.clock_step_khz() if gpu is not None else VF_STEP_KHZ

    def step_mhz(self):
        """The grid in whole MHz, for widgets that can only hold integers.

        Rounded, and on a non-integer grid that rounding is real: GP102's
        12.657 shows here as 13, so a nudge is 'about a bin'. The planners use
        the exact kHz value; only the arrow keys and the sliders see this."""
        return max(1, int(round(self.step_khz() / 1000.0)))

    def n_vf_rows(self):
        """How many V/F delta rows THIS card has, for text that quotes a count.

        Not VFP_POINTS: that is the 128-entry capacity of the NVAPI struct, and
        it is the GPU point count on Turing only by coincidence. GP102 has 80
        GPU points inside an 84-entry table whose last four rows are the memory
        V/F points. Falls back to whatever the editor is currently holding, then
        to the struct capacity, so this can never raise inside a tooltip."""
        gpu = getattr(self, "gpu", None)
        lay = gpu.vfp_layout() if gpu is not None else None
        if lay is not None:
            return lay.n_gpu
        return len(getattr(self, "vf_points", ()) or ()) or VFP_POINTS

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
        # The list above is the real log and is always appended to; only the
        # DRAWING is skipped mid-rebuild, and repaint_log() replays it once the
        # new items exist. Worker threads call this, so without the flag a
        # capture finishing during a card switch would write to a tag that is
        # being deleted on the main thread.
        if self._rebuilding:
            return
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
                # The card selector's own size. Between the title and body
                # text on purpose: which GPU every control on screen is
                # pointed at should be readable without looking for it.
                self._fonts["sel"] = dpg.add_font(sb, self.s(21))
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
    # 'unpopulated' is a domain reading zero on a card that is demonstrably
    # running: it gets no name at all, because the alternative - what this panel
    # actually did on GP102 - is four dead rows labelled GPC / XBAR / SYSCLK /
    # VIDEO in CONFIRMED styling while the real GPU clock sat unnamed elsewhere.
    GRADE_COL = {PRIV_CONFIRMED: TEXT, PRIV_LIKELY: WARN, PRIV_UNNAMED: DIM,
                 PRIV_UNPOPULATED: DIM}

    # Divergence bands, in whole 15 MHz clock bins. Inside one bin is the
    # counter jittering; past one bin the card is genuinely not running at the
    # frequency the tiles report.
    # (the one- and three-bin thresholds now live in dom_band(), which asks the
    # card - as class constants they were fixed at Turing's 15/45 MHz)
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
                    with dpg.table_row(tag=f"dom_row_{dom}", show=False):
                        dpg.add_text(f"{dom:>2}", tag=f"dom_{dom}_ix",
                                     color=DIM)
                        # Written per TICK, not once here. It used to come from
                        # the static PRIV_DOMAIN_ID table, which is a map from
                        # DOMAIN NUMBER to name - exactly the thing that moves
                        # between architectures. The backend now earns each name
                        # against the driver's own core/memory figures, and a
                        # name decided per card has to be drawn per card.
                        dpg.add_text("--", tag=f"dom_{dom}_name", color=DIM)
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
        # bands are ONE and THREE clock bins, so they follow the card's grid
        # rather than a Turing-sized 15/45 MHz
        warn = self.step_khz() / 1000.0
        return ("bad" if d >= 3 * warn else "warn" if d >= warn else "ok")

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
            # the name is now per-card, so it is drawn per tick - but only
            # re-themed when it actually changes, same as the delta band
            grade = r.get("grade", PRIV_UNNAMED)
            label = ((r["name"] + "?") if grade == PRIV_LIKELY
                     else (r["name"] or "--"))
            if self._dom_name.get(dom) != (label, grade):
                self._dom_name[dom] = (label, grade)
                dpg.set_value(f"dom_{dom}_name", label)
                dpg.configure_item(f"dom_{dom}_name",
                                   color=self.GRADE_COL.get(grade, DIM))
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
            # The plot and the plan banner share ONE budget, and the banner is
            # served first. It is measured, not fixed, and it grows with what it
            # has to warn about - a hard de-flatten's note runs three lines longer than
            # "nothing staged" - so left alone it pushes 'Apply to GPU' down the
            # page until the button the banner describes is off screen. A
            # warning that scrolls its own button out of view has defeated
            # itself, and the plot is the one thing here that degrades
            # gracefully: it has a s(240) floor and a fixed 0-3000 MHz pan range,
            # so it loses rows, never reach. PLAN_H_IDLE is what the box measures
            # with nothing staged, i.e. the height this split was tuned around.
            budget = int(H * 0.34) + self.s(self.PLAN_H_IDLE)
            dpg.configure_item("vf_plot",
                               height=max(self.s(240),
                                          budget - self.plan_h()
                                          - self.hard_block_h()))
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
                    "cmp_legend", "div_sub", "tim_state", "tim_induce_note",
                    "tw_warn", "tw_hint", "tw_plan", "tw_result", "tw_backup"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, wrap=wrapw)
        if dpg.does_item_exist("pan_tim_hdr"):
            hdr_h = self.tim_hdr_h(wrapw)
            dpg.configure_item("pan_tim_hdr", height=hdr_h)
            # The write block is always on screen now, and it is not a fixed
            # height - the hazard text wraps and the plan grows when it has
            # warnings. Unaccounted, the panels below share a budget that no
            # longer exists and the comparison table slides off the bottom.
            rest = max(self.s(320),
                       H - hdr_h - self.tw_block_h(wrapw) - self.s(104))
            div_h = (max(self.s(120), int(rest * 0.20))
                     if dpg.is_item_shown("pan_div") else 0)
            tim_h = max(self.s(180), int((rest - div_h) * 0.56))
            cmp_h = max(self.s(150), rest - div_h - tim_h - self.s(30))
            for tag, h in (("pan_tim", tim_h), ("pan_cmp", cmp_h),
                           ("pan_div", div_h)):
                if h and dpg.does_item_exist(tag):
                    dpg.configure_item(tag, height=h)
        self.size_plan_banner()

    def tw_block_h(self, wrap):
        """Vertical cost of the always-visible timing-write block.

        Measured for the same reason tim_hdr_h is: three of its lines wrap and
        two of them are empty until something happens, so a fixed number would
        be wrong at every width and wrong again after the first write."""
        lh = self.text_h("Ag", "ui") or self.s(19)
        h = lh                                    # the heading line
        for tag in ("tw_warn", "tw_hint", "tw_plan", "tw_result", "tw_backup"):
            if dpg.does_item_exist(tag):
                txt = dpg.get_value(tag) or ""
                if txt:
                    h += self.text_h(txt, "ui", wrap) or lh
        h += 2 * (lh + self.s(12))                # the two button rows
        h += self.s(20) + lh                      # spacers and the separator
        return h

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
        # POWER, corrected for a shunt mod when one is configured. The card
        # measures rail current as a voltage across a sense resistor and
        # divides by the resistance it was BUILT to expect, so a modified
        # shunt makes it under-report by exactly R_orig/R_effective - and the
        # limit is enforced on the number it believes, which is why the real
        # ceiling is shown too. See shuntmod.
        pw = d.get("power_w")
        lim = d.get("pl_now_mw", 0) // 1000
        sh = self.shunt
        if sh.active and pw is not None:
            dpg.set_value("t_pwr", f"{sh.apply(pw):.0f}")
            dpg.configure_item("t_pwr", color=WARN if sh.exact else BAD)
            dpg.set_value(
                "s_pwr",
                f"raw {pw:.0f} W x{sh.factor:.4g}"
                + ("" if sh.exact else " est")
                + f"\nlimit {lim} W = {lim * sh.factor:.0f} W real")
        else:
            dpg.set_value("t_pwr", f"{pw:.0f}" if pw is not None else "--")
            dpg.configure_item("t_pwr", color=ACCENT)
            dpg.set_value("s_pwr", f"limit {lim} W")
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
                dpg.add_spacer(width=self.s(20))
                # Beside 'Reset all to stock' deliberately: they are the two
                # ends of the same axis, and the way back should never be
                # further from the hand than the way out.
                dpg.add_button(label="Max it  (fan + power + volts + curve)",
                               tag="go_ocmax", callback=self.oc_max,
                               width=self.s(330), height=self.s(28))
                # ORANGE. Not red - red in this app means "this writes the
                # memory controller and can hang the machine" (tw_apply) and
                # that meaning should not be diluted. Not green either: green
                # is the ordinary V/F apply. Orange is its own band, for the
                # one button that moves four knobs at once.
                with dpg.theme() as ocmax_th:
                    with dpg.theme_component(dpg.mvAll):
                        dpg.add_theme_color(dpg.mvThemeCol_Button, (168, 88, 16))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                            (206, 112, 24))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                            (238, 138, 34))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (24, 16, 6))
                dpg.bind_item_theme("go_ocmax", ocmax_th)
                self._ctl_widgets.append("go_ocmax")
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(
                        "The four things done by hand at the start of every\n"
                        "session, in one click and in a safe order:\n\n"
                        "  1. fan            -> 100%\n"
                        "  2. power limit    -> this card's maximum\n"
                        "  3. voltage boost  -> 100%\n"
                        "  4. V/F curve      -> de-flatten, apply, then hold\n"
                        "                       the cap point (same as Ctrl+H)\n\n"
                        "Headroom first, clocks last: cooling before the power\n"
                        "budget rises, budget before the extra voltage spends\n"
                        "it, and the curve last because it is the only step\n"
                        "that asks for more clock.\n\n"
                        "ONE undo point covers the four WRITES - Profiles >\n"
                        "Undo last write puts them back.\n\n"
                        "IT DOES NOT RELEASE THE HOLD. A lock is driver state,\n"
                        "not a profile value, so Undo leaves the card pinned.\n"
                        "Press Ctrl+H, the Release button, or 'Reset all to\n"
                        "stock' - which returns everything to factory AND\n"
                        "drops the hold.\n\n"
                        "Steps are independent: a card that refuses one still\n"
                        "gets the rest, and each outcome is logged.\n\n"
                        "The fans stay at 100% MANUAL until you press Auto or\n"
                        "Reset all to stock - they do not ramp back down.\n\n"
                        "De-flatten works BELOW the voltage cap in the V/F\n"
                        "editor's cap box, so that box bounds what 'max'\n"
                        "means. Refused if V/F edits are already staged - it\n"
                        "will not write a plan it did not make.\n\n"
                        "De-flatten usually LOWERS the curve's nominal top:\n"
                        "the cap point becomes the unique peak so the arbiter\n"
                        "parks THERE instead of at the bottom of a flat run.\n"
                        "The log says so each time.\n\n"
                        "This raises voltage, power and clocks together. It is\n"
                        "an overclock, and it can destabilise the driver.")
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
                                    note=f"Apply snaps DOWN to this card's "
                                         f"{self.step_khz()/1000:.4g} MHz "
                                         f"grid, then shows what was written")

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
        curve to stock' is a whole-table write that also discards staged
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
        step = self.step_mhz()
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
        # SAME delta table as the curve (see nvbackend.set_clock_offset),
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

    # ---- the opening ritual, in one click --------------------------------- #
    def oc_max(self, sender=None, app_data=None, user_data=None):
        """Fan 100%, power limit to its ceiling, voltage boost 100%,
        de-flatten and apply the curve, then hold the cap point the way Ctrl+H
        does. The session-opening ritual, in the order that keeps it safe.

        THE HOLD IS WHAT MAKES THE REST STICK. A de-flattened curve is a SHAPE;
        without a hold the arbiter still chooses where to sit on it, and it
        picks the lowest voltage of any peak-frequency flat run - the very
        behaviour de-flatten exists to work around.

        THE ORDER IS THE POINT. Headroom first, clocks last: fan before power
        so the cooling is already up when the budget rises, power before
        voltage so the extra voltage has a budget to spend, and the curve last
        because it is the only step that asks for more clock. Reversed, each
        step spends headroom the next one is still about to provide.

        ONE undo point covers the four WRITES - vf_apply is called with
        autosave=False so it does not take a second. It does NOT cover the
        hold: a lock is driver state and profiles.capture deliberately does
        not record one, so 'Undo last write' leaves the card pinned. Ctrl+H,
        Release, or 'Reset all to stock' drop it. Verified on hardware - the
        lock reads back as still in force after an undo. That matters: 'Undo last
        write' loads the NEWEST autosave, so a second point taken after the
        three knobs had already moved would undo only the curve and leave fan,
        power and voltage maxed, which is precisely what this promises not to
        do.

        VALIDATION HAPPENS BEFORE ANY WRITE. The button refuses outright if
        V/F edits are already staged (it will not write a plan it did not
        make - a hard de-flatten left staged for a look is the dangerous case)
        . That refusal leaves the card untouched.

        A de-flatten that LOWERS the nominal peak is DISCLOSED and written
        anyway. Refusing it was the first cut and it was wrong: on the Titan
        RTX at the default cap the top moves 1995 -> 1965 MHz, so the button
        would have refused every press on the card it was written for. Lowering
        the nominal top is what de-flatten does - the cap point becomes the
        unique peak so the arbiter parks there instead of at the bottom of a
        flat run - and the manual path has always allowed it behind a banner.

        Steps are independent: a card that refuses one still gets the rest,
        and every outcome is logged. A refusal here is ordinary - not every
        card exposes a voltage boost, and fan control needs admin."""
        if not self.guard():
            return
        st = self.gpu.static

        # ---- VALIDATE FIRST, WRITE SECOND --------------------------------- #
        # Every refusal below happens before a single register moves, so a
        # refused press costs nothing. Written the other way round - knobs
        # first, curve checks after - a refusal would leave the card sitting
        # at max fan, max power and max volts with the curve untouched, which
        # is a state the user never asked for and did not get told about.
        if not self.vf_points:
            self.vf_read()
        curve = bool(self.vf_points)

        if curve:
            # REFUSE ON A DIRTY WORKING COPY. vf_deflatten stages ON TOP of
            # whatever is already staged and vf_apply writes everything that
            # differs from hardware, so without this the button silently
            # commits edits nobody pressed Apply for. The case that makes it a
            # bug rather than a surprise is a HARD de-flatten staged for a
            # look and left there: its acknowledgement is checked when it is
            # STAGED and never at the write, and the app's own banner says
            # that plan crashes a card without the external voltage mod.
            dirty = sum(1 for i in self.vf_work
                        if self.vf_work[i] != self.vf_orig.get(i))
            if dirty:
                self.log(f"max: {dirty} staged V/F edit(s) pending - Apply or "
                         f"Revert them first. This button will not write a "
                         f"plan it did not make. Nothing was changed.", False)
                return
            # vf_RAMP, not vf_deflatten. The two are named the opposite way
            # round from the buttons: the control labelled "De-flatten" is
            # go_ramp -> vf_ramp, which rebuilds every point from the floor up
            # to the cap; vf_deflatten is the DEMOTED narrow one, sitting in
            # the Clocks menu as "Limited de-flatten". Picking by method name
            # got the wrong planner - "Max it" reported "idx 103 is already
            # the unique top - nothing to do" and left all 14 flat runs below
            # the cap exactly where they were. The curve was not de-flattened,
            # which is precisely what the button promised to do.
            self.vf_ramp()
            # The ramp works BETWEEN the floor and the voltage cap in the two
            # boxes, so "max" is bounded by them. Name the numbers rather than
            # let the button quietly mean something different session to
            # session.
            cap = dpg.get_value("vcap")
            plan = self.apply_plan()
            # A peak-lowering plan is DISCLOSED, not refused. Refusing was the
            # first cut and it was wrong: measured on the Titan RTX, de-flatten
            # at the default 1093.75 mV cap moves the top from 1995 to 1965 MHz,
            # so the button would have refused every press on that card and
            # been useless for the ritual it exists to replace. Lowering the
            # nominal top is what de-flatten DOES - it makes the cap point the
            # unique peak so the arbiter parks THERE instead of at the bottom
            # of a flat run - and the manual De-flatten + Apply path has always
            # allowed it behind a red banner. Being stricter than the manual
            # path for the same write helps nobody.
            if plan and plan.get("lowers_peak"):
                self.log(f"max: heads up - de-flatten below {cap:.1f} mV LOWERS "
                         f"the curve's nominal top. That is what it does: the "
                         f"cap point becomes the unique peak so the arbiter "
                         f"parks there rather than at the bottom of a flat "
                         f"run. Undo last write reverses it.", False)

        # ---- from here on, everything writes ------------------------------ #
        # ONE undo point, and it has to be here: taken before the first write
        # and after every refusal, so the ring never collects a snapshot for a
        # press that changed nothing.
        self.autosave_before("one-click max")
        # No header line: the log renders NEWEST FIRST, so a heading would sit
        # underneath the block it introduces. Each line names itself instead,
        # which reads correctly in either direction.

        def step(label, fn, slider=None, value=None):
            try:
                ok, msg = fn()
            except Exception as e:                              # noqa: BLE001
                ok, msg = False, f"{label}: {e}"
            self.log(f"max: {label} - {msg}", ok)
            # keep the slider honest about what the card actually took, so the
            # knob never shows a value the write did not achieve
            if ok and slider and dpg.does_item_exist(slider):
                dpg.set_value(slider, value)
            return ok

        step("fan 100%", lambda: self.gpu.set_fan(100), "sl_fan", 100)
        pl_max = st.get("pl_max_mw")
        if pl_max:
            step(f"power limit {pl_max // 1000} W",
                 lambda: self.gpu.set_power_limit_mw(pl_max),
                 "sl_pl", pl_max // 1000)
        else:
            self.log("max: power limit - this card reports no maximum", False)
        step("voltage boost 100%",
             lambda: self.gpu.set_voltage_boost(100), "sl_volt", 100)

        if curve:
            self.log(f"max: de-flatten up to {cap:.1f} mV", None)
            # autosave=False: the point above already covers all four. A second
            # one here would be the NEWEST, and 'Undo last write' reads the
            # newest - so one press would put the curve back while leaving fan,
            # power and voltage maxed.
            self.vf_apply(autosave=False)
            self.hold_cap_point(cap)
        else:
            self.log("max: curve not readable on this card - the other three "
                     "still applied", False)

    def hold_cap_point(self, cap):
        """Final step: pin the card on the cap point, the way Ctrl+H does.

        Without this the ritual sets a shape and then leaves the arbiter to
        choose where on it to sit - and the arbiter runs the LOWEST voltage of
        any peak-frequency flat run, which is the behaviour the de-flatten
        exists to work around rather than something to trust afterwards. The
        point lock is what makes the card actually hold the top: measured, it
        keeps true P0 on an idle card where the NVML clock lock leaves memory
        at 810.

        Selects the highest point AT OR BELOW the cap - the same below_cap()
        rule the planner and the readouts use, so the point held is the point
        the plan was built around. Goes through hold_point() rather than
        calling the driver directly, so the on-screen hold record, the handover
        from any existing lock and the read-back all stay in one place."""
        pts = self.work_pts() or []
        under = [p for p in pts if below_cap(p["volt_mv"], cap)]
        if not under:
            self.log(f"max: no V/F point at or below {cap:.1f} mV to hold",
                     False)
            return
        top = max(under, key=lambda p: p["volt_mv"])
        self.vf_select(top["idx"])
        self.hold_point()

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
        offsets, voltage boost, power limit, fan and every delta - and
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
            dpg.add_input_float(tag="vcap", default_value=self.VCAP_DEFAULT,
                                width=self.s(130), step=6.25, format="%.2f",
                                min_value=600.0, max_value=1300.0,
                                min_clamped=True, max_clamped=True,
                                callback=self.vcap_changed)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    "Voltage ceiling for the deflattener (clamped 600-1300 mV).\n"
                    "VF points are 6.25 mV apart, so the reachable top is\n"
                    "the highest point at or below this value - and editing\n"
                    "this box snaps it DOWN onto that same 6.25 mV grid, so\n"
                    "+/- always lands on a voltage a point really has.")
            dpg.add_button(label="Read curve", callback=lambda: self.vf_read(),
                           width=self.s(110))
            gs = self.step_khz() / 1000.0
            dpg.add_button(label=f"Re-phase to {gs:.4g} MHz increments",
                           tag="go_rephase",
                           callback=self.vf_rephase, width=self.s(350))
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    f"Puts every point back on ONE {gs:.4g} MHz phase - this\n"
                    f"card's own grid, read from the driver's lockable-clock\n"
                    f"table, not a fixed 15 MHz.\n\n"
                    f"The clock is floor((base+delta)/step)*step, so two points\n"
                    "only move together when their deltas share a remainder\n"
                    "mod the step. A stray point crosses bin boundaries at a\n"
                    "different offset and silently re-creates a flat.\n\n"
                    f"Off-phase deltas are rounded DOWN, never up, so a point\n"
                    f"can only drop, by at most {gs:.4g} MHz.\n\n"
                    "Rephase is NOT a reset function.\n"
                    "If every point already agrees, it does nothing.")
            dpg.add_button(label="Fit view", callback=self.fit_view,
                           width=self.s(90))
            dpg.add_button(label="Reset curve to stock", tag="go_vfreset",
                           callback=self.vf_reset, width=self.s(170))
        # SECOND row, and deliberately its own: the ramp planners answer a
        # different question from de-flatten (granularity while throttling, not
        # the steady-state park point) and they take a second bound of their
        # own. Sharing the row would have read as four variants of one button.
        with dpg.group(horizontal=True):
            dpg.add_text("ramp floor (mV)")
            # min/max are widened to the CURVE's own range by vf_read, exactly
            # like vf_idx: an input_float carries DPG's bounds and ignores them
            # on entry, so a floor no point has would plan against an empty
            # band. The owner asked to be able to go below 800, so nothing here
            # stops at 800 - only the table's own lowest point does.
            dpg.add_input_float(tag="rfloor",
                                default_value=self.RAMP_FLOOR_DEFAULT,
                                width=self.s(130), step=self.VCAP_STEP,
                                format="%.2f",
                                min_value=300.0, max_value=1300.0,
                                min_clamped=True, max_clamped=True,
                                callback=self.rfloor_changed)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    "Bottom of the band the ramp rebuilds. Snapped DOWN onto\n"
                    "the same 6.25 mV grid as the voltage cap, and clamped to\n"
                    "the voltages this curve actually has.\n\n"
                    "Everything BELOW this is left alone: the low-voltage floor\n"
                    "is many points pinned at the minimum clock, and ramping\n"
                    "them means demanding high clocks at tiny voltages.\n\n"
                    "The wider the band, the more the top clips - and a clipped\n"
                    "ramp pays for it at the floor. The staged plan says by how\n"
                    "much, in MHz, before you press Apply.")
            dpg.add_button(label="De-flatten", tag="go_ramp",
                           width=self.s(150), callback=self.vf_ramp)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    f"THE DE-FLATTEN TOOL. Stages a plan (nothing is written\n"
                    f"yet): rebuilds every point from the floor up to the cap\n"
                    f"as a STRICTLY INCREASING {gs:.4g} MHz ramp - one distinct\n"
                    f"frequency per voltage point, no ties.\n\n"
                    "A throttling card walks LEFT down the curve until it is\n"
                    "under budget, and the arbiter can only sit at the LOWEST\n"
                    "voltage carrying each frequency - so every flat run is a\n"
                    "voltage band it cannot use at all. Stock on TU102, between\n"
                    "1050 and 1175 mV, 21 points exist and 4 are usable.\n\n"
                    f"The cap point takes the highest allowed clock and each\n"
                    f"point below it drops one {gs:.4g} MHz bin. Where the cap\n"
                    f"point is ALREADY at the hardware maximum there is nothing\n"
                    f"to raise into, so the band shrinks to the rungs that fit\n"
                    f"rather than sliding down and being flattened - the plan\n"
                    f"says how many were dropped and why.\n\n"
                    "Where it does have headroom this is an OVERCLOCK as well\n"
                    "as a granularity fix - every rung asks for more clock at\n"
                    "its voltage than stock did, so every rung has to be\n"
                    "stable in its own right.\n\n"
                    "'Limited de-flatten' (Clocks menu) is the narrow version:\n"
                    "it only makes the cap point the unique top and leaves the\n"
                    "rest of the band alone.")
        # COLLAPSED, and in a header of its own. Not tidiness: this mode is
        # inert - worse, fatal - on a card without an external voltage mod, so it
        # should take a deliberate act to even see its controls. Keeping it out
        # of the button row also keeps the plan banner and 'Apply to GPU' on
        # screen together, which three more rows of always-visible widgets would
        # have cost (see relayout).
        with dpg.collapsing_header(tag="hdf_hdr",
                                   label="Hard de-flatten  (needs an external "
                                         "voltage mod - read this before using)",
                                   default_open=False):
            # THE GATE, and it is a checkbox rather than a tooltip on purpose: a
            # tooltip is something you can decline to read, and this one is a
            # statement about the user's soldering, not about the software.
            dpg.add_checkbox(
                tag="hdf_ack", default_value=False, callback=self.hdf_ack_changed,
                label="I have a functional hardware voltage mod on this card, "
                      "and `refin_adj` (or this board's equivalent circuit) is "
                      "completely nonoperational")
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    "This tick is never remembered. It starts clear every\n"
                    "session - nothing in this app writes it to disk - and it\n"
                    "clears itself again as soon as the plan it authorised is\n"
                    "written or dropped, so one tick buys exactly one hard\n"
                    "de-flatten.\n\n"
                    "While it is set, the hard floor is drawn on the plot in\n"
                    "red, so 'this card is armed for a mode that assumes a\n"
                    "soldering iron' is visible without opening this header.")
            # the DANGER stays here in red, not in that tooltip: a tooltip is
            # something a user can decline to read, and this is the sentence the
            # whole gate exists for
            dpg.add_text(
                "Without it the card really IS at the floor voltage, cannot "
                "hold the flat top, is asked for high clocks hundreds of mV "
                "lower by the 45 MHz repair, and crashes the driver.",
                tag="hdf_warn", color=BAD, wrap=self.s(1100))
            with dpg.group(horizontal=True):
                dpg.add_text("floor (mV)")
                dpg.add_input_float(
                    tag="hdf_floor", default_value=GPU.HARD_FLOOR_MV,
                    width=self.s(130), step=self.VCAP_STEP, format="%.2f",
                    min_value=300.0, max_value=1300.0,
                    min_clamped=True, max_clamped=True,
                    callback=self.hdf_floor_changed)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(
                        "The voltage the GPU will BELIEVE it is running at, and\n"
                        "the voltage the card will park at. Everything at or\n"
                        "above it is flattened onto one frequency; the arbiter\n"
                        "runs the lowest voltage of a flat run, so that bottom\n"
                        "point is where it lands.\n\n"
                        "800.00 by default. Lower is allowed and lowers the\n"
                        "believed voltage further - it also deepens the cascade\n"
                        "the driver drags out from under the floor, which the\n"
                        "staged plan states in points and MHz.")
                dpg.add_text("   target (MHz)")
                # seeded from the curve's own peak on every read (see vf_read):
                # the target only has to be high enough to keep the card in P0,
                # and the curve's peak is the one number on screen that is
                # certainly high enough and certainly reachable.
                dpg.add_input_int(
                    tag="hdf_target", default_value=2010,
                    step=self.step_mhz(),
                    width=self.s(130),
                    min_value=self.gpu.static.get("gfx_min", 300),
                    max_value=self.gpu.static.get("gfx_max", 2160),
                    min_clamped=True, max_clamped=True,
                    callback=self.hdf_target_changed)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(
                        "The single frequency every point at or above the floor\n"
                        "is set to. Snapped DOWN to the 15 MHz grid - a mid-bin\n"
                        "target floors on evaluation and the ONE frequency the\n"
                        "whole mechanism depends on would quietly become two.\n\n"
                        "It has to be high enough to hold the card in P0, so it\n"
                        "is seeded from the curve's own peak on every read.")
                dpg.add_button(label="Hard de-flatten", tag="go_hardflat",
                               width=self.s(170),
                               callback=self.vf_hard_deflatten)
                with dpg.tooltip(dpg.last_item()):
                    dpg.add_text(
                        "Stages a plan (nothing is written yet): sets EVERY\n"
                        "point at or above the floor to the target frequency -\n"
                        "deliberately making the curve completely flat.\n\n"
                        "THE OPPOSITE OF THE RAMP, on purpose. The ramp removes\n"
                        "flats so a throttling card has fine steps to walk down.\n"
                        "This builds the biggest flat it can, because the arbiter\n"
                        "runs the LOWEST voltage of a peak-frequency flat run:\n"
                        "flatten 72 points onto one frequency and the card asks\n"
                        "for that frequency at the bottom of the run.\n\n"
                        "The point is not performance through granularity, it is\n"
                        "DECEIVING THE POWER ESTIMATOR: the GPU believes it is at\n"
                        "800 mV, computes low power from that belief and stops\n"
                        "throttling - while the real rail is driven externally by\n"
                        "the hard mod and is invisible to all GPU software,\n"
                        "including this app.\n\n"
                        "Needs the acknowledgement above ticked. Without the mod\n"
                        "the card really is at 800 mV and this will crash it.")
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
                dpg.add_inf_line_series([self.VCAP_DEFAULT], label="cap",
                                        tag="vf_capline")
                # Same argument as the cap line, and the ramp needs it more:
                # the floor decides how WIDE the band is, and width is what
                # decides whether the top clips and the floor pays for it. A
                # floor sitting down in the idle rungs is obvious as a line and
                # invisible as a number in a box.
                dpg.add_inf_line_series([self.RAMP_FLOOR_DEFAULT],
                                        label="ramp floor", tag="vf_floorline")
                # Shown ONLY while the hard-mod acknowledgement is ticked, which
                # makes "this card is armed for a mode that assumes a soldering
                # iron" a thing you can see on the picture rather than a
                # checkbox state inside a collapsed header. It is also why it is
                # red where the ramp floor is violet.
                dpg.add_inf_line_series([], label="hard floor",
                                        tag="vf_hardline")
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
        # a third colour, not a second amber one: cap, floor and hold are three
        # different things and two of them bound the same band
        with dpg.theme() as floorth:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvPlotCol_Line, VIOLET,
                                    category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight,
                                    self.s(2), category=dpg.mvThemeCat_Plots)
        dpg.bind_item_theme("vf_floorline", floorth)
        with dpg.theme() as hardth:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvPlotCol_Line, BAD,
                                    category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight,
                                    self.s(3), category=dpg.mvThemeCat_Plots)
        dpg.bind_item_theme("vf_hardline", hardth)
        with dpg.theme() as holdth:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvPlotCol_Line, GOOD,
                                    category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight,
                                    self.s(3), category=dpg.mvThemeCat_Plots)
        dpg.bind_item_theme("vf_holdline", holdth)
        # NOTE: no per-point drag widgets - every dot stays draggable without
        # one item per point. The grab is a PIXEL hit test against the marker
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
            # Labels DERIVED from the card's bin, not hardcoded to Turing's 15.
            # The write was always correct (set_work_freq snaps to step_khz),
            # but on GP102 a button labelled "+15" moved +12.657 - and it sat
            # directly above the keyboard hint, which renders the real figure.
            # Two controls for the same action disagreeing on screen at once.
            _b, _b5 = self.step_mhz(), self.step_mhz() * 5
            for lbl, delta in ((f"-{_b5}", -_b5), (f"-{_b}", -_b),
                               (f"+{_b}", _b), (f"+{_b5}", _b5)):
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
            dpg.add_input_int(tag="vf_set", default_value=0,
                              step=self.step_mhz(),
                              width=self.s(120),
                              min_value=self.gpu.static.get("gfx_min", 300),
                              max_value=self.gpu.static.get("gfx_max", 2160),
                              min_clamped=True, max_clamped=True)
            dpg.add_button(label="Set MHz", width=self.s(90),
                           callback=self.vf_set_freq)
            dpg.add_button(label="Revert edits", width=self.s(120),
                           callback=self.vf_revert)
            # GREEN, and the only coloured button on this row: it is the one
            # control here that reaches the hardware. Its name says both halves
            # of what it does - the re-phase is not a separate step the user has
            # to remember any more (see vf_apply).
            dpg.add_button(label="Re-phase and apply V/F curve to GPU",
                           tag="go_vfapply",
                           width=self.s(300), callback=self.vf_apply)
            with dpg.theme() as vfapply_th:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, (28, 92, 48))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                        (40, 124, 65))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                        (52, 152, 82))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, (232, 248, 236))
            dpg.bind_item_theme("go_vfapply", vfapply_th)
        self._ctl_widgets += ["go_rephase", "go_vfapply", "go_vfreset"]
        dpg.add_text("--", tag="vf_sel_info", color=TEXT)
        self.bind("vf_sel_info", "mono")
        dpg.add_text(f"drag a dot to move it  \u2022  drag anywhere else to "
                     f"pan  \u2022  A/D select  \u2022  W/S move "
                     f"\u00b1{self.step_mhz()} MHz  \u2022  hold Shift for "
                     f"\u00b1{self.step_mhz() * self.SHIFT_MULT}  \u2022  "
                     f"Ctrl+H hold the selected point (again to release)  \u2022  "
                     f"Ctrl+Z / Ctrl+Y undo and redo staged edits",
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
            dpg.add_key_press_handler(dpg.mvKey_Z, callback=self.on_key_undo)
            dpg.add_key_press_handler(dpg.mvKey_Y, callback=self.on_key_redo)

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
        # the working copy has just been rebased on the hardware, so any staged
        # plan is gone and the note describing it must go with it - the banner
        # would otherwise keep warning about a plan that no longer exists. The
        # hard-mod acknowledgement goes at the same moment and for the same
        # reason: it authorised THAT plan, and a write arrives back here.
        self._plan_note = None
        self.clear_hard_ack()
        # The undo history goes too, and it has to. Every snapshot is a set of
        # deltas that only means anything against the baseline it was taken
        # under; after a rebase, restoring one would re-stage edits measured
        # from a curve the card no longer has. Undoing a WRITE is a different
        # feature with a different mechanism - the autosave point Apply takes,
        # reachable from 'Undo last write'.
        self._undo.clear()
        self._redo.clear()
        # also reseed when a re-read comes back WITHOUT the selected index: wf()
        # is a bare dict lookup, so a stale selection raises KeyError out of
        # sync_sel_inputs below and aborts vf_read before the redraw, the axis
        # clamp and the success log - DPG swallows that to stderr, so the tab
        # would just freeze mid-update with nothing in the log.
        if self.vf_sel is None or self.vf_sel not in self.vf_by_idx:
            self.vf_sel = pts[len(pts) // 2]["idx"]
        # the index box may only name a point that EXISTS: an input_int carries
        # DPG's default 0..100 bounds (wrong for any real curve) and ignores
        # them on entry, and an index the curve does not have would leave the
        # box showing one point while every nudge drove another
        if dpg.does_item_exist("vf_idx"):
            dpg.configure_item("vf_idx", min_value=min(self.vf_by_idx),
                               max_value=max(self.vf_by_idx),
                               min_clamped=True, max_clamped=True)
        # and the ramp floor to the voltages this curve HAS - same reason, one
        # dimension over. A floor below the lowest point plans against the whole
        # table (including the idle rungs the ramp exists to leave alone) and a
        # floor above the top one plans against nothing at all.
        if dpg.does_item_exist("rfloor"):
            vlo = min(p["volt_mv"] for p in pts)
            vhi = max(p["volt_mv"] for p in pts)
            dpg.configure_item("rfloor", min_value=vlo, max_value=vhi,
                               min_clamped=True, max_clamped=True)
            # set_value bypasses DPG's clamp, so a default outside this card's
            # range would sit in the box unenforced until first edited
            dpg.set_value("rfloor",
                          min(vhi, max(vlo, float(dpg.get_value("rfloor")))))
        if dpg.does_item_exist("hdf_floor"):
            vlo = min(p["volt_mv"] for p in pts)
            vhi = max(p["volt_mv"] for p in pts)
            dpg.configure_item("hdf_floor", min_value=vlo, max_value=vhi,
                               min_clamped=True, max_clamped=True)
            dpg.set_value("hdf_floor",
                          min(vhi, max(vlo, float(dpg.get_value("hdf_floor")))))
        # The hard-de-flatten target is SEEDED from the curve's own peak on every
        # read, uncapped - peak_info's max clock. It only has to be high enough
        # to hold the card in P0, and the curve's peak is the one number on
        # screen that is certainly high enough and certainly reachable. Seeded
        # rather than left alone for the same reason vf_set is: a target carried
        # over from another curve names a frequency this one may not have.
        if dpg.does_item_exist("hdf_target"):
            pk, _pi, _pm, _n = GPU.peak_info(pts)
            if pk:
                dpg.set_value("hdf_target", int(pk))
        cap = dpg.get_value("vcap")
        peak, pidx, pmv, _npk = GPU.peak_info(pts, cap)
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
        peak, pidx, pmv, npk = GPU.peak_info(pts, cap)
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
    # On the grid (175 * 6.25), so the box shows a real point from the first
    # frame rather than a number that only becomes one once the field is
    # touched. Whether the RAIL reaches it is a per-card question: this one
    # stops near 1093.75, which is idx 103 and exactly this default.
    VCAP_DEFAULT = 1093.75
    # Bottom of the regular ramp band, on the same grid. 1000.00 is not an
    # arbitrary round number: it is where this card's V/F table stops being
    # uniform. Below it the stock curve already steps 12.50 mV / 15 MHz with
    # nothing shadowed, so a ramp there would rewrite points that are already
    # as fine-grained as the grid allows; above it the steps blow out to 31-56
    # mV and 17 of 21 points become unreachable. It is also the widest band
    # whose ramp does not clip on this card - 15 rungs down from 2130 lands on
    # exactly the 1905 stock already has at 1000.00 mV.
    RAMP_FLOOR_DEFAULT = 1000.00

    def rfloor_changed(self, sender=None, app_data=None, user_data=None):
        """Snap the ramp floor onto the 6.25 mV VF-point grid, DOWNWARD, for the
        same reasons as vcap_changed - the +/- buttons step 6.25 mV from
        whatever is in the box, and an unsnapped start walks values no curve has.

        Note the two bounds round the band in OPPOSITE directions and both round
        it inwards: the cap resolves to the highest point at or BELOW it, the
        floor to the lowest point at or ABOVE it (below_cap / above_floor). So
        snapping the floor down can only ever make the box name a voltage the
        band does not reach down to - never quietly annex a point under it."""
        v = float(dpg.get_value("rfloor"))
        snapped = math.floor(v / self.VCAP_STEP + 1e-9) * self.VCAP_STEP
        if abs(snapped - v) > 1e-6:
            dpg.set_value("rfloor", snapped)
        self.vf_redraw()

    def hdf_floor_changed(self, sender=None, app_data=None, user_data=None):
        """Same 6.25 mV snap as the cap and the ramp floor. This one is also the
        voltage the GPU will be told it is running at, so the number in the box
        is a claim about the hardware mod as much as a planner bound."""
        v = float(dpg.get_value("hdf_floor"))
        snapped = math.floor(v / self.VCAP_STEP + 1e-9) * self.VCAP_STEP
        if abs(snapped - v) > 1e-6:
            dpg.set_value("hdf_floor", snapped)
        self.vf_redraw()

    def hdf_target_changed(self, sender=None, app_data=None, user_data=None):
        """Snap the target DOWN to the 15 MHz grid. compute_hard_deflatten does
        this too, but doing it in the box as well is the difference between the
        user being told what will happen and being shown it: a mid-bin target
        floors on evaluation, and the ONE frequency this whole mechanism depends
        on would quietly become two."""
        v = int(dpg.get_value("hdf_target"))
        step = self.step_mhz()
        snapped = (v // step) * step
        if snapped != v:
            dpg.set_value("hdf_target", snapped)

    def hdf_ack_changed(self, sender=None, app_data=None, user_data=None):
        """Ticking the acknowledgement puts the hard floor on the plot; unticking
        takes it off. The redraw is the whole callback - the gate itself is read
        at the moment the button is pressed, not cached here, so there is one
        place that decides whether this mode may run."""
        self.vf_redraw()

    def clear_hard_ack(self):
        """Untick the acknowledgement. Called wherever the working copy is reset
        (vf_read, vf_revert), i.e. whenever the plan it authorised stops
        existing - a write goes through vf_read(force) on its way back, so one
        tick buys exactly one hard de-flatten. It also means the tick cannot
        persist across sessions even by accident: a fresh process starts with the
        box clear, and nothing in this app ever writes it to disk."""
        if dpg.does_item_exist("hdf_ack") and dpg.get_value("hdf_ack"):
            dpg.set_value("hdf_ack", False)

    def vcap_changed(self, sender=None, app_data=None, user_data=None):
        """Snap the cap onto the 6.25 mV VF-point grid. The +/- buttons step by
        6.25 mV from whatever is in the box, so an unsnapped start walks
        1097.25, 1103.50 ... and never names a voltage any curve has.

        DOWNWARD, like every other snap in this app: below_cap() already
        resolves a cap to the highest point at or below it, so flooring makes
        the number in the box the cap that is actually planned against, and a
        typo can only ever ask for LESS voltage than typed, never more.

        Note the cap cannot raise the delivered voltage past what the RAIL
        delivers, which on this card stops near 1093.75 mV. It is NOT bounded
        by where the table ends: on TU102 the 128-point table runs to idx 127 @
        1243.75 mV, so a cap typed above the rail still resolves to a real
        table point rather than being clamped to the reachable ceiling. (Under
        the old 103-point read the table appeared to stop at 1087.50, which is
        why this docstring used to claim 1094 and 1300 resolve alike. They do
        not.) And when the top of the curve is a flat run the arbiter drops to
        its LOWEST voltage anyway - on TU102 8 points hold 1965 MHz from 1050.00
        to 1093.75, so the card parks at idx 96 @ 1050.00 whatever the cap says.

        GP102 is the same story with different numbers, which is why none of
        them belong in code: 80 GPU points, and 17 of them hold 1911 MHz from
        1081.25 mV up, while the rail stops near 1062.5.
        Raising the ceiling is De-flatten's or a ramp's job, not this field's -
        this field only says where the band those two plan against ENDS (the
        ramp floor box says where it begins)."""
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
        if dpg.does_item_exist("vf_floorline"):
            dpg.set_value("vf_floorline", [[float(dpg.get_value("rfloor"))]])
        if dpg.does_item_exist("vf_hardline"):
            # empty list = no line drawn, so the armed state and the picture
            # cannot disagree
            dpg.set_value("vf_hardline",
                          [[float(dpg.get_value("hdf_floor"))]]
                          if dpg.get_value("hdf_ack") else [[]])
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
        # ONE undo point per grab, taken here rather than in on_plot_drag: the
        # drag callback fires every frame the mouse moves, so snapshotting there
        # would fill the history with sub-pixel steps and make Ctrl+Z useless.
        # A grab that never moves the point leaves a no-op entry, which is
        # cheaper than the alternative and still undoes to the same curve.
        self.push_undo(f"drag idx {idx}")
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
        self.vf_nudge(int(user_data or 1) * self.step_mhz() * mult)

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
    # ---- undo / redo for the working copy --------------------------------- #
    # This is UNDO FOR THE EDITOR, not for the card. It moves the STAGED plan
    # backwards and forwards and never touches hardware - the undo that reaches
    # the GPU is the autosave point Apply takes ('Undo last write'). Keeping the
    # two separate matters: Ctrl+Z after an Apply must not silently re-write the
    # table, which is exactly what a single merged history would do.
    UNDO_MAX = 64

    def push_undo(self, label):
        """Snapshot the working copy before a mutation. Every stage-changing
        operation calls this, so one Ctrl+Z undoes one user action rather than
        one point."""
        self._undo.append((label, dict(self.vf_work)))
        if len(self._undo) > self.UNDO_MAX:
            self._undo.pop(0)
        # A new edit invalidates the redo branch, same as every editor.
        self._redo.clear()

    def _restore_work(self, snap):
        self.vf_work = dict(snap)
        self.sync_sel_inputs()
        self.vf_redraw()

    def vf_undo(self):
        if not self._undo:
            self.log("nothing to undo", None)
            return
        label, snap = self._undo.pop()
        self._redo.append((label, dict(self.vf_work)))
        self._restore_work(snap)
        pend = sum(1 for i in self.vf_work
                   if self.vf_work.get(i) != self.vf_orig.get(i))
        self.log(f"undo: {label} ({len(self._undo)} more) - {pend} edit(s) "
                 f"still staged", None)

    def vf_redo(self):
        if not self._redo:
            self.log("nothing to redo", None)
            return
        label, snap = self._redo.pop()
        self._undo.append((label, dict(self.vf_work)))
        self._restore_work(snap)
        self.log(f"redo: {label} ({len(self._redo)} more)", None)

    def on_key_undo(self, sender=None, app_data=None, user_data=None):
        """Ctrl+Z. Same two guards as Ctrl+H: the registry is window-wide, so a
        bare Z must not fire and neither must Ctrl+Z typed into a number box -
        where it is the text box's own undo and stealing it would be worse than
        not having the shortcut."""
        if self.typing() or not dpg.is_key_down(dpg.mvKey_ModCtrl):
            return
        # Auto-repeat is ON in DPG's key-press handler, so a leaned-on Ctrl+Z
        # would unwind the whole stack in half a second. Rate-limited like the
        # hold toggle - though here the cost is a lost plan, not a driver write.
        now = time.monotonic()
        if now - self._undo_t < self.HOLD_REPEAT_S:
            return
        self._undo_t = now
        if dpg.is_key_down(dpg.mvKey_ModShift):   # Ctrl+Shift+Z = redo
            self.vf_redo()
        else:
            self.vf_undo()

    def on_key_redo(self, sender=None, app_data=None, user_data=None):
        """Ctrl+Y."""
        if self.typing() or not dpg.is_key_down(dpg.mvKey_ModCtrl):
            return
        now = time.monotonic()
        if now - self._undo_t < self.HOLD_REPEAT_S:
            return
        self._undo_t = now
        self.vf_redo()

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
        """Druta's answer to Afterburner's Ctrl+L curve lock: pin the card
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
        step = self.step_khz()
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
        self.push_undo(f"nudge idx {self.vf_sel} {int(mhz):+} MHz")
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
        grid = self.step_khz()
        bins = int(math.floor((want - base_f) / grid))
        self.push_undo(f"set idx {self.vf_sel} to {want_mhz} MHz")
        self.set_work_freq(self.vf_sel, base_f + bins * grid)
        # write the LANDED frequency back into the box: the request is floored to
        # a bin, so leaving the asked-for number sitting there would make the
        # box disagree with the point it names
        self.sync_sel_inputs()
        self.vf_redraw()
        self.log(f"idx {self.vf_sel}: asked {want/1000:.0f} -> landed "
                 f"{self.wf(self.vf_sel)/1000:.0f} MHz "
                 f"({grid/1000:.4g} MHz grid)")

    def vf_revert(self):
        # Revert is itself undoable: dropping a whole plan by accident is the
        # single most expensive misclick in this editor, and 'Revert' sits one
        # button away from Apply.
        if self.vf_work != self.vf_orig:
            self.push_undo("revert edits")
        self.vf_work = dict(self.vf_orig)
        self._plan_note = None      # the plan it described has just been dropped
        self.clear_hard_ack()       # and so has the thing it authorised
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
    # What plan_h() measures with nothing staged, in UNSCALED pixels (156 px at
    # this desktop's 150%). relayout() hands the plot 0.34 of the height PLUS
    # this, then serves the banner out of that pot first - so at idle the split
    # is exactly what it always was, and every line the banner grows comes off
    # the plot instead of off the bottom of the page. Not measured live, because
    # the thing being asked is "how tall is this box when it has nothing to say",
    # which cannot be measured while it is saying something.
    PLAN_H_IDLE = 104

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

    # unscaled, and measured rather than guessed for the part that wraps: the
    # checkbox row plus the floor/target/button row, which do not.
    HARD_BLOCK_ROWS = 70

    def hard_block_h(self):
        """Vertical cost of the gated hard-de-flatten block, or 0 while its
        header is collapsed. It sits ABOVE the plot, so an open header pushes
        everything under it down - including 'Apply to GPU', which is the one
        thing that may not go off screen while the banner above it is warning
        about a plan. Same treatment as the banner, then: the plot pays."""
        # A CHILD's visibility, not dpg.is_item_toggled_open(): that flag tracks
        # the user's click and stays False when the header is opened any other
        # way (measured - configure_item(default_open=True) opens the header and
        # leaves toggled_open False). is_item_visible on something inside it
        # answers the question actually being asked, which is whether this block
        # is on the page. It also reads False while another TAB is in front,
        # which is harmless: the plot is not on screen then either, and the next
        # relayout tick (4 Hz) sizes it correctly the moment Control comes back.
        if not (dpg.does_item_exist("hdf_warn")
                and dpg.is_item_visible("hdf_warn")):
            return 0
        return ((self.text_h(dpg.get_value("hdf_warn") or "Ag", "ui",
                             self.plan_wrap()) or self.s(60))
                + self.s(self.HARD_BLOCK_ROWS))

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
        _pk, pidx, pmv, npk = GPU.peak_info(wpts, cap)
        # A plan that drags the peak DOWN (a cap that landed in the low-voltage
        # floor levels the whole upper curve onto it) otherwise reads exactly
        # like a raise - it was the case Tk's confirm dialog existed to catch.
        # A ramp and a hard de-flatten are described by the SAME four words as
        # any other edit - point count, top ≤cap, peak, park - and every one of
        # them reads as good news: the ramp raises all four, and a hard
        # de-flatten's park point moving to 800 mV looks like a triumph rather
        # than a claim about the user's soldering. The note carries what those
        # words cannot - the floor cost, the sub-floor cascade, the mod.
        note = self._plan_note or {}
        return {
            "changed": changed,
            "note": note.get("text", ""),
            "note_hard": bool(note.get("hard")),
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
            body = ("Drag a dot, nudge with W/S, Set MHz, De-flatten or Hard "
                    "de-flatten, and this box says exactly what one click on "
                    "'Re-phase and apply V/F curve to GPU' will write - before "
                    "the click, not after it. That click also re-phases the "
                    "table afterwards, which is usually a no-op and is lossy "
                    "when it is not.")
        else:
            # A hard de-flatten is red for the same reason a peak-lowering plan
            # is: both are one click away from a result the user did not want.
            # It does not displace the peak warning - that one names a change
            # to the curve, this one names a prerequisite outside the computer
            # - so lowers_peak keeps the headline and the note is printed
            # either way.
            band = "bad" if (plan["lowers_peak"] or plan["note_hard"]) else "warn"
            head = ("APPLY TO GPU  ·  ONE CLICK LOWERS THE PEAK"
                    if plan["lowers_peak"] else
                    "APPLY TO GPU  ·  HARD DE-FLATTEN - WITHOUT THE VOLTAGE "
                    "MOD THIS WILL CRASH" if plan["note_hard"] else
                    "APPLY TO GPU  ·  ONE CLICK WRITES THIS")
            # The undo-point sentence is the pre-click half of the bargain that
            # bought single-click Apply, so it may only promise what the click
            # really delivers: the snapshot is ATTEMPTED first, and if it comes
            # back without the delta table (or not at all) the status line
            # below says so in red and the write still goes ahead. Promising a
            # restore here that autosave_before then could not take would be
            # the one lie this box cannot afford.
            body = (plan["warn"] + "Writes " + plan["text"]
                    + ".\n"
                    + (plan["note"] + "\n" if plan["note"] else "")
                    + "An undo point is taken immediately before the write "
                      "and Profiles > Undo last write puts this state back - "
                      "unless the status line reports that the snapshot came "
                      "back incomplete, in which case the write still happens "
                      "and the curve is NOT recoverable from it. Zeroing the "
                      "table is always available via 'Reset curve to stock' "
                      "or a reboot.")
        reset = (f"'Reset curve to stock' is one click too: it zeroes all "
                 f"{self.n_vf_rows()} deltas back to the factory curve and "
                 f"discards {pending} staged edit(s).")
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

    def vf_apply(self, autosave=True):
        """ONE CLICK. The plan banner directly above this button has been
        saying what the click writes for as long as the edits have existed
        (update_plan_banner), and autosave_before makes the write recoverable -
        which is strictly more than the press-again arm gave, since that only
        described the plan once the user had already committed to pressing."""
        if not self.guard() or not self.vf_points:
            return
        # RE-PHASE THE STAGED DELTAS, BEFORE PLANNING, so exactly one write
        # happens and the plan describes what that write contains. Doing it
        # after the write (as this did briefly) meant two writes and a banner
        # that had promised the first one.
        #
        # Normally there is nothing to do: set_work_freq moves deltas by whole
        # grid bins, so anything edited here already shares a phase. What does
        # not is the CORE OFFSET slider - it lands in this same table in whole
        # MHz, and on GP102 a +64 MHz offset is 64000 kHz against a 12657 kHz
        # grid. Those arrive in the working copy through Read curve, so they are
        # points the user never touched, which is why this is logged loudly
        # rather than folded silently into the plan.
        rp, _ph = GPU.compute_rephase(dict(self.vf_work), self.step_khz())
        if rp:
            for i, d in rp.items():
                self.vf_work[i] = int(d)
            self.vf_redraw()
            self.log(f"re-phased {len(rp)} off-phase point(s) (idx "
                     f"{sorted(rp)}) onto one "
                     f"{self.step_khz()/1000:.4g} MHz phase before writing - "
                     f"rounded DOWN, so this is more than the banner above "
                     f"described", None)
        plan = self.apply_plan()
        if plan is None:
            self.log("no edits to apply")
            return
        changed = plan["changed"]
        predicted = {i: self.wf(i) for i in changed}
        # `autosave` is False only for oc_max, which already took a point
        # covering the three knobs it moved first. Taking a second one here
        # would make it the NEWEST, and "Undo last write" reads the newest -
        # so one press would restore the curve while leaving fan, power and
        # voltage maxed, which is exactly what the button promised not to do.
        if autosave:
            self.autosave_before("vf-apply")
        # the log is the only receipt a write leaves anywhere in the app, so
        # the plan goes into it at the moment of commit as well as standing in
        # the banner beforehand - and it lands directly under the undo point
        # that would take it back
        # the planner's note goes into the receipt as well as the banner: a
        # hard de-flatten applied to a card with no voltage mod crashes it, and
        # the log is the only record that survives to say what was written and
        # on what assumption about the hardware it was written
        self.log(plan["warn"] + "writing " + plan["text"]
                 + (". " + plan["note"] if plan["note"] else ""),
                 False if (plan["lowers_peak"] or plan["note_hard"]) else None)
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
        # (the phase correction happened BEFORE the write, at the top of this
        # method, so there is nothing to do here and only one write occurred)
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
            pts, cap, max_khz=(gmax * 1000 if gmax else None),
            step_khz=self.step_khz())
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
                # Reaching here now means BOTH routes are blocked - the boundary
                # cannot be raised (hardware max) and nothing below it could be
                # lowered either. That used to be the common case; since
                # compute_deflatten learned to lower the shadowing points it is
                # a genuine dead end rather than a missing feature.
                self.log(f"idx {b} cannot be made the unique top: it is at the "
                         f"hardware max and the points shadowing it cannot be "
                         f"lowered either", False)
            else:
                self.log(f"idx {b} is already the unique top at "
                         f"\u2264{cap:.0f} mV - nothing to do", True)
            return
        top_before, peak_before = self.curve_top(pts, cap)
        self.push_undo("limited de-flatten")
        for idx, _v, _o, _n, nd in ch:
            self.vf_work[idx] = int(nd)
        self.sync_sel_inputs()
        self.vf_redraw()
        top_after, peak_after = self.curve_top(self.work_pts(), cap)
        note = (" (clamped at hw max)" if meta.get("clamped") else "")
        lowered = meta.get("lowered") or 0
        if lowered:
            # The boundary was already at the hardware maximum, so it was made
            # unique by lowering what shadowed it rather than by raising it.
            # Say which happened: one costs nothing at the park point, the other
            # is an overclock, and they should not read the same in the log.
            note += (f" - the boundary was already at the hardware max, so "
                     f"{lowered} shadowing point(s) below it were LOWERED by "
                     f"{meta.get('lowered_by_mhz', 0):.0f} MHz instead of "
                     f"raising it")
        if not meta.get("unique", True):
            note += (" - a point below is already at the max clock and could "
                     "not be lowered either, so the top cannot be made unique")
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
                    else " - press 'Re-phase and apply V/F curve to GPU' to write"),
                 not down)

    def vf_ramp(self, sender=None, app_data=None, user_data=None):
        """Stage a strictly-increasing ramp onto the working copy - PREVIEW
        only, exactly like De-flatten: nothing reaches the GPU until Apply. See
        GPU.compute_ramp for the reasoning and the measured step table.

        This is the tool for throttling that is going to happen anyway: it gives
        a descending card fine-grained operating points to walk down through.
        `Hard de-flatten` (vf_hard_deflatten) is the opposite transform for the
        opposite problem - throttling that should not happen at all."""
        if not self.vf_points:
            self.log("read the curve first", False)
            return
        lo = float(dpg.get_value("rfloor"))
        cap = float(dpg.get_value("vcap"))
        pts = self.work_pts()
        gmax = self.gpu.static.get("gfx_max")
        ch, _cb, _ca, meta = GPU.compute_ramp(
            pts, lo, cap, max_khz=(gmax * 1000 if gmax else None),
            step_khz=self.step_khz())
        if not ch:
            # Two ways to get here and only one is good news, same distinction
            # de-flatten draws: an empty band is a bound that matched nothing,
            # not a curve that is already a ramp.
            if not meta.get("rungs"):
                self.log(f"no curve points between {lo:.2f} and {cap:.2f} mV - "
                         f"the floor is above the cap, or both bounds sit off "
                         f"this curve; nothing to plan", False)
            else:
                self.log(f"{meta['rungs']} point(s) from {meta['lo_mv']:.2f} to "
                         f"{meta['cap_mv']:.2f} mV are already a "
                         f"{self.step_khz()/1000:.4g} MHz ramp topping out at "
                         f"{meta['top_mhz']:.0f} MHz - nothing to do", True)
            return
        self.push_undo("de-flatten")
        for idx, _v, _o, _n, nd in ch:
            self.vf_work[idx] = int(nd)
        self.sync_sel_inputs()

        # Written TIGHT, and that is a constraint rather than a style: the
        # banner measures itself and grows, and a note long enough to push
        # 'Apply to GPU' below the fold has defeated the reason the banner sits
        # between the plot and the button. The full version of the hard-mod
        # explanation lives in the button's tooltip and in the README; what is
        # here is what must be read without hovering anything.
        cost = meta["floor_cost_mhz"]
        # `rungs` is what was asked for, `delivered` is what the card will run:
        # the driver reshapes the evaluated curve and the delta table cannot show
        # it (GPU.evaluate_curve_law). Quoting only the first would be quoting
        # the one number that is not the point of the feature.
        band = (f"{meta['rungs']} rungs → {meta['delivered']} distinct "
                f"operating points, {meta['lo_mv']:.2f} → "
                f"{meta['cap_mv']:.2f} mV, top {meta['top_mhz']:.0f} MHz")
        # Dropped rungs are not a failure and must not read as one: the band was
        # wider than the headroom, and the alternative to dropping them was
        # emitting rungs the driver would have flattened onto one value.
        if meta.get("dropped_rungs"):
            band += (f"\n{meta['dropped_rungs']} lower rung(s) dropped - "
                     f"{meta['dropped_reason']}. They keep their stock values, "
                     f"which are already increasing.")
        # The floor is the one number the generic banner cannot reach: it talks
        # about the top ≤cap, the peak and the park point, all of which a ramp
        # RAISES. A clipped ramp pays for that at the bottom of the band, and
        # that is the price the owner has to see before the click, not after.
        if cost > 0:
            floor_txt = (f"Top clipped at the {meta['top_mhz']:.0f} MHz hw max, "
                         f"so the floor pays: {meta['lo_mv']:.2f} mV drops "
                         f"{meta['floor_before_mhz']:.0f} → "
                         f"{meta['floor_after_mhz']:.0f} MHz (-{cost:.0f})")
            if meta.get("shadowed"):
                floor_txt += (f" - below the untouched point under the band "
                              f"({meta['under_band_mhz']:.0f} MHz), so the "
                              f"driver raises the bottom {meta['shadowed']} "
                              f"rung(s) onto it and they arrive as ONE flat")
        else:
            # No "the floor went UP" case, and there cannot be one: the ceiling
            # is min(max_khz, floor + rungs-1 bins), so the floor either keeps
            # its frequency or pays for a clip. Anything that made the top
            # anchor unconditionally at the hardware max would break that
            # invariant - and would also start demanding 2130 MHz at whatever
            # voltage the cap happened to name.
            floor_txt = (f"No clip, so the floor keeps its "
                         f"{meta['floor_after_mhz']:.0f} MHz at "
                         f"{meta['lo_mv']:.2f} mV")
        # The one thing that can ask the rail for something OUTSIDE the band. The
        # ramp alone never causes it - its floor never rises above the frequency
        # that point already had - but a hand-drag that pulled a point under the
        # floor down leaves a gap wider than the driver tolerates, and the write
        # then pulls that point back up. It is the hazard the "leave everything
        # below the floor alone" rule exists to avoid, so it is named.
        if meta.get("lifted_below"):
            floor_txt += (f". {meta['lifted_below']} point(s) BELOW the floor "
                          f"get dragged up too (max +{meta['lift_max_mhz']:.0f} "
                          f"MHz): the driver allows at most 45 MHz between "
                          f"neighbouring points, so the curve under the band is "
                          f"pulled up to meet it - more clock at LOWER voltages "
                          f"than the band asked for")
        note = (f"RAMP - {band}. {floor_txt}. Every rung asks more clock at "
                f"its voltage than stock did - the granularity fix and the "
                f"overclock are one edit, so each rung has to be stable.")
        self.stage_note(note, hard=False)
        if not meta.get("unique", True):
            self.log(f"idx {meta['cap_idx']} cannot be made the park point: an "
                     f"untouched point below the floor already holds "
                     f"{meta['top_mhz']:.0f} MHz at a lower voltage", False)
        # BRIEF, and not the note. log() mirrors its last line into vf_status,
        # which sits ABOVE the plot and wraps - so logging the full note pushed
        # the plot, the banner and 'Apply to GPU' a hundred pixels down the page
        # to say a second time what the banner is already saying, permanently,
        # right above the button. The note goes in the log at the moment of the
        # WRITE (vf_apply), where a permanent receipt is the point.
        self.log(f"staged: {band}, {len(ch)} point(s) changed"
                 + (f", floor -{cost:.0f} MHz" if cost > 0 else "")
                 + (f", bottom {meta['shadowed']} rung(s) will be flattened by "
                    f"the driver" if meta.get("shadowed") else "")
                 + " - read the plan box above before pressing Apply")

    def stage_note(self, note, hard):
        """Hand a freshly staged plan's note to the banner. ONE place, because
        both transforms need the same two things done to it and one of them is a
        safety property.

        STICKY HARD. Staging a ramp on top of a hard de-flatten does not unstage
        the flat top underneath it - it is still in the working copy and still in
        what one click on Apply would write - so a later, tamer note must not be
        allowed to take the red away."""
        still_hard = hard or bool((self._plan_note or {}).get("hard"))
        if still_hard and not hard:
            note += (" A HARD DE-FLATTEN IS STILL STAGED UNDERNEATH THIS and "
                     "Apply writes both: 'Revert edits' is what drops it.")
        self._plan_note = {"text": note, "hard": still_hard}
        self.vf_redraw()          # rebuilds the banner from _plan_note

    def vf_hard_deflatten(self):
        """Stage a HARD DE-FLATTEN - PREVIEW only, like every other planner here.
        See GPU.compute_hard_deflatten for the mechanism and the measured
        cascade; this method is the gate and the explanation.

        THE OPPOSITE OF THE RAMP. `Ramp ≤ cap` removes flats so a throttling card
        has fine steps to descend through. This builds the biggest flat it can -
        every point at or above the floor set to one frequency - because the
        arbiter runs the LOWEST voltage of a peak-frequency flat run, so the card
        then parks AT the floor. Not performance through granularity: DECEIVING
        THE POWER ESTIMATOR, so the throttling never starts.

        THE GATE IS A CHECKBOX, NOT A TOOLTIP, and it is read here rather than
        cached anywhere: a tooltip is something a user can decline to read, and
        what is being acknowledged is a fact about their soldering iron that no
        amount of software can check. Without the mod the card really is at the
        floor voltage, the flat top demands clocks it cannot hold, the shape-law
        cascade demands high clocks hundreds of millivolts further down, and the
        driver crashes.

        THE CASCADE IS THE THING TO SHOW. "Nothing below the floor is written" is
        true and it is not the same claim as "nothing below the floor changes":
        the 45 MHz shape law drags 16 points below an 800 mV floor upward, as far
        down as 700.00 mV, worst case 1530 -> 1965 MHz - with no delta written to
        any of them. That belongs in front of the user before the click, not in a
        post-mortem, so it goes in the staged plan."""
        if not self.vf_points:
            self.log("read the curve first", False)
            return
        if not dpg.get_value("hdf_ack"):
            self.log("hard de-flatten refused: tick the hardware-mod "
                     "acknowledgement first. This mode only does anything on a "
                     "card whose core rail is driven externally with `refin_adj` "
                     "(or the equivalent circuit) dead - on any other card it "
                     "demands clocks the real 800 mV cannot hold and crashes the "
                     "driver.", False)
            return
        floor = float(dpg.get_value("hdf_floor"))
        target = int(dpg.get_value("hdf_target"))
        pts = self.work_pts()
        ch, before, after, meta = GPU.compute_hard_deflatten(
            pts, floor, target * 1000, step_khz=self.step_khz())
        if not ch:
            if not meta.get("n_flat"):
                self.log(f"no curve points at or above {floor:.2f} mV - nothing "
                         f"to flatten", False)
            else:
                self.log(f"every point at or above {meta['floor_mv']:.2f} mV is "
                         f"already at {meta['target_mhz']:.0f} MHz - nothing to "
                         f"do", True)
            return
        self.push_undo("hard de-flatten")
        for idx, _v, _o, _n, nd in ch:
            self.vf_work[idx] = int(nd)
        self.sync_sel_inputs()

        park = (f"the card parks at idx {meta['park_idx']} @ "
                f"{meta['park_mv']:.2f} mV / {meta['park_mhz']:.0f} MHz")
        if not meta["parks_at_floor"]:
            # The one way this plan silently misses: a target low enough that an
            # untouched point below the floor still holds the peak takes the park
            # point with it, and the GPU is then told a voltage nobody chose.
            park += (f", NOT at the {meta['floor_mv']:.2f} mV floor - the target "
                     f"is too low, a point below the floor still holds the peak. "
                     f"Raise the target")
        cascade = ""
        if meta["lifted_below"]:
            cascade = (f" The driver then drags {meta['lifted_below']} point(s) "
                       f"BELOW the floor up with it, as far down as "
                       f"{meta['lift_lowest_mv']:.2f} mV, worst case "
                       f"+{meta['lift_max_mhz']:.0f} MHz - no delta is written to "
                       f"any of them (45 MHz max step, shape law), so this asks "
                       f"the rail for high clocks well under the floor.")
        note = (f"HARD DE-FLATTEN - {meta['n_flat']} point(s) at or above "
                f"{meta['floor_mv']:.2f} mV flattened onto ONE frequency, "
                f"{meta['target_mhz']:.0f} MHz, so {park}. NEEDS THE EXTERNAL "
                f"VOLTAGE MOD: the GPU will believe it is at "
                f"{meta['floor_mv']:.2f} mV and compute low power from that "
                f"belief, which is the entire point - without the mod it really "
                f"IS at {meta['floor_mv']:.2f} mV and this crashes it."
                + cascade)
        self.stage_note(note, hard=True)
        self.log(f"staged: hard de-flatten, {len(ch)} point(s) changed, "
                 f"{meta['n_flat']} flat at {meta['target_mhz']:.0f} MHz from "
                 f"{meta['floor_mv']:.2f} mV, park idx {meta['park_idx']} @ "
                 f"{meta['park_mv']:.2f} mV"
                 + (f", +{meta['lifted_below']} point(s) dragged up below the "
                    f"floor" if meta["lifted_below"] else "")
                 + " - read the plan box above before pressing Apply", False)

    def vf_rephase(self):
        """Stage a phase correction onto the WORKING COPY - preview only, like
        every other planner here.

        It used to write the HARDWARE deltas immediately, and refuse whenever an
        edit was staged. Both were wrong way round: the staged deltas are the
        ones about to be written, so they are the ones whose phases have to
        agree, and re-phasing what the card currently holds while a plan sits on
        top of it corrects a curve that is about to be replaced. With nothing
        staged the working copy IS the hardware, so the no-edit case still does
        what it always did - it just goes through Apply like everything else."""
        if not self.vf_points:
            self.log("read the curve first", False)
            return
        gm = self.step_khz() / 1000.0
        changes, _phase = GPU.compute_rephase(dict(self.vf_work),
                                              self.step_khz())
        if not changes:
            self.log(f"all {len(self.vf_work)} staged deltas already share one "
                     f"{gm:.4g} MHz phase - nothing to do", True)
            return
        self.push_undo("re-phase")
        for i, d in changes.items():
            self.vf_work[i] = int(d)
        self.sync_sel_inputs()
        self.vf_redraw()
        # Still the lossy one, and saying so matters more now that it stages:
        # the rounding is visible on the plot before the write instead of being
        # discovered in the log after it.
        self.log(f"staged: re-phase, {len(changes)} off-phase point(s) "
                 f"(idx {sorted(changes)}) rounded DOWN onto one {gm:.4g} MHz "
                 f"phase - press the green Apply to write", None)

    def vf_reset(self):
        """Zeroing every delta only moves the card toward stock, but it is still
        a full table write AND it re-reads with force=True, which throws
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
        self.log(f"zeroing all {self.n_vf_rows()} V/F deltas back to the "
                 f"factory curve, discarding {pending} staged edit(s)", None)
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
        return f"""Druta - device report

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
        'Reset all to stock' and a profile Load. All six write the same
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
                      f"⚠  '{pend[0]}': {pend[1]}.\nA V/F table is "
                      f"{self.n_vf_rows()} frequencies measured on ONE piece "
                      f"of silicon - "
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
                # Picking a card is NOT here any more - it is the combo in the
                # header. A menu is where you put things people go looking
                # for; which GPU every control on screen is pointed at is
                # something they need to SEE. Only the second-window action
                # stays, because that one genuinely is occasional.
                with dpg.menu(label="Open a second window on"):
                    if not self.gpu_list:
                        dpg.add_text("no NVIDIA GPU enumerated", color=BAD)
                    for g in self.gpu_list:
                        dpg.add_menu_item(
                            label=f"{g['name']}   {g['slot']}",
                            user_data=g["slot"],
                            callback=self.open_gpu_window)
                    dpg.add_text("for watching both at once, or holding\n"
                                 "a lock on one while tuning the other",
                                 color=DIM)
                dpg.add_separator()
                dpg.add_menu_item(label="Device report...", user_data="win_device",
                                  callback=self.show_win)
                dpg.add_menu_item(label="Copy device report",
                                  callback=self.copy_device_report)
                dpg.add_separator()
                # nvtune is NOT shipped with Druta, so the Timings tab needs to
                # be pointed at it once. This is that once.
                #
                # Not a licensing constraint: nvtune is GPL-3.0-or-later, the
                # same licence as Druta, at github.com/sebastianmarrufo/nvtune.
                # It is what loading its driver COSTS. nvtunedrv.sys is
                # self-signed with a test certificate, so using it means Secure
                # Boot off, Memory Integrity off, testsigning on, and that
                # certificate imported into LocalMachine\Root - after which it
                # can sign anything the machine trusts. That is a machine-wide
                # security decision about nvtune, and it should be taken from
                # nvtune's author with nvtune's instructions in hand, not
                # arrive inside a monitoring tool.
                dpg.add_menu_item(label="Locate nvtune...",
                                  callback=self.open_locate_nvtune)
                dpg.add_menu_item(label="Forget nvtune location",
                                  callback=self.forget_nvtune)
                # WHERE THIS LIVES IS THE POINT. While nvtune cannot be
                # loaded, the Timings tab carries the button, because that is
                # the one screen where the operator is stuck. Once it loads,
                # the tab drops it and this is all that is left: still
                # reachable - test signing can be turned back off, or need
                # re-applying after a Windows update - but no longer in the
                # way of a tab that now works.
                dpg.add_menu_item(label="Enable test signing...",
                                  callback=self.open_testsign)
                dpg.add_separator()
                dpg.add_menu_item(label="Shunt mod (corrected power)...",
                                  callback=self.open_shunt)
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
                # DEMOTED here from the V/F button row. It answers a narrower
                # question than the main de-flatten - it only makes the cap
                # point the unique top, leaving every flat below it in place -
                # which is what you want for severe thermal throttling with
                # aggressive undervolting, and not what you want the rest of
                # the time. The full ramp is the default now.
                dpg.add_text("LIMITED DE-FLATTEN", color=ACCENT)
                dpg.add_text("Makes only the cap point the unique top; leaves\n"
                             "the rest of the band alone. Niche - the V/F tab's\n"
                             "'De-flatten' rebuilds the whole band and is the\n"
                             "one to reach for first.", color=DIM)
                dpg.add_button(label="Limited de-flatten ≤ cap",
                               tag="go_deflat_ltd", width=self.s(230),
                               callback=self.vf_deflatten)
                dpg.add_separator()
                dpg.add_text("GPU CLOCK LOCK", color=ACCENT)
                dpg.add_text(f"{gmin}-{gmax} MHz lockable", color=DIM)
                dpg.add_separator()
                # clamped to the supported range for the same reason as the
                # sliders: an input_int carries DPG's default 0..100 bounds and
                # ignores them on entry, so a typo here reaches lock_gpu_clocks
                # and comes back as a refusal in the log
                # step from the CARD's clock bin, not 15: these two sat at
                # Turing's grid while the buttons and hints beside them were
                # fixed to derive it, so on GP102 the arrows here stepped
                # 15 MHz onto a 12.657 MHz grid.
                lstep = self.step_mhz()
                dpg.add_input_int(tag="lock_min", label="min MHz",
                                  default_value=gmin,
                                  width=self.s(130), step=lstep,
                                  min_value=gmin, max_value=gmax,
                                  min_clamped=True, max_clamped=True)
                dpg.add_input_int(tag="lock_max", label="max MHz",
                                  default_value=gmax,
                                  width=self.s(130), step=lstep,
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
                # Not optional furniture. This is how a recipient of the
                # onefile exe actually gets at the licence text bundled with
                # it - see resource_path and Druta.spec's datas.
                dpg.add_menu_item(label="Licences", user_data="win_licence",
                                  callback=self.show_win)
                dpg.add_menu_item(label="About", user_data="win_about",
                                  callback=self.show_win)

    # Only the bindings that EXIST in build_vf's handler_registry, and only the
    # gestures on_plot_click / on_plot_drag really implement. A list naming a
    # key nothing implements is worse than no list at all - and so is one
    # describing a gesture the code has since replaced, which is what the
    # Tk-era "nearest by voltage" and "middle-drag pans" rows had become. Both
    # rules bind the same way: anything added to that registry, or any change to
    # which button does what, has to move these rows in the same change.
    def vf_keys(self):
        """Built per card, not a class constant: the bin is 15 MHz on TU102 and
        12.657 on GP102, and a shortcut list that states the wrong one is the
        same defect as a button labelled '+15' that moves 12.657."""
        b = self.step_mhz()
        return [
        ("W / S", f"move the selected point +/- {b} MHz (one clock bin)"),
        ("A / D", "select the previous / next point along the curve"),
        ("Shift + W/S",
         f"move {self.SHIFT_MULT} bins at once (+/- {b * self.SHIFT_MULT} MHz)"),
        ("Shift + A/D",
         f"step the selection {self.SHIFT_MULT} points at a time"),
        ("Ctrl + Z", "undo the last staged edit (64 deep). This moves the "
                     "STAGED plan only - it never touches the card. Undoing a "
                     "write is 'Profiles > Undo last write'"),
        ("Ctrl + Y", "redo (Ctrl+Shift+Z does the same)"),
        ("left-click ON a dot",
         "select it. The hit test is in SCREEN PIXELS - the press has to land "
         "within ~14 px of the drawn marker - not 'the nearest point by "
         "voltage', which made empty sky a grab handle for whatever dot shared "
         "that column"),
        ("left-drag ON a dot",
         "move it; snaps to whole clock bins. Vertical only: voltage is fixed "
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
                         f"{self.n_vf_rows()} V/F deltas, as JSON in profiles/ "
                         "next to the app. Saving writes nothing to the GPU.",
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
                         f"{self.n_vf_rows()} driver rows. An undo point is taken "
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
                for keys, what in self.vf_keys():
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

        with dpg.window(label="Shunt mod", tag="win_shunt", show=False,
                        width=self.s(900), height=self.s(560),
                        pos=[self.s(150), self.s(110)]):
            dpg.add_text("Shunt-mod corrected power", color=ACCENT)
            dpg.add_text(
                "A board measures rail current as the voltage across a sense "
                "resistor, then divides by the resistance it was BUILT to "
                "expect. Lower that resistance and the card believes it is "
                "drawing less than it is - which is the point, because the "
                "power limit is enforced on the believed number.",
                color=DIM, wrap=self.s(860))
            dpg.add_spacer(height=self.s(4))
            dpg.add_text(
                "Give each rail its ORIGINAL and its EFFECTIVE resistance. "
                "A resistor soldered on top of an existing shunt bridges the "
                "same two pads, so the two are in PARALLEL and the value "
                "HALVES: 5 -> 2.5 mOhm is x2. Replacing the part outright "
                "works the same way - only the two numbers matter, not how "
                "you got there.", color=DIM, wrap=self.s(860))
            dpg.add_spacer(height=self.s(8))
            with dpg.table(tag="shunt_table", header_row=True,
                           policy=dpg.mvTable_SizingFixedFit,
                           borders_innerH=True, borders_outerH=True,
                           borders_innerV=True, borders_outerV=True):
                for lbl, w in (("rail", 130), ("original mOhm", 150),
                               ("effective mOhm", 150), ("multiplier", 110),
                               ("", 90)):
                    dpg.add_table_column(label=lbl, width_fixed=True,
                                         init_width_or_weight=self.s(w))
            dpg.add_spacer(height=self.s(8))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Add 8-pin", width=self.s(130),
                               user_data="pin8", callback=self.shunt_add)
                dpg.add_button(label="Add 6-pin", width=self.s(130),
                               user_data="pin6", callback=self.shunt_add)
                dpg.add_button(label="Add PCIe slot", width=self.s(150),
                               user_data="slot", callback=self.shunt_add)
                dpg.add_spacer(width=self.s(20))
                dpg.add_button(label="Reset to stock (no mod)",
                               width=self.s(220), callback=self.shunt_reset)
            dpg.add_spacer(height=self.s(10))
            dpg.add_separator()
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("", tag="shunt_result", wrap=self.s(860))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("", tag="shunt_note", color=DIM, wrap=self.s(860))
            dpg.add_spacer(height=self.s(10))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", width=self.s(140),
                               callback=self.shunt_save)
                dpg.add_spacer(width=self.s(14))
                dpg.add_button(label="Close", width=self.s(140),
                               callback=lambda: dpg.configure_item(
                                   "win_shunt", show=False))

        with dpg.window(label="Enable test signing", tag="win_testsign",
                        show=False, modal=True, no_resize=False,
                        width=self.s(860), height=self.s(620),
                        pos=[self.s(140), self.s(90)]):
            dpg.add_text("To load nvtune's driver, Windows must be in test "
                         "signing mode.", color=ACCENT, wrap=self.s(820))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("Run these in an elevated CMD, with Secure Boot "
                         "DISABLED in firmware:", wrap=self.s(820))
            # copiable: a read-only multiline input is selectable and
            # Ctrl+C-able, which a plain text item is not
            dpg.add_input_text(tag="ts_cmds", multiline=True, readonly=True,
                               width=-1, height=self.s(74),
                               default_value="\n".join(
                                   " ".join(c) for c in self.TESTSIGN_CMDS))
            self.bind("ts_cmds", "mono")
            dpg.add_text("Only the third one does the work. Microsoft "
                         "documents `testsigning` as what makes Windows load "
                         "test-signed kernel code; `nointegritychecks` is "
                         "documented as ignored on modern Windows, and "
                         "DISABLE_INTEGRITY_CHECKS is not a documented "
                         "setting at all. The first two are here because they "
                         "are the recipe known to work on this rig.",
                         color=DIM, wrap=self.s(820))
            dpg.add_spacer(height=self.s(6))
            dpg.add_separator()
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("", tag="ts_state", wrap=self.s(820))
            dpg.add_spacer(height=self.s(4))
            dpg.add_text("NOTHING TAKES EFFECT UNTIL YOU REBOOT.",
                         color=WARN, wrap=self.s(820))
            dpg.add_text(
                "This lowers a kernel security boundary for the whole "
                "machine, not just for Druta: any test-signed driver will "
                "load afterwards, and Windows shows a Test Mode watermark. "
                "To undo it, run the same shell with:", wrap=self.s(820))
            dpg.add_input_text(tag="ts_undo", multiline=True, readonly=True,
                               width=-1, height=self.s(74),
                               default_value="\n".join(
                                   " ".join(c) for c in self.TESTSIGN_UNDO))
            self.bind("ts_undo", "mono")
            dpg.add_spacer(height=self.s(8))
            with dpg.group(horizontal=True):
                dpg.add_button(label="OK", width=self.s(120),
                               callback=lambda: dpg.configure_item(
                                   "win_testsign", show=False))
                dpg.add_spacer(width=self.s(16))
                dpg.add_button(
                    label="I accept the risk - run these commands now",
                    tag="ts_run", width=self.s(430), callback=self.run_testsign)
                with dpg.theme() as ts_th:
                    with dpg.theme_component(dpg.mvAll):
                        dpg.add_theme_color(dpg.mvThemeCol_Button, (140, 28, 32))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                            (180, 38, 42))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                            (212, 48, 52))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 236, 236))
                dpg.bind_item_theme("ts_run", ts_th)
            dpg.add_spacer(height=self.s(4))
            dpg.add_text("", tag="ts_result", wrap=self.s(820))

        # The outcome gets its OWN modal rather than a line in the dialog
        # behind it. Two reasons: a bcdedit failure is the thing most worth
        # not scrolling past, and success needs an answer to a question
        # ("reboot?") rather than an acknowledgement.
        with dpg.window(label="Test signing", tag="win_ts_done", show=False,
                        modal=True, width=self.s(720), height=self.s(400),
                        pos=[self.s(200), self.s(150)]):
            dpg.add_text("", tag="tsd_head", wrap=self.s(680))
            dpg.add_spacer(height=self.s(6))
            dpg.add_input_text(tag="tsd_detail", multiline=True, readonly=True,
                               width=-1, height=self.s(190))
            self.bind("tsd_detail", "mono")
            dpg.add_spacer(height=self.s(8))
            with dpg.group(horizontal=True, tag="tsd_ok_row", show=False):
                dpg.add_button(label="Reboot now", tag="tsd_reboot",
                               width=self.s(180), callback=self.reboot_now)
                with dpg.theme() as tsd_th:
                    with dpg.theme_component(dpg.mvAll):
                        dpg.add_theme_color(dpg.mvThemeCol_Button, (140, 28, 32))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                            (180, 38, 42))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                            (212, 48, 52))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 236, 236))
                dpg.bind_item_theme("tsd_reboot", tsd_th)
                dpg.add_spacer(width=self.s(16))
                dpg.add_button(label="Later", width=self.s(160),
                               callback=lambda: dpg.configure_item(
                                   "win_ts_done", show=False))
                dpg.add_spacer(width=self.s(16))
                dpg.add_text("reboot starts in 10s; 'shutdown /a' aborts it",
                             color=DIM)
            with dpg.group(horizontal=True, tag="tsd_bad_row", show=False):
                dpg.add_button(label="Close", width=self.s(160),
                               callback=lambda: dpg.configure_item(
                                   "win_ts_done", show=False))

        with dpg.window(label="Licences", tag="win_licence", show=False,
                        width=self.s(860), height=self.s(640),
                        pos=[self.s(120), self.s(90)]):
            dpg.add_text("Druta  -  GNU General Public License, version 3 or "
                         "later", color=ACCENT)
            dpg.add_text("Copyright (C) 2026 Thermetery Technology Co Limited",
                         color=DIM)
            dpg.add_spacer(height=self.s(4))
            with dpg.tab_bar():
                with dpg.tab(label="GPL-3.0 (Druta)"):
                    dpg.add_input_text(default_value=self.read_licence("COPYING"),
                                       multiline=True, readonly=True,
                                       width=-1, height=self.s(520),
                                       tag="lic_gpl")
                    self.bind("lic_gpl", "mono")
                with dpg.tab(label="Third-party notices"):
                    dpg.add_text(
                        "Licences of software redistributed inside Druta.exe. "
                        "Running from source redistributes none of it.",
                        color=DIM, wrap=self.s(820))
                    dpg.add_input_text(
                        default_value=self.read_licence(
                            "THIRD-PARTY-NOTICES.md"),
                        multiline=True, readonly=True,
                        width=-1, height=self.s(496), tag="lic_third")
                    self.bind("lic_third", "mono")

        # GPLv3 section 5(d): "If the work has interactive user interfaces,
        # each must display Appropriate Legal Notices." Section 0 defines those
        # as "a convenient and prominently visible feature that (1) displays an
        # appropriate copyright notice, and (2) tells the user that there is no
        # warranty for the work ..., that licensees may convey the work under
        # this License, and how to view a copy of this License."
        #
        # This box is that feature - the GPL's own appendix names an "about
        # box" as the GUI equivalent of the terminal startup notice. The
        # s5(d) carve-out ("if the Program has interactive interfaces that do
        # not display Appropriate Legal Notices, your work need not make them
        # do so") does NOT apply: it exempts a modifier from retrofitting
        # notices onto an upstream work that lacked them, and there is no
        # upstream here - Druta is original work.
        with dpg.window(label="About Druta", tag="win_about", show=False,
                        width=self.s(620), height=self.s(430),
                        pos=[self.s(180), self.s(160)]):
            dpg.add_text("Thermetery Druta", color=ACCENT)
            dpg.add_text("Copyright (C) 2026 Thermetery Technology Co Limited")
            dpg.add_text(
                "This program comes with ABSOLUTELY NO WARRANTY. It is free "
                "software, and you are welcome to redistribute it under the "
                "terms of the GNU General Public License, either version 3 of "
                "the License, or (at your option) any later version. See "
                "Help > Licences for the full text, the COPYING file "
                "distributed with this program, or "
                "<https://www.gnu.org/licenses/gpl-3.0.html>.",
                wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text(
                "Druta bundles third-party software under its own terms - "
                "see Help > Licences. nvtune is a separate program and is "
                "not bundled.", color=DIM, wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text(
                "DRUTA IS AN INDEPENDENT, UNOFFICIAL TOOL. IT IS NOT "
                "AFFILIATED WITH, SPONSORED BY, OR ENDORSED BY NVIDIA "
                "CORPORATION, ASUSTEK COMPUTER INC., OR MICRO-STAR "
                "INTERNATIONAL CO., LTD.", color=WARN, wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_separator()
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("Monitor and tuner for TU102 (Titan RTX) and GP102 "
                         "(Titan Xp). Per-card quantities - V/F point count, "
                         "clock grid, domain names - are probed from the "
                         "driver rather than assumed. Developed against a "
                         "Titan RTX die on an ASUS RTX 2080 Ti Strix board, "
                         "and a stock Titan Xp.", wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("README.md, shipped beside this app, is the single "
                         f"source of truth for the hardware: the "
                         f"{self.step_khz() / 1000:.3f} MHz clock "
                         "quantisation and phase rules, the two-knob voltage "
                         "mechanism, what is reversible, and the footguns this "
                         "tool deliberately does not put behind a button.",
                         color=DIM, wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("This window follows one card at a time. Device > "
                         "Card switches in place, rebuilding every control "
                         "from the new card's own measurements; the same menu "
                         "opens a second window if you want to watch both.",
                         color=DIM, wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text("The core/mem offset sliders and the V/F curve are the "
                         "SAME delta table, and Afterburner writes it too - "
                         "drive clocks from ONE tool at a time.", color=WARN,
                         wrap=self.s(580))
            dpg.add_spacer(height=self.s(6))
            dpg.add_text(f"backend: {self.gpu.status_line()}", color=DIM)

    # ====================================================================== #
    #  TIMINGS                                                               #
    # ====================================================================== #
    # THE READ PATH cannot write: timings.py talks to nvtune through a
    # whitelist of read-only subcommands and refuses
    # --commit/--force/set/restore/apply/daemon before a process is created.
    # Writing an FBPA timing register can hang the machine and corrupt VRAM,
    # so that refusal is code, not a convention (see timings._check_argv).
    #
    # THE TAB ITSELF CAN WRITE, through one other module. timingwrite.py is the
    # only thing in Druta that may build a writing argv, and the Edit panel
    # below drives it. This banner used to say the tab could not write at all,
    # which was false from the moment timingwrite.py landed and contradicted
    # TIM_READONLY twenty lines further down.
    #
    # WHAT THE TAB IS FOR: a timing register holds a CYCLE COUNT, and a cycle
    # count is meaningless without the memory clock it was counted against.
    # The same registers read as garbage at idle and as textbook GDDR6 at P0.
    # So every number here is shown WITH the clock its snapshot was taken at,
    # and the comparison view puts what a field's cycle count did next to what
    # the clock did between the same two states - two ratios agreeing is what
    # proves the decode is real.
    TIM_COLS = (("field", 118), ("register", 88), ("bits", 76),
                ("cycles", 62), ("new value", 104),
                ("ns at the capture clock", 250), ("what it is", 560))

    # "This tab cannot write" was still being said here long after it stopped
    # being true - the same claim a comment further up already records as
    # having been wrong. The boundary is between MODULES, not tabs.
    TIM_READONLY = ("Reading goes through nvtune's read-only subcommands: "
                    "timings.py cannot build a writing command line at all. "
                    "Writing lives in one other module, timingwrite.py, and "
                    "THIS TAB DRIVES IT - the Apply button below is that "
                    "module.")

    TIM_WRITEWARN = (
        "WRITING A TIMING REGISTER CAN HANG THE MACHINE AND CORRUPT VRAM. "
        "Measured on our hardware: GP102 (Pascal) accepts these writes; TU102 "
        "(Turing) rejects every one of them at the hardware. A stock backup for "
        "THIS card is taken before the first write, and 'Restore stock' puts it "
        "back. Timings are selected per memory CLOCK BAND, so a write edits the "
        "band the card is in right now.")

    # verdict -> colour for the comparison table. 'flat' is DIM, not red: a
    # field that does not move with the clock is usually a mode or bus
    # turnaround value counted in cycles by design, not a broken decode.
    CMP_COL = {"tracks": GOOD, "flat": DIM, "partial": WARN, "--": DIM}

    def build_timings(self):
        # The capture controls used to sit in a row of their own at the top of
        # the tab, above a paragraph arguing that a P2 capture is as good as a
        # P0 one. Both were written when P0 was hard to reach. Ctrl+H holds it
        # directly now, so the argument is gone and the buttons have moved down
        # beside the edit controls - one block of actions instead of two.
        with dpg.tab(label="  Timings  (needs nvtune)  ", tag="tab_tim"):
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
            # Said once, at the top, in the tab's own colour rather than
            # buried in the read-only paragraph: this is the only tab that
            # does nothing at all without a second program installed, and the
            # tab label alone is easy to miss once the tab is open.
            # THE SETUP SCREEN. Shown INSTEAD of the tab's contents, not above
            # them: without nvtune every control below is inert, and a live
            # -looking table nobody can refresh is worse than an empty tab. It
            # names which of the two halves is missing and puts that half's
            # action first, because "not available" is not something anyone can
            # act on - "the exe is not where I looked" and "the driver is not
            # running" have completely different fixes.
            with dpg.group(tag="tim_needs", show=False):
                dpg.add_spacer(height=self.s(20))
                dpg.add_text("NVTUNE IS NOT LOADED", tag="tim_setup_head",
                             color=BAD)
                self.bind("tim_setup_head", "big")
                dpg.add_spacer(height=self.s(6))
                dpg.add_text("", tag="tim_setup_what", color=TEXT,
                             wrap=self.s(980))
                dpg.add_spacer(height=self.s(10))
                # the specific degradation, from timings.available()
                dpg.add_text("", tag="tim_reason", color=WARN,
                             wrap=self.s(980))
                dpg.add_spacer(height=self.s(16))
                dpg.add_text("", tag="tim_step1", color=ACCENT)
                self.bind("tim_step1", "sel")
                dpg.add_spacer(height=self.s(4))
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Locate nvtune.exe...",
                                   tag="tim_locate", height=self.s(34),
                                   width=self.s(260),
                                   callback=self.open_locate_nvtune)
                    dpg.add_spacer(width=self.s(14))
                    # Its driver is test-signed, so it needs a machine in test
                    # signing mode. The button EXPLAINS before it offers - see
                    # open_testsign; the commands are shown, copiable, and the
                    # red one is dead unless the machine can take them.
                    dpg.add_button(label="Enable test signing...",
                                   tag="tim_testsign", height=self.s(34),
                                   width=self.s(260),
                                   callback=self.open_testsign)
                dpg.add_spacer(height=self.s(16))
                dpg.add_text(
                    "nvtune is a separate program by Sebastian Marrufo, "
                    "GPL-3.0-or-later, not shipped with Druta:",
                    color=DIM, wrap=self.s(980))
                dpg.add_input_text(tag="tim_upstream", readonly=True,
                                   width=self.s(560),
                                   default_value=timings.NVTUNE_HOME)
                self.bind("tim_upstream", "mono")
                dpg.add_spacer(height=self.s(10))
                dpg.add_text(
                    "Everything else in Druta works without it - only this tab "
                    "needs BAR0. Its driver is self-signed, so loading it costs "
                    "Secure Boot, Memory Integrity and driver-signature "
                    "enforcement, machine-wide. Read what the button says "
                    "before you use it.", color=DIM, wrap=self.s(980))
            # EVERYTHING THE TAB DOES, in one group, so it can be taken
            # off screen wholesale when nvtune is not loaded. A dead table
            # over a dead write panel with one small band of explanation
            # at the top is not a state anyone can act on; the setup panel
            # above replaces it entirely.
            with dpg.group(tag="tim_work"):
                dpg.add_spacer(height=self.s(4))
                dpg.add_text(self.TIM_READONLY, tag="tim_ro", color=DIM,
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

                # NOT behind a collapsing header. It was, on the reasoning that a
                # writing control should take a deliberate act to reach - but the
                # act it actually gated was reading the warning, while the editable
                # cells it describes sat in plain sight in the table below. A fold
                # that hides the explanation and not the controls is worse than no
                # fold, so the whole thing is exposed and the hazard text leads.
                dpg.add_spacer(height=self.s(6))
                dpg.add_text("EDIT TIMINGS  ·  writes to the memory controller",
                             color=BAD)
                dpg.add_text(self.TIM_WRITEWARN, color=BAD,
                             wrap=self.s(1100), tag="tw_warn")
                dpg.add_text("Edit the 'new value' column in the table below. A "
                             "cell is green while it matches the register and red "
                             "once it does not; only red rows are written.",
                             color=DIM, wrap=self.s(1100), tag="tw_hint")
                dpg.add_spacer(height=self.s(4))
                dpg.add_text("no edits - every cell matches the register",
                             tag="tw_plan", color=TEXT, wrap=self.s(1100))
                dpg.add_spacer(height=self.s(4))
                # Two rows, grouped by what the controls DO. `force` modifies Apply
                # and belongs beside it; Revert and Restore are the two ways back
                # and belong together. One ragged row mixing a checkbox with three
                # different button widths is what this replaced.
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Apply to memory controller",
                                   tag="tw_apply", width=self.s(260),
                                   callback=self.tw_apply)
                    dpg.add_spacer(width=self.s(10))
                    dpg.add_checkbox(label="force  (write despite range warnings)",
                                     tag="tw_force")
                dpg.add_spacer(height=self.s(2))
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Revert edits", tag="tw_clear",
                                   width=self.s(150), callback=self.tw_clear)
                    dpg.add_spacer(width=self.s(10))
                    dpg.add_button(label="Restore stock", tag="tw_restore",
                                   width=self.s(150), callback=self.tw_restore)
                dpg.add_spacer(height=self.s(6))
                dpg.add_separator()
                dpg.add_spacer(height=self.s(6))
                # READ controls, sharing the write panel's block rather than a row
                # of their own at the top of the tab. The separator above is what
                # keeps them out of the red write block: nothing below it writes a
                # register. (It was described here before it existed - the comment
                # claimed a boundary the layout did not draw.)
                with dpg.group(horizontal=True):
                    # THE read button, and the only one that gets you a reading
                    # worth having. Blue: it is the primary action on this tab, and
                    # it is the one that changes the card's state.
                    dpg.add_button(label="Read memory timings  (will hold P0)",
                                   tag="tim_read", width=self.s(300),
                                   callback=self.timings_read_p0)
                    with dpg.theme() as read_th:
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_color(dpg.mvThemeCol_Button, (26, 84, 152))
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                                (34, 108, 192))
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                                (44, 132, 232))
                            dpg.add_theme_color(dpg.mvThemeCol_Text, (238, 245, 255))
                    dpg.bind_item_theme("tim_read", read_th)
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(
                            "Puts the card in its top memory band and reads the\n"
                            "timing registers there - the only state whose timings\n"
                            "mean anything.\n\n"
                            "It HOLDS P0 with the V/F point lock, the same lock\n"
                            "Ctrl+H uses, and LEAVES IT ON. That is deliberate:\n"
                            "the band is still up afterwards, so 'Re-read timings'\n"
                            "beside this becomes a cheap sanity check instead of\n"
                            "another round trip. Ctrl+H, Release or 'Reset all to\n"
                            "stock' drop the hold.\n\n"
                            "If the hold cannot be taken - controls locked, no\n"
                            "readable V/F curve - it falls back to a CUDA memcpy\n"
                            "load and captures while that runs, which reaches the\n"
                            "same band but only for as long as the load lasts.\n\n"
                            "If the card is ALREADY at P0 it captures directly:\n"
                            "measured, opening a CUDA context on a P0 card pulls\n"
                            "it DOWN to P2.")
                    dpg.add_spacer(width=self.s(10))
                    # Demoted from "Capture" to what it is actually for. On its own
                    # it usually samples idle, which is the error this whole tab
                    # exists to prevent - it earns its place as the cheap re-read
                    # once the blue button has the band held.
                    dpg.add_button(label="Re-read timings", tag="tim_cap",
                                   width=self.s(150), callback=self.timings_capture)
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(
                            "Reads the registers again, right now, in whatever\n"
                            "state the card happens to be in, and files the result\n"
                            "under that memory clock.\n\n"
                            "A SANITY CHECK, not the way to get a reading: on an\n"
                            "idle card this returns idle timings, which say nothing\n"
                            "about performance. Use the blue button for a reading\n"
                            "worth having, then this to confirm it is stable.\n\n"
                            "It is also how you capture a SECOND state to prove the\n"
                            "decode: read held at P0, release, let the card idle,\n"
                            "re-read. The comparison below then shows each field's\n"
                            "cycle ratio beside the clock ratio.\n\n"
                            "Nothing here writes to the GPU.")
                    dpg.add_spacer(width=self.s(10))
                    # Armed by default: the memory p-state cannot be forced on this
                    # card, so catching the top band when it happens is worth
                    # having on. Costs nothing while it waits.
                    dpg.add_checkbox(label="Auto-capture at P0", tag="tim_auto",
                                     default_value=True,
                                     callback=self.timings_auto_toggled)
                    with dpg.tooltip(dpg.last_item()):
                        dpg.add_text(
                            "Captures the first time the card reaches its top\n"
                            "memory state, and again on each re-entry. Start a\n"
                            "game and come back: a valid capture will be waiting.\n\n"
                            "It reads. It never writes.")
                dpg.add_text("", tag="tw_result", color=TEXT, wrap=self.s(1100))
                dpg.add_text("", tag="tw_backup", color=DIM, wrap=self.s(1100))
                dpg.add_spacer(height=self.s(4))
                dpg.add_separator()

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

    # ---- the editable cell ------------------------------------------------ #
    def tw_theme(self, key, colour):
        """Cached text-colour theme. Built lazily because the table is torn down
        and rebuilt on every capture, and rebuilding a theme per row per capture
        leaks DPG items."""
        th = self._tw_themes.get(key)
        if th is None:
            with dpg.theme() as th:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_Text, colour)
            self._tw_themes[key] = th
        return th

    def tw_cell(self, f, cycles):
        """The 'new value' cell for one field.

        Green while it still equals the cycle count actually in the register,
        red the moment it does not - so 'this row will be written' is visible
        without reading the plan text. A field we would refuse to write anyway
        (structural, or in an inferred-offset register) gets no input at all:
        offering an editable box and then rejecting the click is worse than not
        offering it."""
        tag = f"twv_{f.name}"
        if cycles is None or f.structural or f.inferred:
            dpg.add_text("--", color=DIM)
            self.bind(dpg.last_item(), "mono")
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text(
                    "not writable: " + ("this register's offset is INFERRED, "
                                        "not observed" if f.inferred else
                                        "structural - a training/phase value "
                                        "with no safe direction to nudge"),
                    wrap=self.s(420))
            return
        self._tw_base[f.name] = cycles
        staged = self._tw_pending.get(f.name, cycles)
        dpg.add_input_int(tag=tag, default_value=staged, width=self.s(92),
                          min_value=0, max_value=f.max_value,
                          min_clamped=True, max_clamped=True,
                          step=1, callback=self.tw_cell_edit,
                          user_data=f.name)
        dpg.bind_item_theme(tag, self.tw_theme(
            "chg" if staged != cycles else "ok",
            BAD if staged != cycles else GOOD))

    def tw_cell_edit(self, sender, app_data, user_data):
        name, val = user_data, int(app_data)
        base = self._tw_base.get(name)
        if base is not None and val == base:
            self._tw_pending.pop(name, None)
        else:
            self._tw_pending[name] = val
        tag = f"twv_{name}"
        if dpg.does_item_exist(tag):
            changed = name in self._tw_pending
            dpg.bind_item_theme(tag, self.tw_theme("chg" if changed else "ok",
                                                   BAD if changed else GOOD))
        self.tw_plan()

    # ---- the write panel -------------------------------------------------- #
    # These run INLINE rather than on the capture worker's thread. A write is
    # four short subprocess spawns (dry run, commit, two reads) and finishes in
    # well under a second; if nvtune ever hangs long enough for that to matter,
    # the memory controller is in a state where a responsive UI is not the
    # problem worth solving.
    def tw_clear(self, sender=None, app_data=None, user_data=None):
        """Put every cell back to the value in the register. Undoes edits; does
        NOT undo a write that already happened - that is 'Restore stock'."""
        self._tw_pending = {}
        for name, base in self._tw_base.items():
            tag = f"twv_{name}"
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, base)
                dpg.bind_item_theme(tag, self.tw_theme("ok", GOOD))
        dpg.set_value("tw_result", "")
        self.tw_plan()

    def tw_plan(self):
        """Refresh the staged-plan text and the Apply button's colour.

        Shows OUR refusals first - they are the ones the user can act on - then
        nvtune's own dry run."""
        self.tw_button()
        if not self._tw_pending:
            dpg.set_value("tw_plan", "no edits - every cell matches the "
                                     "register")
            dpg.configure_item("tw_plan", color=TEXT)
            return
        ft = getattr(self, "_tim_ft", None)
        snap = self._tim
        problems = timingwrite.check(self._tw_pending, ft, snap)
        try:
            p = timingwrite.plan(self._tw_pending, self.gpu.slot())
            body = p.summary()
            colour = WARN if p.needs_force else GOOD
        except Exception as e:                                  # noqa: BLE001
            body, colour = f"dry run failed: {e}", BAD
        if problems:
            body = ("REFUSED before nvtune is consulted:\n  - "
                    + "\n  - ".join(problems) + "\n\n" + body)
            colour = BAD
        dpg.set_value("tw_plan", body)
        dpg.configure_item("tw_plan", color=colour)

    def tw_button(self):
        """Apply goes red exactly when clicking it would write to the memory
        controller, and sits neutral when it would do nothing. The button and
        the red cells are the same signal said twice, which is the point: the
        control that does the dangerous thing should look like it."""
        band = "armed" if self._tw_pending else "idle"
        if self._tw_btn == band or not dpg.does_item_exist("tw_apply"):
            return
        self._tw_btn = band
        if band == "idle":
            dpg.bind_item_theme("tw_apply", 0)
            dpg.configure_item("tw_apply", label="Apply to memory controller")
            return
        th = self._tw_themes.get("btn")
        if th is None:
            with dpg.theme() as th:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, (122, 30, 34))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                        (160, 40, 45))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                        (190, 50, 55))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 235, 235))
            self._tw_themes["btn"] = th
        dpg.bind_item_theme("tw_apply", th)
        dpg.configure_item(
            "tw_apply",
            label=f"Apply {len(self._tw_pending)} change(s) to the memory "
                  f"controller")

    def tw_apply(self, sender=None, app_data=None, user_data=None):
        if not self._tw_pending:
            return
        ft = getattr(self, "_tim_ft", None)
        problems = timingwrite.check(self._tw_pending, ft, self._tim)
        if problems:
            dpg.set_value("tw_result", "not applied - " + "; ".join(problems))
            dpg.configure_item("tw_result", color=BAD)
            return
        path, made, err = timingwrite.ensure_backup(self.gpu)
        dpg.set_value("tw_backup",
                      (f"stock backup {'taken now' if made else 'already held'}"
                       f": {path}") if not err else f"BACKUP FAILED: {err}")
        if err:
            dpg.set_value("tw_result",
                          "not applied - refusing to write without a stock "
                          "backup for this card")
            dpg.configure_item("tw_result", color=BAD)
            return
        self.autosave_before("timing-write")
        force = bool(dpg.get_value("tw_force"))
        _plan, results = timingwrite.apply(dict(self._tw_pending),
                                           self.gpu.slot(), force=force)
        lines, worst = [], GOOD
        for r in results:
            lines.append(f"{r.name}: {r.before} → asked {r.requested}, "
                         f"reads {r.after}   "
                         f"{timingwrite.OUTCOME_TEXT[r.outcome]}"
                         + (f"  ({r.detail})" if r.detail else ""))
            if r.outcome == timingwrite.LANDED:
                continue
            worst = WARN if r.outcome == timingwrite.TOOL_REFUSED else BAD
        if any(r.outcome == timingwrite.DROPPED for r in results):
            lines.append("A dropped write REACHED the hardware and was "
                         "rejected there - that is a property of this GPU, not "
                         "of the tool. Measured: TU102 rejects all of them.")
        dpg.set_value("tw_result", "\n".join(lines))
        dpg.configure_item("tw_result", color=worst)
        for ln in lines:
            self.log("timing: " + ln, None)
        self.timings_capture()

    def tw_restore(self, sender=None, app_data=None, user_data=None):
        # existing_backup_path, not card_backup_path: a backup taken before the
        # slot joined the filename still restores this card, and offering to
        # restore only the new name would hide it.
        path = (timingwrite.existing_backup_path(self.gpu)
                or timingwrite.card_backup_path(self.gpu))
        code, boot0 = timingwrite.backup_describes(path)
        if code is None:
            dpg.set_value("tw_result", f"no stock backup for this card at {path}")
            dpg.configure_item("tw_result", color=BAD)
            return
        ok, out = timingwrite.restore(path, self.gpu.slot())
        dpg.set_value("tw_result",
                      f"restore from {code} backup ({boot0}): "
                      + ("done" if ok else "FAILED") + f"\n{out}")
        dpg.configure_item("tw_result", color=GOOD if ok else BAD)
        self.log(f"timing restore from {path}: {'ok' if ok else 'failed'}", ok)
        self._tw_pending = {}
        self.tw_plan()
        self.timings_capture()

    def open_testsign(self, sender=None, app_data=None, user_data=None):
        """Open the dialog, and decide THERE whether the red button is live.

        Every gate is re-read on each open rather than cached: Secure Boot can
        be turned off in firmware and the machine rebooted between one look at
        this dialog and the next, and a stale answer would either block a
        legitimate press or offer one that is going to fail."""
        sb = self.secure_boot_state()
        bl = self.bitlocker_on()
        admin = is_admin()
        lines, blocked = [], []
        if sb is True:
            lines.append("Secure Boot is ON. These commands cannot be applied "
                         "while it is - Windows refuses nointegritychecks "
                         "outright. Turn it off in firmware first.")
            blocked.append("secure boot")
        elif sb is False:
            lines.append("Secure Boot: OFF (checked, not assumed).")
        else:
            lines.append("Secure Boot state could not be read on this machine. "
                         "Confirm it is off yourself before running these.")
        if not admin:
            lines.append("Druta is NOT running as administrator - bcdedit "
                         "will refuse. Restart it elevated.")
            blocked.append("not elevated")
        if bl:
            lines.append(f"BITLOCKER IS ON ({bl}). Changing boot "
                         f"configuration can force a recovery-key prompt at "
                         f"the next boot. Suspend BitLocker first, and have "
                         f"your recovery key to hand before you reboot.")
        elif bl is None:
            lines.append("BitLocker status could not be read. If any volume is "
                         "protected, suspend it first - a boot-config change "
                         "can trigger a recovery-key prompt.")
        dpg.set_value("ts_state", "\n".join(lines))
        dpg.configure_item("ts_state",
                           color=BAD if blocked else (WARN if bl else GOOD))
        dpg.configure_item("ts_run", enabled=not blocked)
        dpg.set_value("ts_result", "")
        dpg.configure_item("win_testsign", show=True)
        dpg.focus_item("win_testsign")

    def run_testsign(self, sender=None, app_data=None, user_data=None):
        """Run the three commands, report each one honestly.

        Refuses again here rather than trusting the button's enabled state -
        the dialog can sit open while the machine changes underneath it, and
        this is a boot-configuration write."""
        if self.secure_boot_state() is True or not is_admin():
            dpg.set_value("ts_result",
                          "refused: re-checked at the click and the machine is "
                          "not ready (Secure Boot on, or not elevated).")
            dpg.configure_item("ts_result", color=BAD)
            return
        out, bad = [], 0
        for cmd in self.TESTSIGN_CMDS:
            shown = " ".join(cmd)
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=30,
                                   creationflags=getattr(
                                       subprocess, "CREATE_NO_WINDOW", 0))
                msg = ((r.stdout or "") + (r.stderr or "")).strip() or "ok"
                ok = r.returncode == 0
            except (OSError, subprocess.SubprocessError) as e:
                ok, msg = False, str(e)
            bad += (not ok)
            out.append(f"{'ok  ' if ok else 'FAIL'}  {shown}\n        {msg}")
            self.log(f"testsigning: {shown} -> {msg}", ok)
        dpg.set_value("ts_result", "")
        dpg.configure_item("win_testsign", show=False)

        # Hand the outcome to its own modal. On failure that is the whole
        # point - a bcdedit error is not something to leave as one more line
        # in a dialog. On success it is a QUESTION, because none of this has
        # done anything yet.
        dpg.set_value("tsd_detail", "\n".join(out))
        if bad:
            dpg.set_value("tsd_head",
                          f"{bad} of {len(self.TESTSIGN_CMDS)} commands "
                          f"FAILED. The boot configuration may be partly "
                          f"changed, or not changed at all - read the output "
                          f"below before rebooting, and check "
                          f"'bcdedit /enum {{current}}' yourself.")
            dpg.configure_item("tsd_head", color=BAD)
        else:
            dpg.set_value("tsd_head",
                          "All commands succeeded - and NOTHING HAS CHANGED "
                          "YET. Test signing takes effect at the next boot. "
                          "Reboot now?")
            dpg.configure_item("tsd_head", color=WARN)
        dpg.configure_item("tsd_ok_row", show=not bad)
        dpg.configure_item("tsd_bad_row", show=bool(bad))
        dpg.configure_item("win_ts_done", show=True)
        dpg.focus_item("win_ts_done")

    def reboot_now(self, sender=None, app_data=None, user_data=None):
        """Restart the machine, with a window to change your mind.

        /t 10 rather than /t 0 on purpose: this button sits one click from a
        dialog the operator opened to read, and other applications may have
        unsaved work. Ten seconds and a documented abort ('shutdown /a') is
        the difference between a decision and an accident."""
        try:
            r = subprocess.run(
                ["shutdown", "/r", "/t", "10", "/c",
                 "Druta: applying test signing (shutdown /a aborts)"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            msg = ((r.stdout or "") + (r.stderr or "")).strip()
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            ok, msg = False, str(e)
        self.log(f"reboot requested: {msg or 'in 10s'}", ok)
        dpg.set_value("tsd_head",
                      "Rebooting in 10 seconds. Run 'shutdown /a' in a shell "
                      "to abort." if ok else
                      f"Could not schedule the reboot: {msg}")
        dpg.configure_item("tsd_head", color=WARN if ok else BAD)

    # ---- switching cards --------------------------------------------------- #
    def reset_card_state(self):
        """Drop everything measured on, or staged against, the outgoing card.

        Every field here is per-card, and the ones that are not are left alone
        on purpose: log_lines is the session's receipt and outlives the switch,
        the theme caches are colour definitions rather than measurements, and
        gpu_list describes the MACHINE, which has not changed.

        The V/F working copy is the important one. It is a table of DELTAS
        against a specific card's factory curve, at that card's point indices;
        carried across, "index 74, +90 MHz" would silently mean a different
        voltage on a table of a different length."""
        # editor
        self.vf_points, self.vf_by_idx, self.vf_sel = None, {}, None
        self.vf_work, self.vf_orig = {}, {}
        self._undo, self._redo = [], []
        self._plan_note = None
        self._fitted = False        # the plot must refit: the cap moves 2160->1911
        self._lockable = None       # probed from the driver's own clock table
        self._drag_idx = None
        # arming flags, so a half-pressed confirm cannot carry over
        self._discard_armed = False
        self._reset_armed = False
        self._pending_load = None
        # telemetry
        self._snap, self._snap_err, self._snap_t = None, None, None
        self._stale = False
        self._once = {}
        # drawn-appearance caches: these say "the widget already shows this",
        # which stops being true the moment the widgets are rebuilt
        self._bar_band, self._dom_band = {}, {}
        self._dom_name, self._dom_shown = {}, set()
        self._tw_btn = None
        self._plan_band = None
        # timings tab
        with self._tim_lock:
            self._tim = None
            self._tim_avail = None
            self._tim_caps = {}
            self._tim_note = self._tim_what = ""
            self._tim_p0_in = False
            self._tim_auto_t = 0.0
            self._tim_new = False
            self._tim_ft = None
            self._tw_pending, self._tw_base = {}, {}

    def repaint_log(self):
        """Replay the log into the freshly built widgets.

        log() keeps appending to log_lines through a rebuild but skips drawing,
        so without this the pane comes back empty and the session's receipt
        looks lost."""
        if dpg.does_item_exist("log"):
            dpg.set_value("log", "\n".join(reversed(self.log_lines[-40:])))

    def swap_gpu(self, slot):
        """Re-point this window at another card, in place.

        Deletes the header and the tabs and builds them again against the new
        GPU. That is the whole trick: every per-card figure baked into a widget
        comes back through the same code that derived it correctly at startup,
        so there is no list of widgets to keep in step (see build_body).

        Runs on the UI thread - DPG dispatches callbacks inside
        render_dearpygui_frame - so no frame can be drawn against a half-built
        tree. The background workers are handled by the generation stamp rather
        than by blocking, because an induce can hold the card for 25 s and
        refusing to switch for that long would be worse than dropping its
        result."""
        old = self.gpu.static.get("name", "?")
        self._gpu_gen += 1
        self._rebuilding = True
        try:
            self.reset_card_state()
            # Build the new GPU BEFORE tearing anything down: if the card has
            # gone (unplugged, driver reset, TDR), the old window is still
            # standing and the switch can be refused with everything intact.
            fresh = GPU(slot)
            if not fresh.available():
                self.log(f"cannot switch: {fresh.status_line()}", False)
                return False
            self.gpu = fresh
            self.gpu_list = enumerate_gpus()
            self.build_ui(rebuild=True)
        finally:
            self._rebuilding = False
        st = self.gpu.static
        if len(self.gpu_list) > 1:
            dpg.set_viewport_title(f"Thermetery Druta  -  {st.get('name')}  "
                                   f"{self.gpu.slot()}")
        self.repaint_log()
        self.relayout()
        self.sync_lock_ui()
        self.log(f"switched from {old} to {st.get('name')} at "
                 f"{self.gpu.slot()}  -  {self.gpu.status_line()}", True)
        if self.gpu.pairing_error:
            self.log(self.gpu.pairing_error, False)
        self.vf_read()
        self.timings_capture()
        return True

    # ---- the header card selector ----------------------------------------- #
    def card_labels(self):
        """One entry per GPU, in the same slot order everything else uses."""
        return [self.card_label(g["slot"]) for g in self.gpu_list] or ["no GPU"]

    def card_label(self, slot):
        for g in self.gpu_list:
            if same_slot(g["slot"], slot):
                return f"{g['name']}   {g['slot']}"
        return slot or "no GPU"

    def sync_card_combo(self):
        """Put the combo back on the card actually being driven.

        A dropdown that shows the card you PICKED rather than the card you GOT
        is a lie, and this one can differ: switch_gpu refuses outright while a
        lock is held, and asks twice when there are staged edits. Both leave
        the selection where it was, so the widget has to be walked back."""
        if dpg.does_item_exist("hdr_card"):
            dpg.set_value("hdr_card", self.card_label(self.gpu.slot()))

    def on_pick_card(self, sender=None, app_data=None, user_data=None):
        want = next((g["slot"] for g in self.gpu_list
                     if self.card_label(g["slot"]) == app_data), "")
        if want:
            self.switch_gpu(user_data=want)
        # unconditional: a successful switch rebuilt the header (and this
        # combo) against the new card, and a refused one has to be undone
        self.sync_card_combo()

    def switch_gpu(self, sender=None, app_data=None, user_data=None):
        """Point this window at another card, after checking it is safe to.

        Refuses outright while this window is HOLDING the current card. A clock
        lock or a V/F point lock is state in the driver, not in this window: a
        hold left behind stays on the card it was placed on, while the Release
        button in front of the user would now act on a different GPU. Two clicks
        - Release, then switch - beats one click with an ambiguous target.

        Staged-but-unwritten work is different: losing it costs nothing but the
        typing, so that only arms rather than refuses."""
        slot = user_data
        if not slot:
            return
        if same_slot(slot, self.gpu.slot()):
            return
        target = next((g for g in self.gpu_list
                       if same_slot(g["slot"], slot)), None)
        label = f"{target['name']} at {slot}" if target else slot

        if self._clk_lock:
            self._switch_armed = None
            self.log(f"not switching to {label}: this window is holding the "
                     f"current card with the "
                     f"{self.LOCK_NAME[self._clk_lock['kind']]}"
                     f". Release it first - the hold stays on the card it was "
                     f"placed on, and after a switch the Release button would "
                     f"be aimed at the other GPU.", False)
            return

        staged = []
        if self.vf_work:
            staged.append(f"{len(self.vf_work)} staged V/F edit(s)")
        if self._tw_pending:
            staged.append(f"{len(self._tw_pending)} staged timing write(s)")
        if staged and self._switch_armed != slot:
            self._switch_armed = slot
            self.log(f"switching to {label} discards " + " and ".join(staged)
                     + " - they are measured against THIS card and mean "
                       "nothing on another. Choose it again to confirm.", False)
            return
        self._switch_armed = None
        self.swap_gpu(slot)

    def open_gpu_window(self, sender=None, app_data=None, user_data=None):
        """Start a SECOND window on another card, leaving this one alone.

        Kept alongside the in-place switch because they answer different
        questions: switching is for moving attention, a second window is for
        watching both cards at once. It is also the only way to hold a lock on
        one card while working on the other, which the in-place switch refuses
        by design."""
        slot = user_data
        if not slot:
            return
        try:
            argv = self.relaunch_argv(slot)
        except RuntimeError as e:
            self.log(f"cannot open a second window: {e}", False)
            return
        try:
            subprocess.Popen(argv, close_fds=True)
        except OSError as e:
            self.log(f"could not start a second window: {e}", False)
            return
        self.log(f"opened a second window on {slot}", True)

    @staticmethod
    def relaunch_argv(slot):
        """This same program, told to open on `slot`.

        A frozen build IS the executable; a source run needs the interpreter and
        the script, and sys.argv[0] is resolved to an absolute path because the
        new process does not inherit this one's working directory reliably."""
        if getattr(sys, "frozen", False):
            return [sys.executable, "--gpu", slot]
        script = os.path.abspath(sys.argv[0] or __file__)
        if not os.path.isfile(script):
            raise RuntimeError(f"cannot find this script to relaunch ({script})")
        return [sys.executable, script, "--gpu", slot]

    # ---- locating nvtune, which this build does not ship ------------------ #
    @staticmethod
    def pick_file_native(title, initial_dir="", filename="", spec=()):
        """The WINDOWS file picker, through comdlg32.GetOpenFileNameW.

        Dear PyGui's own file dialog is not usable for this. It renders drives
        as a row of unlabelled buttons ("+ R Drives E C: Users"), has no
        breadcrumb, no places bar, no typing a path, and no shell integration -
        so finding a file outside the default directory is a guessing game. The
        native dialog is the one people already know how to drive.

        ctypes rather than tkinter: tkinter is not in the frozen build (see
        Druta.spec - no _tkinter.pyd in the bundle) and pulling it in to draw
        one dialog would be a strange dependency. comdlg32 is already loaded in
        every Windows process.

        Returns (path, error). Cancel is ("", ""); a real failure carries its
        reason, because a picker that silently returns nothing is
        indistinguishable from a user pressing Cancel - and that is exactly how
        a NameError in this function hid behind a bare except during
        development."""
        try:
            class OFN(ctypes.Structure):
                _fields_ = [
                    ("lStructSize", ctypes.c_uint32),
                    ("hwndOwner", ctypes.c_void_p),
                    ("hInstance", ctypes.c_void_p),
                    ("lpstrFilter", ctypes.c_wchar_p),
                    ("lpstrCustomFilter", ctypes.c_wchar_p),
                    ("nMaxCustFilter", ctypes.c_uint32),
                    ("nFilterIndex", ctypes.c_uint32),
                    ("lpstrFile", ctypes.c_wchar_p),
                    ("nMaxFile", ctypes.c_uint32),
                    ("lpstrFileTitle", ctypes.c_wchar_p),
                    ("nMaxFileTitle", ctypes.c_uint32),
                    ("lpstrInitialDir", ctypes.c_wchar_p),
                    ("lpstrTitle", ctypes.c_wchar_p),
                    ("Flags", ctypes.c_uint32),
                    ("nFileOffset", ctypes.c_uint16),
                    ("nFileExtension", ctypes.c_uint16),
                    ("lpstrDefExt", ctypes.c_wchar_p),
                    ("lCustData", ctypes.c_void_p),
                    ("lpfnHook", ctypes.c_void_p),
                    ("lpTemplateName", ctypes.c_wchar_p),
                    ("pvReserved", ctypes.c_void_p),
                    ("dwReserved", ctypes.c_uint32),
                    ("FlagsEx", ctypes.c_uint32)]

            # the filter is a run of NUL-separated pairs ending in a double NUL
            flt = "".join(f"{label}\0{pattern}\0" for label, pattern in
                          (spec or (("All files", "*.*"),))) + "\0"
            buf = ctypes.create_unicode_buffer(filename or "", 4096)
            ofn = OFN()
            ofn.lStructSize = ctypes.sizeof(OFN)
            ofn.lpstrFilter = flt
            ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
            ofn.nMaxFile = 4096
            ofn.lpstrInitialDir = initial_dir or None
            ofn.lpstrTitle = title
            # EXPLORER gives the modern shell dialog; FILEMUSTEXIST and
            # PATHMUSTEXIST make it reject a typo rather than hand back a path
            # to nothing; NOCHANGEDIR stops it moving OUR working directory,
            # which would quietly break every relative path in the process.
            ofn.Flags = 0x00080000 | 0x00001000 | 0x00000800 | 0x00000008
            if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
                return buf.value, ""
        except Exception as e:                                  # noqa: BLE001
            return "", f"{type(e).__name__}: {e}"
        return "", ""

    def open_locate_nvtune(self, sender=None, app_data=None, user_data=None):
        """Browse to an nvtune.exe and remember where it is.

        Runs the picker on a worker thread. GetOpenFileNameW is modal and
        blocks until it closes; on the UI thread that would stop
        render_dearpygui_frame for as long as the dialog is open, leaving a
        frozen, non-repainting window behind it - which reads as a hang."""
        cur = timings.find_exe()
        start = os.path.dirname(cur) if cur else os.path.expanduser("~")

        def worker():
            path, err = self.pick_file_native(
                "Locate nvtune.exe", start, timings.NVTUNE_EXE,
                ((f"nvtune ({timings.NVTUNE_EXE})", timings.NVTUNE_EXE),
                 ("Executables (*.exe)", "*.exe"),
                 ("All files (*.*)", "*.*")))
            if err:
                self.log(f"file picker failed: {err}", False)
            elif path:
                self.locate_nvtune_done(app_data={"file_path_name": path})

        threading.Thread(target=worker, daemon=True,
                         name="Druta-filedlg").start()

    def locate_nvtune_done(self, sender=None, app_data=None, user_data=None):
        path = (app_data or {}).get("file_path_name") or ""
        ok, msg = timings.set_configured_exe(path)
        self.log(msg, ok)
        if ok:
            # re-check availability immediately: registering a path and then
            # still seeing "not found" until the next capture would read as the
            # registration having failed
            self.timings_capture()

    def forget_nvtune(self, sender=None, app_data=None, user_data=None):
        ok, msg = timings.set_configured_exe(None)
        self.log(msg + " - falling back to the derived locations", ok)
        if ok:
            self.timings_capture()

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
                         name="Druta-timings").start()

    def _timings_worker(self):
        # try/finally around the WHOLE body, matching _induce_worker. Without
        # it, anything raising out of available() or snapshot() kills the thread
        # before _tim_busy is cleared - and refresh_timings() then leaves
        # Capture, Induce and Clear disabled for the rest of the session, with
        # nothing on screen saying why. A dead worker should cost one capture,
        # not the tab.
        av, snap, err = None, None, ""
        gen, gpu = self._gpu_gen, self.gpu
        try:
            av = timings.available()
            snap = timings.snapshot(gpu) if av.ok else None
        except Exception as e:                                  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        finally:
            with self._tim_lock:
                # `gpu`, not self.gpu: a switch during the capture leaves this
                # snapshot describing the card it started on, and filing it
                # would put one card's registers under another card's tab with
                # that card's memory divisor applied to the ns column. Written
                # as an if/else rather than an early return because a `return`
                # inside `finally` discards whatever exception was in flight.
                stale = gen != self._gpu_gen
                if not stale:
                    if av is not None:
                        self._tim_avail = av
                    # a plain capture is not the induced one: its landing note
                    # would describe a load that is no longer running
                    self._tim_note = ""
                    self._tim_what = ""
                    if snap is not None:
                        self._tim = snap
                        # Only a snapshot whose clock HELD STILL is filed as a
                        # capture: the captures are keyed by memory clock, and
                        # one taken across a reclock has no single clock to key
                        # it by.
                        if snap.ok and snap.mem_stable and snap.key:
                            self._tim_caps[snap.key] = snap
                    self._tim_new = True
                self._tim_busy = False
        if err and not stale:
            self.log(f"timings capture failed: {err}", False)

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
        """Every memory clock the driver enumerates, ascending. Card-specific:
        TU102 gives [405, 810, 5001, 6801, 7001] and GP102 [405, 810, 5505,
        5705]. The card can exceed the top entry under load, because a memory
        offset rides on top of the top state."""
        return sorted(self.gpu.static.get("mem_clocks") or [])

    def mem_hint(self, i):
        """'~810 reported / ~203 MHz true' for enumerated state `i`, built from
        THIS card's table. It used to be a hardcoded pair of Turing numbers,
        which on a Pascal card told the user to look for a state it does not
        have."""
        st = self.mem_states()
        if not st:
            return "the top memory state"
        try:
            nvml = st[i]
        except IndexError:
            nvml = st[-1]
        div = self.gpu.static.get("mem_div") or 0
        if not div:
            return f"~{nvml} reported"
        return f"~{nvml} reported / ~{nvml / div:.0f} MHz true"

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
    def timings_read_p0(self, sender=None, app_data=None, user_data=None):
        """THE read button: put the card in its top memory band and capture.

        Two ways to get there, and the order matters. The V/F point lock is
        tried FIRST because it actually holds - measured, it keeps true P0 on
        an idle card, where a CUDA load only visits the top band for as long
        as it runs. Holding is better for reading timings in every way that
        counts: the capture is not racing a load, and the band is still there
        afterwards, so 'Re-read timings' beside this button becomes a cheap
        sanity check rather than another 25-second round trip.

        The load is the FALLBACK, for when the hold cannot be taken - controls
        locked, no NVAPI, no readable curve. It is what this button used to do
        unconditionally.

        The hold is left in force. That is the difference the label states:
        this button changes the card's state and says so. Ctrl+H, the Release
        button, or 'Reset all to stock' drop it."""
        with self._tim_lock:
            if self._tim_busy:
                return
            self._tim_busy = True
            self._tim_what = "holding P0…"
        # On the UI thread, before the worker starts: hold_point selects a
        # point and writes widget values, and a background thread has no
        # business doing either.
        held = self.hold_for_read()
        with self._tim_lock:
            self._tim_what = "reading at P0…" if held else "inducing GPU load…"
        threading.Thread(target=self._induce_worker, args=(held,), daemon=True,
                         name="Druta-read-p0").start()

    def hold_for_read(self):
        """Pin the card on the cap point so a capture lands in the top band.

        Returns True only if the lock is really in force. Every failure is a
        reason to fall back to the load, not to give up: a locked gate, a card
        with no readable V/F table, or an NVAPI that will not take the lock all
        leave the CUDA path perfectly able to reach the band."""
        if not self.unlocked():
            self.log("read: controls are locked, so the P0 hold was skipped - "
                     "falling back to a GPU load", None)
            return False
        if not self.vf_points:
            self.vf_read()
        if not self.vf_points:
            self.log("read: no readable V/F curve to hold - falling back to a "
                     "GPU load", None)
            return False
        if self._clk_lock and self._clk_lock.get("kind") == self.LOCK_VF:
            return True                      # already holding; nothing to do
        try:
            self.hold_cap_point(dpg.get_value("vcap"))
        except Exception as e:                                  # noqa: BLE001
            self.log(f"read: could not take the P0 hold ({e}) - falling back "
                     f"to a GPU load", None)
            return False
        return bool(self._clk_lock
                    and self._clk_lock.get("kind") == self.LOCK_VF)

    # How long to give the card to climb into its top memory band after the
    # V/F point lock goes on. MEASURED: it arrives in well under a second on
    # this card; the margin is for a card busy with something else.
    HOLD_SETTLE_S = 6.0

    def wait_for_band(self, gpu, seconds=None):
        """Poll until the card is in its top memory band, or give up.

        The lock is a VOLTAGE request and the clocks follow it, so there is a
        gap between 'the lock took' and 'the memory clock is up'. Capturing
        inside that gap would file idle timings under a P0 heading, which is
        the one mistake this tab exists to prevent."""
        deadline = time.monotonic() + (seconds or self.HOLD_SETTLE_S)
        last = (None, None)
        while time.monotonic() < deadline:
            try:
                d = gpu.read()
                last = (d.get("mem"), d.get("pstate"))
                if self.is_p0(*last):
                    return True, last
            except Exception:                                   # noqa: BLE001
                pass
            time.sleep(0.25)
        return False, last

    def _induce_worker(self, held=False):
        note, snap = "", None
        # Same stamp-and-drop as _timings_worker, and it matters more here:
        # this one can spend 25 s running a CUDA load, which is the widest
        # window in the app for the card to change underneath it.
        gen, gpu = self._gpu_gen, self.gpu
        try:
            av = timings.available()
            if not av.ok:
                note = av.reason
            else:
                mem, ps = None, None
                try:
                    d = gpu.read()
                    mem, ps = d.get("mem"), d.get("pstate")
                except Exception:
                    pass
                if self.is_p0(mem, ps):
                    # MEASURED: creating a CUDA context on a card already at P0
                    # drops it to P2 (7428/P0 -> 7228/P2). Running the load
                    # here would destroy the very state we came for.
                    snap = timings.snapshot(gpu)
                    note = (f"Already at P0 (memory {mem}, p-state {ps}) - "
                            f"captured directly, no load started. Opening a "
                            f"CUDA context on a P0 card pulls it DOWN to P2.")
                elif held:
                    # The lock is on but the clocks follow it with a lag, so
                    # wait for the band rather than capture into the gap.
                    up, (mem, ps) = self.wait_for_band(gpu)
                    snap = timings.snapshot(gpu)
                    if up:
                        note = (f"Holding P0 (memory {mem}) with the V/F point "
                                f"lock - no load needed, and the band stays up "
                                f"after this capture. 'Re-read timings' is now "
                                f"a cheap sanity check. Ctrl+H releases.")
                    else:
                        note = (f"The V/F point lock is on, but the card was "
                                f"still at memory {mem}, p-state {ps} after "
                                f"{self.HOLD_SETTLE_S:.0f}s. Captured anyway - "
                                f"read the state line above before trusting "
                                f"the table.")
                else:
                    ok, why = gpuload.available()
                    if not ok:
                        note = why
                    else:
                        res = gpuload.induce(
                            gpu,
                            on_settled=lambda: timings.snapshot(gpu))
                        snap = res.get("result")
                        note = self.induce_note(res, snap)
        except Exception as e:
            note = f"induce failed: {type(e).__name__}: {e}"
        with self._tim_lock:
            if gen != self._gpu_gen:
                self._tim_busy = False
                return
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
            return f"Induced P0 (memory {mem}). {how}."
        if snap is not None and snap.perf_band:
            # This used to be three paragraphs proving a P2 capture is as good
            # as a P0 one. It was written when P0 was hard to reach; Ctrl+H
            # holds it directly now, so the argument is no longer load-bearing
            # and the claim can just be stated.
            return (f"Induced p-state {ps} (memory {mem}) — top clock band, a "
                    f"valid reading. {how}.")
        return (f"Reached memory {mem}, p-state {ps} — below the top clock "
                f"band, so not worth reading. {how}. Hold the card with "
                f"Ctrl+H on the V/F curve, or arm 'Auto-capture'.")

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
        for tag in ("tim_cap", "tim_read"):
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
        # THE WHOLE TAB SWAPS. Setup screen or working tab, never both, and
        # never a working tab that cannot work.
        dpg.configure_item("tim_needs", show=bad)
        dpg.configure_item("tim_work", show=not bad)
        if bad:
            # Which half is missing decides what the screen leads with. A
            # missing exe is a deployment problem and Locate is the answer; a
            # stopped driver is a code-signing problem and test signing is.
            no_exe = not (av and av.exe)
            dpg.set_value("tim_setup_head",
                          "NVTUNE NOT FOUND" if no_exe
                          else "NVTUNE'S DRIVER IS NOT RUNNING")
            dpg.set_value(
                "tim_setup_what",
                "This tab reads and writes the framebuffer-partition memory "
                "timing registers, and it does that through nvtune - a "
                "separate program with its own kernel driver. Druta cannot "
                "reach those registers on its own."
                if no_exe else
                "nvtune.exe was found, but its kernel driver is not running, "
                "so nothing can reach BAR0 yet. The driver is signed with a "
                "self-signed test certificate, which Windows will not load "
                "unless the machine is in test signing mode.")
            dpg.set_value("tim_step1",
                          "Point Druta at it:" if no_exe
                          else "Let Windows load its driver:")
            # the action that is not the answer stays available but stops
            # looking like the thing to press
            dpg.configure_item("tim_locate", enabled=True)
            dpg.configure_item("tim_testsign", enabled=True)
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
            # the captures still have to be redrawn: this path is reached
            # whenever the most recent snapshot failed, and a comparison left
            # standing over captures that no longer exist is a lie
            self.draw_comparison(caps)
            return

        # ---- WHICH MEMORY STATE, before anything else --------------------- #
        # Red, first, and in full. The startup capture almost always lands at
        # idle, and an idle capture presented calmly under a confident ns table
        # is exactly the mistake this tab exists to stop people making.
        # The loud case is an IDLE capture. A P2 capture is bit-identical to a
        # P0 one on this card, so calling it second-rate would be its own
        # false-authority error - the exact thing this banner exists to stop.
        # ONLY WHEN IT IS WRONG. A capture in the top band needs no sentence -
        # the title line below already says which band it is in, and a green
        # paragraph confirming success on every read is the noise that made
        # this banner worth trimming in the first place. What must never be
        # quiet is a capture taken OUTSIDE the band, because that is an
        # authoritative-looking table measured in a state nobody runs work in.
        band = snap.perf_band
        dpg.configure_item("tim_state", show=not band)
        if not band:
            dpg.set_value("tim_state", "⚠ " + snap.state_headline)
            dpg.configure_item("tim_state",
                               color=WARN if band is None else BAD)
        dpg.set_value("tim_title", (
            f"DECODED TIMINGS  ·  broadcast aperture  ·  "
            f"{snap.state_tag}, top clock band" if band else
            "DECODED TIMINGS  ·  IDLE STATE — NOT PERFORMANCE-RELEVANT"))
        dpg.configure_item("tim_title", color=ACCENT if band else BAD)

        # The write panel's field list comes from the same nvtune build that
        # just answered, so it can only offer fields this binary actually knows.
        try:
            self._tim_ft = timings.field_table()
        except Exception:                                       # noqa: BLE001
            self._tim_ft = None

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
                self.tw_cell(f, r.cycles)
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
                "needed, at DIFFERENT memory clocks. Let the card idle until "
                f"the memory clock drops ({self.mem_hint(-2)}) and press "
                f"Capture, then load it to the top state ({self.mem_hint(-1)}) "
                "and press Capture again. The same registers decode to "
                "different cycle counts at the two states, and this table is "
                "where that stops looking like noise and starts being the "
                "proof."))
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
    @staticmethod
    def resource_path(name):
        """A file shipped ALONGSIDE the code, found in both build shapes.

        PyInstaller's onefile bootloader unpacks bundled data to a temp
        directory and points sys._MEIPASS at it; a source run just looks beside
        this file. Used for COPYING and THIRD-PARTY-NOTICES.md, which are not
        decoration: GPL-3.0 section 4 requires the licence to be conveyed with
        the Program, and a onefile exe has nowhere else to carry it."""
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, name)

    @classmethod
    def read_licence(cls, name):
        try:
            with open(cls.resource_path(name), encoding="utf-8",
                      errors="replace") as f:
                return f.read()
        except OSError as e:
            # Never fatal, and never silent: if the licence did not make it
            # into the build, the window says so rather than showing nothing,
            # because an empty Licences window looks like a UI bug rather than
            # the compliance problem it actually is.
            return (f"{name} was not found in this build ({e}).\n\n"
                    f"This is a packaging fault - it should be bundled. See\n"
                    f"https://www.gnu.org/licenses/gpl-3.0.html for the "
                    f"licence text, and the project repository for the "
                    f"third-party notices.")

    # ---- shunt-mod corrected power ------------------------------------------ #
    def open_shunt(self, sender=None, app_data=None, user_data=None):
        self.draw_shunt_rows()
        dpg.configure_item("win_shunt", show=True)
        dpg.focus_item("win_shunt")

    def draw_shunt_rows(self):
        """Rebuild the rail table from self.shunt_rails, and re-fold.

        Rebuilt wholesale rather than patched: rows come and go, and a table
        that is edited in place has to keep row tags and list indices in step -
        which is the bug this avoids rather than solves."""
        dpg.delete_item("shunt_table", children_only=True, slot=1)
        for i, rail in enumerate(self.shunt_rails):
            with dpg.table_row(parent="shunt_table"):
                dpg.add_text(shuntmod.RAIL_KINDS[rail["kind"]][0])
                dpg.add_input_float(default_value=float(rail["orig"]),
                                    tag=f"sh_o{i}", width=-1, step=0.5,
                                    format="%.3f", min_value=0.0,
                                    min_clamped=True, user_data=("orig", i),
                                    callback=self.shunt_edit)
                dpg.add_input_float(default_value=float(rail["mod"]),
                                    tag=f"sh_m{i}", width=-1, step=0.5,
                                    format="%.3f", min_value=0.0,
                                    min_clamped=True, user_data=("mod", i),
                                    callback=self.shunt_edit)
                m = shuntmod.rail_multiplier(rail)
                dpg.add_text(f"x{m:.4g}", tag=f"sh_x{i}",
                             color=WARN if abs(m - 1.0) > 1e-9 else DIM)
                dpg.add_button(label="remove", width=-1, user_data=i,
                               callback=self.shunt_remove)
        self.shunt_refold()

    def shunt_refold(self):
        """Recompute the correction and say, in the dialog, what it is."""
        self.shunt = shuntmod.correction(self.shunt_rails)
        sh = self.shunt
        if not sh.active:
            dpg.set_value("shunt_result",
                          "No rail is modified - power is reported as the "
                          "driver gives it.")
            dpg.configure_item("shunt_result", color=DIM)
        elif sh.exact:
            dpg.set_value("shunt_result",
                          f"x{sh.factor:.4g} on the board total. EXACT: every "
                          f"rail carries the same multiplier, so how the load "
                          f"divides between them does not matter.")
            dpg.configure_item("shunt_result", color=GOOD)
        else:
            dpg.set_value("shunt_result",
                          f"x{sh.factor:.4g} on the board total - ESTIMATE, "
                          f"not a measurement. {sh.why}.")
            dpg.configure_item("shunt_result", color=BAD)
        dpg.set_value("shunt_note", "\n".join(shuntmod.describe(
            self.shunt_rails)))

    def shunt_edit(self, sender=None, app_data=None, user_data=None):
        which, i = user_data
        if 0 <= i < len(self.shunt_rails):
            self.shunt_rails[i][which] = float(app_data or 0.0)
            m = shuntmod.rail_multiplier(self.shunt_rails[i])
            if dpg.does_item_exist(f"sh_x{i}"):
                dpg.set_value(f"sh_x{i}", f"x{m:.4g}")
                dpg.configure_item(f"sh_x{i}",
                                   color=WARN if abs(m - 1.0) > 1e-9 else DIM)
        self.shunt_refold()

    def shunt_add(self, sender=None, app_data=None, user_data=None):
        self.shunt_rails.append({"kind": user_data,
                                 "orig": shuntmod.DEFAULT_MOHM,
                                 "mod": shuntmod.DEFAULT_MOHM})
        self.draw_shunt_rows()

    def shunt_remove(self, sender=None, app_data=None, user_data=None):
        if 0 <= user_data < len(self.shunt_rails):
            del self.shunt_rails[user_data]
        self.draw_shunt_rows()

    def shunt_reset(self, sender=None, app_data=None, user_data=None):
        self.shunt_rails = [dict(r) for r in shuntmod.DEFAULT_RAILS]
        self.draw_shunt_rows()

    def shunt_save(self, sender=None, app_data=None, user_data=None):
        ok, msg = shuntmod.save(self.shunt_rails)
        self.log(msg, ok)
        self.shunt_refold()

    # ---- test signing, which nvtune's driver needs -------------------------- #
    # Exactly what gets run, in order, as one copiable block. THE THIRD ONE IS
    # THE LOAD-BEARING COMMAND: Microsoft documents `testsigning` as what makes
    # Windows "load any type of test-signed kernel-mode code", and documents
    # `nointegritychecks` as "ignored by Windows 7 and Windows 8" and as
    # something that "cannot be set when secure boot is enabled".
    # DISABLE_INTEGRITY_CHECKS is not a documented datatype at all. The first
    # two are kept because they are the recipe that is known to work on the rig
    # this was written for - but the dialog says which one does the work rather
    # than teaching all three as equals.
    TESTSIGN_CMDS = [
        ["bcdedit", "/set", "loadoptions", "DISABLE_INTEGRITY_CHECKS"],
        ["bcdedit", "/set", "nointegritychecks", "on"],
        ["bcdedit", "/set", "TESTSIGNING", "ON"],
    ]
    TESTSIGN_UNDO = [
        ["bcdedit", "/deletevalue", "loadoptions"],
        ["bcdedit", "/set", "nointegritychecks", "off"],
        ["bcdedit", "/set", "TESTSIGNING", "OFF"],
    ]

    @staticmethod
    def _ps(script):
        """One short PowerShell probe. Returns stdout, or "" on any failure."""
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 script], capture_output=True, text=True, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return (r.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def secure_boot_state(self):
        """True / False / None(unknown). CHECKED, not asserted.

        The red button used to ask the operator to promise Secure Boot was
        off. It can be read instead, and it must be: Microsoft documents that
        nointegritychecks "cannot be set when secure boot is enabled", so a
        wrong promise buys a half-applied boot configuration and a confusing
        error rather than an honest refusal."""
        out = self._ps("try { [string](Confirm-SecureBootUEFI) } "
                       "catch { 'UNKNOWN' }").lower()
        return True if out == "true" else (False if out == "false" else None)

    def bitlocker_on(self):
        """Any volume with protection ON, or None if it cannot be determined.

        This is the failure here that costs DATA rather than security posture:
        Microsoft's own bcdedit page says to suspend BitLocker before changing
        boot options, because the change can force a recovery-key prompt at
        next boot. Someone without their key is locked out of the machine."""
        out = self._ps(
            "try { $v = Get-BitLockerVolume -ErrorAction Stop | "
            "Where-Object { $_.ProtectionStatus -ne 'Off' }; "
            "if ($v) { ($v.MountPoint) -join ',' } else { 'NONE' } } "
            "catch { 'UNKNOWN' }")
        if not out or out == "UNKNOWN":
            return None
        return "" if out == "NONE" else out

    def build_ui(self, rebuild=False):
        """Build (or rebuild) everything that depends on which card this is.

        Themes and handler registries are created UNPARENTED, so deleting the
        header, the tabs and the tool windows does not touch them - they
        accumulate. Measured before this existed: +129 items per switch, of
        which 32 themes were merely wasteful and the handler registry was a
        real defect. Every rebuild added a second live registry with its own
        copy of the seven key handlers, so after N switches one press of W
        nudged the selected point N+1 times and one Ctrl+Z walked back N+1
        edits.

        Rather than keep a list of what to clean up - the same list-maintenance
        problem that argued for rebuilding in the first place - each build
        records the ids it created outside the tree, and the next one deletes
        exactly those. Anything a future build method creates unparented is
        covered without being enumerated here."""
        for i in self._orphans:
            if dpg.does_item_exist(i):
                dpg.delete_item(i)
        self._orphans = []
        if rebuild:
            # _ctl_widgets is repopulated by the builders; not clearing it
            # would leave the unlock gate holding dead tags from the old tree
            # and every guard() call walking them.
            self._ctl_widgets = []
            for tag in ("hdr_row", "tabs", "menubar", "win_device", "win_save",
                        "win_profiles", "win_keys", "win_about",
                        "win_licence", "win_testsign", "win_ts_done",
                        "win_shunt"):
                if dpg.does_item_exist(tag):
                    dpg.delete_item(tag)
        before = set(dpg.get_all_items())
        self.build_menu_bar()             # viewport-owned, so NOT inside 'root'
        self.build_body()
        self.build_tool_windows()         # hidden until the menu bar asks
        self._orphans = [i for i in set(dpg.get_all_items()) - before
                         if not self._has_parent(i)]

    @staticmethod
    def _has_parent(item):
        try:
            return bool(dpg.get_item_info(item).get("parent"))
        except Exception:                                       # noqa: BLE001
            return True     # unreadable: leave it alone rather than delete it

    def build_body(self):
        """The header row and the three tabs, as children of `root`.

        Split out of run() so a card switch can DELETE and re-run it rather
        than hunting down the per-card values baked into individual widgets.
        An audit of what a switch would otherwise have to patch by hand found
        them in five build methods: the two clock sliders' min/max from
        gfx_min/gfx_max, their step and the nudge buttons' labels from the
        card's clock bin, the core-offset slider's range, the keyboard hint,
        the memory divisor and type on the domains panel, the row counts quoted
        in the Profiles and Device windows, and the About box's grid figure.
        Rebuilding re-derives all of them through the same code that got them
        right at startup - and, unlike a patch list, cannot fall out of date
        when a widget is added.

        `menu_pad` is deliberately NOT in here. It is the spacer relayout()
        keeps in step with the menu bar, has nothing per-card about it, and
        leaving it in place keeps these two rebuilt in the right order after
        root's other children."""
        st = self.gpu.static
        with dpg.group(horizontal=True, tag="hdr_row", parent="root"):
            dpg.add_text("Thermetery Druta", tag="hdr", color=ACCENT)
            self.bind("hdr", "big")
            dpg.add_text(f"   {st.get('name')}  •  driver "
                         f"{st.get('driver')}  •  vbios "
                         f"{st.get('vbios')}"
                         + (f"  •  {st.get('slot')}"
                            if len(self.gpu_list) > 1 else ""), color=DIM)
            dpg.add_text("   admin" if st.get("admin")
                         else "   NOT admin (lock/fan/PL need admin)",
                         color=GOOD if st.get("admin") else WARN)
            dpg.add_text("", tag="stale", color=BAD)
            # THE CARD SELECTOR, in the header rather than three levels into a
            # menu. Every control in this window is pointed at exactly one GPU
            # and means different numbers on a different one, so which card is
            # selected is a permanent question, not an occasional one - and a
            # menu is where you put things people look for, not things they
            # need to see. Large, because it is the label for the whole window.
            dpg.add_spacer(width=self.s(24))
            # Same font as the combo it labels. At the body size it read as a
            # caption on a control three times its height, which made the pair
            # look like an afterthought rather than the header's main control.
            dpg.add_text("card", tag="hdr_card_lbl", color=DIM)
            self.bind("hdr_card_lbl", "sel")
            dpg.add_combo(self.card_labels(), tag="hdr_card",
                          default_value=self.card_label(self.gpu.slot()),
                          width=self.s(420), callback=self.on_pick_card)
            self.bind("hdr_card", "sel")
            with dpg.tooltip("hdr_card"):
                dpg.add_text(
                    "Which GPU this window drives. Switching rebuilds every\n"
                    "control from the new card's own measurements.\n\n"
                    "Refused while this window is holding the card with a\n"
                    "clock or V/F point lock. Staged edits ask once, then\n"
                    "go through on a second pick.\n\n"
                    "Device > Open a second window on... watches both at once.")
        with dpg.tab_bar(tag="tabs", parent="root"):
            # Control first: it is what the app is opened to do. Monitor
            # second. Timings last and labelled, because it is the only tab
            # that needs a separate tool installed to do anything at all.
            self.build_control()          # the V/F editor lives inside this tab
            self.build_monitor()
            self.build_timings()

    def run(self):
        # main() reports this properly (and visibly, on a console-less build)
        # before calling run(); this is the guard for any other caller.
        if not self.gpu.available():
            _tell("No GPU backend: " + self.gpu.status_line())
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
        # The card is in the TITLE, not only inside the window: two Drutas open
        # on two cards are otherwise identical in the taskbar, and picking the
        # wrong one is picking the wrong GPU to write to.
        title = "Thermetery Druta"
        if len(self.gpu_list) > 1:
            title += f"  -  {self.gpu.static.get('name')}  {self.gpu.slot()}"
        dpg.create_viewport(title=title, width=self.s(1180), height=vh)
        self.load_fonts()

        with dpg.window(tag="root"):
            # DPG draws the viewport menu bar OVER the primary window instead of
            # insetting it, so without this pad the title row is half-hidden
            # behind File/Device/Profiles/Clocks/Help. relayout() keeps it in
            # step with the bar's measured height. Outside build_ui because it
            # is the one child of root with nothing per-card about it, and
            # leaving it in place keeps the rebuilt rows in the right order.
            dpg.add_spacer(tag="menu_pad", height=self.menu_h())
        self.build_ui()

        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("root", True)
        dpg.set_viewport_resize_callback(self.relayout)
        self.relayout()

        threading.Thread(target=self.poll_loop, daemon=True,
                         name="Druta-poll").start()
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


def _tell(text):
    """Say something from the command line, from a build that has no console.

    The shipped EXE is built console=False, which leaves sys.stdout as None -
    so `Druta.exe --list-gpus` would print into nothing and look like it did
    nothing at all. Three ways out, in order of how much they respect where the
    user is looking:

      1. a real stdout (source run, or a console build)    - just print
      2. the console of whatever launched us               - AttachConsole,
         which is what makes this readable from PowerShell or cmd
      3. no console anywhere (Explorer, a shortcut)        - a message box

    The message box is LAST because it is modal: reached when a console was
    available, it would hang a piped invocation waiting for a click."""
    text = str(text)
    out = getattr(sys, "stdout", None)
    if out is not None:
        try:
            print(text)
            return
        except (OSError, ValueError, AttributeError):
            pass
    try:
        k32 = ctypes.windll.kernel32
        if k32.GetConsoleWindow() or k32.AttachConsole(-1):  # PARENT_PROCESS
            with open("CONOUT$", "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text + "\n")
            return
    except (OSError, AttributeError):
        pass
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Druta", 0x40)
    except Exception:                                       # noqa: BLE001
        pass


def main(argv=None):
    """--gpu SLOT picks the card; --list-gpus reports what is available.

    The slot spelling is nvtune's and NVML's, so the three tools can be pointed
    at one card with one copied string."""
    argv = list(sys.argv[1:] if argv is None else argv)
    slot = None
    while argv:
        a = argv.pop(0)
        if a in ("--list-gpus", "-l"):
            found = enumerate_gpus()
            if not found:
                _tell("no NVIDIA GPU enumerated")
                return 1
            _tell("\n".join(
                f"{g['slot']}  {g['name']}"
                + ("" if g["has_nvapi"] else "   (no NVAPI handle)")
                for g in found))
            return 0
        if a in ("--gpu", "-d"):
            if not argv:
                _tell("--gpu needs a PCI slot, e.g. --gpu 0000:01:00.0")
                return 2
            slot = argv.pop(0)
        elif a in ("-h", "--help"):
            _tell("usage: Druta [--gpu SLOT] [--list-gpus]\n\n"
                  "  --gpu SLOT   open on that card, e.g. 0000:02:00.0\n"
                  "  --list-gpus  print the slot and name of every card\n\n"
                  "With no --gpu, Druta opens on the lowest PCI slot.\n"
                  "Device > Card switches cards in a running window.")
            return 0
        else:
            _tell(f"unknown argument {a!r} (try --help)")
            return 2
    app = Druta(slot)
    if not app.gpu.available():
        _tell("No GPU backend: " + app.gpu.status_line()
              + ("\n\ncards present:\n" + "\n".join(
                  f"  {g['slot']}  {g['name']}" for g in app.gpu_list)
                 if app.gpu_list else ""))
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
