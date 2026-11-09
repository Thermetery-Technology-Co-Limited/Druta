// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/rail_limits.h: thin, exception-free wrappers over backend/rail_limits.h.
#include "backend/rail_limits.h"
#include "core/abi_internal.h"

#include <cmath>

namespace {

namespace rail = druta::backend::rail;

dc_status status_of(rail::Error e) {
    switch (e) {
        case rail::Error::none: return DC_OK;
        case rail::Error::key: return DC_E_RL_KEY;
        case rail::Error::type: return DC_E_RL_TYPE;
        case rail::Error::attribute: return DC_E_RL_ATTRIBUTE;
        case rail::Error::conversion: return DC_E_RL_CONVERSION;
        case rail::Error::big_int: return DC_E_RL_BIG_INT;
    }
    return DC_E_ARGUMENT;
}

dc_status value_in(const dc_rl_value& in, rail::Value& out) {
    out = rail::Value{};
    out.f = in.f;
    switch (in.kind) {
        case DC_RL_ABSENT: out.kind = rail::Kind::absent; return DC_OK;
        case DC_RL_NONE: out.kind = rail::Kind::none; return DC_OK;
        case DC_RL_INT:
            // exact within 2^53; a larger int travels as DC_RL_BIG
            if (!(std::fabs(in.f) <= rail::kIntLimit) || std::trunc(in.f) != in.f) return DC_E_ARGUMENT;
            out.kind = rail::Kind::integer;
            return DC_OK;
        case DC_RL_BIG:
            if (std::isnan(in.f) || !(std::fabs(in.f) >= rail::kIntLimit)) return DC_E_ARGUMENT;  // float(2**53 + 1) is 2**53
            out.kind = rail::Kind::big;
            return DC_OK;
        case DC_RL_FLOAT: out.kind = rail::Kind::real; return DC_OK;
        case DC_RL_OTHER:
            out.kind = rail::Kind::other;
            out.truthy = in.truthy != 0;
            out.has_float = in.has_float != 0;
            return DC_OK;
        default: break;
    }
    return DC_E_ARGUMENT;
}

// NULL is the caller's None (or a missing optional argument).
dc_status value_in(const dc_rl_value* in, rail::Value& out) {
    if (!in) {
        out = rail::Value::none();
        return DC_OK;
    }
    return value_in(*in, out);
}

dc_status row_in(const dc_rl_row* in, rail::Row& out) {
    if (!in || in->size != sizeof(dc_rl_row)) return DC_E_ARGUMENT;
    switch (in->bases_kind) {
        case DC_RL_BASES_ABSENT: out.bases = rail::Bases::absent; break;
        case DC_RL_BASES_MAPPING: out.bases = rail::Bases::mapping; break;
        case DC_RL_BASES_NO_GET: out.bases = rail::Bases::no_get; break;
        default: return DC_E_ARGUMENT;
    }
    for (std::size_t i = 0; i < rail::kFieldCount; ++i) {
        dc_status s = value_in(in->limit[i], out.limit[i]);
        if (s == DC_OK) s = value_in(in->base[i], out.base[i]);
        if (s != DC_OK) return s;
    }
    dc_status s = value_in(in->boost_mv, out.boost_mv);
    if (s == DC_OK) s = value_in(in->effective_mv, out.effective_mv);
    if (s == DC_OK) s = value_in(in->headroom_mv, out.headroom_mv);
    if (s == DC_OK) s = value_in(in->type, out.type);
    return s;
}

// NULL is None.
dc_status record_in(const dc_rl_record* in, rail::Record& out, const rail::Record*& use) {
    use = nullptr;
    if (!in) return DC_OK;
    if (in->size != sizeof(dc_rl_record)) return DC_E_ARGUMENT;
    out.truthy = in->truthy != 0;
    dc_status s = value_in(in->raised_ceiling_mv, out.raised_ceiling_mv);
    if (s == DC_OK) s = value_in(in->hold_mv, out.hold_mv);
    if (s == DC_OK) s = value_in(in->margin_mv, out.margin_mv);
    if (s == DC_OK) s = value_in(in->grain_mv, out.grain_mv);
    if (s == DC_OK) s = value_in(in->live_offset_mv, out.live_offset_mv);
    if (s == DC_OK) use = &out;
    return s;
}

static_assert(static_cast<int>(rail::TargetReason::not_numbers) == DC_RL_TARGET_NOT_NUMBERS &&
                  static_cast<int>(rail::TargetReason::field_no_reference) == DC_RL_TARGET_FIELD_NO_REFERENCE,
              "TargetReason follows the DC_RL_TARGET_* numbering");

dc_status num_out(const rail::Result<rail::Num>& r, dc_rl_value* out) {
    if (r.error != rail::Error::none) return status_of(r.error);
    *out = dc_rl_value{};
    out->kind = r.value.is_int ? DC_RL_INT : DC_RL_FLOAT;
    out->f = r.value.v;
    return DC_OK;
}

}  // namespace

extern "C" {

DC_API const char* DC_CALL dc_rl_status_text(dc_status status) {
    switch (status) {
        case DC_E_RL_KEY: return "a limit the row does not have (KeyError)";
        case DC_E_RL_TYPE: return "None or a non-number object in arithmetic or a comparison (TypeError)";
        case DC_E_RL_ATTRIBUTE: return "_base_mv has no .get (AttributeError)";
        case DC_E_RL_CONVERSION: return "float() of the value failed";
        case DC_E_RL_BIG_INT: return "arithmetic on an integer beyond +-2^53, or float() of one beyond the double range";
        default: break;
    }
    return dc_status_text(status);
}

DC_API dc_status DC_CALL dc_rl_constants_get(dc_rl_constants* out) {
    if (!out || out->size != sizeof(dc_rl_constants)) return DC_E_ARGUMENT;
    static_assert(rail::kVoltRailArchitectureCount <= sizeof(out->architectures) / sizeof(out->architectures[0]),
                  "architecture list does not fit dc_rl_constants");
    out->architecture_count = static_cast<uint32_t>(rail::kVoltRailArchitectureCount);
    for (std::size_t i = 0; i < rail::kVoltRailArchitectureCount; ++i)
        out->architectures[i] = rail::kVoltRailArchitectures[i];
    out->max_margin_mv = rail::kHoldHeadroomMaxMarginMv;
    out->fallback_grain_mv = rail::kHoldHeadroomFallbackGrainMv;
    out->grain_min_mv = rail::kHoldHeadroomGrainMinMv;
    out->grain_max_mv = rail::kHoldHeadroomGrainMaxMv;
    return DC_OK;
}

DC_API const char* DC_CALL dc_rl_field_name(uint32_t field) { return rail::field_name(field); }

DC_API dc_status DC_CALL dc_rl_volt_rail_architecture(int32_t has_arch, int64_t arch, int32_t* supported) {
    if (!supported) return DC_E_ARGUMENT;
    *supported = rail::volt_rail_architecture(has_arch != 0, arch);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rl_valid_volt_rail_state(const dc_rl_value* rail_in, const dc_rl_row* state,
                                                     int32_t* valid) {
    if (!valid) return DC_E_ARGUMENT;
    rail::Value r;
    rail::Row row;
    dc_status s = value_in(rail_in, r);
    if (s == DC_OK) s = row_in(state, row);
    if (s != DC_OK) return s;
    const rail::Result<bool> v = rail::valid_volt_rail_state(r, row);
    if (v.error != rail::Error::none) return status_of(v.error);
    *valid = v.value ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rl_volt_boost_headroom_mv(const dc_rl_row* fields, double* out) {
    if (!out) return DC_E_ARGUMENT;
    rail::Row row;
    if (fields) {
        const dc_status s = row_in(fields, row);
        if (s != DC_OK) return s;
    }
    const rail::Result<double> r = rail::volt_boost_headroom_mv(fields ? &row : nullptr);
    if (r.error != rail::Error::none) return status_of(r.error);
    *out = r.value;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rl_abs_limit_mv(const dc_rl_row* fields, uint32_t field, dc_rl_value* out) {
    rail::Row row;
    if (!out || field >= DC_RL_FIELDS) return DC_E_ARGUMENT;
    const dc_status s = row_in(fields, row);
    return s != DC_OK ? s : num_out(rail::abs_limit_mv(row, field), out);
}

DC_API dc_status DC_CALL dc_rl_rail_ceiling_mv(const dc_rl_row* fields, dc_rl_value* out) {
    rail::Row row;
    if (!out) return DC_E_ARGUMENT;
    const dc_status s = row_in(fields, row);
    return s != DC_OK ? s : num_out(rail::rail_ceiling_mv(row), out);
}

DC_API dc_status DC_CALL dc_rl_rail_floor_mv(const dc_rl_row* fields, dc_rl_value* out) {
    rail::Row row;
    if (!out) return DC_E_ARGUMENT;
    const dc_status s = row_in(fields, row);
    return s != DC_OK ? s : num_out(rail::rail_floor_mv(row), out);
}

DC_API dc_status DC_CALL dc_rl_hold_headroom_ceiling_mv(const dc_rl_row* row_in_, dc_rl_value* out) {
    rail::Row row;
    if (!out) return DC_E_ARGUMENT;
    const dc_status s = row_in(row_in_, row);
    return s != DC_OK ? s : num_out(rail::hold_headroom_ceiling_mv(row), out);
}

DC_API dc_status DC_CALL dc_rl_headroom_enforced_mv(const dc_rl_row* row_in_, const dc_rl_value* grain,
                                                    dc_rl_value* out) {
    rail::Row row;
    rail::Value g;
    if (!out) return DC_E_ARGUMENT;
    dc_status s = row_in(row_in_, row);
    if (s == DC_OK) s = value_in(grain, g);
    return s != DC_OK ? s : num_out(rail::headroom_enforced_mv(row, grain ? &g : nullptr), out);
}

DC_API dc_status DC_CALL dc_rl_hold_headroom_targets(const dc_rl_row* fields, const dc_rl_value* hold_mv,
                                                     const dc_rl_value* margin_mv, const dc_rl_value* maximum_mv,
                                                     dc_rl_targets* out) {
    if (!out || out->size != sizeof(dc_rl_targets) || !hold_mv || !margin_mv || !maximum_mv) return DC_E_ARGUMENT;
    rail::Row row;
    rail::Value hold, margin, maximum;
    dc_status s = row_in(fields, row);
    if (s == DC_OK) s = value_in(*hold_mv, hold);
    if (s == DC_OK) s = value_in(*margin_mv, margin);
    if (s == DC_OK) s = value_in(*maximum_mv, maximum);
    if (s != DC_OK) return s;
    const rail::Result<rail::Targets> r = rail::hold_headroom_targets(row, hold, margin, maximum);
    if (r.error != rail::Error::none) return status_of(r.error);
    const uint32_t size = out->size;
    *out = dc_rl_targets{};
    out->size = size;
    out->reason = r.value.planned ? DC_RL_TARGET_PLANNED : static_cast<int32_t>(r.value.reason);
    out->field = static_cast<uint32_t>(r.value.field);
    for (std::size_t i = 0; i < rail::kHeadroomFieldCount; ++i) {
        out->has_target[i] = r.value.has_target[i] ? 1 : 0;
        out->target_mv[i] = r.value.target_mv[i];
    }
    druta::abi::copy_field(out->message, sizeof(out->message), r.value.message);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rl_hold_headroom_bound_mv(const dc_rl_record* record, const dc_rl_record* active,
                                                      double* out) {
    if (!out) return DC_E_ARGUMENT;
    rail::Record a, b;
    const rail::Record *ra = nullptr, *rb = nullptr;
    dc_status s = record_in(record, a, ra);
    if (s == DC_OK) s = record_in(active, b, rb);
    if (s != DC_OK) return s;
    const rail::Result<double> r = rail::hold_headroom_bound_mv(ra, rb);
    if (r.error != rail::Error::none) return status_of(r.error);
    *out = r.value;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_rl_hold_headroom_allowance_mv(const dc_rl_record* record, const dc_rl_record* active,
                                                          double* out) {
    if (!out) return DC_E_ARGUMENT;
    rail::Record a, b;
    const rail::Record *ra = nullptr, *rb = nullptr;
    dc_status s = record_in(record, a, ra);
    if (s == DC_OK) s = record_in(active, b, rb);
    if (s != DC_OK) return s;
    const rail::Result<double> r = rail::hold_headroom_allowance_mv(ra, rb);
    if (r.error != rail::Error::none) return status_of(r.error);
    *out = r.value;
    return DC_OK;
}

}  // extern "C"
