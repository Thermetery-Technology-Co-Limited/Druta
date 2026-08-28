"""
Named tune profiles, and automatic pre-write undo snapshots.

Why this is not the baseline snapshot an earlier review rejected: that one was
IMPLICIT - captured on first read and labelled "stock" - so whatever OC happened
to be live at launch became the restore target. A profile here is only ever
written when someone asks for it, and it says on its face what it is and when it
was taken. It never claims to be factory state; "Reset all to stock" remains the
only thing that does.

Files are plain JSON in profiles/ next to the script, so they can be diffed,
kept in git, and hand-edited.

ORDERING NOTE, and it matters: the core clock offset and the V/F delta table are
the SAME 103-row table in the driver. Whichever is written last wins, so restore
writes the delta table LAST and treats it as authoritative; the stored core
offset is applied first only so the slider reads back sensibly.
"""
import glob
import json
import os
import re
import time

SCHEMA = 1
DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
AUTOSAVE_PREFIX = "autosave-"
KEEP_AUTOSAVES = 20


def _slug(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-")
    return s or "unnamed"


def path_for(name):
    return os.path.join(DIR, _slug(name) + ".json")


def _fan_is_manual(gpu):
    """True/False from nvmlDeviceGetFanControlPolicy_v2 (0 = temperature curve,
    1 = manual), or None when the driver will not say."""
    import ctypes
    try:
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


def capture(gpu):
    """Snapshot every knob this tool can write. Values are stored in the units
    the corresponding setter expects, so restore is a straight hand-back."""
    d = gpu.read()
    scale = 1
    try:
        scale = gpu.mem_offset_scale()[0] or 1
    except Exception:
        pass
    mem_units = d.get("mem_off")
    state = {
        "schema": SCHEMA,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device": {
            "name": gpu.static.get("name"),
            "vbios": gpu.static.get("vbios"),
            "driver": gpu.static.get("driver"),
        },
        "core_off_mhz": d.get("core_off"),
        # stored both ways: units is what the driver holds, true MHz is what
        # set_clock_offset(2, ...) takes for a known GDDR type
        "mem_off_units": mem_units,
        "mem_off_true_mhz": (mem_units / scale) if mem_units is not None else None,
        "mem_off_scale": scale,
        # the applied limit is "pl_now_mw"; fan duty lives in the per-fan list
        "power_limit_mw": d.get("pl_now_mw"),
        "volt_boost_pct": None,
        # Duty alone is not restorable state. A card idling at 0% on the auto
        # curve and a card pinned to 0% manually read identically, and handing
        # a captured duty back as a MANUAL duty would pin the fans - a thermal
        # behaviour change, not a restore. So record the policy too.
        "fan_pct": (d.get("fans") or [(None, None)])[0][0],
        "fan_manual": _fan_is_manual(gpu),
        "vf_deltas": None,
    }
    try:
        state["volt_boost_pct"] = gpu.read_voltage_boost()
    except Exception:
        pass
    try:
        pts, err = gpu.read_vf_curve()
        if pts:
            state["vf_deltas"] = {str(p["idx"]): int(p["delta_khz"])
                                  for p in pts}
    except Exception:
        pass
    return state


def save(name, state):
    os.makedirs(DIR, exist_ok=True)
    p = path_for(name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    return p


def load(name):
    with open(path_for(name), encoding="utf-8") as f:
        return json.load(f)


def list_profiles():
    """[(display_name, path, saved_at, is_autosave)] newest first."""
    out = []
    for p in glob.glob(os.path.join(DIR, "*.json")):
        base = os.path.splitext(os.path.basename(p))[0]
        when = ""
        try:
            with open(p, encoding="utf-8") as f:
                when = json.load(f).get("saved_at", "")
        except Exception:
            pass
        out.append((base, p, when, base.startswith(AUTOSAVE_PREFIX)))
    out.sort(key=lambda r: (r[2], r[0]), reverse=True)
    return out


def autosave(gpu, action):
    """Undo point taken immediately before a destructive write. Distinct from a
    named profile: it is not a tune you chose to keep, it is the state you are
    about to leave. Old ones are pruned so the directory stays readable."""
    name = f"{AUTOSAVE_PREFIX}{_slug(action)}-{time.strftime('%Y%m%d-%H%M%S')}"
    p = save(name, capture(gpu))
    autos = [r for r in list_profiles() if r[3]]
    for _n, old, _w, _a in autos[KEEP_AUTOSAVES:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return name, p


def device_mismatch(state, gpu):
    """Profiles are per-card by nature - the V/F table is 103 points of THIS
    silicon. Returns a warning string, or None when it is the same card."""
    dev = state.get("device") or {}
    for key, live in (("name", gpu.static.get("name")),
                      ("vbios", gpu.static.get("vbios"))):
        if dev.get(key) and live and dev[key] != live:
            return (f"profile was saved on {dev.get('name')} / vbios "
                    f"{dev.get('vbios')}, this card is {gpu.static.get('name')} "
                    f"/ vbios {gpu.static.get('vbios')}")
    return None


def restore(gpu, state, apply_curve=True):
    """Write a profile back. Returns [(ok, message)] per knob, in write order.
    Never raises: a knob that fails is reported and the rest still run."""
    results = []

    def step(label, fn):
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"{label}: {e}"
        results.append((ok, msg))

    mw = state.get("power_limit_mw")
    if mw:
        step("power limit", lambda: gpu.set_power_limit_mw(int(mw)))

    vb = state.get("volt_boost_pct")
    if vb is not None:
        step("voltage boost", lambda: gpu.set_voltage_boost(int(vb)))

    mm = state.get("mem_off_true_mhz")
    if mm is not None:
        step("mem offset", lambda: gpu.set_clock_offset(2, int(round(mm))))

    co = state.get("core_off_mhz")
    if co is not None:
        step("core offset", lambda: gpu.set_clock_offset(0, int(co)))

    # Only ever pin the fans if the profile recorded them as manual. When the
    # policy was the temperature curve - or is simply unknown - hand control
    # back to the driver rather than freezing a captured duty.
    manual, fan = state.get("fan_manual"), state.get("fan_pct")
    if manual and fan is not None:
        step("fan", lambda: gpu.set_fan(int(fan)))
    elif manual is False:
        step("fan", gpu.reset_fan)

    # LAST, and authoritative: the delta table subsumes the core offset above.
    if apply_curve and state.get("vf_deltas"):
        deltas = {int(k): int(v) for k, v in state["vf_deltas"].items()}
        step("v/f curve", lambda: gpu.apply_vf_deltas(deltas))

    return results


def summarize(state):
    """One-line description for a menu row or a confirmation banner."""
    bits = []
    co = state.get("core_off_mhz")
    if isinstance(co, int):
        bits.append(f"core {co:+d} MHz")
    mm = state.get("mem_off_true_mhz")
    if isinstance(mm, (int, float)):
        bits.append(f"mem {int(round(mm)):+d} MHz")
    mw = state.get("power_limit_mw")
    if mw:
        bits.append(f"PL {int(mw) // 1000} W")
    vb = state.get("volt_boost_pct")
    if vb is not None:
        bits.append(f"vboost {vb}%")
    d = state.get("vf_deltas") or {}
    nz = sum(1 for v in d.values() if v)
    if d:
        bits.append(f"{nz}/{len(d)} VF deltas set")
    return "   ".join(bits) or "empty profile"
