/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_planner.h"
#include <algorithm>
#include <cmath>
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
            status = api.ramp(points.data(), points.size(), &floor, &cap, req.max_mhz ? &maximum : nullptr, &step, &ramp);
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
    return result;
}
}
