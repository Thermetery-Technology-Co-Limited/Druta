# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual PCIe link retraining requests for the current adapter."""

import dearpygui.dearpygui as dpg


class PcieSpeedPanel:
    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.snapshot_gpu = None
        self.stale = True
        self.previous = None

    def build(self):
        dpg.add_text(
            "Request a PCIe generation and retrain the link. This is not a permanent speed lock; "
            "the driver may change the link later. Refresh to see current readback.", wrap=self.app.s(900))
        with dpg.group(horizontal=True):
            dpg.add_button(label="Refresh", tag="pcie_speed_refresh", callback=self.read)
            dpg.add_combo([], tag="pcie_speed_generation", width=self.app.s(120), enabled=False,
                          callback=self._selection_changed)
            dpg.add_button(label="Request speed", tag="pcie_speed_request", callback=self.request, enabled=False)
            dpg.add_button(label="Request previous speed", tag="pcie_speed_previous",
                           callback=self.request_previous, enabled=False)
        dpg.add_text("Refresh status before requesting a speed.", tag="pcie_speed_status", wrap=self.app.s(900))
        dpg.add_input_text(tag="pcie_speed_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(110))

    def _controller(self, gpu):
        from .pcie_speed_live import PcieSpeedController
        return PcieSpeedController(gpu)

    def reset_view(self):
        self.snapshot = None
        self.snapshot_gpu = None
        self.previous = None
        self.stale = True
        dpg.set_value("pcie_speed_readback", "")
        dpg.set_value("pcie_speed_status", "Refresh status before requesting a speed.")
        dpg.configure_item("pcie_speed_generation", items=[])
        dpg.set_value("pcie_speed_generation", "")
        self._configure_actions()

    def _fresh(self):
        return bool(not self.stale and self.snapshot and self.snapshot.get("available")
                    and self.snapshot_gpu is self.app.gpu and self.snapshot.get("identity") is not None)

    def _selected(self):
        value = dpg.get_value("pcie_speed_generation")
        for generation in (self.snapshot or {}).get("allowed_generations", []):
            if value == f"Gen {generation}":
                return generation
        return None

    def _configure_actions(self):
        fresh = self._fresh()
        choices = (self.snapshot or {}).get("allowed_generations", [])
        dpg.configure_item("pcie_speed_generation", enabled=bool(fresh and choices))
        dpg.configure_item("pcie_speed_request", enabled=bool(fresh and self._selected() is not None))
        previous = self.previous
        dpg.configure_item("pcie_speed_previous", enabled=bool(
            fresh and previous and previous[0] == self.snapshot.get("identity") and previous[1] in choices))

    def _selection_changed(self, *unused):
        self._configure_actions()

    @staticmethod
    def _format(snapshot):
        if not snapshot.get("available"):
            return "Unavailable: " + str(snapshot.get("status", "No PCIe readback."))
        def gen(value):
            return f"Gen {value}" if value is not None else "unknown"
        def width(value):
            return f"x{value}" if value is not None else "unknown"
        return (f"Programmed target: {gen(snapshot.get('target_generation'))}\n"
                f"GPU link: {gen(snapshot.get('generation'))}, {width(snapshot.get('width'))}; "
                f"root link: {gen(snapshot.get('root_generation'))}, {width(snapshot.get('root_width'))}\n"
                f"Endpoint maximum: GPU {gen(snapshot.get('gpu_max_generation'))}; "
                f"root {gen(snapshot.get('root_max_generation'))}")

    def _show(self, snapshot, gpu, message):
        if self.previous and self.previous[0] != snapshot.get("identity"):
            self.previous = None
        self.snapshot = snapshot
        self.snapshot_gpu = gpu
        self.stale = False
        choices = snapshot.get("allowed_generations", []) if snapshot.get("available") else []
        labels = [f"Gen {generation}" for generation in choices]
        dpg.configure_item("pcie_speed_generation", items=labels)
        selected = f"Gen {snapshot.get('target_generation')}"
        dpg.set_value("pcie_speed_generation", selected if selected in labels else (labels[0] if labels else ""))
        dpg.set_value("pcie_speed_readback", self._format(snapshot))
        dpg.set_value("pcie_speed_status", message)
        self._configure_actions()

    def _failed(self, operation, exc):
        self.stale = True
        self.previous = None
        old = dpg.get_value("pcie_speed_readback")
        if old and not old.startswith("Historical readback"):
            dpg.set_value("pcie_speed_readback", "Historical readback (refresh required):\n" + old)
        self._configure_actions()
        message = f"PCIe speed {operation} failed: {exc}. Refresh before requesting a speed."
        dpg.set_value("pcie_speed_status", message)
        self.app.log(message, False)

    def read(self, *unused):
        gpu = self.app.gpu
        if self.snapshot_gpu is not None and self.snapshot_gpu is not gpu:
            self.previous = None
        try:
            snapshot = self._controller(gpu).read()
            message = "Status refreshed." if snapshot.get("available") else str(snapshot.get("status", "Unavailable."))
            self._show(snapshot, gpu, message)
        except Exception as exc:  # noqa: BLE001
            self._failed("refresh", exc)

    def _request(self, generation):
        if not self._fresh() or generation not in self.snapshot.get("allowed_generations", []):
            if self.snapshot_gpu is not self.app.gpu:
                self._failed("request", "GPU changed")
            else:
                self._configure_actions()
            return
        if not self.app.guard():
            return
        gpu = self.app.gpu
        identity = self.snapshot["identity"]
        try:
            result = self._controller(gpu).set_generation(generation, expected_identity=identity)
            if result.get("identity") != identity:
                raise ValueError("readback identity changed")
            verification = result.get("verification")
            before = result.get("before") or {}
            if (result.get("request_accepted") and verification == "target_and_link"
                    and before.get("identity") == identity
                    and before.get("target_generation") is not None
                    and before.get("target_generation") != generation):
                self.previous = (identity, before["target_generation"])
            elif verification != "already_matches":
                self.previous = None
            if verification == "target_and_link" and result.get("request_accepted"):
                message = f"Requested Gen {generation}; target and link readback verified."
            elif verification == "already_matches":
                message = f"Gen {generation} already matches the readback."
            else:
                message = f"Requested Gen {generation}; target and link change was not verified. Refresh to check."
            self._show(result, gpu, message)
            if verification not in ("target_and_link", "already_matches"):
                self.stale = True
                dpg.set_value("pcie_speed_readback", "Historical or unverified readback (refresh required):\n"
                              + dpg.get_value("pcie_speed_readback"))
                self._configure_actions()
            self.app.log(message, verification in ("target_and_link", "already_matches"))
        except Exception as exc:  # noqa: BLE001
            self._failed("request", exc)

    def request(self, *unused):
        self._request(self._selected())

    def request_previous(self, *unused):
        if self.previous and self._fresh() and self.previous[0] == self.snapshot.get("identity"):
            self._request(self.previous[1])
        else:
            self._configure_actions()
