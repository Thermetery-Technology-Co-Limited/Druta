// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Generation roles mirror GPU.CURRENT_LIMIT_GENERATION_POLICIES. Measured
// transport layouts are generated from the shared Python catalog.
#include "backend/power_layouts.h"
#include "backend/power_layout_catalog_generated.h"

namespace druta::backend {
namespace {

// NVML's own architecture enum. Kepler 2, Maxwell 3, Pascal 4, Volta 5,
// Turing 6, Ampere 7, Ada 8, Hopper 9, Blackwell 10.
constexpr std::uint32_t kArchPascal = 4;
constexpr std::uint32_t kArchTuring = 6;
constexpr std::uint32_t kArchAmpere = 7;
constexpr std::uint32_t kArchAda = 8;
constexpr std::uint32_t kArchBlackwell = 10;  // the literal key 10 in the Python table

// ---- current-limit policies ------------------------------------------ //
// The surrounding ABI varies with the driver; generation determines the
// valid current-policy descriptors, independently of the packet layout.
// Select only by NVML architecture, then validate every live descriptor,
// mask, stored value and effective value before exposing a slider.
//
// Generation selects policy semantics; board-specific channels and ranges
// come from the driver. Exact packet geometry and mA units remain required.
constexpr GenerationPolicy kPascalPolicies[] = {
    {13, "Core current", 0x0B, 11, 500'000},
};
constexpr GenerationPolicy kTuringPolicies[] = {
    {13, "Core current", 0x0B, 19, 500'000},
};
// Type 0x0F / channel 19 were read from the GA104 info block, not
// copied from Turing. A different channel is relabeled; a different
// type stays unavailable. Policy 18 on that adapter is the 5001 A
// sentinel, not a second rail.
constexpr GenerationPolicy kAmperePolicies[] = {
    {13, "Core current", 0x0F, 19, 500'000},
};
// AD103 policy 13 reports mA, type 0x0F, channel 19. A 1 A decrease
// and restoration verified stored and effective limits. Ranges and
// channel identity still come from each adapter's live descriptors.
constexpr GenerationPolicy kAdaPolicies[] = {
    {13, "Core current", 0x0F, 19, 500'000},
};
constexpr GenerationPolicy kBlackwellPolicies[] = {
    {13, "Core current", 0x12, 13, 500'000},
    {14, "Other rail current", 0x12, 12, 200'000},
};

template <std::size_t N>
constexpr GenerationPolicies policies_of(const GenerationPolicy (&table)[N]) {
    return GenerationPolicies{table, N};
}

struct ArchPolicies {
    std::uint32_t architecture;
    GenerationPolicies policies;
};

// CURRENT_LIMIT_GENERATION_POLICIES, in its dict order.
constexpr ArchPolicies kGenerationPolicies[] = {
    {kArchPascal, policies_of(kPascalPolicies)}, {kArchTuring, policies_of(kTuringPolicies)},
    {kArchAmpere, policies_of(kAmperePolicies)}, {kArchAda, policies_of(kAdaPolicies)},
    {kArchBlackwell, policies_of(kBlackwellPolicies)},
};

constexpr bool same_key(const PowerLayoutKey& a, const PowerLayoutKey& b) {
    return a.command == b.command && a.packet_bytes == b.packet_bytes && a.parameter_bytes == b.parameter_bytes;
}

}  // namespace

GenerationPolicies generation_policies(std::optional<std::uint32_t> architecture) {
    if (!architecture) return {};  // Python's dict.get(None, {})
    for (const ArchPolicies& entry : kGenerationPolicies)
        if (entry.architecture == *architecture) return entry.policies;
    return {};
}

const PowerLayout* find_power_layout(PowerLayoutKey key) {
    for (const auto& entry : catalog::kPowerLayouts)
        if (same_key(entry.key, key)) return &entry.layout;
    return nullptr;
}

std::vector<const PowerLayout*> power_layouts(PowerLayoutKey key) {
    std::vector<const PowerLayout*> layouts;
    for (const auto& entry : catalog::kPowerLayouts)
        if (same_key(entry.key, key)) layouts.push_back(&entry.layout);
    return layouts;
}

std::vector<const PowerLayout*> all_power_layouts() {
    std::vector<const PowerLayout*> layouts;
    for (const auto& entry : catalog::kPowerLayouts) layouts.push_back(&entry.layout);
    return layouts;
}

std::uint32_t max_power_layout_bytes() {
    std::uint32_t largest = 0;
    for (const PowerLayout* layout : all_power_layouts())
        for (std::uint32_t size : layout->sizes)
            if (size > largest) largest = size;
    return largest;
}

}  // namespace druta::backend
