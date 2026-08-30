"""
Automated window-move benchmark - no human dragging required.

During a real title-bar drag Windows repeatedly repositions the window; the cost
of each reposition (including repainting/clipping every child HWND and handing
the result to DWM) is what you feel as lag. This times SetWindowPos directly, so
each UI variant can be compared objectively and repeatably.

Each variant is built, moved N times, then destroyed. Reports ms per move.
A smooth drag at 119 Hz needs < ~8 ms/move; > 16 ms will visibly lag.
"""
import ctypes
import sys
import time
import tkinter as tk

user32 = ctypes.windll.user32
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def count_children(hwnd):
    n = [0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, l):
        n[0] += 1
        return True
    user32.EnumChildWindows(hwnd, proto(cb), None)
    return n[0]


# --------------------------------------------------------------------------- #
#  variants                                                                     #
# --------------------------------------------------------------------------- #
def v_bare(root):
    return "empty window"


def _labels(root, n):
    f = tk.Frame(root, bg="#16181d")
    f.pack(fill="both", expand=True)
    for i in range(n):
        tk.Label(f, text=f"label {i}", bg="#1e2128", fg="#e6e8ec").grid(
            row=i // 6, column=i % 6, padx=2, pady=2)
    return f"{n} Labels (flat)"


def v_lab30(root):
    return _labels(root, 30)


def v_lab60(root):
    return _labels(root, 60)


def v_lab90(root):
    return _labels(root, 90)


def v_canvas20(root):
    """20 SEPARATE small canvases - the pattern the old lamps/bars used."""
    f = tk.Frame(root, bg="#16181d")
    f.pack(fill="both", expand=True)
    for i in range(20):
        c = tk.Canvas(f, width=150, height=14, bg="#252932",
                      highlightthickness=0)
        c.create_rectangle(0, 0, 90, 14, fill="#4aa3ff", outline="")
        c.grid(row=i // 2, column=i % 2, padx=4, pady=2)
    return "20 separate Canvases"


def v_canvas1(root):
    """ONE canvas holding 500 drawn items."""
    c = tk.Canvas(root, bg="#0f1114", highlightthickness=0)
    c.pack(fill="both", expand=True)
    for i in range(500):
        x = 20 + (i % 50) * 18
        y = 20 + (i // 50) * 30
        c.create_oval(x, y, x + 8, y + 8, fill="#4aa3ff", outline="")
    return "1 Canvas, 500 items"


def v_nested(root):
    """90 labels buried in deep nested frames - tests hierarchy depth."""
    parent = root
    for _ in range(8):                      # 8 levels deep
        parent = tk.Frame(parent, bg="#16181d")
        parent.pack(fill="both", expand=True)
    for i in range(90):
        tk.Label(parent, text=f"n{i}", bg="#1e2128", fg="#e6e8ec").grid(
            row=i // 6, column=i % 6)
    return "90 Labels, 8 frames deep"


def v_app(root):
    return None                              # handled specially


VARIANTS = [
    ("bare", v_bare), ("lab30", v_lab30), ("lab60", v_lab60),
    ("lab90", v_lab90), ("canvas20", v_canvas20), ("canvas1", v_canvas1),
    ("nested", v_nested),
]


def bench(win, moves=150, dx=3):
    """Time `moves` SetWindowPos repositions of the toplevel."""
    win.update_idletasks()
    win.update()
    hwnd = user32.GetAncestor(int(win.winfo_id()), 2)   # GA_ROOT
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    x0, y0 = r.left, r.top
    flags = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    times = []
    for i in range(moves):
        x = x0 + (i % 40) * dx
        y = y0 + (i % 20) * dx
        t0 = time.perf_counter()
        user32.SetWindowPos(hwnd, None, x, y, 0, 0, flags)
        win.update()                    # let Tk process the resulting events
        times.append((time.perf_counter() - t0) * 1000)
    user32.SetWindowPos(hwnd, None, x0, y0, 0, 0, flags)
    times.sort()
    n = len(times)
    return times[n // 2], times[int(n * 0.95)], times[-1], hwnd


def run_one(name, builder, size):
    root = tk.Tk()
    root.title(f"bench {name}")
    root.geometry(size)
    root.configure(bg="#16181d")
    desc = builder(root)
    root.update()
    med, p95, mx, hwnd = bench(root)
    kids = count_children(hwnd)
    root.destroy()
    return name, desc, kids, med, p95, mx


def main():
    size = sys.argv[1] if len(sys.argv) > 1 else "1500x1050"
    print(f"window size {size}   (smooth at 119Hz needs < ~8 ms/move)\n")
    print(f"{'variant':10} {'HWNDs':>6} {'median':>8} {'p95':>8} {'max':>8}   what")
    for name, builder in VARIANTS:
        try:
            n, desc, kids, med, p95, mx = run_one(name, builder, size)
            print(f"{n:10} {kids:6d} {med:7.2f}ms {p95:7.2f}ms {mx:7.2f}ms   {desc}")
        except Exception as e:
            print(f"{name:10} FAILED: {e}")
    # the real app, if importable
    try:
        import app as legacy_ui
        import nvbackend
        legacy_ui._enable_dpi_awareness()
        legacy_ui.SCALE = legacy_ui._detect_scale()
        g = nvbackend.GPU(nvbackend.slot_from_argv())
        a = legacy_ui.App(g)
        a.update()
        med, p95, mx, hwnd = bench(a)
        kids = count_children(hwnd)
        print(f"{'Tk app.py':10} {kids:6d} {med:7.2f}ms {p95:7.2f}ms {mx:7.2f}ms   the real app")
        a._on_close()
    except Exception as e:
        print(f"{'Tk app.py':10} skipped: {e}")


if __name__ == "__main__":
    main()
