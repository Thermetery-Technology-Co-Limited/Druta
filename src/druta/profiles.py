# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Named tune profiles, and automatic pre-write undo snapshots.

Why this is not the baseline snapshot an earlier review rejected: that one was
IMPLICIT - captured on first read and labelled "stock" - so whatever OC happened
to be live at launch became the restore target. A profile here is only ever
written when someone asks for it, and it says on its face what it is and when it
was taken. It never claims to be factory state; "Reset all to stock" remains the
only thing that does.

Files are plain JSON in the existing source/bundle profiles/ directory, or in
per-user storage for an installed package, so they can be hand-edited.

ORDERING NOTE, and it matters: the core clock offset and the V/F delta table are
the SAME table in the driver. Whichever is written last wins, so restore
writes the delta table LAST and treats it as authoritative; the stored core
offset is applied first only so the slider reads back sensibly.
"""
import glob
import hashlib
import json
import math
import os
import re
import time
from .startup import atomic_json
from .paths import profile_dir

SCHEMA = 2
DIR = str(profile_dir())
AUTOSAVE_PREFIX = "autosave-"
KEEP_AUTOSAVES = 20
# what capture() could not read. A list, not a flag, so the reason travels with
# the snapshot into the log line the user actually sees.
INCOMPLETE_KEY = "incomplete"


def vf_applicable(gpu):
    return getattr(gpu, "vf_curve_applicable", lambda: True)()


def incomplete(state):
    """What this snapshot is MISSING, as human-readable strings (empty = it is
    whole). A profile written before this field existed reports nothing missing
    unless its V/F table is absent, which is the case that matters."""
    miss = list(state.get(INCOMPLETE_KEY) or [])
    if not miss and not state.get("vf_deltas") and state.get("vf_applicable") is not False:
        miss.append("V/F delta table NOT captured")
    return miss


def _slug(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-")
    return s or "unnamed"


def path_for(name):
    return os.path.join(DIR, _slug(name) + ".json")


def _fan_is_manual(gpu):
    """Read the common NVML/NVAPI policy; None for unknown or mixed modes."""
    import ctypes
    try:
        reader = getattr(gpu, "read_fan_manual", None)
        if callable(reader):
            return reader()
        nv = gpu.nvml
        if not (nv.ok and nv.has("nvmlDeviceGetFanControlPolicy_v2")):
            return None
        pol = ctypes.c_uint32(0)
        if nv.dll.nvmlDeviceGetFanControlPolicy_v2(
                nv.dev, 0, ctypes.byref(pol)) != 0:
            return None
        return pol.value == 1
    except Exception:
        return None


def capture(gpu, rail=None):
    """Snapshot every knob this tool can write. Values are stored in the units
    the corresponding setter expects, so restore is a straight hand-back.

    A knob that could not be READ is recorded in state[INCOMPLETE_KEY] rather
    than left as a silent None. That matters for exactly one field: the V/F
    delta table is the whole reason an undo point is taken before a curve
    write, and a snapshot missing it can restore everything EXCEPT the thing it
    existed to protect. The caller has to be able to see that before it tells
    anyone their state can be put back (see incomplete())."""
    d = gpu.read()
    scale = 1
    try:
        scale = gpu.mem_offset_scale()[0] or 1
    except Exception:
        pass
    mem_units = d.get("mem_off")
    power_reader = getattr(gpu, "read_power_limit_mw", None)
    power_error = ""
    if callable(power_reader):
        try:
            power_limit = power_reader()
        except Exception as exc:
            power_limit, power_error = None, str(exc)
    else:
        # Compatibility for older adapters without the configured-limit getter.
        power_limit = d.get("pl_requested_mw", d.get("pl_now_mw"))
    now = time.time()
    state = {
        "schema": SCHEMA,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        # the SORT key, and why it is stored as well as the readable string:
        # saved_at has one-second resolution, so two autosaves in the same
        # second tie and the tiebreak (the action label, alphabetically) can
        # hand back the OLDER state - reachable by double-clicking Apply.
        "saved_ts": now,
        "device": {
            "name": gpu.static.get("name"),
            "vbios": gpu.static.get("vbios"),
            "driver": gpu.static.get("driver"),
            # The only two that separate two IDENTICAL cards in one machine,
            # which name and vbios cannot: the UUID is the card, the slot is
            # where it was sitting. Both are recorded, and device_mismatch
            # judges on the UUID - a card moved to another slot is still the
            # card its V/F deltas were measured on.
            "uuid": gpu.static.get("uuid"),
            "slot": gpu.static.get("slot"),
        },
        "core_off_mhz": d.get("core_off"),
        # stored both ways: units is what the driver holds, true MHz is what
        # set_clock_offset(2, ...) takes for a known GDDR type
        "mem_off_units": mem_units,
        "mem_off_true_mhz": (mem_units / scale) if mem_units is not None else None,
        "mem_off_scale": scale,
        # The enforced ceiling can lag/quantize; replay the configured request.
        "power_limit_mw": power_limit,
        "volt_boost_pct": None,
        # Duty alone is not restorable state. A card idling at 0% on the auto
        # curve and a card pinned to 0% manually read identically, and handing
        # a captured duty back as a MANUAL duty would pin the fans - a thermal
        # behaviour change, not a restore. So record the policy too.
        "fan_pct": (d.get("fans") or [(None, None)])[0][0],
        "fan_manual": None,
        "fan_control_state": None,
        "vf_deltas": None,
        "vf_applicable": vf_applicable(gpu),
        INCOMPLETE_KEY: [],
    }
    if (callable(power_reader) and power_limit is None
            and gpu.static.get("pl_min_mw") is not None
            and gpu.static.get("pl_max_mw") is not None):
        state[INCOMPLETE_KEY].append("requested power limit NOT captured"
                                     + (f" ({power_error})" if power_error else ""))
    # Modern NVML and the legacy NVAPI fallbacks expose requested per-fan
    # levels. The measured duty above can still be ramping toward that request.
    try:
        reader = getattr(gpu, "read_fan_control_state", None)
        fan_state = reader() if callable(reader) else None
        if isinstance(fan_state, dict) and fan_state.get("fans"):
            state["fan_control_state"] = fan_state
            state["fan_pct"] = fan_state["fans"][0]["level"]
            modes = {fan["manual"] for fan in fan_state["fans"]}
            state["fan_manual"] = modes.pop() if len(modes) == 1 else None
    except Exception:
        pass
    if state["fan_control_state"] is None:
        state["fan_manual"] = _fan_is_manual(gpu)
    try:
        state["volt_boost_pct"] = gpu.read_voltage_boost()
    except Exception:
        pass
    try:
        pts, err = gpu.read_vf_curve() if state["vf_applicable"] else (None, None)
        if pts:
            state["vf_deltas"] = {str(p["idx"]): int(p["delta_khz"])
                                  for p in pts}
        elif state["vf_applicable"]:
            state[INCOMPLETE_KEY].append(
                f"V/F delta table NOT captured ({err or 'no points returned'})")
    except Exception as e:
        state[INCOMPLETE_KEY].append(f"V/F delta table NOT captured ({e})")
    capture_rails(gpu, state, rail)
    return state


def rail_identity(rail):
    """Pin bus addressing AND the complete, locally validated regulator recipe."""
    encoded = json.dumps(rail.p.src, sort_keys=True, default=str).encode("utf-8")
    return {"profile": getattr(rail.p, "profile_name", rail.p.name),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "port": rail.p.port, "addr7": rail.addr7, "rail": rail.p.rail}


def clock_controls(gpu):
    controls = set(gpu.clkdom_controls_for_ui()) - {0}
    # Additional Memory Clock Offset is deliberately independent of the
    # private-getter pairing. Pascal R470 has no pairing but this knob works.
    if (hasattr(gpu, "clkdom_domains") and 2 in gpu.clkdom_domains()
            and gpu.clkdom_delta_inert(2) is False):
        controls.add(2)
    return controls


def capture_rails(gpu, state, rail):
    state.update(rail_limits_mv={}, nvvdd_offset_mv=None,
                 clock_domain_offsets_mhz={}, i2c=None,
                 xoc=bool(getattr(gpu, "voltage_xoc_enabled", False)))
    missing = state[INCOMPLETE_KEY]
    try:
        reader = getattr(gpu, "read_volt_rail_limits", None)
        records = reader() if reader else None
        for index, record in (records or {}).items():
            fields = gpu.volt_rail_limit_fields(index)
            if fields:
                state["rail_limits_mv"][str(index)] = {
                    key: gpu.abs_limit_mv(record, key) for key in fields}
        # One readable rail does not prove a complete capture on a two-rail
        # board. Name each known writer whose state this undo point is missing.
        if reader:
            for index in (0, 1):
                if (gpu.volt_rail_limit_fields(index)
                        and str(index) not in state["rail_limits_mv"]):
                    missing.append(f"per-rail limits NOT captured (rail {index})")
    except Exception as e:
        missing.append(f"per-rail limits NOT captured ({e})")
    try:
        reader = getattr(gpu, "read_rail_offset_mv", None)
        if reader:
            state["nvvdd_offset_mv"] = reader(0)
            layout = gpu.clkdom_layout()
            if layout and layout.nvvdd_uv is not None and state["nvvdd_offset_mv"] is None:
                missing.append("NVVDD offset NOT captured")
        reader = getattr(gpu, "read_clk_domain_offsets", None)
        if reader:
            controls = clock_controls(gpu)
            records, error = reader()
            for index in controls:
                if index not in (records or {}):
                    missing.append(f"clock control {index} NOT captured ({error})")
                else:
                    state["clock_domain_offsets_mhz"][str(index)] = records[index]["freq_khz"] / 1000
    except Exception as e:
        missing.append(f"voltage/clock offsets NOT captured ({e})")
    if rail is not None and not rail.p.read_only:
        try:
            if getattr(rail, "absolute_voltage", False):
                control = rail.capture_control()
                state["i2c"] = dict(rail_identity(rail), control=control,
                                    display_name=rail.p.name)
            else:
                offset = rail.telemetry().get("offset_mv")
                if offset is None:
                    raise ValueError("offset read failed")
                state["i2c"] = dict(rail_identity(rail), offset_mv=offset,
                                    display_name=rail.p.name)
        except Exception as e:
            missing.append(f"I2C offset NOT captured ({e})")
    # Unticking XOC does not undo above-normal values already in the card.
    # Replaying that carryover after reboot still needs the wider envelope.
    required = any(value > getattr(gpu, "VOLT_LIMIT_MAX_MV", 1200)
                   for fields in state["rail_limits_mv"].values() for value in fields.values())
    offset = state["nvvdd_offset_mv"]
    required = required or (offset is not None and not -100 <= offset <= 200)
    required = required or bool(state["clock_domain_offsets_mhz"].get("2"))
    if state["i2c"]:
        if "control" in state["i2c"]:
            try:
                rail.validate_control(state["i2c"]["control"], xoc=False)
            except ValueError:
                required = True
        else:
            offset = state["i2c"]["offset_mv"]
            required = required or not (getattr(rail.p, "env_min", -200) <= offset
                                       <= getattr(rail.p, "env_max", 100))
    state["xoc"] = state["xoc"] or required


def strict_device_error(state, gpu):
    """Private controls and unattended loads require the same silicon and firmware."""
    old, live = state.get("device") or {}, gpu.static
    identity = "uuid" if old.get("uuid") and live.get("uuid") else "slot"
    for key in (identity, "name", "vbios", "driver"):
        if not old.get(key) or not live.get(key) or old[key] != live[key]:
            return f"profile {key} does not match this GPU/driver; save a fresh profile on this card"
    return None


def _number(value, label, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise ValueError(f"{label}: non-finite or non-numeric setting")
    if integer and value != int(value):
        raise ValueError(f"{label} must be an integer")
    return value


def _vf_deltas(state):
    """Decode only the serialized indices/values, without truncating bad input."""
    saved = state.get("vf_deltas")
    if saved is None:
        return {}
    if not isinstance(saved, dict):
        raise ValueError("V/F deltas must be an index-to-delta mapping")
    deltas = {}
    for key, value in saved.items():
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise ValueError(f"invalid V/F point index {key!r}")
        try:
            index = int(key)
        except (ValueError, OverflowError):
            raise ValueError(f"invalid V/F point index {key!r}") from None
        if index < 0 or str(index) != str(key) or index in deltas:
            raise ValueError(f"invalid or duplicate V/F point index {key!r}")
        _number(value, f"V/F point {index} delta", integer=True)
        deltas[index] = int(value)
    return deltas


def _validate_saved_fields(gpu, state):
    """Reject deterministic payload errors before any setting is changed.

    Missing fields still mean "not captured" for both supported schemas. GPU
    availability and live read-back checks remain with the individual setters.
    """
    for key in ("device", "rail_limits_mv", "clock_domain_offsets_mhz", "i2c"):
        if state.get(key) is not None and not isinstance(state[key], dict):
            raise ValueError(f"{key} must be a mapping")
    for key in ("xoc", "fan_manual", "vf_applicable"):
        if state.get(key) is not None and not isinstance(state[key], bool):
            raise ValueError(f"{key} must be a boolean")
    for key in ("core_off_mhz", "mem_off_true_mhz", "power_limit_mw",
                "volt_boost_pct", "fan_pct", "nvvdd_offset_mv"):
        if state.get(key) is not None:
            _number(state[key], key)
    core = state.get("core_off_mhz")
    if core is not None and not -(1 << 31) <= core < (1 << 31):
        raise ValueError("core offset is outside the driver's representation")
    memory = state.get("mem_off_true_mhz")
    if memory is not None:
        scale = gpu.mem_offset_scale()[0]
        _number(scale, "memory offset scale")
        if not 0 < scale < (1 << 31):
            raise ValueError("memory offset scale is invalid")
        units = memory * scale
        if not math.isfinite(units) or not -(1 << 31) <= units < (1 << 31):
            raise ValueError("memory offset is outside the driver's representation")
        if units != int(units):
            raise ValueError("memory offset is not representable in driver units")
        step = getattr(gpu, "memory_offset_step_units", lambda: 1)()
        _number(step, "memory offset step", integer=True)
        if step <= 0 or units % step:
            raise ValueError("memory offset is not representable on this GPU/driver's offset grid")
    power = state.get("power_limit_mw")
    if power is not None:
        _number(power, "power limit", integer=True)
    if power:
        low = gpu.static.get("pl_min_mw", 50000)
        high = gpu.static.get("pl_max_mw", 400000)
        if not low <= power <= high:
            raise ValueError(f"power limit is outside [{low}..{high}] mW")
    for key in ("volt_boost_pct", "fan_pct"):
        value = state.get(key)
        if value is not None and not 0 <= value <= 100:
            raise ValueError(f"{key} is outside [0..100]%")
    fans = state.get("fan_control_state")
    if fans is not None:
        if (not isinstance(fans, dict) or fans.get("source") not in
                ("nvml", "nvapi_cooler", "nvapi_client")):
            raise ValueError("fan snapshot has an unknown control source")
        rows = fans.get("fans")
        if not isinstance(rows, list) or not rows:
            raise ValueError("fan snapshot has no fan list")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("manual"), bool):
                raise ValueError("fan snapshot has an unknown policy")
            level = _number(row.get("level"), "fan requested level", integer=True)
            if not 0 <= level <= 100:
                raise ValueError("fan requested level is outside [0..100]%")
    deltas = _vf_deltas(state)
    maximum = getattr(gpu, "MAX_ABS_DELTA_KHZ", 1_000_000)
    for index, delta in deltas.items():
        if abs(delta) > maximum:
            raise ValueError(f"V/F point {index} delta is outside +/-{maximum} kHz")
    return deltas


def preflight(gpu, state, rail=None, *, apply_curve=True):
    """Validate saved private controls before ANY write, including I2C verification.

    Old profiles omit these fields and retain their existing restore behaviour.
    No live-voltage telemetry or arbitrary register image is ever replayed.
    """
    if (not isinstance(state, dict) or isinstance(state.get("schema"), bool)
            or state.get("schema", 1) not in (1, SCHEMA)):
        return "unsupported profile format"
    try:
        deltas = _validate_saved_fields(gpu, state)
        applicable = vf_applicable(gpu)
        if state.get("vf_applicable") is False and applicable:
            raise ValueError("this GPU requires a V/F snapshot; save a fresh profile on this card")
        if deltas and not applicable:
            raise ValueError("V/F curve profiles cannot be applied to this GPU")
        if deltas and apply_curve:
            reader = getattr(gpu, "vfp_layout", None)
            if callable(reader):
                layout = reader()
                if layout is None:
                    raise ValueError("could not determine this card's V/F table layout")
                invalid = set(deltas) - set(layout.gpu_idx)
                if invalid:
                    raise ValueError(f"V/F point indices {sorted(invalid)} are not GPU points on this card")
        limits = state.get("rail_limits_mv") or {}
        offsets = state.get("clock_domain_offsets_mhz") or {}
        i2c = state.get("i2c")
        nvvdd = state.get("nvvdd_offset_mv")
        if not (limits or offsets or i2c or nvvdd is not None):
            return None
        error = strict_device_error(state, gpu)
        if error:
            return error
        if i2c:
            if rail is None or rail.p.read_only:
                raise ValueError("saved I2C regulator is not available")
            if any(i2c.get(k) != v for k, v in rail_identity(rail).items()):
                raise ValueError("I2C regulator/profile/limits changed; save a fresh profile")
            if getattr(rail, "absolute_voltage", False):
                if "offset_mv" in i2c:
                    raise ValueError("absolute I2C voltage cannot load an offset")
                rail.validate_control(i2c.get("control"), xoc=bool(state.get("xoc")))
            else:
                if "control" in i2c:
                    raise ValueError("offset I2C regulator cannot load absolute voltage")
                ok, message = rail.validate_offset_mv(i2c.get("offset_mv"), xoc=bool(state.get("xoc")))
                if not ok:
                    raise ValueError(message)
            if not rail.present():
                raise ValueError("saved I2C regulator is not available")
        if limits:
            if not gpu.volt_rail_limits_supported():
                raise ValueError("per-rail limits are not supported on this GPU/driver")
            current = gpu.read_volt_rail_limits()
            maximum = (getattr(gpu, "VOLT_LIMIT_XOC_MAX_MV", 1500) if state.get("xoc")
                       else getattr(gpu, "VOLT_LIMIT_MAX_MV", 1200))
            for key, values in limits.items():
                if key not in ("0", "1") or not isinstance(values, dict) or not values:
                    raise ValueError("invalid voltage rail")
                if set(values) - set(gpu.volt_rail_limit_fields(int(key))):
                    raise ValueError(f"unconfirmed limit field on rail {key}")
                for field, value in values.items():
                    _number(value, f"rail {key} {field}")
                    bound = max(maximum, gpu.abs_limit_mv(current[int(key)], field))
                    if not getattr(gpu, "VOLT_LIMIT_MIN_MV", 300) <= value <= bound:
                        raise ValueError(f"rail {key} {field} is outside the saved mode's voltage bounds")
        if nvvdd is not None:
            current = gpu.read_rail_offset_mv(0)
            if current is None:
                raise ValueError("NVVDD offset is not readable on this GPU/driver")
            lower, upper = (-500, 500) if state.get("xoc") else (-100, 200)
            if not min(lower, current) <= nvvdd <= max(upper, current):
                raise ValueError("NVVDD offset is outside the saved mode's voltage bounds")
        if offsets:
            controls = clock_controls(gpu)
            for key, value in offsets.items():
                if str(int(key)) != key or int(key) not in controls:
                    raise ValueError(f"unconfirmed clock control {key}")
                _number(value, f"clock control {key}")
                if (not -(1 << 31) <= value * 1000 < (1 << 31)
                        or not -(1 << 31) <= round(value) * 1000 < (1 << 31)):
                    raise ValueError(f"clock control {key} is outside the driver's representation")
    except Exception as e:
        return str(e)
    return None


def save(name, state):
    p = path_for(name)
    atomic_json(p, state)
    return p


def load(name):
    with open(path_for(name), encoding="utf-8") as f:
        return json.load(f)


def list_profiles():
    """[(display_name, path, saved_at, is_autosave)] newest first.

    Ordered on saved_ts - a float - and NOT on the human-readable saved_at,
    which has one-second resolution: two autosaves in the same second tied
    there, and the tiebreak fell to the action label alphabetically, so 'Undo
    last write' could hand back the OLDER of the two states (double-click
    Apply and it does). A profile written before saved_ts existed falls back to
    the file's mtime, which is also sub-second, rather than to 0 - that would
    park every old profile at the bottom of the list regardless of its age."""
    out = []
    for p in glob.glob(os.path.join(DIR, "*.json")):
        base = os.path.splitext(os.path.basename(p))[0]
        when, ts = "", None
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
            when = st.get("saved_at", "")
            ts = st.get("saved_ts")
        except Exception:
            pass
        if not isinstance(ts, (int, float)):
            try:
                ts = os.path.getmtime(p)
            except OSError:
                ts = 0.0
        out.append((base, p, when, base.startswith(AUTOSAVE_PREFIX), ts))
    out.sort(key=lambda r: (r[4], r[2], r[0]), reverse=True)
    return [r[:4] for r in out]


def autosave(gpu, action, rail=None):
    """Undo point taken immediately before a destructive write. Distinct from a
    named profile: it is not a tune you chose to keep, it is the state you are
    about to leave. Old ones are pruned so the directory stays readable.

    Returns (name, path, missing) - `missing` being incomplete(), so the caller
    can refuse to call this a usable undo point when the snapshot did not get
    everything (see capture()).

    The filename carries MILLISECONDS as well as seconds. Two undo points taken
    in the same second for the same action produced the same name, and the
    second one silently overwrote the first - losing the earlier state, which
    is the one 'undo twice' needs."""
    t = time.time()
    stamp = (time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
             + f".{int((t % 1) * 1000):03d}")
    name = f"{AUTOSAVE_PREFIX}{_slug(action)}-{stamp}"
    state = capture(gpu, rail)
    p = save(name, state)
    autos = [r for r in list_profiles() if r[3]]
    for _n, old, _w, _a in autos[KEEP_AUTOSAVES:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return name, p, incomplete(state)


def device_mismatch(state, gpu):
    """Profiles are per-card by nature - the V/F table is the points of THIS
    silicon. Returns a warning string, or None when it is the same card.

    The UUID is checked FIRST and, when both sides have one, it is the whole
    answer: it is the only field that distinguishes two cards of the same model
    and VBIOS sitting in one machine, where name and vbios match by
    construction and every per-point delta is still measured on different
    silicon. Profiles written before the UUID was recorded have none, and those
    fall back to the name/vbios comparison rather than being called a
    mismatch."""
    dev = state.get("device") or {}
    live_uuid = gpu.static.get("uuid")
    if dev.get("uuid") and live_uuid:
        if dev["uuid"] == live_uuid:
            return None
        return (f"profile was saved on {dev.get('name')} "
                f"({dev.get('uuid')}, slot {dev.get('slot') or '?'}), this is "
                f"a DIFFERENT card: {gpu.static.get('name')} ({live_uuid}, "
                f"slot {gpu.static.get('slot') or '?'})")
    for key, live in (("name", gpu.static.get("name")),
                      ("vbios", gpu.static.get("vbios"))):
        if dev.get(key) and live and dev[key] != live:
            return (f"profile was saved on {dev.get('name')} / vbios "
                    f"{dev.get('vbios')}, this card is {gpu.static.get('name')} "
                    f"/ vbios {gpu.static.get('vbios')}")
    return None


def restore(gpu, state, apply_curve=True, *, rail=None, i2c_verified=False):
    """Write a profile back. Returns [(ok, message)] per knob, in write order.
    Voltage failures stop before clocks; other knob failures are reported.
    The caller enables the profile's displayed rail/XOC modes before calling.
    """
    error = preflight(gpu, state, rail, apply_curve=apply_curve)
    if error:
        return [(False, f"profile not applied: {error}")]
    if state.get("i2c") and not i2c_verified:
        return [(False, "profile not applied: I2C must be verified in this session first")]
    results = []
    try:
        return _restore_validated(gpu, state, apply_curve, rail, results)
    except Exception as e:
        # An unexpected failure between steps must not erase the record of
        # settings already changed. Stop here; the caller can show every result.
        results.append((False, f"profile stopped after an unexpected error: {e}"))
        return results


def _restore_validated(gpu, state, apply_curve, rail, results):
    deltas = _vf_deltas(state)

    def step(label, fn):
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"{label}: {e}"
        results.append((ok, msg))
        return ok

    # Voltage requests precede clocks. If any voltage operation fails, do not
    # apply a curve that may depend on it. Individual setters preserve their
    # normal whitelist, bounds and read-back checks.
    for key, values in (state.get("rail_limits_mv") or {}).items():
        if not step(f"rail {key} limits", lambda: gpu.set_volt_rail_limits(int(key), **values)):
            results.append((False, "profile stopped after a rail failure; remaining settings were not applied"))
            return results
    offset = state.get("nvvdd_offset_mv")
    if offset is not None:
        def restore_offset():
            ok, msg = gpu.set_rail_offset_mv(offset, 0)
            back = gpu.read_rail_offset_mv(0) if ok else None
            if ok and (back is None or abs(back - offset) > 0.0005):
                return False, f"NVVDD offset read-back mismatch: requested {offset}, got {back} mV"
            return ok, msg
        if not step("NVVDD offset", restore_offset):
            return results + [(False, "profile stopped after NVVDD offset failure")]
    if state.get("i2c"):
        if "control" in state["i2c"]:
            if not step("I2C voltage/mode", lambda: rail.restore_control(state["i2c"]["control"])):
                return results
        else:
            offset = state["i2c"]["offset_mv"]
            if not step("I2C dry run", lambda: rail.plan(offset)):
                return results
            if not step("I2C offset", lambda: rail.set_offset_mv(offset, acknowledged=True)):
                return results

    mw = state.get("power_limit_mw")
    if mw:
        step("power limit", lambda: gpu.set_power_limit_mw(int(mw)))

    vb = state.get("volt_boost_pct")
    if vb is not None:
        step("voltage boost", lambda: gpu.set_voltage_boost(int(vb)))

    mm = state.get("mem_off_true_mhz")
    if mm is not None:
        step("mem offset", lambda: gpu.set_clock_offset(2, mm))

    co = state.get("core_off_mhz")
    if co is not None:
        step("core offset", lambda: gpu.set_clock_offset(0, int(co)))

    for key, mhz in (state.get("clock_domain_offsets_mhz") or {}).items():
        step(f"clock control {key}", lambda: gpu.set_clk_domain_offset(int(key), mhz))

    # Only ever pin the fans if the profile recorded them as manual. When the
    # policy was the temperature curve - or is simply unknown - hand control
    # back to the driver rather than freezing a captured duty.
    manual, fan = state.get("fan_manual"), state.get("fan_pct")
    fan_min = (getattr(gpu, "static", {}) or {}).get("fan_min")
    fan_state = state.get("fan_control_state")
    if fan_state is not None:
        restore_fans = getattr(gpu, "restore_fan_control_state", None)
        if callable(restore_fans):
            step("fan", lambda: restore_fans(fan_state))
        else:
            results.append((False, "fan: this backend cannot restore per-fan control state"))
    elif manual and fan is not None and fan_min is not None and fan < fan_min:
        # A captured duty BELOW the hardware minimum is the zero-RPM idle
        # curve, which the driver reports as "manual" at 0%. set_fan refuses it
        # ("0% below hardware minimum 41%") and the fans would then be left
        # wherever the write that is being undone had put them - at 100% after
        # a 'Max it'. Auto IS what 0% meant, so hand control back.
        step(f"fan (captured {fan}% is the zero-RPM curve)", gpu.reset_fan)
    elif manual and fan is not None:
        step("fan", lambda: gpu.set_fan(int(fan)))
    elif manual is False:
        step("fan", gpu.reset_fan)
    elif fan is not None:
        # manual is None: the driver would not say whether the fan was under
        # manual control when this was captured, so there is nothing safe to
        # put back. SAY SO. Silence here reads as "fan restored" in a results
        # list where every other knob reports - and it matters more now that
        # 'Max it' pins both fans to 100% manual on every press.
        results.append((False, "fan: the control policy was not readable when "
                               "this snapshot was taken, so the fan was NOT "
                               "restored - use Auto or Reset all to stock"))

    # LAST, and authoritative: the delta table subsumes the core offset above.
    if apply_curve and vf_applicable(gpu):
        if deltas:
            step("v/f curve", lambda: gpu.apply_vf_deltas(deltas))
        else:
            # NOT silence. Skipping the one table an undo point exists to
            # protect, while every other knob reports success, makes a restore
            # that did not restore the curve look clean.
            results.append((False, "v/f curve: this snapshot has no delta "
                                   "table - the curve was NOT restored"))

    return results


def summarize(state):
    """One-line description for a menu row or a confirmation banner."""
    bits = []
    co = state.get("core_off_mhz")
    if isinstance(co, int):
        bits.append(f"core {co:+d} MHz")
    mm = state.get("mem_off_true_mhz")
    if isinstance(mm, (int, float)):
        bits.append(f"mem {mm:+g} MHz")
    mw = state.get("power_limit_mw")
    if mw:
        bits.append(f"PL {mw / 1000:g} W")
    vb = state.get("volt_boost_pct")
    if vb is not None:
        bits.append(f"vboost {vb}%")
    for key, fields in (state.get("rail_limits_mv") or {}).items():
        name = "NVVDD" if key == "0" else "MSVDD"
        values = "/".join(f"{k} {v:g}" for k, v in fields.items())
        bits.append(f"{name} limits {values} mV")
    offset = state.get("nvvdd_offset_mv")
    if offset is not None:
        bits.append(f"NVVDD offset {offset:+g} mV")
    i2c = state.get("i2c")
    if i2c:
        if "control" in i2c:
            from .ncp4206 import decode_vid
            control = i2c["control"]
            target = (f"{decode_vid(control['command']):g} mV" if control['enabled'] else 'Auto (GPU VID)')
            label = i2c.get("display_name") or i2c['profile']
            bits.append(f"I2C {i2c['rail']} {target} ({label})")
        else:
            bits.append(f"I2C {i2c['rail']} {i2c['offset_mv']:+g} mV ({i2c['profile']})")
    for key, value in (state.get("clock_domain_offsets_mhz") or {}).items():
        label = "Additional Memory Clock Offset" if key == "2" else f"clock control {key}"
        bits.append(f"{label} {value:+g} MHz")
    if state.get("xoc"):
        bits.append("XOC")
    if state.get("schema", 1) < 2:
        bits.append("legacy profile: I2C and per-rail settings not saved")
    d = state.get("vf_deltas") or {}
    nz = sum(1 for v in d.values() if v)
    if d:
        bits.append(f"{nz}/{len(d)} VF deltas set")
    # last, and unabbreviated: this row is how one snapshot is told from
    # another in the Profiles list, and "it cannot restore your curve" is the
    # single most important thing it can say about one
    miss = incomplete(state)
    if miss:
        bits.append("INCOMPLETE - " + "; ".join(miss))
    return "   ".join(bits) or "empty profile"
