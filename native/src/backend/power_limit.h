// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The board power limit (increment A8): a port of nvbackend.GPU's POWER_LIMIT_XOC_MAX_MW,
// _normal_power_bounds_mw, _power_request_bounds_mw, power_limit_bounds_mw, _read_power_limit_nvml,
// read_power_limit_mw, set_power_limit_mw and _set_power_limit_nvml. It composes what is already
// native: Telemetry::read_power_limit_nvml (the configured NVML request), the client-power
// controller's live read (A6) and request SET with its compensation (A7), and CurrentLimits'
// pin re-application (B3). No driver version, board, device, subsystem or VBIOS identity is
// consulted anywhere. Everything is session-only: an NVML write or a live SET is the user's
// request of this call; nothing is stored for, or re-applied after, a GPU driver restart.
#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <string>

#include "backend/client_power_controller.h"
#include "backend/current_limits.h"
#include "backend/gpu.h"
#include "backend/telemetry.h"

namespace druta::backend {

// GPU.POWER_LIMIT_XOC_MAX_MW: the software request envelope, not a physical ceiling.
inline constexpr std::int64_t kPowerLimitXocMaxMw = 1999000;

struct PowerBounds {
    std::int64_t low = 0, high = 0;
};

// _normal_power_bounds_mw over the caller's static keys (absent and None alike fall back).
PowerBounds normal_power_bounds(const StaticTriState& st);
// _power_request_bounds_mw(current, xoc=, live=): current is Python's `current` when
// `type(current) is int` holds (nullopt otherwise: None, a float, a bool); the binding applies
// that gate, the range gate 0 < current < 0xFFFFFFFF is applied here.
PowerBounds power_request_bounds(const StaticTriState& st, std::optional<std::int64_t> current, bool xoc, bool live);
// f"{mw/1000:g}" for the watt figures of the messages (mw within +-2**53, where the quotient is exact).
std::string watts_g(std::int64_t mw);

// _set_power_limit_nvml's (ok, message, got).
struct NvmlLimitSet {
    bool ok = false;
    std::string message;
    std::optional<std::uint32_t> got;
};

// set_power_limit_mw's (ok, message), or the IndexError Python lets escape from
// _reapply_power_policy_pins (in_bounds false; ok and message unset).
struct PowerLimitSet {
    bool in_bounds = true;
    bool ok = false;
    std::string message;
};

// The live-read hook of a caller that keeps its own `static` dict: before a live read the core
// cannot see it, so it is passed in (StaticTriState) and updated here where Python's dict changes.
class PowerLimit {
public:
    // statics: the handle's capability store (GPU.static on the core side), which the live read
    // back-fills (ClientPowerController::live_read).
    PowerLimit(Gpu& gpu, Telemetry& telemetry, ClientPowerController& cp, CurrentLimits& cl,
               std::function<StaticCapabilities&()> statics)
        : gpu_(gpu), telemetry_(telemetry), cp_(cp), cl_(cl), statics_(std::move(statics)) {}

    // _read_power_limit_nvml: Telemetry::read_power_limit_nvml (status 0 and a nonzero value).
    std::optional<std::uint32_t> read_nvml();
    // read_live_power_request on the sticky controller. On success the caller's absent or None
    // pl_* keys (st, when given) take the handle store's values after its back-fill: the keys the
    // binding re-syncs from capabilities() after the call (A6's contract).
    CpRead<CpReading> live_read(StaticTriState* st);
    // read_power_limit_mw: NVML, else (NVAPI live) the live request, else nullopt. *from_live (when
    // given) is set true when the value came from a live read that succeeded, untouched otherwise.
    std::optional<std::uint32_t> read(StaticTriState* st, bool* from_live = nullptr);
    // power_limit_bounds_mw(xoc=): xoc is the caller's flag (the binding defaults it to
    // voltage_xoc_enabled).
    PowerBounds bounds(StaticTriState& st, bool xoc);
    // set_power_limit_mw(mw): mw is the binding's int; outside 0 < mw < 2**32 it is refused with
    // Python's text before any read. xoc is bool(voltage_xoc_enabled). peer is
    // GPU._write_policy_limit for the A7 compensation. The caller holds the handle lock.
    PowerLimitSet set(std::int64_t mw, bool xoc, StaticTriState& st, const CpPeerWriter& peer);
    // _set_power_limit_nvml(mw): the write with ctypes' u32 packing, then read_power_limit_mw. The
    // caller has checked that the setter export exists (Python's attribute access raises otherwise).
    NvmlLimitSet set_nvml(std::int64_t mw, StaticTriState& st);

    // The last set() message in full (dc_pl_message_get). Session memory only.
    std::string message;

private:
    PolicyWriteResult reapply(std::optional<std::int64_t> board_mw);

    Gpu& gpu_;
    Telemetry& telemetry_;
    ClientPowerController& cp_;
    CurrentLimits& cl_;
    std::function<StaticCapabilities&()> statics_;
};

}  // namespace druta::backend
