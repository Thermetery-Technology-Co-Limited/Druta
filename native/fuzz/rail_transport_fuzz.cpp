// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// libFuzzer entry for the rail packet parser: arbitrary bytes must never
// crash recognition, summary or SET preparation, and a prepared SET must
// differ from its GET only in the owned words.
//
// Input: byte 0 bits 0-1 are the requested rail mask and bit 2 the mode;
// byte 1 is the boost. Raw mode: the rest is the payload. Structured mode:
// byte 2 & 3 picks a known layout (out of range is skipped), the canonical
// GET packet for it is built from the layout table, and the remaining bytes
// are 6-byte patches (u16 word index modulo the packet's word count, u32
// value, little-endian) applied before the same checks. Structured inputs
// reach the recognized GET path without a seed corpus.
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>

#include "core/rail_transport.h"

namespace {

using namespace druta::rail;

void check(druta::ByteView payload, std::uint32_t mask, std::uint32_t boost) {
    (void)packet_summary(payload);
    auto index = recognize_packet(payload, mask);
    Record records[2] = {{0, {1, -2, 3, -4}}, {1, {-5, 6, -7, 8}}};
    std::size_t count = 0;
    Record selected[2];
    for (std::uint32_t rail = 0; rail < 2; ++rail)
        if (mask & (1u << rail)) selected[count++] = records[rail];
    std::vector<std::uint8_t> out;
    Error error = prepare_write(payload, mask, selected, count, boost, out);
    if (index && boost <= 100 && error != Error::none) __builtin_trap();     // recognized GET must prepare
    if (!index && error == Error::none) __builtin_trap();                     // unrecognized must not
    if (error == Error::none) {
        const Layout& layout = known_layouts()[*index];
        if (out.size() != payload.size) __builtin_trap();
        for (std::size_t word = 0; word < out.size() / 4; ++word) {
            bool owned = word == kCommandWord || word == layout.mask_word || word == layout.boost_word;
            for (std::uint32_t rail = 0; rail < 2 && !owned; ++rail) {
                if (!(mask & (1u << rail))) continue;
                std::size_t base = layout.record0 + rail * layout.record_stride;
                owned = word >= base && word <= base + kRecordValueCount;
                if (layout.valid_word) owned = owned || word == base + *layout.valid_word;
            }
            if (!owned && std::memcmp(out.data() + word * 4, payload.data + word * 4, 4) != 0) __builtin_trap();
        }
    }
}

std::uint32_t le16(const std::uint8_t* p) { return static_cast<std::uint32_t>(p[0] | (p[1] << 8)); }

std::uint32_t le32(const std::uint8_t* p) {
    return static_cast<std::uint32_t>(p[0]) | (static_cast<std::uint32_t>(p[1]) << 8) |
           (static_cast<std::uint32_t>(p[2]) << 16) | (static_cast<std::uint32_t>(p[3]) << 24);
}

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
    if (size < 2) return 0;
    const std::uint32_t mask = data[0] & 3;
    const bool structured = (data[0] & 4) != 0;
    const std::uint32_t boost = data[1];
    if (!structured) {
        check(druta::ByteView(data + 2, size - 2), mask, boost);
        return 0;
    }
    if (size < 3) return 0;
    const auto& layouts = known_layouts();
    const std::size_t layout_index = data[2] & 3u;
    if (layout_index >= layouts.size()) return 0;
    const Layout& layout = layouts[layout_index];
    std::vector<std::uint8_t> packet(layout.packet_size, 0);
    druta::put_u32(packet, kCommandWord, layout.get_command);
    druta::put_u32(packet, kParamsSizeWord, layout.params_size);
    druta::put_u32(packet, layout.mask_word, mask);
    const std::size_t words = packet.size() / druta::kDwordSize;
    if (words == 0) return 0;
    for (std::size_t at = 3; at + 6 <= size; at += 6)
        druta::put_u32(packet, le16(data + at) % words, le32(data + at + 2));
    check(druta::ByteView(packet), mask, boost);
    return 0;
}
