"""Isolate painted AREA: identical content, two window sizes, plus a
canvas-only large window as control. Each in its own process."""
import subprocess, sys, tkinter as tk, time

def build(root):
    """~TitanTune-like visible content."""
    top = tk.Frame(root, bg="#16181d"); top.pack(fill="x")
    for i in range(6):
        f = tk.Frame(top, bg="#1e2128", padx=12, pady=8); f.grid(row=0, column=i, padx=4)
        tk.Label(f, text=f"STAT {i}", bg="#1e2128", fg="#8b9099").pack(anchor="w")
        tk.Label(f, text="1234", bg="#1e2128", fg="#4aa3ff",
                 font=("Segoe UI", 22, "bold")).pack(anchor="w")
    mid = tk.Frame(root, bg="#1e2128"); mid.pack(fill="both", expand=True)
    for i in range(14):
        r = tk.Frame(mid, bg="#1e2128"); r.grid(row=i//2, column=i%2, sticky="w", padx=6, pady=2)
        c = tk.Canvas(r, width=12, height=12, bg="#1e2128", highlightthickness=0)
        c.create_oval(2,2,11,11, fill="#46d17a", outline=""); c.pack(side="left")
        tk.Label(r, text=f"lamp {i}", bg="#1e2128", fg="#8b9099").pack(side="left")

def canvas_only(root):
    c = tk.Canvas(root, bg="#0f1114", highlightthickness=0); c.pack(fill="both", expand=True)
    for i in range(400):
        x=20+(i%40)*35; y=20+(i//40)*40
        c.create_oval(x,y,x+10,y+10, fill="#4aa3ff", outline="")
        c.create_text(x+16,y+5, text=str(i), fill="#8b9099", anchor="w")

MODES = {
 "small-content": ("700x500+40+40", build),
 "large-content": ("1500x1050+120+120", build),
 "large-canvas":  ("1500x1050+200+200", canvas_only),
}
def main():
    if len(sys.argv)>1 and sys.argv[1] in MODES:
        geo, fn = MODES[sys.argv[1]]
        r = tk.Tk(); r.title(f"probe: {sys.argv[1]}"); r.geometry(geo); r.configure(bg="#16181d")
        fn(r)
        tk.Label(r, text=sys.argv[1], bg="#16181d", fg="#ffcb47",
                 font=("Segoe UI", 16, "bold")).pack(side="bottom")
        r.mainloop(); return
    ps=[subprocess.Popen([sys.executable, __file__, m]) for m in MODES]
    for p in ps: p.wait()
main()
