// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// libFuzzer entry for the rail packet parser: arbitrary bytes must never
// crash recognition, summary or SET preparation, and a prepared SET must
// differ from its GET only in the owned words.
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>

#include "core/rail_transport.h"

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
    using namespace druta::rail;
    if (size < 2) return 0;
    const std::uint32_t mask = data[0] & 3;
    const std::uint32_t boost = data[1];
    druta::ByteView payload(data + 2, size - 2);
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
    return 0;
}
