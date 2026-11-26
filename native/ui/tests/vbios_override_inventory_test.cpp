// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "vbios_override_inventory.h"
#include "pascal_clock_fixture.h"
#include <algorithm>
#include <iostream>
#include <stdexcept>

using namespace druta::ui;
namespace {
using Bytes = std::vector<uint8_t>;
void put(Bytes& b, size_t at, std::initializer_list<uint8_t> v) { std::copy(v.begin(), v.end(), b.begin() + at); }
void word(Bytes& b, size_t at, unsigned v) {
    b.at(at) = uint8_t(v);
    b.at(at + 1) = uint8_t(v >> 8);
}
void dword(Bytes& b, size_t at, uint32_t v) {
    word(b, at, v);
    word(b, at + 2, v >> 16);
}
void check(bool valid, const char* why) {
    if (!valid)
        throw std::runtime_error(why);
}
constexpr uint16_t device = 0x1bbc;
constexpr size_t p = 0x700, cooler = 0x1000, policy = 0x1100, mapping = 0x1300, control = 0x1400, speed = 0x1500;

Bytes sample() {
    Bytes b = test::pascal_rail_clamp_fixture();
    b.resize(0x3000);
    word(b, 80, 24);
    word(b, 148, 0xec);
    dword(b, p + 0x50, 0xa00);
    put(b, 0xa00, {0x10, 11, 40, 2});
    b[0xa09] = 1;
    b[0xa33] = 1;
    for (unsigned i = 0; i < 3; ++i)
        word(b, 0xa35 + 2 * i, 85 * 32);
    dword(b, 0xc8, 0xb00);
    put(b, 0xb00, {0x40, 13, 15, 10, 3, 1, 3, 0xe8, 3});
    for (unsigned i : {0u, 2u}) {
        const size_t r = 0xb00 + 13 + i * 18;
        b[r] = 1;
        word(b, r + 3, 0xff9c);
        word(b, r + 5, 500);
    }
    dword(b, p + 0x98, 0xc00);
    put(b, 0xc00, {0x10, 7, 5, 2, 5, 2, 0, 1});
    dword(b, 0xc08, 64000);
    b[0xc16] = 1;
    dword(b, 0xc17, 1911000);
    dword(b, p + 0xa8, 0xd00);
    put(b, 0xd00, {0x10, 6, 49, 3});
    for (unsigned i = 0; i < 3; ++i) {
        const size_t r = 0xd06 + i * 49;
        b[r] = 1;
        dword(b, r + 3, 1024);
        dword(b, r + 7, 2048);
        put(b, r + 19, {uint8_t(i == 2 ? 5 : 2 * i), 2, 3});
    }
    dword(b, p + 0xac, 0xe00);
    put(b, 0xe00, {0x10, 5, 10, 1, 0, 0x81});
    dword(b, 0xe0b, 7);
    dword(b, p + 0xe8, 0xe40);
    put(b, 0xe40, {0x10, 11, 17, 2});
    b[0xe4b] = 3;
    dword(b, p + 0x58, cooler);
    put(b, cooler, {0x10, 6, 26, 2});
    put(b, cooler + 6, {1, 0, 10, 90});
    word(b, cooler + 6 + 11, 25000);
    word(b, cooler + 6 + 14, 200);
    word(b, cooler + 6 + 16, 2000);
    dword(b, p + 0x5c, policy);
    put(b, policy, {0x20, 4, 51, 8, 1, 1});
    for (unsigned i = 0; i < 3; ++i) {
        b[policy + 4 + 14 + i] = uint8_t(15 + 20 * i);
        word(b, policy + 4 + 18 + i * 4, (40 + 20 * i) * 32);
        word(b, policy + 4 + 20 + i * 4, 300 + 350 * i);
    }
    dword(b, p + 0x64, mapping);
    put(b, mapping, {0x10, 4, 5, 2, 1});
    dword(b, p + 0xdc, control);
    put(b, control, {0x10, 4, 13, 2, 1});
    b[control + 9] = 0x31;
    dword(b, p + 0xe0, speed);
    put(b, speed, {0x10, 4, 6, 2, 1});
    word(b, speed + 6, 500);
    return b;
}
void insert_archive(Bytes& b) {
    b.resize(0x6000);
    for (const unsigned start : {0x3000u, 0x4000u}) {
        const unsigned header = start + 0x700;
        put(b, start, {0x55, 0xaa});
        word(b, start + 24, 0x700);
        put(b, header, {'P', 'C', 'I', 'R'});
        word(b, header + 4, 0x10de);
        word(b, header + 6, device);
        word(b, header + 10, 24);
        word(b, header + 16, start == 0x3000 ? 8 : 16);
        b[header + 20] = start == 0x3000 ? 3 : 0xe0;
    }
    constexpr size_t dir = 0x4200, base = dir + 16 + 60 * 3;
    dword(b, dir, 3);
    dword(b, dir + 4, 16);
    dword(b, dir + 8, 3);
    dword(b, dir + 12, device);
    dword(b, dir + 16, 0xfb);
    dword(b, dir + 24, 8);
    dword(b, dir + 76, 0xbe);
    dword(b, dir + 80, 0x4700 - base);
    dword(b, dir + 84, 24);
    dword(b, dir + 136, 0x8e);
    dword(b, dir + 140, 0x5000 - base);
    dword(b, dir + 144, 47);
    dword(b, p + 0xe8, 0x4000);
    put(b, 0x5000, {0x10, 11, 18, 2});
    b[0x500b] = 7;
    b[0x501d] = 8;
}
const VbiosOverrideFamily& find(const VbiosOverrideInventory& inv, const std::string& name) {
    for (const auto& family : inv.families)
        if (family.name == name)
            return family;
    throw std::runtime_error("Missing family " + name);
}
void expect(const VbiosOverrideInventory& inv, const char* family, const char* text) {
    const auto& row = find(inv, family);
    if (!row.identified || row.observation.find(text) == std::string::npos)
        throw std::runtime_error(row.name + " returned " + row.observation);
}
} // namespace

int main() {
    try {
        const auto pascal = test::pascal_rail_clamp_fixture();
        expect(inspect_vbios_override_inventory(pascal, device, 4), "P-state clock overrides", "XBAR ratio 95%");
        check(!find(inspect_vbios_override_inventory(pascal, device, 7), "P-state clock overrides").identified,
              "A ROM from a different architecture cannot qualify a P-state layout");
        auto b = sample();
        const auto original = b;
        auto r = inspect_vbios_override_inventory(b, device);
        check(r.error.empty() && b == original, "Valid inventory must preserve the entire ROM");
        expect(r, "Operating temperature", "85 C in policy 1");
        expect(r, "Clock offset ranges", "GPC -100..500 MHz");
        expect(r, "Factory clocks", "Boost clock 1911 MHz");
        expect(r, "Performance controllers", "3 utilization controllers");
        expect(r, "Fan channels and curves", "10..90%, 200..2000 RPM, PWM 25000 Hz");
        expect(r, "Idle power / DIFR", "1 of 2");
        expect(r, "Voltage rails", "3 absolute REL/ALT bounds");
        check(!find(r, "Idle power / MS").identified && !find(r, "Idle power / MS").observation.empty(),
              "A missing low-power table has its own reason");
        b[mapping + 5] = 1;
        b[0xa35] = 3;
        b[0xb00] = 0;
        r = inspect_vbios_override_inventory(b, device);
        for (const auto* key :
             {"Fan channels and curves", "Operating temperature", "Clock offset ranges", "Factory clocks"})
            check(!find(r, key).identified, "Corrupt range, fan route or thermal triplet cannot qualify a field");
        expect(r, "Performance controllers", "3 utilization");
        expect(r, "Voltage rails", "3 absolute");
        check(!inspect_vbios_override_inventory(original, 0xaabb).error.empty(),
              "ROM from another adapter rejected");
        check(!inspect_vbios_override_inventory(original, 0x12340000u | device).error.empty(),
              "An unnormalized device ID must not be truncated into a match");
        b = original; b[cooler + 9] = 101;
        check(!find(inspect_vbios_override_inventory(b,device), "Fan channels and curves").identified,
              "Fan duty outside 0..100 cannot identify the fan limits");
        b = original; word(b, cooler + 22, 60000);
        check(!find(inspect_vbios_override_inventory(b,device), "Fan channels and curves").identified,
              "Unsupported RPM encoding cannot qualify a fan channel");
        b = original;
        dword(b, p + 0xdc, 0x2ffb);
        dword(b, 0xe0b, 0x17);
        r = inspect_vbios_override_inventory(b, device);
        check(!find(r, "Fan channels and curves").identified && !find(r, "Performance controllers").identified,
              "Truncated fan table and out-of-bounds policy members stay unavailable");
        expect(r, "Operating temperature", "85 C");
        b = original;
        insert_archive(b);
        r = inspect_vbios_override_inventory(b, device);
        expect(r, "Idle power / DIFR", "DFPR 1, SW_ASR 1, CG 0");
        dword(b, 0x4200 + 136, 0x8f);
        r = inspect_vbios_override_inventory(b, device);
        check(!find(r, "Idle power / DIFR").identified, "Wrong object ID cannot identify DIFR flags");
        std::cout << "VBIOS inventory fixtures passed\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
