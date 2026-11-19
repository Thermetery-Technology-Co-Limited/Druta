/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/vfplan.h>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct PlannerApi {
    decltype(&dc_vfp_compute_deflatten) deflatten = nullptr;
    decltype(&dc_vfp_compute_ramp) ramp = nullptr;
    decltype(&dc_vfp_compute_rephase) rephase = nullptr;
    decltype(&dc_vfp_predict_curve) predict = nullptr;
    decltype(&dc_vfp_compute_hard_deflatten) hard_deflatten = nullptr;
};
enum class CurvePlanKind { deflatten, ramp, rephase, hard_deflatten };
struct CurvePlanRequest {
    CurvePlanKind kind = CurvePlanKind::deflatten;
    std::vector<dc_vf_point> points; // current draft: physical MHz, raw delta kHz
    std::uint32_t freq_div = 0;      // raw delta units per physical unit
    std::uint32_t step_khz = 0;      // verified physical clock step
    std::int64_t max_abs_delta_khz = 0; // verified raw delta envelope
    std::optional<double> cap_mv, floor_mv, max_mhz, target_mhz;
    // Core's software prediction-model bound, not a physical measurement.
    std::optional<std::int64_t> max_rise_khz;
};
struct CurvePlannedPoint {
    std::uint32_t index = 0;
    double volt_mv = 0, before_mhz = 0, after_mhz = 0;
    std::int32_t before_delta_khz = 0, after_delta_khz = 0;
};
struct CurvePlan {
    bool ok = false;
    dc_status status = DC_OK;
    std::string message;
    std::vector<dc_vf_delta> changes;
    std::vector<CurvePlannedPoint> points;
    std::vector<std::int64_t> predicted_khz; // caller point order, physical kHz
    // Released 1.9.1 plan note for the banner (ramp and hard de-flatten only;
    // the other planners leave the staged note alone, as 1.9.1 does).
    std::string note;
};
CurvePlan plan_curve(const PlannerApi& api, const CurvePlanRequest& request);
// 1.9.1 vf_deflatten / vf_hard_deflatten notes from the core planner's own
// metadata. max_rise_khz is the prediction model's neighbour bound the text names.
std::string curve_ramp_note(const dc_vfp_ramp& meta, std::int64_t max_rise_khz);
std::string curve_hard_note(const dc_vfp_hard& meta, std::int64_t max_rise_khz);
// 1.9.1 stage_note: a tamer plan staged over a hard de-flatten keeps saying so.
std::string curve_stage_note(std::string note, bool hard, bool hard_underneath);
// Core's software-model estimate of what a staged hard de-flatten does below
// its floor: draft versus baseline, point by point. Not a measurement.
// available is false when the model, its bound or a consistent geometry is missing.
struct BelowFloorEstimate {
    bool available = false;
    std::size_t raised = 0;
    double lowest_mv = 0, largest_rise_mhz = 0;
};
BelowFloorEstimate estimate_below_floor(const PlannerApi& api, const std::vector<dc_vf_point>& baseline,
    const std::vector<dc_vf_point>& draft, double floor_mv, std::int64_t max_rise_khz);
}
