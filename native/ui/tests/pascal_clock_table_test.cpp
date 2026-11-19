// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "../pascal_clock_table.h"
#include "pascal_clock_fixture.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <limits>

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
bool near(std::optional<double> actual, double expected) {
    return actual && std::abs(*actual - expected) < 1e-8;
}
void run_legacy_targets() {
    for (uint32_t architecture : {2u, 3u}) {
        const auto rom = test::legacy_clock_fixture(architecture);
        const auto table = inspect_pascal_clocks(rom, architecture);
        const auto* xbar = find(table, Domain::xbar, 0);
        const auto* sys = find(table, Domain::sys, 0);
        const auto* low_xbar = find(table, Domain::xbar, 8);
        check(table.ok && table.state_targets && table.fields.size() == 6 && !find(table, Domain::xbar, -1) &&
              xbar && xbar->current == 1165 && xbar->minimum == 810 && xbar->maximum == 1165 &&
              sys && sys->current == (architecture == 2 ? 1134 : 1120) && low_xbar && low_xbar->current == 810,
              "Legacy plan decodes independent P0 and P8 targets and skips disabled rows");
        check(!inspect_pascal_clocks(rom, 4).ok && !inspect_pascal_clocks(rom, 6).ok &&
              !inspect_pascal_clocks(test::pascal_clock_fixture(), architecture).ok,
              "A legacy record cannot qualify the Pascal or newer architecture and vice versa");

        constexpr int draft = 1001;
        const auto changed = plan_pascal_clocks(rom, {{Domain::xbar, 0, 1165, draft}}, architecture);
        auto expected = rom;
        const size_t xbar_at = architecture == 2 ? 0x3f2 : 0x411;
        expected[xbar_at] = static_cast<uint8_t>(draft & 0xff);
        expected[xbar_at + 1] = static_cast<uint8_t>((expected[xbar_at + 1] & 0xc0) | (draft >> 8));
        check(changed.ok && changed.image == expected,
              "Legacy edit changes just the target bits and preserves neighboring flags, values and states");
        check(!plan_pascal_clocks(rom, {{Domain::xbar, 0, 1165, 1166}}, architecture).ok &&
              !plan_pascal_clocks(rom, {{Domain::xbar, 0, 1165, 809}}, architecture).ok,
              "Legacy writes stay inside this adapter's programmed domain range");
        if (!changed.ok) continue;
        const auto recaptured = inspect_pascal_clocks(changed.image, architecture);
        const auto* recaptured_xbar = find(recaptured, Domain::xbar, 0);
        const auto installed = inspect_pascal_clocks(changed.image, architecture, &table);
        const auto* installed_xbar = find(installed, Domain::xbar, 0);
        const auto undo = plan_pascal_clocks(changed.image, {{Domain::xbar, 0, draft, 1165}}, architecture, &table);
        check(recaptured_xbar && recaptured_xbar->maximum == draft &&
              installed.ok && installed_xbar && installed_xbar->current == draft && installed_xbar->maximum == 1165 &&
              undo.ok && undo.image == rom,
              "Original ranges keep the highest target editable and allow exact restoration");
    }
}
void run_clock_previews() {
    const auto table = inspect_pascal_clocks(test::pascal_clock_fixture());
    constexpr double p0_peak = 1835;
    const auto missing = pascal_clock_preview(table, {}, Domain::xbar, 0);
    check(!missing.nominal_mhz && near(missing.ceiling_mhz, 1800),
          "The independent firmware ceiling is available without the P0 curve peak");
    const auto current = pascal_clock_preview(table, {}, Domain::xbar, 0, p0_peak);
    check(near(current.nominal_mhz, 1743.25) && near(current.ceiling_mhz, 1800),
          "The current XBAR ratio uses the supplied P0 curve peak");

    const std::vector<PascalClockEdit> drafts{{Domain::xbar, -1, 95, 200},
        {Domain::xbar, 0, 3600, 3001}, {Domain::gpc, 0, 3800, 3201}};
    const auto xbar = pascal_clock_preview(table, drafts, Domain::xbar, 0, p0_peak);
    check(near(xbar.nominal_mhz, 3670) && near(xbar.ceiling_mhz, 1500.5),
          "The 200 percent ratio estimate and staged half-MHz XBAR ceiling remain independent");
    const auto gpc = pascal_clock_preview(table, drafts, Domain::gpc, 0, p0_peak);
    check(near(gpc.nominal_mhz, p0_peak) && near(gpc.ceiling_mhz, 1600.5),
          "The GPC estimate reports the P0 curve peak beside the staged ceiling");
    const auto moved = pascal_clock_preview(table, drafts, Domain::xbar, 0, 1800);
    check(near(moved.nominal_mhz, 3600), "A different P0 peak updates the ratio estimate");
    const auto p8 = pascal_clock_preview(table, drafts, Domain::xbar, 8);
    check(!p8.nominal_mhz && near(p8.ceiling_mhz, 600),
          "P8 retains its own ceiling without a supplied P0 peak");
    const auto peer = pascal_clock_preview(table, drafts, Domain::sys, 0, p0_peak);
    check(near(peer.nominal_mhz, 1651.5), "Only XBAR uses the staged ratio");
    auto both = drafts;
    both.push_back({Domain::sys, -1, 90, 200});
    const auto sys = pascal_clock_preview(table, both, Domain::sys, 0, p0_peak);
    check(near(sys.nominal_mhz, 3670) && near(sys.ceiling_mhz, 1700),
          "SYS independently estimates 200 percent of the P0 peak");

    const auto unavailable = pascal_clock_preview(table, {}, Domain::xbar, 2);
    check(!unavailable.nominal_mhz && !unavailable.ceiling_mhz, "An unqualified state never fabricates frequencies");
    const auto stale = pascal_clock_preview(table, {{Domain::xbar, -1, 94, 100}}, Domain::xbar, 0, p0_peak);
    check(!stale.nominal_mhz && stale.ceiling_mhz, "Stale ratio drafts leave the independent ceiling available");
    auto ratio_only = table;
    ratio_only.fields.erase(std::remove_if(ratio_only.fields.begin(), ratio_only.fields.end(),
        [](const PascalClockField& field) { return field.pstate >= 0; }), ratio_only.fields.end());
    const auto ratio_peak = pascal_clock_preview(ratio_only, {}, Domain::xbar, 0, 1200);
    check(near(ratio_peak.nominal_mhz, 1140) && !ratio_peak.ceiling_mhz,
          "An adapter with only a qualified ratio can estimate from a known P0 peak");
    for (const double invalid : {0.0, -1.0, std::numeric_limits<double>::infinity()}) {
        const auto estimate = pascal_clock_preview(table, {}, Domain::xbar, 0, invalid);
        check(!estimate.nominal_mhz && near(estimate.ceiling_mhz, 1800),
              "An invalid P0 peak leaves the estimate unavailable");
    }

    PascalClockTable independent;
    independent.ok = true;
    independent.state_targets = true;
    independent.fields = {{Domain::gpc, 0, 1200, 900, 1200}, {Domain::xbar, 0, 1100, 900, 1100},
                          {Domain::sys, 0, 1000, 900, 1000}, {Domain::xbar, 8, 900, 810, 900}};
    const std::vector<PascalClockEdit> independent_drafts{{Domain::gpc, 0, 1200, 1050},
                                                       {Domain::xbar, 0, 1100, 1001}};
    const auto older = pascal_clock_preview(independent, independent_drafts, Domain::xbar, 0, p0_peak);
    check(near(older.nominal_mhz, 500.5) && !older.ceiling_mhz,
          "An independent firmware target previews its own staged single-rate MHz without a ratio or ceiling");
    check(near(pascal_clock_preview(independent, independent_drafts, Domain::sys, 0, p0_peak).nominal_mhz, 500) &&
          near(pascal_clock_preview(independent, independent_drafts, Domain::xbar, 8).nominal_mhz, 450),
          "A legacy draft leaves peer domains and other states at their programmed targets");
}
void run() {
    run_clock_previews();
    run_legacy_targets();
    constexpr int ratio_max = 200;
    constexpr size_t sys_ratio_byte = 0x200 + 8 + 21 + 14, xbar_ratio_byte = sys_ratio_byte + 3;
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

    auto stock = rom;
    stock[xbar_ratio_byte] = stock[sys_ratio_byte] = 100;
    const auto stock_table = inspect_pascal_clocks(stock);
    const auto xbar_ratio = find(stock_table, Domain::xbar, -1);
    const auto sys_ratio = find(stock_table, Domain::sys, -1);
    check(stock_table.ok && xbar_ratio && xbar_ratio->current == 100 &&
          xbar_ratio->minimum == 1 && xbar_ratio->maximum == ratio_max &&
          sys_ratio && sys_ratio->current == 100 &&
          sys_ratio->minimum == 1 && sys_ratio->maximum == ratio_max,
          "Both stock ratios remain readable with the qualified range");
    auto expected = stock;
    expected[xbar_ratio_byte] = expected[sys_ratio_byte] = ratio_max;
    const auto raised = plan_pascal_clocks(stock, {{Domain::xbar, -1, 100, ratio_max},
                                                  {Domain::sys, -1, 100, ratio_max}});
    check(raised.ok && raised.image == expected, "Both ratios can reach 200 percent by changing their own bytes");
    const auto raised_table = inspect_pascal_clocks(raised.image);
    const auto raised_xbar = find(raised_table, Domain::xbar, -1);
    const auto raised_sys = find(raised_table, Domain::sys, -1);
    check(raised_table.ok && raised_table.fields.size() == t.fields.size() &&
          raised_xbar && raised_xbar->current == ratio_max && raised_sys && raised_sys->current == ratio_max,
          "Installed ratios at the new maxima preserve each domain's state fields");
    const auto restored = plan_pascal_clocks(raised.image, {{Domain::xbar, -1, ratio_max, 100},
                                                           {Domain::sys, -1, ratio_max, 100}});
    check(restored.ok && restored.image == stock, "Restore the original 100 percent ratios exactly");
    const auto lowered = plan_pascal_clocks(raised.image, {{Domain::xbar, -1, ratio_max, 1},
                                                          {Domain::sys, -1, ratio_max, 1}});
    const auto lower_table = inspect_pascal_clocks(lowered.image);
    const auto lower_xbar = find(lower_table, Domain::xbar, -1);
    const auto lower_sys = find(lower_table, Domain::sys, -1);
    check(lowered.ok && lower_xbar && lower_xbar->current == 1 && lower_sys && lower_sys->current == 1,
          "Lower ratio endpoints still apply and parse after a high ratio");
    for (const auto& bad : std::vector<std::vector<PascalClockEdit>>{
             {}, {{Domain::sys, 2, 3400, 3000}}, {{Domain::gpc, -1, 95, 89}},
             {{Domain::sys, 0, 3400, 3501}}, {{Domain::sys, 0, 3400, 1249}},
             {{Domain::sys, 0, 3399, 3200}}, {{Domain::xbar, -1, 95, 0}},
             {{Domain::xbar, -1, 95, ratio_max + 1}}, {{Domain::sys, -1, 90, ratio_max + 1}},
             {{Domain::sys, 0, 3400, 3100}, {Domain::sys, 0, 3400, 3200}},
             {{Domain::sys, 0, 3400, 3100}, {Domain::xbar, 8, 1201, 1100}}}) {
        const auto r = plan_pascal_clocks(rom, bad);
        check(!r.ok && r.image.empty(), "Invalid, stale or duplicate requests return no partial image");
    }
    auto b = rom;
    b[0x200 + 8 + 21 + 13] = 1; // both master slots now identify XBAR, neither identifies SYS
    auto partial = inspect_pascal_clocks(b);
    check(partial.ok && partial.fields.size() == 3 && find(partial, Domain::gpc, 5),
          "Missing and ambiguous dependent bindings leave independently qualified GPC available");
    for (int unqualified : {0, ratio_max + 1, 255}) {
        b = expected;
        b[sys_ratio_byte] = static_cast<uint8_t>(unqualified);
        partial = inspect_pascal_clocks(b);
        check(partial.ok && !find(partial, Domain::sys, -1) &&
              find(partial, Domain::xbar, -1) && find(partial, Domain::gpc, 0),
              "An unqualified installed SYS ratio leaves XBAR and GPC controls available");
    }
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
