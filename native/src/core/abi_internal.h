// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Shared by the C ABI translation units (abi*.cpp) only: the opaque handle
// behind dc_gpu and the small helpers that copy C++ results into the
// caller-sized C structs. Nothing here is part of the ABI itself.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <optional>
#include <string>

#include "backend/clkdom.h"
#include "backend/clocks.h"
#include "backend/fan.h"
#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/telemetry.h"
#include "backend/vfcurve.h"
#include "backend/vflock.h"
#include "drutacore/drutacore.h"

struct dc_gpu {
    druta::backend::Gpu gpu;
    druta::backend::Telemetry telemetry;
    druta::backend::VfLock vflock;  // carries the recovery record across calls, as GPU._vf_lock_recovery
    druta::backend::ClkDom clkdom;  // the control-block discovery caches (GPU._clkdom_*)
    std::optional<druta::backend::StaticCapabilities> statics;  // GPU.static, read once like Python's
    std::optional<druta::backend::VfCurve> vfcurve;             // carries the layout cache, as GPU._vfp_layout_cache
    explicit dc_gpu(const std::string& slot) : gpu(slot), telemetry(gpu), vflock(gpu, telemetry), clkdom(gpu, telemetry) {}
    const druta::backend::StaticCapabilities& capabilities() {
        if (!statics) statics = telemetry.read_static();
        return *statics;
    }
    druta::backend::VfCurve& curve() {
        if (!vfcurve) vfcurve.emplace(gpu, telemetry, capabilities());
        return *vfcurve;
    }
    std::optional<druta::backend::Clocks> clocks_;  // carries the grid cache and the last lock request
    druta::backend::Clocks& clocks() {
        if (!clocks_) clocks_.emplace(gpu, telemetry, capabilities(), curve());
        return *clocks_;
    }
    druta::backend::FanControl fan() { return druta::backend::FanControl(gpu, telemetry, capabilities()); }
};

namespace druta::abi {

inline void copy_field(char* field, std::size_t size, const std::string& text) {
    std::size_t n = text.size() < size - 1 ? text.size() : size - 1;
    std::memcpy(field, text.data(), n);
    field[n] = '\0';
}

inline void put_outcome(const druta::backend::Outcome& outcome, dc_outcome* out) {
    out->ok = outcome.ok ? 1 : 0;
    copy_field(out->message, sizeof(out->message), outcome.message);
}

// has == 0 is Python's None.
inline std::optional<int64_t> optional_arg(int32_t has, int64_t value) {
    return has ? std::optional<int64_t>(value) : std::nullopt;
}
inline std::optional<std::uint32_t> optional_u32(int32_t has, std::uint32_t value) {
    return has ? std::optional<std::uint32_t>(value) : std::nullopt;
}

}  // namespace druta::abi

// Copies a std::string into a fixed char array, truncating.
#define DC_COPY(field, text) ::druta::abi::copy_field(field, sizeof(field), text)
