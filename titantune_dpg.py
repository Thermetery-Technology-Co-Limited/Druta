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
  * every write is behind the "Unlock controls" gate (except reset-to-stock,
    which only ever moves toward stock)
  * footgun knobs (force P-state, TCC, CUDA clocks, hard VF lock) are documented
    in README.md, never wired to a button
  * Tk's modal confirmations have no ImGui equivalent, so every write path that
    had one (editor apply, reset-curve, reset-all) is now a press-again
    confirmation: the first press states the plan, the second commits it.
    De-flatten is NOT one of them because it writes nothing - where Tk previewed
    it on a canvas, it STAGES onto the working curve, so the plan is visible on
    the plot and only 'Apply to GPU' can commit it.
"""
import ctypes
import math
import os
import threading
import time

import dearpygui.dearpygui as dpg

from nvbackend import (GPU, EVENT_REASONS, PERF_DECREASE_BITS, VF_STEP_KHZ,
                       VFP_POINTS, below_cap)

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
        self._reset_armed = False
        self._vf_reset_armed = False
        self._apply_armed = None   # the exact edit set the user confirmed
        self._drag_idx = None
        # THE record of what this app has locked the GPU clock to and why:
        # None, or {"why": "hold"|"manual", "lo", "hi", (+ idx/mv/want)}.
        # Hold and the Clocks menu drive the SAME nvmlDeviceSetGpuLockedClocks,
        # so a second source of truth would let the on-screen hold outlive a
        # Release that already dropped it in the driver.
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
        bot_h = max(self.s(110), int(H * 0.16))
        mid_h = max(self.s(220), H - tile_h - bot_h - self.s(96))
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
        dpg.set_value("state",
                      f"energy {d.get('energy_j',0):.0f} J\n{fantxt}\n"
                      f"offsets: core {d.get('core_off',0):+d} MHz   "
                      f"mem {mdisp:+d} {munit}\n"
                      f"volt-boost {d.get('vboost_pct','--')}%   "
                      f"VF-locked {d.get('vf_locked_domains') or 'none'}")

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

    def apply_lock(self):
        if not self.guard():
            return
        mn, mx = int(dpg.get_value("lock_min")), int(dpg.get_value("lock_max"))
        ok, m = self.gpu.lock_gpu_clocks(mn, mx)
        self.report((ok, m))
        if ok:
            self.set_lock_state({"why": "manual", "lo": mn, "hi": mx})

    def release_lock(self):
        """The ONE release path - Ctrl+H routes here too. Both drive the same
        driver-side lock, so sharing the code is what makes it impossible for
        the hold banner to survive a Release (or to be dropped while the driver
        still holds the clock, if the release fails)."""
        if not self.guard():
            return
        ok, m = self.gpu.reset_gpu_clocks()
        self.report((ok, m))
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
        ok, m = self.gpu.lock_gpu_clocks(gmax, gmax)
        self.report((ok, m))
        if ok:
            self.set_lock_state({"why": "manual", "lo": gmax, "hi": gmax})

    def set_lock_state(self, state):
        """Record what the clock lock is now, and redraw both indicators. Every
        path that moves the driver-side lock - Lock, Lock max, Release, Ctrl+H,
        Reset all - ends here, which is what stops a stale HOLD banner from
        claiming a point the card was already released from."""
        self._clk_lock = state
        held = state if state and state["why"] == "hold" else None
        if dpg.does_item_exist("vf_holdline"):
            dpg.set_value("vf_holdline", [[held["mv"]] if held else []])
        if not dpg.does_item_exist("hold_info"):
            return
        if held:
            txt = (f"HOLD  point {held['idx']} @ {held['mv']:.2f} mV - clock "
                   f"pinned at {held['hi']} MHz"
                   + (f", snapped DOWN from the point's {held['want']} MHz "
                      f"(the highest lockable value at or below it)"
                      if held["hi"] != held["want"] else "")
                   + "   •   Ctrl+H releases")
        elif state:
            txt = (f"clock locked to [{state['lo']}..{state['hi']}] MHz from "
                   f"the Clocks menu - no V/F point is held")
        else:
            txt = ""
        dpg.set_value("hold_info", txt)
        dpg.configure_item("hold_info", color=GOOD if held else WARN)

    def reset_all(self):
        if not self._reset_armed:
            self._reset_armed = True
            self.log("this zeroes offsets + voltage boost, restores the default "
                     "power limit, releases the clock lock, returns fans to auto "
                     "and resets the V/F curve - press again to confirm", False)
            return
        self._reset_armed = False
        failed = 0
        for ok, m in self.gpu.reset_all():
            self.log(m, ok)
            failed += (0 if ok else 1)
        # reset_all() releases the clock lock as one of its steps, so the hold
        # record has to go with it - a HOLD banner left over a released lock
        # would name a point the card is no longer pinned to. A release that
        # failed is one of the `failed` steps and is already in the log above.
        self.set_lock_state(None)
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
        with dpg.plot(tag="vf_plot", height=self.s(380), width=-1,
                      pan_button=dpg.mvMouseButton_Middle):
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
        # NOTE: no per-point drag widgets. Click-and-drag anywhere on the
        # plot grabs the NEAREST point (by voltage) and moves it, which is
        # how the Tk editor behaved and keeps every dot draggable without
        # 103 separate items.

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
        dpg.add_text("drag any dot  \u2022  A/D select  \u2022  W/S move "
                     "\u00b115 MHz  \u2022  hold Shift for \u00b145  \u2022  "
                     "Ctrl+H hold the selected point (again to release)",
                     color=DIM)

        # plot-wide mouse + keyboard control (panning is on the middle button,
        # so the left button belongs to the dots)
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
        # a confirmed plan dies with the edits it described - otherwise
        # re-composing the same edit set later would write without asking
        self._apply_armed = None
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

    def clamp_axes(self, fit=False):
        """Stop the plot being dragged off to infinity: constrain pan/zoom to a
        little beyond the actual data."""
        if not self.vf_points:
            return
        xs = [p["volt_mv"] for p in self.vf_points]
        ys = [self.wf(p["idx"]) / 1000.0 for p in self.vf_points]
        x0, x1 = min(xs) - 25, max(xs) + 25
        y0, y1 = min(ys) - 120, max(ys) + 200
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
        except Exception as e:
            self.log(f"axis clamp: {e}", False)

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
        if not self.vf_points:
            return
        xs = [p["volt_mv"] for p in self.vf_points]
        cur = [self.vf_by_idx[p["idx"]]["freq_mhz"] for p in self.vf_points]
        edit = [self.wf(p["idx"]) / 1000.0 for p in self.vf_points]
        dpg.set_value("vf_cur", [xs, cur])
        dpg.set_value("vf_edit", [xs, edit])
        self.update_vf_info()
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

    SHIFT_MULT = 3          # hold Shift to move 3 bins at a time

    def typing(self):
        """True while a text/number box has focus, so W/A/S/D typed into an
        input box never also retunes the curve."""
        return any(dpg.does_item_exist(t)
                   and (dpg.is_item_focused(t) or dpg.is_item_active(t))
                   for t in ("vcap", "vf_idx", "vf_set", "lock_min", "lock_max",
                             "log", "info"))

    def nearest_idx(self, volt_mv):
        if not self.vf_points:
            return None
        return min(self.vf_points,
                   key=lambda p: abs(p["volt_mv"] - volt_mv))["idx"]

    def on_plot_click(self, sender=None, app_data=None, user_data=None):
        """Left-click on the plot selects the nearest dot and begins a drag."""
        if not self.vf_points or not dpg.is_item_hovered("vf_plot"):
            return
        x, y = dpg.get_plot_mouse_pos()[:2]
        idx = self.nearest_idx(x)
        if idx is None:
            return
        self._drag_idx = idx
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
        self._drag_idx = None

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
        gain clock nobody asked for."""
        below = [c for c in self.lockable_list() if c <= mhz]
        return max(below) if below else None

    def hold_toggle(self):
        if self._clk_lock and self._clk_lock["why"] == "hold":
            # straight through the Release button's own handler: one release
            # path means the banner and the driver cannot end up disagreeing
            self.release_lock()
        else:
            self.hold_point()

    def hold_point(self):
        """TitanTune's answer to Afterburner's Ctrl+L curve lock: pin the card
        at the selected point's frequency with nvmlDeviceSetGpuLockedClocks, and
        the boost arbiter then supplies that point's voltage - the same
        observable result, built from the one clock write this app makes.

        Deliberately NOT the hard per-domain VF lock (NvAPI 0x39442CFB): its
        write struct is unverified on this card and it is rail-adjacent, so it
        stays read-only (see README). The locked-clock path is documented,
        reversible, and proven to hold at idle here with no load needed."""
        if not self.guard():
            return
        if self.vf_sel is None or self.vf_sel not in self.vf_by_idx:
            self.log("hold: no point selected - read the curve first", False)
            return
        idx = self.vf_sel
        p = self.vf_by_idx[idx]
        # the HARDWARE frequency, not wf(): the arbiter reads the curve that is
        # in the card, so a staged edit this point has not been written yet
        # would name a frequency that curve does not carry at this voltage
        want = int(round(p["freq_mhz"]))
        f = self.snap_lockable(want)
        if f is None:
            lst = self.lockable_list()
            self.log(f"cannot hold point {idx}: {want} MHz is below every "
                     f"lockable clock"
                     + (f" (the lowest is {lst[0]} MHz)" if lst else
                        " - the driver enumerated none"), False)
            return
        ok, m = self.gpu.lock_gpu_clocks(f, f)
        if not ok:
            self.log(m, False)
            return
        # no report() on success: the backend's "GPU clock locked to [f..f]"
        # says less than the line below and would push the snap note off the
        # ~9 rows the log shows
        self.set_lock_state({"why": "hold", "lo": f, "hi": f, "idx": idx,
                             "mv": p["volt_mv"], "want": want})
        if f != want:
            self.log(f"point {idx} is {want} MHz; held at {f} MHz, the highest "
                     f"lockable value", None)
        if self.vf_work.get(idx) != self.vf_orig.get(idx):
            self.log(f"note: point {idx} has a staged edit that is not in the "
                     f"card yet - the hold uses its hardware frequency", None)
        self.log(f"holding point {idx} @ {p['volt_mv']:.2f} mV at {f} MHz - the "
                 f"arbiter supplies that point's voltage. Ctrl+H releases",
                 True)

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
        self._apply_armed = None
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

    def vf_apply(self):
        if not self.guard() or not self.vf_points:
            return
        changed = {i: self.vf_work[i] for i in self.vf_work
                   if self.vf_work[i] != self.vf_orig[i]}
        if not changed:
            self._apply_armed = None
            self.log("no edits to apply")
            return
        cap = float(dpg.get_value("vcap"))
        wpts = self.work_pts()
        top, peak = self.curve_top(wpts, cap)
        _hw_top, hw_peak = self.curve_top(self.vf_points, cap)
        _pk, pidx, pmv, npk = GPU.peak_info(wpts)
        # Tk asked in a modal dialog carrying the ceiling and the reversibility
        # note; ImGui has no modal, so the first press states the plan and the
        # second commits it. The arm is keyed to the exact edit set: move one
        # point after arming and it must be confirmed again, so what was
        # described is always what gets written.
        plan = (round(cap, 2), tuple(sorted(changed.items())))
        if self._apply_armed != plan:
            self._apply_armed = plan
            # De-flatten's peak warning has to reach the moment of commit: its
            # line is one of ~9 visible in the log and any nudge since pushes it
            # off the fold, so a plan that drags the peak DOWN (a cap that
            # landed in the low-voltage floor) would otherwise be confirmed
            # against a message that only ever reads like a raise.
            warn = (f"WARNING: this LOWERS the curve's peak from "
                    f"{hw_peak:.0f} to {peak:.0f} MHz. " if peak < hw_peak
                    else "")
            self.log(warn + f"about to write {len(changed)} edited point(s): "
                     f"top ≤{cap:.0f} mV becomes {top:.0f} MHz, peak "
                     f"{peak:.0f} MHz held by {npk} point(s), the card would "
                     f"park at idx {pidx} @ {pmv:.2f} mV - press Apply to GPU "
                     f"again to write it. Reversible via 'Reset curve to "
                     f"stock' or a reboot", False)
            return
        self._apply_armed = None
        predicted = {i: self.wf(i) for i in changed}
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
        ok, m = self.gpu.rephase_deltas()
        self.report((ok, m))
        if ok:
            self.vf_read(force=True)

    def vf_reset(self):
        """Zeroing every delta only moves the card toward stock, but it is still
        a full 103-row table write AND it re-reads with force=True, which throws
        staged edits away with no undo. So it arms and commits the way 'Reset
        all to stock' does - that button does strictly more and still asks -
        and it sits behind the unlock gate like every other write."""
        if not self.guard():
            return
        pending = sum(1 for i in self.vf_work
                      if self.vf_work.get(i) != self.vf_orig.get(i))
        if not self._vf_reset_armed:
            self._vf_reset_armed = True
            self.log(f"this zeroes all {VFP_POINTS} V/F deltas - the factory "
                     f"curve - and discards {pending} staged edit(s) with no "
                     f"undo; press again to confirm", False)
            return
        self._vf_reset_armed = False
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
            # The clock lock lives up here because it was eating the widest row
            # on the Control tab. It is the same widgets with the same tags, so
            # guard() and the unlock gate (_ctl_widgets, below) still cover it.
            # Ctrl+H (hold a V/F point) drives this SAME driver-side lock from
            # the curve editor - see set_lock_state for why there is exactly one
            # record of what is locked.
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
                             "Ctrl+H on the curve editor drives this same lock",
                             color=DIM)
            self._ctl_widgets += ["lock_min", "lock_max", "go_lock",
                                  "go_release", "go_lockmax"]
            with dpg.menu(label="Help"):
                dpg.add_menu_item(label="Keyboard shortcuts",
                                  user_data="win_keys", callback=self.show_win)
                dpg.add_menu_item(label="About", user_data="win_about",
                                  callback=self.show_win)

    # Only the bindings that EXIST in build_vf's handler_registry. A shortcut
    # list that names a key nothing implements is worse than no list at all, so
    # anything added to that registry has to gain a row here in the same change.
    VF_KEYS = [
        ("W / S", "move the selected point +/- 15 MHz (one clock bin)"),
        ("A / D", "select the previous / next point along the curve"),
        ("Shift + W/S", "move 3 bins at once (+/- 45 MHz)"),
        ("Shift + A/D", "step the selection 3 points at a time"),
        ("left-click", "select the dot nearest the click, by voltage"),
        ("left-drag", "move the grabbed dot; it snaps to whole 15 MHz bins"),
        ("middle-drag", "pan the plot (the left button belongs to the dots)"),
        ("Ctrl + H", "hold the selected point: pins the clock there so the "
                     "boost arbiter supplies that point's voltage. Press "
                     "again to release. The point's frequency snaps DOWN to "
                     "the nearest lockable clock, never up"),
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
    #  main                                                                  #
    # ====================================================================== #
    def run(self):
        if not self.gpu.available():
            print("No GPU backend:", self.gpu.status_line())
            return
        dpg.create_context()
        dpg.create_viewport(title="TitanTune", width=self.s(1180),
                            height=self.s(860))
        self.load_fonts()

        st = self.gpu.static
        self.build_menu_bar()             # viewport-owned, so NOT inside 'root'
        with dpg.window(tag="root"):
            # DPG draws the viewport menu bar OVER the primary window instead of
            # insetting it, so without this pad the title row is half-hidden
            # behind File/Device/Clocks/Help. relayout() keeps it in step with
            # the bar's measured height.
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

        last = 0.0
        while dpg.is_dearpygui_running():
            now = time.perf_counter()
            if now - last >= 0.25:
                last = now
                self.relayout()
                with self._lock:
                    d, err, snap_t = self._snap, self._snap_err, self._snap_t
                if err:
                    self.log_once("read", f"read error: {err}")
                else:
                    self.clear_once("read")
                self.set_stale(err, snap_t)
                # Panels refresh even while a read is failing (Tk did the same:
                # the snapshot is simply the last good one), and each panel gets
                # its OWN try/except - sharing one meant a Monitor glitch also
                # silenced the Control tab's live clock readout, the only
                # confirmation that an applied offset took effect.
                if d:
                    for name, fn in (("monitor", self.refresh_monitor),
                                     ("control", self.refresh_control)):
                        try:
                            fn(d)
                            self.clear_once(name)
                        except Exception as e:
                            self.log_once(name, f"{name} panel: {e}")
            dpg.render_dearpygui_frame()

        self._stop.set()
        dpg.destroy_context()


if __name__ == "__main__":
    TitanTune().run()
