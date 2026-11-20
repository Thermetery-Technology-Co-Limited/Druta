// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Bounds-checked little-endian word access over driver packets. Every read
// and write of a packet in the core goes through these; nothing indexes a
// packet buffer directly.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <optional>
#include <vector>

namespace druta {

inline constexpr std::size_t kDwordSize = 4;

struct ByteView {
    const std::uint8_t* data = nullptr;
    std::size_t size = 0;

    ByteView() = default;
    ByteView(const std::uint8_t* d, std::size_t n) : data(d), size(n) {}
    explicit ByteView(const std::vector<std::uint8_t>& bytes) : data(bytes.data()), size(bytes.size()) {}

    bool word_in_bounds(std::size_t word) const { return (word + 1) * kDwordSize <= size; }

    std::optional<std::uint32_t> u32(std::size_t word) const {
        if (!word_in_bounds(word)) return std::nullopt;
        std::uint32_t value;
        std::memcpy(&value, data + word * kDwordSize, kDwordSize);
        return value;  // little-endian hosts only (x86/x64), as the driver packets are
    }

    std::optional<std::int32_t> i32(std::size_t word) const {
        auto value = u32(word);
        if (!value) return std::nullopt;
        return static_cast<std::int32_t>(*value);
    }
};

// Write a word into an owned packet copy; returns false when out of bounds.
inline bool put_u32(std::vector<std::uint8_t>& packet, std::size_t word, std::uint32_t value) {
    if ((word + 1) * kDwordSize > packet.size()) return false;
    std::memcpy(packet.data() + word * kDwordSize, &value, kDwordSize);
    return true;
}

inline bool put_i32(std::vector<std::uint8_t>& packet, std::size_t word, std::int32_t value) {
    return put_u32(packet, word, static_cast<std::uint32_t>(value));
}

}  // namespace druta
