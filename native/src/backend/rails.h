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
//
// Increment R3 adds the reference capture and the readers over it (read_volt_rail_limits,
// _volt_rail_profile, volt_rail_diagnostics, volt_rail_limits_supported, volt_rail_limit_fields,
// stock_limit_mv, volt_rail_limits_mv) and two more pieces of per-GPU state that refresh() keeps, as
// _refresh_volt_rail_capabilities keeps them: the reference (_volt_rail_reference, the per-rail bases
// estimated from the first stable paired sample) and the first-read record (_volt_rail_initial_uv,
// the four control deltas in microvolts, set once per rail). The architecture is the paired
// adapter's own (Telemetry::arch(), Python's self.arch()). Every reference and first-read value is an
// estimate for this adapter at the operating point it was read, never a factory default or a
// generation constant.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

#include "backend/gpu.h"

namespace druta::backend {

class Telemetry;

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

inline constexpr double kVoltLimitMinMv = 650.0;   // VOLT_LIMIT_MIN_MV (a request bound, not a measurement)
inline constexpr double kVoltLimitMaxMv = 1200.0;  // VOLT_LIMIT_MAX_MV (the same)
inline constexpr std::uint32_t kArchPascal = 4;    // GPU.ARCH_PASCAL (NVML's enum)

using RailFields = std::array<double, 4>;          // VOLT_LIMIT_FIELDS order, mV
using RailInitial = std::array<std::int32_t, 4>;   // VOLT_LIMIT_FIELDS order, round(delta * 1000)

// One row of read_volt_rail_limits: _read_volt_rail_deltas' row, then the keys the reader adds.
struct RailLimitRow {
    RailDeltas deltas;                    // type, reliability .. vmin
    std::optional<RailFields> base;       // _base_mv: the reference's bases, or {} (nullopt)
    // _headroom_mv is always None here; _reference_kind is a constant text (the binding adds both).
    bool absolute_ok = false;             // _absolute_ok: the rail was in this read's state
    double boost_mv = 0.0;                // _boost_mv: the state's, else 0.0
    std::optional<double> effective_mv;   // _effective_mv: the state's "effective", else None
};
using RailLimits = std::array<std::optional<RailLimitRow>, kRailCount>;

// _volt_rail_profile(): the reference and the first-read record as they stand, rails in insertion
// order (both dicts gain a rail at the same moment, so one order serves both).
struct RailProfile {
    std::array<std::optional<RailFields>, kRailCount> bases;     // "bases" (and "fields": a key here)
    std::array<std::optional<RailInitial>, kRailCount> poweron;  // "poweron"
    std::vector<std::size_t> order;                              // the dicts' key order
    std::uint32_t control_version = kRailControlVersion;         // "control_version"
    bool settle_vmin = false;                                    // "settle_vmin"
    double min_mv = kVoltLimitMinMv, max_mv = kVoltLimitMaxMv;   // "min_mv", "max_mv"
};

// volt_rail_diagnostics' per-rail reasons, in their precedence order.
enum class RailReason : int {
    none = 0,                   // ""
    no_architecture = 1,        // "GPU architecture could not be detected. Retry detection."
    unknown_architecture = 2,   // "Rail limit control is not yet understood for this GPU architecture."
    no_control = 3,             // "The driver did not return this rail's control settings. Retry detection."
    invalid_state = 4,          // "The driver did not return a valid rail status. Retry detection."
    no_reference = 5,           // "Waiting for stable voltage readings to establish this rail's reference."
};
const char* rail_reason_text(RailReason reason);

struct RailDiagnosis {
    std::optional<std::uint32_t> architecture;           // "architecture"
    std::array<bool, kRailCount> present{};              // the rail is a key of "rails"
    std::array<RailReason, kRailCount> reasons{};        // its "reason" ("available" is reason none)
    bool available = false;                              // "available"
    RailReason reason = RailReason::none;                // "reason": none, or rail 0's
    const char* architecture_source = nullptr;           // "architecture_source" (None: nullptr)
};

// stock_limit_mv's outcome: None, a value, or the KeyError of a field outside VOLT_LIMIT_FIELDS.
struct RailStockLimit {
    enum class Kind { none = 0, value = 1, bad_field = 2 } kind = Kind::none;
    double mv = 0.0;
};

// volt_rail_limits_mv(): per rail, abs_limit_mv of each field (NaN where the base is absent).
using RailLimitsMv = std::array<std::optional<RailFields>, kRailCount>;

class Rails {
public:
    explicit Rails(Gpu& gpu, Telemetry& telemetry) : gpu_(gpu), telemetry_(telemetry) {}

    // _read_volt_rail_blocks(function, version): {} without a ready NVAPI half or getter.
    RailBlocks blocks(RailGetter function, std::uint32_t version);
    // _read_volt_rail_deltas(): VoltRailsCtlGet at 0x00020AC8.
    std::array<std::optional<RailDeltas>, kRailCount> deltas();
    // read_volt_rail_state(): nullopt is Python's None (no rail kept).
    std::optional<std::array<std::optional<RailState>, kRailCount>> state();
    // read_rail_live_mv(rail): one state() read, then that rail's live mV or nullopt.
    std::optional<double> live_mv(std::int32_t rail);

    // read_volt_rail_limits(): nullopt is Python's None (no row).
    std::optional<RailLimits> limits();
    // _volt_rail_profile(): nullopt unless the architecture is one of _VOLT_RAIL_ARCHITECTURES.
    std::optional<RailProfile> profile();
    // volt_rail_diagnostics() without the keys the binding fills from its own state.
    RailDiagnosis diagnostics();
    // volt_rail_limits_supported(rail): rail nullopt is Python's None; any rail other than 0 and 1
    // is a key no set holds.
    bool limits_supported(std::optional<std::int32_t> rail);
    // volt_rail_limit_fields(rail): 4 (VOLT_LIMIT_FIELDS) or 0 (the empty tuple).
    std::size_t limit_fields(std::int32_t rail);
    // stock_limit_mv(rail, key) with key as its VOLT_LIMIT_FIELDS index (anything else is bad_field).
    RailStockLimit stock_limit_mv(std::int32_t rail, std::int32_t field);
    // volt_rail_limits_mv(): nullopt when read_volt_rail_limits() is None.
    std::optional<RailLimitsMv> limits_mv();

    // The four caches _refresh_volt_rail_capabilities resets (its _volt_rail_write_error is the
    // writer's, not part of these getters). The reference and the first-read record stay: refresh
    // retries discovery without redefining the session's restore target.
    void refresh() { caches_ = RailCaches{}; }
    const RailCaches& caches() const { return caches_; }

private:
    bool has_reference() const { return !order_.empty(); }

    Gpu& gpu_;
    Telemetry& telemetry_;
    RailCaches caches_;
    std::array<std::optional<RailFields>, kRailCount> reference_;  // _volt_rail_reference[rail]["bases"]
    std::array<std::optional<RailInitial>, kRailCount> initial_;   // _volt_rail_initial_uv[rail]
    std::vector<std::size_t> order_;  // the rails of reference_ in insertion order (initial_ follows it)
};

}  // namespace druta::backend
