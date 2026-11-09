// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The pure rail-limit and hold-headroom helpers. Every function follows its nvbackend.GPU original line for
// line; the comments name the Python expression each step stands for.
#include "backend/rail_limits.h"

#include <cmath>
#include <cstdio>
#include <limits>
#include <utility>

namespace druta::backend::rail {

const std::uint32_t kVoltRailArchitectures[kVoltRailArchitectureCount] = {4, 6, 7, 8, 10};

namespace {

constexpr double kNan = std::numeric_limits<double>::quiet_NaN();
constexpr const char* kFieldNames[kFieldCount] = {"reliability", "alt_reliability", "overvoltage", "vmin"};

Value value_of(const Num& n) { return n.is_int ? Value::integer(n.v) : Value::real(n.v); }

// float(value): ok, failed (Python's TypeError or ValueError, which the helpers catch) or overflow (the
// OverflowError of float() of an int beyond the double range, which none of them catches).
enum class Conv { ok, failed, overflow };

Conv to_float(const Value& v, double& out) {
    switch (v.kind) {
        case Kind::integer:
        case Kind::real: out = v.f; return Conv::ok;
        case Kind::big:
            if (!std::isfinite(v.f)) return Conv::overflow;
            out = v.f;
            return Conv::ok;
        case Kind::other:
            if (!v.has_float) return Conv::failed;
            out = v.f;
            return Conv::ok;
        case Kind::absent:
        case Kind::none: break;
    }
    return Conv::failed;
}

// a + b with Python's numeric tower; None or another object is the TypeError, int + big int is refused.
Result<Num> add(const Value& a, const Value& b) {
    Result<Num> r;
    const auto numeric = [](const Value& v) { return v.is_number() || v.kind == Kind::big; };
    if (!numeric(a) || !numeric(b)) {
        r.error = Error::type;
        return r;
    }
    if (a.kind == Kind::big || b.kind == Kind::big) {
        // float + int converts the int to the nearest double (f), raising OverflowError past the double
        // range; int + int is exact in Python and refused here.
        const bool both_int = a.kind != Kind::real && b.kind != Kind::real;
        if (both_int || !std::isfinite(a.kind == Kind::big ? a.f : b.f)) {
            r.error = Error::big_int;
            return r;
        }
        r.value = Num{false, a.f + b.f};
        return r;
    }
    if (a.kind == Kind::integer && b.kind == Kind::integer) {
        const double s = a.f + b.f;
        // Both are exact within 2^53; a sum that reaches it may already be rounded.
        if (!(std::fabs(s) < kIntLimit)) {
            r.error = Error::big_int;
            return r;
        }
        r.value = Num{true, s};
        return r;
    }
    r.value = Num{false, a.f + b.f};
    return r;
}

// min(a, b, ...): keep the first unless a later one is strictly less (a NaN is never less, nor passed).
Num py_min(const Num& current, const Num& item) { return item.v < current.v ? item : current; }
Num py_max(const Num& current, const Num& item) { return item.v > current.v ? item : current; }

}  // namespace

const char* field_name(std::size_t field) { return field < kFieldCount ? kFieldNames[field] : nullptr; }

bool Value::is_true() const {
    switch (kind) {
        case Kind::integer:
        case Kind::real: return f != 0.0;  // NaN is true, -0.0 false
        case Kind::big: return true;
        case Kind::other: return truthy;
        case Kind::absent:
        case Kind::none: break;
    }
    return false;
}

int volt_rail_architecture(bool has_arch, std::int64_t arch) {
    // arch = self.arch(); None -> None; return arch in self._VOLT_RAIL_ARCHITECTURES
    if (!has_arch) return -1;
    for (std::uint32_t a : kVoltRailArchitectures)
        if (arch == static_cast<std::int64_t>(a)) return 1;
    return 0;
}

Result<bool> valid_volt_rail_state(const Value& rail, const Row& state) {
    Result<bool> r;
    // if state.get("type") != {0: 1, 1: 3}.get(rail): return False
    // (0.0, -0.0 and False find key 0, 1.0 and True key 1, as their hashes and equality do in Python; a big
    // int finds neither)
    bool expected_none = true;
    double expected = 0.0;
    if (rail.is_number()) {
        if (rail.f == 0.0) {
            expected_none = false;
            expected = 1.0;
        } else if (rail.f == 1.0) {
            expected_none = false;
            expected = 3.0;
        }
    }
    const Value& type = state.type;
    if (expected_none) {
        if (type.kind != Kind::absent && type.kind != Kind::none) return r;
    } else if (!(type.is_number() && type.f == expected)) {
        return r;
    }
    // all(isinstance(state.get(k), (float, int)) and math.isfinite(state[k])
    //     and 0 < state[k] <= 0xFFFFFFFF / 1000 for k in VOLT_LIMIT_FIELDS)
    const double top = 4294967295.0 / 1000.0;
    for (const Value& v : state.limit) {
        if (v.kind == Kind::big) {
            // math.isfinite() of an int beyond the double range raises OverflowError; any other big int
            // is finite and outside (0, top]
            if (!std::isfinite(v.f)) r.error = Error::big_int;
            return r;
        }
        if (!v.is_number() || !std::isfinite(v.f) || !(0.0 < v.f && v.f <= top)) return r;
    }
    r.value = true;
    return r;
}

Result<double> volt_boost_headroom_mv(const Row* fields) {
    // value = (fields or {}).get("_headroom_mv")
    // return float(value) if value is not None else float("nan")
    Result<double> r;
    r.value = kNan;
    if (!fields) return r;
    const Value& v = fields->headroom_mv;
    if (v.kind == Kind::absent || v.kind == Kind::none) return r;
    switch (to_float(v, r.value)) {
        case Conv::ok: break;
        case Conv::failed: r.error = Error::conversion; break;
        case Conv::overflow: r.error = Error::big_int; break;
    }
    return r;
}

Result<Num> abs_limit_mv(const Row& fields, std::size_t key) {
    // bases = fields.get("_base_mv", cls.VOLT_LIMIT_BASE_MV)   # VOLT_LIMIT_BASE_MV is {}
    // return fields[key] + bases.get(key, float("nan"))
    Result<Num> r;
    if (key >= kFieldCount) {
        r.error = Error::key;
        return r;
    }
    const Value& limit = fields.limit[key];
    if (limit.kind == Kind::absent) {
        r.error = Error::key;
        return r;
    }
    Value base = Value::real(kNan);
    if (fields.bases == Bases::no_get) {
        r.error = Error::attribute;
        return r;
    }
    if (fields.bases == Bases::mapping && fields.base[key].kind != Kind::absent) base = fields.base[key];
    return add(limit, base);
}

Result<Num> rail_ceiling_mv(const Row& fields) {
    // min(abs_limit_mv(fields, "reliability") + fields.get("_boost_mv", 0.0),
    //     abs_limit_mv(fields, "alt_reliability"), abs_limit_mv(fields, "overvoltage"))
    Result<Num> rel = abs_limit_mv(fields, reliability);
    if (rel.error != Error::none) return rel;
    const Value boost = fields.boost_mv.kind == Kind::absent ? Value::real(0.0) : fields.boost_mv;
    Result<Num> first = add(value_of(rel.value), boost);
    if (first.error != Error::none) return first;
    Result<Num> alt = abs_limit_mv(fields, alt_reliability);
    if (alt.error != Error::none) return alt;
    Result<Num> ov = abs_limit_mv(fields, overvoltage);
    if (ov.error != Error::none) return ov;
    first.value = py_min(py_min(first.value, alt.value), ov.value);
    return first;
}

Result<Num> rail_floor_mv(const Row& fields) { return abs_limit_mv(fields, vmin); }

namespace {

// try: effective = float(row.get("_effective_mv")) except (TypeError, ValueError): effective = nan
Result<double> effective_of(const Row& row) {
    Result<double> r;
    r.value = kNan;
    const Conv c = to_float(row.effective_mv, r.value);
    if (c == Conv::failed) r.value = kNan;
    if (c == Conv::overflow) r.error = Error::big_int;
    return r;
}

}  // namespace

Result<Num> hold_headroom_ceiling_mv(const Row& row) {
    Result<Num> estimate = rail_ceiling_mv(row);
    if (estimate.error != Error::none) return estimate;
    const Result<double> eff = effective_of(row);
    if (eff.error != Error::none) {
        estimate.error = eff.error;
        return estimate;
    }
    const double effective = eff.value;
    if (std::isfinite(effective) && effective > 0) {
        // return min(estimate, effective) if math.isfinite(estimate) else effective
        if (std::isfinite(estimate.value.v)) {
            estimate.value = py_min(estimate.value, Num{false, effective});
        } else {
            estimate.value = Num{false, effective};
        }
    }
    return estimate;
}

Result<Num> headroom_enforced_mv(const Row& row, const Value* grain) {
    Result<Num> estimate = rail_ceiling_mv(row);
    if (estimate.error != Error::none) return estimate;
    const Result<double> eff = effective_of(row);
    if (eff.error != Error::none) {
        estimate.error = eff.error;
        return estimate;
    }
    const Num effective{false, eff.value};
    // values = [v for v in (estimate, effective) if math.isfinite(v) and v > 0]
    Num values[2];
    std::size_t n = 0;
    for (const Num& v : {estimate.value, effective})
        if (std::isfinite(v.v) && v.v > 0) values[n++] = v;
    Result<Num> r;
    if (n == 0) {
        r.value = Num{false, kNan};
        return r;
    }
    Num enforced = values[0];  // max(values)
    for (std::size_t i = 1; i < n; ++i) enforced = py_max(enforced, values[i]);
    if (std::isfinite(estimate.value.v) && estimate.value.v > 0) {
        // step = grain if grain and grain > 0 else cls.HOLD_HEADROOM_FALLBACK_GRAIN_MV
        Value step = Value::real(kHoldHeadroomFallbackGrainMv);
        if (grain && grain->is_true()) {
            if (!grain->is_number() && grain->kind != Kind::big) {  // grain > 0 of a non-number
                r.error = Error::type;
                return r;
            }
            if (grain->f > 0) step = *grain;
        }
        // enforced = min(enforced, estimate + step)
        Result<Num> sum = add(value_of(estimate.value), step);
        if (sum.error != Error::none) return sum;
        enforced = py_min(enforced, sum.value);
    }
    r.value = enforced;
    return r;
}

Result<Targets> hold_headroom_targets(const Row& fields, const Value& hold_mv, const Value& margin_mv,
                                      const Value& maximum_mv) {
    Result<Targets> r;
    Targets& t = r.value;
    auto refuse = [&](TargetReason reason, std::string message) {
        t.planned = false;
        t.reason = reason;
        t.message = std::move(message);
        return r;
    };
    // hold_mv, margin_mv, maximum_mv = float(hold_mv), float(margin_mv), float(maximum_mv)
    double hold = 0.0, margin = 0.0, maximum = 0.0;
    for (auto [in, out] : {std::pair<const Value*, double*>{&hold_mv, &hold}, {&margin_mv, &margin},
                           {&maximum_mv, &maximum}}) {
        const Conv c = to_float(*in, *out);
        if (c == Conv::overflow) {
            r.error = Error::big_int;
            return r;
        }
        if (c == Conv::failed) return refuse(TargetReason::not_numbers, "hold, margin and bound must be numbers");
    }
    if (!(std::isfinite(hold) && std::isfinite(margin) && std::isfinite(maximum)))
        return refuse(TargetReason::not_finite, "hold, margin and bound must be finite");
    if (margin <= 0) return refuse(TargetReason::margin_not_positive, "headroom margin must be positive");
    char text[768];
    if (margin > kHoldHeadroomMaxMarginMv) {
        std::snprintf(text, sizeof(text), "headroom margin exceeds Druta's %g mV maximum", kHoldHeadroomMaxMarginMv);
        return refuse(TargetReason::margin_too_large, text);
    }
    const double want = hold + margin;
    if (hold > maximum + 1e-6) {
        std::snprintf(text, sizeof(text), "hold %.2f mV exceeds Druta's %.0f mV limit bound", hold, maximum);
        return refuse(TargetReason::hold_too_high, text);
    }
    // boost = float(fields.get("_boost_mv", 0.0) or 0.0)
    double boost = 0.0;
    if (fields.boost_mv.is_true()) {
        const Conv c = to_float(fields.boost_mv, boost);
        if (c == Conv::overflow) {
            r.error = Error::big_int;
            return r;
        }
        if (c == Conv::failed)
            return refuse(TargetReason::boost_not_number, "the reported boost contribution is not a number");
    }
    if (!std::isfinite(boost))
        return refuse(TargetReason::boost_not_finite, "the reported boost contribution is not finite");
    for (std::size_t key = 0; key < kHeadroomFieldCount; ++key) {
        if (fields.limit[key].kind == Kind::absent) {
            t.field = key;
            std::snprintf(text, sizeof(text), "%s is not readable on this rail", kFieldNames[key]);
            return refuse(TargetReason::field_missing, text);
        }
        const Result<Num> current = abs_limit_mv(fields, key);
        if (current.error != Error::none) {
            r.error = current.error;
            return r;
        }
        if (!std::isfinite(current.value.v)) {
            t.field = key;
            std::snprintf(text, sizeof(text), "%s has no readable reference", kFieldNames[key]);
            return refuse(TargetReason::field_no_reference, text);
        }
        const double added = key == reliability ? boost : 0.0;
        if (current.value.v + added < want - 1e-6) {
            t.has_target[key] = true;
            t.target_mv[key] = want - added;
        }
    }
    t.planned = true;
    return r;
}

namespace {

const Record* chosen(const Record* record, const Record* active) {
    // record or self.hold_headroom_record() or {}
    if (record && record->truthy) return record;
    if (active && active->truthy) return active;
    return nullptr;  // {}: every key absent
}

}  // namespace

Result<double> hold_headroom_bound_mv(const Record* record, const Record* active) {
    Result<double> out;
    out.value = kNan;
    const Record* r = chosen(record, active);
    if (!r) return out;  // float({}.get(...)) fails, then {}["hold_mv"] is a KeyError
    // bound = record.get("raised_ceiling_mv"); try: bound = float(bound) except (TypeError, ValueError): nan
    double bound = kNan;
    Conv c = to_float(r->raised_ceiling_mv, bound);
    if (c == Conv::overflow) {
        out.error = Error::big_int;
        return out;
    }
    if (c == Conv::failed) bound = kNan;
    if (!std::isfinite(bound)) {
        // bound = float(record["hold_mv"]) + float(record["margin_mv"]) except (KeyError, TypeError, ValueError)
        double hold = 0.0, margin = 0.0;
        c = to_float(r->hold_mv, hold);
        if (c == Conv::ok) c = to_float(r->margin_mv, margin);
        if (c == Conv::overflow) out.error = Error::big_int;
        if (c != Conv::ok) return out;
        bound = hold + margin;
    }
    out.value = bound;
    return out;
}

Result<double> hold_headroom_allowance_mv(const Record* record, const Record* active) {
    Result<double> out;
    const Record* r = chosen(record, active);
    const Value none = Value::absent();
    const Value& grain_v = r ? r->grain_mv : none;
    const Value& offset_v = r ? r->live_offset_mv : none;
    // try: grain = float(record.get("grain_mv") or FALLBACK); offset = float(record.get("live_offset_mv") or 0.0)
    // except (TypeError, ValueError): grain, offset = FALLBACK, 0.0
    double grain = kHoldHeadroomFallbackGrainMv;
    double offset = 0.0;
    Conv c = grain_v.is_true() ? to_float(grain_v, grain) : Conv::ok;
    if (c == Conv::ok && offset_v.is_true()) c = to_float(offset_v, offset);
    if (c == Conv::overflow) {
        out.error = Error::big_int;
        return out;
    }
    if (c == Conv::failed) {
        grain = kHoldHeadroomFallbackGrainMv;
        offset = 0.0;
    }
    if (!(std::isfinite(grain) && grain > 0)) grain = kHoldHeadroomFallbackGrainMv;
    if (!(std::isfinite(offset) && offset >= 0)) offset = 0.0;
    out.value = offset + grain / 2.0;
    return out;
}

}  // namespace druta::backend::rail
