// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "../pascal_clock_table.h"
#include "pascal_clock_fixture.h"
#include <algorithm>
#include <cstdio>
#include <fstream>
#include <iterator>

namespace {
using namespace druta::ui;
using Domain = PascalClockDomain;
int checks = 0, failures = 0;
void check(bool condition, const char* why) {
    ++checks;
    if (!condition) { ++failures; std::fprintf(stderr, "%s\n", why); }
}
const PascalClockField* find(const PascalClockTable& table, Domain d, int state) {
    const auto it = std::find_if(table.fields.begin(), table.fields.end(), [&](const PascalClockField& field) {
        return field.domain == d && field.pstate == state;
    });
    return it == table.fields.end() ? nullptr : &*it;
}
void run() {
    const auto rom = test::pascal_clock_fixture();
    const auto t = inspect_pascal_clocks(rom);
    check(t.ok && t.fields.size() == 11 && t.device_id == 0x1bbc &&
          t.selector_version == 0x86070102 && t.selector_oem == 0x22,
          "Three independent domains across three states and two shared ratios");
    const auto sys = find(t, Domain::sys, 0);
    check(sys && sys->minimum == 1250 && sys->current == 3400 && sys->maximum == 3500,
          "State nominal, current maximum and program ceiling use the selected card");
    const auto p8 = find(t, Domain::xbar, 8);
    check(p8 && p8->current == 1200 && p8->minimum == 1050, "P8 is decoded by its level, independently of P0");
    const auto result = plan_pascal_clocks(rom, {{Domain::sys, 0, 3400, 2701}});
    check(result.ok && result.image.size() == rom.size(), "Plan a P0 SYS edit");
    if (result.ok) {
        auto expected = rom;
        const size_t packed = 0x300 + 10 + 2 * 57 + 3 + 3 * 6 + 2;
        expected[packed + 1] = uint8_t((expected[packed + 1] & 0x3f) | ((2701u & 3) << 6));
        expected[packed + 2] = uint8_t((2701u >> 2) & 255);
        expected[packed + 3] = uint8_t((expected[packed + 3] & 0xf0) | (2701u >> 10));
        check(result.image == expected, "Only the three intersecting bytes can differ and all adjacent bits survive");
        const auto undo = plan_pascal_clocks(result.image, {{Domain::sys, 0, 2701, 3400}});
        check(undo.ok && undo.image == rom, "Exact image restoration");
    }
    const auto combined = plan_pascal_clocks(rom, {{Domain::gpc, 5, 1800, 1349},
        {Domain::xbar, 8, 1200, 1167}, {Domain::sys, -1, 90, 89}});
    const auto changed = inspect_pascal_clocks(combined.image);
    bool values_match = combined.ok && changed.fields.size() == t.fields.size();
    for (const auto& f : t.fields) {
        const auto after = find(changed, f.domain, f.pstate);
        const int expected = f.pstate == 5 && f.domain == Domain::gpc ? 1349 :
                             f.pstate == 8 && f.domain == Domain::xbar ? 1167 :
                             f.pstate == -1 && f.domain == Domain::sys ? 89 : f.current;
        values_match &= after && after->current == expected;
    }
    check(values_match, "Distinct state maxima and shared ratio apply independently");
    for (const auto& bad : std::vector<std::vector<PascalClockEdit>>{
             {}, {{Domain::sys, 2, 3400, 3000}}, {{Domain::gpc, -1, 95, 89}},
             {{Domain::sys, 0, 3400, 3501}}, {{Domain::sys, 0, 3400, 1249}},
             {{Domain::sys, 0, 3399, 3200}}, {{Domain::xbar, -1, 95, 0}},
             {{Domain::xbar, -1, 95, 101}}, {{Domain::sys, 0, 3400, 3100}, {Domain::sys, 0, 3400, 3200}},
             {{Domain::sys, 0, 3400, 3100}, {Domain::xbar, 8, 1201, 1100}}}) {
        const auto r = plan_pascal_clocks(rom, bad);
        check(!r.ok && r.image.empty(), "Invalid, stale or duplicate requests return no partial image");
    }
    auto b = rom;
    b[0x200 + 8 + 21 + 13] = 1; // both master slots now identify XBAR, neither identifies SYS
    auto partial = inspect_pascal_clocks(b);
    check(partial.ok && partial.fields.size() == 3 && find(partial, Domain::gpc, 5),
          "Missing and ambiguous dependent bindings leave independently qualified GPC available");
    b = rom;
    b[0x100 + 16 + 3] = 2; // XBAR owned by a different master
    partial = inspect_pascal_clocks(b);
    check(partial.ok && !find(partial, Domain::xbar, 0) && find(partial, Domain::sys, 5),
          "An unrelated dependent master cannot inherit GPC's ratio slot");
    b = rom;
    b[0x100 + 7 + 4 * 9 + 1] = b[0x100 + 7 + 4 * 9 + 2] = 2;
    partial = inspect_pascal_clocks(b);
    check(partial.ok && !find(partial, Domain::xbar, -1) && find(partial, Domain::sys, 0),
          "Shared clocks program rejects its ambiguous owner only");
    b = rom;
    b[0x300] = 0x40;
    partial = inspect_pascal_clocks(b);
    check(partial.ok && partial.fields.size() == 2 && find(partial, Domain::sys, -1),
          "Unrecognized separate performance layout retains qualified shared ratios");
    b = rom;
    b[0x300 + 10 + 2 * 57 + 3 + 3 * 6 + 1] = 0xde; // oversized SYS P0 nominal
    partial = inspect_pascal_clocks(b);
    check(partial.ok && !find(partial, Domain::sys, 0) && find(partial, Domain::sys, 5) &&
          find(partial, Domain::xbar, 0), "Unordered tuple isolated to that field and state");
    b = rom; b[0x300 + 10 + 2 * 57] = 10;
    check(!inspect_pascal_clocks(b).ok, "Duplicate performance state rejected");
    b = rom; b[0x100 + 4] = 1;
    check(!inspect_pascal_clocks(b).ok, "Unrecognized HAL domain mapping refused");
    b = rom; b[0xcc] = 0; b[0xcd] = 1;
    check(!inspect_pascal_clocks(b).ok, "Aliased clock domains and programs refused");
    b = rom; b[0xd0] = 0x20; b[0xd1] = 2;
    check(!inspect_pascal_clocks(b).ok, "Performance table pointing inside a ratio program refused");
    b = rom; b[0xd0] = 0xff; b[0xd1] = 0x0f;
    check(!inspect_pascal_clocks(b).ok, "Truncated performance extent refused");
    b = rom; b[0xd3] = 0xff;
    check(!inspect_pascal_clocks(b).ok, "Pointer beyond host size refused");
    b = rom; b[139]++;
    check(!inspect_pascal_clocks(b).ok, "Bad BIT checksum refused");
    b = rom; b[152] = 'C';
    check(!inspect_pascal_clocks(b).ok, "Duplicate BIT token refused");
    b = rom; b.resize(692);
    check(!inspect_pascal_clocks(b).ok, "Incomplete legacy image refused");
}
}

int main(int argc, char** argv) {
    run();
    // Optional, strictly offline capture check. Do not bundle a board ROM.
    if (argc == 2) {
        std::ifstream file(argv[1], std::ios::binary);
        std::vector<uint8_t> image{std::istreambuf_iterator<char>(file), {}};
        const auto t = druta::ui::inspect_pascal_clocks(image);
        check(t.ok, t.message.c_str());
        for (const auto& f : t.fields) {
            std::printf("domain=%d state=%d current=%d range=%d..%d\n",
                        int(f.domain), f.pstate, f.current, f.minimum, f.maximum);
            if (f.current <= f.minimum) continue;
            const auto patch = druta::ui::plan_pascal_clocks(image, {{f.domain, f.pstate, f.current, f.current - 1}});
            const auto restore = druta::ui::plan_pascal_clocks(patch.image, {{f.domain, f.pstate, f.current - 1, f.current}});
            check(patch.ok && patch.image != image && restore.ok && restore.image == image, "Capture field round trip");
        }
    }
    std::printf("Pascal clock table: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
