"""
Find the child-HWND threshold where dragging turns laggy on THIS machine.

Four windows, identical size, differing only in how many real widgets they hold
(8 / 16 / 24 / 40). Drag each; the count where it turns bad tells us how far the
Control tab has to be cut - so we rewrite exactly as much as needed and no more.

Each window reports its own HWND count and drag smoothness (moves/sec, max gap
between move events - the lag you feel).
"""
import subprocess
import sys
import time
import tkinter as tk

BG, PANEL, TEXT, DIM = "#16181d", "#1e2128", "#e6e8ec", "#8b9099"
GOOD, WARN, BAD = "#46d17a", "#ffcb47", "#ff5c5c"
COUNTS = (8, 16, 24, 40)


def build(root, n):
    """n interactive-ish widgets, like the Control tab's sliders/buttons."""
    grid = tk.Frame(root, bg=BG)
    grid.pack(fill="both", expand=True, padx=10, pady=10)
    made = 0
    row = 0
    while made < n:
        f = tk.Frame(grid, bg=BG)
        f.grid(row=row, column=0, sticky="w", pady=4)
        made += 1
        if made < n:
            tk.Label(f, text=f"control {row}", bg=BG, fg=TEXT,
                     width=16, anchor="w").pack(side="left")
            made += 1
        if made < n:
            tk.Scale(f, from_=0, to=100, orient="horizontal", length=260,
                     bg=BG, fg=TEXT, troughcolor=PANEL,
                     highlightthickness=0).pack(side="left", padx=6)
            made += 1
        if made < n:
            tk.Button(f, text="Apply", bg=PANEL, fg=TEXT,
                      relief="flat").pack(side="left", padx=4)
            made += 1
        row += 1


class Probe(tk.Tk):
    def __init__(self, n):
        super().__init__()
        self.title(f"threshold: {n} widgets")
        self.configure(bg=BG)
        i = COUNTS.index(n)
        self.geometry(f"900x620+{60 + i * 60}+{60 + i * 50}")
        self.info = tk.Label(self, text="drag me", bg=BG, fg=TEXT,
                             font=("Segoe UI", 15, "bold"), justify="left")
        self.info.pack(anchor="nw", padx=14, pady=8)
        build(self, n)
        self.n = n
        self._t = []
        self.bind("<Configure>", lambda e: self._t.append(time.perf_counter()))
        self.after(300, self._report)

    def _hwnds(self):
        import ctypes
        u = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                   ctypes.c_void_p)
        c = [0]

        def cb(h, l):
            if u.IsWindowVisible(h):
                c[0] += 1
            return True
        u.EnumChildWindows(u.GetAncestor(int(self.winfo_id()), 2),
                           proto(cb), None)
        return c[0]

    def _report(self):
        now = time.perf_counter()
        self._t = [t for t in self._t if t >= now - 1.0]
        k = len(self._t)
        gap = max((b - a) * 1000 for a, b in zip(self._t, self._t[1:])) \
            if k > 1 else 0.0
        if k > 2:
            col = GOOD if gap < 30 else (WARN if gap < 80 else BAD)
            verdict = ("SMOOTH" if gap < 30 else
                       "hitching" if gap < 80 else "LAGGY")
            self.info.config(
                text=f"{verdict}\n{self.n} widgets / {self._hwnds()} HWNDs\n"
                     f"moves/sec {k}   max gap {gap:.0f} ms", fg=col)
        else:
            self.info.config(
                text=f"drag me\n{self.n} widgets / {self._hwnds()} HWNDs",
                fg=TEXT)
        self.after(300, self._report)


def main():
    if len(sys.argv) > 1:
        Probe(int(sys.argv[1])).mainloop()
        return
    ps = [subprocess.Popen([sys.executable, __file__, str(n)]) for n in COUNTS]
    for p in ps:
        p.wait()


if __name__ == "__main__":
    main()
