"""Render the real Dear PyGui file picker against an isolated Unicode fixture.

The JSON checks the current path, default filename, and a nonblank screenshot.
The adjacent screenshot still needs visual inspection for directory entries;
this test does not click a file or claim that a user selection was made. It
never opens a GPU tuning backend.
"""
import argparse
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def framebuffer_content(dpg, screenshot):
    """Reject blank captures using DPG's own decoder and a small pixel grid."""
    decoded = dpg.load_image(str(screenshot))
    if decoded is None:
        return {"has_visible_content": False, "error": "Cannot decode framebuffer PNG"}
    width, height, channels, pixels = decoded
    samples = [pixels[(y * width + x) * channels + channel]
               for y in range(0, height, max(1, height // 64))
               for x in range(0, width, max(1, width // 64))
               for channel in range(3)]
    darkest, brightest = min(samples), max(samples)
    return {"width": width, "height": height,
            "sampled_rgb_min": darkest, "sampled_rgb_max": brightest,
            "has_visible_content": brightest - darkest > 1 / 255}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--warp", action="store_true")
    parser.add_argument("--extension", help="Test a freshly built _dearpygui.pyd")
    parser.add_argument("--crt-directory", help="Preload the matched app-local VC runtime")
    parser.add_argument("--hold-seconds", type=float, default=0)
    parser.add_argument("--offscreen", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    screenshot = output.with_suffix(".png")
    output.unlink(missing_ok=True)
    screenshot.unlink(missing_ok=True)
    if args.warp:
        os.environ["DRUTA_D3D11_WARP"] = "1"
    else:
        os.environ.pop("DRUTA_D3D11_WARP", None)
    if args.crt_directory:
        for name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
            ctypes.WinDLL(str(Path(args.crt_directory).resolve() / name))
    if args.extension:
        import dearpygui
        spec = importlib.util.spec_from_file_location(
            "dearpygui._dearpygui", str(Path(args.extension).resolve()))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    import dearpygui.dearpygui as dpg
    get_module_handle = ctypes.windll.kernel32.GetModuleHandleW
    get_module_handle.argtypes = [ctypes.c_wchar_p]
    get_module_handle.restype = ctypes.c_void_p

    report = {"status": "failed", "renderer_request": "warp" if args.warp else "hardware",
              "platform": str(sys.getwindowsversion()), "screenshot": str(screenshot),
              "selection_performed": False}
    dpg.create_context()
    try:
        with tempfile.TemporaryDirectory(prefix="Druta-Vista-dialog-") as temporary:
            fixture = Path(temporary) / "folder-caf\u00e9"
            fixture.mkdir()
            (fixture / "subfolder-caf\u00e9").mkdir()
            filename = "file-caf\u00e9.txt"
            (fixture / filename).write_text("Druta file-dialog smoke fixture\n", encoding="utf-8")
            (fixture / "visible-ascii.txt").write_text("ASCII comparison\n", encoding="utf-8")
            dpg.configure_app(manual_callback_management=True)
            dpg.create_viewport(title="Druta Vista file-dialog verification", width=1000,
                                height=760, x_pos=-2000 if args.offscreen else 80, y_pos=80)
            with dpg.file_dialog(tag="fixture", label="Vista Unicode directory test",
                                 default_path=str(fixture), default_filename=filename,
                                 width=800, height=560):
                dpg.add_file_extension(".txt")
            dpg.setup_dearpygui()
            dpg.show_viewport()
            capture_started = time.monotonic()
            for attempt in range(1, 4):
                # Vista's virtual hardware can present black frames initially.
                # Require elapsed render time, not just a handful of frames.
                warmup_deadline = time.monotonic() + 0.75
                first_frame = dpg.get_frame_count()
                while (time.monotonic() < warmup_deadline
                       or dpg.get_frame_count() - first_frame < 8):
                    if not dpg.is_dearpygui_running():
                        raise RuntimeError("Viewport closed before framebuffer capture")
                    dpg.render_dearpygui_frame()
                dpg.output_frame_buffer(str(screenshot))
                for _ in range(3):
                    dpg.render_dearpygui_frame()
                capture = framebuffer_content(dpg, screenshot)
                report.update({"capture_attempts": attempt, "framebuffer": capture,
                               "capture_elapsed_seconds": time.monotonic() - capture_started})
                if capture["has_visible_content"]:
                    break
            info = dpg.get_file_dialog_info("fixture")
            deadline = time.monotonic() + max(0, args.hold_seconds)
            while time.monotonic() < deadline and dpg.is_dearpygui_running():
                dpg.render_dearpygui_frame()
            report.update({"dearpygui_version": dpg.get_dearpygui_version(),
                           "dialog": info, "expected_path": str(fixture),
                           "expected_filename": filename,
                           "expected_visible_entries": ["subfolder-caf\u00e9", filename, "visible-ascii.txt"],
                           "rendered_frames": dpg.get_frame_count(),
                           "warp_module_loaded": bool(get_module_handle("d3d10warp.dll"))})
            assert Path(info["current_path"]).resolve() == fixture.resolve(), info
            assert info["file_name"] == filename, info
            assert screenshot.is_file() and screenshot.stat().st_size > 100, "No framebuffer screenshot"
            assert capture["has_visible_content"], "Framebuffer remained blank after three capture attempts"
            report["status"] = "passed"
    except Exception as error:
        report["error"] = repr(error)
    finally:
        dpg.destroy_context()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
