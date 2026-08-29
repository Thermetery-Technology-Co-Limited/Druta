"""
Drag probe - isolates WHY a Tk window drags slowly on this machine.

Opens several windows, each in its OWN process so they can't affect each other.
Drag each by its title bar for ~3 seconds and compare the on-window readout.

Each window measures its own drag smoothness objectively: Windows delivers a
<Configure> event per move step, so during a smooth drag you should see a high
events/sec and a small max-gap. A laggy drag shows few events/sec and large gaps.

    moves/sec  : how many move events the window actually processed
    max gap    : longest stall between two move events (ms) - the lag you feel

Modes (what each isolates):
    bare    - empty window, DPI-unaware, 1000x700   -> is plain Tk slow here?
    sized   - empty window, DPI-aware,  1500x1050   -> does size/DPI matter?
    widgets - TitanTune's widget tree, NO polling   -> is it the widget count?
    canvas  - a 103-point canvas like the VF editor -> is it canvas drawing?

Run:  python drag_probe.py          (launches all four)
      python drag_probe.py bare     (just one)
"""
import ctypes
import subprocess
import sys
import time
import tkinter as tk

MODES = ("bare", "sized", "widgets", "canvas")
BG = "#16181d"
PANEL = "#1e2128"
TEXT = "#e6e8ec"
DIM = "#8b9099"
GOOD = "#46d17a"
WARN = "#ffcb47"
BAD = "#ff5c5c"


def dpi_aware():
    for fn in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
               lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            fn()
            return
        except Exception:
            continue


class Probe(tk.Tk):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.title(f"drag probe: {mode}")
        self.configure(bg=BG)

        scale = 1.0
        if mode == "sized":
            scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
            self.tk.call("tk", "scaling", scale * 96.0 / 72.0)
        w, h = (int(1000 * scale), int(700 * scale))
        pos = {"bare": "+40+40", "sized": "+120+120",
               "widgets": "+200+200", "canvas": "+280+280"}[mode]
        self.geometry(f"{w}x{h}{pos}")

        # --- the readout (kept deliberately tiny so it costs nothing) ---
        self.lbl = tk.Label(self, text="drag me by the title bar...",
                            bg=BG, fg=TEXT, font=("Segoe UI", 14, "bold"),
                            justify="left")
        self.lbl.pack(anchor="nw", padx=16, pady=12)
        self.sub = tk.Label(self, text=f"mode: {mode}", bg=BG, fg=DIM,
                            font=("Segoe UI", 9))
        self.sub.pack(anchor="nw", padx=16)

        if mode == "widgets":
            self._build_widgets()
        elif mode == "canvas":
            self._build_canvas()

        self._times = []
        self._last_report = 0.0
        self.bind("<Configure>", self._on_cfg)
        self.after(200, self._report)

    # ---- payloads ---- #
    def _build_widgets(self):
        """Approximate TitanTune's tree: tiles + lamps + bars + labels."""
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=6, pady=6)
        for i in range(6):                      # 6 stat tiles
            f = tk.Frame(top, bg=PANEL, padx=12, pady=8,
                         highlightbackground="#252932", highlightthickness=1)
            f.grid(row=0, column=i, sticky="nsew", padx=4)
            top.columnconfigure(i, weight=1)
            tk.Label(f, text=f"STAT {i}", bg=PANEL, fg=DIM,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
            tk.Label(f, text="1234", bg=PANEL, fg="#4aa3ff",
                     font=("Segoe UI", 22, "bold")).pack(anchor="w")
            tk.Label(f, text="sub", bg=PANEL, fg=DIM,
                     font=("Segoe UI", 8)).pack(anchor="w")
        mid = tk.Frame(self, bg=PANEL)
        mid.pack(fill="both", expand=True, padx=10, pady=6)
        for i in range(14):                     # 14 lamp canvases
            row = tk.Frame(mid, bg=PANEL)
            row.grid(row=i // 2, column=i % 2, sticky="w", padx=6, pady=2)
            c = tk.Canvas(row, width=12, height=12, bg=PANEL,
                          highlightthickness=0)
            c.create_oval(2, 2, 11, 11, fill=GOOD, outline="")
            c.pack(side="left")
            tk.Label(row, text=f"lamp {i}", bg=PANEL, fg=DIM,
                     font=("Segoe UI", 8)).pack(side="left", padx=4)
        for i in range(7):                      # 7 bar canvases
            row = tk.Frame(mid, bg=PANEL)
            row.grid(row=i, column=2, sticky="w", padx=10, pady=2)
            tk.Label(row, text=f"bar{i}", bg=PANEL, fg=DIM,
                     font=("Segoe UI", 8), width=6).pack(side="left")
            c = tk.Canvas(row, width=150, height=12, bg="#252932",
                          highlightthickness=0)
            c.create_rectangle(0, 0, 90, 12, fill="#4aa3ff", outline="")
            c.pack(side="left")

    def _build_canvas(self):
        cv = tk.Canvas(self, bg="#0f1114", highlightthickness=0)
        cv.pack(fill="both", expand=True, padx=10, pady=10)
        self.update_idletasks()
        w = cv.winfo_width() or 900
        h = cv.winfo_height() or 560
        pts = []
        for i in range(103):                    # stand-in count; the real VF curve is 128
            x = 40 + i * (w - 60) / 103.0
            y = h - 40 - (i * (h - 80) / 103.0)
            pts += [x, y]
            cv.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#4aa3ff",
                           outline="")
        cv.create_line(*pts, fill="#4aa3ff", width=2)
        for gx in range(10):                    # grid lines
            x = 40 + gx * (w - 60) / 10.0
            cv.create_line(x, 8, x, h - 20, fill="#191d24")

    # ---- measurement ---- #
    def _on_cfg(self, _e):
        self._times.append(time.perf_counter())

    def _report(self):
        now = time.perf_counter()
        cutoff = now - 1.0
        self._times = [t for t in self._times if t >= cutoff]
        n = len(self._times)
        gap = 0.0
        if n > 1:
            gap = max((b - a) * 1000
                      for a, b in zip(self._times, self._times[1:]))
        if n > 2:
            col = GOOD if gap < 30 else (WARN if gap < 80 else BAD)
            verdict = ("SMOOTH" if gap < 30 else
                       "hitching" if gap < 80 else "LAGGY")
            self.lbl.config(
                text=f"{verdict}\nmoves/sec {n}\nmax gap {gap:.0f} ms",
                fg=col)
        else:
            self.lbl.config(text="drag me by the title bar...", fg=TEXT)
        self.after(200, self._report)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in MODES:
        mode = sys.argv[1]
        if mode == "sized":
            dpi_aware()
        Probe(mode).mainloop()
        return
    # launcher: one process per mode so they cannot influence each other
    procs = [subprocess.Popen([sys.executable, __file__, m]) for m in MODES]
    print("Launched:", ", ".join(MODES))
    print("Drag each window by its title bar for ~3s and compare the readout.")
    for p in procs:
        p.wait()


if __name__ == "__main__":
    main()
