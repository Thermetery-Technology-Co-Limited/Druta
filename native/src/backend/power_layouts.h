// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The pure current-limit tables of nvbackend.GPU: CURRENT_LIMIT_GENERATION_POLICIES,
// _CURRENT_LIMIT_LAYOUTS and _CURRENT_LIMIT_LAYOUT_FALLBACKS, copied verbatim with the
// evidence comments beside the originals. Generation selects policy semantics; the
// record layout is keyed by the exact unmodified power GET (its command AND its packet
// and parameter sizes), never by a driver string or a device identity.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

namespace druta::backend {

struct GenerationPolicy {
    std::uint32_t policy;  // the policy index (13, 14)
    const char* label;
    std::uint32_t type;
    std::uint32_t channel;
    std::uint32_t normal_maximum_ma;
};

struct GenerationPolicies {
    const GenerationPolicy* policies = nullptr;
    std::size_t count = 0;
    bool empty() const { return count == 0; }
};

// CURRENT_LIMIT_GENERATION_POLICIES.get(arch, {}): empty for an unknown or absent architecture.
GenerationPolicies generation_policies(std::optional<std::uint32_t> architecture);

// A transport's identity: _current_limit_key(header) = (header[14], header[2], header[15]).
struct PowerLayoutKey {
    std::uint32_t command, packet_bytes, parameter_bytes;
};

// The four GETs a layout sizes, in the order of its "sizes" and "commands" entries.
inline constexpr std::uint32_t kPowerGetCommands[4] = {0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B};

struct PowerLayout {
    std::uint32_t sizes[4];       // packet bytes for kPowerGetCommands[i]
    bool has_commands;            // the 2612 family maps each GET to its own 26xx command
    std::uint32_t commands[4];    // when has_commands; zeros otherwise
    std::uint32_t info_base, info_stride;
    std::uint32_t status_base, status_stride;
    std::uint32_t control_base, control_stride;
    std::optional<std::uint32_t> status_mask_word;
    std::optional<std::uint32_t> control_mask_word;
};

// _CURRENT_LIMIT_LAYOUTS.get(key): nullptr for an unknown key.
const PowerLayout* find_power_layout(PowerLayoutKey key);

// (layout, *_CURRENT_LIMIT_LAYOUT_FALLBACKS.get(key, ())), as _current_limit_transport_layouts
// returns them; empty for an unknown key.
std::vector<const PowerLayout*> power_layouts(PowerLayoutKey key);

// Every layout the tables hold: _CURRENT_LIMIT_LAYOUTS.values() then every fallback group, in
// table order (the selector scan of _current_limit_rm).
std::vector<const PowerLayout*> all_power_layouts();

// The largest packet any layout sizes, in bytes: a caller's buffer bound.
std::uint32_t max_power_layout_bytes();

}  // namespace druta::backend
