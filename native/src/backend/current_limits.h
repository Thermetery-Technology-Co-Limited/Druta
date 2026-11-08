// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The current-policy operations on the borrowed RM transport: the layout
// choice by read-only probe, the four measured policy GET/SET operations with
// their request validation, and the whole-table read. Port of
// nvbackend.GPU._current_limit_abi, _current_limit_probe_info,
// _current_limit_rm, _power_policy_table and _current_limit_capacity.
//
// Every refusal is Python's, byte for byte: a ValueError raised before the
// driver is touched is an `invalid` error, one raised after a failed read a
// `transient` one (with `fixed` where Python sets exc.fixed), and a
// CurrentLimitUnsupported an `unsupported` one.
//
// Increment 6b adds the policy rows and the setter: _current_limit_state,
// get_current_limits, set_current_limit_ma and the attributes they keep. The
// one dependency not ported is read_live_power_request (client_power_runtime):
// the confirmation of a derived clamp on Turing/Ampere is a callback the
// caller supplies, and without one a derived clamp stays an invalid record.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "backend/clkdom.h"
#include "backend/gpu.h"
#include "backend/power_layouts.h"
#include "backend/rm_error.h"
#include "backend/rm_transport.h"
#include "backend/telemetry.h"

namespace druta::backend {

inline constexpr std::uint32_t kRmCurrentPolicySizeRejected = 0x1F;  // RM_CURRENT_POLICY_SIZE_REJECTED

struct Words {
    std::optional<std::vector<std::uint32_t>> words;  // the parameter words after the 17-word header
    RmError error;
};

struct AbiResult {
    const PowerLayout* layout = nullptr;  // the chosen layout; nullptr with error
    std::uint32_t index = 0;              // its position among power_layouts(key) (the stored choice)
    RmError error;
};

struct PolicyTable {  // _last_policy_table: (layout, info, control, status)
    const PowerLayout* layout = nullptr;
    std::vector<std::uint32_t> info;
    std::optional<std::vector<std::uint32_t>> control;  // nullopt when the mask is empty
    std::optional<std::vector<std::uint32_t>> dynamic;
};

struct PolicyTableResult {
    std::optional<PolicyTable> table;
    RmError error;
};

// One row of _current_limit_state / get_current_limits.
struct CurrentRow {
    std::uint32_t policy = 0;
    std::string label;
    std::uint32_t channel = 0, minimum_ma = 0, maximum_ma = 0, normal_maximum_ma = 0, default_ma = 0;
    std::uint32_t limit_ma = 0, requested_ma = 0, value_ma = 0;
};

// One entry of _current_limit_unavailable. Python's dict carries "unsupported" only when present.
struct UnavailablePolicy {
    std::uint32_t policy = 0;
    std::string label;
    bool present = false;
    bool unsupported = false;
    std::string error;
};

// The read_live_power_request branch of _current_limit_state: whether a fresh paired FE
// observation confirms the derived clamp (requested, effective) of `policy` against the table
// just read. The caller owns that read; nullptr never confirms.
using ConfirmDerivedClamp = bool (*)(void* context, std::uint32_t policy, std::uint32_t requested_ma,
                                     std::uint32_t effective_ma, const std::vector<std::uint32_t>& info,
                                     const std::vector<std::uint32_t>& control);

struct StateResult {  // _current_limit_state: (rows, control), or the exception it raised
    std::vector<CurrentRow> rows;
    std::optional<std::vector<std::uint32_t>> control;  // None when the mask is empty
    RmError error;                                      // kind != none: Python raised
};

struct SetResult {  // set_current_limit_ma: (ok, message)
    bool ok = false;
    std::string message;
};

class CurrentLimits {
public:
    CurrentLimits(Gpu& gpu, Telemetry& telemetry, RmTransport& rm, ClkDom& clkdom)
        : gpu_(gpu), telemetry_(telemetry), rm_(rm), clkdom_(clkdom) {}

    // _current_limit_state: the generation's policy rows from one validated table read. Resets
    // `unavailable` first, as Python does, even when the read then fails.
    StateResult state(ConfirmDerivedClamp confirm, void* context);
    // get_current_limits: the rows, with the error bookkeeping the UI and the profiles read.
    std::vector<CurrentRow> get_current_limits(ConfirmDerivedClamp confirm, void* context);
    // set_current_limit_ma: change one policy, verify stored and effective limits, else restore.
    // `policy` and `ma` are Python ints the binding has already type-checked.
    SetResult set_current_limit_ma(std::int64_t policy, std::int64_t ma, bool pin, ConfirmDerivedClamp confirm,
                                   void* context);

    // _current_limit_capacity: indices bounded by the three blocks and the 32-bit mask.
    static std::uint32_t capacity(const PowerLayout& layout);

    // _current_limit_abi: the stored choice, the only candidate, or the first candidate whose
    // read-only info probe the driver accepts.
    AbiResult abi();
    // _current_limit_probe_info: the info GET for one candidate layout; the words on success.
    Words probe_info(const PowerLayout& layout);
    // _current_limit_rm: one of the four policy operations. params nullptr is Python's None.
    Words rm(std::uint32_t command, const std::vector<std::uint32_t>* params, std::optional<std::uint32_t> policy_mask,
             bool any_live_policy);
    // _power_policy_table: one validated read of the whole table.
    PolicyTableResult policy_table();

    std::uint32_t power_policy_mask = 0;           // _power_policy_mask
    std::optional<PolicyTable> last_policy_table;  // _last_policy_table
    std::vector<UnavailablePolicy> unavailable;    // _current_limit_unavailable
    std::vector<CurrentRow> last_rows;             // _current_limit_last_rows
    std::string error, read_error;                 // _current_limit_error, _current_limit_read_error
    std::uint32_t established = 0;                 // _current_limit_established, as a policy mask
    std::map<std::uint32_t, std::int64_t> pins;    // _power_policy_pins

private:
    SetResult set_unpinned(std::int64_t policy, std::int64_t ma, ConfirmDerivedClamp confirm, void* context);

    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport& rm_;
    ClkDom& clkdom_;
};

}  // namespace druta::backend
