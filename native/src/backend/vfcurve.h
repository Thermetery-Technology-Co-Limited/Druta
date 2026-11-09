// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The V/F curve: the table's probed layout, the GPU points with their deltas,
// and the read-modify-write of the delta table. Port of nvbackend.GPU.vfp_layout,
// _probe_vfp_entry_count, _vfp_curve_error, read_vf_curve, read_vfp_other_rows,
// apply_vf_deltas, reset_vf_curve, _vf_delta_snapshot and rephase_deltas.
//
// Nothing here caps at a per-architecture constant: the entry count, the
// GPU/other boundary and the frequency scale are read from what the driver
// answers (a hard-coded 103 and then 128 were both wrong once).
#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/structs.h"
#include "backend/telemetry.h"

namespace druta::backend {

struct VfpLayout {
    std::uint32_t n_entries = 0;
    std::vector<std::uint32_t> gpu_idx;    // the leading run of non-decreasing voltage
    std::vector<std::uint32_t> other_idx;  // trailing rows (Pascal: the MEMORY points)
    std::uint32_t freq_div = 1;            // 2 when the GPU rows carry GPC2CLK
    std::string notes;
};

struct VfPoint {
    std::uint32_t idx = 0;
    double volt_mv = 0;
    double freq_mhz = 0;    // evaluated, includes the current delta
    std::int32_t delta_khz = 0;
};

struct VfOtherRow {
    std::uint32_t idx = 0;
    double volt_mv = 0;
    double value = 0;       // raw kHz / 1000
    bool memory = false;    // its rounded value is one of the driver's mem_clocks
};

// (points, err) as read_vf_curve returns it: points or an error text.
struct CurveResult {
    std::optional<std::vector<VfPoint>> points;
    std::string error;
};

struct DeltaSnapshot {
    std::uint32_t freq_div = 1;
    std::vector<std::pair<std::uint32_t, std::int32_t>> deltas;  // idx -> delta_khz, in curve order
};

// Ask for exactly n points: one mask bit per entry, never more than kVfpPoints.
void set_point_masks(std::uint32_t masks[4], std::int64_t nbits);

class VfCurve {
public:
    VfCurve(Gpu& gpu, Telemetry& telemetry, const StaticCapabilities& caps)
        : gpu_(gpu), telemetry_(telemetry), caps_(caps) {}

    std::optional<VfpLayout> layout(bool force = false);  // vfp_layout; cached
    const std::string& layout_error() const { return layout_error_; }
    CurveResult read_vf_curve();
    std::vector<VfOtherRow> read_vfp_other_rows();
    // deltas: idx -> absolute delta_khz, in the caller's order (Python dict order decides the first refusal).
    Outcome apply_vf_deltas(const std::vector<std::pair<std::int64_t, std::int64_t>>& deltas);
    Outcome reset_vf_curve();
    std::optional<DeltaSnapshot> vf_delta_snapshot();
    // rephase_deltas: every delta onto ONE phase of this card's grid (physical step x freq_div),
    // off-phase deltas rounded DOWN to the common phase and written in one apply_vf_deltas.
    // clock_step_khz is the card's physical grid (Clocks::clock_step_khz); it is asked once,
    // after the curve read and before the layout, in the Python's order.
    Outcome rephase_deltas(const std::function<std::uint32_t()>& clock_step_khz);

    static constexpr std::int64_t kMaxAbsDeltaKhz = 1000000;  // |delta| never exceeds 1 GHz

private:
    bool vf_curve_applicable();
    std::uint32_t probe_entry_count();
    static std::string curve_error(const VfpCurve& cv, std::uint32_t n);

    Gpu& gpu_;
    Telemetry& telemetry_;
    const StaticCapabilities& caps_;
    std::optional<VfpLayout> layout_cache_;
    std::string layout_error_;
};

}  // namespace druta::backend
