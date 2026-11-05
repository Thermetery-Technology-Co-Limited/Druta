# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, typed snapshots of the independently qualified live controls."""


def controller(gpu, kind):
    if kind == "gate":
        from .voltage_request_gate import VoltageRequestGate

        return VoltageRequestGate(gpu)
    if kind == "rel_bounds":
        from .voltage_rel_live import VoltageRelController

        return VoltageRelController(gpu)
    if kind == "constants":
        from .voltage_constants_live import VoltageConstantsController

        return VoltageConstantsController(gpu)
    if kind == "switch_delay":
        from .voltage_device_runtime import VoltageDeviceController

        return VoltageDeviceController(gpu)
    raise ValueError("unknown voltage control kind")


def _integer(value, minimum=0, maximum=0xFFFFFFFF):
    return type(value) is int and minimum <= value <= maximum


def _identity(identity):
    if (
        not isinstance(identity, dict)
        or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}
        or not isinstance(identity["slot"], str)
        or not identity["slot"]
        or any(not _integer(identity[k]) for k in ("architecture", "device_id", "subsystem_id"))
    ):
        raise ValueError("invalid voltage control adapter identity")
    return dict(identity)


def capture(snapshot, kind, selection=None):
    identity = _identity(snapshot.get("identity"))
    if kind == "gate":
        if selection is not None or type(snapshot.get("enabled")) is not bool:
            raise ValueError("invalid gate snapshot")
        rows = [{"enabled": snapshot["enabled"]}]
    else:
        if kind not in ("rel_bounds", "constants", "switch_delay"):
            raise ValueError("unknown voltage control kind")
        rows = []
        for row in snapshot.get("rows", []):
            if row.get("status") != "ready":
                continue
            if kind == "rel_bounds":
                rail = row["rail"]
                mode = "bounds"
                if isinstance(selection, tuple) and len(selection) == 2:
                    selected, mode = selection
                else:
                    selected = selection
                if mode not in ("upper", "bounds") or (selected is not None and selected != rail):
                    continue
                if mode == "bounds" and row.get("fixed_status") != "ready":
                    if selection is None:
                        mode = "upper"
                    else:
                        continue
                item = {k: row[k] for k in ("rail", "equation", "upper_uv")}
                if mode == "bounds":
                    item["lower_uv"] = row["lower_uv"]
            elif kind == "constants":
                if selection is not None and selection != (row["rail"], row["role"]):
                    continue
                item = {k: row[k] for k in ("rail", "role", "equation", "value_uv")}
            else:
                if selection is not None and selection != row["index"]:
                    continue
                item = {
                    k: row[k]
                    for k in (
                        "index",
                        "type",
                        "voltage_domain",
                        "operation_type",
                        "i2c_device_index",
                        "switch_delay_us",
                    )
                }
            rows.append(item)
        if not rows:
            raise ValueError("no selected ready voltage control rows")
    return {"kind": kind, "identity": identity, "rows": rows}


def validate(state):
    allowed = {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "voltage_controls"}
    if (
        not isinstance(state, dict)
        or state.get("scope") != "voltage_controls"
        or type(state.get("schema")) is not int
        or state["schema"] != 2
        or set(state) - allowed
    ):
        raise ValueError("unsupported voltage control profile scope or fields")
    if not isinstance(state.get("incomplete", []), list) or any(
        not isinstance(x, str) for x in state.get("incomplete", [])
    ):
        raise ValueError("invalid completeness metadata")
    data = state.get("voltage_controls")
    if not isinstance(data, dict) or set(data) != {"kind", "identity", "rows"}:
        raise ValueError("invalid voltage control profile")
    _identity(data["identity"])
    kind, rows = data["kind"], data["rows"]
    if kind not in ("gate", "rel_bounds", "constants", "switch_delay") or not isinstance(rows, list) or not rows:
        raise ValueError("invalid voltage control kind or rows")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid voltage control row")
        if kind == "gate":
            valid = set(row) == {"enabled"} and type(row["enabled"]) is bool and len(rows) == 1
            key = 0
        elif kind == "rel_bounds":
            valid = (
                set(row) in ({"rail", "equation", "upper_uv"}, {"rail", "equation", "lower_uv", "upper_uv"})
                and type(row.get("rail")) is int
                and row["rail"] in (0, 1)
                and _integer(row.get("equation"))
                and _integer(row.get("upper_uv"), 1)
                and ("lower_uv" not in row or (_integer(row["lower_uv"]) and row["lower_uv"] <= row["upper_uv"]))
            )
            key = row.get("rail")
        elif kind == "constants":
            valid = (
                set(row) == {"rail", "role", "equation", "value_uv"}
                and type(row.get("rail")) is int
                and row["rail"] in (0, 1)
                and row.get("role") in ("ALT", "OV", "VMIN")
                and _integer(row.get("equation"))
                and _integer(row.get("value_uv"), 0 if row.get("role") == "ALT" else 1)
            )
            key = (row.get("rail"), row.get("role"))
        else:
            valid = (
                set(row) == {"index", "type", "voltage_domain", "operation_type", "i2c_device_index", "switch_delay_us"}
                and _integer(row.get("index"), 0, 31)
                and type(row.get("type")) is int
                and row["type"] == 3
                and type(row.get("voltage_domain")) is int
                and row["voltage_domain"] in (1, 2, 3)
                and type(row.get("operation_type")) is int
                and row["operation_type"] in (1, 2, 3, 4)
                and _integer(row.get("i2c_device_index"), 0, 255)
                and _integer(row.get("switch_delay_us"), 1)
            )
            key = row.get("index")
        if not valid or key in seen:
            raise ValueError("invalid or duplicate voltage control row")
        seen.add(key)
        try:
            if kind == "rel_bounds":
                from .vfe_equation_runtime import _clamp_uv

                _clamp_uv(row["upper_uv"], positive=True)
                if "lower_uv" in row:
                    _clamp_uv(row["lower_uv"])
            elif kind == "constants":
                from .voltage_ceiling_runtime import _whole

                _whole(row["value_uv"], zero=row["role"] == "ALT")
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError(f"invalid voltage control value: {exc}") from exc
    return data


def check_live(data, snapshot):
    if _identity(snapshot.get("identity")) != data["identity"]:
        raise ValueError("voltage control adapter identity changed")
    kind = data["kind"]
    for target in data["rows"]:
        if kind == "gate":
            if type(snapshot.get("enabled")) is not bool:
                raise ValueError("voltage gate unavailable")
            continue
        selection = (
            (target["rail"], "bounds" if "lower_uv" in target else "upper")
            if kind == "rel_bounds"
            else ((target["rail"], target["role"]) if kind == "constants" else target["index"])
        )
        current = capture(snapshot, kind, selection)["rows"][0]
        if (
            kind == "rel_bounds"
            and "lower_uv" not in target
            and target["upper_uv"] < next(r["lower_uv"] for r in snapshot["rows"] if r.get("rail") == target["rail"])
        ):
            raise ValueError("saved REL upper bound is below the current lower bound")
        values = {
            "rel_bounds": {"upper_uv", "lower_uv"},
            "constants": {"value_uv"},
            "switch_delay": {"switch_delay_us"},
        }[kind]
        if {k: v for k, v in current.items() if k not in values} != {
            k: v for k, v in target.items() if k not in values
        }:
            raise ValueError("voltage control row identity changed")
    return snapshot


def restore(gpu, data):
    ctl = controller(gpu, data["kind"])
    snapshot = check_live(data, ctl.read())
    results = []
    for row in data["rows"]:
        kind = data["kind"]
        label = (
            f"REL rail {row['rail']}"
            if kind == "rel_bounds"
            else f"rail {row['rail']} {row['role']}"
            if kind == "constants"
            else f"device {row['index']} switch delay"
            if kind == "switch_delay"
            else "voltage gate"
        )
        try:
            # Recheck the complete target set after each write, before dispatching another.
            check_live(data, snapshot)
            kind = data["kind"]
            if kind == "gate":
                ctl.set_enabled(row["enabled"], expected=snapshot["enabled"])
            elif kind == "rel_bounds":
                if "lower_uv" in row:
                    ctl.set_bounds(row["rail"], row["lower_uv"], row["upper_uv"])
                else:
                    ctl.set(row["rail"], row["upper_uv"])
            elif kind == "constants":
                ctl.set(row["rail"], row["role"], row["value_uv"])
            else:
                ctl.set(
                    row["index"],
                    row["switch_delay_us"],
                    voltage_domain=row["voltage_domain"],
                    operation_type=row["operation_type"],
                )
            results.append((True, f"{label} restored"))
            if len(results) < len(data["rows"]):
                snapshot = ctl.read()
        except Exception as exc:
            results.append((False, f"{label} restore failed: {exc}; remaining rows not applied"))
            break
    return results
