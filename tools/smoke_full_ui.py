"""Render Druta's actual UI without an NVIDIA device or application workers.

Run from a source checkout with its runtime requirements installed:
    python tools/smoke_full_ui.py --output full-ui-smoke.json

This is a developer fixture, not a telemetry/tuning test. It never calls
Druta.run(), executes callbacks, or supplies invented sensor measurements.
"""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
from unittest.mock import patch

# Support invocation from any working directory, including inside the VM.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class UnavailableBackend:
    """Only the state of an unavailable library; no driver functions exist."""
    ok = False
    selected = None
    gpu = None
    dev = None
    gpus = ()
    err_detail = "disabled by the full-UI smoke-test fixture"

    def __init__(self, slot=None):
        pass

    def has(self, name):
        return False


def smoke(output, hold_seconds=0):
    if not 0 <= hold_seconds <= 30:
        raise ValueError("hold_seconds must be between 0 and 30")
    report = {"status": "running", "stage": "imports",
              "python_version": platform.python_version(),
              "platform": platform.platform(), "rendered_tabs": [],
              "rendered_frames": 0, "forbidden_attempts": [],
              "hold_seconds": hold_seconds,
              "scope": "Actual UI with unavailable NVIDIA backends; no telemetry or tuning"}
    dpg = None
    context_created = False

    def save():
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")

    def forbidden(name):
        def reject(*args, **kwargs):
            report["forbidden_attempts"].append(name)
            raise RuntimeError("Forbidden during full-UI smoke test: " + name)
        return reject

    def cleanup():
        nonlocal context_created
        if context_created:
            context_created = False
            dpg.destroy_context()

    save()
    try:
        import dearpygui.dearpygui as dpg
        import nvbackend

        # These substitutions precede construction of the real GPU facade and
        # stay active until the DPG context is destroyed. Any unexpected worker
        # or subprocess attempt fails the test, even if application code catches
        # the exception. DPG's native renderer needs neither Python operation.
        with ExitStack() as guards:
            guards.enter_context(patch.object(nvbackend, "NvAPI", UnavailableBackend))
            guards.enter_context(patch.object(nvbackend, "Nvml", UnavailableBackend))
            guards.enter_context(patch.object(nvbackend, "is_admin", return_value=False))
            guards.enter_context(patch.object(subprocess, "Popen", forbidden("subprocess")))
            guards.enter_context(patch.object(threading.Thread, "start", forbidden("worker")))
            import druta
            guards.enter_context(patch.object(druta, "enumerate_gpus", return_value=[]))
            guards.enter_context(patch.object(druta.shuntmod, "load", return_value=[]))

            report["stage"] = "construct UI"
            save()
            app = druta.Druta()
            if app.gpu.available() or app.gpu_list:
                raise RuntimeError("Fixture unexpectedly exposed a GPU")
            app.gpu.static["name"] = "OFFLINE UI TEST - no GPU"
            report["dpi_scale"] = app.scale
            dpg.create_context()
            context_created = True
            guards.callback(cleanup)
            # Queued callbacks are deliberately never dispatched. User input
            # during the short test cannot invoke any application action.
            dpg.configure_app(manual_callback_management=True)
            report["dearpygui_version"] = dpg.get_dearpygui_version()
            dpg.create_viewport(title="Druta OFFLINE full-UI smoke test", width=1180,
                                height=900)
            app._dpg_ready = True
            app.load_fonts()
            with dpg.window(tag="root"):
                dpg.add_spacer(tag="menu_pad", height=app.menu_h())
            app.build_ui()
            dpg.set_value("unlock", False)
            app.sync_lock_ui()
            app.log("OFFLINE UI TEST: no NVIDIA backend; callbacks are disabled.")
            report["item_count"] = len(dpg.get_all_items())
            tabs = dpg.get_item_children("tabs", 1)
            if len(tabs) != 3:
                raise RuntimeError("Expected Control, Monitor and Timings tabs")
            report["stage"] = "render UI"
            save()
            try:
                dpg.setup_dearpygui()
                dpg.show_viewport()
                dpg.set_primary_window("root", True)
                for tab in tabs:
                    label = dpg.get_item_label(tab).strip()
                    report["stage"] = "render " + label
                    save()
                    dpg.set_value("tabs", tab)
                    for _ in range(3):
                        if not dpg.is_dearpygui_running():
                            raise RuntimeError("Viewport closed before all tabs rendered")
                        app.relayout()
                        dpg.render_dearpygui_frame()
                        report["rendered_frames"] += 1
                    selected = dpg.get_value("tabs")
                    if isinstance(selected, str):
                        selected = dpg.get_alias_id(selected)
                    if selected != tab:
                        raise RuntimeError("Tab selection did not reach " + label)
                    report["rendered_tabs"].append(label)
                    save()
                    # Optional screenshot window; no callbacks are dispatched
                    # while the final frame of this tab remains on screen.
                    if hold_seconds:
                        time.sleep(hold_seconds)
                if report["forbidden_attempts"]:
                    raise RuntimeError("Application attempted a worker or subprocess")
            finally:
                cleanup()
            report["stage"] = "complete"
            report["status"] = "passed"
    except Exception as e:
        report["status"] = "failed"
        report["error"] = type(e).__name__ + ": " + str(e)
    finally:
        if context_created:
            try:
                cleanup()
            except Exception as e:
                report["cleanup_error"] = str(e)
        save()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="Path to the JSON validation report")
    parser.add_argument("--hold-seconds", type=float, default=0,
                        help="Hold each rendered tab for screenshots (0 to 30 seconds)")
    args = parser.parse_args()
    if not 0 <= args.hold_seconds <= 30:
        parser.error("--hold-seconds must be between 0 and 30")
    sys.exit(smoke(args.output, args.hold_seconds))
