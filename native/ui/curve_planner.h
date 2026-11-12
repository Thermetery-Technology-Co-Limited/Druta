/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/vfplan.h>
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
};
enum class CurvePlanKind { deflatten, ramp, rephase };
struct CurvePlanRequest {
    CurvePlanKind kind = CurvePlanKind::deflatten;
    std::vector<dc_vf_point> points; // current draft: physical MHz, raw delta kHz
    std::uint32_t freq_div = 0;      // raw delta units per physical unit
    std::uint32_t step_khz = 0;      // verified physical clock step
    std::int64_t max_abs_delta_khz = 0; // verified raw delta envelope
    std::optional<double> cap_mv, floor_mv, max_mhz;
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
};
CurvePlan plan_curve(const PlannerApi& api, const CurvePlanRequest& request);
}
