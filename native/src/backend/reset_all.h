// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Reset all (increment G3): a port of nvbackend.GPU.reset_all as one call that composes the knobs
// that are already native, in Python's exact step order, with Python's conditions for emitting
// each step and Python's texts. Nothing here re-implements a knob: every step is the component's
// own operation (Clocks, ClkDom, Rails, CurrentLimits, PowerLimit, LegacyP0, VfLock, FanControl,
// set_voltage_boost, VfCurve).
//
// Session-only: the call drops this session's policy pins first, then restores the DRIVER's values
// and this session's first-read rail limits. It persists nothing and re-applies nothing, and no
// step is decided by a driver version, a device, subsystem or VBIOS identity: the NVAPI checks are
// interface-presence capability checks (Nvapi::interface), as the components' own.
//
// The caller holds the handle lock (it is recursive; the components take it again).
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "backend/client_power_controller.h"
#include "backend/clkdom.h"
#include "backend/clocks.h"
#include "backend/current_limits.h"
#include "backend/fan.h"
#include "backend/gpu.h"
#include "backend/legacy_p0.h"
#include "backend/outcome.h"
#include "backend/power_limit.h"
#include "backend/rails.h"
#include "backend/rm_transport.h"
#include "backend/telemetry.h"
#include "backend/vfcurve.h"
#include "backend/vflock.h"

namespace druta::backend {

// GPU.LOCK_STEP, GPU.VF_LOCK_STEP, GPU.P0_LOCK_STEP: a contract with the front ends, which drop a
// lock banner only when the step of that name succeeded.
inline constexpr char kResetLockStep[] = "clock lock";
inline constexpr char kResetVfLockStep[] = "v/f point lock";
inline constexpr char kResetP0LockStep[] = "legacy P0 hold";

// The most steps one call can produce, each group bounded by what can feed it:
//   core offset, mem offset                                   2
//   clock-domain offsets: one failure, or one per domain      32  (control indices are < 32)
//   core rail offset                                          1
//   MSVDD requests: one per domain                            32  (written / seen / candidate < 32)
//   rail limits (the failure and the reset are exclusive)     1
//   current limits failure + one per current row              33  (rows are policies < 32)
//   power limit                                               1
//   power policies: one failure, or one per policy row        32  (the policy mask is 32 bits)
//   clock lock, legacy P0 hold, v/f point lock, fan,
//   voltage boost, vf curve                                   6
inline constexpr std::size_t kResetStepsMax = 2 + 32 + 1 + 32 + 1 + 33 + 1 + 32 + 6;
static_assert(kResetStepsMax == 140, "the documented bound (drutacore/reset.h)");

struct ResetStep {
    std::string name;
    bool ok = false;
    std::string message;
};

// Where a Python callee would have raised out of reset_all (the steps so far are then lost, as in
// Python, while everything already written stays written):
//   caller       - a caller-supplied callback reported the exception it keeps (the static pl_*
//                  keys read or back-filled for set_power_limit_mw, the inputs of
//                  read_power_policies);
//   index_error  - the IndexError Python lets escape set_power_limit_mw (_reapply_power_policy_pins),
//                  read_power_policies (_power_policy_rows) or _write_policy_limit.
enum class ResetStop { none, caller, index_error };

struct ResetAllResult {
    ResetStop stop = ResetStop::none;
    std::vector<ResetStep> steps;
};

// The handle's components. The lazily built ones are reached through the handle's accessors.
struct ResetParts {
    Gpu& gpu;
    Telemetry& telemetry;
    ClkDom& clkdom;
    Rails& rails;
    CurrentLimits& cl;
    RmTransport& rm;
    ClientPowerController& cp;
    PowerLimit& pl;
    LegacyP0& legacy_p0;
    VfLock& vflock;
    std::function<Clocks&()> clocks;
    std::function<FanControl()> fan;
    std::function<VfCurve&()> curve;
};

// What only the caller can supply.
struct ResetHooks {
    // get_current_limits / set_current_limit_ma / read_power_policies' derived-clamp confirmation.
    ConfirmDerivedClamp confirm = nullptr;
    void* confirm_context = nullptr;
    // read_power_policies' inputs (read_power_limit_mw, the static keys, _board_policy).
    ReadPolicyInputs inputs = nullptr;
    void* inputs_context = nullptr;
    // The caller's static pl_* keys as they stand when Python reads self.static["pl_def_mw"]
    // (false: reading them raised). Then, after set_power_limit_mw, the keys its live read filled:
    // the caller applies the back-fill where Python's dict changed (false: that raised). The
    // back-fill runs after PowerLimit::set has written, where Python's runs before any write
    // (DESIGN-TRANSPORT.md: reset all).
    std::function<bool(StaticTriState&)> read_statics;
    std::function<bool(const StaticTriState& before, const StaticTriState& after)> backfill;
    // GPU._write_policy_limit for the live SET's compensation (set_power_limit_mw).
    CpPeerWriter peer;
    // bool(voltage_xoc_enabled) for set_power_limit_mw.
    bool xoc = false;
};

// nvbackend.GPU.reset_all. `rail_trace` notes the rail-limit reset's _write_rail_records calls as
// they run (the caller hands the writer's attributes back even when a later step stops the call).
ResetAllResult reset_all(const ResetParts& parts, const ResetHooks& hooks, RailWriterTrace& rail_trace);

}  // namespace druta::backend
