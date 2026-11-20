// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "../pascal_clock_table.h"
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

inline PascalTimingSample pascal_full_timing_sample() {
    PascalTimingSample sample{3504,0x2966c296,0x220038aa};
    sample.set_word(0,0x1b58c147);sample.set_word(2,0x881c0900);
    sample.set_word(4,0xd5838032);sample.set_word(5,0xa6b270d2);
    return sample;
}

// Two different bands, multiple strap columns, and header-declared padding.
inline std::vector<uint8_t> pascal_timing_fixture() {
    auto b=pascal_clock_fixture();
    auto u32=[&](size_t at,uint32_t v){for(unsigned j=0;j<4;++j)b[at+j]=uint8_t(v>>(j*8));};
    auto put=[&](size_t at,std::initializer_list<uint8_t> bytes){std::copy(bytes.begin(),bytes.end(),b.begin()+at);};
    b[148]=12; b[150]=0xe0; // Relocate the BIT P payload to fit three pointers.
    u32(0xe0,0x300);u32(0xe4,0x600);u32(0xe8,0x700);
    put(0x600,{0x11,8,4,2,3,2});
    put(0x608,{0,0,0xe8,3,0,7,1,7,255,7});
    put(0x612,{0xe9,3,0xa0,15,1,8,2,8,2,8});
    put(0x700,{0x20,8,32,0,0,3});
    u32(0x708+32+4,0xba987654);u32(0x708+32+12,0x11221855);
    const auto sample=pascal_full_timing_sample();
    for(unsigned i=0;i<6;++i)u32(0x748+i*4,sample.word(i));
    return b;
}

inline std::vector<uint8_t> legacy_clock_fixture(uint32_t architecture) {
    constexpr size_t perf = 0x300;
    // Recorded GTX 770 and GM107 P0/P8 values and flags, with disabled
    // slots and arbitrary reserved words in a synthetic PCI image.
    const bool kepler = architecture == 2;
    const size_t header = kepler ? 30 : 33;
    const size_t row_header = kepler ? 25 : 32;
    const size_t stride = row_header + 9 * 4;
    auto rom = pascal_clock_fixture();
    std::fill(rom.begin() + 0xc8, rom.begin() + 0xd0, uint8_t(0)); // No Pascal clock-program pointers.
    rom[perf] = 0x40;
    rom[perf + 1] = static_cast<uint8_t>(header);
    rom[perf + 2] = static_cast<uint8_t>(row_header);
    rom[perf + 3] = 4;
    rom[perf + 4] = 9;
    rom[perf + 5] = 4;
    rom[147] = kepler ? 1 : 2; // BIT P token versions both locate PERF.
    const size_t p0 = perf + header + 3 * stride;
    const size_t p8 = perf + header;
    rom[p8] = 7;
    rom[p8 + stride] = rom[p8 + 2 * stride] = 0xff;
    rom[p0] = 15;
    const unsigned p0_clocks[]{1080, 1165, kepler ? 1115u : 1130u, kepler ? 3505u : 900u,
        kepler ? 1134u : 1120u, 1080, 540, 324, kepler ? 540u : 648u};
    const unsigned p8_clocks[]{810, 810, 810, kepler ? 324u : 405u, 810, 648, 405, 324, kepler ? 540u : 405u};
    for (size_t domain = 0; domain < 9; ++domain) {
        const size_t high = p0 + row_header + domain * 4;
        const size_t low = p8 + row_header + domain * 4;
        const unsigned p0_flags = domain < 5 ? 0x4000 : 0;
        const unsigned p8_flags = kepler && domain < 5 && domain != 3 ? 0x8000 : 0;
        rom[high] = static_cast<uint8_t>(p0_clocks[domain]);
        rom[high + 1] = static_cast<uint8_t>((p0_clocks[domain] | p0_flags) >> 8);
        rom[high + 2] = 0xA5;
        rom[high + 3] = 0x5A;
        rom[low] = static_cast<uint8_t>(p8_clocks[domain]);
        rom[low + 1] = static_cast<uint8_t>((p8_clocks[domain] | p8_flags) >> 8);
        rom[low + 2] = 0xC3;
        rom[low + 3] = 0x3C;
    }
    return rom;
}
}
