// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The voltage-rail getters (port of nvbackend.GPU._read_volt_rail_blocks, _read_volt_rail_deltas,
// read_volt_rail_state and read_rail_live_mv): increment R2.
//
// Two NVAPI getters, each called with one singleton rail mask at a time (1 << rail, rails 0 and 1;
// the combined mask 3 is never sent): VoltRailsCtlGet (0xA3070DB0, the signed control deltas, at
// version 0x00020AC8 with the 0x00010AC8 fallback after INCOMPATIBLE_STRUCT_VERSION only) and
// VoltRailsAbs (0x5D0634EE, the absolute and live microvolts, at 0x00010AC8). A rail is read when
// the getter returns 0 and echoes both the version and the mask; record contents never decide
// presence (a stock control record is entirely zero). The class keeps the per-GPU caches of the
// Python original with the same semantics: the rails each getter has read (_volt_rail_masks_cache),
// the retry time of a rail that was not read (_volt_rail_retry_after, two seconds on Python's
// time.monotonic(), here druta::monotonic_seconds()), the last attempt per getter and rail
// (_volt_rail_getter_diagnostics) and the version each getter last answered at
// (_volt_rail_read_versions). No board, VBIOS or driver identity is consulted.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

#include "backend/gpu.h"

namespace druta::backend {

// The getter a read goes through (Python's `function` name).
enum class RailGetter : int { Control = 0, Abs = 1 };  // "VoltRailsCtlGet", "VoltRailsAbs"

inline constexpr std::size_t kRailGetters = 2;
inline constexpr std::size_t kRailCount = 2;           // rails 0 (NVVDD) and 1 (MSVDD), positional
inline constexpr std::size_t kRailBlockSize = 8192;    // the zeroed buffer each attempt hands the getter
inline constexpr std::uint32_t kRailControlVersion = 0x00020AC8;  // _read_volt_rail_deltas
inline constexpr std::uint32_t kRailControlV1 = 0x00010AC8;       // the VoltRailsCtlGet fallback layout
inline constexpr std::uint32_t kLiveRailVersion = 0x00010AC8;     // GPU.LIVE_RAIL_VER
inline constexpr std::uint32_t kRailRecordBase = 0x48;            // LIVE_RAIL_BASE (both blocks)
inline constexpr std::uint32_t kRailRecordStride = 0x54;          // LIVE_RAIL_STRIDE (both blocks)
inline constexpr double kRailRetrySeconds = 2.0;
inline constexpr int kNvapiIncompatibleStructVersion = -9;

using RailBlock = std::vector<std::uint8_t>;  // kRailBlockSize bytes, as the getter left them

// One rail's entry of _volt_rail_getter_diagnostics[function]. Python's except branch (status None,
// "error" str(exc)) is reached when ctypes converts a Windows structured exception inside the getter
// into OSError. The core, like every other NVAPI call it makes, has no such guard, so a faulting
// export is not survived: "error" is always None and status always an int here.
struct RailDiagnostic {
    std::uint32_t version = 0;  // "0x%08X" in Python
    std::int32_t status = 0;
    bool valid = false;
    std::int32_t attempts = 0;  // 1 or 2
};

// {rail: buffer}: Python's out dict of _read_volt_rail_blocks.
struct RailBlocks {
    std::array<std::optional<RailBlock>, kRailCount> rails;
};

// _read_volt_rail_deltas' row: type, then VOLT_LIMIT_FIELDS in millivolts.
struct RailDeltas {
    std::int32_t type = 0;
    double reliability = 0, alt_reliability = 0, overvoltage = 0, vmin = 0;
};

// read_volt_rail_state's row: "_boost_mv", then LIVE_RAIL_FIELDS ("type" the raw u32, the rest mV).
struct RailState {
    double boost_mv = 0;
    std::uint32_t type = 0;
    double live = 0, reliability = 0, alt_reliability = 0, overvoltage = 0, effective = 0, vmin = 0;
};

// The four caches, as the GPU keeps them. An absent optional is an absent dict key.
struct RailCaches {
    std::array<std::optional<std::uint32_t>, kRailGetters> known;  // bit r: rail r in the tuple
    std::array<std::array<std::optional<double>, kRailCount>, kRailGetters> retry_after;
    std::array<std::array<std::optional<RailDiagnostic>, kRailCount>, kRailGetters> diagnostics;
    std::array<std::optional<std::uint32_t>, kRailGetters> versions;
};

class Rails {
public:
    explicit Rails(Gpu& gpu) : gpu_(gpu) {}

    // _read_volt_rail_blocks(function, version): {} without a ready NVAPI half or getter.
    RailBlocks blocks(RailGetter function, std::uint32_t version);
    // _read_volt_rail_deltas(): VoltRailsCtlGet at 0x00020AC8.
    std::array<std::optional<RailDeltas>, kRailCount> deltas();
    // read_volt_rail_state(): nullopt is Python's None (no rail kept).
    std::optional<std::array<std::optional<RailState>, kRailCount>> state();
    // read_rail_live_mv(rail): one state() read, then that rail's live mV or nullopt.
    std::optional<double> live_mv(std::int32_t rail);

    // The four caches _refresh_volt_rail_capabilities resets (its _volt_rail_write_error is the
    // writer's, not part of these getters).
    void refresh() { caches_ = RailCaches{}; }
    const RailCaches& caches() const { return caches_; }

private:
    Gpu& gpu_;
    RailCaches caches_;
};

}  // namespace druta::backend
