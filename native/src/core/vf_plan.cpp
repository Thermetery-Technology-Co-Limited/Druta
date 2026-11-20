// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Port of the static V/F planners of nvbackend.GPU. The comments name the
// Python line each step reproduces; the reasoning behind each planner lives in
// the Python docstrings and is not repeated here.
#include "core/vf_plan.h"

#include <algorithm>
#include <cmath>

namespace druta::vfplan {
namespace {

// ---- Python number semantics -------------------------------------------------------------------

// Exact a <=> b for an int and a float, as Python compares them: -1, 0, 1, or 2 when b is NaN.
int compare_int_double(std::int64_t a, double b) {
    if (std::isnan(b)) return 2;
    if (b >= 9223372036854775808.0) return -1;
    if (b < -9223372036854775808.0) return 1;
    const double whole = std::trunc(b);
    const auto t = static_cast<std::int64_t>(whole);
    if (a < t) return -1;
    if (a > t) return 1;
    const double fraction = b - whole;  // exact
    if (fraction > 0.0) return -1;
    if (fraction < 0.0) return 1;
    return 0;
}

int compare(const Num& a, const Num& b) {
    if (!a.is_float() && !b.is_float()) return a.i < b.i ? -1 : (a.i > b.i ? 1 : 0);
    if (!a.is_float()) return compare_int_double(a.i, b.f);
    if (!b.is_float()) {
        const int c = compare_int_double(b.i, a.f);
        return c == 2 ? 2 : -c;
    }
    if (std::isnan(a.f) || std::isnan(b.f)) return 2;
    return a.f < b.f ? -1 : (a.f > b.f ? 1 : 0);
}

bool gt(const Num& a, const Num& b) { return compare(a, b) == 1; }
bool lt(const Num& a, const Num& b) { return compare(a, b) == -1; }
bool eq(const Num& a, const Num& b) { return compare(a, b) == 0; }
bool le(const Num& a, const Num& b) {
    const int c = compare(a, b);
    return c == -1 || c == 0;
}

// int op int stays int; any float operand makes the result float.
Num add(const Num& a, const Num& b) {
    if (!a.is_float() && !b.is_float()) return Num::of(a.i + b.i);
    return Num::real(a.as_double() + b.as_double());
}

Num sub(const Num& a, const Num& b) {
    if (!a.is_float() && !b.is_float()) return Num::of(a.i - b.i);
    return Num::real(a.as_double() - b.as_double());
}

// `x or default`
bool truthy(const Num& n) {
    if (n.is_none()) return false;
    if (n.is_float()) return n.f != 0.0;  // NaN is truthy
    return n.i != 0;
}

Error checked_float_to_int(double whole, std::int64_t& out) {
    if (whole > static_cast<double>(kIntLimit) || whole < -static_cast<double>(kIntLimit)) return Error::big_int;
    out = static_cast<std::int64_t>(whole);
    return Error::none;
}

// int(x)
Error to_int(const Num& n, std::int64_t& out) {
    if (n.is_none()) return Error::none_int;
    if (!n.is_float()) {
        out = n.i;
        return Error::none;
    }
    if (std::isnan(n.f)) return Error::nan_int;
    if (std::isinf(n.f)) return Error::inf_int;
    return checked_float_to_int(std::trunc(n.f), out);
}

// round(x) of a float: ties to even (the default rounding mode)
Error py_round(double x, std::int64_t& out) {
    if (std::isnan(x)) return Error::nan_int;
    if (std::isinf(x)) return Error::inf_int;
    return checked_float_to_int(std::nearbyint(x), out);
}

// a // b and a % b for b != 0: floor towards negative infinity, the remainder takes b's sign
std::int64_t floordiv(std::int64_t a, std::int64_t b) {
    std::int64_t q = a / b;
    const std::int64_t r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) --q;
    return q;
}

std::int64_t pymod(std::int64_t a, std::int64_t b) {
    std::int64_t r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}

// max(a, b) / min(a, b) of two ints: the first unless the second is strictly beyond it
std::int64_t py_max(std::int64_t a, std::int64_t b) { return b > a ? b : a; }

// ---- shared planner steps ----------------------------------------------------------------------

// khz = [int(round(p["freq_mhz"] * 1000)) for p in points]
Error rounded_khz(const Points& points, std::vector<std::int64_t>& khz) {
    khz.assign(points.size(), 0);
    for (std::size_t i = 0; i < points.size(); ++i) {
        const Error e = py_round(points[i].freq_mhz * 1000.0, khz[i]);
        if (e != Error::none) return e;
    }
    return Error::none;
}

// below_cap(v, cap) is v <= cap + 0.01; above_floor(v, lo) is v >= lo - 0.01. The bound is
// only evaluated when a point is tested, which is when a None bound raises.
Error cap_bound(const Num& cap_mv, double& bound) {
    if (cap_mv.is_none()) return Error::none_add;
    bound = cap_mv.as_double() + 0.01;
    return Error::none;
}

Error floor_bound(const Num& lo_mv, double& bound) {
    if (lo_mv.is_none()) return Error::none_sub;
    bound = lo_mv.as_double() - 0.01;
    return Error::none;
}

// sorted(positions, key=volt_mv): stable. A NaN key makes Python's result depend on its sort
// algorithm's comparison sequence, which this does not reproduce, so it refuses instead.
Error sort_by_volt(const Points& points, std::vector<std::size_t>& positions) {
    if (positions.size() > 1) {
        for (std::size_t i : positions) {
            if (std::isnan(points[i].volt_mv)) return Error::unordered;
        }
    }
    std::stable_sort(positions.begin(), positions.end(),
                     [&](std::size_t a, std::size_t b) { return points[a].volt_mv < points[b].volt_mv; });
    return Error::none;
}

// max(positions, key=volt_mv) / min(...): the first of equals; positions is non-empty
std::size_t max_by_volt(const Points& points, const std::vector<std::size_t>& positions) {
    std::size_t best = positions.front();
    for (std::size_t i : positions) {
        if (points[i].volt_mv > points[best].volt_mv) best = i;
    }
    return best;
}

std::size_t min_by_volt(const Points& points, const std::vector<std::size_t>& positions) {
    std::size_t best = positions.front();
    for (std::size_t i : positions) {
        if (points[i].volt_mv < points[best].volt_mv) best = i;
    }
    return best;
}

// (idx, volt_mv, khz[i] / 1000.0, new[i] / 1000.0, delta_khz + (new[i] - khz[i]))
Change make_change(const Points& points, const std::vector<std::int64_t>& khz, std::size_t i, const Num& planned) {
    Change c;
    c.pos = i;
    c.old_mhz = static_cast<double>(khz[i]) / 1000.0;
    c.new_mhz = planned.as_double() / 1000.0;
    c.delta_khz = add(Num::of(points[i].delta_khz), sub(planned, Num::of(khz[i])));
    return c;
}

void collect_changes(const Points& points, const std::vector<std::int64_t>& khz,
                     const std::vector<std::optional<Num>>& planned, Plan& plan) {
    for (std::size_t i = 0; i < planned.size(); ++i) {
        if (planned[i]) plan.changes.push_back(make_change(points, khz, i, *planned[i]));
    }
}

Error cascade_for(const Points& points, const std::vector<std::int64_t>& khz, const std::vector<std::int64_t>& real,
                  const std::vector<std::size_t>& pos, const std::vector<std::size_t>& idxs, Cascade& out) {
    std::vector<CascadeRow> rows;
    rows.reserve(idxs.size());
    for (std::size_t i : idxs) rows.push_back({Num::of(real[pos[i]]), Num::of(khz[i]), true, points[i].volt_mv});
    return cascade_meta(rows, out);
}

std::vector<std::size_t> inverse(const std::vector<std::size_t>& order) {
    std::vector<std::size_t> pos(order.size(), 0);
    for (std::size_t k = 0; k < order.size(); ++k) pos[order[k]] = k;
    return pos;
}

}  // namespace

// ---- peak_info ---------------------------------------------------------------------------------

Error peak_info(const Points& points, const Num& cap_mv, Peak& out) {
    out = Peak{};
    if (points.size() > kMaxPoints) return Error::argument;
    if (points.empty()) return Error::none;
    std::vector<std::size_t> usable;
    if (cap_mv.is_none()) {
        for (std::size_t i = 0; i < points.size(); ++i) usable.push_back(i);
    } else {
        const double bound = cap_mv.as_double() + 0.01;
        for (std::size_t i = 0; i < points.size(); ++i) {
            if (points[i].volt_mv <= bound) usable.push_back(i);
        }
    }
    if (usable.empty()) return Error::none;
    // peak = max(p["freq_mhz"] for p in usable)
    std::size_t peak = usable.front();
    for (std::size_t i : usable) {
        if (points[i].freq_mhz > points[peak].freq_mhz) peak = i;
    }
    std::vector<std::size_t> at;
    for (std::size_t i : usable) {
        if (points[i].freq_mhz == points[peak].freq_mhz) at.push_back(i);
    }
    if (at.empty()) return Error::empty_index;  // a NaN peak: at[0] of nothing
    const Error e = sort_by_volt(points, at);
    if (e != Error::none) return e;
    out.found = true;
    out.peak_pos = peak;
    out.park_pos = at.front();
    out.n_at_peak = at.size();
    return Error::none;
}

// ---- resolve_vf_point --------------------------------------------------------------------------

Error resolve_vf_point(const Points& points, const Num& volt_mv, std::optional<std::size_t>& out) {
    out.reset();
    if (points.size() > kMaxPoints) return Error::argument;
    if (points.empty()) return Error::none;
    double bound = 0.0;
    Error e = cap_bound(volt_mv, bound);
    if (e != Error::none) return e;
    std::vector<std::size_t> under;
    for (std::size_t i = 0; i < points.size(); ++i) {
        if (points[i].volt_mv <= bound) under.push_back(i);
    }
    if (under.empty()) return Error::none;
    const std::size_t cap = max_by_volt(points, under);
    std::vector<std::size_t> flat;
    for (std::size_t i = 0; i < points.size(); ++i) {
        if (points[i].freq_mhz == points[cap].freq_mhz) flat.push_back(i);
    }
    if (flat.empty()) return Error::empty_min;
    out = min_by_volt(points, flat);
    return Error::none;
}

// ---- compute_deflatten -------------------------------------------------------------------------

Error compute_deflatten(const Points& points, const Num& vcap_mv, const Num& max_khz,
                        const Num& extra_points_above, const Num& step_khz, Plan& plan, DeflattenMeta& meta) {
    plan = Plan{};
    meta = DeflattenMeta{};
    if (points.size() > kMaxPoints) return Error::argument;
    const Num extra = extra_points_above.is_none() ? Num::of(kExtraPointsAboveCap) : extra_points_above;
    std::int64_t step = 0;
    Error e = to_int(truthy(step_khz) ? step_khz : Num::of(kStepKhz), step);
    if (e != Error::none) return e;
    const std::size_t n = points.size();
    std::vector<std::int64_t> khz;
    if ((e = rounded_khz(points, khz)) != Error::none) return e;
    std::vector<std::size_t> below;
    if (n > 0) {
        double bound = 0.0;
        if ((e = cap_bound(vcap_mv, bound)) != Error::none) return e;
        for (std::size_t i = 0; i < n; ++i) {
            if (points[i].volt_mv <= bound) below.push_back(i);
        }
    }
    if (n == 0 || below.empty()) return Error::none;  // {"clamped": False, "boundary_idx": None, "unique": False}

    // B = min(max(below) + max(0, extra_points_above), n - 1)
    const std::size_t top_below = below.back();
    const Num extra_clamped = gt(extra, Num::of(0)) ? extra : Num::of(0);
    const Num sum = add(Num::of(static_cast<std::int64_t>(top_below)), extra_clamped);
    const Num last = Num::of(static_cast<std::int64_t>(n - 1));
    const Num b_num = lt(last, sum) ? last : sum;
    const std::int64_t ceil_before = khz[top_below];
    // peak_below = max(khz[:B]) if B > 0 else -1; a float B > 0 is a float slice index
    if (b_num.is_float()) return Error::float_slice;
    const auto B = static_cast<std::size_t>(b_num.i);
    std::int64_t peak_below = -1;
    if (B > 0) {
        peak_below = khz[0];
        for (std::size_t i = 1; i < B; ++i) peak_below = py_max(peak_below, khz[i]);
    }
    Num target = Num::of(py_max(khz[B], peak_below + step));
    bool clamped = false;
    if (!max_khz.is_none() && gt(target, max_khz)) {
        target = max_khz;
        clamped = true;
    }
    std::vector<std::optional<Num>> planned(n);
    if (!eq(Num::of(khz[B]), target)) planned[B] = target;
    for (std::size_t i = B + 1; i < n; ++i) {
        if (!eq(Num::of(khz[i]), target)) planned[i] = target;
    }
    std::int64_t lowered = 0;
    if (le(target, Num::of(peak_below)) && B > 0) {
        Num above_val = target;
        for (std::size_t i = B; i-- > 0;) {
            if (lt(Num::of(khz[i]), above_val)) break;
            const Num want = sub(above_val, Num::of(step));
            if (le(want, Num::of(0))) break;
            planned[i] = want;
            above_val = want;
            ++lowered;
        }
    }
    collect_changes(points, khz, planned, plan);
    plan.before_mhz = static_cast<double>(ceil_before) / 1000.0;
    plan.after_mhz = target.as_double() / 1000.0;
    meta.full = true;
    meta.clamped = clamped;
    meta.boundary_pos = B;
    meta.unique = gt(target, Num::of(peak_below)) || lowered > 0;
    meta.lowered = lowered;
    meta.lowered_by_mhz = static_cast<double>(lowered * step) / 1000.0;
    return Error::none;
}

// ---- evaluate_curve_law / predict_curve / cascade_meta -----------------------------------------

Error evaluate_curve_law(const std::vector<Num>& khz, const Num& max_rise_khz, std::vector<std::int64_t>& out) {
    out.clear();
    if (khz.size() > kMaxPoints) return Error::argument;
    std::int64_t rise = 0;
    Error e = to_int(truthy(max_rise_khz) ? max_rise_khz : Num::of(kMaxRiseKhz), rise);
    if (e != Error::none) return e;
    std::vector<std::int64_t> values(khz.size(), 0);
    for (std::size_t i = 0; i < khz.size(); ++i) {
        if ((e = to_int(khz[i], values[i])) != Error::none) return e;
    }
    for (std::size_t i = 1; i < values.size(); ++i) {  // non-decreasing
        if (values[i] < values[i - 1]) values[i] = values[i - 1];
    }
    for (std::size_t i = values.size() >= 2 ? values.size() - 1 : 0; i-- > 0;) {  // at most `rise` per point
        if (values[i] < values[i + 1] - rise) values[i] = values[i + 1] - rise;
    }
    out = std::move(values);
    return Error::none;
}

Error predict_curve(const Points& points, const std::vector<Num>& values, const Num& max_rise_khz,
                    std::vector<std::int64_t>& real, std::vector<std::size_t>& order) {
    real.clear();
    order.clear();
    if (points.size() > kMaxPoints || values.size() != points.size()) return Error::argument;
    std::vector<std::size_t> ranked(points.size(), 0);
    for (std::size_t i = 0; i < ranked.size(); ++i) ranked[i] = i;
    Error e = sort_by_volt(points, ranked);
    if (e != Error::none) return e;
    std::vector<Num> in_order;
    in_order.reserve(ranked.size());
    for (std::size_t i : ranked) in_order.push_back(values[i]);
    if ((e = evaluate_curve_law(in_order, max_rise_khz, real)) != Error::none) return e;
    order = std::move(ranked);
    return Error::none;
}

Error cascade_meta(const std::vector<CascadeRow>& rows, Cascade& out) {
    out = Cascade{};
    if (rows.size() > kMaxPoints) return Error::argument;
    std::vector<std::size_t> lifted;
    for (std::size_t r = 0; r < rows.size(); ++r) {
        if (!eq(rows[r].real, rows[r].khz)) lifted.push_back(r);
    }
    out.lifted_below = lifted.size();
    if (lifted.empty()) return Error::none;
    Num best = sub(rows[lifted.front()].real, rows[lifted.front()].khz);
    for (std::size_t r : lifted) {
        const Num lift = sub(rows[r].real, rows[r].khz);
        if (gt(lift, best)) best = lift;
    }
    out.lift_max_mhz = best.as_double() / 1000.0;
    std::optional<std::size_t> lowest;
    for (std::size_t r : lifted) {
        if (!rows[r].has_volt) {
            out.missing_row = r;
            return Error::missing;
        }
        if (!lowest || rows[r].volt_mv < rows[*lowest].volt_mv) lowest = r;
    }
    out.lowest_row = lowest;
    return Error::none;
}

// ---- compute_hard_deflatten --------------------------------------------------------------------

Error compute_hard_deflatten(const Points& points, const Num& floor_mv, const Num& target_khz,
                             const Num& step_khz, Plan& plan, HardMeta& meta) {
    plan = Plan{};
    meta = HardMeta{};
    if (points.size() > kMaxPoints) return Error::argument;
    const std::size_t n = points.size();
    std::vector<std::int64_t> khz;
    Error e = rounded_khz(points, khz);
    if (e != Error::none) return e;
    std::int64_t grid = 0;
    if ((e = to_int(truthy(step_khz) ? step_khz : Num::of(kStepKhz), grid)) != Error::none) return e;
    // target = max(0, int(target_khz) // grid) * grid
    std::int64_t asked = 0;
    if ((e = to_int(target_khz, asked)) != Error::none) return e;
    if (grid == 0) return Error::floordiv_zero;
    const std::int64_t target = py_max(0, floordiv(asked, grid)) * grid;
    meta.target_mhz = static_cast<double>(target) / 1000.0;
    std::vector<std::size_t> band;
    if (n > 0) {
        double bound = 0.0;
        if ((e = floor_bound(floor_mv, bound)) != Error::none) return e;
        for (std::size_t i = 0; i < n; ++i) {
            if (points[i].volt_mv >= bound) band.push_back(i);
        }
    }
    if ((e = sort_by_volt(points, band)) != Error::none) return e;
    if (band.empty()) return Error::none;
    const std::size_t F = band.front();
    std::vector<std::optional<Num>> planned(n);
    for (std::size_t i : band) {
        if (khz[i] != target) planned[i] = Num::of(target);
    }
    std::vector<Num> values(n);
    for (std::size_t i = 0; i < n; ++i) values[i] = planned[i] ? *planned[i] : Num::of(khz[i]);
    std::vector<std::int64_t> real;
    std::vector<std::size_t> order;
    if ((e = predict_curve(points, values, Num::of(kMaxRiseBins * grid), real, order)) != Error::none) return e;
    const std::vector<std::size_t> pos = inverse(order);
    // the arbiter's rule on the predicted curve: highest frequency, then the lowest voltage carrying it
    std::int64_t peak = real.front();
    for (std::int64_t v : real) peak = py_max(peak, v);
    std::vector<std::size_t> at_peak;
    for (std::size_t i = 0; i < n; ++i) {
        if (real[pos[i]] == peak) at_peak.push_back(i);
    }
    const std::size_t park = min_by_volt(points, at_peak);
    std::vector<std::size_t> under;
    for (std::size_t i = 0; i < n; ++i) {
        if (points[i].volt_mv < points[F].volt_mv - 0.01) under.push_back(i);
    }
    meta.has_band = true;
    meta.floor_pos = F;
    meta.n_flat = band.size();
    meta.park_pos = park;
    meta.park_mhz = static_cast<double>(peak) / 1000.0;
    meta.parks_at_floor = park == F;
    Cascade cascade;
    if ((e = cascade_for(points, khz, real, pos, under, cascade)) != Error::none) return e;
    meta.lifted_below = cascade.lifted_below;
    meta.lift_max_mhz = cascade.lift_max_mhz;
    if (cascade.lowest_row) meta.lift_lowest_pos = under[*cascade.lowest_row];
    collect_changes(points, khz, planned, plan);
    plan.before_mhz = static_cast<double>(khz[F]) / 1000.0;
    plan.after_mhz = static_cast<double>(target) / 1000.0;
    return Error::none;
}

// ---- compute_ramp ------------------------------------------------------------------------------

Error compute_ramp(const Points& points, const Num& lo_mv, const Num& cap_mv, const Num& max_khz,
                   const Num& step_khz, Plan& plan, RampMeta& meta) {
    plan = Plan{};
    meta = RampMeta{};
    if (points.size() > kMaxPoints) return Error::argument;
    std::int64_t grid = 0;
    Error e = to_int(truthy(step_khz) ? step_khz : Num::of(kStepKhz), grid);
    if (e != Error::none) return e;
    const std::size_t n = points.size();
    std::vector<std::int64_t> khz;
    if ((e = rounded_khz(points, khz)) != Error::none) return e;
    std::vector<std::size_t> band;
    std::vector<std::size_t> above;
    if (n > 0) {
        double low = 0.0;
        double high = 0.0;
        if ((e = floor_bound(lo_mv, low)) != Error::none) return e;
        if ((e = cap_bound(cap_mv, high)) != Error::none) return e;
        for (std::size_t i = 0; i < n; ++i) {
            if (points[i].volt_mv >= low && points[i].volt_mv <= high) band.push_back(i);
            if (!(points[i].volt_mv <= high)) above.push_back(i);
        }
    }
    if ((e = sort_by_volt(points, band)) != Error::none) return e;
    if (band.empty()) return Error::none;
    const std::size_t L = band.front();
    const std::size_t B = band.back();

    // new[i] = max(stock[i], new[i-1] + grid), ceilinged, then whole bins up from stock
    std::vector<std::int64_t> plan_khz(n, 0);
    bool clamped = false;
    std::optional<std::int64_t> prev;
    for (std::size_t i : band) {
        std::int64_t want = prev ? py_max(khz[i], *prev + grid) : khz[i];
        if (!max_khz.is_none() && gt(Num::of(want), max_khz)) {
            if ((e = to_int(max_khz, want)) != Error::none) return e;
            clamped = true;
        }
        if (grid == 0) return Error::floordiv_zero;
        const std::int64_t bins = py_max(0, floordiv(want - khz[i], grid));
        want = khz[i] + bins * grid;
        plan_khz[i] = want;
        prev = want;
    }
    const std::int64_t top = plan_khz[B];
    std::vector<std::optional<Num>> planned(n);
    for (std::size_t i : band) {
        if (khz[i] != plan_khz[i]) planned[i] = Num::of(plan_khz[i]);
    }
    const std::int64_t floor_after = plan_khz[L];
    std::vector<std::size_t> under;
    for (std::size_t i = 0; i < n; ++i) {
        if (points[i].volt_mv < points[L].volt_mv - 0.01) under.push_back(i);
    }
    std::optional<std::size_t> u;
    if (!under.empty()) u = max_by_volt(points, under);

    std::vector<Num> values(n);
    for (std::size_t i = 0; i < n; ++i) values[i] = planned[i] ? *planned[i] : Num::of(khz[i]);
    std::vector<std::int64_t> real;
    std::vector<std::size_t> order;
    if ((e = predict_curve(points, values, Num::of(kMaxRiseBins * grid), real, order)) != Error::none) return e;
    const std::vector<std::size_t> pos = inverse(order);
    std::size_t shadowed = 0;
    std::vector<std::int64_t> distinct;
    for (std::size_t i : band) {
        if (real[pos[i]] != values[i].i) ++shadowed;
        distinct.push_back(real[pos[i]]);
    }
    std::sort(distinct.begin(), distinct.end());
    distinct.erase(std::unique(distinct.begin(), distinct.end()), distinct.end());
    std::int64_t real_max = real.front();
    for (std::int64_t v : real) real_max = py_max(real_max, v);
    bool unique = real[pos[B]] == real_max;
    if (unique) {
        for (std::size_t i = 0; i < n; ++i) {
            if (points[i].volt_mv < points[B].volt_mv && !(real[pos[i]] < real[pos[B]])) {
                unique = false;
                break;
            }
        }
    }
    std::size_t leveled = 0;
    for (std::size_t i : above) {
        if (planned[i]) ++leveled;
    }
    meta.has_band = true;
    meta.clamped = clamped;
    meta.unique = unique;
    meta.boundary_pos = B;
    meta.lo_pos = L;
    meta.rungs = band.size();
    meta.top_mhz = static_cast<double>(top) / 1000.0;
    meta.floor_before_mhz = static_cast<double>(khz[L]) / 1000.0;
    meta.floor_after_mhz = static_cast<double>(floor_after) / 1000.0;
    meta.floor_cost_mhz = static_cast<double>(khz[L] - floor_after) / 1000.0;
    meta.leveled_above = leveled;
    if (u) meta.under_band_mhz = static_cast<double>(khz[*u]) / 1000.0;
    meta.shadowed = shadowed;
    meta.delivered = distinct.size();
    Cascade cascade;
    if ((e = cascade_for(points, khz, real, pos, under, cascade)) != Error::none) return e;
    meta.lifted_below = cascade.lifted_below;
    meta.lift_max_mhz = cascade.lift_max_mhz;
    if (cascade.lowest_row) meta.lift_lowest_pos = under[*cascade.lowest_row];
    collect_changes(points, khz, planned, plan);
    plan.before_mhz = static_cast<double>(khz[B]) / 1000.0;
    plan.after_mhz = static_cast<double>(top) / 1000.0;
    return Error::none;
}

// ---- compute_rephase ---------------------------------------------------------------------------

Error compute_rephase(const std::vector<Num>& deltas, const Num& step_khz, Rephase& out) {
    out = Rephase{};
    if (deltas.size() > kMaxPoints) return Error::argument;
    if (deltas.empty()) return Error::none;
    std::int64_t grid = 0;
    Error e = to_int(step_khz, grid);
    if (e != Error::none) return e;
    std::vector<std::int64_t> values(deltas.size(), 0);
    std::vector<std::pair<std::int64_t, std::size_t>> counts;  // remainder -> count, first-seen order
    for (std::size_t k = 0; k < deltas.size(); ++k) {
        if ((e = to_int(deltas[k], values[k])) != Error::none) return e;
        if (grid == 0) return Error::mod_zero;
        const std::int64_t r = pymod(values[k], grid);
        auto it = std::find_if(counts.begin(), counts.end(), [&](const auto& c) { return c.first == r; });
        if (it == counts.end()) {
            counts.emplace_back(r, 1);
        } else {
            ++it->second;
        }
    }
    // max(counts, key=counts.get): the first remainder with the highest count
    auto best = counts.begin();
    for (auto it = counts.begin(); it != counts.end(); ++it) {
        if (it->second > best->second) best = it;
    }
    const std::int64_t phase = best->first;
    for (std::size_t k = 0; k < values.size(); ++k) {
        const std::int64_t r = pymod(values[k], grid);
        if (r != phase) out.changes.emplace_back(k, values[k] - pymod(r - phase, grid));
    }
    out.phase = phase;
    return Error::none;
}

}  // namespace druta::vfplan
