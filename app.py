"""
TitanTune - a compact Afterburner alternative for the Titan-RTX-on-Strix card.

Foregrounds the telemetry Afterburner hides on Turing (hotspot delta, GPU/board
power split, the 9-reason clocks-event mask, the insufficient-aux-power canary,
PCIe error counters) and offers the reversible write knobs (clock offsets, power
limit, locked clocks, fan). Riskier knobs are documented in the Info tab rather
than fired from a button.
"""
import ctypes
import math
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from nvbackend import (GPU, EVENT_REASONS, PERF_DECREASE_BITS,
                       VF_STEP_KHZ, below_cap)

# ---- HiDPI --------------------------------------------------------------- #
# tkinter is DPI-unaware by default: on a scaled display Windows renders the app
# at 96 DPI then bitmap-stretches it, which makes text look blurry/"dotted". We
# declare per-monitor awareness so the app draws at native resolution, then scale
# fonts (via tk scaling) and pixel geometry (via px()) by the same factor so the
# window keeps its apparent size but renders crisply.
SCALE = 1.0
NO_POLL = False
USE_COMPOSITED = False   # opt-in: WS_EX_COMPOSITED HANGS repaint here


def px(n):
    return int(round(n * SCALE))


def _enable_dpi_awareness():
    """SYSTEM DPI awareness. Deliberately NOT per-monitor-v2: Tk 8.6 has no
    WM_DPICHANGED handling, and PMv2 makes Windows negotiate DPI with the window
    during moves - which on some setups turns dragging into a crawl. System
    awareness gives identical crispness on a single-scale desktop."""
    for fn in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
               lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            fn()
            return
        except Exception:
            continue


def enable_double_buffer(win):
    """Turn on WS_EX_COMPOSITED for a toplevel.

    Measured cause of this app's drag lag: with "show window contents while
    dragging" on, Windows composites the window's CONTENT every move step. Each
    child HWND paints and blits separately, so cost scales with child count -
    an empty Tk window at the same size drags smoothly, this one did not, and
    dragging with an outline (no content composited) is smooth.

    WS_EX_COMPOSITED makes Windows paint the window and all descendants into a
    single off-screen buffer and present it once, collapsing all those blits
    into one. Applied after the HWND exists; failure is non-fatal.
    """
    GWL_EXSTYLE, WS_EX_COMPOSITED = -20, 0x02000000
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(int(win.winfo_id()), 2)  # GA_ROOT
        getl = ctypes.windll.user32.GetWindowLongPtrW
        setl = ctypes.windll.user32.SetWindowLongPtrW
        getl.restype = ctypes.c_longlong
        setl.restype = ctypes.c_longlong
        setl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
        cur = getl(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
        setl(ctypes.c_void_p(hwnd), GWL_EXSTYLE, cur | WS_EX_COMPOSITED)
        return True
    except Exception:
        return False


def _detect_scale():
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0

# ---- palette -------------------------------------------------------------- #
BG = "#16181d"
PANEL = "#1e2128"
PANEL2 = "#252932"
TEXT = "#e6e8ec"
DIM = "#8b9099"
ACCENT = "#4aa3ff"
GOOD = "#46d17a"
WARN = "#ffcb47"
BAD = "#ff5c5c"
IDLE = "#3a3f4b"

FONT = "Segoe UI"


def _bind_autowrap(label, container=None, pad=16):
    """Keep `label`'s wraplength tracking the width of `container` (or the
    label itself, if no container is given) so long dynamic status strings
    wrap onto the next line instead of running off the window edge and being
    clipped. tk.Label never wraps unless wraplength (pixels) is set - this
    makes it dynamic across window resizes via <Configure>.

    Safe to call before the widget is mapped (falls back to a no-op until the
    first <Configure> fires) and safe during teardown (guards tk.TclError)."""
    target = container if container is not None else label
    last = {"w": -1}

    def _on_configure(event=None):
        try:
            w = target.winfo_width()
            # Only reconfigure when the WIDTH actually changed. Without this
            # guard, every window move fires a stream of <Configure> events and
            # each label.config() re-dirties geometry - a feedback loop that
            # makes dragging the window crawl. A pure move keeps width constant,
            # so this becomes a no-op and drags stay smooth.
            if w > 1 and w != last["w"]:
                last["w"] = w
                label.config(wraplength=max(1, w - pad))
        except tk.TclError:
            pass

    target.bind("<Configure>", _on_configure)
    _on_configure()


class Tile(tk.Frame):
    """A big-number stat tile."""
    def __init__(self, parent, label, unit="", accent=ACCENT):
        super().__init__(parent, bg=PANEL, padx=12, pady=8,
                         highlightbackground=PANEL2, highlightthickness=1)
        tk.Label(self, text=label.upper(), bg=PANEL, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        row = tk.Frame(self, bg=PANEL)
        row.pack(anchor="w", fill="x")
        self.value = tk.Label(row, text="--", bg=PANEL, fg=accent,
                              font=(FONT, 22, "bold"))
        self.value.pack(side="left")
        self.unit = tk.Label(row, text=unit, bg=PANEL, fg=DIM,
                             font=(FONT, 9), padx=4)
        self.unit.pack(side="left", anchor="s", pady=(0, 6))
        self.sub = tk.Label(self, text="", bg=PANEL, fg=DIM, font=(FONT, 8))
        self.sub.pack(anchor="w")

    def set(self, value, sub=None, color=None):
        self.value.config(text=value)
        if color:
            self.value.config(fg=color)
        if sub is not None:
            self.sub.config(text=sub)


class Lamp(tk.Frame):
    """A labelled status dot for throttle / event reasons."""
    def __init__(self, parent, label):
        super().__init__(parent, bg=PANEL)
        self.dot = tk.Canvas(self, width=px(12), height=px(12), bg=PANEL,
                             highlightthickness=0)
        self.circle = self.dot.create_oval(px(2), px(2), px(11), px(11),
                                           fill=IDLE, outline="")
        self.dot.pack(side="left")
        tk.Label(self, text=label, bg=PANEL, fg=DIM,
                 font=(FONT, 8)).pack(side="left", padx=4)

    def set(self, on, color=BAD):
        self.dot.itemconfig(self.circle, fill=(color if on else IDLE))


class Bar(tk.Frame):
    """A labelled horizontal percentage bar."""
    def __init__(self, parent, label, width=150, color=ACCENT):
        super().__init__(parent, bg=PANEL)
        width = px(width)
        tk.Label(self, text=label, bg=PANEL, fg=DIM, font=(FONT, 8),
                 width=6, anchor="w").pack(side="left")
        self.cv = tk.Canvas(self, width=width, height=px(12), bg=PANEL2,
                            highlightthickness=0)
        self.cv.pack(side="left", padx=4)
        self.rect = self.cv.create_rectangle(0, 0, 0, 12, fill=color, outline="")
        self.txt = tk.Label(self, text="--", bg=PANEL, fg=TEXT, font=(FONT, 8),
                            width=5, anchor="e")
        self.txt.pack(side="left")
        self.width = width
        self.color = color

    def set(self, pct, text=None):
        true_pct = float(pct)
        pct = max(0.0, min(100.0, true_pct))
        self.cv.coords(self.rect, 0, 0, self.width * pct / 100.0, 12)
        col = self.color
        if pct >= 95:
            col = BAD
        elif pct >= 80:
            col = WARN
        self.cv.itemconfig(self.rect, fill=col)
        self.txt.config(text=text if text is not None else f"{pct:.0f}%")



# --------------------------------------------------------------------------- #
#  Canvas-drawn widget GROUPS                                                  #
#                                                                              #
#  Tk creates a real Windows HWND for EVERY widget. Measured on this machine:   #
#  a window with ~92 child HWNDs drags laggily and drags the whole desktop down #
#  with it (the window manager has to move/clip/repaint every child), while a   #
#  window with 4 HWNDs - even one holding a 103-point canvas - is perfectly     #
#  smooth. Canvas ITEMS are nearly free; widgets are not. So the tiles, lamps   #
#  and bars are drawn as items inside ONE canvas each instead of being dozens   #
#  of Frame/Label/Canvas widgets.                                              #
# --------------------------------------------------------------------------- #
class TileGroup(tk.Frame):
    """One canvas holding all the big stat tiles."""
    def __init__(self, parent, specs, height=78):
        super().__init__(parent, bg=BG)
        self.specs = specs
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0,
                            height=px(height))
        self.cv.pack(fill="both", expand=True)
        self.items = {}
        self._lastw = 0
        self.cv.bind("<Configure>", self._on_cfg)
        self._vals = {k: ("--", "", None) for k, _l, _u, _c in specs}

    def _on_cfg(self, e):
        if e.width != self._lastw:
            self._lastw = e.width
            self._layout()

    def _layout(self):
        cv = self.cv
        cv.delete("all")
        self.items.clear()
        n = len(self.specs)
        if n == 0 or self._lastw <= 1:
            return
        gap = px(8)
        w = (self._lastw - gap * (n - 1)) / n
        h = int(cv.winfo_height()) or px(78)
        for i, (key, label, unit, color) in enumerate(self.specs):
            x = i * (w + gap)
            cv.create_rectangle(x, 0, x + w, h, fill=PANEL, outline=PANEL2)
            cv.create_text(x + px(12), px(10), text=label.upper(), anchor="w",
                           fill=DIM, font=(FONT, 8, "bold"))
            val = cv.create_text(x + px(12), px(34), text="--", anchor="w",
                                 fill=color, font=(FONT, 20, "bold"))
            cv.create_text(x + w - px(10), px(38), text=unit, anchor="e",
                           fill=DIM, font=(FONT, 8))
            sub = cv.create_text(x + px(12), px(60), text="", anchor="w",
                                 fill=DIM, font=(FONT, 8))
            self.items[key] = (val, sub)
            v, sv, c = self._vals.get(key, ("--", "", None))
            cv.itemconfig(val, text=v)
            if c:
                cv.itemconfig(val, fill=c)
            cv.itemconfig(sub, text=sv)

    def set(self, key, value, sub=None, color=None):
        cur = self._vals.get(key, ("--", "", None))
        self._vals[key] = (str(value),
                           cur[1] if sub is None else sub,
                           color or cur[2])
        it = self.items.get(key)
        if not it:
            return
        val, subit = it
        self.cv.itemconfig(val, text=str(value))
        if color:
            self.cv.itemconfig(val, fill=color)
        if sub is not None:
            self.cv.itemconfig(subit, text=sub)


class LampGroup(tk.Frame):
    """One canvas holding a grid of status dots + labels."""
    def __init__(self, parent, names, cols=2, rowh=17):
        super().__init__(parent, bg=PANEL)
        rows = (len(names) + cols - 1) // cols
        self.cv = tk.Canvas(self, bg=PANEL, highlightthickness=0,
                            height=px(rowh) * rows)
        self.cv.pack(fill="x", expand=True)
        self.dots = {}
        self.names, self.cols, self.rowh = names, cols, rowh
        self._lastw = 0
        self.cv.bind("<Configure>", self._on_cfg)
        self._state = {n: (False, IDLE) for n in names}

    def _on_cfg(self, e):
        if e.width != self._lastw:
            self._lastw = e.width
            self._layout()

    def _layout(self):
        cv = self.cv
        cv.delete("all")
        self.dots.clear()
        if self._lastw <= 1:
            return
        colw = self._lastw / self.cols
        r = px(4)
        for i, name in enumerate(self.names):
            cx = (i % self.cols) * colw + px(8)
            cy = (i // self.cols) * px(self.rowh) + px(9)
            on, col = self._state.get(name, (False, IDLE))
            d = cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                               fill=(col if on else IDLE), outline="")
            cv.create_text(cx + px(10), cy, text=name, anchor="w", fill=DIM,
                           font=(FONT, 8))
            self.dots[name] = d

    def set(self, name, on, color=BAD):
        self._state[name] = (bool(on), color)
        d = self.dots.get(name)
        if d:
            self.cv.itemconfig(d, fill=(color if on else IDLE))


class BarGroup(tk.Frame):
    """One canvas holding a stack of labelled percentage bars."""
    def __init__(self, parent, rows, rowh=18, barw=150):
        super().__init__(parent, bg=PANEL)
        self.rows = rows                       # [(key, label, color), ...]
        self.rowh, self.barw = rowh, barw
        self.cv = tk.Canvas(self, bg=PANEL, highlightthickness=0,
                            height=px(rowh) * len(rows))
        self.cv.pack(fill="x", expand=True)
        self.items = {}
        self._lastw = 0
        self.cv.bind("<Configure>", self._on_cfg)
        self._vals = {k: (0.0, None) for k, _l, _c in rows}

    def _on_cfg(self, e):
        if e.width != self._lastw:
            self._lastw = e.width
            self._layout()

    def _layout(self):
        cv = self.cv
        cv.delete("all")
        self.items.clear()
        if self._lastw <= 1:
            return
        lx, bx = px(4), px(52)
        bw = px(self.barw)
        for i, (key, label, color) in enumerate(self.rows):
            y = i * px(self.rowh) + px(9)
            cv.create_text(lx, y, text=label, anchor="w", fill=DIM,
                           font=(FONT, 8))
            cv.create_rectangle(bx, y - px(6), bx + bw, y + px(6),
                                fill=PANEL2, outline="")
            fill = cv.create_rectangle(bx, y - px(6), bx, y + px(6),
                                       fill=color, outline="")
            txt = cv.create_text(bx + bw + px(8), y, text="--", anchor="w",
                                 fill=TEXT, font=(FONT, 8))
            self.items[key] = (fill, txt, bx, bw, y, color)
            pct, t = self._vals.get(key, (0.0, None))
            self._paint(key, pct, t)

    def _paint(self, key, pct, text):
        it = self.items.get(key)
        if not it:
            return
        fill, txt, bx, bw, y, color = it
        p = max(0.0, min(100.0, float(pct)))
        col = BAD if p >= 95 else (WARN if p >= 80 else color)
        self.cv.coords(fill, bx, y - px(6), bx + bw * p / 100.0, y + px(6))
        self.cv.itemconfig(fill, fill=col)
        self.cv.itemconfig(txt, text=text if text is not None else f"{p:.0f}%")

    def set(self, key, pct, text=None):
        self._vals[key] = (pct, text)
        self._paint(key, pct, text)




class DragGuard:
    """Know EXACTLY when Windows is running its modal move/size loop.

    Root cause of this app's drag lag, from Tk 8.6's own source: on
    WM_ENTERSIZEMOVE Tk does Tcl_SetServiceMode(TCL_SERVICE_ALL) and then calls
    Tcl_ServiceAll() from its window procedure - draining the WHOLE idle queue
    and every after() timer synchronously, 3-4 times per mouse-move step, inside
    the OS modal loop. So during a drag Tk runs the app's redraws instead of
    moving the window.

    Subclassing the wrapper HWND gives the precise drag window, so everything
    can be frozen for its duration instead of guessing with a timeout.
    """
    WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, GWLP_WNDPROC = 0x0231, 0x0232, -4

    def __init__(self, win, on_end=None):
        self.dragging = False
        self._old = None
        self._proc = None
        self.ok = False
        try:
            u = ctypes.windll.user32
            LRESULT = ctypes.c_longlong
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p,
                                         ctypes.c_uint, ctypes.c_ulonglong,
                                         ctypes.c_longlong)
            u.CallWindowProcW.restype = LRESULT
            u.CallWindowProcW.argtypes = [ctypes.c_longlong, ctypes.c_void_p,
                                          ctypes.c_uint, ctypes.c_ulonglong,
                                          ctypes.c_longlong]
            u.SetWindowLongPtrW.restype = ctypes.c_longlong
            u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_longlong]
            u.GetWindowLongPtrW.restype = ctypes.c_longlong
            u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]

            def proc(h, msg, wp, lp):
                if msg == self.WM_ENTERSIZEMOVE:
                    self.dragging = True
                elif msg == self.WM_EXITSIZEMOVE:
                    self.dragging = False
                    if on_end:
                        try:
                            win.after_idle(on_end)
                        except tk.TclError:
                            pass
                return u.CallWindowProcW(self._old, h, msg, wp, lp)

            self._proc = WNDPROC(proc)      # keep a ref or the thunk is freed
            win.update_idletasks()
            hwnd = ctypes.c_void_p(int(win.wm_frame(), 16))
            self._old = u.GetWindowLongPtrW(hwnd, self.GWLP_WNDPROC)
            u.SetWindowLongPtrW(
                hwnd, self.GWLP_WNDPROC,
                ctypes.cast(self._proc, ctypes.c_void_p).value)
            self.ok = True
        except Exception:
            self.ok = False


class MonitorView(tk.Canvas):
    """The ENTIRE Monitor tab drawn as items on ONE canvas.

    Measured root cause of this app's drag lag: with "show window contents while
    dragging" enabled, Windows composites the window content on every move step
    and each child HWND is painted/blitted separately. Proven with probes: a
    1500x1050 window holding ONE canvas of 400 drawn items drags SMOOTHLY, while
    a 700x500 window holding ~50 small widgets is LAGGY - so the cost tracks
    child-window count, not painted area. Canvas items cost almost nothing;
    widgets cost a lot. This tab therefore uses zero child widgets.
    """
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG, highlightthickness=0)
        self.app = app
        self.ids = {}
        self._size = (0, 0)
        self._vals = {}
        self.bind("<Configure>", self._on_cfg)

    def _on_cfg(self, e):
        if (e.width, e.height) != self._size:
            self._size = (e.width, e.height)
            if getattr(self.app, "_guard", None) and self.app._guard.dragging:
                return                     # relayout after the drag, not during
            self._layout()

    # ---- drawing helpers ---- #
    def _panel(self, x, y, w, h, title):
        self.create_rectangle(x, y, x + w, y + h, fill=PANEL, outline=PANEL2)
        self.create_text(x + px(10), y + px(11), text=title, anchor="w",
                         fill=ACCENT, font=(FONT, 8, "bold"))

    def _layout(self):
        self.delete("all")
        self.ids.clear()
        W, H = self._size
        if W <= 1 or H <= 1:
            return
        pad = px(6)

        # ---- top: six stat tiles ----
        th = px(76)
        specs = [("core", "Core clock", "MHz", ACCENT),
                 ("mem", "Mem clock", "MHz", ACCENT),
                 ("edge", "Edge temp", "°C", GOOD),
                 ("hot", "Hotspot", "°C", WARN),
                 ("pwr", "Power", "W", ACCENT),
                 ("vcore", "Vcore", "mV", ACCENT)]
        tw = (W - pad * (len(specs) + 1)) / len(specs)
        for i, (key, label, unit, color) in enumerate(specs):
            x = pad + i * (tw + pad)
            self.create_rectangle(x, pad, x + tw, pad + th, fill=PANEL,
                                  outline=PANEL2)
            self.create_text(x + px(10), pad + px(12), text=label.upper(),
                             anchor="w", fill=DIM, font=(FONT, 8, "bold"))
            self.ids[f"t_{key}"] = self.create_text(
                x + px(10), pad + px(38), text="--", anchor="w", fill=color,
                font=(FONT, 19, "bold"))
            self.create_text(x + tw - px(8), pad + px(42), text=unit,
                             anchor="e", fill=DIM, font=(FONT, 8))
            self.ids[f"s_{key}"] = self.create_text(
                x + px(10), pad + px(62), text="", anchor="w", fill=DIM,
                font=(FONT, 8), width=tw - px(16))

        # ---- middle: throttle panel (left) and power/util panel (right) ----
        my = pad * 2 + th
        mh = H - my - px(92) - pad
        pw_ = (W - pad * 3) / 2
        self._panel(pad, my, pw_, mh, "THROTTLE / CLOCKS-EVENT REASONS")
        yy = my + px(28)
        for i, (_b, name) in enumerate(EVENT_REASONS):
            cx = pad + px(14) + (i % 2) * (pw_ / 2)
            cy = yy + (i // 2) * px(17)
            r = px(4)
            self.ids[f"lamp_{name}"] = self.create_oval(
                cx - r, cy - r, cx + r, cy + r, fill=IDLE, outline="")
            self.create_text(cx + px(10), cy, text=name, anchor="w", fill=DIM,
                             font=(FONT, 8))
        yy += ((len(EVENT_REASONS) + 1) // 2) * px(17) + px(10)
        self.create_text(pad + px(10), yy, text="perf-decrease (NVAPI):",
                         anchor="w", fill=DIM, font=(FONT, 8, "bold"))
        yy += px(16)
        for i, (_b, name) in enumerate(PERF_DECREASE_BITS):
            cx = pad + px(14) + (i % 2) * (pw_ / 2)
            cy = yy + (i // 2) * px(17)
            r = px(4)
            self.ids[f"pd_{name}"] = self.create_oval(
                cx - r, cy - r, cx + r, cy + r, fill=IDLE, outline="")
            self.create_text(cx + px(10), cy, text=name, anchor="w", fill=DIM,
                             font=(FONT, 8))

        bx = pad * 2 + pw_
        self._panel(bx, my, pw_, mh, "POWER SPLIT & UTILIZATION")
        rows = [("gpu", "GPU", ACCENT), ("board", "Board", "#a06cff"),
                ("target", "PL tgt", WARN), (None, None, None),
                ("ugpu", "GPU", GOOD), ("ufb", "FB", GOOD),
                ("uvid", "VID", GOOD), ("ubus", "BUS", GOOD)]
        by = my + px(30)
        barx = bx + px(58)
        barw = min(px(170), pw_ - px(120))
        for i, (key, label, color) in enumerate(rows):
            y = by + i * px(19)
            if key is None:
                self.create_line(bx + px(8), y, bx + pw_ - px(8), y,
                                 fill=PANEL2)
                continue
            self.create_text(bx + px(10), y, text=label, anchor="w", fill=DIM,
                             font=(FONT, 8))
            self.create_rectangle(barx, y - px(6), barx + barw, y + px(6),
                                  fill=PANEL2, outline="")
            self.ids[f"bar_{key}"] = self.create_rectangle(
                barx, y - px(6), barx, y + px(6), fill=color, outline="")
            self.ids[f"bart_{key}"] = self.create_text(
                barx + barw + px(8), y, text="--", anchor="w", fill=TEXT,
                font=(FONT, 8))
            self.ids[f"barc_{key}"] = (barx, barw, y, color)

        # ---- bottom: pcie + state ----
        byy = my + mh + pad
        bh = px(92) - pad
        self._panel(pad, byy, pw_, bh, "PCIE LINK")
        self.ids["pcie"] = self.create_text(
            pad + px(10), byy + px(30), text="--", anchor="nw", fill=TEXT,
            font=(FONT, 9), width=pw_ - px(20))
        self._panel(bx, byy, pw_, bh, "STATE")
        self.ids["state"] = self.create_text(
            bx + px(10), byy + px(30), text="--", anchor="nw", fill=TEXT,
            font=(FONT, 9), width=pw_ - px(20))
        for k, v in self._vals.items():        # repaint cached values
            self._apply(k, v)

    # ---- value updates ---- #
    def _apply(self, key, val):
        i = self.ids.get(key)
        if i is None:
            return
        if key.startswith("bar_"):
            barx, barw, y, color = self.ids[f"barc_{key[4:]}"]
            pct = max(0.0, min(100.0, float(val)))
            col = BAD if pct >= 95 else (WARN if pct >= 80 else color)
            self.coords(i, barx, y - px(6), barx + barw * pct / 100.0,
                        y + px(6))
            self.itemconfig(i, fill=col)
        elif key.startswith("lamp_") or key.startswith("pd_"):
            on, col = val
            self.itemconfig(i, fill=(col if on else IDLE))
        elif isinstance(val, tuple):
            txt, col = val
            self.itemconfig(i, text=txt, **({"fill": col} if col else {}))
        else:
            self.itemconfig(i, text=str(val))

    def set(self, key, val):
        self._vals[key] = val
        self._apply(key, val)


class App(tk.Tk):
    def __init__(self, gpu):
        super().__init__()
        self.gpu = gpu
        s = gpu.static
        self.title("TitanTune")
        self.configure(bg=BG)
        # match Tk's point->pixel scaling to the display so fonts render crisp
        self.tk.call("tk", "scaling", SCALE * 96.0 / 72.0)
        self.geometry(f"{px(1000)}x{px(700)}")
        self.minsize(px(900), px(640))

        self._init_style()
        self._header(s)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.mon = tk.Frame(self.nb, bg=BG)
        self.ctl = tk.Frame(self.nb, bg=BG)
        self.info = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.mon, text="  Monitor  ")
        self.nb.add(self.ctl, text="  Control  ")
        self.nb.add(self.info, text="  Info  ")

        self._once = {}
        self._after_id = None
        # Telemetry runs on a BACKGROUND thread. Driver calls (NVAPI/NVML) block
        # for tens of ms; doing them on the UI thread stalled the Tk message pump
        # ~1x/sec, which made dragging the window lurch behind the cursor.
        self._snap = None
        self._snap_err = None
        self._snap_seq = 0        # bumped by the poller; UI repaints only on change
        self._drawn_seq = -1
        self._snap_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="titantune-poll")
        if not NO_POLL:
            self._poller.start()
        self._build_monitor()
        self._build_control()
        self._build_info()

        # While the user drags the title bar, Windows runs a MODAL move loop and
        # the app owns mouse capture; the system input queue is synchronised with
        # it, so any work this app does during the drag backs the queue up and
        # makes the WHOLE desktop lag - which is exactly the reported symptom.
        # Tk still services timers inside that loop, so our periodic repaint was
        # running mid-drag. Freeze all repainting until the move settles.
        self._last_cfg = 0.0
        self._guard = DragGuard(self, on_end=self._after_drag)
        self.bind("<Configure>", self._note_configure)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if USE_COMPOSITED:
            enable_double_buffer(self)
        self._tick()

    def _note_configure(self, ev=None):
        # A <Configure> bound on the toplevel ALSO fires for every descendant
        # widget (the toplevel is in every widget's bindtags), so this runs many
        # times per move step. Ignore everything that is not this window.
        if ev is not None and ev.widget is not self:
            return
        self._last_cfg = time.monotonic()

    def _after_drag(self):
        self._drawn_seq = -1          # force one repaint once the drag ends
        self._note_configure()

    def _moving(self):
        if self._guard.ok:
            return self._guard.dragging
        return (time.monotonic() - self._last_cfg) < 0.20

    # ---- styling ---------------------------------------------------------- #
    def _init_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=DIM,
                     padding=(16, 6), font=(FONT, 9, "bold"))
        st.map("TNotebook.Tab", background=[("selected", PANEL2)],
               foreground=[("selected", TEXT)])
        st.configure("TScale", background=PANEL)

    def _header(self, s):
        h = tk.Frame(self, bg=BG, padx=12, pady=8)
        h.pack(fill="x")
        tk.Label(h, text="TitanTune", bg=BG, fg=ACCENT,
                 font=(FONT, 15, "bold")).pack(side="left")
        admin = "admin" if s["admin"] else "NOT admin (clock lock / fan / PL need admin)"
        acol = GOOD if s["admin"] else WARN
        tk.Label(h, text=f"{s['name']}   •   driver {s['driver']}"
                         f"   •   vbios {s['vbios']}",
                 bg=BG, fg=DIM, font=(FONT, 9)).pack(side="left", padx=14)
        tk.Label(h, text=admin, bg=BG, fg=acol,
                 font=(FONT, 8, "bold")).pack(side="right")

    # ---- memory clock presentation ---------------------------------------- #
    def _mem_fmt(self, reported):
        """(text, sub) - true memory clock when the memory type is known,
        otherwise the raw NVAPI figure. Gbps is correct either way."""
        if not isinstance(reported, (int, float)):
            return "--", ""
        div = self.gpu.static.get("mem_div")
        gbps = reported * 2 / 1000.0
        mtype = self.gpu.static.get("mem_type", "unknown")
        if div:
            v = reported / div
            txt = f"{v:.1f}".rstrip("0").rstrip(".")
            return txt, f"{mtype} · {gbps:.2f} Gbps"
        return f"{reported:.0f}", f"{mtype} · {gbps:.2f} Gbps (raw NVAPI)"

    # ---- monitor tab ------------------------------------------------------ #
    def _build_monitor(self):
        # ONE canvas, zero child widgets - see MonitorView's docstring for the
        # measured reason (child HWNDs are what make dragging lag).
        self.monitor = MonitorView(self.mon, self)
        self.monitor.pack(fill="both", expand=True, padx=4, pady=4)

    def _panel(self, parent, title):
        f = tk.Frame(parent, bg=PANEL, padx=10, pady=8,
                     highlightbackground=PANEL2, highlightthickness=1)
        tk.Label(f, text=title.upper(), bg=PANEL, fg=ACCENT,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        return f

    # ---- control tab ------------------------------------------------------ #
    def _build_control(self):
        c = self.ctl
        gate = tk.Frame(c, bg=BG, padx=10, pady=8)
        gate.pack(fill="x")
        self.unlocked = tk.BooleanVar(value=False)
        tk.Checkbutton(gate, text="Unlock controls", variable=self.unlocked,
                       command=self._toggle_lock, bg=BG, fg=WARN,
                       selectcolor=PANEL, activebackground=BG,
                       activeforeground=WARN, font=(FONT, 10, "bold"),
                       highlightthickness=0).pack(side="left")
        tk.Label(gate, text="all changes below are reversible and reset on reboot",
                 bg=BG, fg=DIM, font=(FONT, 8)).pack(side="left", padx=10)

        s = self.gpu.static
        body = tk.Frame(c, bg=BG)
        body.pack(fill="both", expand=True, padx=10)
        self._ctl_widgets = []
        self._sliders = {}

        core_lo, core_hi = -200, 300
        if s.get("core_off_range"):
            core_lo, core_hi = s["core_off_range"][0], s["core_off_range"][1]
        self.sl_core = self._slider_row(body, 0, "core", "Core clock offset",
                                        "MHz", core_lo, core_hi, 0,
                                        lambda v: self._apply_core(v),
                                        resolution=VF_STEP_KHZ // 1000)
        # for a known GDDR type the slider is in TRUE memory MHz, else raw
        # ('effective'); mscale = NVML units per slider unit. Round bounds inward
        # so every reachable slider position maps to an in-range NVML value.
        mscale, _munit = self.gpu.mem_offset_scale()
        mem_lo, mem_hi = -500, 1500
        if s.get("mem_off_range"):
            mem_lo = int(math.ceil(s["mem_off_range"][0] / mscale))
            mem_hi = int(math.floor(s["mem_off_range"][1] / mscale))
        mem_label = ("Memory offset (true)" if s.get("mem_div")
                     else "Memory offset (effective)")
        self.sl_mem = self._slider_row(body, 1, "mem", mem_label,
                                       "MHz", mem_lo, mem_hi, 0,
                                       lambda v: self._apply_mem(v))
        pl_lo = s.get("pl_min_mw", 100000) // 1000
        pl_hi = s.get("pl_max_mw", 320000) // 1000
        pl_def = s.get("pl_def_mw", 260000) // 1000
        self.sl_pl = self._slider_row(body, 2, "pl", "Power limit", "W",
                                      pl_lo, pl_hi, pl_def,
                                      lambda v: self._apply_pl(v))
        _vraw = self.gpu.read_voltage_boost()
        vb0 = 0 if _vraw is None else max(0, min(100, int(_vraw)))
        self.sl_volt = self._slider_row(body, 3, "volt", "Core voltage boost", "%",
                                        0, 100, vb0,
                                        lambda v: self._apply_vboost(v))
        fan_floor = s.get("fan_min", 30)
        self.sl_fan = self._slider_row(body, 4, "fan", "Fan duty (manual)", "%",
                                       fan_floor, 100, fan_floor,
                                       lambda v: self._apply_fan(v),
                                       extra=("Auto", self._fan_auto))

        # locked clocks row
        lk = tk.Frame(body, bg=BG)
        lk.grid(row=5, column=0, columnspan=4, sticky="w", pady=10)
        tk.Label(lk, text="GPU clock lock", bg=BG, fg=TEXT,
                 font=(FONT, 10, "bold"), width=22, anchor="w").pack(side="left")
        gmin = s.get("gfx_min", 300)
        gmax = s.get("gfx_max", 2160)
        tk.Label(lk, text="min", bg=BG, fg=DIM, font=(FONT, 8)).pack(side="left")
        self.e_lockmin = tk.Entry(lk, width=6, bg=PANEL2, fg=TEXT,
                                  insertbackground=TEXT, relief="flat")
        self.e_lockmin.insert(0, str(gmin))
        self.e_lockmin.pack(side="left", padx=4)
        tk.Label(lk, text="max", bg=BG, fg=DIM, font=(FONT, 8)).pack(side="left")
        self.e_lockmax = tk.Entry(lk, width=6, bg=PANEL2, fg=TEXT,
                                  insertbackground=TEXT, relief="flat")
        self.e_lockmax.insert(0, str(gmax))
        self.e_lockmax.pack(side="left", padx=4)
        b_lock = tk.Button(lk, text="Lock", command=self._apply_lock,
                           bg=PANEL2, fg=TEXT, relief="flat", width=8,
                           activebackground=ACCENT)
        b_lock.pack(side="left", padx=4)
        b_rel = tk.Button(lk, text="Release", command=self._release_lock,
                          bg=PANEL2, fg=TEXT, relief="flat", width=8,
                          activebackground=ACCENT)
        b_rel.pack(side="left", padx=4)
        tk.Label(lk, text=f"({gmin}-{gmax} MHz supported)", bg=BG, fg=DIM,
                 font=(FONT, 8)).pack(side="left", padx=6)
        self._ctl_widgets += [self.e_lockmin, self.e_lockmax, b_lock, b_rel]

        # live clock readout (this tab, so you see effects while tuning)
        ro = tk.Frame(body, bg=PANEL, padx=10, pady=6,
                      highlightbackground=PANEL2, highlightthickness=1)
        ro.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 4))
        self.l_ctl_clocks = tk.Label(ro, text="--", bg=PANEL, fg=TEXT,
                                     font=("Consolas", 10), justify="left",
                                     anchor="w")
        self.l_ctl_clocks.pack(anchor="w", fill="x")
        _bind_autowrap(self.l_ctl_clocks, ro)

        # VF curve panel
        vf = tk.Frame(body, bg=PANEL, padx=10, pady=8,
                      highlightbackground=PANEL2, highlightthickness=1)
        vf.grid(row=7, column=0, columnspan=4, sticky="ew", pady=4)
        hdr = tk.Frame(vf, bg=PANEL)
        hdr.pack(fill="x")
        tk.Label(hdr, text="V/F CURVE", bg=PANEL, fg=ACCENT,
                 font=(FONT, 8, "bold")).pack(side="left")
        tk.Label(hdr, text="voltage cap (mV)", bg=PANEL, fg=DIM,
                 font=(FONT, 8)).pack(side="left", padx=(16, 4))
        self.e_vcap = tk.Entry(hdr, width=6, bg=PANEL2, fg=TEXT,
                               insertbackground=TEXT, relief="flat")
        self.e_vcap.insert(0, "1091")
        self.e_vcap.pack(side="left")
        b_read = tk.Button(hdr, text="Read curve", command=self._vf_read,
                           bg=PANEL2, fg=TEXT, relief="flat",
                           activebackground=ACCENT)
        b_read.pack(side="left", padx=6)
        b_flat = tk.Button(hdr, text="De-flatten ≤ cap", command=self._vf_deflatten,
                           bg="#2a3a2f", fg=GOOD, relief="flat",
                           activebackground=GOOD, activeforeground=BG)
        b_flat.pack(side="left", padx=4)
        b_phase = tk.Button(hdr, text="Re-phase", command=self._vf_rephase,
                            bg=PANEL2, fg=TEXT, relief="flat",
                            activebackground=ACCENT)
        b_phase.pack(side="left", padx=4)
        self._ctl_widgets.append(b_phase)
        b_edit = tk.Button(hdr, text="Edit curve ⤡", command=self._open_vf_editor,
                           bg="#2a3550", fg=ACCENT, relief="flat",
                           activebackground=ACCENT, activeforeground=BG)
        b_edit.pack(side="left", padx=4)
        b_rest = tk.Button(hdr, text="Reset curve to stock", command=self._vf_reset,
                           bg=PANEL2, fg=TEXT, relief="flat",
                           activebackground=ACCENT)
        b_rest.pack(side="left", padx=4)
        self._ctl_widgets.append(b_flat)  # write knob: behind the unlock gate
        # Read is read-only and Restore is a recovery path: both stay ungated,
        # same escape-hatch philosophy as Reset-all.
        self.vf_canvas = tk.Canvas(vf, width=px(760), height=px(170), bg="#0f1114",
                                   highlightthickness=0)
        self.vf_canvas.pack(fill="x", pady=(6, 2))
        self.l_vf = tk.Label(vf, text="curve not read yet", bg=PANEL, fg=DIM,
                             font=(FONT, 8), justify="left", anchor="w")
        self.l_vf.pack(anchor="w", fill="x")
        _bind_autowrap(self.l_vf, vf)
        self._vf_points = None

        # reset + log
        rr = tk.Frame(c, bg=BG, padx=10, pady=6)
        rr.pack(fill="x")
        tk.Button(rr, text="Reset all to stock", command=self._reset_all,
                  bg="#3a2530", fg=BAD, relief="flat", font=(FONT, 10, "bold"),
                  activebackground=BAD, activeforeground=TEXT,
                  padx=14, pady=6).pack(side="left")
        self.log = tk.Text(c, height=7, bg="#0f1114", fg=DIM, relief="flat",
                           font=("Consolas", 9), padx=8, pady=6)
        self.log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self._logln("Controls locked. Tick 'Unlock controls' to enable.")
        self._toggle_lock()

    def _slider_row(self, parent, row, key, label, unit, lo, hi, init, apply_fn,
                    extra=None, resolution=1):
        f = tk.Frame(parent, bg=BG)
        f.grid(row=row, column=0, columnspan=4, sticky="ew", pady=8)
        parent.columnconfigure(0, weight=1)
        tk.Label(f, text=label, bg=BG, fg=TEXT, font=(FONT, 10, "bold"),
                 width=22, anchor="w").pack(side="left")
        var = tk.DoubleVar(value=init)

        def fmt(v):
            return f"{int(v):+d} {unit}" if unit == "MHz" else f"{int(v)} {unit}"

        val_lbl = tk.Label(f, text=fmt(init), bg=BG, fg=ACCENT,
                           font=(FONT, 10, "bold"), width=10)
        sl = tk.Scale(f, from_=lo, to=hi, orient="horizontal", variable=var,
                      resolution=resolution,
                      showvalue=0, length=px(380), bg=BG, fg=TEXT,
                      troughcolor=PANEL2, highlightthickness=0, relief="flat",
                      activebackground=ACCENT,
                      command=lambda v: val_lbl.config(text=fmt(float(v))))
        sl.pack(side="left", padx=8)
        val_lbl.pack(side="left")
        b = tk.Button(f, text="Apply", command=lambda: apply_fn(var.get()),
                      bg=PANEL2, fg=TEXT, relief="flat", width=8,
                      activebackground=ACCENT)
        b.pack(side="left", padx=6)
        self._ctl_widgets += [sl, b]
        if extra:
            eb = tk.Button(f, text=extra[0], command=extra[1], bg=PANEL2,
                           fg=TEXT, relief="flat", width=6,
                           activebackground=ACCENT)
            eb.pack(side="left", padx=2)
            self._ctl_widgets.append(eb)
        self._sliders[key] = {"var": var, "lbl": val_lbl, "fmt": fmt}
        return var

    def _set_slider(self, key, value):
        s = self._sliders.get(key)
        if s:
            s["var"].set(value)
            s["lbl"].config(text=s["fmt"](value))

    def _toggle_lock(self):
        state = "normal" if self.unlocked.get() else "disabled"
        for w in self._ctl_widgets:
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    # ---- info tab --------------------------------------------------------- #
    def _build_info(self):
        s = self.gpu.static
        txt = tk.Text(self.info, bg="#0f1114", fg=TEXT, relief="flat",
                      font=("Consolas", 9), padx=12, pady=10, wrap="word")
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        cr = s.get("core_off_range")
        mr = s.get("mem_off_range")
        content = f"""TitanTune - discovered-knob reference for this card

Device : {s['name']}
Driver : {s['driver']}    VBIOS : {s['vbios']}
Offset ranges (driver-reported):
    core : {cr}
    mem  : {mr}   (NVML units; effective MHz = half)
Power  : {s.get('pl_min_mw','?')}..{s.get('pl_max_mw','?')} mW, default {s.get('pl_def_mw','?')}
Gfx clk: {s.get('gfx_min','?')}-{s.get('gfx_max','?')} MHz

WIRED IN THIS TOOL (reversible)
    - Core / memory clock offset      NVML nvmlDeviceSetClockOffsets
    - Power limit                     NVML nvmlDeviceSetPowerManagementLimit
    - Core voltage boost %            NvAPI ClientVoltRailsSetControl 0xB9306D9B
    - GPU clock min/max lock          NVML Set/ResetGpuLockedClocks   (admin)
    - Fan duty / auto                 NVML SetFanSpeed_v2 / SetDefaultFanSpeed_v2 (admin)
    - V/F curve edit + de-flatten     NvAPI SetClockBoostTable 0x0733E009

TO ACTUALLY BOOST PAST ~1.062 V (two separate knobs, both needed)
    1. Core voltage boost % raises the reliability-voltage CEILING toward the
       ~1.093 V VBIOS hard cap. At 0% the card refuses to exceed ~1.062 V no
       matter how the curve looks. This is AB's "Core Voltage" slider.
    2. De-flatten / edit the V/F curve so it rises strictly below the cap, so
       the boost arbiter (which sits at the LOWEST voltage of any flat segment)
       climbs to the cap voltage instead of parking early.
    Turing headroom is only ~30 mV, so the voltage % is a small absolute bump -
    its value is letting the top curve points become reachable. Effect shows
    under load, not at idle (idle stays ~0.675 V regardless).

TELEMETRY AFTERBURNER DOES NOT SHOW ON TURING (all live on the Monitor tab)
    - Hotspot vs edge delta           NvAPI 0x65FE3AAD ThermalGetSensors
    - GPU vs BOARD power split        NvAPI 0xEDCF624E ClientPowerTopologyGetStatus
    - 9-reason clocks-event mask      NVML GetCurrentClocksEventReasons
    - Insufficient-aux-power canary   NvAPI 0x7F7F4600 GetPerfDecreaseInfo bit 0x10
    - PCIe error counters             NVML field values 173-183
    - Core rail microvolts            NvAPI 0x465F9BCF ClientVoltRailsGetStatus

FOOTGUN KNOBS - NOT wired to a button (run yourself, deliberately)
    Force P-state P0 (pin max clocks; no clean auto-release without driver reload):
        via NvAPI SetForcePstate 0x025BFB10 - use nvidia-pstated, or
    Remove CUDA P2 memory-clock cap (admin):
        nvidia-smi -cc 1        (restore: nvidia-smi -cc 0)
    Driver model TCC - DROPS DISPLAY OUTPUT on this card, do not run blind (admin):
        nvidia-smi -dm 1        (restore: nvidia-smi -dm 0)

    Per-domain HARD VF lock (NvAPI 0x39442CFB) - the crown-jewel XOC knob - is
    read-only here (lock state shown on Monitor). The write struct is unverified
    on this card; test it in isolation before trusting it, then it can be wired.

MEMORY CLOCK - WHICH NUMBER IS SHOWN
    NVAPI reports GDDR at half the data rate (7001 MHz ~= 14 Gbps). GPU-Z and the
    vendors quote the TRUE memory clock. The tiles and the Control readout show
    the true clock, obtained by dividing the reported figure by a per-technology
    factor read from the card (NvAPI_GPU_GetRamType 0x57F7CAAC):
        GDDR5 /2      GDDR5X /4      GDDR6 /4      anything else: shown RAW
    Only positively identified memory types are scaled - an unrecognised id is
    displayed as the raw NVAPI number and labelled as such, so an unfamiliar card
    never gets a silently wrong figure. This card reports id 14 = GDDR6, so
    7254 / 4 = 1813.5 MHz, exactly what GPU-Z shows. Gbps is right either way.
    NOTE the memory OFFSET slider stays in the driver/NVAPI scale, not the true
    clock: +N on the slider moves the reported clock by N and the true clock by
    N/4 here. Measured on this card: slider +253 -> reported 7001 -> 7254, true
    clock 1750.25 -> 1813.5 MHz, data rate 14.00 -> 14.51 Gbps.

CLOCK GRID / QUANTIZATION (measured on this card, matters for curve editing)
    Legal core clocks are EXACTLY multiples of 15 MHz (121 of them, 360..2160;
    from nvmlDeviceGetSupportedGraphicsClocks).
    A VF point evaluates as   floor((base + delta) / 15) * 15
    and the delta is stored verbatim. `base` is NOT readable - the frequency the
    API reports is already floored - so base hides a remainder in [0,15). 101 of
    this card's 103 base frequencies are off-grid by 5 MHz.
    Consequence: a delta computed from an absolute target lands mid-bin and the
    hardware silently floors it (ask for 2150 -> get 2145), which collides with
    the point below and RE-CREATES the flat you were removing. This tool only
    ever changes a delta by whole 15 MHz bins, which moves the real clock by
    exactly that much (verified 5/5 against hardware).

VOLTAGE GRID vs THE CAP FIELD
    VF points are spaced 6.25 mV. With cap = 1091 mV the highest point at or
    below it is idx 89 @ 1087.50 mV - so the card lands at ~1.087 V, which is
    correct, not a failure. The next point up is 1093.75 mV: to try for it, set
    the cap field to 1094. Whether the card actually holds there depends on the
    real VBIOS/driver lock (~1.09x V) - if it refuses, 1087.50 mV is the ceiling.

V/F CURVE + DE-FLATTEN (Control tab)
    Turing's boost arbiter runs the LOWEST voltage of any peak-frequency flat, so
    when the top clock is held by many voltage points the card parks at the
    cheapest (lowest) one. "De-flatten <= cap" makes ONE point - the first VF
    point past the cap - the UNIQUE top, so the card parks there at the highest
    voltage/clock the cap allows, then levels everything above it onto that clock
    (a flat top whose lowest member is that point). It targets one VF point above
    the cap on purpose: the reliability lock can sit a notch above the nominal
    cap, and that point is exactly the one a below-cap-only pass leaves tied.
    IMPORTANT: points BELOW the cap are left untouched - the low-voltage floor is
    dozens of points pinned at the minimum clock, and ramping them up would make
    the card demand high clocks at tiny voltages (instant instability). So
    de-flatten only removes the top tie (about +1 bin); the OVERALL ceiling is
    raised by the core-offset slider, not by de-flatten.
        read : ClkVfPointsGetStatus 0x21537AD4 + boost table 0x23F1B133
        write: SetClockBoostTable 0x0733E009 (the same table AB's editor writes)
    "Reset curve to stock" zeroes every delta - stock Turing deltas are 0, so
    that IS the factory curve and needs no saved baseline. A reboot also clears.
    De-flatten only ever raises a point to prev+15 MHz, so its total gain is
    (length of the flat run below the cap) x 15 MHz. It is a de-flattener, not a
    maximizer: broad headroom still comes from the core offset.

PHASE - why a 1 MHz offset can undo a de-flatten
    Because the clock is floor((base+delta)/15)*15, two points only move together
    when their deltas share a remainder mod 15 MHz. A non-multiple-of-15 offset
    pushes some points across a bin boundary and not others, re-creating a flat.
    So: the core-offset slider is quantized to 15 MHz here, and "Re-phase" puts
    every delta back on one common 15 MHz phase (rounding DOWN, never up) if an
    older edit or another tool left points off-grid.

NOTE: the slider offsets and the curve are the SAME underlying delta table on
this driver (a slider offset shifts every point uniformly; de-flatten edits
points individually). Afterburner writes this table too - applying an AB
profile will clobber curve edits made here. Drive clocks from ONE tool at a time.
"""
        txt.insert("1.0", content)
        txt.config(state="disabled")

    # ---- write handlers --------------------------------------------------- #
    def _logln(self, msg):
        try:
            if not self.log.winfo_exists():
                return
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.config(state="disabled")
        except tk.TclError:
            pass

    def _report(self, res):
        ok, msg = res
        self._logln(("[ok] " if ok else "[!!] ") + msg)

    def _apply_core(self, v):
        self._report(self.gpu.set_clock_offset(0, int(v)))

    def _apply_mem(self, v):
        self._report(self.gpu.set_clock_offset(2, int(v)))  # backend takes effective MHz

    def _apply_pl(self, v):
        self._report(self.gpu.set_power_limit_mw(int(v) * 1000))

    def _apply_vboost(self, v):
        self._report(self.gpu.set_voltage_boost(int(v)))

    def _apply_fan(self, v):
        self._report(self.gpu.set_fan(int(v)))

    def _fan_auto(self):
        self._report(self.gpu.reset_fan())

    def _apply_lock(self):
        try:
            mn = int(self.e_lockmin.get())
            mx = int(self.e_lockmax.get())
        except ValueError:
            self._logln("[!!] lock: min/max must be integers")
            return
        self._report(self.gpu.lock_gpu_clocks(mn, mx))

    def _release_lock(self):
        self._report(self.gpu.reset_gpu_clocks())

    # ---- VF curve handlers ------------------------------------------------ #
    def _vcap(self):
        try:
            return float(self.e_vcap.get())
        except ValueError:
            self._logln("[!!] voltage cap must be a number (mV)")
            return None

    def _vf_read(self):
        pts, err = self.gpu.read_vf_curve()
        if err:
            self._logln("[!!] " + err)
            return None
        self._vf_points = pts
        vcap = self._vcap() or 1091
        flats = self._count_flats(pts, vcap)
        under = [p for p in pts if below_cap(p["volt_mv"], vcap)]
        top = under[-1] if under else None
        peak, pidx, pmv, npk = GPU.peak_info(pts)
        toptxt = (f"top ≤cap: idx {top['idx']} @ {top['volt_mv']:.2f} mV = "
                  f"{top['freq_mhz']:.0f} MHz  •  " if top else "")
        self.l_vf.config(
            text=f"{len(pts)} points  •  {toptxt}peak {peak:.0f} MHz held by "
                 f"{npk} point(s), lowest = idx {pidx} @ {pmv:.2f} mV "
                 f"(where the card parks)  •  flat runs below cap: {flats}")
        self._draw_curve(pts, None, vcap)
        self._logln(f"[ok] curve read: peak {peak:.0f} MHz held by {npk} point(s), "
                    f"card parks at idx {pidx} @ {pmv:.2f} mV; "
                    f"{flats} flat run(s) below {vcap:.0f} mV")
        return pts

    @staticmethod
    def _count_flats(pts, vcap):
        flats, run = 0, 1
        for a, b in zip(pts, pts[1:]):
            if below_cap(b["volt_mv"], vcap) and b["freq_mhz"] <= a["freq_mhz"]:
                run += 1
            else:
                flats, run = flats + (1 if run > 1 else 0), 1
        return flats + (1 if run > 1 else 0)

    def _vf_deflatten(self):
        vcap = self._vcap()
        if vcap is None:
            return
        pts = self._vf_read()
        if not pts:
            return
        gmax = self.gpu.static.get("gfx_max")
        changes, before, after, meta = self.gpu.compute_deflatten(
            pts, vcap, max_khz=(gmax * 1000 if gmax else None))
        bidx = meta.get("boundary_idx")
        if not changes:
            self._logln(f"[ok] idx {bidx} is already the unique top point at "
                        f"≤{vcap:.0f} mV (+1) - nothing to do")
            return
        self._draw_curve(pts, changes, vcap)
        note = ""
        if meta.get("clamped"):
            note = f"\n(top clamped at the {gmax} MHz max supported clock.)"
        if not meta.get("unique", True):
            note += ("\n(a point below is already at the max clock - the card "
                     "can't be lifted higher by de-flatten alone.)")
        if not messagebox.askyesno(
                "De-flatten V/F curve",
                f"Make idx {bidx} (one VF point past {vcap:.0f} mV) the UNIQUE "
                f"top point, so the arbiter parks there at the highest voltage "
                f"the cap allows, and level everything above it onto that clock. "
                f"Points below the cap are left untouched.\n\n"
                f"Top clock: {before:.0f} → {after:.0f} MHz "
                f"({after-before:+.0f})   ({len(changes)} points changed){note}\n\n"
                f"de-flatten removes the top tie (~1 bin); the core offset is what "
                f"raises the overall ceiling.\n"
                f"Reversible via \"Reset curve to stock\" or a reboot.\n"
                f"Apply?"):
            self._logln("[--] de-flatten cancelled")
            return
        new_deltas = {c[0]: c[4] for c in changes}
        ok, m = self.gpu.apply_vf_deltas(new_deltas)
        self._report((ok, m))
        if ok:
            self._vf_read()
            self._logln(f"[ok] de-flatten applied: idx {bidx} now unique top at "
                        f"{after:.0f} MHz")

    def _vf_rephase(self):
        ok, m = self.gpu.rephase_deltas()
        self._report((ok, m))
        if ok:
            self._vf_read()

    def _vf_reset(self):
        ok, m = self.gpu.reset_vf_curve()
        self._report((ok, m))
        if ok:
            self._vf_read()

    def _open_vf_editor(self):
        if getattr(self, "_editor", None) and self._editor.winfo_exists():
            self._editor.lift()
            return
        self._editor = VFEditor(self, self.gpu)

    def _draw_curve(self, pts, changes, vcap):
        changes = changes or []
        cv = self.vf_canvas
        cv.delete("all")
        w = int(cv.winfo_width()) or 760
        h = int(cv.winfo_height()) or 170
        pad = 34
        vmin = min(p["volt_mv"] for p in pts) - 10
        vmax = max(p["volt_mv"] for p in pts) + 10
        fmin = min(p["freq_mhz"] for p in pts) - 30
        fmax = max(max(p["freq_mhz"] for p in pts),
                   max((c[3] for c in changes), default=0)) + 30

        def xy(v, f):
            x = pad + (v - vmin) / (vmax - vmin) * (w - pad - 8)
            y = h - 18 - (f - fmin) / (fmax - fmin) * (h - 30)
            return x, y

        # axes labels
        for f in range(int(fmin // 300 + 1) * 300, int(fmax), 300):
            _, y = xy(vmin, f)
            cv.create_line(pad, y, w - 8, y, fill="#1c2027")
            cv.create_text(4, y, text=str(f), anchor="w", fill=DIM,
                           font=(FONT, 7))
        for v in range(600, int(vmax), 100):
            x, _ = xy(v, fmin)
            cv.create_text(x, h - 8, text=str(v), fill=DIM, font=(FONT, 7))
        # cap line
        xc, _ = xy(vcap, fmin)
        cv.create_line(xc, 6, xc, h - 16, fill=BAD, dash=(3, 3))
        cv.create_text(xc + 3, 10, text=f"{vcap:.0f} mV cap", anchor="w",
                       fill=BAD, font=(FONT, 7))
        # current curve
        coords = []
        for p in pts:
            coords += xy(p["volt_mv"], p["freq_mhz"])
        cv.create_line(*coords, fill=ACCENT, width=2)
        # proposed curve
        if changes:
            newf = {c[0]: c[3] for c in changes}
            coords = []
            for p in pts:
                coords += xy(p["volt_mv"], newf.get(p["idx"], p["freq_mhz"]))
            cv.create_line(*coords, fill=GOOD, width=1, dash=(4, 2))

    def _reset_all(self):
        if not messagebox.askyesno("Reset all",
                                    "Zero clock offsets, restore default power "
                                    "limit, release clock lock, return fans to "
                                    "auto?"):
            return
        failed = 0
        for ok, m in self.gpu.reset_all():
            self._logln(("[reset] " if ok else "[!!] ") + m)
            if not ok:
                failed += 1
        # resync the controls to the values we just tried to apply
        self._set_slider("core", 0)
        self._set_slider("mem", 0)
        s = self.gpu.static
        self._set_slider("pl", s.get("pl_def_mw", 260000) // 1000)
        vb = self.gpu.read_voltage_boost()   # resync to hardware, not intent
        self._set_slider("volt", 0 if vb is None else max(0, min(100, vb)))
        self._set_slider("fan", s.get("fan_min", 30))
        if failed:
            self._logln(f"[!!] reset incomplete: {failed} step(s) failed "
                        f"(admin required?)")
        else:
            self._logln("[ok] reset to stock complete")

    # ---- live update ------------------------------------------------------ #
    def _poll_loop(self):
        """Background: the only place that touches the driver for telemetry."""
        while not self._stop_evt.is_set():
            try:
                d = self.gpu.read()
                with self._snap_lock:
                    self._snap, self._snap_err = d, None
                    self._snap_seq += 1
            except Exception as e:
                with self._snap_lock:
                    self._snap_err = str(e)
            self._stop_evt.wait(1.0)

    def _tick(self):
        if self._moving():          # a drag/resize is in flight - do nothing
            self._after_id = self.after(120, self._tick)
            return
        with self._snap_lock:
            d, err, seq = self._snap, self._snap_err, self._snap_seq
        if seq == self._drawn_seq:
            # nothing new since the last repaint - don't touch a single widget
            self._after_id = self.after(250, self._tick)
            return
        self._drawn_seq = seq
        if err:
            self._log_once("read", f"read error: {err}")
        else:
            self._clear_once("read")
        if d is not None:
            for name, fn in (("tiles", self._rf_tiles),
                             ("throttle", self._rf_throttle),
                             ("bars", self._rf_bars),
                             ("pcie", self._rf_pcie),
                             ("state", self._rf_state),
                             ("ctlclocks", self._rf_ctl_clocks)):
                try:
                    fn(d)
                    self._clear_once(name)
                except Exception as e:
                    self._log_once(name, f"{name} panel: {e}")
        self._after_id = self.after(250, self._tick)

    def _rf_tiles(self, d):
        m = self.monitor
        m.set("t_core", d.get("core", "--"))
        m.set("s_core", f"P{d.get('pstate','?')}")
        mtxt, msub = self._mem_fmt(d.get("mem"))
        m.set("t_mem", mtxt)
        m.set("s_mem", msub)
        m.set("t_edge", d.get("temp_edge", "--"))
        hot = d.get("temp_hotspot")
        if hot is not None:
            delta = d.get("temp_delta", 0)
            col = BAD if hot >= 90 else WARN if hot >= 80 else GOOD
            m.set("t_hot", (f"{hot:.0f}", col))
            m.set("s_hot", f"Δ {delta:.0f} °C over edge")
        pw = d.get("power_w")
        m.set("t_pwr", f"{pw:.0f}" if pw is not None else "--")
        m.set("s_pwr", f"limit {d.get('pl_now_mw',0)//1000} W")
        vc = d.get("vcore_mv")
        m.set("t_vcore", f"{vc:.0f}" if vc is not None else "--")

    def _rf_throttle(self, d):
        em = d.get("event_mask", 0)
        for bit, name in EVENT_REASONS:
            self.monitor.set(f"lamp_{name}",
                             (bool(em & bit), GOOD if name == "Idle" else BAD))
        pdv = d.get("perf_decrease", 0)
        for bit, name in PERF_DECREASE_BITS:
            self.monitor.set(f"pd_{name}", (bool(pdv & bit), BAD))

    def _rf_bars(self, d):
        m = self.monitor
        for key, val in (("gpu", d.get("pwr_gpu_pct", 0)),
                         ("board", d.get("pwr_board_pct", 0)),
                         ("ugpu", d.get("util_gpu", 0)),
                         ("ufb", d.get("util_fb", 0)),
                         ("uvid", d.get("util_vid", 0)),
                         ("ubus", d.get("util_bus", 0))):
            m.set(f"bar_{key}", val)
            m.set(f"bart_{key}", f"{val:.0f}%")
        tgt = d.get("pl_target_pct", 0)
        m.set("bar_target", tgt)
        m.set("bart_target", f"{tgt:.0f}%")   # true value even if >100

    def _rf_pcie(self, d):
        errt = d.get("pcie_err_total", 0)
        text = (f"Gen {d.get('pcie_gen','?')}  x{d.get('pcie_width','?')}\n"
                f"errors: {errt}   (since launch: {d.get('pcie_err_since',0)})")
        if errt:
            nz = {k: v for k, v in d.get("pcie_err", {}).items() if v}
            text += "\n" + "  ".join(f"{k}:{v}" for k, v in nz.items())
        self.monitor.set("pcie", (text, GOOD if errt == 0 else BAD))

    def _rf_ctl_clocks(self, d):
        core = d.get("core", "?")
        mem = self._mem_fmt(d.get("mem"))[0]
        c_tgt = d.get("core_p0max", "?")
        m_tgt = self._mem_fmt(d.get("mem_p0max"))[0]
        vc = d.get("vcore_mv")
        vtxt = f"{vc:.0f} mV" if vc is not None else "--"
        vb = d.get("vboost_pct")
        vbtxt = f"{vb}%" if vb is not None else "--"
        self.l_ctl_clocks.config(
            text=f"core {core} MHz  (P0 target max {c_tgt} MHz)      "
                 f"VRAM {mem} MHz  (P0 target max {m_tgt} MHz)\n"
                 f"Vcore {vtxt}   volt-boost {vbtxt}   P{d.get('pstate','?')}")

    def _rf_state(self, d):
        fans = d.get("fans", [])
        fantxt = "  ".join(f"fan{i}: {duty}% {rpm or 0}rpm"
                           for i, (duty, rpm) in enumerate(fans)) or "--"
        vfl = d.get("vf_locked_domains", [])
        core_off = d.get("core_off", 0)
        mem_off = d.get("mem_off", 0)
        mscale, munit = self.gpu.mem_offset_scale()
        mem_disp = int(mem_off / mscale) if isinstance(mem_off, int) else 0
        self.monitor.set("state",
                         (f"energy: {d.get('energy_j',0):.0f} J\n{fantxt}\n"
                          f"offsets: core {core_off:+d} MHz  "
                          f"mem {mem_disp:+d} {munit}\n"
                          f"VF-locked: {vfl or 'none'}", TEXT))

    def _log_once(self, key, msg):
        if self._once.get(key) != msg:
            self._once[key] = msg
            self._logln("[!!] " + msg)

    def _clear_once(self, key):
        self._once.pop(key, None)

    def _on_close(self):
        self._stop_evt.set()
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self.destroy()


class VFEditor(tk.Toplevel):
    """Interactive per-point V/F curve editor. Edits a WORKING copy of the delta
    table (so you compose edits and see the resulting curve before committing),
    which sidesteps the re-flatten-on-uniform-offset problem: every point is
    moved individually and the driver's 15 MHz quantization is shown live."""
    STEP_KHZ = VF_STEP_KHZ
    STEP = VF_STEP_KHZ // 1000  # MHz per bin

    def __init__(self, app, gpu):
        super().__init__(app)
        self.app = app
        self.gpu = gpu
        self.title("V/F Curve Editor")
        self.configure(bg=BG)
        self.geometry(f"{px(960)}x{px(600)}")
        self.minsize(px(820), px(520))

        pts, err = gpu.read_vf_curve()
        if err:
            messagebox.showerror("V/F Curve Editor", err)
            self.after(10, self.destroy)
            return
        self.idxs = [p["idx"] for p in pts]
        self.volt = {p["idx"]: p["volt_mv"] for p in pts}
        self.orig_delta = {p["idx"]: p["delta_khz"] for p in pts}
        self.orig_fkhz = {p["idx"]: int(round(p["freq_mhz"] * 1000))
                          for p in pts}
        self.work_delta = dict(self.orig_delta)
        self.sel = self.idxs[len(self.idxs) // 2]
        self._drag = False
        self._drag_b = None
        # sane defaults so pre-first-draw input can't AttributeError
        self.W, self.H, self.PAD = 940, 380, 42
        self._b = self._bounds()

        self._build()
        if USE_COMPOSITED:
            enable_double_buffer(self)
        self.bind("<Left>", lambda e: self._kbd(self._select_step, -1))
        self.bind("<Right>", lambda e: self._kbd(self._select_step, 1))
        self.bind("<Up>", lambda e: self._kbd(self._nudge, self.STEP))
        self.bind("<Down>", lambda e: self._kbd(self._nudge, -self.STEP))
        self.after(60, self._draw)

    def _kbd(self, fn, arg):
        # ignore arrow keys while a text field has focus (they'd edit the caret
        # AND move a curve point otherwise)
        if isinstance(self.focus_get(), tk.Entry):
            return
        fn(arg)

    # ---- geometry --------------------------------------------------------- #
    def wfkhz(self, idx):
        return self.orig_fkhz[idx] + (self.work_delta[idx] - self.orig_delta[idx])

    def _bounds(self):
        vs = [self.volt[i] for i in self.idxs]
        fs = [self.wfkhz(i) / 1000 for i in self.idxs] + \
             [self.orig_fkhz[i] / 1000 for i in self.idxs]
        return min(vs) - 8, max(vs) + 8, min(fs) - 40, max(fs) + 40

    def _xy(self, v, f):
        vlo, vhi, flo, fhi = self._b
        x = self.PAD + (v - vlo) / (vhi - vlo) * (self.W - self.PAD - 12)
        y = self.H - 26 - (f - flo) / (fhi - flo) * (self.H - 42)
        return x, y

    def _y_to_freq(self, y):
        vlo, vhi, flo, fhi = self._b
        f = flo + (self.H - 26 - y) / (self.H - 42) * (fhi - flo)
        return f

    # ---- build ------------------------------------------------------------ #
    def _build(self):
        top = tk.Frame(self, bg=BG, padx=10, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="V/F CURVE EDITOR", bg=BG, fg=ACCENT,
                 font=(FONT, 10, "bold")).pack(side="left")
        tk.Label(top, text="click selects • drag to move • ←→ select • ↑↓ nudge 15 MHz",
                 bg=BG, fg=DIM, font=(FONT, 8)).pack(side="left", padx=12)
        tk.Label(top, text="cap (mV)", bg=BG, fg=DIM,
                 font=(FONT, 8)).pack(side="left", padx=(10, 2))
        self.e_cap = tk.Entry(top, width=6, bg=PANEL2, fg=TEXT,
                              insertbackground=TEXT, relief="flat")
        self.e_cap.insert(0, "1091")
        self.e_cap.pack(side="left")
        self.e_cap.bind("<Return>", lambda e: self._draw())

        self.cv = tk.Canvas(self, bg="#0f1114", highlightthickness=0)
        self.cv.pack(fill="both", expand=True, padx=10, pady=6)
        self._cv_size = (0, 0)
        self.cv.bind("<Configure>", self._on_cv_configure)
        self.cv.bind("<Button-1>", self._on_click)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_release)

        info = tk.Frame(self, bg=BG, padx=10, pady=4)
        info.pack(fill="x")
        self.l_sel = tk.Label(info, text="--", bg=BG, fg=TEXT,
                              font=("Consolas", 9), justify="left", anchor="w")
        self.l_sel.pack(side="left", fill="x", expand=True)
        _bind_autowrap(self.l_sel, info)

        ctl = tk.Frame(self, bg=BG, padx=10, pady=8)
        ctl.pack(fill="x")
        for txt, dv in (("-75", -75), ("-15", -15), ("+15", 15), ("+75", 75)):
            tk.Button(ctl, text=txt, width=4, command=lambda d=dv: self._nudge(d),
                      bg=PANEL2, fg=TEXT, relief="flat",
                      activebackground=ACCENT).pack(side="left", padx=2)
        tk.Label(ctl, text="set MHz", bg=BG, fg=DIM,
                 font=(FONT, 8)).pack(side="left", padx=(12, 2))
        self.e_set = tk.Entry(ctl, width=7, bg=PANEL2, fg=TEXT,
                              insertbackground=TEXT, relief="flat")
        self.e_set.pack(side="left")
        tk.Button(ctl, text="Set", width=5, command=self._set_freq, bg=PANEL2,
                  fg=TEXT, relief="flat", activebackground=ACCENT).pack(
                      side="left", padx=4)
        tk.Button(ctl, text="Monotonic ≤ cap", command=self._monotonic,
                  bg="#2a3a2f", fg=GOOD, relief="flat", activebackground=GOOD,
                  activeforeground=BG).pack(side="left", padx=(16, 4))
        tk.Button(ctl, text="Revert edits", command=self._revert, bg=PANEL2,
                  fg=TEXT, relief="flat", activebackground=ACCENT).pack(
                      side="left", padx=4)
        tk.Button(ctl, text="Apply to GPU", command=self._apply, bg="#2a3550",
                  fg=ACCENT, relief="flat", font=(FONT, 9, "bold"),
                  activebackground=ACCENT, activeforeground=BG).pack(
                      side="right", padx=4)

    # ---- cap helper ------------------------------------------------------- #
    def _cap(self):
        try:
            return float(self.e_cap.get())
        except ValueError:
            return 1091.0

    # ---- interaction ------------------------------------------------------ #
    def _nearest(self, x):
        best, bd = self.sel, 1e9
        for i in self.idxs:
            px, _ = self._xy(self.volt[i], self.wfkhz(i) / 1000)
            if abs(px - x) < bd:
                bd, best = abs(px - x), i
        return best

    def _on_click(self, e):
        # click SELECTS only (no move); freeze the axis so a subsequent drag
        # doesn't rubber-band the scale under the cursor
        self.sel = self._nearest(e.x)
        self._drag = True
        self._drag_b = self._b
        self._draw()

    def _on_drag(self, e):
        if self._drag:
            self._set_freq_from_y(e.y)

    def _on_release(self, e):
        self._drag = False
        self._drag_b = None
        self._draw()

    def _on_cv_configure(self, event):
        self.app._note_configure()
        # redraw only when the canvas actually changed SIZE (a window move fires
        # <Configure> too, and redrawing 103 points on every move-tick makes the
        # window drag lag)
        size = (event.width, event.height)
        if size != self._cv_size:
            self._cv_size = size
            self._draw()

    def _set_freq_from_y(self, y):
        f = self._y_to_freq(y)
        self._set_work_freq(self.sel, f * 1000)
        self._draw()

    def _select_step(self, d):
        i = self.idxs.index(self.sel)
        self.sel = self.idxs[max(0, min(len(self.idxs) - 1, i + d))]
        self._draw()

    def _nudge(self, mhz):
        self._set_work_freq(self.sel, self.wfkhz(self.sel) + mhz * 1000)
        self._draw()

    def _set_freq(self):
        try:
            want = float(self.e_set.get()) * 1000
        except ValueError:
            return
        idx = self.sel
        step = self.STEP_KHZ
        # floor toward the request: never grant unrequested clock
        bins = int(math.floor((want - self.orig_fkhz[idx]) / step))
        self._set_work_freq(idx, self.orig_fkhz[idx] + bins * step)
        self._draw()
        landed = self.wfkhz(idx) / 1000
        self.app._logln(f"[--] idx {idx}: asked {want/1000:.0f} MHz -> "
                        f"landed {landed:.0f} MHz (15 MHz grid)")

    def _set_work_freq(self, idx, target_khz):
        # The driver evaluates floor((base + delta)/15MHz)*15MHz, and `base` has
        # an unknowable sub-15 remainder (the reported freq is already floored).
        # So never derive a delta from an absolute target: only ever change the
        # delta by WHOLE 15 MHz bins, which moves the real clock by exactly that
        # much. Anything else lands mid-bin and the hardware silently floors it.
        step = self.STEP_KHZ
        d0 = self.orig_delta[idx]
        lim = GPU.MAX_ABS_DELTA_KHZ
        bins = int(math.floor((target_khz - self.orig_fkhz[idx]) / step + 0.5))
        if bins > 0:
            bins = min(bins, (lim - d0) // step)
        elif bins < 0:
            bins = max(bins, -((lim + d0) // step))
        self.work_delta[idx] = int(d0 + bins * step)

    def _monotonic(self):
        # same rule as the backend on the working copy: make the boundary point
        # the unique top, level above it, leave the floor alone
        pts = [{"idx": i, "volt_mv": self.volt[i],
                "freq_mhz": self.wfkhz(i) / 1000,
                "delta_khz": self.work_delta[i]} for i in self.idxs]
        gmax = self.gpu.static.get("gfx_max")
        changes, before, after, meta = GPU.compute_deflatten(
            pts, self._cap(), max_khz=(gmax * 1000 if gmax else None))
        for idx, _v, _o, _n, new_delta in changes:
            self.work_delta[idx] = int(new_delta)
        self._draw()
        extra = " (clamped at max)" if meta.get("clamped") else ""
        self.app._logln(f"[--] monotonic: idx {meta.get('boundary_idx')} -> "
                        f"unique top {after:.0f} MHz, {len(changes)} points "
                        f"changed{extra}")

    def _revert(self):
        self.work_delta = dict(self.orig_delta)
        self._draw()

    def _apply(self):
        if not self.app.unlocked.get():
            messagebox.showwarning("Locked", "Tick 'Unlock controls' on the main "
                                   "window before applying.")
            return
        changed = {i: self.work_delta[i] for i in self.idxs
                   if self.work_delta[i] != self.orig_delta[i]}
        if not changed:
            messagebox.showinfo("V/F Editor", "No edits to apply.")
            return
        cap = self._cap()
        ceil = max((self.wfkhz(i) / 1000 for i in self.idxs
                    if below_cap(self.volt[i], cap)), default=0)
        if not messagebox.askyesno(
                "Apply V/F curve",
                f"Write {len(changed)} edited points to the GPU?\n"
                f"Ceiling at <= {cap:.0f} mV becomes {ceil:.0f} MHz.\n\n"
                f"Reversible via \"Reset curve to stock\" or a reboot."):
            return
        predicted = {i: self.wfkhz(i) for i in changed}
        ok, m = self.gpu.apply_vf_deltas(changed)
        self.app._report((ok, m))
        if ok:
            # re-read so the editor baseline matches hardware
            pts, err = self.gpu.read_vf_curve()
            if not err:
                actual = {p["idx"]: int(round(p["freq_mhz"] * 1000)) for p in pts}
                bad = {i: (predicted[i], actual[i]) for i in predicted
                       if i in actual and actual[i] != predicted[i]}
                if bad:
                    i0 = next(iter(bad)); pv, av = bad[i0]
                    self.app._logln(
                        f"[!!] {len(bad)}/{len(predicted)} points landed off "
                        f"prediction (idx {i0}: predicted {pv/1000:.0f}, "
                        f"hardware {av/1000:.0f} MHz) - clamped, or another "
                        f"tool is writing this table")
                self.orig_delta = {p["idx"]: p["delta_khz"] for p in pts}
                self.orig_fkhz = actual
                self.work_delta = dict(self.orig_delta)
            else:
                # re-read failed: advance BOTH baselines together, or the
                # frequency anchor goes stale while the delta baseline moves
                self.app._logln("[!!] post-write curve re-read failed: "
                                f"{err} - displayed values are predicted")
                new_f = {i: self.wfkhz(i) for i in self.idxs}
                self.orig_fkhz = new_f
                self.orig_delta = dict(self.work_delta)
            self._draw()
            try:                                    # refresh the main VF panel too
                if self.app.winfo_exists():
                    self.app._vf_read()
            except tk.TclError:
                pass

    # ---- draw ------------------------------------------------------------- #
    def _draw(self):
        cv = self.cv
        self.W = int(cv.winfo_width()) or 940
        self.H = int(cv.winfo_height()) or 380
        self.PAD = 42
        # hold the axis fixed while dragging so the point tracks the cursor
        self._b = self._drag_b if (self._drag and self._drag_b) else self._bounds()
        cv.delete("all")
        vlo, vhi, flo, fhi = self._b
        cap = self._cap()

        for f in range(int(flo // 150 + 1) * 150, int(fhi), 150):
            _, y = self._xy(vlo, f)
            cv.create_line(self.PAD, y, self.W - 12, y, fill="#191d24")
            cv.create_text(6, y, text=str(f), anchor="w", fill=DIM,
                           font=(FONT, 7))
        for v in range(int(vlo // 50 + 1) * 50, int(vhi), 50):
            x, _ = self._xy(v, flo)
            cv.create_line(x, 8, x, self.H - 22, fill="#141821")
            cv.create_text(x, self.H - 10, text=str(v), fill=DIM,
                           font=(FONT, 7))
        xc, _ = self._xy(cap, flo)
        cv.create_line(xc, 8, xc, self.H - 22, fill=BAD, dash=(3, 3))
        cv.create_text(xc + 3, 16, text=f"{cap:.0f} mV cap", anchor="w",
                       fill=BAD, font=(FONT, 7))

        # original (dim) then working (bright)
        oc, wc = [], []
        for i in self.idxs:
            oc += self._xy(self.volt[i], self.orig_fkhz[i] / 1000)
            wc += self._xy(self.volt[i], self.wfkhz(i) / 1000)
        cv.create_line(*oc, fill="#33507a", width=1)
        cv.create_line(*wc, fill=ACCENT, width=2)
        for i in self.idxs:
            x, y = self._xy(self.volt[i], self.wfkhz(i) / 1000)
            r = 4 if i == self.sel else 2
            col = WARN if i == self.sel else (
                GOOD if self.work_delta[i] != self.orig_delta[i] else ACCENT)
            cv.create_oval(x - r, y - r, x + r, y + r, fill=col, outline="")

        under = [i for i in self.idxs if below_cap(self.volt[i], cap)]
        ceil = max((self.wfkhz(i) / 1000 for i in under), default=0)
        wpts = [{"idx": i, "volt_mv": self.volt[i],
                 "freq_mhz": self.wfkhz(i) / 1000} for i in self.idxs]
        peak, pidx, pmv, npk = GPU.peak_info(wpts)
        nedit = sum(1 for i in self.idxs
                    if self.work_delta[i] != self.orig_delta[i])
        s = self.sel
        top = max(under) if under else None
        toptxt = (f"top point ≤cap: idx {top} @ {self.volt[top]:.2f} mV "
                  f"= {self.wfkhz(top)/1000:.0f} MHz" if top is not None else "")
        self.l_sel.config(
            text=f"selected idx {s}:  {self.volt[s]:.2f} mV   "
                 f"{self.wfkhz(s)/1000:.0f} MHz   "
                 f"delta {self.work_delta[s]/1000:+.0f} MHz    |    "
                 f"{toptxt}   peak {peak:.0f} MHz x{npk}, parks idx {pidx} @ "
                 f"{pmv:.2f} mV   edits pending: {nedit}")


def main():
    global SCALE
    # --no-dpi reverts to the pre-DPI behaviour (blurry on a scaled display, but
    # a known-good baseline) so a drag/perf regression can be A/B'd instantly.
    import sys as _sys
    global NO_POLL, USE_COMPOSITED
    NO_POLL = "--no-poll" in _sys.argv
    # WS_EX_COMPOSITED double-buffering deadlocks Tk's repaint on this stack -
    # update() never returns - so it is strictly opt-in for experiments.
    USE_COMPOSITED = "--composited" in _sys.argv
    if "--no-dpi" not in _sys.argv:
        _enable_dpi_awareness()      # must precede any Tk window
        SCALE = _detect_scale()
    else:
        SCALE = 1.0
    gpu = GPU()
    if not gpu.available():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("TitanTune",
                             "No GPU backend available.\n" + gpu.status_line())
        return
    App(gpu).mainloop()


if __name__ == "__main__":
    main()
