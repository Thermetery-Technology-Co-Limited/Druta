// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Clock offsets, the NVML frequency lock and the card's clock grid. Port of
// nvbackend.GPU.set_clock_offset, _set_pstate20_offset, _keep_vf_shape,
// clock_step_khz, max_rise_khz, step_is_measured, mem_offset_scale,
// memory_offset_step_units, lockable_clocks_by_mem, lock_gpu_clocks,
// last_clock_lock_request and _reset_gpu_clocks.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/telemetry.h"
#include "backend/vfcurve.h"

namespace druta::backend {

inline constexpr std::uint32_t kVfStepKhz = 15000;   // VF_STEP_KHZ: the fallback grid
inline constexpr std::uint32_t kVfMaxRiseBins = 3;   // VF_MAX_RISE_BINS

struct MemOffsetScale {
    std::uint32_t scale;  // NVML units per slider unit
    std::string unit;     // "MHz true" or "MHz eff"
};

struct ClockRow {
    std::uint32_t mem_mhz;
    std::vector<std::uint32_t> graphics;
};

class Clocks {
public:
    Clocks(Gpu& gpu, Telemetry& telemetry, const StaticCapabilities& caps, VfCurve& curve)
        : gpu_(gpu), telemetry_(telemetry), caps_(caps), curve_(curve) {}

    MemOffsetScale mem_offset_scale() const;
    std::uint32_t memory_offset_step_units() const { return 1; }
    std::uint32_t clock_step_khz();     // measured from the lockable table, else kVfStepKhz; cached
    std::uint32_t max_rise_khz() { return kVfMaxRiseBins * clock_step_khz(); }
    bool step_is_measured();
    std::vector<ClockRow> lockable_clocks_by_mem();

    // ctype 0 = GRAPHICS (mhz snapped to this card's grid), 2 = MEMORY (true
    // MHz for a known GDDR type, else effective). mhz is finite and inside the
    // signed 32-bit range (the binding checks the Python types).
    Outcome set_clock_offset(std::uint32_t ctype, double mhz, bool preserve_curve);

    Outcome lock_gpu_clocks(std::int64_t min_mhz, std::int64_t max_mhz);
    std::optional<std::pair<std::uint32_t, std::uint32_t>> last_clock_lock_request() const { return last_lock_; }
    Outcome reset_gpu_clocks(bool allow_pascal_noop = false);

private:
    Outcome set_pstate20_offset(std::uint32_t ctype, std::int64_t units, double mhz, const std::string& unit);
    // (changed, note): changed is nullopt when nothing was written.
    std::pair<std::optional<bool>, std::string> keep_vf_shape(
        const std::vector<std::pair<std::uint32_t, std::int32_t>>& deltas, std::int64_t shift_khz);

    Gpu& gpu_;
    Telemetry& telemetry_;
    const StaticCapabilities& caps_;
    VfCurve& curve_;
    std::optional<std::uint32_t> step_cache_;
    bool step_measured_ = false;
    std::optional<std::pair<std::uint32_t, std::uint32_t>> last_lock_;
};

}  // namespace druta::backend
