/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_planner.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <windows.h>
using namespace druta::ui;
static unsigned checks = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #expr); std::abort(); } } while (false)
static int scenario = 0, calls = 0; static int64_t rephase_step = 0;
static dc_status DC_CALL deflatten(const dc_vf_point* p, size_t n, const dc_vfp_num* cap,
                                   const dc_vfp_num*, const dc_vfp_num*, const dc_vfp_num* step, dc_vfp_deflatten* out) {
    ++calls; CHECK(n == 2 && cap && cap->f == 1000 && step->i == 12500);
    if (scenario == 1) return DC_E_FAILED;
    out->change_count = scenario == 2 ? 2 : 1;
    out->changes[0].pos = 1; out->changes[0].idx = p[1].idx;
    out->changes[0].delta_khz.kind = DC_VFP_INT; out->changes[0].delta_khz.i = p[1].delta_khz + 12500;
    if (scenario == 2) out->changes[1] = out->changes[0];
    if (scenario == 3) out->changes[0].delta_khz.i = 1000000;
    if (scenario == 4) { out->changes[0].delta_khz.kind = DC_VFP_FLOAT; out->changes[0].delta_khz.f = p[1].delta_khz + 0.25; }
    return DC_OK;
}
static dc_status DC_CALL ramp(const dc_vf_point* p, size_t n, const dc_vfp_num* low, const dc_vfp_num* cap,
                              const dc_vfp_num*, const dc_vfp_num* step, dc_vfp_ramp* out) {
    ++calls; CHECK(n == 2 && low->f == 800 && cap->f == 1000 && step->i == 12500);
    out->change_count = 1; out->changes[0].pos = 0; out->changes[0].idx = p[0].idx;
    out->changes[0].delta_khz.kind = DC_VFP_INT; out->changes[0].delta_khz.i = p[0].delta_khz - 12500;
    if (scenario == 7) { out->has_band = 1; out->rungs = 2; out->delivered = 2; out->lo_mv = 800; out->cap_mv = 1000;
        out->top_mhz = 1600; out->floor_after_mhz = 1500; }
    return DC_OK;
}
static dc_status DC_CALL rephase(const dc_vfp_num* raw, size_t n, const dc_vfp_num* step, dc_vfp_rephase* out) {
    ++calls; CHECK(n == 2 && raw[0].i == 1234 && raw[1].i == -4321); rephase_step = step->i;
    out->change_count = 1; out->changes[0].pos = 0; out->changes[0].delta_khz = 1000; return DC_OK;
}
static dc_status DC_CALL hard(const dc_vf_point* p, size_t n, const dc_vfp_num* floor,
    const dc_vfp_num* target, const dc_vfp_num* step, dc_vfp_hard* out) {
    ++calls; CHECK(n == 2 && floor->f == 900 && target->f == 1700000 && step->i == 12500);
    if (scenario == 1) return DC_E_FAILED;
    out->change_count = 1; out->changes[0].pos = scenario == 2 ? 0 : 1;
    out->changes[0].idx = p[out->changes[0].pos].idx;
    out->changes[0].delta_khz.kind = DC_VFP_INT;
    out->changes[0].delta_khz.i = scenario == 3 ? 1000000 : p[out->changes[0].pos].delta_khz + 12500;
    return DC_OK;
}
static dc_status DC_CALL predict(const dc_vf_point*, size_t n, const dc_vfp_num* khz, const dc_vfp_num*,
                                 int64_t* real, uint32_t* order, size_t capacity) {
    CHECK(n == 2 && capacity == n);
    if (scenario == 5) return DC_E_FAILED;
    order[0] = 1; order[1] = scenario == 6 ? 1 : 0;
    real[0] = khz[1].i; real[1] = khz[0].i; return DC_OK;
}
int wmain(int argc, wchar_t** argv) {
    PlannerApi api{deflatten, ramp, rephase, predict, hard};
    CurvePlanRequest req; req.points = {{9, 850, 1500, 1234}, {2, 900, 1600, -4321}};
    req.freq_div = 2; req.step_khz = 12500; req.max_abs_delta_khz = 100000;
    req.cap_mv = 1000; req.floor_mv = 800; req.max_rise_khz = 37500;
    auto plan = plan_curve(api, req);
    CHECK(plan.ok && plan.changes.size() == 1 && plan.changes[0].idx == 2 && plan.changes[0].delta_khz == 20679);
    CHECK(plan.points[0].before_delta_khz == -4321 && plan.points[0].after_mhz == 1612.5);
    CHECK(plan.predicted_khz[0] == 1500000 && plan.predicted_khz[1] == 1612500);
    CHECK(req.points[1].delta_khz == -4321); // input never mutated
    for (scenario = 1; scenario <= 6; ++scenario) {
        const auto result = plan_curve(api, req); CHECK(!result.ok && result.changes.empty() && result.points.empty() && result.predicted_khz.empty());
    }
    scenario = 0; req.kind = CurvePlanKind::ramp; plan = plan_curve(api, req);
    CHECK(plan.ok && plan.changes[0].idx == 9 && plan.changes[0].delta_khz == -23766);
    req.kind = CurvePlanKind::rephase; plan = plan_curve(api, req);
    CHECK(plan.ok && rephase_step == 25000 && plan.changes[0].delta_khz == 1000);
    req.freq_div = 1; plan = plan_curve(api, req); CHECK(plan.ok && rephase_step == 12500);
    req.freq_div = 2; req.kind = CurvePlanKind::hard_deflatten; req.floor_mv = 900; req.cap_mv.reset(); req.target_mhz = 1700;
    plan = plan_curve(api, req);
    CHECK(plan.ok && plan.changes.size() == 1 && plan.changes[0].idx == 2 && plan.changes[0].delta_khz == 20679);
    CHECK(req.points[0].delta_khz == 1234 && req.points[1].delta_khz == -4321);
    for (scenario = 1; scenario <= 3; ++scenario) CHECK(!plan_curve(api, req).ok);
    scenario = 0; req.target_mhz = 0; CHECK(!plan_curve(api, req).ok);
    req.target_mhz = 1700; api.hard_deflatten = nullptr; CHECK(!plan_curve(api, req).ok); api.hard_deflatten = hard;
    req.kind = CurvePlanKind::rephase; req.cap_mv = 1000; req.floor_mv = 800; req.target_mhz.reset();
    const auto count = calls; req.freq_div = 0; CHECK(!plan_curve(api, req).ok && calls == count);
    req.freq_div = 2; req.points[1].idx = 9; CHECK(!plan_curve(api, req).ok && calls == count);
    req.points[1].idx = 2; req.points[0].volt_mv = std::numeric_limits<double>::quiet_NaN(); CHECK(!plan_curve(api, req).ok);
    req.points[0].volt_mv = 850; req.max_abs_delta_khz = 0; CHECK(!plan_curve(api, req).ok);
    req.max_abs_delta_khz = 100000; req.step_khz = 0; CHECK(!plan_curve(api, req).ok);
    req.step_khz = 12500; req.floor_mv = 1100; CHECK(!plan_curve(api, req).ok);
    req.floor_mv = 800; req.max_rise_khz.reset(); CHECK(!plan_curve(api, req).ok);
    // Below-floor estimate: the fake model returns each point's own request, so
    // only a raised point under the floor counts; at or above it never does.
    {
        scenario = 0;
        const std::vector<dc_vf_point> before{{9, 850, 1500, 0}, {2, 900, 1600, 0}};
        auto after = before; after[0].freq_mhz = 1530; after[1].freq_mhz = 1700;
        auto estimate = estimate_below_floor(api, before, after, 900, 37500);
        CHECK(estimate.available && estimate.raised == 1 && estimate.lowest_mv == 850 && estimate.largest_rise_mhz == 30);
        estimate = estimate_below_floor(api, before, after, 850, 37500);
        CHECK(estimate.available && estimate.raised == 0);
        after[0].freq_mhz = 1480;
        CHECK(estimate_below_floor(api, before, after, 900, 37500).raised == 0); // lowered is not raised
        CHECK(!estimate_below_floor(api, before, after, 900, 0).available);        // no model bound
        scenario = 5; CHECK(!estimate_below_floor(api, before, after, 900, 37500).available); // model failure
        scenario = 6; CHECK(!estimate_below_floor(api, before, after, 900, 37500).available); // duplicate order
        scenario = 0;
        auto missing = api; missing.predict = nullptr;
        CHECK(!estimate_below_floor(missing, before, after, 900, 37500).available);
    }
    // Released 1.9.1 plan notes, from the core planner's metadata (arbitrary values).
    {
        dc_vfp_ramp m{}; m.has_band = 1; m.rungs = 12; m.delivered = 9; m.lo_mv = 812.5; m.cap_mv = 1050; m.top_mhz = 1965;
        m.floor_after_mhz = 1605;
        auto note = curve_ramp_note(m, 37500);
        CHECK(note == "RAMP - 12 rungs \xE2\x86\x92 9 distinct operating points, 812.50 \xE2\x86\x92 1050.00 mV, top 1965 MHz. "
            "The floor keeps its 1605 MHz at 812.50 mV. Every rung asks more clock at its voltage than stock did - the "
            "granularity fix and the overclock are one edit, so each rung has to be stable.");
        m.floor_cost_mhz = 30; m.floor_before_mhz = 1635; m.shadowed = 2; m.has_under_band = 1; m.under_band_mhz = 1620;
        m.lifted_below = 3; m.lift_max_mhz = 22.4; m.dropped_rungs = 4; std::snprintf(m.dropped_reason, sizeof(m.dropped_reason), "out of headroom");
        note = curve_ramp_note(m, 37500);
        CHECK(note.find("top 1965 MHz\n4 lower rung(s) dropped - out of headroom. They keep their stock values") != std::string::npos);
        CHECK(note.find(". Top clipped at the 1965 MHz hw max, so the floor pays: 812.50 mV drops 1635 \xE2\x86\x92 1605 MHz (-30)"
            " - below the untouched point under the band (1620 MHz), so the driver raises the bottom 2 rung(s) onto it") != std::string::npos);
        CHECK(note.find(". 3 point(s) BELOW the floor get dragged up too (max +22 MHz): the driver allows at most 37.5 MHz between") != std::string::npos);
        m.has_under_band = 0;
        CHECK(curve_ramp_note(m, 37500).find("under the band (unknown MHz)") != std::string::npos);
        m.has_band = 0; CHECK(curve_ramp_note(m, 37500).empty());

        dc_vfp_hard h{}; h.has_band = 1; h.n_flat = 14; h.floor_mv = 825; h.target_mhz = 1890; h.park_idx = 61; h.park_mv = 825;
        h.park_mhz = 1890; h.parks_at_floor = 1;
        note = curve_hard_note(h, 37500);
        CHECK(note == "HARD DE-FLATTEN - 14 point(s) at or above 825.00 mV flattened onto ONE frequency, 1890 MHz, so the card "
            "parks at idx 61 @ 825.00 mV / 1890 MHz. NEEDS THE EXTERNAL VOLTAGE MOD: the GPU will believe it is at 825.00 mV "
            "and compute low power from that belief, which is the entire point - without the mod it really IS at 825.00 mV "
            "and this crashes it.");
        h.parks_at_floor = 0; h.park_mv = 800; h.lifted_below = 5; h.has_lift_lowest = 1; h.lift_lowest_mv = 712.5; h.lift_max_mhz = 140;
        note = curve_hard_note(h, 37500);
        CHECK(note.find(", NOT at the 825.00 mV floor - the target is too low") != std::string::npos);
        CHECK(note.find(" The driver then drags 5 point(s) BELOW the floor up with it, as far down as 712.50 mV, worst case "
            "+140 MHz - no delta is written to any of them (37.5 MHz max step, shape law)") != std::string::npos);
        h.has_band = 0; CHECK(curve_hard_note(h, 37500).find("NEEDS THE EXTERNAL VOLTAGE MOD") != std::string::npos);

        const std::string sticky = " A HARD DE-FLATTEN IS STILL STAGED UNDERNEATH THIS and Apply writes both: 'Revert edits' is what drops it.";
        CHECK(curve_stage_note("RAMP - x.", false, true) == "RAMP - x." + sticky);
        CHECK(curve_stage_note("RAMP - x.", false, false) == "RAMP - x.");
        CHECK(curve_stage_note("HARD - y.", true, true) == "HARD - y."); // a new hard plan names itself
    }
    // plan_curve hands the ramp note through only with staged changes.
    {
        scenario = 0; req = {}; req.points = {{9, 850, 1500, 1234}, {2, 900, 1600, -4321}};
        req.freq_div = 2; req.step_khz = 12500; req.max_abs_delta_khz = 100000; req.cap_mv = 1000; req.floor_mv = 800;
        req.max_rise_khz = 37500; req.kind = CurvePlanKind::ramp;
        CHECK(plan_curve(api, req).note.empty()); // the fake reports no band
        scenario = 7; const auto ramp_plan = plan_curve(api, req);
        CHECK(ramp_plan.ok && ramp_plan.note.rfind("RAMP - 2 rungs \xE2\x86\x92" " 2 distinct operating points, 800.00", 0) == 0
            && ramp_plan.note.find("allows at most") == std::string::npos);
        req.kind = CurvePlanKind::rephase; CHECK(plan_curve(api, req).note.empty());
        scenario = 0;
    }
    if (argc > 1) {
        HMODULE module = LoadLibraryExW(argv[1], nullptr, LOAD_WITH_ALTERED_SEARCH_PATH); CHECK(module);
        PlannerApi actual{};
        actual.deflatten = reinterpret_cast<decltype(actual.deflatten)>(GetProcAddress(module, "dc_vfp_compute_deflatten"));
        actual.ramp = reinterpret_cast<decltype(actual.ramp)>(GetProcAddress(module, "dc_vfp_compute_ramp"));
        actual.rephase = reinterpret_cast<decltype(actual.rephase)>(GetProcAddress(module, "dc_vfp_compute_rephase"));
        actual.predict = reinterpret_cast<decltype(actual.predict)>(GetProcAddress(module, "dc_vfp_predict_curve"));
        actual.hard_deflatten = reinterpret_cast<decltype(actual.hard_deflatten)>(GetProcAddress(module, "dc_vfp_compute_hard_deflatten"));
        CHECK(actual.deflatten && actual.ramp && actual.rephase && actual.predict && actual.hard_deflatten);
        CurvePlanRequest real; real.points = {{7, 800, 1000, 1234}, {4, 900, 1100, -4321}, {9, 1000, 1100, 2345}};
        real.freq_div = 2; real.step_khz = 12500; real.max_abs_delta_khz = 1000000;
        real.cap_mv = 900; real.floor_mv = 800; real.max_rise_khz = 37500;
        for (auto kind : {CurvePlanKind::deflatten, CurvePlanKind::ramp, CurvePlanKind::rephase}) {
            real.kind = kind; auto result = plan_curve(actual, real); CHECK(result.ok && result.predicted_khz.size() == real.points.size());
        }
        // Captured 1000–1100 mV band: an inferred fractional grid must not
        // silently change Max it's released fallback ramp policy.
        CurvePlanRequest band; band.kind = CurvePlanKind::ramp; band.freq_div = 2;
        band.step_khz = 12656; band.max_abs_delta_khz = 1000000; band.max_rise_khz = 37968;
        band.floor_mv = 1000; band.cap_mv = 1100;
        band.points = {{55,1000,1784.5,0},{56,1012.5,1797,0},{57,1025,1809.5,0},
            {58,1031.25,1822.5,0},{59,1043.75,1835,0},{60,1050,1847.5,0},
            {61,1062.5,1847.5,0},{62,1075,1860,0},{63,1081.25,1873,0},
            {64,1093.75,1873,0},{65,1100,1885.5,0}};
        const auto measured = plan_curve(actual, band); CHECK(measured.ok && measured.changes.size() == 4);
        CHECK(measured.changes.back().idx == 65 && measured.changes.back().delta_khz == 25312);
        band.ramp_use_default_step = true;
        const auto inferred = plan_curve(actual, band); CHECK(inferred.ok && inferred.changes.size() == 5);
        CHECK(inferred.changes.back().idx == 65 && inferred.changes.back().delta_khz == 60000);
        real.kind = CurvePlanKind::hard_deflatten; real.floor_mv = 900; real.cap_mv.reset(); real.target_mhz = 1124;
        const auto hard_plan = plan_curve(actual, real); CHECK(hard_plan.ok && hard_plan.predicted_khz.size() == real.points.size());
        for (const auto& change : hard_plan.changes) CHECK(change.idx != 7); // below-floor point retained
        FreeLibrary(module);
    }
    std::printf("curve_planner_test: %u checks passed\n", checks);
}
