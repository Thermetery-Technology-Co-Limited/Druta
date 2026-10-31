# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controls for independently reported VFE policy offsets."""

import json
from decimal import Decimal, InvalidOperation

import dearpygui.dearpygui as dpg

from . import devicereset, paths, startup
from . import voltage_offsets_live as offsets


class VoltageOffsetsPanel:
    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.choices = {}

    def build(self):
        with dpg.collapsing_header(label="Voltage policy offsets", tag="voltage_offsets_header"):
            dpg.add_text(
                "Independent offsets for REL, ALT, OV and VMIN policies. Enable once to install the "
                "offset definitions and restart the selected GPU. Live changes do not restart the GPU. "
                "Voltage boost can recompute REL and effective limits. "
                "Reported values are not measured rail voltage. These settings are separate from legacy "
                "voltage requests.", wrap=self.app.s(900),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Read offsets", callback=self.refresh)
                dpg.add_button(label="Enable offsets and restart GPU", callback=self.enable)
            with dpg.group(horizontal=True):
                dpg.add_combo([], tag="voltage_offsets_select", width=self.app.s(250), enabled=False,
                              callback=self.select)
                dpg.add_input_text(label="Offset (mV)", tag="voltage_offsets_value", width=self.app.s(130),
                                   enabled=False)
                dpg.add_button(label="Apply now", tag="voltage_offsets_apply", callback=self.apply, enabled=False)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save offset profile", callback=self.save_profile)
                dpg.add_button(label="Load and apply offset profile", callback=self.load_profile)
            dpg.add_text("Read offsets for the selected adapter.", tag="voltage_offsets_status", wrap=self.app.s(900))
            dpg.add_input_text(tag="voltage_offsets_inventory", readonly=True, multiline=True, width=-1,
                               height=self.app.s(160))

    def _slot(self):
        gpu = getattr(self.app, "gpu", None)
        if gpu is None:
            raise ValueError("Select a GPU first")
        slot = gpu.slot()
        if not slot:
            raise ValueError("The selected GPU has no adapter slot")
        return slot

    def _status(self, message, ok=True):
        dpg.set_value("voltage_offsets_status", message)
        if not ok:
            self.app.log(message, False)

    def _clear(self):
        self.snapshot = None
        self.choices = {}
        for tag in ("voltage_offsets_select", "voltage_offsets_value", "voltage_offsets_apply"):
            dpg.configure_item(tag, enabled=False)

    @staticmethod
    def _label(key):
        parts = key.split(".")
        if len(parts) != 4 or parts[0] != "voltage" or parts[3] != "offset":
            raise ValueError("Unknown voltage offset entry")
        return f"Rail {parts[1]} {parts[2]}"

    def refresh(self, *unused):
        if not self.app.guard():
            return
        try:
            slot = self._slot()
            result = offsets.read(slot)
            if self._slot() != slot or result["slot"] != slot:
                raise ValueError("Selected adapter changed while reading offsets")
            self.snapshot = result
            self.choices = {self._label(key): key for key in result["values"]}
            labels = list(self.choices)
            dpg.configure_item("voltage_offsets_select", items=labels, enabled=bool(labels))
            lines = [f"{self._label(key)} offset: {Decimal(value) / 1000} mV"
                     for key, value in result["values"].items()]
            lines.extend(f"{self._label(key)}: unavailable" for key in result["unsupported"])
            for rail, row in result["policy"]["rails"].items():
                for name, label in (("reliability", "REL"), ("alt_reliability", "ALT"),
                                    ("overvoltage", "OV"), ("vmin", "VMIN"), ("effective", "effective")):
                    if name in row["policy_uv"]:
                        value = Decimal(row["policy_uv"][name]) / 1000
                        lines.append(f"Rail {rail} reported {label}: {value} mV")
            dpg.set_value("voltage_offsets_inventory", "\n".join(lines))
            if labels:
                dpg.set_value("voltage_offsets_select", labels[0])
                self.select()
            else:
                dpg.configure_item("voltage_offsets_value", enabled=False)
                dpg.configure_item("voltage_offsets_apply", enabled=False)
            self._status("Offsets read for the selected adapter.")
        except Exception as exc:
            self._clear()
            dpg.set_value("voltage_offsets_inventory", "")
            self._status("Could not read offsets: " + str(exc), False)

    def select(self, *unused):
        label = dpg.get_value("voltage_offsets_select")
        valid = bool(self.snapshot and label in self.choices and self.snapshot["slot"] == self._slot())
        dpg.configure_item("voltage_offsets_value", enabled=valid)
        dpg.configure_item("voltage_offsets_apply", enabled=valid)
        if valid:
            key = self.choices[label]
            dpg.set_value("voltage_offsets_value", format(Decimal(self.snapshot["values"][key]) / 1000, "f"))

    def enable(self, *unused):
        if not self.app.guard():
            return
        try:
            slot = self._slot()
            if not devicereset.is_admin():
                raise ValueError("Administrator rights are required to enable offsets")
            from . import voltage_offsets_cli
            voltage_offsets_cli.launch_enable(slot)
        except Exception as exc:
            self._status("Could not start offset activation: " + str(exc), False)
            return
        self.app._closing = True
        self.app._stop.set()
        dpg.stop_dearpygui()

    def apply(self, *unused):
        if not self.app.guard():
            return
        try:
            slot = self._slot()
            label = dpg.get_value("voltage_offsets_select")
            if not self.snapshot or self.snapshot["slot"] != slot or label not in self.choices:
                raise ValueError("Read offsets for the selected adapter first")
            key = self.choices[label]
            value = Decimal(dpg.get_value("voltage_offsets_value").strip()) * 1000
            if not value.is_finite() or value != value.to_integral_value():
                raise ValueError("Enter an offset with whole-microvolt precision")
            offsets.set_offsets(slot, {key: int(value)})
            self.refresh()
        except (InvalidOperation, ValueError, RuntimeError) as exc:
            self._status("Could not apply offset: " + str(exc), False)

    def _pick(self, *, save):
        folder = paths.profile_dir() / "voltage-offsets"
        folder.mkdir(parents=True, exist_ok=True)
        path, error = self.app.pick_file_native(
            "Save offset profile" if save else "Load offset profile", str(folder),
            "voltage-offsets.json" if save else "", (("Offset profiles (*.json)", "*.json"),), save=save,
        )
        if error:
            raise ValueError(error)
        return path

    def save_profile(self, *unused):
        if not self.app.guard():
            return
        try:
            slot = self._slot()
            values = offsets.capture(slot)
            path = self._pick(save=True)
            if not path:
                self._status("Profile save cancelled.")
                return
            if self._slot() != slot:
                raise ValueError("Selected adapter changed while choosing a file")
            startup.atomic_json(path, {"schema": 1, "slot": slot, "values": values})
            self._status("Offset profile saved.")
        except (OSError, ValueError, RuntimeError) as exc:
            self._status("Could not save offset profile: " + str(exc), False)

    def load_profile(self, *unused):
        if not self.app.guard():
            return
        try:
            slot = self._slot()
            path = self._pick(save=False)
            if not path:
                self._status("Profile load cancelled.")
                return
            if self._slot() != slot:
                raise ValueError("Selected adapter changed while choosing a file")
            with open(path, encoding="utf-8") as source:
                document = json.load(source)
            if (not isinstance(document, dict) or set(document) != {"schema", "slot", "values"}
                    or type(document["schema"]) is not int or document["schema"] != 1
                    or document["slot"] != slot or not isinstance(document["values"], dict)):
                raise ValueError("Profile does not match the selected adapter")
            offsets.replay(slot, document["values"])
            self.refresh()
        except (OSError, ValueError, RuntimeError) as exc:
            self._status("Could not load offset profile: " + str(exc), False)
