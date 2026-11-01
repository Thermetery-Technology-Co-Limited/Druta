# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed BIOS-field previews and durable undo in the Controls page."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

import dearpygui.dearpygui as dpg

from . import registry_overrides as overrides
from . import vbios_fields
from .perf_diagnostics import PerfDiagnosticsReader
from .voltage_request_gate import VoltageRequestGate

_VF_LIVE_FIELDS = (
    ("clock_vf.monotonicity", "vf_monotonicity_enforced", "reg_vf_live_monotonicity"),
    ("clock_vf.smoothening", "vf_smoothening_enforced", "reg_vf_live_smoothening"),
)
_DIFR_LABELS = {"9": "DFPR", "10": "MS DIFR SW-ASR", "11": "MS DIFR CG"}


def _type4_queries_become_unavailable(contract):
    before = contract["before"].get("bit1", {}).get("roles", {})
    after = contract["after"].get("bit1", {}).get("roles", {})
    return bool(before and after and any(row["available"] for row in before.values())
                and all(not row["available"] for row in after.values()))


class RegistryPanel:
    def __init__(self, app):
        self.app = app
        self.fields = {}
        self.inputs = {}
        self.preview = None
        self.active = None
        self.inspection = None
        self.diagnostics_reader = None
        self.diagnostics = None
        self.arbitration = None
        self.arbitration_target = None
        self.voltage_gate = None
        self.voltage_gate_snapshot = None
        self.voltage_gate_initial = None
        self.clock_vf_control = None
        self.clock_vf_snapshot = None
        self.clock_vf_initial = None
        self.voltage_device_control = None
        self.voltage_device_snapshot = None
        self.voltage_device_choices = {}
        self.difr_readback = None
        self.rel_live_control = None
        self.rel_live_snapshot = None
        self.rel_live_choices = {}
        self.rel_live_stale = False
        self.constants_live_control = None
        self.constants_live_snapshot = None
        self.constants_live_choices = {}
        self.constants_live_stale = False
        if not hasattr(app, "_constants_live_initial_states"):
            app._constants_live_initial_states = {}
        if not hasattr(app, "_rel_live_initial_states"):
            app._rel_live_initial_states = {}
        if not hasattr(app, "_voltage_gate_initial_states"):
            app._voltage_gate_initial_states = {}
        if not hasattr(app, "_clock_vf_initial_states"):
            app._clock_vf_initial_states = {}
        if not hasattr(app, "_voltage_device_initial_states"):
            app._voltage_device_initial_states = {}

    def build(self):
        dpg.add_text(
            "Change BIOS defaults, ranges, and driver model inputs used when the GPU starts.", wrap=self.app.s(900)
        )
        dpg.add_text(
            "Registry apply restarts the selected GPU and reopens Druta. Windows stays running. "
            "Changes persist until restored. Runtime tuning profiles are not reapplied.",
            wrap=self.app.s(900),
        )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Read current fields", callback=self.refresh)
            dpg.add_button(label="Preview changes", callback=self.prepare, tag="reg_preview_button", enabled=False)
            dpg.add_button(label="Apply and restart GPU", callback=self.apply, tag="reg_apply_button", enabled=False)
            dpg.add_button(
                label="Restore previous registry settings",
                callback=self.restore,
                tag="reg_restore_button",
                enabled=False,
            )
        dpg.add_text("Read fields to inspect this adapter.", tag="reg_status", wrap=self.app.s(900))
        dpg.add_text("", tag="reg_last_recovery", wrap=self.app.s(900), show=False)
        dpg.add_input_text(tag="reg_preview", readonly=True, multiline=True, width=-1, height=self.app.s(180))
        with dpg.group(tag="reg_fields"):
            pass
        with dpg.collapsing_header(label="Driver policy diagnostics"):
            dpg.add_text(
                "Read registered performance limits and feedback-policy flags. "
                "Registered slots do not identify which limits are currently active.",
                wrap=self.app.s(900),
            )
            dpg.add_button(label="Read driver policies", callback=self.refresh_diagnostics)
            dpg.add_input_text(tag="reg_diagnostics", readonly=True, multiline=True, width=-1, height=self.app.s(220))
        with dpg.collapsing_header(label="DIFR configured state"):
            dpg.add_text(
                "Read the loaded P-state to LPWR mapping and configured DFPR/MS DIFR support. "
                "These values describe configuration, not the current P-state, residency or measured power savings. "
                "DIFR entry and subfeature disables use the registry field preview and GPU restart above.",
                wrap=self.app.s(900),
            )
            dpg.add_button(label="Read DIFR configuration", callback=self.refresh_difr)
            dpg.add_input_text(tag="reg_difr_readback", readonly=True, multiline=True, width=-1, height=self.app.s(190))
        from .difr_live_panel import DifrLivePanel

        self.difr_live_panel = DifrLivePanel(self.app)
        with dpg.collapsing_header(label="Live DIFR disable request", tag="reg_difr_live_header"):
            self.difr_live_panel.build()
        from .gr_gating_live_panel import GrGatingLivePanel

        self.gr_gating_live_panel = GrGatingLivePanel(self.app)
        with dpg.collapsing_header(label="Live GR power gating", tag="reg_gr_gating_live_header"):
            self.gr_gating_live_panel.build()
        with dpg.collapsing_header(label="Power/current arbitration"):
            dpg.add_text(
                "Compare each request with host votes, firmware votes, and the effective limit. "
                "Firmware values can lag a recent change; refresh to check again. "
                "Client numbers have not been assigned physical names.",
                wrap=self.app.s(900),
            )
            dpg.add_button(label="Read arbitration", callback=self.refresh_arbitration)
            dpg.add_input_text(tag="reg_arbitration", readonly=True, multiline=True, width=-1, height=self.app.s(260))
        with dpg.collapsing_header(label="Runtime voltage-request control"):
            dpg.add_text(
                "Allow or suppress the driver's voltage-request processing. Changes take effect immediately. "
                "This switch does not set a voltage or raise a voltage ceiling.",
                wrap=self.app.s(900),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Read voltage-request gate", callback=self.refresh_voltage_gate)
                dpg.add_checkbox(label="Allow voltage requests", tag="reg_voltage_gate_value", enabled=False)
                dpg.add_button(
                    label="Apply gate now",
                    tag="reg_voltage_gate_apply",
                    callback=self.apply_voltage_gate,
                    enabled=False,
                )
                dpg.add_button(
                    label="Restore initial gate",
                    tag="reg_voltage_gate_restore",
                    callback=self.restore_voltage_gate,
                    enabled=False,
                )
            dpg.add_text(
                "Read the current gate before changing it.", tag="reg_voltage_gate_status", wrap=self.app.s(900)
            )
        with dpg.collapsing_header(label="Runtime VF policy control"):
            dpg.add_text(
                "Change the driver's active VF checks without restarting the GPU. "
                "Some startup modes reject disabling a check; use the registry setting first in that case. "
                "Live changes retain the startup processing mode and apply to this driver session. "
                "Physical firmware enforcement has not been measured.",
                wrap=self.app.s(900),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Read VF policies", callback=self.refresh_clock_vf_live)
                dpg.add_checkbox(label="Enforce monotonicity", tag="reg_vf_live_monotonicity", enabled=False)
                dpg.add_checkbox(label="Enforce smoothing", tag="reg_vf_live_smoothening", enabled=False)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Apply policies now",
                    tag="reg_vf_live_apply",
                    callback=self.apply_clock_vf_live,
                    enabled=False,
                )
                dpg.add_button(
                    label="Restore initial policies",
                    tag="reg_vf_live_restore",
                    callback=self.restore_clock_vf_live,
                    enabled=False,
                )
            dpg.add_text(
                "Read the policies before changing them. Restore initial policies before registry Undo.",
                tag="reg_vf_live_status",
                wrap=self.app.s(900),
            )
        from .voltage_offsets_panel import VoltageOffsetsPanel

        self.voltage_offsets_panel = VoltageOffsetsPanel(self.app)
        self.voltage_offsets_panel.build()
        from .voltage_requests_panel import VoltageRequestsPanel

        self.voltage_requests_panel = VoltageRequestsPanel(self.app)
        self.voltage_requests_panel.build()
        with dpg.collapsing_header(label="Live REL equation bounds", tag="reg_rel_live_header"):
            dpg.add_text(
                "Change the loaded REL equation bounds for this driver session without restarting the GPU. "
                "Fix both bounds uses the entered value for both bounds; linked equations can still affect REL and ALT. "
                "These are equation settings, not measured rail voltage.",
                wrap=self.app.s(900),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Read", callback=self.refresh_rel_live)
                dpg.add_combo(
                    [], tag="reg_rel_live_select", width=self.app.s(220), enabled=False, callback=self.select_rel_live
                )
                dpg.add_input_text(
                    label="Bound value (mV)", tag="reg_rel_live_value", width=self.app.s(130), enabled=False
                )
                dpg.add_button(label="Apply upper", tag="reg_rel_live_apply", callback=self.apply_rel_live, enabled=False)
                dpg.add_button(
                    label="Restore initial upper",
                    tag="reg_rel_live_restore",
                    callback=self.restore_rel_live,
                    enabled=False,
                )
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Fix both bounds", tag="reg_rel_live_fixed", callback=self.fix_rel_live, enabled=False
                )
                dpg.add_button(
                    label="Restore BIOS bounds", tag="reg_rel_live_restore_bounds",
                    callback=self.restore_rel_live_bounds, enabled=False,
                )
            dpg.add_text("Read live bounds before changing them.", tag="reg_rel_live_status", wrap=self.app.s(900))
            dpg.add_input_text(
                tag="reg_rel_live_inventory", readonly=True, multiline=True, width=-1, height=self.app.s(110)
            )
        with dpg.collapsing_header(label="Live voltage constants", tag="reg_constants_live_header"):
            dpg.add_text(
                "Change an OV or VMIN constant, or an ALT addend, for this driver session without restarting the GPU. "
                "Reported policy values can differ. Physical enforcement has not been measured.",
                wrap=self.app.s(900),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Read", callback=self.refresh_constants_live)
                dpg.add_combo([], tag="reg_constants_live_select", width=self.app.s(240), enabled=False,
                              callback=self.select_constants_live)
                dpg.add_input_text(label="Value (mV)", tag="reg_constants_live_value",
                                   width=self.app.s(130), enabled=False)
                dpg.add_button(label="Apply now", tag="reg_constants_live_apply",
                               callback=self.apply_constants_live, enabled=False)
                dpg.add_button(label="Restore initial value", tag="reg_constants_live_restore",
                               callback=self.restore_constants_live, enabled=False)
            dpg.add_text("Read live values before changing them.", tag="reg_constants_live_status", wrap=self.app.s(900))
            dpg.add_input_text(tag="reg_constants_live_inventory", readonly=True, multiline=True, width=-1,
                               height=self.app.s(130))
        with dpg.collapsing_header(label="Voltage switch-delay requests"):
            dpg.add_text(
                "Read and change a voltage device's switch-delay request in microseconds. "
                "DEFAULT and IPC_VMIN are separate operations. Changes apply to this driver session "
                "without a GPU restart. Physical switching latency has not been measured; "
                "the separate power-up/down delays are not controlled here.",
                wrap=self.app.s(900),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Read device delays", callback=self.refresh_voltage_devices)
                dpg.add_combo(
                    [],
                    tag="reg_volt_device_select",
                    width=self.app.s(310),
                    enabled=False,
                    callback=self.select_voltage_device,
                )
                dpg.add_input_text(
                    label="Delay request (us)",
                    tag="reg_volt_device_delay",
                    width=self.app.s(130),
                    enabled=False,
                )
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Apply delay now",
                    tag="reg_volt_device_apply",
                    callback=self.apply_voltage_device_delay,
                    enabled=False,
                )
                dpg.add_button(
                    label="Restore initial delay",
                    tag="reg_volt_device_restore",
                    callback=self.restore_voltage_device_delay,
                    enabled=False,
                )
            dpg.add_text("Read devices before changing a delay.", tag="reg_volt_device_status", wrap=self.app.s(900))
            dpg.add_input_text(
                tag="reg_volt_device_inventory",
                readonly=True,
                multiline=True,
                width=-1,
                height=self.app.s(100),
            )
        self.recover()

    @staticmethod
    def _format_difr(observation):
        lines = ["Loaded P-state to LPWR mapping:"]
        for row in observation["pstates"]["rows"]:
            states = "/".join(f"P{i}" for i in range(32) if row["pstate_id_mask"] >> i & 1)
            selector = "FF (unassigned)" if row["lpwr_index"] == 0xFF else str(row["lpwr_index"])
            lines.append(f"  {states}: LPWR selector {selector}")
        lines.append("Configured support and P-state masks:")
        for key, label in _DIFR_LABELS.items():
            row = observation["controllers"][key]
            if not row["available"]:
                detail = "support readback unavailable"
            elif row["supported"] is False:
                detail = "support no; state mask unavailable"
            elif not row["states_available"]:
                detail = "support yes; state mask unavailable"
            else:
                mask = row["configured_pstate_mask"]
                states = "/".join(f"P{i}" for i in range(32) if mask >> i & 1) or "none"
                detail = f"support yes; configured states {states} (0x{mask:X})"
            lines.append(f"  {label}: {detail}")
        return "\n".join(lines)

    def refresh_difr(self, *unused):
        from . import lpwr_runtime

        try:
            observation, error = lpwr_runtime.read(self.app.gpu)
        except Exception as exc:
            observation, error = None, str(exc)
        if error or observation is None:
            message = "DIFR configuration read unavailable: " + str(error or "No observation")
            prior = self.difr_readback
            if prior and prior["identity"]["slot"] == self.app.gpu.slot():
                message += "\nPrevious read; refresh failed:\n" + self._format_difr(prior)
            else:
                self.difr_readback = None
            dpg.set_value("reg_difr_readback", message)
            self.app.log(message.split("\n", 1)[0], False)
            return
        self.difr_readback = observation
        dpg.set_value("reg_difr_readback", self._format_difr(observation))

    @staticmethod
    def _rel_live_key(snapshot, row):
        identity = snapshot["identity"]
        return (tuple(sorted(identity.items())), snapshot["image_sha256"], row["rail"], row["equation"])

    @staticmethod
    def _rel_mv(value):
        return format(Decimal(value) / 1000, "f")

    @staticmethod
    def _parse_live_mv(text):
        amount = Decimal(text.strip()) * 1000
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError("not a whole microvolt")
        return int(amount)

    @staticmethod
    def _constants_live_key(snapshot, row):
        return (tuple(sorted(snapshot["identity"].items())), snapshot["image_sha256"],
                row["rail"], row["role"], row["equation"])

    @staticmethod
    def _constants_live_role(role):
        return {"ALT": "ALT addend", "OV": "OV constant", "VMIN": "VMIN constant"}.get(role, role)

    def _constants_live_failed(self, message):
        self.constants_live_snapshot = None
        self.constants_live_choices = {}
        self.constants_live_stale = False
        for tag in ("reg_constants_live_select", "reg_constants_live_value", "reg_constants_live_apply",
                    "reg_constants_live_restore"):
            dpg.configure_item(tag, enabled=False)
        dpg.set_value("reg_constants_live_status", message)
        dpg.set_value("reg_constants_live_inventory", "")
        self.app.log(message, False)

    def _show_constants_live(self, snapshot, prefix=""):
        self.constants_live_snapshot = snapshot
        self.constants_live_stale = False
        self.constants_live_choices = {}
        lines = []
        for row in snapshot["rows"]:
            role = self._constants_live_role(row["role"])
            label = f"{row['label']} (rail {row['rail']}) {role}"
            if row["status"] == "ready":
                self.constants_live_choices[label] = row
                self.app._constants_live_initial_states.setdefault(self._constants_live_key(snapshot, row), row["value_uv"])
                reported = row.get("reported_uv")
                reported_text = f"{self._rel_mv(reported)} mV" if reported is not None else "unavailable"
                detail = (f"{label}: current {self._rel_mv(row['value_uv'])} mV; "
                          f"stored {self._rel_mv(row['stored_value_uv'])} mV; "
                          f"reported {row['role']} {reported_text}")
                if row["role"] == "VMIN" and row.get("maximum_uv") is not None:
                    detail += f"; current maximum VMIN input {self._rel_mv(row['maximum_uv'])} mV"
                lines.append(detail)
            else:
                lines.append(f"{label}: {row['status']}; " + "; ".join(row.get("issues", [])))
        unavailable = snapshot.get("unavailable_peers", [])
        if unavailable:
            lines.append("Optional readbacks unavailable: " + ", ".join(unavailable) + ".")
        inventory = "\n".join(lines) or "No values returned."
        dpg.set_value("reg_constants_live_inventory", inventory)
        if not self.constants_live_choices:
            self._constants_live_failed(prefix + "No supported live values are available.")
            dpg.set_value("reg_constants_live_inventory", inventory)
            return
        previous = dpg.get_value("reg_constants_live_select")
        selected = previous if previous in self.constants_live_choices else next(iter(self.constants_live_choices))
        dpg.configure_item("reg_constants_live_select", items=list(self.constants_live_choices), enabled=True)
        dpg.set_value("reg_constants_live_select", selected)
        self.select_constants_live()
        if prefix:
            dpg.set_value("reg_constants_live_status", prefix + dpg.get_value("reg_constants_live_status"))

    def select_constants_live(self, *unused):
        row = self.constants_live_choices.get(dpg.get_value("reg_constants_live_select"))
        if row is None or self.constants_live_snapshot is None:
            self._constants_live_failed("Read live values for the selected adapter first.")
            return
        dpg.set_value("reg_constants_live_value", self._rel_mv(row["value_uv"]))
        dpg.configure_item("reg_constants_live_value", enabled=True)
        dpg.configure_item("reg_constants_live_apply", enabled=True)
        initial = self.app._constants_live_initial_states[self._constants_live_key(self.constants_live_snapshot, row)]
        dpg.configure_item("reg_constants_live_restore", enabled=row["value_uv"] != initial)
        role = self._constants_live_role(row["role"])
        dpg.set_value("reg_constants_live_status", ("Previous read is stale. " if self.constants_live_stale else "")
                      + f"{row['label']} {role}: {self._rel_mv(row['value_uv'])} mV.")

    def refresh_constants_live(self, *unused):
        from .voltage_constants_live import VoltageConstantsController

        same = (self.constants_live_control is not None and self.constants_live_control.gpu is self.app.gpu
                and self.constants_live_snapshot is not None
                and self.constants_live_snapshot["identity"]["slot"] == self.app.gpu.slot())
        try:
            if self.constants_live_control is None or self.constants_live_control.gpu is not self.app.gpu:
                self.constants_live_snapshot = None
                self.constants_live_control = VoltageConstantsController(self.app.gpu)
            self._show_constants_live(self.constants_live_control.read())
        except Exception as exc:
            message = "Live value read unavailable: " + str(exc)
            if same and getattr(exc, "kind", None) == "transient":
                self.constants_live_stale = True
                dpg.set_value("reg_constants_live_status", message + " Previous read is stale; refresh to verify.")
                self.app.log(message, False)
            else:
                self._constants_live_failed(message)

    def _set_constants_live(self, value):
        if not self.app.guard():
            return
        row = self.constants_live_choices.get(dpg.get_value("reg_constants_live_select"))
        if (self.constants_live_control is None or self.constants_live_control.gpu is not self.app.gpu
                or self.constants_live_snapshot is None or row is None
                or self.constants_live_snapshot["identity"]["slot"] != self.app.gpu.slot()):
            self._constants_live_failed("Read live values for the selected adapter first.")
            return
        try:
            self._show_constants_live(self.constants_live_control.set(row["rail"], row["role"], value), "Applied live. ")
            self.app.log("Live voltage value applied.", True)
        except Exception as exc:
            message = "Live value change failed: " + str(exc)
            rollback = getattr(exc, "rollback", None)
            if not getattr(exc, "attempted", False):
                if getattr(exc, "kind", None) == "identity":
                    self._constants_live_failed(message)
                else:
                    dpg.set_value("reg_constants_live_status", message)
                    self.app.log(message, False)
            elif isinstance(rollback, dict) and rollback.get("verified") and rollback.get("readback"):
                self._show_constants_live(rollback["readback"], message + " Original value restored. ")
                self.app.log(message, False)
            else:
                self._constants_live_failed(message + " Recovery required; read and verify the current state.")

    def apply_constants_live(self, *unused):
        try:
            value = self._parse_live_mv(dpg.get_value("reg_constants_live_value"))
        except (AttributeError, TypeError, ValueError, InvalidOperation):
            dpg.set_value("reg_constants_live_status", "Enter a finite mV value with whole-microvolt precision.")
            return
        self._set_constants_live(value)

    def restore_constants_live(self, *unused):
        row = self.constants_live_choices.get(dpg.get_value("reg_constants_live_select"))
        if row is not None and self.constants_live_snapshot is not None:
            value = self.app._constants_live_initial_states.get(self._constants_live_key(self.constants_live_snapshot, row))
            if value is not None:
                self._set_constants_live(value)

    def _rel_live_failed(self, message):
        self.rel_live_snapshot = None
        self.rel_live_choices = {}
        self.rel_live_stale = False
        for tag in ("reg_rel_live_select", "reg_rel_live_value", "reg_rel_live_apply", "reg_rel_live_restore",
                    "reg_rel_live_fixed", "reg_rel_live_restore_bounds"):
            dpg.configure_item(tag, enabled=False)
        dpg.set_value("reg_rel_live_status", message)
        dpg.set_value("reg_rel_live_inventory", "")
        self.app.log(message, False)

    def _show_rel_live(self, snapshot, prefix=""):
        self.rel_live_snapshot = snapshot
        self.rel_live_stale = False
        self.rel_live_choices = {}
        lines = []
        for row in snapshot["rows"]:
            label = f"{row['label']} (rail {row['rail']})"
            if row["status"] == "ready":
                self.rel_live_choices[label] = row
                self.app._rel_live_initial_states.setdefault(self._rel_live_key(snapshot, row), row["upper_uv"])
                policy = row.get("policy_uv") or {}
                parts = [
                    f"{name}: {self._rel_mv(policy[name])} mV"
                    if policy.get(name) is not None
                    else f"{name}: unavailable"
                    for name in ("reliability", "alt_reliability", "effective")
                ]
                lines.append(
                    f"{label}: loaded bounds {self._rel_mv(row['lower_uv'])} to {self._rel_mv(row['upper_uv'])} mV; "
                    f"stored bounds {self._rel_mv(row['stored_lower_uv'])} to "
                    f"{self._rel_mv(row['stored_upper_uv'])} mV; " + ", ".join(parts)
                )
                if row.get("fixed_status") != "ready":
                    lines.append("  Fixed bounds unavailable: " + "; ".join(row.get("fixed_issues", [])))
            else:
                lines.append(f"{label}: {row['status']}; " + "; ".join(row.get("issues", [])))
        unavailable = snapshot.get("unavailable_peers", [])
        if unavailable:
            lines.append("Optional readbacks unavailable: " + ", ".join(unavailable) + ".")
        dpg.set_value("reg_rel_live_inventory", "\n".join(lines) or "No rails returned.")
        if not self.rel_live_choices:
            self._rel_live_failed(prefix + "No supported live bounds are available.")
            dpg.set_value("reg_rel_live_inventory", "\n".join(lines) or "No rails returned.")
            return
        previous = dpg.get_value("reg_rel_live_select")
        selected = previous if previous in self.rel_live_choices else next(iter(self.rel_live_choices))
        dpg.configure_item("reg_rel_live_select", items=list(self.rel_live_choices), enabled=True)
        dpg.set_value("reg_rel_live_select", selected)
        self.select_rel_live()
        if prefix:
            dpg.set_value("reg_rel_live_status", prefix + dpg.get_value("reg_rel_live_status"))

    def select_rel_live(self, *unused):
        row = self.rel_live_choices.get(dpg.get_value("reg_rel_live_select"))
        if row is None or self.rel_live_snapshot is None:
            self._rel_live_failed("Read live bounds for the selected adapter first.")
            return
        dpg.set_value("reg_rel_live_value", self._rel_mv(row["upper_uv"]))
        dpg.configure_item("reg_rel_live_value", enabled=True)
        dpg.configure_item("reg_rel_live_apply", enabled=True)
        dpg.configure_item("reg_rel_live_fixed", enabled=row.get("fixed_status") == "ready")
        dpg.configure_item(
            "reg_rel_live_restore_bounds",
            enabled=row.get("fixed_status") == "ready" and
            (row["lower_uv"], row["upper_uv"]) != (row["stored_lower_uv"], row["stored_upper_uv"]),
        )
        initial = self.app._rel_live_initial_states[self._rel_live_key(self.rel_live_snapshot, row)]
        dpg.configure_item("reg_rel_live_restore", enabled=row["upper_uv"] != initial)
        dpg.set_value(
            "reg_rel_live_status",
            ("Previous read is stale. " if self.rel_live_stale else "")
            + f"{row['label']}: loaded bounds {self._rel_mv(row['lower_uv'])} "
            + f"to {self._rel_mv(row['upper_uv'])} mV.",
        )

    def refresh_rel_live(self, *unused):
        from .voltage_rel_live import VoltageRelController

        same = (
            self.rel_live_control is not None
            and self.rel_live_control.gpu is self.app.gpu
            and self.rel_live_snapshot is not None
            and self.rel_live_snapshot["identity"]["slot"] == self.app.gpu.slot()
        )
        try:
            if self.rel_live_control is None or self.rel_live_control.gpu is not self.app.gpu:
                self.rel_live_snapshot = None
                self.rel_live_control = VoltageRelController(self.app.gpu)
            self._show_rel_live(self.rel_live_control.read())
        except Exception as exc:
            message = "Live bound read unavailable: " + str(exc)
            if same and getattr(exc, "kind", None) == "transient":
                self.rel_live_stale = True
                dpg.set_value("reg_rel_live_status", message + " Previous read is stale; refresh to verify.")
                self.app.log(message, False)
            else:
                self._rel_live_failed(message)

    def _set_rel_live(self, value, *, operation="upper"):
        if not self.app.guard():
            return
        row = self.rel_live_choices.get(dpg.get_value("reg_rel_live_select"))
        if (
            self.rel_live_control is None
            or self.rel_live_control.gpu is not self.app.gpu
            or self.rel_live_snapshot is None
            or row is None
            or self.rel_live_snapshot["identity"]["slot"] != self.app.gpu.slot()
        ):
            self._rel_live_failed("Read live bounds for the selected adapter first.")
            return
        try:
            if operation == "fixed":
                snapshot = self.rel_live_control.set_fixed(row["rail"], value)
            elif operation == "restore_bounds":
                snapshot = self.rel_live_control.restore_bounds(row["rail"])
            else:
                snapshot = self.rel_live_control.set(row["rail"], value)
            self._show_rel_live(snapshot, "Applied live. ")
            self.app.log("Live REL equation bounds applied.", True)
        except Exception as exc:
            message = "Live bound change failed: " + str(exc)
            rollback = getattr(exc, "rollback", None)
            if not getattr(exc, "attempted", False):
                if getattr(exc, "kind", None) == "identity":
                    self._rel_live_failed(message)
                else:
                    dpg.set_value("reg_rel_live_status", message)
                    self.app.log(message, False)
            elif isinstance(rollback, dict) and rollback.get("verified") and rollback.get("readback"):
                self._show_rel_live(rollback["readback"], message + " Original bounds restored. ")
                self.app.log(message, False)
            else:
                self._rel_live_failed(message + " Recovery required; read and verify the current state.")

    def apply_rel_live(self, *unused):
        self._apply_rel_live_value("upper")

    def fix_rel_live(self, *unused):
        self._apply_rel_live_value("fixed")

    def _apply_rel_live_value(self, operation):
        try:
            value = self._parse_live_mv(dpg.get_value("reg_rel_live_value"))
        except (AttributeError, TypeError, ValueError, InvalidOperation):
            dpg.set_value("reg_rel_live_status", "Enter a finite mV value with whole-microvolt precision.")
            return
        self._set_rel_live(value, operation=operation)

    def restore_rel_live_bounds(self, *unused):
        self._set_rel_live(None, operation="restore_bounds")

    def restore_rel_live(self, *unused):
        row = self.rel_live_choices.get(dpg.get_value("reg_rel_live_select"))
        if row is not None and self.rel_live_snapshot is not None:
            value = self.app._rel_live_initial_states.get(self._rel_live_key(self.rel_live_snapshot, row))
            if value is not None:
                self._set_rel_live(value)

    @staticmethod
    def _voltage_device_key(snapshot, row):
        identity = snapshot["identity"]
        return tuple(identity[name] for name in ("slot", "architecture", "device_id", "subsystem_id")) + (
            row["index"],
            row["voltage_domain"],
            row["operation_type"],
        )

    def _voltage_devices_failed(self, message):
        self.voltage_device_snapshot = None
        for tag in (
            "reg_volt_device_select",
            "reg_volt_device_delay",
            "reg_volt_device_apply",
            "reg_volt_device_restore",
        ):
            dpg.configure_item(tag, enabled=False)
        dpg.set_value("reg_volt_device_status", message)
        self.app.log(message, False)

    def _show_voltage_devices(self, snapshot, prefix=""):
        from .voltage_device_runtime import DOMAIN_LABELS, OPERATION_LABELS

        self.voltage_device_snapshot = snapshot
        self.voltage_device_choices = {}
        lines = []
        for row in snapshot["rows"]:
            if row["status"] == "ready":
                label = f"{row['index']}: {DOMAIN_LABELS[row['voltage_domain']]} / {OPERATION_LABELS[row['operation_type']]}"
                self.voltage_device_choices[label] = row
                self.app._voltage_device_initial_states.setdefault(
                    self._voltage_device_key(snapshot, row), row["switch_delay_us"]
                )
                lines.append(f"Device {label}: {row['switch_delay_us']} us request")
            else:
                lines.append(f"Device {row['index']}, type {row['type']}: {row['status']}; " + "; ".join(row["issues"]))
        dpg.set_value("reg_volt_device_inventory", "\n".join(lines) or "No voltage devices returned.")
        if not self.voltage_device_choices:
            self._voltage_devices_failed(
                prefix + "No supported switch-delay requests are available. Read again to retry."
            )
            return
        old = dpg.get_value("reg_volt_device_select")
        selected = old if old in self.voltage_device_choices else next(iter(self.voltage_device_choices))
        dpg.configure_item("reg_volt_device_select", items=list(self.voltage_device_choices), enabled=True)
        dpg.set_value("reg_volt_device_select", selected)
        self.select_voltage_device()
        if prefix:
            dpg.set_value("reg_volt_device_status", prefix + dpg.get_value("reg_volt_device_status"))

    def select_voltage_device(self, *unused):
        row = self.voltage_device_choices.get(dpg.get_value("reg_volt_device_select"))
        if self.voltage_device_snapshot is None or row is None:
            self._voltage_devices_failed("Read the selected adapter's voltage devices before changing a delay.")
            return
        value = row["switch_delay_us"]
        dpg.set_value("reg_volt_device_delay", str(value))
        dpg.configure_item("reg_volt_device_delay", enabled=True)
        dpg.configure_item("reg_volt_device_apply", enabled=True)
        initial = self.app._voltage_device_initial_states[self._voltage_device_key(self.voltage_device_snapshot, row)]
        dpg.configure_item("reg_volt_device_restore", enabled=value != initial)
        dpg.set_value("reg_volt_device_status", f"Device {row['index']}: {value} us reported request.")

    def refresh_voltage_devices(self, *unused):
        from .voltage_device_runtime import VoltageDeviceController

        try:
            if self.voltage_device_control is None or self.voltage_device_control.gpu is not self.app.gpu:
                self.voltage_device_snapshot = None
                self.voltage_device_control = VoltageDeviceController(self.app.gpu)
            self._show_voltage_devices(self.voltage_device_control.read())
        except Exception as exc:
            self._voltage_devices_failed("Device delay read unavailable: " + str(exc))

    def _set_voltage_device_delay(self, value):
        if not self.app.guard():
            return
        row = self.voltage_device_choices.get(dpg.get_value("reg_volt_device_select"))
        if (
            self.voltage_device_control is None
            or self.voltage_device_control.gpu is not self.app.gpu
            or self.voltage_device_snapshot is None
            or row is None
            or self.voltage_device_snapshot["identity"]["slot"] != self.app.gpu.slot()
        ):
            self._voltage_devices_failed("Read the selected adapter's voltage devices before changing a delay.")
            return
        try:
            snapshot = self.voltage_device_control.set(
                row["index"],
                value,
                voltage_domain=row["voltage_domain"],
                operation_type=row["operation_type"],
            )
            self._show_voltage_devices(snapshot, "Applied live. ")
            self.app.log(f"Applied device {row['index']} switch-delay request: {value} us.", True)
        except Exception as exc:
            message = "Device delay change failed: " + str(exc)
            rollback = getattr(exc, "rollback", None)
            if isinstance(rollback, dict) and rollback.get("verified") and rollback.get("readback"):
                self._show_voltage_devices(rollback["readback"], message + " Original requests restored. ")
                self.app.log(message, False)
            else:
                self._voltage_devices_failed(message)

    def apply_voltage_device_delay(self, *unused):
        try:
            value = int(dpg.get_value("reg_volt_device_delay").strip(), 10)
            if not 1 <= value <= 0xFFFFFFFF:
                raise ValueError("outside positive uint32 range")
        except (AttributeError, TypeError, ValueError):
            message = "Enter a whole microsecond request from 1 to 4294967295. This is the request format, not a measured timing range."
            dpg.set_value("reg_volt_device_status", message)
            self.app.log(message, False)
            return
        self._set_voltage_device_delay(value)

    def restore_voltage_device_delay(self, *unused):
        row = self.voltage_device_choices.get(dpg.get_value("reg_volt_device_select"))
        if self.voltage_device_snapshot is not None and row is not None:
            initial = self.app._voltage_device_initial_states.get(
                self._voltage_device_key(self.voltage_device_snapshot, row)
            )
            if initial is not None:
                self._set_voltage_device_delay(initial)

    def _show_clock_vf_live(self, snapshot, prefix=""):
        self.clock_vf_snapshot = snapshot
        values = {key: snapshot[member] for key, member, _ in _VF_LIVE_FIELDS}
        for key, _, tag in _VF_LIVE_FIELDS:
            dpg.set_value(tag, values[key])
            dpg.configure_item(tag, enabled=True)
        dpg.configure_item("reg_vf_live_apply", enabled=True)
        dpg.configure_item("reg_vf_live_restore", enabled=values != self.clock_vf_initial)
        state = ", ".join(
            f"{label}: {'on' if values[key] else 'off'}"
            for key, label in (("clock_vf.monotonicity", "Monotonicity"), ("clock_vf.smoothening", "smoothing"))
        )
        dpg.set_value("reg_vf_live_status", prefix + state + ". Startup mode and registry settings are retained.")

    def _clock_vf_live_failed(self, message):
        self.clock_vf_snapshot = None
        for tag in ("reg_vf_live_monotonicity", "reg_vf_live_smoothening", "reg_vf_live_apply", "reg_vf_live_restore"):
            dpg.configure_item(tag, enabled=False)
        dpg.set_value("reg_vf_live_status", message)
        self.app.log(message, False)

    def refresh_clock_vf_live(self, *unused):
        from .clock_vf_runtime import ClockVfController

        try:
            if self.clock_vf_control is None or self.clock_vf_control.gpu is not self.app.gpu:
                self.clock_vf_snapshot = None
                self.clock_vf_initial = None
                self.clock_vf_control = ClockVfController(self.app.gpu)
            snapshot = self.clock_vf_control.read()
            identity = snapshot["identity"]
            key = tuple(identity[name] for name in ("slot", "architecture", "device_id", "subsystem_id"))
            initial = {name: snapshot[member] for name, member, _ in _VF_LIVE_FIELDS}
            self.clock_vf_initial = self.app._clock_vf_initial_states.setdefault(key, initial)
            self._show_clock_vf_live(snapshot)
        except Exception as exc:
            self._clock_vf_live_failed("VF policy read unavailable: " + str(exc))

    def _set_clock_vf_live(self, changes):
        if not self.app.guard():
            return
        if (
            self.clock_vf_control is None
            or self.clock_vf_control.gpu is not self.app.gpu
            or self.clock_vf_snapshot is None
            or self.clock_vf_snapshot["identity"]["slot"] != self.app.gpu.slot()
        ):
            self._clock_vf_live_failed("Read the selected adapter's VF policies before changing them.")
            return
        try:
            snapshot = self.clock_vf_control.set(changes)
            self._show_clock_vf_live(snapshot, "Applied live. ")
            self.invalidate()
            self.app.log("Applied live VF policies; startup mode and registry settings retained.", True)
        except Exception as exc:
            message = "Live VF policy change failed: " + str(exc)
            rollback = getattr(exc, "rollback", None)
            if isinstance(rollback, dict) and rollback.get("verified") and rollback.get("readback"):
                self._show_clock_vf_live(rollback["readback"], message + " Original policies restored. ")
                self.app.log(message, False)
            else:
                self.invalidate()
                self._clock_vf_live_failed(message)

    def apply_clock_vf_live(self, *unused):
        self._set_clock_vf_live({name: dpg.get_value(tag) for name, _, tag in _VF_LIVE_FIELDS})

    def restore_clock_vf_live(self, *unused):
        if self.clock_vf_initial is not None:
            self._set_clock_vf_live(dict(self.clock_vf_initial))

    def _show_voltage_gate(self, snapshot, prefix=""):
        self.voltage_gate_snapshot = snapshot
        enabled = snapshot["enabled"]
        dpg.set_value("reg_voltage_gate_value", enabled)
        dpg.configure_item("reg_voltage_gate_value", enabled=True)
        dpg.configure_item("reg_voltage_gate_apply", enabled=True)
        dpg.configure_item("reg_voltage_gate_restore", enabled=enabled is not self.voltage_gate_initial)
        dpg.set_value(
            "reg_voltage_gate_status", prefix + "Driver voltage requests: " + ("allowed." if enabled else "suppressed.")
        )

    def _voltage_gate_failed(self, message):
        self.voltage_gate_snapshot = None
        self.voltage_gate = None
        for tag in ("reg_voltage_gate_value", "reg_voltage_gate_apply", "reg_voltage_gate_restore"):
            dpg.configure_item(tag, enabled=False)
        dpg.set_value("reg_voltage_gate_status", message)
        self.app.log(message, False)

    def refresh_voltage_gate(self, *unused):
        try:
            if self.voltage_gate is None or self.voltage_gate.gpu is not self.app.gpu:
                self.voltage_gate_snapshot = None
                self.voltage_gate_initial = None
                self.voltage_gate = VoltageRequestGate(self.app.gpu)
            snapshot = self.voltage_gate.read()
            identity = snapshot["identity"]
            key = tuple(identity[name] for name in ("slot", "architecture", "device_id", "subsystem_id"))
            self.voltage_gate_initial = self.app._voltage_gate_initial_states.setdefault(key, snapshot["enabled"])
            self._show_voltage_gate(snapshot)
        except Exception as exc:
            self._voltage_gate_failed("Gate read unavailable: " + str(exc))

    def _set_voltage_gate(self, value):
        if not self.app.guard():
            return
        if (
            self.voltage_gate is None
            or self.voltage_gate.gpu is not self.app.gpu
            or self.voltage_gate_snapshot is None
            or self.voltage_gate_snapshot["identity"]["slot"] != self.app.gpu.slot()
        ):
            self._voltage_gate_failed("Read the selected adapter's gate before changing it.")
            return
        try:
            result = self.voltage_gate.set_enabled(value, expected=self.voltage_gate_snapshot["enabled"])
            self._show_voltage_gate(result["readback"], "Applied and verified without a GPU restart. ")
            self.app.log("Voltage-request gate applied and verified.", True)
        except Exception as exc:
            self._voltage_gate_failed("Gate change failed: " + str(exc))

    def apply_voltage_gate(self, *unused):
        self._set_voltage_gate(dpg.get_value("reg_voltage_gate_value"))

    def restore_voltage_gate(self, *unused):
        if self.voltage_gate_initial is not None:
            self._set_voltage_gate(self.voltage_gate_initial)

    def refresh_arbitration(self, *unused):
        target = (id(self.app.gpu), self.app.gpu.slot())
        if self.arbitration_target != target:
            self.arbitration = None
            self.arbitration_target = target
        try:
            report, error = self.app.gpu.read_power_arbitration()
        except Exception as exc:
            report, error = None, str(exc)
        lines = []
        if report is not None and report["available"] and not error:
            self.arbitration = report
        else:
            lines.append("Arbitration read unavailable: " + (error or "No decoded result"))
            if self.arbitration is not None:
                lines.append("Previous successful read follows; these values are historical.")
            report = self.arbitration
        if report is not None:
            lines.extend(self.arbitration_lines(report))
        dpg.set_value("reg_arbitration", "\n".join(lines))

    @staticmethod
    def arbitration_lines(report):
        def amount(value, unit):
            if value == 0xFFFFFFFF:
                return "0xFFFFFFFF (sentinel)"
            if unit in ("mW", "mA"):
                return f"{value / 1000:g} " + ("W" if unit == "mW" else "A")
            return f"{value} (raw units)"

        lines = [
            "Adapter: " + report["identity"]["slot"],
            "Read sequentially; firmware votes and effective limits may still be updating after a change.",
        ]
        for row in report["policies"]:
            lines.append(f"Policy {row['policy']}: type {row['type']}, channel {row['channel']}")
            if not row["available"]:
                lines.append("  Unavailable: " + row["reason"])
                continue
            unit = row["unit"]
            lines.append(
                f"  Requested {amount(row['requested'], unit)}; effective {amount(row['effective'], unit)}; "
                f"adjustment {amount(row['delta'], unit)}"
            )
            for name, label in (("host", "Host"), ("pmu", "Firmware")):
                vector = row[name]
                if not vector["available"]:
                    lines.append(f"  {label}: unavailable ({vector['reason']})")
                    continue
                votes = (
                    ", ".join(f"client {item['client']}: {amount(item['value'], unit)}" for item in vector["inputs"])
                    or "no active inputs"
                )
                lines.append(f"  {label}: {vector['method']} -> {amount(vector['output'], unit)}; {votes}")
            lines.extend("  " + observation for observation in row["observations"])
        if not report["policies"]:
            lines.append("No registered power/current policies.")
        return lines

    def refresh_diagnostics(self, *unused):
        try:
            if self.diagnostics_reader is None or self.diagnostics_reader.gpu is not self.app.gpu:
                self.diagnostics_reader = PerfDiagnosticsReader(self.app.gpu)
            self.diagnostics = self.diagnostics_reader.read()
        except Exception as exc:
            dpg.set_value("reg_diagnostics", "Driver policy read failed: " + str(exc))
            self.app.log("Driver policy read failed: " + str(exc), False)
            return
        lines = []
        names = {
            "limits": "Performance-limit slots",
            "cf_controller": "Feedback controllers",
            "cf_policy": "Feedback policies",
        }
        for name, result in self.diagnostics["groups"].items():
            lines.append(names.get(name, name) + ": " + result["status"])
            snapshot = result.get("snapshot")
            if result.get("error"):
                lines.append("  " + result["error"])
            if snapshot is None:
                snapshot = result.get("last_valid")
                if snapshot:
                    lines.append("  Previous successful read follows; these values are historical.")
            if snapshot is None:
                continue
            lines.append(f"  Registered entries: {snapshot['present_count']}")
            for key, flag in snapshot.get("flags", {}).items():
                lines.append(f"  {key}: {flag['raw']} (raw flag)")
            for target in snapshot.get("registry_targets", []):
                value = (
                    "absent" if target["index"] is None else f"policy {target['index']}, raw flag {target['flag_raw']}"
                )
                lines.append(f"  {target['registry_name']} {target['field']}: {value}")
                if target.get("issue"):
                    lines.append("    " + target["issue"])
            for row in snapshot.get("entries", []):
                if row.get("driver_label"):
                    lines.append(f"  {row['index']}: {row['driver_label']}")
                if "role_id" in row:
                    indices = [str(i) for i in range(32) if row["controller_mask"] & (1 << i)]
                    lines.append(
                        f"  Policy {row['index']}: role {row['role_id']}, "
                        "controller selection " + (", ".join(indices) or "empty")
                    )
                for issue in row.get("issues", []):
                    lines.append(f"  Entry {row['index']}: {issue}")
            for issue in snapshot.get("issues", []):
                lines.append("  " + issue)
            lines.append("")
        dpg.set_value("reg_diagnostics", "\n".join(lines))

    def recover(self):
        try:
            slot = self.app.gpu.slot()
            result = getattr(self.app, "_recovery_result", None)
            show = (isinstance(result, dict) and isinstance(result.get("slot"), str)
                    and result["slot"].casefold() == slot.casefold() and isinstance(result.get("message"), str))
            dpg.configure_item("reg_last_recovery", show=show)
            dpg.set_value("reg_last_recovery", "Last device recovery (historical): " + result["message"] if show else "")
            self.active = overrides.recovery_receipt(slot)
            dpg.configure_item("reg_restore_button", enabled=self.active is not None)
            if self.active:
                self.status("Saved override transaction: " + self.active["state"])
        except Exception as exc:
            self.active = None
            dpg.configure_item("reg_restore_button", enabled=False)
            self.status("Could not discover saved undo: " + str(exc), False)

    def status(self, text, ok=True):
        dpg.set_value("reg_status", text)
        self.app.log(text, ok)

    @staticmethod
    def display(field):
        unit = field["units"]
        factor = 1000 if unit in ("mW", "mA", "uV", "kHz") else 1
        return factor, {"mW": "W", "mA": "A", "uV": "mV", "kHz": "MHz"}.get(unit, unit)

    def invalidate(self, *unused):
        self.preview = None
        dpg.configure_item("reg_apply_button", enabled=False)
        dpg.set_value("reg_preview", "Values staged in this window. Preview changes before applying.")

    def _startup_field(self, target):
        module = overrides.voltage_startup_runtime
        observation = module.read_checked(self.app.gpu)
        registry = overrides.WindowsRegistry(target).snapshot()
        entry = next(
            (row for name, row in registry.items() if name.casefold() == module.REGISTRY_NAME.casefold()), None
        )
        current = None
        if entry and entry["type"] == 4 and type(entry["value"]) is int and entry["value"] in (1, 2):
            current = entry["value"] == 1
        reason, editable = "", True
        try:
            module.from_observation(observation, {module.KEY: current if current is not None else False})
        except Exception as exc:
            reason, editable = str(exc), False
        key = "startup." + module.KEY
        return {
            "key": key,
            "label": "Startup REL adjustment",
            "group": "Driver behavior",
            "units": "selection",
            "current": current,
            "minimum": 0,
            "maximum": 1,
            "mask": 1,
            "capability": "editable" if editable else "read-only",
            "reason": reason,
            "choices": {
                "Leave unchanged": None,
                "Force -50 mV startup adjustment": True,
                "Suppress startup adjustment": False,
            },
            "detail": "Set the affected rails' REL offset during GPU startup. Other captured rail requests and boost "
            "are checked after replay. Undo restores the previous registry setting and verifies the original requests. "
            "If the driver cannot restore a captured runtime offset, Druta reports recovery required.",
        }

    def refresh(self, *unused):
        self.invalidate()
        self.recover()
        try:
            target = overrides.bind_target(self.app.gpu)
        except Exception as exc:
            self.status("Could not refresh adapter fields: " + str(exc), False)
            return
        errors = []
        try:
            image = overrides.read_vbios(self.app.gpu)
            inspection = vbios_fields.inspect(image)
            self.inspection = inspection
            self.fields = {
                key: field
                for key, field in self.fields.items()
                if key.startswith(("registry.", "flag.", "pstate.", "clockvf.", "startup."))
            }
            self.fields.update({field["key"]: field for field in inspection["fields"]})
        except Exception as exc:
            # Independent transient reads preserve previously established fields.
            errors.append("BIOS fields: " + str(exc))
        vfe, error = overrides.read_model_inputs(self.app.gpu)
        if error:
            errors.append("Model inputs: " + error)
        else:
            self.fields = {key: field for key, field in self.fields.items() if not key.startswith("registry.")}
            for item in vfe["inputs"]:
                name = item["registry_name"]
                editable = (
                    name in overrides._MODEL_VALUES
                    and item["cache_ready"]
                    and not item["signed"]
                    and sum(peer["registry_name"] == name for peer in vfe["inputs"]) == 1
                )
                reason = "" if editable else "A unique, ready unsigned model input is required."
                if editable and name in overrides.model_registry.IDDQ_NAMES:
                    try:
                        overrides.model_registry.contracts(vfe, {name: item["active"]})
                    except Exception as exc:
                        editable, reason = False, str(exc)
                key = "registry." + name
                self.fields[key] = {
                    "key": key,
                    "label": item["label"],
                    "group": "Silicon model inputs",
                    "units": "model value",
                    "current": item["active"],
                    "minimum": item["minimum"],
                    "maximum": item["maximum"],
                    "mask": None,
                    "capability": "editable" if editable else "read-only",
                    "reason": reason,
                    "detail": f"Raw hardware: {item['raw_hardware']}; driver fallback: {item['default']}. "
                    "This changes a software model input; hardware fuses remain unchanged."
                    + (
                        " IDDQ uses raw model integers; physical units are unresolved."
                        " Changing an IDDQ input may also change calculated voltage limits on linked rails."
                        if name in overrides.model_registry.IDDQ_NAMES
                        else ""
                    ),
                }
        flags, error = overrides.registry_flags.read(self.app.gpu)
        if error:
            errors.append("Driver flags: " + error)
        else:
            self.fields = {key: field for key, field in self.fields.items() if not key.startswith("flag.")}
            definitions = [
                (
                    "perf_limits.cache",
                    "cache_enabled",
                    "Cache limit-conversion results",
                    "Reuse cached performance-limit calculations. Disabling this forces recalculation.",
                ),
                (
                    "perf_limits.skip_updates",
                    "skip_updates",
                    "Skip driver performance-limit updates",
                    "Suppress host-initiated performance-limit updates. Firmware enforcement effects remain unverified.",
                ),
            ]
            for name, observation, label, detail in definitions:
                if observation not in flags:
                    continue
                current = flags[observation]
                reason = ""
                try:
                    overrides.registry_flags.contracts({name: current}, flags)
                    editable = True
                except Exception as exc:
                    editable, reason = False, str(exc)
                key = "flag." + name
                self.fields[key] = {
                    "key": key,
                    "label": label,
                    "group": "Driver behavior",
                    "units": "enabled",
                    "current": current,
                    "minimum": 0,
                    "maximum": 1,
                    "mask": 1,
                    "capability": "editable" if editable else "read-only",
                    "reason": reason,
                    "detail": detail + " The active flag and other decoded flags are checked after a GPU restart.",
                }
        pstates, error = overrides.all_pstates_registry.read(self.app.gpu)
        if error:
            errors.append("All-P-state editability: " + error)
        else:
            self.fields = {key: field for key, field in self.fields.items() if not key.startswith("pstate.")}
            current = pstates["enabled"]
            reason = ""
            try:
                overrides.all_pstates_registry.contracts({overrides.all_pstates_registry.KEY: current}, pstates)
                editable = type(current) is bool
            except Exception as exc:
                editable, reason = False, str(exc)
            key = "pstate." + overrides.all_pstates_registry.KEY
            self.fields[key] = {
                "key": key,
                "label": "Expose all-P-state overclocking editability",
                "group": "Driver behavior",
                "units": "enabled",
                "current": current,
                "minimum": 0,
                "maximum": 1,
                "mask": 1,
                "capability": "editable" if editable else "read-only",
                "reason": reason,
                "detail": "Enable the driver override for supported non-P0 clock domains. Clearing it removes the "
                "override and checks that driver-default editability is disabled. This does not set an offset; "
                "individual state setters require separate validation. GPU restart required.",
            }
        clock_vf, error = overrides.clock_vf_registry.read(self.app.gpu)
        if error:
            errors.append("VF policy flags: " + error)
        else:
            self.fields = {key: field for key, field in self.fields.items() if not key.startswith("clockvf.")}
            for name, member, label in (
                ("clock_vf.monotonicity", "vf_monotonicity_enforced", "Enforce VF curve monotonicity"),
                ("clock_vf.smoothening", "vf_smoothening_enforced", "Enforce VF curve smoothing"),
            ):
                current = clock_vf.get(member)
                reason = ""
                try:
                    overrides.clock_vf_registry.contracts({name: current}, clock_vf)
                    editable = type(current) is bool
                except Exception as exc:
                    editable, reason = False, str(exc)
                key = "clockvf." + name
                self.fields[key] = {
                    "key": key,
                    "label": label,
                    "group": "Driver behavior",
                    "units": "enabled",
                    "current": current,
                    "minimum": 0,
                    "maximum": 1,
                    "mask": 1,
                    "capability": "editable" if editable else "read-only",
                    "reason": reason,
                    "detail": "Change the driver's VF policy after a GPU restart. Disabling either check also "
                    "changes a derived VF processing mode. This does not write a curve or request a voltage. "
                    "The selected flags and other returned configuration fields are verified after apply and undo.",
                }
        try:
            field = self._startup_field(target)
            self.fields[field["key"]] = field
        except Exception as exc:
            if "startup." + overrides.voltage_startup_runtime.KEY in self.fields:
                errors.append("Startup REL adjustment: " + str(exc))
        self.inputs = {}
        self.invalidate()
        dpg.delete_item("reg_fields", children_only=True)
        groups = defaultdict(list)
        for field in self.fields.values():
            if field["key"].startswith("lpwr.difr."):
                field["detail"] = (
                    "Disable a captured enabled DIFR entry or its selected subfeatures in a separate registry transaction. "
                    "Apply and Restore check the loaded P-state routing, configured controller support and state masks. "
                    "An unsupported controller has an unavailable mask, not a zero mask. "
                    "Physical residency and power savings are unmeasured."
                )
            elif field["key"].startswith("lpwr.gr_pg."):
                field["detail"] = (
                    "Disable a captured enabled GR power-gating entry in a separate transaction. "
                    "Apply and Restore verify configured P-state eligibility and the predicted low-power controller changes. "
                    "A qualified removal of the last shared auxiliary contributor also verifies its available readbacks. "
                    "Preview admits understood dependency transitions and verifies the predicted related readbacks. "
                    "Physical residency and power savings are unmeasured."
                )
            groups[field["group"]].append(field)
        with dpg.group(parent="reg_fields"):
            dpg.add_text(
                "Experimental fields: BIOS bytes are verified after restart. "
                "Understood power/current policies, core/memory offset ranges, constant VMIN/OV and ALT addends, fan bounds/PWM/temperatures, operating temperature "
                "and the performance-limit flags are checked through the driver. "
                "Other fields still need separate runtime-effect validation.",
                wrap=self.app.s(900),
            )
            for name, fields in groups.items():
                with dpg.collapsing_header(label=name, default_open=False):
                    for field in fields:
                        factor, unit = self.display(field)
                        writable = field["capability"] == "editable" and (
                            field["current"] is not None or field.get("choices")
                        )
                        tag = "reg_field_" + field["key"]
                        with dpg.group(horizontal=True):
                            if writable and field.get("explicit_selection"):
                                dpg.add_checkbox(
                                    label="Set",
                                    tag=tag + "_select",
                                    default_value=False,
                                    callback=self.invalidate,
                                )
                            if field.get("choices") and writable:
                                label = next(
                                    name for name, value in field["choices"].items() if value is field["current"]
                                )
                                dpg.add_combo(
                                    list(field["choices"]),
                                    tag=tag,
                                    default_value=label,
                                    width=self.app.s(300),
                                    callback=self.invalidate,
                                )
                            elif field["mask"] is not None and writable:
                                dpg.add_checkbox(
                                    tag=tag, default_value=bool(field["current"]), callback=self.invalidate
                                )
                            elif writable:
                                dpg.add_input_double(
                                    tag=tag,
                                    default_value=field["current"] / factor,
                                    width=self.app.s(135),
                                    step=0,
                                    format="%.4f",
                                    callback=self.invalidate,
                                )
                            else:
                                dpg.add_text(
                                    "unavailable" if field["current"] is None else
                                    str(bool(field["current"])) if unit == "bool" else str(field["current"] / factor),
                                    tag=tag,
                                )
                            dpg.add_text(f"{field['label']} ({unit})")
                        if writable:
                            self.inputs[field["key"]] = (tag, factor)
                            with dpg.tooltip(tag):
                                dpg.add_text(
                                    (
                                        "Select an explicit startup setting.\n"
                                        if field.get("choices")
                                        else f"Stored: {field['current'] / factor:g} {unit}\n"
                                    )
                                    + "Driver and hardware behavior may impose additional limits.",
                                    wrap=self.app.s(420),
                                )
                        elif field.get("reason"):
                            dpg.add_text(field["reason"], wrap=self.app.s(850))
                        if field.get("detail"):
                            dpg.add_text(field["detail"], wrap=self.app.s(850))
            for diagnostic in (self.inspection or {}).get("diagnostics", []):
                dpg.add_text(f"{diagnostic['group']}: {diagnostic['message']}", wrap=self.app.s(850))
        dpg.configure_item("reg_preview_button", enabled=bool(self.inputs))
        dpg.configure_item("reg_restore_button", enabled=self.active is not None)
        suffix = f" Last transaction: {self.active['state']}." if self.active else ""
        if errors:
            suffix += " Previous values retained where refresh failed: " + "; ".join(errors)
        self.status(f"Read {len(self.fields)} fields from {target.slot}.{suffix}", not errors)

    def prepare(self, *unused):
        if not self.app.guard():
            return
        changes = {}
        for key, (tag, factor) in self.inputs.items():
            field = self.fields[key]
            explicit = field.get("explicit_selection", False)
            if explicit and not dpg.get_value(tag + "_select"):
                continue
            value = dpg.get_value(tag)
            if field.get("choices"):
                if value not in field["choices"]:
                    self.status("Read startup settings again before previewing.", False)
                    return
                value = field["choices"][value]
                if value is None:
                    continue
            if field["mask"] is None:
                value *= factor
                if float(value).is_integer():
                    value = int(value)
            if explicit or value != field["current"]:
                changes[key] = value
        if not changes:
            self.status("No fields changed.")
            return
        try:
            bios_changes = {
                key: value
                for key, value in changes.items()
                if not key.startswith(("registry.", "flag.", "pstate.", "clockvf.", "startup."))
            }
            named_changes = {
                key[len("registry.") :]: value for key, value in changes.items() if key.startswith("registry.")
            }
            flag_changes = {key[len("flag.") :]: value for key, value in changes.items() if key.startswith("flag.")}
            pstate_changes = {
                key[len("pstate.") :]: value for key, value in changes.items() if key.startswith("pstate.")
            }
            clock_vf_changes = {
                key[len("clockvf.") :]: value for key, value in changes.items() if key.startswith("clockvf.")
            }
            startup_changes = {
                key[len("startup.") :]: value for key, value in changes.items() if key.startswith("startup.")
            }
            if bios_changes:
                image = overrides.read_vbios(self.app.gpu)
                if vbios_fields.inspect(image)["sha256"] != self.inspection["sha256"]:
                    raise overrides.OverrideError("The running BIOS changed; read fields again before previewing")
            receipt = overrides.prepare(
                self.app.gpu,
                bios_changes,
                named_changes=named_changes,
                flag_changes=flag_changes,
                **({"pstate_changes": pstate_changes} if pstate_changes else {}),
                **({"clock_vf_changes": clock_vf_changes} if clock_vf_changes else {}),
                **({"startup_changes": startup_changes} if startup_changes else {}),
            )
        except Exception as exc:
            self.status("Could not prepare overrides: " + str(exc), False)
            return
        self.preview = receipt
        lines = []
        for key, value in changes.items():
            field = self.fields[key]
            factor, unit = self.display(field)
            if field.get("explicit_selection") and "upper_clamp_binding" in field:
                lower = field["upper_clamp_binding"]["lower_uv"] / factor
                lines.append(
                    f"{field['label']}: bounds {lower:g}..{field['current'] / factor:g} -> "
                    f"{value / factor:g}..{value / factor:g} {unit}"
                )
            elif field.get("choices"):
                old = next(name for name, state in field["choices"].items() if state is field["current"])
                new = next(name for name, state in field["choices"].items() if state is value)
                lines.append(f"{field['label']}: {old} -> {new}")
            elif field["mask"] is not None:
                old, new = ("enabled" if state else "disabled" for state in (field["current"], value))
                lines.append(f"{field['label']}: {old} -> {new}")
            else:
                lines.append(f"{field['label']}: {field['current'] / factor:g} -> {value / factor:g} {unit}")
        if any(value is False for value in clock_vf_changes.values()):
            lines.append("Disabling either VF check also changes a derived VF processing mode.")
        if receipt.get("lpwr_contracts"):
            contract = receipt["lpwr_contracts"]

            def difr_states(row):
                if not row["supported"]:
                    return "unavailable"
                mask = row["configured_pstate_mask"]
                return "/".join(f"P{i}" for i in range(32) if mask >> i & 1) or "none"

            def feature_text(values, controller):
                return f"0x{values[controller]:X}" if controller in values else "unavailable"

            outcomes = contract.get("after_alternatives") or [
                {"controllers": contract["after"], "features": contract.get("after_features")}
            ]
            if len(outcomes) > 1:
                lines.append("Driver dependencies allow either of these complete configured outcomes:")
            for number, outcome in enumerate(outcomes, 1):
                if len(outcomes) > 1:
                    lines.append(f"Possible outcome {number}:")
                for key, label in _DIFR_LABELS.items():
                    before = contract["before"]["controllers"][key]
                    after = outcome["controllers"][key]
                    support = "retained" if after["supported"] else "removed" if before["supported"] else "unavailable"
                    lines.append(
                        f"{label}: configured P-states {difr_states(before)} -> {difr_states(after)}; support {support}."
                    )
                    if outcome["features"] is not None:
                        old = feature_text(contract["before"]["features"], key)
                        new = feature_text(outcome["features"], key)
                        lines.append(f"  {label} configured features: {old} -> {new}.")
            lines.append(
                "DIFR: loaded P-state routing and configured DFPR/MS DIFR support and masks must match after GPU restart. "
                "Unsupported controllers have unavailable state masks. Restore verifies the original configuration. "
                "This checks configuration; physical residency and power savings remain unmeasured."
            )
        if receipt.get("ms_contracts"):
            contract = receipt["ms_contracts"]
            before_state = contract["before"]["core"] if contract.get("schema") == 4 else contract["before"]
            before = before_state["controllers"]["4"]["configured_pstate_mask"]
            after = contract["after"]["controllers"]["4"]

            def ms_states(mask):
                return "/".join(f"P{i}" for i in range(32) if mask >> i & 1) or "none"

            state = ms_states(after["configured_pstate_mask"]) if after["supported"] else "unavailable"
            support = "retained" if after["supported"] else "removed"
            lines.append(
                f"Memory clock gating: configured P-states {ms_states(before)} -> {state}; support {support}. "
                "Related low-power support and eligibility must also match after the GPU restart. "
                "Undo verifies the original configuration. Physical residency and power savings remain unmeasured."
            )
            if contract.get("schema") == 2:
                lines.append(
                    "The shared MS support query remains readable and reports false. "
                    "Available MS features are reduced; related DIFR support remains."
                )
            elif contract.get("schema") == 4:
                lines.append(
                    "Available MS features are reduced while the remaining mapped capability stays supported. "
                    "Its mapping, related support and all captured peers must match the preview."
                )
        if receipt.get("nvdec_contracts"):
            contract = receipt["nvdec_contracts"]
            before = contract["before"]["controllers"]["12"]["configured_pstate_mask"]
            after = contract["after"]["controllers"]["12"]

            def nvdec_states(mask):
                return "/".join(f"P{i}" for i in range(32) if mask >> i & 1) or "none"

            state = nvdec_states(after["configured_pstate_mask"]) if after["supported"] else "unavailable"
            support = "retained" if after["supported"] else "removed"
            lines.append(
                f"NVDEC low power: configured P-states {nvdec_states(before)} -> {state}; support {support}. "
                "Related low-power support and eligibility must also match after the GPU restart. "
                "Undo verifies the original configuration. Physical residency and power savings remain unmeasured."
            )
            if _type4_queries_become_unavailable(contract):
                lines.append(
                    "All support queries for this auxiliary group become unavailable; the selected controller's "
                    "unsupported reply and the other captured readbacks must match the preview."
                )
        if receipt.get("media_contracts"):
            contract = receipt["media_contracts"]
            family = contract["family"]
            controller = str({"nvenc": 13, "ofa": 14, "nvjpg": 15}[family])
            before = contract["before"]["controllers"][controller]["configured_pstate_mask"]
            after = contract["after"]["controllers"][controller]

            def media_states(mask):
                return "/".join(f"P{i}" for i in range(32) if mask >> i & 1) or "none"

            state = media_states(after["configured_pstate_mask"]) if after["supported"] else "unavailable"
            support = "retained" if after["supported"] else "removed"
            lines.append(
                f"{family.upper()} low power: configured P-states {media_states(before)} -> {state}; support {support}. "
                "Related low-power support and eligibility must also match after the GPU restart. "
                "Undo verifies the original configuration. Physical residency and power savings remain unmeasured."
            )
            if _type4_queries_become_unavailable(contract):
                lines.append(
                    "All support queries for this auxiliary group become unavailable; the selected controller's "
                    "unsupported reply and the other captured readbacks must match the preview."
                )
        if receipt.get("grpg_contracts"):
            contract = receipt["grpg_contracts"]
            before = contract["before"]["controllers"]["0"]["configured_pstate_mask"]

            def states(mask):
                return "/".join(f"P{i}" for i in range(32) if mask >> i & 1) or "none"

            if contract.get("schema") in (5, 10):
                cleared = {index for profile in contract["after_profiles"] for index in profile["cleared"]}
                optional = []
                if 2 in cleared:
                    optional.append("controller 2 (GR RG)")
                if 4 in cleared:
                    optional.append("controller 4 (MS CG)")
                possible_loss = (
                    " Depending on the accepted readback, " + " and ".join(optional) + " may also become unsupported."
                    if optional
                    else ""
                )
                lines.append(
                    f"GR power gating: main controller 0 support enabled -> unsupported; configured P-state "
                    f"eligibility {states(before)} -> unavailable.{possible_loss} "
                    "The readback must match an accepted support and configuration profile. "
                    "Restore verifies the original configuration."
                )
                if contract.get("schema") == 10:
                    profiles = contract["after_profiles"]
                    support_absent = [all(not row["available"] for row in p["bit1"]["roles"].values()) for p in profiles]
                    group_absent = [not p["main"]["bit4_group"]["1:0"]["available"] for p in profiles]
                    if any(support_absent):
                        lines.append(
                            "All support queries for one auxiliary group become unavailable."
                            if all(support_absent) else
                            "Depending on the accepted profile, all support queries for one auxiliary group may become unavailable."
                        )
                    if any(group_absent):
                        lines.append(
                            "The final shared capability group becomes unavailable."
                            if all(group_absent) else
                            "Depending on the accepted profile, the final shared capability group may become unavailable."
                        )
                    lines.append(
                        "The related auxiliary support, configured masks, features and all captured peers must match "
                        "one complete previewed profile. Physical residency and power savings remain unmeasured."
                    )
            else:
                after_controllers = contract["after"]["controllers"] if contract.get("schema") in (6, 7, 8, 9) else contract["after"]
                after = after_controllers["0"]["configured_pstate_mask"]
            support_queries_unavailable = (
                contract.get("schema") in (7, 8, 9)
                and all(not item["available"] for item in contract["after"]["bit1"]["roles"].values())
            )
            group_removed = (
                contract.get("schema") in (6, 7)
                and not contract["after"]["main"]["bit4_group"]["1:0"]["available"]
            )
            if contract.get("schema") in (8, 9):
                shared = (
                    "The selected auxiliary support queries all become unavailable; the main controller remains "
                    "supported and provides a separate configured-mask and feature readback. "
                    if support_queries_unavailable else
                    "The selected auxiliary role's support query becomes unavailable while another supported role "
                    "remains readable. "
                )
                shared += "Related support, configured masks, features and all captured peers must match the preview. "
            elif group_removed:
                shared = (
                    "The last contributor to a shared capability group will be removed. "
                    "All readbacks for that group become unavailable; the main controller remains supported "
                    "and provides a separate configuration readback. Other selected shared-capability changes "
                    "and all untouched peers must also match the preview. "
                )
            elif contract.get("schema") == 7:
                shared = (
                    "The last contributors to multiple shared auxiliary capabilities will be removed. "
                    "Their support, configured masks and controller features must match the preview; "
                    "the main controller remains supported. "
                )
            elif contract.get("schema") == 6:
                shared = (
                    "The last contributor to a shared auxiliary capability will be removed; "
                    "its support, configured P-state mask and controller feature must clear while "
                    "the main controller remains supported and another auxiliary role remains available. "
                )
            elif contract.get("schema") == 2:
                shared = (
                    "The last contributor to one shared auxiliary role will be removed. "
                    "Its support, configured P-state mask and controller feature must match the preview; "
                    "the other auxiliary roles remain unchanged. "
                )
            elif contract.get("schema") == 3:
                shared = (
                    "The last contributor to one shared auxiliary role will be removed. "
                    "Its support query must become unavailable while another supported role remains readable. "
                    "A separate per-P-state mask for this role is unavailable. "
                )
            elif contract.get("schema") == 4:
                shared = (
                    "The last contributors to two shared auxiliary roles will be removed. "
                    "One role's support, configured P-state mask and controller feature must match the preview. "
                    "The other role's support query must become unavailable while another supported role remains readable. "
                    "Both checks must pass; the other auxiliary roles remain unchanged. "
                )
            elif contract.get("schema") not in (5, 10):
                shared = "Shared capability flags are retained. "
            if contract.get("schema") == 7 and support_queries_unavailable:
                shared += (
                    "All support queries for the selected auxiliary group also become unavailable; "
                    "the other captured groups and peers must match the preview. "
                )
            if contract.get("schema") not in (5, 10):
                lines.append(
                    f"GR power gating: configured P-state eligibility {states(before)} -> {states(after)}. "
                    "Loaded routing, controller support and the other low-power controller masks must stay unchanged. "
                    + shared
                    + "Restore verifies the original configuration. "
                    "Physical residency and power savings remain unmeasured."
                )
        if receipt.get("voltage_startup_contracts"):
            lines.append(
                "The selected REL adjustment must take effect during GPU startup before boost replay. "
                "Other captured rail words and voltage definitions must stay exact. "
                "Undo verifies the original requests; an un-restorable runtime offset leaves recovery required."
            )
        if receipt.get("linked_current_contracts"):
            lines.append(
                "The linked-current cache and firmware-reported value must match after GPU restart. "
                "Other loaded power definitions and requests must stay exact. A lower firmware vote can still "
                "determine the effective current limit. Undo verifies the original settings. "
                "This checks configuration and reported limits; physical enforcement is unmeasured."
            )
        if receipt.get("voltage_contracts"):
            lines.append(
                "VMIN must read back exactly after restart; voltage-step rounding triggers undo. "
                "Existing rail offsets and other reported limits are preserved."
            )
        if receipt.get("voltage_ceiling_contracts"):
            lines.append(
                "Loaded voltage equations and reported limits must match after restart; voltage-step rounding triggers undo. "
                "ALT is an addend to REL and can change shared voltage boost on both rails. "
                "The boost request and rail offsets are retained. Effective limits are computed by firmware; "
                "this verifies configuration, not measured regulator voltage or enforcement under load."
            )
        if receipt.get("voltage_rel_contracts"):
            fixed = any(key.endswith(".REL.value") for key in changes)
            lines.append(
                (
                    "Selected REL equation bounds must load exactly after restart. A fixed-bounds selection sets both "
                    "bounds equal; an upper-clamp selection changes only the upper bound. Polynomial terms and linked "
                    "equations are retained. Reported REL can differ because of linked adjustments, offsets and voltage "
                    "steps. "
                    if fixed
                    else "The loaded REL upper clamp must match exactly after restart. Reported REL can remain below the clamp "
                    "and can be rounded to a voltage step. "
                )
                + "Linked ALT limits and shared voltage boost may change. "
                "Druta checks the retained requests and other loaded definitions; firmware computes the reported limits. "
                "This verifies configuration; physical voltage and enforcement under load require separate measurement."
            )
        if receipt.get("model_peer_contracts"):
            lines.append(
                "Evaluated model inputs and cached overrides must match after restart. "
                "Hardware values, peer inputs and other model configuration are preserved. "
                "IDDQ physical units remain unresolved; changing an IDDQ input may also "
                "change calculated voltage limits on linked rails."
            )
        if receipt.get("fan_policy_contracts"):
            lines.append(
                "Fan-policy values must read back exactly after restart. Other curve points, stop settings, "
                "cooler routing and bounds are preserved. This checks configuration, not measured fan behavior."
            )
            seen = set()
            for contract in receipt["fan_policy_contracts"].values():
                mode = contract.get("output_mode")
                if not mode or (contract["index"], contract["role"]) in seen:
                    continue
                seen.add((contract["index"], contract["role"]))
                if not mode["selected_active"]:
                    coordinate = "duty" if contract["role"] == "level" else "RPM"
                    active = "duty" if mode["coordinate"] == "level" else "RPM"
                    lines.append(
                        f"Policy {contract['index']}: stored {coordinate} coordinate; currently controls {active}."
                    )
        if receipt.get("fan_stop_contracts"):
            lines.append(
                "Zero-RPM support and enable flags change across every affected fan policy. "
                "All flags must match after restart; curves, routing and cooler settings are preserved."
            )
        if receipt.get("clock_bios_contracts"):
            lines.append(
                "Clock bounds must match the driver's loaded domain table after restart. "
                "Other clock metadata and current user requests are preserved."
            )
        if receipt.get("factory_clock_contracts"):
            lines.append(
                "Factory offsets must match after restart, including any linked XBAR offset. "
                "Your per-domain offsets and VF tuning requests are preserved. "
                "Reported frequencies may change as the driver evaluates the new factory values."
            )
        if receipt.get("rated_clock_contracts"):
            lines.append(
                "The rated clock must match after restart, including any record linked by its original frequency. "
                "Your per-domain offsets and VF tuning requests are preserved. "
                "Rated clocks are metadata; actual operating clocks depend on the GPU's running policies."
            )
        coupled = set()
        for key, contract in receipt.get("policy_contracts", {}).items():
            if contract.get("mode") != "rated-maximum" or contract["policy"] in coupled:
                continue
            coupled.add(contract["policy"])
            factor, unit = self.display(self.fields[key])
            old, new = contract["before_bounds"]["maximum"], contract["after_bounds"]["maximum"]
            lines.append(
                f"Policy {contract['policy']} active maximum: {old / factor:g} -> {new / factor:g} {unit} "
                "(follows the default limit)."
            )
        lines += ["GPU restart required. Undo saved before applying.", "Receipt: " + receipt["path"]]
        dpg.set_value("reg_preview", "\n".join(lines))
        dpg.configure_item("reg_apply_button", enabled=True)
        self.status("Preview ready. Registry settings have not changed.")

    def _launch(self, receipt, restore):
        if not self.app.guard():
            return
        if not overrides.devicereset.is_admin():
            self.status("Administrator rights are required to change GPU registry settings.", False)
            return
        try:
            trial = getattr(self.app, "_registry_ui_trial", None)
            if trial is None:
                overrides.launch_helper(receipt, restore=restore)
            else:
                overrides.launch_helper(receipt, restore=restore, ui_trial=trial)
        except Exception as exc:
            self.status("Could not start override helper: " + str(exc), False)
            return
        self.app._closing = True
        self.app._stop.set()
        dpg.stop_dearpygui()

    def apply(self, *unused):
        if self.preview:
            self._launch(self.preview, False)

    def restore(self, *unused):
        if self.active:
            self._launch(self.active, True)
