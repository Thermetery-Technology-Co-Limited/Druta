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
void timing() {
    const auto image=test::pascal_timing_fixture();
    const PascalTimingSample sample{3504,0x2966c296,0x38aa};
    const PascalClockEdit edit{Domain::tfaw,0,28,29,sample};
    auto table=inspect_pascal_clocks(image,4);
    inspect_pascal_tfaw(table,image,4,sample);
    check(table.ok && table.timing_sample==sample,"Resolve the P0 memory band and controller fingerprint");
    const auto plan=plan_pascal_clocks(image,{edit});
    auto expected=image;
    expected[0x755]^=2;
    check(plan.ok&&plan.image==expected,"Only one tFAW bit changes, with tCL, reserved CONFIG3 bits and all other rows preserved");
    const auto changed=with_tfaw(sample,29);
    check(plan.ok && plan_pascal_clocks(plan.image,{{Domain::tfaw,0,29,28,changed}}).image==image,"tFAW restores the exact source image");
    auto contradicts=sample;
    contradicts.config3|=0x31000000u;
    check(!plan_pascal_clocks(image,{{Domain::tfaw,0,28,29,contradicts}}).ok,
          "A reported nonzero CONFIG3 high byte must match the ROM");
    auto full=sample;
    full.config3|=0x22000000u;
    check(plan_pascal_clocks(image,{{Domain::tfaw,0,28,29,full}}).image==expected,
          "A complete matching controller word qualifies");
    auto other=image;
    other[0x60c]=2;
    check(!plan_pascal_clocks(other,{edit}).ok,"A row shared with another memory band cannot be changed here");
    other=image;
    std::copy_n(other.begin()+0x748,24,other.begin()+0x728);
    other[0x728+15]=0; // exact upper-byte match vs GP107's known masked counterpart
    check(!plan_pascal_clocks(other,{edit}).ok,"Indistinguishable strap rows are ambiguous");
    other=image;other[0x616]=5;
    check(!plan_pascal_clocks(other,{edit}).ok,"Malformed row indexes block timing changes without affecting normal clocks");
    check(!plan_pascal_clocks(image,{{Domain::tfaw,5,28,29,sample}}).ok &&
          !plan_pascal_clocks(image,{{Domain::tfaw,0,28,29}}).ok &&
          !plan_pascal_clocks(image,{{Domain::tfaw,0,28,256,sample}}).ok &&
          !plan_pascal_clocks(image,{{Domain::tfaw,0,28,-1,sample}}).ok &&
          !plan_pascal_clocks(image,{edit},7).ok,"Only qualified Pascal P0 and representable field encodings proceed");
}

void all_timing_fields() {
    const auto image=test::pascal_timing_fixture();
    const auto sample=test::pascal_full_timing_sample();
    PascalClockTable table;
    inspect_pascal_timings(table,image,4,sample);
    check(table.timing_fields.size()==23 && table.timing_message.empty(),"All six matching words expose 22 timing controls and tCL");
    const std::vector<std::pair<Domain,uint32_t>> masks{
        {Domain::trc,0xff}, {Domain::trfc,0x1ff00}, {Domain::tras,0xfe0000}, {Domain::trp,0x7f000000},
        {Domain::tcl,0x7f}, {Domain::twl,0x3f80}, {Domain::trcd_rd,0xfc000}, {Domain::trcd_wr,0x3f00000},
        {Domain::rpre,0xf}, {Domain::wpre,0xf0}, {Domain::cdlr,0x7f00}, {Domain::twr,0x7f0000},
        {Domain::w2r_bus,0xf000000}, {Domain::r2w_bus,0xf0000000}, {Domain::pdex,0x1f},
        {Domain::pden2pdex,0x1e0}, {Domain::tfaw,0x1fe00}, {Domain::aond,0xfe0000},
        {Domain::tccdl,0xf000000}, {Domain::tccds,0xf0000000}, {Domain::trefi,0x7ff8},
        {Domain::trrd,0x1f8000}, {Domain::wrcrc,0x7f0}
    };
    auto expected=image;
    auto changed=sample;
    std::vector<PascalClockEdit> edits,restore;
    for(const auto& [domain,mask]:masks) {
        const auto& field=*pascal_timing_field(domain);
        const int current=pascal_timing_value(sample,domain);
        const int maximum=int(mask>>field.shift);
        if(domain==Domain::tcl) {
            check(!field.writable&&!plan_pascal_clocks(image,{{domain,0,current,current+1,sample}}).ok,
                  "A directly requested tCL change is always refused");
            continue;
        }
        check(field.writable && pascal_timing_label(domain)[0],"Qualified controller timing has an editable label");
        edits.push_back({domain,0,current,maximum,sample});
        changed=with_pascal_timing(changed,domain,maximum);
        for(unsigned j=0;j<4;++j)expected[0x748+field.word*4+j]|=static_cast<uint8_t>(mask>>(j*8));
        check(!plan_pascal_clocks(image,{{domain,0,current,maximum+1,sample}}).ok,"Field maximum protects adjacent bits");
    }
    const auto plan=plan_pascal_clocks(image,edits);
    check(plan.ok && plan.image==expected && pascal_timing_value(changed,Domain::tcl)==pascal_timing_value(sample,Domain::tcl),
          "Batch updates full-width adjacent fields, leaving tCL, reserved, training, and other rows unchanged");
    for(const auto& edit:edits)restore.push_back({edit.domain,0,edit.value,edit.expected,changed});
    check(plan.ok&&plan_pascal_clocks(plan.image,restore).image==image,"All firmware timing fields restore byte for byte");
    edits.push_back(edits.front());
    check(!plan_pascal_clocks(image,edits).ok,"Reject duplicate fields even within one controller word");

    auto partial=sample;
    partial.present&=~uint8_t(1u<<2);
    partial.config3&=0xffffff;
    partial.config5^=0x10;
    inspect_pascal_timings(table,image,4,partial);
    check(table.timing_fields.size()==14 && !table.timing_message.empty(),"Preserve other fields when CONFIG2 is missing, CCD is masked and WRCRC differs");
    for(const auto d:{Domain::twr,Domain::tccdl,Domain::tccds,Domain::wrcrc})
        check(!plan_pascal_clocks(image,{{d,0,0,1,partial}}).ok,"Unqualified field cannot enter a direct timing request");
    const PascalTimingSample legacy{3504,sample.config1,sample.config3};
    check(matches_pascal_timing(sample,legacy) && !matches_pascal_timing(legacy,sample),"Two-word legacy evidence can be verified by a six-word sample");

    const auto zero=with_pascal_timing(sample,Domain::tfaw,0);
    const auto zero_row=plan_pascal_clocks(image,{{Domain::tfaw,0,28,0,sample}});
    check(zero_row.ok,"A zero tFAW encoding is representable in a populated P0 row");
    inspect_pascal_timings(table,zero_row.image,4,zero);
    check(table.timing_fields.size()==23,"Zero tFAW leaves independently matched timings available");
    const auto raised=plan_pascal_clocks(zero_row.image,{{Domain::tfaw,0,0,1,zero},{Domain::trfc,0,pascal_timing_value(zero,Domain::trfc),511,zero}});
    const auto applied=with_pascal_timing(with_pascal_timing(zero,Domain::tfaw,1),Domain::trfc,511);
    check(raised.ok&&plan_pascal_clocks(raised.image,{{Domain::tfaw,0,1,0,applied},
          {Domain::trfc,0,511,pascal_timing_value(zero,Domain::trfc),applied}}).image==zero_row.image,
          "Mixed edits restore a captured zero timing byte for byte");
    auto empty=zero;empty.config1=0;empty.config3=0;
    inspect_pascal_timings(table,zero_row.image,4,empty);
    check(!table.timing_sample&&table.timing_fields.empty(),"An empty mandatory fingerprint still cannot identify a P0 row");
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
    check(sys && sys->minimum == 1250 && sys->current == 3400 && sys->maximum == 7000,
          "State nominal and 200 percent program ceiling use the selected card");
    const auto p8 = find(t, Domain::xbar, 8);
    check(p8 && p8->current == 1200 && p8->minimum == 1050, "P8 is decoded by its level, independently of P0");
    for (const auto& field : t.fields) {
        if (field.pstate < 0) continue;
        const auto raised = plan_pascal_clocks(rom, {{field.domain, field.pstate, field.current, field.maximum}});
        check(raised.ok, "Every state and domain accepts its 200 percent endpoint");
        if (!raised.ok) continue;
        const auto installed = inspect_pascal_clocks(raised.image, 4, &t);
        const auto* after = find(installed, field.domain, field.pstate);
        check(after && after->current == field.maximum && after->maximum == field.maximum,
              "Reinspection retains the original upper range and installed value");
        const auto revised = plan_pascal_clocks(raised.image,
            {{field.domain, field.pstate, field.maximum, field.current}}, 4, &t);
        check(revised.ok && revised.image == rom, "A raised field can be restored exactly");
    }
    auto large_cap = rom;
    large_cap[0x200 + 8 + 21 + 1] = 0xff;
    large_cap[0x200 + 8 + 21 + 2] = 0x3f;
    const auto bounded = inspect_pascal_clocks(large_cap);
    const auto* bounded_gpc = find(bounded, Domain::gpc, 0);
    check(bounded_gpc && bounded_gpc->maximum == 0x3fff &&
          plan_pascal_clocks(large_cap, {{Domain::gpc, 0, bounded_gpc->current, 0x3fff}}).ok,
          "Ceiling stops at the representable 14-bit maximum");
    auto foreign = rom;
    const size_t sys_packed = 0x300 + 10 + 2 * 57 + 3 + 3 * 6 + 2;
    const uint32_t foreign_word = 0xb0000000u | (7500u << 14) | 1000u;
    for (unsigned i = 0; i < 4; ++i) foreign[sys_packed + i] = uint8_t(foreign_word >> (8 * i));
    check(!find(inspect_pascal_clocks(foreign), Domain::sys, 0) &&
          !plan_pascal_clocks(foreign, {{Domain::sys, 0, 7500, 7250}}).ok,
          "Unowned out-of-program capture cannot authorize an edit");
    const auto foreign_with_original = inspect_pascal_clocks(foreign, 4, &t);
    const auto* foreign_sys = find(foreign_with_original, Domain::sys, 0);
    check(foreign_sys && foreign_sys->current == 7500 && foreign_sys->maximum == 7000 &&
          !plan_pascal_clocks(foreign, {{Domain::sys, 0, 7500, 7250}}, 4, &t).ok &&
          !plan_pascal_clocks(foreign, {{Domain::sys, 0, 7500, 6900}}, 4, &t).ok,
          "A readback beyond the original range cannot extend or authorize it");
    auto partial_stock = rom;
    const uint32_t partial_word = 0xb0000000u | (3600u << 14) | 1000u;
    for (unsigned i = 0; i < 4; ++i) partial_stock[sys_packed + i] = uint8_t(partial_word >> (8 * i));
    const auto partial_original = inspect_pascal_clocks(partial_stock);
    const auto partial_edit = plan_pascal_clocks(partial_stock, {{Domain::gpc, 0, 3800, 7800}});
    check(partial_original.ok && !find(partial_original, Domain::sys, 0) && partial_edit.ok,
          "An unqualified sibling does not prevent an independent edit");
    if (partial_edit.ok) {
        const auto partial_installed = inspect_pascal_clocks(partial_edit.image, 4, &partial_original);
        const auto* gpc = find(partial_installed, Domain::gpc, 0);
        check(partial_installed.ok && !find(partial_installed, Domain::sys, 0) && gpc &&
              gpc->current == 7800 && gpc->maximum == 7800 &&
              partial_installed.fields.size() == partial_original.fields.size(),
              "Active reinspection retains qualified fields and omits the original unqualified sibling");
    }
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
             {{Domain::sys, 0, 3400, 7001}}, {{Domain::sys, 0, 3400, 1249}},
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
    timing();
    all_timing_fields();
    run();
    // Validate saved effective images with previously recorded hardware samples.
    if (argc == 4 && std::string(argv[1]) == "--timing-evidence") {
        const auto read=[](const char* path) {
            std::ifstream f(path,std::ios::binary);
            std::vector<uint8_t> b{std::istreambuf_iterator<char>(f),{}};
            for(size_t at=0;at+26<=b.size();at+=512) {
                if(b[at]!=0x55 || b[at+1]!=0xaa)continue;
                const size_t pci=at+b[at+24]+size_t(b[at+25])*256;
                if(pci+24<=b.size() && b[pci]=='P' && b[pci+1]=='C' && b[pci+2]=='I' && b[pci+3]=='R'
                   && b[pci+4]==0xde && b[pci+5]==0x10 && b[pci+20]==0)
                    return std::vector<uint8_t>(b.begin()+at,b.end());
            }
            return std::vector<uint8_t>{};
        };
        const auto gp=read(argv[2]),ga=read(argv[3]);
        const PascalTimingSample sample{3504,0x2966c296,0x38aa};
        const auto plan=plan_pascal_clocks(gp,{{Domain::tfaw,0,28,29,sample}},4);
        auto expected=gp;
        if(expected.size()>0x2dd81)expected[0x2dd81]^=2;
        check(plan.ok && gp!=expected && plan.image==expected, "GP107: reproduce the recorded row 9 one-bit edit");
        check(plan.ok && plan_pascal_clocks(plan.image,{{Domain::tfaw,0,29,28,with_tfaw(sample,29)}}).image==gp,
              "GP107: exact full-image restore");
        // The independent P0 inventory on 2026-11-20 read CONFIG0..5 on this GPU.
        auto offline=sample;
        offline.set_word(0,0x1b58c147);
        offline.set_word(2,0x881c0900);
        offline.set_word(4,0xd5838032);
        offline.set_word(5,0xa6b270d2);
        PascalClockTable inventory;
        inspect_pascal_timings(inventory,gp,4,offline);
        check(inventory.timing_fields.size()==21,"GP107: six-word offline inventory omits the masked CCD fields");
        std::vector<PascalClockEdit> alternatives,undo;
        auto projected=offline;
        for(const auto& field:inventory.timing_fields) {
            if(!pascal_timing_field(field.domain)->writable)continue;
            const int value=field.current==field.maximum?field.current-1:field.current+1;
            alternatives.push_back({field.domain,0,field.current,value,offline});
            projected=with_pascal_timing(projected,field.domain,value);
        }
        for(const auto& edit:alternatives)undo.push_back({edit.domain,0,edit.value,edit.expected,projected});
        const auto batch=plan_pascal_clocks(gp,alternatives);
        check(alternatives.size()==20&&batch.ok&&plan_pascal_clocks(batch.image,undo).image==gp,
              "GP107: all twenty offline-qualified writable fields restore the exact saved image");
        const PascalTimingSample ampere{9501,0x79e9839e,0x2300294c};
        PascalClockTable unsupported;
        inspect_pascal_tfaw(unsupported,ga,7,ampere);
        check(!ga.empty() && !plan_pascal_clocks(ga,{{Domain::tfaw,0,20,21,ampere}},7).ok && !unsupported.timing_sample &&
              unsupported.timing_message.find("only on Pascal")!=std::string::npos,
              "GA104: no BIOS tFAW write path");
        if(!plan.ok)std::fprintf(stderr,"GP107 parser: %s\n",plan.message.c_str());
    }
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
