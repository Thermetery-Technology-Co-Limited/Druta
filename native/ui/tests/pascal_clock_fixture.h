// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <algorithm>
#include <cstdint>
#include <vector>

namespace druta::ui::test {
// Different identity, clocks, program indices and performance row ordering
// from the GP107 validation board. All nine HAL2 slots are present.
inline std::vector<uint8_t> pascal_clock_fixture() {
    std::vector<uint8_t> b(4096, 0);
    const auto u16 = [&](size_t pos, unsigned value) {
        b[pos] = static_cast<uint8_t>(value);
        b[pos + 1] = static_cast<uint8_t>(value >> 8);
    };
    const auto u32 = [&](size_t pos, uint32_t value) {
        u16(pos, value);
        u16(pos + 2, value >> 16);
    };
    const auto put = [&](size_t pos, std::initializer_list<uint8_t> data) {
        std::copy(data.begin(), data.end(), b.begin() + pos);
    };
    put(0, {0x55, 0xaa});
    u16(24, 64);
    put(64, {'P', 'C', 'I', 'R'});
    u16(68, 0x10de); u16(70, 0x1bbc); u16(74, 24); u16(80, 8);
    put(128, {0xff, 0xb8, 'B', 'I', 'T', 0, 0, 1, 12, 6, 3});
    for (int i = 128; i < 139; ++i) b[139] -= b[i];
    put(140, {'C', 2, 16, 0, 0xc0, 0});
    put(146, {'P', 2, 4, 0, 0xd0, 0});
    put(152, {'i', 2, 5, 0, 0xd8, 0});
    u32(0xd8, 0x86070102); b[0xdc] = 0x22;
    constexpr size_t c = 0x100, programs = 0x200, p = 0x300;
    u32(0xc8, c); u32(0xcc, programs); u32(0xd0, p);
    put(c, {0x10, 7, 9, 9, 2, 0, 0});
    for (int i = 0; i < 9; ++i) put(c + 7 + i * 9, {0, 255, 255});
    put(c + 7, {1, 1, 1});
    put(c + 16, {2, 2, 2});
    put(c + 34, {2, 0, 0});
    put(programs, {0x10, 8, 13, 3, 3, 2, 2, 1});
    constexpr size_t stride = 21, sys = programs + 8, gpc = sys + stride, xbar = gpc + stride;
    b[sys] = 0x22; u16(sys + 1, 3500);
    b[gpc] = 0xa0; u16(gpc + 1, 3900);
    b[xbar] = 0xa2; u16(xbar + 1, 3700);
    put(gpc + 13, {3, 90, 0xe6, 1, 95, 0x4a});
    put(p, {0x50, 10, 3, 4, 6, 9});
    for (int i = 0; i < 4; ++i) {
        const size_t row = p + 10 + 57 * i;
        constexpr uint8_t levels[] = {10, 255, 15, 7};
        b[row] = levels[i];
        for (int d = 0; d < 9; ++d) {
            const size_t at = row + 3 + 6 * d;
            const unsigned nominal = i == 3 ? 1050 : 1250;
            const unsigned max_p0 = d == 0 ? 3800u : d == 1 ? 3600u : 3400u;
            const unsigned maximum = i == 0 ? 1800 : i == 3 ? 1200 : max_p0;
            u16(at, 0xc000 | nominal);
            u32(at + 2, 0xb0000000u | (maximum << 14) | 1000u);
        }
    }
    return b;
}
}
