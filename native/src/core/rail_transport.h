// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Pure recognition and editing of understood voltage-rail packets.
// Port of src/druta/rail_transport.py; the Python tests run against both.
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "core/bytes.h"

namespace druta::rail {

struct Layout {
    std::uint32_t packet_size;
    std::uint32_t params_size;
    std::uint32_t get_command;
    std::uint32_t set_command;
    std::uint32_t mask_word;
    std::uint32_t boost_word;
    std::uint32_t record0;
    std::uint32_t record_stride;
    std::uint32_t record_type;
    std::optional<std::uint32_t> valid_word;
};

// Same order and values as rail_transport.KNOWN_LAYOUTS.
const std::array<Layout, 3>& known_layouts();

inline constexpr std::uint32_t kSupportedRailMask = 0b11;
inline constexpr std::size_t kRecordValueCount = 4;
inline constexpr std::size_t kCommandWord = 14;
inline constexpr std::size_t kParamsSizeWord = 15;
inline constexpr std::size_t kStatusWord = 16;

struct Record {
    std::uint32_t rail;
    std::array<std::int32_t, kRecordValueCount> values;
};

enum class Error {
    none,
    mask,          // requested_mask must select rail 0, rail 1, or both
    records,       // requested_mask must exactly match the record rails
    boost,         // boost must be an integer from 0 through 100
    unrecognized,  // payload is not an exact known rail GET packet
};

bool valid_requested_mask(std::uint32_t requested_mask);

// Index into known_layouts() of the exact understood GET layout, or nullopt.
// Recognition depends only on packet contents, never on driver or GPU identity.
std::optional<std::size_t> recognize_packet(ByteView payload, std::uint32_t requested_mask);

// Create a SET packet by changing only the understood owned words.
Error prepare_write(ByteView payload, std::uint32_t requested_mask, const Record* records, std::size_t record_count,
                    std::uint32_t boost, std::vector<std::uint8_t>& out);

// A compact, non-guessing description for diagnostics.
std::string packet_summary(ByteView payload);

}  // namespace druta::rail
