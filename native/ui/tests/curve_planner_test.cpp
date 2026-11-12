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
    return DC_OK;
}
static dc_status DC_CALL rephase(const dc_vfp_num* raw, size_t n, const dc_vfp_num* step, dc_vfp_rephase* out) {
    ++calls; CHECK(n == 2 && raw[0].i == 1234 && raw[1].i == -4321); rephase_step = step->i;
    out->change_count = 1; out->changes[0].pos = 0; out->changes[0].delta_khz = 1000; return DC_OK;
}
static dc_status DC_CALL predict(const dc_vf_point*, size_t n, const dc_vfp_num* khz, const dc_vfp_num*,
                                 int64_t* real, uint32_t* order, size_t capacity) {
    CHECK(n == 2 && capacity == n);
    if (scenario == 5) return DC_E_FAILED;
    order[0] = 1; order[1] = scenario == 6 ? 1 : 0;
    real[0] = khz[1].i; real[1] = khz[0].i; return DC_OK;
}
int wmain(int argc, wchar_t** argv) {
    PlannerApi api{deflatten, ramp, rephase, predict};
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
    const auto count = calls; req.freq_div = 0; CHECK(!plan_curve(api, req).ok && calls == count);
    req.freq_div = 2; req.points[1].idx = 9; CHECK(!plan_curve(api, req).ok && calls == count);
    req.points[1].idx = 2; req.points[0].volt_mv = std::numeric_limits<double>::quiet_NaN(); CHECK(!plan_curve(api, req).ok);
    req.points[0].volt_mv = 850; req.max_abs_delta_khz = 0; CHECK(!plan_curve(api, req).ok);
    req.max_abs_delta_khz = 100000; req.step_khz = 0; CHECK(!plan_curve(api, req).ok);
    req.step_khz = 12500; req.floor_mv = 1100; CHECK(!plan_curve(api, req).ok);
    req.floor_mv = 800; req.max_rise_khz.reset(); CHECK(!plan_curve(api, req).ok);
    if (argc > 1) {
        HMODULE module = LoadLibraryExW(argv[1], nullptr, LOAD_WITH_ALTERED_SEARCH_PATH); CHECK(module);
        PlannerApi actual{};
        actual.deflatten = reinterpret_cast<decltype(actual.deflatten)>(GetProcAddress(module, "dc_vfp_compute_deflatten"));
        actual.ramp = reinterpret_cast<decltype(actual.ramp)>(GetProcAddress(module, "dc_vfp_compute_ramp"));
        actual.rephase = reinterpret_cast<decltype(actual.rephase)>(GetProcAddress(module, "dc_vfp_compute_rephase"));
        actual.predict = reinterpret_cast<decltype(actual.predict)>(GetProcAddress(module, "dc_vfp_predict_curve"));
        CHECK(actual.deflatten && actual.ramp && actual.rephase && actual.predict);
        CurvePlanRequest real; real.points = {{7, 800, 1000, 1234}, {4, 900, 1100, -4321}, {9, 1000, 1100, 2345}};
        real.freq_div = 2; real.step_khz = 12500; real.max_abs_delta_khz = 1000000;
        real.cap_mv = 900; real.floor_mv = 800; real.max_rise_khz = 37500;
        for (auto kind : {CurvePlanKind::deflatten, CurvePlanKind::ramp, CurvePlanKind::rephase}) {
            real.kind = kind; auto result = plan_curve(actual, real); CHECK(result.ok && result.predicted_khz.size() == real.points.size());
        }
        FreeLibrary(module);
    }
    std::printf("curve_planner_test: %u checks passed\n", checks);
}
