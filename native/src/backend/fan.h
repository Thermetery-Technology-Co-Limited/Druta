// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Fan control: requested levels and policies through NVML, or the classic
// CoolerSettings / ClientFanCoolers NVAPI interfaces. Port of
// nvbackend.GPU.read_fan_control_state, read_fan_manual, fan_capabilities,
// set_fan, reset_fan, _set_nvapi_fans and restore_fan_control_state.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/telemetry.h"

namespace druta::backend {

struct FanCapabilities {
    bool manual = false;
    bool automatic = false;
    std::string source = "nvml";
    std::uint32_t min = 30;
    std::uint32_t max = 100;
};

// A saved fan snapshot row as the caller hands it in. The flags carry what
// Python's dict checks would see, so every refusal keeps its exact text and
// order (restore_fan_control_state in nvbackend).
struct FanSnapshotRow {
    bool has_manual = false;  // isinstance(old, dict) and isinstance(old.get("manual"), bool)
    bool manual = false;
    bool has_id = false;
    std::uint32_t id = 0;
    bool level_valid = false;  // an integer-valued number in 0..100
    std::int32_t level = 0;
    bool has_policy = false;
    std::uint32_t policy = 0;
};

struct FanSnapshot {
    std::string source;  // "nvml", "nvapi_cooler" or "nvapi_client"; anything else is refused
    std::vector<FanSnapshotRow> fans;
};

class FanControl {
public:
    FanControl(Gpu& gpu, Telemetry& telemetry, const StaticCapabilities& caps)
        : gpu_(gpu), telemetry_(telemetry), caps_(caps) {}

    std::optional<NativeFanState> read_fan_control_state();
    std::optional<bool> read_fan_manual();
    FanCapabilities fan_capabilities();
    Outcome set_fan(std::int32_t percent);
    Outcome reset_fan();
    Outcome restore_fan_control_state(const FanSnapshot& state);

private:
    Outcome set_nvapi_fans(std::optional<std::int32_t> percent);
    std::uint32_t fan_min() const { return caps_.fan_min.value_or(30); }
    std::uint32_t fan_max() const { return caps_.fan_max.value_or(100); }

    Gpu& gpu_;
    Telemetry& telemetry_;
    const StaticCapabilities& caps_;
};

}  // namespace druta::backend
