"""
Measure, from OUTSIDE, whether a window's UI thread stays responsive.

Sends WM_NULL (a no-op message) to the target window every ~20 ms with
SendMessageTimeout and records the round-trip time. WM_NULL does nothing, so the
round-trip is purely "how long until that window's thread got round to me".

  round-trip small  -> the app's thread is responsive; any drag lag is BELOW the
                       app (DWM compositing / driver / MPO).
  round-trip large  -> the app's thread is blocked or busy; the lag is the app's
                       own doing and we can go find what blocks it.

Usage:  python ping_ui.py [seconds] [window_title_substring]
Writes a summary to ping_ui_result.txt and prints it.
"""
import ctypes
import sys
import time

user32 = ctypes.windll.user32
WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002
SMTO_NORMAL = 0x0000


def find_windows(substr):
    out = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if substr.lower() in buf.value.lower():
                out.append((hwnd, buf.value))
        return True
    user32.EnumWindows(proto(cb), None)
    return out


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
    title = sys.argv[2] if len(sys.argv) > 2 else "Druta"
    wins = find_windows(title)
    if not wins:
        print(f"no visible window matching {title!r}")
        return
    hwnd, name = wins[0]
    print(f"pinging '{name}' (hwnd {hwnd}) for {dur:.0f}s ...")
    print("DRAG THE WINDOW AROUND NOW.\n")

    res = ctypes.c_ulong()
    samples = []
    t_end = time.perf_counter() + dur
    while time.perf_counter() < t_end:
        t0 = time.perf_counter()
        user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0,
                                   SMTO_NORMAL | SMTO_ABORTIFHUNG, 2000,
                                   ctypes.byref(res))
        samples.append(((time.perf_counter() - t0) * 1000, t0))
        time.sleep(0.02)

    vals = sorted(s for s, _t in samples)
    n = len(vals)
    over = [(round(s), round(t, 2)) for s, t in samples if s > 50]
    lines = [
        f"window: {name}",
        f"samples: {n} over {dur:.0f}s",
        f"median  {vals[n//2]:8.2f} ms",
        f"p95     {vals[int(n*0.95)]:8.2f} ms",
        f"p99     {vals[int(n*0.99)]:8.2f} ms",
        f"MAX     {vals[-1]:8.2f} ms",
        f"round-trips >50ms: {len([v for v in vals if v > 50])}",
        f"round-trips >200ms: {len([v for v in vals if v > 200])}",
        "",
        "VERDICT: " + (
            "UI THREAD IS BLOCKED during drag -> the app is the cause"
            if vals[int(n * 0.95)] > 50 else
            "UI thread stayed RESPONSIVE -> lag is below the app "
            "(DWM/compositing/driver, e.g. MPO)"),
    ]
    txt = "\n".join(lines)
    print(txt)
    with open("ping_ui_result.txt", "w", encoding="utf-8") as f:
        f.write(txt + "\n\nslow samples (ms, t): " + repr(over[:40]))


if __name__ == "__main__":
    main()
