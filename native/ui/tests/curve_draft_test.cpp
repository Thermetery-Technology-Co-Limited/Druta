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
static void pascal_p0_curve_estimate() {
    const std::vector<dc_vf_point> curve{
        {64, 1131.25, 1885.5, 0},
        {61, 1056.251, 1847.5, 25000},
        {62, 1056.25, 1835, 12500},
        {60, 1050, 1835, 0},
        {59, 1043.75, 1800, 0},
    };
    const auto p0 = pascal_p0_curve_peak(curve, 2, 1056.25);
    CHECK(p0 && p0->peak_mhz == 1835 && p0->park_idx == 60 && p0->park_mv == 1050 && p0->at_peak == 2);
    const auto lower_cap = pascal_p0_curve_peak(curve, 2, 1049.999);
    CHECK(lower_cap && lower_cap->peak_mhz == 1800 && lower_cap->park_idx == 59);
    const auto staged = pascal_p0_curve_peak(curve, 2, 1056.251, -12.5);
    CHECK(staged && staged->peak_mhz == 1835 && staged->park_idx == 61);
    CHECK(!pascal_p0_curve_peak(curve, 2, 1043.749));
    CHECK(!pascal_p0_curve_peak(curve, 2, std::nullopt));
    CHECK(!pascal_p0_curve_peak(curve, 2, 0));
    CHECK(!pascal_p0_curve_peak(curve, 0, 1056.25));
    CHECK(!pascal_p0_curve_peak(curve, 1, 1056.25));
    auto malformed = curve;
    malformed[0].volt_mv = std::numeric_limits<double>::quiet_NaN();
    CHECK(!pascal_p0_curve_peak(malformed, 2, 1056.25));
    malformed = curve;
    malformed[0].idx = malformed[1].idx;
    CHECK(!pascal_p0_curve_peak(malformed, 2, 1056.25));
}
int main() {
    pascal_p0_curve_estimate();
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

    // 1.9.1 vf_revert and push_undo labels: Revert is one undo step, needs no
    // fresh read, and undo/redo report the step they moved.
    CurveDraft classic;
    classic.observe(7, points, true, 1);
    CHECK(classic.stage_khz(9, 11000, "nudge idx 9 +1 MHz") && classic.stage_khz(2, -1000, "set idx 2 to 1601 MHz"));
    classic.observe(7, points, false, 1); // the curve went stale: Revert still works
    CHECK(classic.revert_undoable() && !classic.dirty() && classic.undo_depth() == 3 && classic.undo_label() == "revert edits");
    CHECK(classic.undo_edit() && classic.redo_label() == "revert edits" && classic.undo_depth() == 2);
    CHECK(classic.changes().size() == 2 && classic.draft()[0].delta_khz == 11000 && classic.draft()[1].delta_khz == -1000);
    CHECK(classic.undo_edit() && classic.redo_label() == "set idx 2 to 1601 MHz" && classic.undo_label() == "nudge idx 9 +1 MHz");
    CHECK(classic.redo_edit() && classic.undo_label() == "set idx 2 to 1601 MHz" && classic.redo_depth() == 1);
    CHECK(classic.revert_undoable() && classic.revert_undoable() && classic.undo_depth() == 3); // a clean draft records nothing
    // A fresh read that finds the same curve keeps the history only when asked to.
    classic.observe(7, points, true, 1);
    CHECK(!classic.can_undo());
    CHECK(classic.stage_khz(9, 11000, "nudge idx 9 +1 MHz") && classic.revert_undoable());
    classic.keep_history_across_reads(true);
    classic.observe(7, points, true, 1);
    CHECK(classic.undo_depth() == 2 && classic.undo_edit() && classic.dirty());
    CHECK(classic.revert_undoable());
    classic.observe(7, changed, true, 1); // a changed curve under a clean draft still drops it
    CHECK(!classic.can_undo() && classic.baseline()[0].volt_mv == 851);
    // A draft whose hardware changed underneath is rebased, as revert() is.
    CHECK(classic.stage_khz(9, 5));
    classic.observe(7, points, true, 1);
    CHECK(!classic.can_apply() && classic.revert_undoable() && !classic.dirty() && !classic.can_undo()
        && classic.baseline()[0].volt_mv == 850);
    classic.begin_gesture("drag idx 9");
    CHECK(classic.stage_khz(9, 20000) && classic.stage_khz(9, 30000));
    classic.end_gesture();
    CHECK(classic.undo_depth() == 1 && classic.undo_label() == "drag idx 9");

    // Released 1.9.1 statistics line and plan banner, from arbitrary points.
    const std::vector<dc_vf_point> shape{point(5, 950, 1700, 0), point(0, 700, 1500, 0), point(2, 800, 1600, 0),
        point(7, 1100, 1800, 0), point(1, 750, 1600, 0), point(4, 900, 1700, 0), point(3, 850, 1700, 0), point(6, 1000, 1650, 0)};
    const auto peak = curve_peak(shape, 1000);
    CHECK(peak.peak_mhz == 1700 && peak.at_peak == 3 && peak.park_idx && *peak.park_idx == 3 && peak.park_mv == 850);
    CHECK(curve_flat_runs(shape, 1000) == 2);             // idx 1-2 and idx 3-6, walked in index order
    CHECK(curve_flat_runs(shape, 790) == 0);              // runs above the cap do not count
    CHECK(curve_below_cap(1000.005, 1000) && !curve_below_cap(1000.02, 1000));
    CHECK(!curve_peak(shape, 600).park_idx && curve_peak(shape, 600).at_peak == 0);
    const std::string idle_info = curve_info_text(shape, shape, 1000);
    CHECK(idle_info == "8 points   top \xE2\x89\xA4" "cap: idx 6 @ 1000.00 mV = 1650 MHz   peak 1700 MHz held by 3 point(s), "
        "lowest = idx 3 @ 850.00 mV (where the card parks)   flat runs below cap: 2");
    CHECK(curve_info_text(shape, shape, 600).find("top") == std::string::npos
        && curve_info_text(shape, shape, 600).find("lowest = idx -- @ 0.00 mV") != std::string::npos);
    CHECK(curve_info_text(shape, {}, 1000).empty());
    auto edited = shape;
    edited[7].delta_khz = 30000; edited[7].freq_mhz = 1680; // idx 6 raised by 30 MHz
    CHECK(curve_pending_edits(shape, edited) == 1);
    CHECK(curve_info_text(shape, edited, 1000).find("top \xE2\x89\xA4" "cap: idx 6 @ 1000.00 mV = 1680 MHz") != std::string::npos);
    CHECK(curve_info_text(shape, edited, 1000).find("   [staged curve, 1 edit(s) not yet written]") != std::string::npos);

    CurvePlanBannerInput input;
    input.cap_mv = 1000;
    auto banner = curve_plan_banner(shape, shape, input);
    CHECK(banner.band == CurvePlanBand::idle && banner.body == "Nothing staged" && banner.warning.empty() && banner.point_detail.empty());
    banner = curve_plan_banner(shape, edited, input);
    CHECK(banner.band == CurvePlanBand::warn && banner.warning.empty());
    // As 1.9.1 apply_plan: "top" here is the fastest point under the cap, the
    // peak is the whole table's, and the held count is the capped peak's.
    CHECK(banner.body == "Writes 1 edited point(s): top \xE2\x89\xA4" "1000 mV becomes 1700 MHz, peak 1800 MHz held by 3 point(s), "
        "the card parks at idx 3 @ 850.00 mV.");
    CHECK(banner.point_detail == "Point 6 at 1000.00 mV: 1650 to 1680 MHz (offset +30 MHz)");
    auto lowered = shape;
    lowered[3].delta_khz = -50000; lowered[3].freq_mhz = 1750; // idx 7, above the cap, holds the table peak
    banner = curve_plan_banner(shape, lowered, input);
    CHECK(banner.band == CurvePlanBand::bad && banner.warning.empty());
    CHECK(banner.body.rfind("WARNING: this LOWERS the curve's peak from 1800 to 1750 MHz. Writes 1 edited point(s)", 0) == 0);
    input.note = "Hard plan note"; input.note_hard = true;
    banner = curve_plan_banner(shape, edited, input);
    CHECK(banner.band == CurvePlanBand::bad && banner.warning == "HARD DE-FLATTEN - WITHOUT THE VOLTAGE MOD THIS WILL CRASH");
    CHECK(banner.body.find("mV.\nHard plan note") != std::string::npos);
    banner = curve_plan_banner(shape, lowered, input);
    CHECK(banner.band == CurvePlanBand::bad && !banner.warning.empty()
        && banner.body.find("LOWERS the curve's peak") != std::string::npos && banner.body.find("Hard plan note") != std::string::npos);

    // Index box: exact point, else the next existing index the way the value moved.
    const std::vector<dc_vf_point> sparse{point(9, 800, 1500, 0), point(2, 750, 1400, 0), point(20, 900, 1600, 0), point(4, 760, 1450, 0)};
    CHECK(curve_nearest_index(sparse, 4, 9) == 4u);
    CHECK(curve_nearest_index(sparse, 10, 9) == 20u && curve_nearest_index(sparse, 8, 9) == 4u);
    CHECK(curve_nearest_index(sparse, 21, 20) == 20u && curve_nearest_index(sparse, -1, 2) == 2u);
    CHECK(curve_nearest_index(sparse, 15, 9) == 20u && curve_nearest_index(sparse, 3, 9) == 2u);
    CHECK(!curve_nearest_index({}, 3, 0));

    // Set-MHz entry clamps to the adapter's reported range; an unreported bound clamps nothing.
    CHECK(curve_clamp_entry_mhz(150, 405u, 1995u) == 405 && curve_clamp_entry_mhz(2500, 405u, 1995u) == 1995);
    CHECK(curve_clamp_entry_mhz(1200, 405u, 1995u) == 1200);
    CHECK(curve_clamp_entry_mhz(2500, std::nullopt, std::nullopt) == 2500 && curve_clamp_entry_mhz(5, std::nullopt, 1995u) == 5);
    CHECK(curve_clamp_entry_mhz(2500, 405u, std::nullopt) == 2500 && curve_clamp_entry_mhz(150, 405u, std::nullopt) == 405);
    CHECK(curve_clamp_entry_mhz(150, 2000u, 1000u) == 150); // inconsistent bounds are not applied

    // 1.9.1 voltage boxes snap DOWN onto the 6.25 mV grid and clamp; on-grid values stay.
    CHECK(curve_snap_mv(1100, curve_cap_min_mv, curve_cap_max_mv) == 1100);
    CHECK(curve_snap_mv(1099.99, curve_cap_min_mv, curve_cap_max_mv) == 1093.75);
    CHECK(curve_snap_mv(1093.75 - 1e-10, curve_cap_min_mv, curve_cap_max_mv) == 1093.75 - 1e-10); // float slop is not a step down
    CHECK(curve_snap_mv(1500, curve_cap_min_mv, curve_cap_max_mv) == 1300 && curve_snap_mv(10, curve_cap_min_mv, curve_cap_max_mv) == 600);
    CHECK(curve_snap_mv(std::numeric_limits<double>::quiet_NaN(), 750, 900) == 750);
    CHECK(curve_snap_mv(800, 750, 900) == 800 && curve_snap_mv(950, 750, 900) == 900);
    CHECK(std::fmod(curve_cap_default_mv, curve_voltage_grid_mv) == 0);
    // Hard target: whole-MHz grid from the adapter's step, floored.
    CHECK(curve_snap_target_mhz(1897, 15000) == 1890 && curve_snap_target_mhz(1890, 15000) == 1890);
    CHECK(curve_snap_target_mhz(1900, 12657) == 1898 && curve_snap_target_mhz(1901.7, 0) == 1901);

    // vf_read seed: the middle point of the read order, floors clamped to the
    // curve's own voltage range, target from the uncapped table peak.
    auto seed = curve_editor_seed(shape, 1000, 800);
    CHECK(seed.middle_idx == 1u); // shape[4]: read order, not index order
    CHECK(seed.ramp_floor_mv == 1000.0 && seed.hard_floor_mv == 800.0 && seed.hard_target_mhz == 1800.0);
    seed = curve_editor_seed(sparse, 1000, 300);
    CHECK(seed.middle_idx == 20u && seed.ramp_floor_mv == 900.0 && seed.hard_floor_mv == 750.0 && seed.hard_target_mhz == 1600.0);
    seed = curve_editor_seed({}, 1000, 800);
    CHECK(!seed.middle_idx && !seed.ramp_floor_mv && !seed.hard_floor_mv && !seed.hard_target_mhz);

    // vf_read log line from the same peak/park/flat walk as the statistics line.
    CHECK(curve_read_summary(shape, 1000) == "curve read: peak 1700 MHz, parks idx 3 @ 850.00 mV, 2 flat run(s) below 1000 mV");
    CHECK(curve_read_summary(shape, 1093.75) == "curve read: peak 1700 MHz, parks idx 3 @ 850.00 mV, 2 flat run(s) below 1094 mV");
    CHECK(curve_read_summary(shape, 600) == "curve read: peak 0 MHz, parks idx -- @ 0.00 mV, 0 flat run(s) below 600 mV");

    // Plot budget: int(H*0.34) + idle banner, minus what the banner and open blocks take, 240 floor.
    CHECK(curve_plot_budget(1261, 104, 104, 0, 240) == 428);
    CHECK(curve_plot_budget(1261, 104, 160, 90, 240) == 282);
    CHECK(curve_plot_budget(700, 104, 200, 0, 240) == 240);
    // The shares are of the page height under the menu bar and pinned chrome:
    // 1.9.1's own tree at 1528x1261 (menu 24, tab selector 28, unlock strip 28)
    // records a 386 px plot under a 119 px banner and a 153 px log.
    const float share = curve_share_height(1261, 24, 28 + 28);
    CHECK(share == 1181);
    CHECK(curve_plot_budget(share, 104, 119, 0, 240) == 386);
    CHECK(curve_log_height(share, 90) == 153);
    CHECK(curve_log_height(curve_share_height(1261, 0, 0), 90) == 163); // the raw client height overshoots
    CHECK(curve_log_height(curve_share_height(600, 24, 56), 90) == 90 && curve_share_height(40, 24, 56) == 0);

    // 1.9.1 update_vf_corner: three-space gaps, 2-decimal mV, whole MHz, a unitless
    // signed delta in physical MHz (raw delta over the request divisor).
    CHECK(curve_corner_text({63, 845, 1710, 0}, 1) == "idx 63   845.00 mV   1710 MHz   delta +0");
    CHECK(curve_corner_text({7, 812.5, 1695, -30000}, 2) == "idx 7   812.50 mV   1695 MHz   delta -15");
    CHECK(curve_corner_text({7, 812.5, 1695, -30000}, 0) == "idx 7   812.50 mV   1695 MHz   delta unverified");
    // 1.9.1's key help, with the amounts one W/S press and a Shift press stage on this grid.
    CHECK(curve_key_help(15000) == "drag a dot to move it  \xE2\x80\xA2  drag anywhere else to pan  \xE2\x80\xA2  A/D select  \xE2\x80\xA2  "
        "W/S move \xC2\xB1" "15 MHz  \xE2\x80\xA2  hold Shift for \xC2\xB1" "45  \xE2\x80\xA2  "
        "Ctrl+H hold the selected point (again to release)  \xE2\x80\xA2  Ctrl+Z / Ctrl+Y undo and redo staged edits");
    CHECK(curve_key_help(12657).find("W/S move \xC2\xB1" "12.657 MHz  \xE2\x80\xA2  hold Shift for \xC2\xB1" "37.971  ") != std::string::npos);
    CHECK(curve_key_help(0).find("W/S move -- MHz  \xE2\x80\xA2  hold Shift for --  ") != std::string::npos);
    std::printf("curve_draft_test: %u checks passed\n", checks);
}
