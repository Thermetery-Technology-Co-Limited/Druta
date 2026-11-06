# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact, scoped snapshots of understood ordinary power requests."""

_FIELDS = ("policy", "type", "channel", "unit", "minimum", "maximum",
           "requested", "board", "named_current", "writable", "consistent")
_GEOMETRY = ("policy", "type", "channel", "unit", "minimum", "maximum", "board", "named_current")


def _board_only_supported(gpu):
    """Only FE's non-legacy path verifies that its peer control bytes stay put."""
    nv = getattr(gpu, "nvml", None)
    nv_set = bool(nv and nv.ok and nv.has("nvmlDeviceSetPowerManagementLimit"))
    # A transiently unreadable NVML getter can recover before the action,
    # causing its setter to take the NVML route and recompute peer policies.
    if nv_set:
        return False
    observation, _ = gpu.read_live_power_request()
    if not isinstance(observation, dict):
        return False
    # Schema 1 omits the layout key for its modern layout. Require its full
    # documented binding and packet geometry, rather than treating an unknown
    # or incomplete observation as evidence of peer-preserving semantics.
    from . import client_power_runtime as runtime
    binding = observation.get("binding")
    if (observation.get("schema") != 1 or observation.get("stable_reads") != 2
            or not isinstance(binding, dict) or "layout" in binding
            or binding.get("semantic_role") != runtime.ROLE
            or binding.get("client") != runtime.CLIENT or binding.get("units") != "mW"
            or type(binding.get("client_index")) is not int
            or not 0 <= binding["client_index"] < runtime.CAPACITY
            or type(binding.get("native_index")) is not int
            or not 0 <= binding["native_index"] < runtime.NATIVE_CAPACITY):
        return False
    for field, command in (("info_hex", runtime.INFO), ("control_hex", runtime.CONTROL),
                           ("native_info_hex", runtime.NATIVE_INFO),
                           ("native_control_hex", runtime.NATIVE_CONTROL)):
        value = observation.get(field)
        if not isinstance(value, str) or len(value) != 2 * runtime.SIZES[command]:
            return False
    return True


def capture(gpu):
    board = gpu.read_power_limit_mw()
    if type(board) is not int or not 0 < board < 0xffffffff:
        raise ValueError("configured power request is unavailable")
    pins = dict(getattr(gpu, "_power_policy_pins", None) or {})
    rows, error = gpu.read_power_policies()
    if error:
        if pins or not _board_only_supported(gpu):
            raise ValueError(f"power policy table unavailable: {error}")
        rows = None
    else:
        rows = [{key: row[key] for key in _FIELDS} for row in rows]
    data = {"board_mw": board, "rows": rows,
            "pins": {str(k): v for k, v in pins.items()}}
    return _validate_data(data)


def _validate_data(data):
    if not isinstance(data, dict) or set(data) != {"board_mw", "rows", "pins"}:
        raise ValueError("invalid power action snapshot")
    board, rows, pins = data["board_mw"], data["rows"], data["pins"]
    if type(board) is not int or not 0 < board < 0xffffffff:
        raise ValueError("invalid board request")
    if not isinstance(pins, dict):
        raise ValueError("invalid power policy pins")
    parsed = {}
    for key, value in pins.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or str(int(key)) != key or not 0 <= int(key) < 32 or type(value) is not int or not 0 <= value < 0xffffffff:
            raise ValueError("invalid power policy pin")
        parsed[int(key)] = value
    if rows is None:
        if parsed:
            raise ValueError("board-only snapshot cannot contain policy pins")
        return {"board_mw": board, "rows": None, "pins": {}}
    if not isinstance(rows, list) or not rows:
        raise ValueError("invalid power policy table")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(_FIELDS):
            raise ValueError("invalid power policy record")
        for key in ("policy", "type", "channel", "minimum", "maximum", "requested"):
            if type(row[key]) is not int or not 0 <= row[key] <= 0xffffffff:
                raise ValueError("invalid power policy field")
        if row["policy"] >= 32 or row["type"] > 255 or row["channel"] > 255:
            raise ValueError("invalid power policy identity")
        if row["unit"] not in (None, "mW", "mA") or any(type(row[k]) is not bool for k in ("board", "named_current", "writable", "consistent")):
            raise ValueError("invalid power policy properties")
        if row["policy"] in by_id or row["minimum"] > row["maximum"]:
            raise ValueError("invalid power policy table geometry")
        by_id[row["policy"]] = dict(row)
    if any(p not in by_id for p in parsed):
        raise ValueError("pin is missing from power policy table")
    return {"board_mw": board, "rows": [by_id[p] for p in sorted(by_id)],
            "pins": {str(p): parsed[p] for p in sorted(parsed)}}


def validate(state):
    allowed = {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "power_action"}
    if not isinstance(state, dict) or type(state.get("schema")) is not int or state["schema"] != 2 or state.get("scope") != "power_action" or set(state) - allowed:
        raise ValueError("unsupported power action profile scope")
    missing = state.get("incomplete", [])
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ValueError("invalid completeness metadata")
    return _validate_data(state.get("power_action"))


def check_live(gpu, data):
    data = _validate_data(data)
    current = capture(gpu)
    if (data["rows"] is None) != (current["rows"] is None):
        raise ValueError("power policy table availability changed")
    if data["rows"] is not None:
        old = {r["policy"]: r for r in data["rows"]}
        live = {r["policy"]: r for r in current["rows"]}
        if old.keys() != live.keys() or any(any(old[p][k] != live[p][k] for k in _GEOMETRY) for p in old):
            raise ValueError("power policy identity or geometry changed")
        # A prior request outside the current range cannot be represented by
        # the documented setter, regardless of the present request.
        for row in old.values():
            if not row["board"] and (row["writable"] or row["named_current"]) and not row["minimum"] <= row["requested"] <= row["maximum"]:
                raise ValueError(f"policy {row['policy']} original request is outside its range")
    return current


def restore(gpu, data):
    results = []
    try:
        data = _validate_data(data)
        current = check_live(gpu, data)
    except Exception as exc:
        return [(False, f"power action not restored: {exc}")]
    # Temporarily suppress current intent so a board write cannot reapply the
    # values being undone. Reinstall only original pins verified at the end.
    gpu._power_policy_pins = {}
    if current["board_mw"] != data["board_mw"]:
        try:
            ok, message = gpu.set_power_limit_mw(data["board_mw"])
        except Exception as exc:
            ok, message = False, str(exc)
        results.append((ok, f"power limit: {message}"))
    if data["rows"] is not None:
        try:
            check_live(gpu, data)
            for row in data["rows"]:
                # Each selected write can change other requests. Re-read before
                # choosing the next write, and recheck table identity as well.
                live = {r["policy"]: r for r in check_live(gpu, data)["rows"]}
                now = live.get(row["policy"])
                if now is None or now["requested"] == row["requested"] or row["board"]:
                    continue
                if not (now["writable"] or now["named_current"]) or not now["consistent"]:
                    results.append((False, f"policy {row['policy']} cannot be restored"))
                    continue
                try:
                    ok, message = gpu.set_power_policy(row["policy"], row["requested"], pin=False, allow_named=now["named_current"])
                except Exception as exc:
                    ok, message = False, str(exc)
                results.append((ok, f"policy {row['policy']}: {message}"))
        except Exception as exc:
            results.append((False, f"power policy restoration failed: {exc}"))
    try:
        final = check_live(gpu, data)
        if final["board_mw"] != data["board_mw"]:
            results.append((False, "power limit request does not match the snapshot"))
        if data["rows"] is not None:
            actual = {r["policy"]: r["requested"] for r in final["rows"]}
            for row in data["rows"]:
                if actual.get(row["policy"]) != row["requested"]:
                    results.append((False, f"policy {row['policy']} request does not match the snapshot"))
            captured = {r["policy"]: r["requested"] for r in data["rows"]}
            pins = {int(p): v for p, v in data["pins"].items()
                    if actual.get(int(p)) == captured[int(p)]}
            gpu._power_policy_pins = pins
            if len(pins) != len(data["pins"]):
                results.append((False, "some original policy pins could not be restored"))
    except Exception as exc:
        results.append((False, f"power action readback failed: {exc}; original pins could not be verified"))
    if not results:
        results.append((True, "power requests and pins restored"))
    return results
