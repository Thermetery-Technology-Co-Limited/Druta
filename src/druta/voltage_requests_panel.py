# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Six independently addressed legacy request words for each present rail."""

from decimal import Decimal

import dearpygui.dearpygui as dpg

from . import voltage_requests_live as requests


class VoltageRequestsPanel:
    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.gpu = None
        self.choices = {}

    @staticmethod
    def _tag(position, suffix):
        return f"voltage_requests_{position}_{suffix}"

    def build(self):
        with dpg.collapsing_header(
            label="Live voltage rail requests", tag="voltage_requests_header", default_open=False
        ):
            dpg.add_text(
                "Signed offsets in mV for the driver's stored legacy rail requests. "
                "Readback confirms requests, not measured voltage. The request gate is read separately; "
                "applying here never enables it.",
                wrap=self.app.s(900),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Read requests", callback=self.refresh)
                dpg.add_combo(
                    [], tag="voltage_requests_select", width=self.app.s(260), enabled=False, callback=self.select
                )
            for position, field in enumerate(requests.FIELDS):
                with dpg.group(horizontal=True):
                    dpg.add_input_text(
                        label=f"{field} offset (mV)",
                        tag=self._tag(position, "value"),
                        width=self.app.s(130),
                        enabled=False,
                    )
                    dpg.add_button(
                        label=f"Apply {field}",
                        tag=self._tag(position, "apply"),
                        enabled=False,
                        callback=self.apply,
                        user_data=field,
                    )
                    if field.startswith("VCRIT"):
                        dpg.add_text(requests.VCRIT_NOTE)
            dpg.add_text("Read requests for the selected adapter.", tag="voltage_requests_status", wrap=self.app.s(900))
            dpg.add_input_text(
                tag="voltage_requests_inventory", readonly=True, multiline=True, width=-1, height=self.app.s(140)
            )

    def _slot(self):
        gpu = getattr(self.app, "gpu", None)
        if gpu is None or not gpu.slot():
            raise ValueError("Select a GPU with an adapter slot first")
        return gpu.slot()

    def _current(self):
        return bool(self.snapshot and self.gpu is self.app.gpu and self.snapshot["slot"] == self._slot())

    def _status(self, message, ok=True):
        dpg.set_value("voltage_requests_status", message)
        if not ok:
            self.app.log(message, False)

    def _clear(self):
        self.snapshot, self.gpu, self.choices = None, None, {}
        dpg.configure_item("voltage_requests_select", items=[], enabled=False)
        dpg.set_value("voltage_requests_select", "")
        dpg.set_value("voltage_requests_inventory", "")
        for position in range(6):
            for suffix in ("value", "apply"):
                dpg.configure_item(self._tag(position, suffix), enabled=False)

    def _show(self, snapshot, gpu):
        selected = self.choices.get(dpg.get_value("voltage_requests_select"))
        self.snapshot, self.gpu = snapshot, gpu
        self.choices = {
            f"Rail {index} ({snapshot['request']['rail_identity'][index]['name']})": index
            for index in snapshot["values"]
        }
        dpg.configure_item("voltage_requests_select", items=list(self.choices), enabled=bool(self.choices))
        choice = next(
            (label for label, index in self.choices.items() if index == selected), next(iter(self.choices), "")
        )
        dpg.set_value("voltage_requests_select", choice)
        lines = [
            f"Rail {rail} {field}: {Decimal(value) / 1000} mV"
            + (f" ({requests.VCRIT_NOTE})" if field.startswith("VCRIT") else "")
            for rail, values in snapshot["values"].items()
            for field, value in values.items()
        ]
        for rail, row in snapshot["request"]["policy"]["rails"].items():
            for name, label in (
                ("reliability", "REL"),
                ("alt_reliability", "ALT/OP"),
                ("overvoltage", "OV"),
                ("vmin", "VMIN"),
                ("effective", "effective"),
            ):
                if name in row["policy_uv"]:
                    lines.append(f"Rail {rail} reported {label} policy: {Decimal(row['policy_uv'][name]) / 1000} mV")
        lines.append(f"Preserved voltage boost request: {snapshot['request']['boost_percent']}%")
        missing = snapshot["request"]["missing_indices"]
        if missing:
            lines.append(f"Active rails unavailable for complete preservation: {missing}; applying disabled")
        dpg.set_value("voltage_requests_inventory", "\n".join(lines))
        self.select()

    def _gate_message(self):
        if self.snapshot["gate_enabled"] is False:
            return "Voltage-request gate disabled: requests are suppressed. Enable Allow voltage requests separately."
        if self.snapshot["gate_enabled"] is None:
            return "Request gate read unavailable; applying disabled: " + str(self.snapshot["gate_error"])
        if not self.snapshot["request"]["complete"]:
            return "Requests read; complete preservation is unavailable, so applying is disabled."
        return "Requests read. Voltage-request gate enabled."

    def refresh(self, *unused):
        if not self.app.guard():
            return
        try:
            slot, gpu = self._slot(), self.app.gpu
            snapshot = requests.read(gpu, slot=slot)
            if self.app.gpu is not gpu or self._slot() != slot or snapshot["slot"] != slot:
                raise requests.VoltageRequestsError("identity", "Selected adapter changed while reading requests")
            self._show(snapshot, gpu)
            self._status(self._gate_message())
        except Exception as exc:
            retained = False
            try:
                retained = self._current() and getattr(exc, "kind", None) in ("transient", "unstable")
            except ValueError:
                pass
            if not retained:
                self._clear()
            self._status(
                "Could not read requests: "
                + str(exc)
                + (". Previous valid display retained; applying always reads fresh requests." if retained else ""),
                False,
            )

    def select(self, *unused):
        try:
            valid = self._current()
        except ValueError:
            valid = False
        rail = self.choices.get(dpg.get_value("voltage_requests_select"))
        valid = bool(valid and rail in self.snapshot["values"]) if valid else False
        writable = bool(valid and self.snapshot["gate_enabled"] is True and self.snapshot["request"]["complete"])
        for position, field in enumerate(requests.FIELDS):
            dpg.configure_item(self._tag(position, "value"), enabled=valid)
            dpg.configure_item(self._tag(position, "apply"), enabled=writable)
            if valid:
                dpg.set_value(
                    self._tag(position, "value"), format(Decimal(self.snapshot["values"][rail][field]) / 1000, "f")
                )

    def apply(self, sender=None, app_data=None, user_data=None):
        if not self.app.guard():
            return
        try:
            slot = self._slot()
            rail = self.choices.get(dpg.get_value("voltage_requests_select"))
            if not self._current() or rail is None:
                raise ValueError("Read requests for the selected adapter first")
            if user_data not in requests.FIELDS:
                raise ValueError("Select a request field to apply")
            position = requests.FIELDS.index(user_data)
            value = requests.millivolts_to_uv(dpg.get_value(self._tag(position, "value")))
            gpu = self.app.gpu
            result = requests.set_request(gpu, rail, user_data, value, expected=self.snapshot, slot=slot)
            if self.app.gpu is not gpu or self._slot() != slot:
                raise ValueError("Selected adapter changed during request apply; read requests again")
            self._show(result["snapshot"], gpu)
            if not result["changed"]:
                self._status("No change requested; stored request already equals this value. No SET sent.")
            else:
                self._status(
                    "Stored request applied and exact full request readback verified."
                    + (f" VCRIT: {requests.VCRIT_NOTE}." if user_data.startswith("VCRIT") else "")
                )
        except Exception as exc:
            if getattr(exc, "kind", None) == "identity":
                self._clear()
            self._status("Could not apply request: " + str(exc), False)
