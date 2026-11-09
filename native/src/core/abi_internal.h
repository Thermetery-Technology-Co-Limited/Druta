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
#include "backend/current_limits.h"
#include "backend/target.h"
#include "backend/fan.h"
#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/power_layouts.h"
#include "backend/rm_transport.h"
#include "backend/telemetry.h"
#include "backend/vfcurve.h"
#include "backend/vflock.h"
#include "drutacore/drutacore.h"

struct dc_gpu {
    druta::backend::Gpu gpu;
    druta::backend::Telemetry telemetry;
    druta::backend::RmTransport rm;  // the RM escape transport cache (GPU._current_limit_transport, ...)
    druta::backend::VfLock vflock;   // carries the recovery record across calls, as GPU._vf_lock_recovery
    druta::backend::ClkDom clkdom;   // the control-block discovery caches (GPU._clkdom_*)
    druta::backend::CurrentLimits cl;  // the current-policy operations and their last table
    druta::backend::TargetBinding target;  // the handle's shared paired-target binding (one sticky identity)
    std::optional<druta::backend::StaticCapabilities> statics;  // GPU.static, read once like Python's
    std::optional<druta::backend::VfCurve> vfcurve;             // carries the layout cache, as GPU._vfp_layout_cache
    explicit dc_gpu(const std::string& slot) : gpu(slot), telemetry(gpu), rm(gpu, telemetry), vflock(gpu, telemetry, &rm), clkdom(gpu, telemetry), cl(gpu, telemetry, rm, clkdom), target(gpu, telemetry) {}
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


// A pure current-limit layout into its caller-sized row (the row's size is kept).
inline void put_power_layout(const druta::backend::PowerLayout& l, dc_power_layout* out) {
    const uint32_t size = out->size;
    std::memset(out, 0, sizeof(*out));
    out->size = size;
    for (std::size_t i = 0; i < DC_POWER_GET_COUNT; ++i) {
        out->sizes[i] = l.sizes[i];
        out->commands[i] = l.commands[i];
    }
    out->has_commands = l.has_commands ? 1 : 0;
    out->info_base = l.info_base;
    out->info_stride = l.info_stride;
    out->status_base = l.status_base;
    out->status_stride = l.status_stride;
    out->control_base = l.control_base;
    out->control_stride = l.control_stride;
    out->has_status_mask_word = l.status_mask_word ? 1 : 0;
    out->status_mask_word = l.status_mask_word.value_or(0u);
    out->has_control_mask_word = l.control_mask_word ? 1 : 0;
    out->control_mask_word = l.control_mask_word.value_or(0u);
}

// The reverse: a caller's row as the pure layout.
inline druta::backend::PowerLayout get_power_layout(const dc_power_layout& in) {
    druta::backend::PowerLayout l{};
    for (std::size_t i = 0; i < DC_POWER_GET_COUNT; ++i) {
        l.sizes[i] = in.sizes[i];
        l.commands[i] = in.has_commands ? in.commands[i] : 0u;
    }
    l.has_commands = in.has_commands != 0;
    l.info_base = in.info_base;
    l.info_stride = in.info_stride;
    l.status_base = in.status_base;
    l.status_stride = in.status_stride;
    l.control_base = in.control_base;
    l.control_stride = in.control_stride;
    if (in.has_status_mask_word) l.status_mask_word = in.status_mask_word;
    if (in.has_control_mask_word) l.control_mask_word = in.control_mask_word;
    return l;
}

}  // namespace druta::abi

// Copies a std::string into a fixed char array, truncating.
#define DC_COPY(field, text) ::druta::abi::copy_field(field, sizeof(field), text)
