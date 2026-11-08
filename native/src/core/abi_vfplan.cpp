// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of the pure V/F planners (drutacore/vfplan.h): thin, exception-free
// wrappers over core/vf_plan.h.
#include "drutacore/vfplan.h"

#include <optional>
#include <vector>

#include "core/vf_plan.h"

namespace {

using druta::vfplan::Error;
using druta::vfplan::Num;
using druta::vfplan::Points;

dc_status status_of(Error e) {
    switch (e) {
        case Error::none: return DC_OK;
        case Error::argument: return DC_E_ARGUMENT;
        case Error::floordiv_zero: return DC_E_VFP_FLOORDIV_ZERO;
        case Error::mod_zero: return DC_E_VFP_MOD_ZERO;
        case Error::nan_int: return DC_E_VFP_NAN_INT;
        case Error::inf_int: return DC_E_VFP_INF_INT;
        case Error::none_int: return DC_E_VFP_NONE_INT;
        case Error::none_add: return DC_E_VFP_NONE_ADD;
        case Error::none_sub: return DC_E_VFP_NONE_SUB;
        case Error::empty_index: return DC_E_VFP_EMPTY_INDEX;
        case Error::empty_min: return DC_E_VFP_EMPTY_MIN;
        case Error::float_slice: return DC_E_VFP_FLOAT_SLICE;
        case Error::unordered: return DC_E_VFP_UNORDERED;
        case Error::big_int: return DC_E_VFP_BIG_INT;
        case Error::missing: return DC_E_VFP_MISSING;
    }
    return DC_E_ARGUMENT;
}

// NULL is None. An int outside +-2^53 is refused rather than silently rounded.
dc_status num_in(const dc_vfp_num* in, Num& out) {
    out = Num::none();
    if (!in || in->kind == DC_VFP_NONE) return DC_OK;
    if (in->kind == DC_VFP_INT) {
        if (in->i > druta::vfplan::kIntLimit || in->i < -druta::vfplan::kIntLimit) return DC_E_VFP_BIG_INT;
        out = Num::of(in->i);
        return DC_OK;
    }
    if (in->kind == DC_VFP_FLOAT) {
        out = Num::real(in->f);
        return DC_OK;
    }
    return DC_E_ARGUMENT;
}

dc_vfp_num num_out(const Num& n) {
    dc_vfp_num out{};
    if (n.is_none()) {
        out.kind = DC_VFP_NONE;
    } else if (n.is_float()) {
        out.kind = DC_VFP_FLOAT;
        out.f = n.f;
    } else {
        out.kind = DC_VFP_INT;
        out.i = n.i;
    }
    return out;
}

dc_status nums_in(const dc_vfp_num* in, size_t count, std::vector<Num>& out) {
    out.clear();
    if (count > DC_VFP_POINTS || (count > 0 && !in)) return DC_E_ARGUMENT;
    out.resize(count);
    for (size_t i = 0; i < count; ++i) {
        const dc_status s = num_in(&in[i], out[i]);
        if (s != DC_OK) return s;
    }
    return DC_OK;
}

dc_status points_in(const dc_vf_point* in, size_t count, Points& out) {
    out.clear();
    if (count > DC_VFP_POINTS || (count > 0 && !in)) return DC_E_ARGUMENT;
    out.resize(count);
    for (size_t i = 0; i < count; ++i) out[i] = {in[i].idx, in[i].volt_mv, in[i].freq_mhz, in[i].delta_khz};
    return DC_OK;
}

uint32_t u32(size_t v) { return static_cast<uint32_t>(v); }

void changes_out(const Points& points, const druta::vfplan::Plan& plan, dc_vfp_change* out, uint32_t* count) {
    *count = u32(plan.changes.size());
    for (size_t k = 0; k < plan.changes.size(); ++k) {
        const druta::vfplan::Change& c = plan.changes[k];
        out[k].pos = u32(c.pos);
        out[k].idx = points[c.pos].idx;
        out[k].volt_mv = points[c.pos].volt_mv;
        out[k].old_mhz = c.old_mhz;
        out[k].new_mhz = c.new_mhz;
        out[k].delta_khz = num_out(c.delta_khz);
    }
}

// Every ABI entry runs inside this: no exception crosses the boundary (only allocation can throw,
// and the planners never hold more than DC_VFP_POINTS of anything).
template <typename F>
dc_status guarded(F&& body) {
    try {
        return body();
    } catch (...) {
        return DC_E_ARGUMENT;
    }
}

}  // namespace

extern "C" {

DC_API const char* DC_CALL dc_vfp_status_text(dc_status status) {
    switch (status) {
        case DC_E_VFP_FLOORDIV_ZERO: return "integer division by a zero grid step";
        case DC_E_VFP_MOD_ZERO: return "integer modulo by a zero grid step";
        case DC_E_VFP_NAN_INT: return "cannot convert float NaN to integer";
        case DC_E_VFP_INF_INT: return "cannot convert float infinity to integer";
        case DC_E_VFP_NONE_INT: return "an integer argument is None";
        case DC_E_VFP_NONE_ADD: return "the voltage cap is None";
        case DC_E_VFP_NONE_SUB: return "the voltage floor is None";
        case DC_E_VFP_EMPTY_INDEX: return "no point carries the peak frequency (NaN)";
        case DC_E_VFP_EMPTY_MIN: return "no point carries the resolved frequency (NaN)";
        case DC_E_VFP_FLOAT_SLICE: return "extra_points_above produced a non-integer boundary";
        case DC_E_VFP_UNORDERED: return "a NaN voltage cannot be ordered";
        case DC_E_VFP_BIG_INT: return "an integer is outside the planners' +-2^53 range";
        case DC_E_VFP_MISSING: return "a lifted row has no voltage";
        default: return dc_status_text(status);
    }
}

DC_API dc_status DC_CALL dc_vfp_peak_info(const dc_vf_point* points, size_t count, const dc_vfp_num* cap_mv,
                                          dc_vfp_peak* out) {
    if (!out || out->size != sizeof(dc_vfp_peak)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        Points pts;
        Num cap;
        dc_status s = points_in(points, count, pts);
        if (s == DC_OK) s = num_in(cap_mv, cap);
        if (s != DC_OK) return s;
        druta::vfplan::Peak peak;
        s = status_of(druta::vfplan::peak_info(pts, cap, peak));
        if (s != DC_OK) return s;
        dc_vfp_peak r{};
        r.size = sizeof(dc_vfp_peak);
        r.found = peak.found ? 1 : 0;
        if (peak.found) {
            r.peak_pos = u32(peak.peak_pos);
            r.park_pos = u32(peak.park_pos);
            r.park_idx = pts[peak.park_pos].idx;
            r.n_at_peak = u32(peak.n_at_peak);
            r.peak_mhz = pts[peak.peak_pos].freq_mhz;
            r.park_mv = pts[peak.park_pos].volt_mv;
        }
        *out = r;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_resolve_vf_point(const dc_vf_point* points, size_t count,
                                                 const dc_vfp_num* volt_mv, uint32_t* pos) {
    if (!pos) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        Points pts;
        Num volt;
        dc_status s = points_in(points, count, pts);
        if (s == DC_OK) s = num_in(volt_mv, volt);
        if (s != DC_OK) return s;
        std::optional<size_t> found;
        s = status_of(druta::vfplan::resolve_vf_point(pts, volt, found));
        if (s != DC_OK) return s;
        if (!found) return DC_E_UNAVAILABLE;
        *pos = u32(*found);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_compute_deflatten(const dc_vf_point* points, size_t count,
                                                  const dc_vfp_num* vcap_mv, const dc_vfp_num* max_khz,
                                                  const dc_vfp_num* extra_points_above, const dc_vfp_num* step_khz,
                                                  dc_vfp_deflatten* out) {
    if (!out || out->size != sizeof(dc_vfp_deflatten)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        Points pts;
        Num cap, ceiling, extra, step;
        dc_status s = points_in(points, count, pts);
        if (s == DC_OK) s = num_in(vcap_mv, cap);
        if (s == DC_OK) s = num_in(max_khz, ceiling);
        if (s == DC_OK) s = num_in(extra_points_above, extra);
        if (s == DC_OK) s = num_in(step_khz, step);
        if (s != DC_OK) return s;
        druta::vfplan::Plan plan;
        druta::vfplan::DeflattenMeta meta;
        s = status_of(druta::vfplan::compute_deflatten(pts, cap, ceiling, extra, step, plan, meta));
        if (s != DC_OK) return s;
        dc_vfp_deflatten r{};
        r.size = sizeof(dc_vfp_deflatten);
        changes_out(pts, plan, r.changes, &r.change_count);
        r.ceil_before_mhz = plan.before_mhz;
        r.ceil_after_mhz = plan.after_mhz;
        r.full_meta = meta.full ? 1 : 0;
        r.clamped = meta.clamped ? 1 : 0;
        r.unique = meta.unique ? 1 : 0;
        if (meta.full) {
            r.boundary_pos = u32(meta.boundary_pos);
            r.boundary_idx = pts[meta.boundary_pos].idx;
            r.lowered = meta.lowered;
            r.lowered_by_mhz = meta.lowered_by_mhz;
        }
        *out = r;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_evaluate_curve_law(const dc_vfp_num* khz, size_t count,
                                                   const dc_vfp_num* max_rise_khz, int64_t* out, size_t capacity) {
    if ((count > 0 && !out) || capacity < count) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        std::vector<Num> values;
        Num rise;
        dc_status s = nums_in(khz, count, values);
        if (s == DC_OK) s = num_in(max_rise_khz, rise);
        if (s != DC_OK) return s;
        std::vector<int64_t> curve;
        s = status_of(druta::vfplan::evaluate_curve_law(values, rise, curve));
        if (s != DC_OK) return s;
        for (size_t i = 0; i < curve.size(); ++i) out[i] = curve[i];
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_predict_curve(const dc_vf_point* points, size_t count, const dc_vfp_num* values,
                                              const dc_vfp_num* max_rise_khz, int64_t* real, uint32_t* order,
                                              size_t capacity) {
    if ((count > 0 && (!real || !order)) || capacity < count) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        Points pts;
        std::vector<Num> planned;
        Num rise;
        dc_status s = points_in(points, count, pts);
        if (s == DC_OK) s = nums_in(values, count, planned);
        if (s == DC_OK) s = num_in(max_rise_khz, rise);
        if (s != DC_OK) return s;
        std::vector<int64_t> curve;
        std::vector<size_t> ranked;
        s = status_of(druta::vfplan::predict_curve(pts, planned, rise, curve, ranked));
        if (s != DC_OK) return s;
        for (size_t k = 0; k < curve.size(); ++k) {
            real[k] = curve[k];
            order[k] = u32(ranked[k]);
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_cascade_meta(const dc_vfp_cascade_row* rows, size_t count, dc_vfp_cascade* out) {
    if (!out || out->size != sizeof(dc_vfp_cascade) || count > DC_VFP_POINTS || (count > 0 && !rows)) {
        return DC_E_ARGUMENT;
    }
    return guarded([&]() -> dc_status {
        std::vector<druta::vfplan::CascadeRow> in(count);
        for (size_t r = 0; r < count; ++r) {
            dc_status s = num_in(&rows[r].real, in[r].real);
            if (s == DC_OK) s = num_in(&rows[r].khz, in[r].khz);
            if (s != DC_OK) return s;
            if (in[r].real.is_none() || in[r].khz.is_none()) return DC_E_ARGUMENT;
            in[r].has_volt = rows[r].has_volt != 0;
            in[r].volt_mv = rows[r].volt_mv;
        }
        druta::vfplan::Cascade cascade;
        const Error e = druta::vfplan::cascade_meta(in, cascade);
        dc_vfp_cascade r{};
        r.size = sizeof(dc_vfp_cascade);
        if (e == Error::missing) {
            r.missing_row = u32(cascade.missing_row);
            *out = r;
        }
        if (e != Error::none) return status_of(e);
        r.lifted_below = u32(cascade.lifted_below);
        r.lift_max_mhz = cascade.lift_max_mhz;
        if (cascade.lowest_row) {
            r.has_lowest = 1;
            r.lowest_row = u32(*cascade.lowest_row);
            r.lift_lowest_mv = in[*cascade.lowest_row].volt_mv;
        }
        *out = r;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_compute_hard_deflatten(const dc_vf_point* points, size_t count,
                                                       const dc_vfp_num* floor_mv, const dc_vfp_num* target_khz,
                                                       const dc_vfp_num* step_khz, dc_vfp_hard* out) {
    if (!out || out->size != sizeof(dc_vfp_hard)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        Points pts;
        Num floor, target, step;
        dc_status s = points_in(points, count, pts);
        if (s == DC_OK) s = num_in(floor_mv, floor);
        if (s == DC_OK) s = num_in(target_khz, target);
        if (s == DC_OK) s = num_in(step_khz, step);
        if (s != DC_OK) return s;
        druta::vfplan::Plan plan;
        druta::vfplan::HardMeta meta;
        s = status_of(druta::vfplan::compute_hard_deflatten(pts, floor, target, step, plan, meta));
        if (s != DC_OK) return s;
        dc_vfp_hard r{};
        r.size = sizeof(dc_vfp_hard);
        changes_out(pts, plan, r.changes, &r.change_count);
        r.floor_before_mhz = plan.before_mhz;
        r.target_after_mhz = plan.after_mhz;
        r.target_mhz = meta.target_mhz;
        r.has_band = meta.has_band ? 1 : 0;
        if (meta.has_band) {
            r.n_flat = u32(meta.n_flat);
            r.floor_pos = u32(meta.floor_pos);
            r.floor_idx = pts[meta.floor_pos].idx;
            r.floor_mv = pts[meta.floor_pos].volt_mv;
            r.park_pos = u32(meta.park_pos);
            r.park_idx = pts[meta.park_pos].idx;
            r.park_mv = pts[meta.park_pos].volt_mv;
            r.park_mhz = meta.park_mhz;
            r.parks_at_floor = meta.parks_at_floor ? 1 : 0;
            r.lifted_below = u32(meta.lifted_below);
            r.lift_max_mhz = meta.lift_max_mhz;
            if (meta.lift_lowest_pos) {
                r.has_lift_lowest = 1;
                r.lift_lowest_pos = u32(*meta.lift_lowest_pos);
                r.lift_lowest_mv = pts[*meta.lift_lowest_pos].volt_mv;
            }
        }
        *out = r;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_compute_ramp(const dc_vf_point* points, size_t count, const dc_vfp_num* lo_mv,
                                             const dc_vfp_num* cap_mv, const dc_vfp_num* max_khz,
                                             const dc_vfp_num* step_khz, dc_vfp_ramp* out) {
    if (!out || out->size != sizeof(dc_vfp_ramp)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        Points pts;
        Num lo, cap, ceiling, step;
        dc_status s = points_in(points, count, pts);
        if (s == DC_OK) s = num_in(lo_mv, lo);
        if (s == DC_OK) s = num_in(cap_mv, cap);
        if (s == DC_OK) s = num_in(max_khz, ceiling);
        if (s == DC_OK) s = num_in(step_khz, step);
        if (s != DC_OK) return s;
        druta::vfplan::Plan plan;
        druta::vfplan::RampMeta meta;
        s = status_of(druta::vfplan::compute_ramp(pts, lo, cap, ceiling, step, plan, meta));
        if (s != DC_OK) return s;
        dc_vfp_ramp r{};
        r.size = sizeof(dc_vfp_ramp);
        changes_out(pts, plan, r.changes, &r.change_count);
        r.ceil_before_mhz = plan.before_mhz;
        r.ceil_after_mhz = plan.after_mhz;
        r.has_band = meta.has_band ? 1 : 0;
        r.clamped = meta.clamped ? 1 : 0;
        r.unique = meta.unique ? 1 : 0;
        if (meta.has_band) {
            r.boundary_pos = u32(meta.boundary_pos);
            r.boundary_idx = pts[meta.boundary_pos].idx;
            r.lo_pos = u32(meta.lo_pos);
            r.lo_idx = pts[meta.lo_pos].idx;
            r.lo_mv = pts[meta.lo_pos].volt_mv;
            r.cap_mv = pts[meta.boundary_pos].volt_mv;
        }
        r.rungs = u32(meta.rungs);
        r.top_mhz = meta.top_mhz;
        r.floor_before_mhz = meta.floor_before_mhz;
        r.floor_after_mhz = meta.floor_after_mhz;
        r.floor_cost_mhz = meta.floor_cost_mhz;
        r.leveled_above = u32(meta.leveled_above);
        if (meta.under_band_mhz) {
            r.has_under_band = 1;
            r.under_band_mhz = *meta.under_band_mhz;
        }
        r.shadowed = u32(meta.shadowed);
        r.delivered = u32(meta.delivered);
        r.lifted_below = u32(meta.lifted_below);
        r.lift_max_mhz = meta.lift_max_mhz;
        if (meta.lift_lowest_pos) {
            r.has_lift_lowest = 1;
            r.lift_lowest_pos = u32(*meta.lift_lowest_pos);
            r.lift_lowest_mv = pts[*meta.lift_lowest_pos].volt_mv;
        }
        r.dropped_rungs = u32(meta.dropped_rungs);
        r.dropped_reason[0] = '\0';
        *out = r;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_vfp_compute_rephase(const dc_vfp_num* deltas, size_t count, const dc_vfp_num* step_khz,
                                                dc_vfp_rephase* out) {
    if (!out || out->size != sizeof(dc_vfp_rephase)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        std::vector<Num> values;
        Num step;
        dc_status s = nums_in(deltas, count, values);
        if (s == DC_OK) s = num_in(step_khz, step);
        if (s != DC_OK) return s;
        druta::vfplan::Rephase rephase;
        s = status_of(druta::vfplan::compute_rephase(values, step, rephase));
        if (s != DC_OK) return s;
        dc_vfp_rephase r{};
        r.size = sizeof(dc_vfp_rephase);
        r.phase = rephase.phase;
        r.change_count = u32(rephase.changes.size());
        for (size_t k = 0; k < rephase.changes.size(); ++k) {
            r.changes[k].pos = u32(rephase.changes[k].first);
            r.changes[k].delta_khz = rephase.changes[k].second;
        }
        *out = r;
        return DC_OK;
    });
}

}  // extern "C"
