// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Layer 2 of the RM escape transport: the per-GPU transport cache and its
// validation. Port of nvbackend.GPU._rm_power_transport,
// _current_limit_transport_layouts and the cached attributes they keep
// (DESIGN-TRANSPORT.md 2.6, 3.5).
//
// The cache is validated when used, never when set: fixtures assign a
// transport directly and the next use decides whether it is one. A missing
// escape capability stays a failed read here, as on main (open question D4).
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "backend/gpu.h"
#include "backend/hook.h"
#include "backend/power_layouts.h"
#include "backend/rm_error.h"
#include "backend/telemetry.h"

namespace druta::backend {

struct TransportResult {
    std::optional<CapturedTransport> transport;  // set on success
    RmError error;                               // kind != none otherwise
};

struct LayoutsResult {
    std::vector<const PowerLayout*> layouts;  // (layout, *fallbacks); empty on error
    RmError error;
};

class RmTransport {
public:
    RmTransport(Gpu& gpu, Telemetry& telemetry) : gpu_(gpu), telemetry_(telemetry) {}

    // The cached attributes, as Python keeps them on the GPU object.
    std::optional<CapturedTransport> power;       // _current_limit_transport
    std::optional<std::uint32_t> layout_choice;   // _current_limit_layout_choice
    std::optional<CapturedTransport> legacy_clk;  // _legacy_clk_transport
    Observed observed;                            // _current_limit_observed_transport

    // _current_limit_generation_policies / _current_limit_profile_supported.
    GenerationPolicies generation_policies();
    bool profile_supported();

    // The bare captures (None = nullopt). capture_power resets `observed` first, as Python does.
    std::optional<CapturedTransport> capture_power();
    std::optional<CapturedTransport> capture_legacy_clk();

    // _rm_power_transport: the cached transport or a fresh capture, validated; both error classes.
    TransportResult power_transport();
    // _current_limit_transport_layouts: every layout the captured power GET can be.
    LayoutsResult power_layouts();

    // Python's _current_limit_key(header).
    static PowerLayoutKey key(const CapturedTransport& transport) {
        return PowerLayoutKey{transport.header[14], transport.header[2], transport.header[15]};
    }

private:
    Gpu& gpu_;
    Telemetry& telemetry_;
    bool last_capture_no_escape_path_ = false;
};

}  // namespace druta::backend
