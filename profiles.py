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
the SAME table in the driver. Whichever is written last wins, so restore
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
# what capture() could not read. A list, not a flag, so the reason travels with
# the snapshot into the log line the user actually sees.
INCOMPLETE_KEY = "incomplete"


def incomplete(state):
    """What this snapshot is MISSING, as human-readable strings (empty = it is
    whole). A profile written before this field existed reports nothing missing
    unless its V/F table is absent, which is the case that matters."""
    miss = list(state.get(INCOMPLETE_KEY) or [])
    if not miss and not state.get("vf_deltas"):
        miss.append("V/F delta table NOT captured")
    return miss


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
        INCOMPLETE_KEY: [],
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
        else:
            state[INCOMPLETE_KEY].append(
                f"V/F delta table NOT captured ({err or 'no points returned'})")
    except Exception as e:
        state[INCOMPLETE_KEY].append(f"V/F delta table NOT captured ({e})")
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


def autosave(gpu, action):
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
    state = capture(gpu)
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
    if apply_curve:
        if state.get("vf_deltas"):
            deltas = {int(k): int(v) for k, v in state["vf_deltas"].items()}
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
    # last, and unabbreviated: this row is how one snapshot is told from
    # another in the Profiles list, and "it cannot restore your curve" is the
    # single most important thing it can say about one
    miss = incomplete(state)
    if miss:
        bits.append("INCOMPLETE - " + "; ".join(miss))
    return "   ".join(bits) or "empty profile"
