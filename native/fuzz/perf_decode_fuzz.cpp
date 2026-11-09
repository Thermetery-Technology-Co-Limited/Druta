// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// libFuzzer target for the PERF group decoder (perf_decode_group, increment P2). It must never crash
// or read out of bounds, whatever the sizes, masks and row bytes.
//
// Input: byte 0 selects the group (bits 0-1: limits, cf_controller, cf_policy, or 3 for an unknown
// name), whether CONTROL is present (bit 2), and the geometry (bits 3-4: 0 and 1 the exact layout
// sizes, 2 a size one byte off taken from byte 1's sign, 3 a size taken from bytes 1-2 directly).
// Bit 5 copies INFO's group byte and mask into CONTROL, so the row decode is reached often. The rest
// is the INFO bytes followed by the CONTROL bytes; each buffer is exactly its size (heap-allocated,
// so ASan sees any overrun), zero-filled where the input runs out.
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "backend/perf_diagnostics.h"

namespace {

std::vector<std::uint8_t> take(const std::uint8_t*& data, std::size_t& size, std::size_t want) {
    std::vector<std::uint8_t> out(want, 0);
    const std::size_t n = want < size ? want : size;
    if (n) std::memcpy(out.data(), data, n);
    data += n;
    size -= n;
    return out;
}

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
    if (size < 3) return 0;
    const std::uint8_t selector = data[0];
    const std::uint8_t a = data[1], b = data[2];
    data += 3;
    size -= 3;
    const auto& table = druta::backend::perf_layouts();
    const unsigned group = selector & 3u;
    const std::string name = group < 3 ? table[group].name : std::string("limits\0x", 8);
    const druta::backend::PerfGroupLayout& layout = table[group < 3 ? group : 0];
    const bool with_control = (selector & 4u) != 0;
    const unsigned geometry = (selector >> 3) & 3u;
    std::size_t info_size = layout.info_size, control_size = layout.control_size;
    if (geometry == 2) {
        if (a & 1) info_size = (a & 0x80) ? info_size - 1 : info_size + 1;
        if (a & 2) control_size = (a & 0x40) ? control_size - 1 : control_size + 1;
    } else if (geometry == 3) {
        info_size = static_cast<std::size_t>(a) | (static_cast<std::size_t>(b) << 8);
        control_size = static_cast<std::size_t>(b) * 3u;
    }
    std::vector<std::uint8_t> info = take(data, size, info_size);
    std::vector<std::uint8_t> control = take(data, size, control_size);
    if ((selector & 0x20u) && info.size() >= 4u + layout.mask_bytes && control.size() >= 4u + layout.mask_bytes) {
        control[0] = info[0];
        std::memcpy(control.data() + 4, info.data() + 4, layout.mask_bytes);
    }
    // Empty buffers are passed as a pointer to a live byte, as the C ABI does.
    static const std::uint8_t empty = 0;
    druta::backend::PerfDecodeResult r = druta::backend::perf_decode_group(
        name, info.empty() ? &empty : info.data(), info.size(),
        with_control ? (control.empty() ? &empty : control.data()) : nullptr, control.size());
    if (r.snapshot) {
        // Touch everything the ABI would copy.
        std::size_t sink = r.snapshot->info_sha256.size();
        for (const auto& e : r.snapshot->entries) sink += e.info_raw.size() + e.control_raw.size() + e.issues.size();
        for (const auto& f : r.snapshot->flags) sink += f.raw;
        for (const auto& t : r.snapshot->targets) sink += t.present ? 1u : 0u;
        if (sink == 0xFFFFFFFFu) __builtin_trap();
    }
    return 0;
}
