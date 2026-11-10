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
#include <vector>

#include "backend/clkdom.h"
#include "backend/clock_bios.h"
#include "backend/clocks.h"
#include "backend/current_limits.h"
#include "backend/client_power_controller.h"
#include "backend/perf_diagnostics.h"
#include "backend/target.h"
#include "backend/fan.h"
#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/power_layouts.h"
#include "backend/power_limit.h"
#include "backend/legacy_p0.h"
#include "backend/rails.h"
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
    druta::backend::ClientPowerController cp;  // the sticky client-power controller (_get_live_power_controller)
    druta::backend::PerfDiagnostics perf;      // the PERF diagnostics reader (its own sticky identity)
    druta::backend::ClockBios clock_bios;      // the clock-BIOS reader (its own sticky identity)
    druta::backend::Rails rails;               // the voltage-rail getters and their caches (GPU._volt_rail_*)
    druta::backend::LegacyP0 legacy_p0;        // the session's legacy P0 request ownership (GPU._legacy_p0_owned)
    druta::backend::PowerLimit pl;             // the board power limit (read / bounds / set_power_limit_mw)
    std::optional<druta::backend::StaticCapabilities> statics;  // GPU.static, read once like Python's
    std::optional<druta::backend::VfCurve> vfcurve;             // carries the layout cache, as GPU._vfp_layout_cache
    std::vector<std::string> reset_messages;  // the last dc_gpu_reset_all's full step messages (G3)
    explicit dc_gpu(const std::string& slot) : gpu(slot), telemetry(gpu), rm(gpu, telemetry), vflock(gpu, telemetry, &rm), clkdom(gpu, telemetry), cl(gpu, telemetry, rm, clkdom), target(gpu, telemetry), cp(gpu, telemetry, rm), perf(gpu, telemetry, rm), clock_bios(gpu, telemetry, rm, clkdom), rails(gpu, telemetry), legacy_p0(gpu, telemetry),
          pl(gpu, telemetry, cp, cl, [this]() -> druta::backend::StaticCapabilities& { capabilities(); return *statics; }) {
        // The sweep's pl_requested_mw is read_power_limit_mw, live fallback included (nvbackend
        // _read_power). The handle owns both, so the reader never outlives what it refers to.
        telemetry.power_limit_reader = [this](bool& from_live) { return pl.read(nullptr, &from_live); };
    }
    dc_gpu(const dc_gpu&) = delete;  // the readers above capture this handle
    dc_gpu& operator=(const dc_gpu&) = delete;
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

// GPU._write_policy_limit on this handle as the client-power controller's peer writer (defined in
// abi_client_power_controller.cpp; the context is the dc_gpu). Shared by dc_cpc_set_request and
// dc_pl_set, which both run it under the handle lock they hold.
druta::backend::CpPeerWrite write_peer(void* context, std::uint32_t policy, std::uint32_t value);

inline void copy_field(char* field, std::size_t size, const std::string& text) {
    std::size_t n = text.size() < size - 1 ? text.size() : size - 1;
    std::memcpy(field, text.data(), n);
    field[n] = '\0';
}

inline void put_outcome(const druta::backend::Outcome& outcome, dc_outcome* out) {
    out->ok = outcome.ok ? 1 : 0;
    copy_field(out->message, sizeof(out->message), outcome.message);
}

// dc_cl_statics (the caller's GPU.static pl_* keys, DC_CL_STATIC_*): shared by the current-limit
// (B2) and power-limit (A8) entries. statics_ok checks the size and each state; get_statics maps
// them to the backend's tri-state keys (an absent and a None key stay distinct).
inline bool statics_ok(const dc_cl_statics* st) {
    auto state_ok = [](int32_t s) { return s == DC_CL_STATIC_ABSENT || s == DC_CL_STATIC_NONE || s == DC_CL_STATIC_VALUE; };
    return st && st->size == sizeof(dc_cl_statics) && state_ok(st->pl_min_state) && state_ok(st->pl_def_state) &&
           state_ok(st->pl_max_state);
}

inline druta::backend::StaticKey static_key(int32_t state, int64_t value) {
    using druta::backend::StaticKey;
    StaticKey key;
    key.kind = state == DC_CL_STATIC_VALUE  ? StaticKey::Kind::value
               : state == DC_CL_STATIC_NONE ? StaticKey::Kind::none
                                            : StaticKey::Kind::absent;
    key.value = value;
    return key;
}

inline druta::backend::StaticTriState get_statics(const dc_cl_statics& st) {
    return {static_key(st.pl_min_state, st.pl_min_mw), static_key(st.pl_def_state, st.pl_def_mw),
            static_key(st.pl_max_state, st.pl_max_mw)};
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

// A dc_rail_write_result from the rail splice's result (dc_rail_write_locked's fields; the R4
// dc_rails_write_outcome embeds one). Sets size; at most 8 observed summaries, each truncated.
inline void put_rail_write_result(dc_rail_write_result* out, const druta::backend::RailWriteResult& r) {
    std::memset(out, 0, sizeof *out);
    out->size = sizeof(dc_rail_write_result);
    out->done = r.done ? 1 : 0;
    out->has_status = r.status ? 1 : 0;
    out->status = r.status.value_or(0u);
    copy_field(out->error, sizeof(out->error), r.error);
    if (r.protocol) {
        out->has_protocol = 1;
        out->get_command = r.protocol->get_command;
        out->set_command = r.protocol->set_command;
        out->packet_size = r.protocol->packet_size;
        out->getter_version = r.protocol->getter_version;
    }
    out->observed_count = static_cast<uint32_t>(r.observed.size() < 8 ? r.observed.size() : 8);
    for (uint32_t i = 0; i < out->observed_count; ++i)
        copy_field(out->observed[i], sizeof(out->observed[i]), r.observed[i]);
}

}  // namespace druta::abi

// Copies a std::string into a fixed char array, truncating.
#define DC_COPY(field, text) ::druta::abi::copy_field(field, sizeof(field), text)
