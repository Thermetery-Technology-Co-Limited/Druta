// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main unused_voltage_graph_fixture_main
#include "vbios_voltage_graph_test.cpp"
#undef main
#include "pascal_clock_fixture.h"
#include "vbios_rail_clamps.h"
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>

namespace {
void test_clamps() {
    constexpr uint16_t device = 0x1bbc;
    const auto bytes = test::pascal_rail_clamp_fixture();
    auto read = [&](const std::vector<uint8_t>& b) { return inspect_vbios_rail_clamps(b, device); };
    const auto rows = read(bytes);
    check(rows.size() == 2 && !rows[0].sram && rows[0].rel_uv == 1012375 && rows[0].alt_uv == 1125125 &&
          rows[1].sram && !rows[1].rel_uv && rows[1].alt_uv == 821750,
          "use HAL rail domains and relocated role selectors, independently of PCI board model");
    check(!parse_vbios_voltage_graph(bytes).ok, "legacy inventory cannot qualify the modern writable graph");
    check(inspect_vbios_rail_clamps(bytes, 0x4321).empty(), "image for another PCI device omitted");

    constexpr size_t rel_eq = 0x920 + 24 * 3;
    auto b = bytes;
    std::swap(b[0x809], b[0x80a]);
    auto swapped = read(b);
    check(swapped.size() == 2 && swapped[0].rel_uv == 1125125 && swapped[0].alt_uv == 1012375,
          "selectors determine attribution when only role order changes");
    for (const auto& change : {std::pair<size_t, uint8_t>{rel_eq + 23, 4}, {rel_eq, 7}, {rel_eq + 1, 1},
                               {rel_eq + 2, 3}, {0x809, 0xfe}, {0x809, 0xff}}) {
        b = bytes;
        b[change.first] = change.second;
        const auto result = read(b);
        check(result.size() == 2 && !result[0].rel_uv && result[0].alt_uv == 1125125,
              "delta, unknown, chained, dangling, or absent equation leaves independent ALT available");
    }
    b = bytes;
    number(b, rel_eq + 11, std::numeric_limits<float>::infinity());
    auto nonfinite = read(b);
    check(nonfinite.size() == 2 && !nonfinite[0].rel_uv, "nonfinite coefficient cannot qualify an upper clamp");
    b = bytes; number(b, rel_eq + 3, 2000000);
    auto reversed = read(b);
    check(reversed.size() == 2 && !reversed[0].rel_uv, "reversed bounds omitted");
    b = bytes; b[0x804] = 2;
    check(read(b).empty(), "unknown HAL cannot label a rail");
    b = bytes; b[0x904] = 23;
    check(read(b).empty(), "unknown equation geometry omitted");
    b = bytes; dword(b, 0x720, 0xffb);
    check(read(b).empty(), "truncated VFE header omitted");

    b = fixture(true, device);
    std::copy_n(bytes.begin() + 0x800, 19, b.begin() + rail);
    std::copy_n(bytes.begin() + 0x900, 32 + 24 * 4, b.begin() + object_vfe);
    const auto archived = read(b);
    check(archived.size() == 2 && archived[0].alt_uv == 1125125,
          "same table layout in a relocated, owned VFE object");
    dword(b, 0x2210 + 120, 0x15);
    check(read(b).empty(), "a different object's bytes cannot qualify as VFE");
}

int inspect(const char* path, const char* device) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    const std::streamoff size = file.tellg();
    if (size < 0 || size > vbios_voltage_max_image_bytes) return 2;
    std::vector<uint8_t> bytes(static_cast<size_t>(size));
    file.seekg(0);
    if (!file.read(reinterpret_cast<char*>(bytes.data()), size)) return 2;
    const auto rows = inspect_vbios_rail_clamps(bytes, static_cast<uint32_t>(std::stoul(device)));
    std::cout << '[' << std::setprecision(std::numeric_limits<float>::max_digits10);
    auto value = [](const std::optional<float>& uv) {
        if (uv) std::cout << *uv;
        else std::cout << "null";
    };
    for (size_t i = 0; i < rows.size(); ++i) {
        const auto& row = rows[i];
        if (i) std::cout << ',';
        std::cout << "{\"domain\":\"" << (row.sram ? "sram" : "logic") << "\",\"REL\":";
        value(row.rel_uv);
        std::cout << ",\"ALT\":";
        value(row.alt_uv);
        std::cout << '}';
    }
    std::cout << "]\n";
    return 0;
}
}

int main(int argc, char** argv) {
    if (argc == 4 && std::string(argv[1]) == "--inspect") return inspect(argv[2], argv[3]);
    if (argc != 1) return 2;
    test_clamps();
    std::printf("VBIOS rail clamps: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
