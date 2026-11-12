/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_draft.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <vector>
using namespace druta::ui;
static unsigned checks = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #expr); std::abort(); } } while (false)
static dc_vf_point point(uint32_t idx, double voltage, double base, int32_t delta) {
    return {idx, voltage, base + delta / 1000.0, delta};
}
int main() {
    const std::vector<dc_vf_point> points{point(9, 850, 1500, 10000), point(2, 900, 1600, -2000)};
    CurveDraft draft;
    draft.observe(1, points, true, 1);
    CHECK(!draft.dirty() && !draft.can_apply());
    CHECK(draft.stage_mhz(2, 12.5));
    CHECK(draft.dirty() && draft.can_apply());
    CHECK(draft.changes().size() == 1 && draft.changes()[0].idx == 2 && draft.changes()[0].delta_khz == 12500);
    CHECK(draft.draft()[1].freq_mhz == 1612.5);
    auto reordered = points; std::reverse(reordered.begin(), reordered.end());
    draft.observe(1, reordered, true, 1);
    CHECK(draft.can_apply() && draft.draft()[1].delta_khz == 12500);
    CHECK(draft.baseline()[1].idx == 2 && draft.baseline()[1].delta_khz == -2000);
    CHECK(!draft.stage_khz(3, 0));
    CHECK(!draft.stage_khz(2, 0.5));
    CHECK(!draft.stage_mhz(2, 0.0005));
    CHECK(!draft.stage_khz(2, std::numeric_limits<double>::infinity()));
    CHECK(!draft.stage_mhz(2, std::numeric_limits<double>::quiet_NaN()));
    CHECK(!draft.stage_khz(2, 2147483648.0));
    CHECK(!draft.stage_khz(2, -2147483649.0));
    CHECK(draft.changes()[0].delta_khz == 12500);
    CHECK(draft.stage_mhz(2, 1.001));
    CHECK(draft.changes()[0].delta_khz == 1001);
    CHECK(draft.undo_edit() && draft.changes()[0].delta_khz == 12500);
    CHECK(draft.undo_edit() && !draft.dirty());
    CHECK(!draft.undo_edit());
    CHECK(draft.stage_khz(9, -333));
    draft.observe(1, points, false, 1);
    CHECK(draft.dirty() && !draft.can_apply() && !draft.revert());
    draft.observe(1, points, true, 1);
    CHECK(draft.can_apply());
    auto changed = points; changed[0] = point(9, 850, 1500, 12000);
    draft.observe(1, changed, true, 1);
    CHECK(draft.dirty() && !draft.can_apply());
    CHECK(draft.draft()[0].delta_khz == -333 && draft.baseline()[0].delta_khz == 10000);
    draft.observe(1, points, true, 1); // a transient return to the old curve does not clear the conflict
    CHECK(!draft.can_apply());
    CHECK(draft.revert() && !draft.dirty() && draft.baseline()[0].delta_khz == 10000);
    CHECK(draft.stage_khz(9, 1));
    changed[0] = point(9, 851, 1500, 10000);
    draft.observe(1, changed, true, 1);
    CHECK(!draft.can_apply());
    CHECK(draft.revert() && draft.baseline()[0].volt_mv == 851);
    CHECK(draft.stage_khz(9, 2));
    changed[0] = point(9, 851, 1501, 10000);
    draft.observe(1, changed, true, 1);
    CHECK(!draft.can_apply());
    CHECK(draft.revert());
    auto duplicate = points; duplicate[1].idx = duplicate[0].idx;
    CHECK(draft.stage_khz(9, 3));
    draft.observe(1, duplicate, true, 1);
    CHECK(draft.dirty() && !draft.can_apply() && !draft.revert());
    auto nan = points; nan[0].freq_mhz = std::numeric_limits<double>::quiet_NaN();
    draft.observe(1, nan, true, 1); CHECK(!draft.can_apply());
    std::vector<dc_vf_point> too_many(DC_VFP_POINTS + 1, points[0]);
    draft.observe(1, too_many, true, 1); CHECK(!draft.can_apply());
    draft.observe(2, reordered, true, 1);
    CHECK(draft.generation() == 2 && !draft.dirty() && !draft.can_undo());
    CHECK(draft.baseline()[0].idx == 2);
    for (int i = 0; i < 80; ++i) CHECK(draft.stage_khz(2, i));
    unsigned undone = 0; while (draft.undo_edit()) ++undone;
    CHECK(undone == 64 && draft.draft()[0].delta_khz == 15);
    CHECK(draft.revert() && !draft.dirty() && !draft.can_undo());
    draft.observe(3, {}, false, 0);
    CHECK(draft.generation() == 3 && draft.baseline().empty() && !draft.can_apply());
    CHECK(!draft.stage_khz(2, 0));
    CurveDraft dual;
    auto dual_points = points;
    dual_points[0] = {9, 850, 1505, 10000};
    dual_points[1] = {2, 900, 1599, -2000};
    dual.observe(4, dual_points, true, 0);
    CHECK(dual.baseline().empty() && !dual.can_apply() && !dual.stage_mhz(9, 1));
    dual.observe(4, dual_points, true, 2);
    CHECK(dual.freq_div() == 2 && dual.stage_mhz(9, 7.5));
    CHECK(dual.changes()[0].delta_khz == 15000 && dual.draft()[0].freq_mhz == 1507.5);
    CHECK(dual.stage_mhz(9, 0.0005) && dual.changes()[0].delta_khz == 1);
    CHECK(!dual.stage_mhz(9, 0.00025));
    dual.observe(4, dual_points, true, 1);
    CHECK(!dual.can_apply() && dual.freq_div() == 2 && dual.draft()[0].delta_khz == 1);
    CHECK(dual.revert() && dual.freq_div() == 1 && !dual.dirty());
    CHECK(dual.stage_khz(9, 123));
    dual.observe(4, dual_points, true, 0);
    CHECK(dual.dirty() && !dual.can_apply() && !dual.revert());
    CurveDraft gestures;
    gestures.observe(5, dual_points, true, 2);
    CHECK(gestures.stage_frequency_mhz(9, 1511.3285, 12657, 50000));
    CHECK(gestures.draft()[0].delta_khz == 35314 && gestures.draft()[0].freq_mhz == 1517.657);
    CHECK(gestures.undo_edit() && gestures.draft()[0].delta_khz == 10000 && gestures.can_redo());
    CHECK(gestures.redo_edit() && gestures.draft()[0].delta_khz == 35314);
    CHECK(gestures.undo_edit());
    CHECK(gestures.stage_frequency_mhz(9, 1498.6715, 12657, 50000));
    CHECK(gestures.draft()[0].delta_khz == 10000); // Negative half-bin rounds upward too.
    CHECK(gestures.stage_frequency_mhz(9, 1540, 12657, 50000, true));
    CHECK(gestures.draft()[0].delta_khz == 35314); // Floor and the raw envelope both apply.
    CHECK(gestures.stage_frequency_mhz(9, -10000, 12657, 50000));
    CHECK(gestures.draft()[0].delta_khz == -40628);
    CHECK(!gestures.stage_frequency_mhz(9, 1500, 0, 50000));
    CHECK(!gestures.stage_frequency_mhz(9, 1500, 12657, 0));
    CHECK(gestures.revert());
    gestures.begin_gesture();
    CHECK(gestures.stage_frequency_mhz(9, 1520, 15000, 1000000));
    CHECK(gestures.stage_frequency_mhz(9, 1535, 15000, 1000000));
    CHECK(gestures.stage_frequency_mhz(2, 1629, 15000, 1000000));
    gestures.end_gesture();
    CHECK(gestures.draft()[0].delta_khz == 70000 && gestures.draft()[1].delta_khz == 58000);
    CHECK(gestures.undo_edit());
    CHECK(gestures.draft()[0].delta_khz == 10000 && gestures.draft()[1].delta_khz == -2000);
    CHECK(!gestures.can_undo() && gestures.can_redo());
    CHECK(gestures.redo_edit() && gestures.draft()[0].delta_khz == 70000 && gestures.draft()[1].delta_khz == 58000);
    CHECK(gestures.undo_edit());
    CHECK(gestures.stage_khz(9, 123) && !gestures.can_redo());
    gestures.observe(6, dual_points, true, 2);
    CHECK(!gestures.can_undo() && !gestures.can_redo());
    CHECK(gestures.stage_khz(9, 123));
    const auto submitted = gestures.draft();
    gestures.observe(6, submitted, true, 2);
    CHECK(gestures.dirty() && !gestures.can_apply());
    CHECK(gestures.accept_applied());
    CHECK(!gestures.dirty() && !gestures.can_undo());
    CHECK(gestures.stage_khz(9, 456));
    gestures.observe(6, submitted, true, 2);
    CHECK(!gestures.accept_applied() && gestures.dirty()); // further local edit retained
    gestures.observe(6, gestures.draft(), false, 2);
    CHECK(!gestures.accept_applied()); // no stale readback acceptance
    std::printf("curve_draft_test: %u checks passed\n", checks);
}
