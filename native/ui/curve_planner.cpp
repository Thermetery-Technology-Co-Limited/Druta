/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_planner.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <unordered_set>

namespace druta::ui {
namespace {
dc_vfp_num integer(std::int64_t value) { dc_vfp_num n{}; n.kind = DC_VFP_INT; n.i = value; return n; }
dc_vfp_num floating(double value) { dc_vfp_num n{}; n.kind = DC_VFP_FLOAT; n.f = value; return n; }
CurvePlan failure(const char* message, dc_status status = DC_E_ARGUMENT) {
    CurvePlan r; r.status = status; r.message = message; return r;
}
bool number(const dc_vfp_num& n, double& out) {
    if (n.kind == DC_VFP_INT) { out = static_cast<double>(n.i); return std::abs(out) <= 9007199254740992.0; }
    if (n.kind == DC_VFP_FLOAT && std::isfinite(n.f)) { out = n.f; return true; }
    return false;
}
}
CurvePlan plan_curve(const PlannerApi& api, const CurvePlanRequest& req) {
    const auto& points = req.points;
    if (points.empty() || points.size() > DC_VFP_POINTS || !req.freq_div || !req.step_khz ||
        req.max_abs_delta_khz <= 0 || req.max_abs_delta_khz > INT32_MAX ||
        !req.max_rise_khz || *req.max_rise_khz <= 0 || !api.predict)
        return failure("Missing curve, scale, step, prediction-model bound, envelope, or planner interface");
    if ((req.kind == CurvePlanKind::deflatten && (!api.deflatten || !req.cap_mv)) ||
        (req.kind == CurvePlanKind::ramp && (!api.ramp || !req.cap_mv || !req.floor_mv)) ||
        (req.kind == CurvePlanKind::rephase && !api.rephase) ||
        (req.kind == CurvePlanKind::hard_deflatten && (!api.hard_deflatten || !req.floor_mv || !req.target_mhz)))
        return failure("Planner interface or voltage bounds unavailable");
    auto finite_positive = [](const std::optional<double>& v) { return !v || (std::isfinite(*v) && *v > 0); };
    if (!finite_positive(req.cap_mv) || !finite_positive(req.floor_mv) || !finite_positive(req.max_mhz) || !finite_positive(req.target_mhz) ||
        (req.floor_mv && req.cap_mv && *req.floor_mv > *req.cap_mv) ||
        (req.max_rise_khz && (*req.max_rise_khz < 0 || *req.max_rise_khz > 9007199254740992LL)))
        return failure("Invalid planner bounds");
    const std::uint64_t raw_step = static_cast<std::uint64_t>(req.step_khz) * req.freq_div;
    if (raw_step > 9007199254740992ULL) return failure("Raw clock step is not representable");
    std::unordered_set<uint32_t> unique;
    for (const auto& p : points) {
        if (!std::isfinite(p.volt_mv) || p.volt_mv <= 0 || !std::isfinite(p.freq_mhz) ||
            p.freq_mhz <= 0 || p.freq_mhz * 1000.0 > 9007199254740992.0 || !unique.insert(p.idx).second)
            return failure("Malformed or duplicate curve point");
    }
    dc_vfp_num cap{}, floor{}, maximum{}, target{}, step = integer(req.step_khz), rise{};
    if (req.target_mhz) {
        const double khz = *req.target_mhz * 1000.0;
        if (!std::isfinite(khz) || khz > 9007199254740992.0) return failure("Target clock is not representable");
        target = floating(khz);
    }
    if (req.cap_mv) cap = floating(*req.cap_mv);
    if (req.floor_mv) floor = floating(*req.floor_mv);
    if (req.max_mhz) {
        const double khz = *req.max_mhz * 1000.0;
        if (!std::isfinite(khz) || khz > 9007199254740992.0) return failure("Maximum clock is not representable");
        maximum = floating(khz);
    }
    if (req.max_rise_khz) rise = integer(*req.max_rise_khz);
    std::vector<std::int64_t> desired; desired.reserve(points.size());
    for (const auto& p : points) desired.push_back(p.delta_khz);
    std::vector<bool> selected(points.size(), false);
    auto changed = [&](std::uint32_t pos, std::int64_t value) {
        if (pos >= points.size() || selected[pos] || value < -req.max_abs_delta_khz ||
            value > req.max_abs_delta_khz || value < INT32_MIN || value > INT32_MAX) return false;
        selected[pos] = true; desired[pos] = value; return true;
    };
    std::string note;
    if (req.kind == CurvePlanKind::rephase) {
        std::vector<dc_vfp_num> raw; raw.reserve(points.size());
        for (const auto& p : points) raw.push_back(integer(p.delta_khz));
        dc_vfp_num wire_step = integer(static_cast<int64_t>(raw_step));
        dc_vfp_rephase out{}; out.size = sizeof(out);
        const dc_status status = api.rephase(raw.data(), raw.size(), &wire_step, &out);
        if (status != DC_OK) return failure("Rephase planner failed", status);
        if (out.change_count > points.size()) return failure("Rephase returned too many points");
        for (uint32_t i = 0; i < out.change_count; ++i)
            if (!changed(out.changes[i].pos, out.changes[i].delta_khz)) return failure("Rephase returned invalid changes");
    } else {
        dc_vfp_deflatten def{}; def.size = sizeof(def);
        dc_vfp_ramp ramp{}; ramp.size = sizeof(ramp);
        dc_vfp_hard hard{}; hard.size = sizeof(hard);
        dc_status status;
        if (req.kind == CurvePlanKind::deflatten)
            status = api.deflatten(points.data(), points.size(), &cap, req.max_mhz ? &maximum : nullptr, nullptr, &step, &def);
        else if (req.kind == CurvePlanKind::ramp)
            status = api.ramp(points.data(), points.size(), &floor, &cap, req.max_mhz ? &maximum : nullptr,
                              req.ramp_use_default_step ? nullptr : &step, &ramp);
        else if (req.kind == CurvePlanKind::hard_deflatten)
            status = api.hard_deflatten(points.data(), points.size(), &floor, &target, &step, &hard);
        else return failure("Unknown planner kind");
        if (status != DC_OK) return failure("Curve planner failed", status);
        const auto count = req.kind == CurvePlanKind::deflatten ? def.change_count : req.kind == CurvePlanKind::ramp ? ramp.change_count : hard.change_count;
        const auto* edits = req.kind == CurvePlanKind::deflatten ? def.changes : req.kind == CurvePlanKind::ramp ? ramp.changes : hard.changes;
        if (count > points.size()) return failure("Planner returned too many points");
        for (uint32_t i = 0; i < count; ++i) {
            const auto& edit = edits[i]; double proposed = 0;
            if (edit.pos >= points.size() || points[edit.pos].idx != edit.idx || !number(edit.delta_khz, proposed))
                return failure("Planner returned an invalid point or delta");
            if (req.kind == CurvePlanKind::hard_deflatten && points[edit.pos].volt_mv < *req.floor_mv - 0.01)
                return failure("Hard de-flatten returned a point below the floor");
            const double baseline = points[edit.pos].delta_khz;
            const double raw = baseline + (proposed - baseline) * req.freq_div;
            if (!std::isfinite(raw) || std::trunc(raw) != raw || raw < INT32_MIN || raw > INT32_MAX ||
                !changed(edit.pos, static_cast<int64_t>(raw))) return failure("Planned delta is unrepresentable or outside its envelope");
        }
        if (req.kind == CurvePlanKind::ramp) note = curve_ramp_note(ramp, *req.max_rise_khz);
        else if (req.kind == CurvePlanKind::hard_deflatten) note = curve_hard_note(hard, *req.max_rise_khz);
    }
    std::vector<dc_vfp_num> khz; khz.reserve(points.size());
    for (size_t i = 0; i < points.size(); ++i) {
        const double physical = points[i].freq_mhz * 1000.0 +
            (static_cast<double>(desired[i]) - points[i].delta_khz) / req.freq_div;
        if (!std::isfinite(physical) || std::abs(physical) > 9007199254740992.0)
            return failure("Planned frequency is not representable");
        khz.push_back(integer(static_cast<int64_t>(std::nearbyint(physical))));
    }
    std::vector<int64_t> voltage_order(points.size()); std::vector<uint32_t> order(points.size());
    const dc_status status = api.predict(points.data(), points.size(), khz.data(), req.max_rise_khz ? &rise : nullptr,
                                          voltage_order.data(), order.data(), points.size());
    if (status != DC_OK) return failure("Curve prediction failed", status);
    std::vector<bool> seen(points.size(), false); std::vector<int64_t> predicted(points.size());
    for (size_t i = 0; i < points.size(); ++i) {
        if (order[i] >= points.size() || seen[order[i]]) return failure("Curve prediction returned invalid ordering");
        seen[order[i]] = true; predicted[order[i]] = voltage_order[i];
    }
    CurvePlan result; result.ok = true; result.status = DC_OK; result.predicted_khz = std::move(predicted);
    for (size_t i = 0; i < points.size(); ++i) if (desired[i] != points[i].delta_khz) {
        result.changes.push_back({points[i].idx, desired[i]});
        result.points.push_back({points[i].idx, points[i].volt_mv, points[i].freq_mhz,
            points[i].freq_mhz + (static_cast<double>(desired[i]) - points[i].delta_khz) / (1000.0 * req.freq_div),
            points[i].delta_khz, static_cast<int32_t>(desired[i])});
    }
    result.message = result.changes.empty() ? "Planner found no changes" : "Plan ready for review";
    if (!result.changes.empty()) result.note = std::move(note);
    return result;
}

namespace {
template <typename... Args> std::string note_text(const char* pattern, Args... args) {
    const int size = std::snprintf(nullptr, 0, pattern, args...);
    if (size <= 0) return {};
    std::string out(static_cast<size_t>(size) + 1, '\0');
    std::snprintf(out.data(), out.size(), pattern, args...);
    out.resize(static_cast<size_t>(size));
    return out;
}
constexpr const char* kArrow = "\xE2\x86\x92"; // U+2192
}

// Written tight, like 1.9.1's: the banner grows with this text, and a note long
// enough to push Apply off screen defeats the banner.
std::string curve_ramp_note(const dc_vfp_ramp& m, std::int64_t max_rise_khz) {
    if (!m.has_band) return {};
    std::string band = note_text("%u rungs %s %u distinct operating points, %.2f %s %.2f mV, top %.0f MHz",
        m.rungs, kArrow, m.delivered, m.lo_mv, kArrow, m.cap_mv, m.top_mhz);
    if (m.dropped_rungs) {
        const std::string reason(m.dropped_reason, strnlen(m.dropped_reason, sizeof(m.dropped_reason)));
        band += note_text("\n%u lower rung(s) dropped - %s. They keep their stock values, which are already increasing.",
            m.dropped_rungs, reason.c_str());
    }
    std::string floor;
    if (m.floor_cost_mhz > 0) {
        floor = note_text("Top clipped at the %.0f MHz hw max, so the floor pays: %.2f mV drops %.0f %s %.0f MHz (-%.0f)",
            m.top_mhz, m.lo_mv, m.floor_before_mhz, kArrow, m.floor_after_mhz, m.floor_cost_mhz);
        if (m.shadowed) {
            const std::string under = m.has_under_band ? note_text("%.0f", m.under_band_mhz) : std::string("unknown");
            floor += note_text(" - below the untouched point under the band (%s MHz), so the driver raises the bottom %u "
                "rung(s) onto it and they arrive as ONE flat", under.c_str(), m.shadowed);
        }
    } else {
        floor = note_text("The floor keeps its %.0f MHz at %.2f mV", m.floor_after_mhz, m.lo_mv);
    }
    if (m.lifted_below) floor += note_text(". %u point(s) BELOW the floor get dragged up too (max +%.0f MHz): the driver "
        "allows at most %.4g MHz between neighbouring points, so the curve under the band is pulled up to meet it - more "
        "clock at LOWER voltages than the band asked for", m.lifted_below, m.lift_max_mhz, max_rise_khz / 1000.0);
    return "RAMP - " + band + ". " + floor + ". Every rung asks more clock at its voltage than stock did - the granularity "
        "fix and the overclock are one edit, so each rung has to be stable.";
}

std::string curve_hard_note(const dc_vfp_hard& m, std::int64_t max_rise_khz) {
    if (!m.has_band) return "HARD DE-FLATTEN - NEEDS THE EXTERNAL VOLTAGE MOD: without it this crashes the driver.";
    std::string park = note_text("the card parks at idx %u @ %.2f mV / %.0f MHz", m.park_idx, m.park_mv, m.park_mhz);
    if (!m.parks_at_floor) park += note_text(", NOT at the %.2f mV floor - the target is too low, a point below the "
        "floor still holds the peak. Raise the target", m.floor_mv);
    std::string cascade;
    if (m.lifted_below) {
        const std::string lowest = m.has_lift_lowest ? note_text("%.2f", m.lift_lowest_mv) : std::string("unknown");
        cascade = note_text(" The driver then drags %u point(s) BELOW the floor up with it, as far down as %s mV, worst "
            "case +%.0f MHz - no delta is written to any of them (%.4g MHz max step, shape law), so this asks the rail for "
            "high clocks well under the floor.", m.lifted_below, lowest.c_str(), m.lift_max_mhz, max_rise_khz / 1000.0);
    }
    return note_text("HARD DE-FLATTEN - %u point(s) at or above %.2f mV flattened onto ONE frequency, %.0f MHz, so ",
               m.n_flat, m.floor_mv, m.target_mhz)
        + park + note_text(". NEEDS THE EXTERNAL VOLTAGE MOD: the GPU will believe it is at %.2f mV and compute low power "
            "from that belief, which is the entire point - without the mod it really IS at %.2f mV and this crashes it.",
            m.floor_mv, m.floor_mv)
        + cascade;
}

std::string curve_stage_note(std::string note, bool hard, bool hard_underneath) {
    if (!hard && hard_underneath) note += " A HARD DE-FLATTEN IS STILL STAGED UNDERNEATH THIS and Apply writes both: "
        "'Revert edits' is what drops it.";
    return note;
}
BelowFloorEstimate estimate_below_floor(const PlannerApi& api, const std::vector<dc_vf_point>& baseline,
    const std::vector<dc_vf_point>& draft, double floor_mv, std::int64_t max_rise_khz) {
    BelowFloorEstimate estimate;
    auto prediction = [&](const std::vector<dc_vf_point>& input, std::vector<std::int64_t>& by_point) {
        if (!api.predict || !max_rise_khz || input.empty()) return false;
        std::vector<dc_vfp_num> khz(input.size());
        for (size_t i = 0; i < input.size(); ++i) {
            const double value = input[i].freq_mhz * 1000.0;
            if (!std::isfinite(value) || value <= 0 || value > 9007199254740992.0) return false;
            khz[i] = integer(static_cast<std::int64_t>(std::nearbyint(value)));
        }
        const dc_vfp_num rise = integer(max_rise_khz);
        std::vector<std::int64_t> values(input.size()); std::vector<std::uint32_t> order(input.size());
        if (api.predict(input.data(), input.size(), khz.data(), &rise, values.data(), order.data(), input.size()) != DC_OK) return false;
        by_point.resize(input.size()); std::vector<bool> seen(input.size());
        for (size_t i = 0; i < input.size(); ++i) {
            if (order[i] >= input.size() || seen[order[i]]) return false;
            seen[order[i]] = true; by_point[order[i]] = values[i];
        }
        return true;
    };
    std::vector<std::int64_t> before, after;
    if (baseline.size() != draft.size() || !prediction(baseline, before) || !prediction(draft, after)) return estimate;
    estimate.available = true;
    for (size_t i = 0; i < draft.size(); ++i) if (draft[i].idx == baseline[i].idx && draft[i].volt_mv < floor_mv && after[i] > before[i]) {
        const double lift = static_cast<double>(after[i] - before[i]) / 1000.0;
        if (!estimate.raised || draft[i].volt_mv < estimate.lowest_mv) estimate.lowest_mv = draft[i].volt_mv;
        ++estimate.raised; estimate.largest_rise_mhz = std::max(estimate.largest_rise_mhz, lift);
    }
    return estimate;
}
}
