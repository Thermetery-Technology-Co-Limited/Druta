// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Port of nvbackend.GPU.CURRENT_LIMIT_GENERATION_POLICIES, _CURRENT_LIMIT_LAYOUTS,
// _CURRENT_LIMIT_LAYOUT_FALLBACKS, _current_limit_generation_policies and the table part of
// _current_limit_transport_layouts. The tables are verbatim copies in the Python dict order, and
// the evidence comments are carried over with them: each entry was earned on the board and driver
// its comment names, and its scope does not widen by being ported.
#include "backend/power_layouts.h"

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

// The 0x2080A618/A619/A61A/E61B GETs mapped to their own 26xx commands (the "commands" entry).
constexpr bool kCommands = true;
constexpr bool kNoCommands = false;
constexpr std::optional<std::uint32_t> kAbsent = std::nullopt;

struct KeyedLayout {
    PowerLayoutKey key;
    PowerLayout layout;
};

// Keyed by the exact unmodified power GET: its command AND its packet and
// parameter sizes. The command belongs in the key because two families can
// present the SAME packet geometry with different record layouts - 3548/3480
// is one layout under 20802612 and a different one under 2080A612. Never
// infer private ABI geometry from a driver string or a device identity.
//
// Field order: sizes {A618, A619, A61A, E61B}, has_commands, commands {same order},
// info (base, stride), status (base, stride), control (base, stride),
// status_mask_word, control_mask_word.
const KeyedLayout kLayouts[] = {
    {{0x20802612, 2524, 2456},
     {{5008, 14148, 2108, 2108},
      kCommands,
      {0x20802618, 0x20802619, 0x2080261A, 0x2080261B},
      52, 116,
      12, 372,
      4, 52,
      0u,
      0u}},
    {{0x20802612, 3548, 3480},
     {{5520, 69148, 2124, 2124},
      kCommands,
      {0x20802618, 0x20802619, 0x2080261A, 0x2080261B},
      0x3C, 0x84,
      0x64, 0x828,
      0x14, 0x34,
      0u,
      kAbsent}},
    // The same packet geometry as the entry above, reached through the A612
    // command family instead, and NOT the same records: a 128-byte info
    // record in a 5392-byte block and a 2136-byte status record in a
    // 70684-byte one. Measured on a GP106 / 516.94, where every 26xx size
    // is refused. Each base and stride was solved against live descriptors.
    {{0x2080A612, 3548, 3480},
     {{5392, 70684, 2124, 2124},
      kNoCommands,
      {0, 0, 0, 0},
      0x3C, 0x80,
      0x64, 0x858,
      0x14, 0x34,
      0u,
      kAbsent}},
    {{0x2080A612, 11748, 11680},
     {{8620, 172080, 4432, 4432},
      kNoCommands,
      {0, 0, 0, 0},
      0x58, 0xE4,
      0x70, 0x1454,
      0x14, 0x7C,
      kAbsent,
      kAbsent}},
    // Shares the 8620-byte info and 4432-byte control blocks above, but NOT
    // their status block: this driver's is 1032 bytes shorter, its header
    // 8 bytes shorter and its per-policy record 32 bytes shorter. Measured
    // on a GP106 / 576.80, where 172080 is refused (RM 0x1F) and 171048 is
    // the only size accepted across a 140000..210000 sweep. The record
    // geometry was then solved from live data rather than carried over:
    // base 0x68 / stride 0x1434 is the only pair in that packet for which
    // all ten occupied policies' status type bytes match their info types
    // and every effective value falls inside its own info bounds.
    // Two blocks agreeing is not evidence that the third does.
    {{0x2080A612, 11740, 11672},
     {{8620, 171048, 4432, 4432},
      kNoCommands,
      {0, 0, 0, 0},
      0x58, 0xE4,
      0x68, 0x1434,
      0x14, 0x7C,
      kAbsent,
      kAbsent}},
    // The same status and control blocks as the entry above, with a
    // 216-byte policy INFO record instead of 228 in an 8236-byte block.
    // Measured on the same GP106 under a different driver, where the
    // 8620- and 8632-byte info GETs are both refused (RM 0x1F). The info
    // base and the status/control geometry were each re-solved against
    // live descriptors rather than assumed from the neighbouring entry.
    {{0x2080A612, 3676, 3608},
     {{8236, 171048, 4432, 4432},
      kNoCommands,
      {0, 0, 0, 0},
      0x58, 0xD8,
      0x68, 0x1434,
      0x14, 0x7C,
      kAbsent,
      kAbsent}},
    {{0x2080A612, 54420, 54352},
     {{20000, 396024, 13840, 13840},
      kNoCommands,
      {0, 0, 0, 0},
      0xCC, 0xFC,
      0x9C, 0x1720,
      0x14, 0xC4,
      kAbsent,
      kAbsent}},
};

// Same power-GET size as the 610.88 layout, but a different info GET.
// nvapi64 pairs 8632/190272/6356 with commands A618/A619/A61A, and the
// 20000-byte info GET is rejected (RM 0x1F) on the measured GA104.
// Record bases were checked against every occupied policy's info type.
const PowerLayout kFallbacks54420[] = {
    {{8632, 0x2E740, 0x18D4, 0x18D4},
     kNoCommands,
     {0, 0, 0, 0},
     0x58, 0xE4,
     0x80, 0x168C,
     0x14, 0xB8,
     kAbsent,
     kAbsent},
    // The implementation DLL's A618/A619/A61A dispatch supplies these
    // sizes. Live GETs on AD103 echoed the masks and agreed on every
    // occupied policy's type; the two preceding info sizes returned
    // RM 0x1F. The accepted info GET, never a driver string, selects it.
    {{20512, 397048, 13876, 13876},
     kNoCommands,
     {0, 0, 0, 0},
     0xCC, 0x104,
     0x9C, 0x1730,
     0x14, 0xC4,
     kAbsent,
     kAbsent},
};

struct KeyedFallbacks {
    PowerLayoutKey key;
    const PowerLayout* layouts;
    std::size_t count;
};

// _CURRENT_LIMIT_LAYOUT_FALLBACKS, in its dict order; each tuple in its own order.
const KeyedFallbacks kFallbacks[] = {
    {{0x2080A612, 54420, 54352}, kFallbacks54420, sizeof(kFallbacks54420) / sizeof(kFallbacks54420[0])},
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
    for (const KeyedLayout& entry : kLayouts)
        if (same_key(entry.key, key)) return &entry.layout;
    return nullptr;
}

std::vector<const PowerLayout*> power_layouts(PowerLayoutKey key) {
    std::vector<const PowerLayout*> layouts;
    const PowerLayout* layout = find_power_layout(key);
    if (!layout) return layouts;
    layouts.push_back(layout);
    for (const KeyedFallbacks& entry : kFallbacks)
        if (same_key(entry.key, key))
            for (std::size_t i = 0; i < entry.count; ++i) layouts.push_back(&entry.layouts[i]);
    return layouts;
}


std::vector<const PowerLayout*> all_power_layouts() {
    std::vector<const PowerLayout*> layouts;
    for (const KeyedLayout& entry : kLayouts) layouts.push_back(&entry.layout);
    for (const KeyedFallbacks& entry : kFallbacks)
        for (std::size_t i = 0; i < entry.count; ++i) layouts.push_back(&entry.layouts[i]);
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
