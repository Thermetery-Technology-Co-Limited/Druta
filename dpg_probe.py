"""
Proof test: does Dear PyGui drag smoothly where Tk does not?

Builds MORE interactive controls than TitanTune's Control tab (the case that is
still very laggy under Tk): 12 sliders, 12 buttons, 8 inputs, 8 checkboxes,
plus a live-updating plot and a log - and it also runs a 1 Hz "telemetry"
callback, so it exercises everything that made Tk struggle.

The whole UI is ONE native window rendered on the GPU (Dear ImGui core +
DirectX 11), instead of Tk's one-HWND-per-widget model.

Drag this window by its title bar and compare with TitanTune's Control tab.
"""
import ctypes
import os
import time

import dearpygui.dearpygui as dpg


def dpi_scale():
    """Real desktop scale factor (1.5 at 150%). Must be read AFTER declaring
    DPI awareness, otherwise Windows lies to us and reports 96 dpi."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def load_fonts(scale):
    """Dear ImGui's built-in font is a small bitmap face - it looks rough on a
    4K/150% display. Rasterise real TTFs at the PHYSICAL pixel size (base*scale)
    so glyphs are sharp instead of magnified."""
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    ui = os.path.join(win, "segoeui.ttf")
    mono = os.path.join(win, "consola.ttf")
    fonts = {}
    with dpg.font_registry():
        if os.path.exists(ui):
            fonts["ui"] = dpg.add_font(ui, int(round(16 * scale)))
            fonts["big"] = dpg.add_font(ui, int(round(22 * scale)))
        if os.path.exists(mono):
            fonts["mono"] = dpg.add_font(mono, int(round(14 * scale)))
    return fonts

N_SLIDERS = 12
N_BUTTONS = 12
N_INPUTS = 8
N_CHECKS = 8


def count_child_hwnds(title="DearPyGui drag proof"):
    """How many native child windows this UI actually creates."""
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, title)
    if not hwnd:
        return None, None
    n = [0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, l):
        n[0] += 1
        return True
    u.EnumChildWindows(hwnd, proto(cb), None)
    return hwnd, n[0]


def main():
    scale = dpi_scale()
    dpg.create_context()
    dpg.create_viewport(title="DearPyGui drag proof",
                        width=int(1200 * scale), height=int(850 * scale))
    fonts = load_fonts(scale)
    if "ui" in fonts:
        dpg.bind_font(fonts["ui"])          # crisp, correctly sized text

    with dpg.window(tag="main", label="Control-tab equivalent (all GPU-drawn)"):
        dpg.add_text("Drag this window by its title bar.", tag="hdr")
        if "big" in fonts:
            dpg.bind_item_font("hdr", fonts["big"])
        dpg.add_text("", tag="stats")
        dpg.add_separator()

        with dpg.group(horizontal=True):
            with dpg.group():
                dpg.add_text("sliders")
                for i in range(N_SLIDERS):
                    dpg.add_slider_int(label=f"slider {i}", default_value=i * 7,
                                       min_value=0, max_value=100, width=int(240 * scale))
            with dpg.group():
                dpg.add_text("inputs / checks")
                for i in range(N_INPUTS):
                    dpg.add_input_int(label=f"value {i}", default_value=i * 15,
                                      width=int(160 * scale))
                for i in range(N_CHECKS):
                    dpg.add_checkbox(label=f"option {i}", default_value=i % 2 == 0)
            with dpg.group():
                dpg.add_text("buttons")
                for i in range(N_BUTTONS):
                    dpg.add_button(label=f"Apply {i}", width=int(120 * scale))

        dpg.add_separator()
        with dpg.plot(label="live telemetry", height=int(200 * scale), width=-1):
            dpg.add_plot_axis(dpg.mvXAxis, label="t")
            with dpg.plot_axis(dpg.mvYAxis, label="MHz", tag="yax"):
                dpg.add_line_series([], [], label="core", tag="series")
        dpg.add_input_text(tag="log", multiline=True, readonly=True,
                           height=int(120 * scale), width=-1,
                           default_value="log line 0\nlog line 1\n")
        if "mono" in fonts:
            dpg.bind_item_font("log", fonts["mono"])

    dpg.setup_dearpygui()
    dpg.show_viewport()

    xs, ys = [], []
    t0 = time.perf_counter()
    last_tel = 0.0
    frames = 0
    last_report = t0
    hwnd_info = [None]

    while dpg.is_dearpygui_running():
        now = time.perf_counter()
        frames += 1

        # 1 Hz "telemetry", same cadence as the real app
        if now - last_tel >= 1.0:
            last_tel = now
            xs.append(now - t0)
            ys.append(1500 + 300 * ((len(xs) % 10) / 10.0))
            if len(xs) > 60:
                xs, ys = xs[-60:], ys[-60:]
            dpg.set_value("series", [xs, ys])

        # frame-rate readout: this is the drag-smoothness signal
        if now - last_report >= 0.5:
            fps = frames / (now - last_report)
            frames = 0
            last_report = now
            if hwnd_info[0] is None:
                hwnd_info[0] = count_child_hwnds()[1]
            total = N_SLIDERS + N_BUTTONS + N_INPUTS + N_CHECKS
            dpg.set_value(
                "stats",
                f"{total} interactive controls   |   native child HWNDs: "
                f"{hwnd_info[0]}   |   {fps:5.1f} FPS   |   DPI scale "
                f"{scale:.2f}, UI font {int(round(16 * scale))}px\n"
                f"(smooth drag = FPS stays high while you move the window)")

        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    main()
