// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The power-arbitration decoder (port of power_arbitration.decode and its helpers). Increment B1.
//
// Read-only interpretation of an understood RM power-policy status prefix. The caller supplies
// the word arrays of one validated policy-table read; nothing here touches a GPU. Unsupported
// packet geometry affects these extra diagnostics only; it must not remove an otherwise working
// power control.
//
// Evidence: experiments/registry-overrides/vbios-fields/policy-layers-contract.json.
// Host CAB180 / GSP 1623D04 produce the common status prefix. Host 907630 / GSP 16590B4 define
// the 0x50-byte arbitration structure. Addresses document one traced binary, never locate data or
// select compatibility at runtime.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "backend/power_layouts.h"

namespace druta::backend {

// BIOS power-policy table versions recorded in the first INFO row byte. The RM row layout does
// not change with them. V4 was traced; on a V3 board every arbitration vector was self-consistent
// and integral rows' effective limits followed request x upper word
// (ampere-ga102-kingpin-v30-policy-rows-61742-20261105.md).
// Scope: the V3 acceptance rests on one board's evidence. These rows are diagnostics only and are
// never a control gate.
inline constexpr std::uint8_t kArbitrationRowFormats[] = {0x30, 0x40};
bool arbitration_row_format(std::uint32_t version);

inline constexpr std::size_t kArbitrationPoliciesMax = 32;
inline constexpr std::size_t kArbitrationInputsMax = 8;

// One packet as the caller holds it. present = false is Python's None; valid = false says the
// caller's sequence held an element that is not an unsigned 32-bit int (a check only the dynamic
// caller can make; the words are then not read).
struct ArbitrationWords {
    bool present = false;
    bool valid = true;
    const std::uint32_t* data = nullptr;
    std::size_t count = 0;
};

struct ArbitrationInput {
    std::uint32_t client;
    std::uint32_t value;
};

// _arbitration's dict. When !available only reason is meaningful.
struct ArbitrationVector {
    bool available = false;
    std::string reason;
    std::string method;
    std::uint32_t method_code = 0;
    std::uint32_t count = 0;
    std::uint32_t output = 0;
    std::uint32_t initial_value = 0;
    std::vector<ArbitrationInput> inputs;
    std::optional<std::uint32_t> computed_output;
    std::optional<bool> consistent;
};

// _row's dict. When !available, host and pmu carry the row's reason and the payload fields are unset.
struct ArbitrationRow {
    std::uint32_t policy = 0;
    std::uint32_t type = 0;
    std::uint32_t channel = 0;
    std::optional<std::string> unit;  // "mW", "mA" or None
    std::uint32_t unit_code = 0;
    bool available = false;
    std::string reason;  // empty when available (Python's None)
    std::uint32_t requested = 0;
    std::uint32_t effective = 0;
    std::uint32_t value = 0;
    std::int32_t delta = 0;
    ArbitrationVector host;
    ArbitrationVector pmu;
    std::vector<std::string> observations;
};

// decode's dict: {"schema": 1, "available", "reason", "policies", "mask"?}.
struct ArbitrationReport {
    std::uint32_t schema = 1;
    bool available = false;
    std::string reason;  // empty when available (Python's None)
    std::optional<std::uint32_t> mask;
    std::vector<ArbitrationRow> policies;
};

// _geometry: empty when the layout is the understood arbitration geometry, else the refusal.
// layout == nullptr is a layout (or its "sizes") that is not a mapping.
std::string arbitration_geometry(const PowerLayout* layout);

// _arbitration(row, offset) over a row's bytes.
ArbitrationVector decode_arbitration_vector(const std::uint8_t* row, std::size_t row_bytes, std::size_t offset);

// decode(layout, info, control, status).
ArbitrationReport decode_power_arbitration(const PowerLayout* layout, const ArbitrationWords& info,
                                           const ArbitrationWords& control, const ArbitrationWords& status);

}  // namespace druta::backend
