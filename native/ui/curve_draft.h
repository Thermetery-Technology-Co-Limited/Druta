/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {
// A point's freq_mhz is evaluated (base frequency plus delta). Drafts never
// access the GPU. `fresh` means the caller actually read this full curve now.
// freq_div is the verified number of raw delta units per physical unit; zero is unknown.
// stage_khz takes raw table kHz; stage_mhz takes physical MHz.
class CurveDraft {
public:
    void observe(std::uint64_t generation, const std::vector<dc_vf_point>& points, bool fresh, std::uint32_t freq_div);
    // `label` names the undo step a staging edit records (1.9.1 push_undo).
    bool stage_khz(std::uint32_t index, double delta_khz, const std::string& label = {});
    bool stage_mhz(std::uint32_t index, double delta_mhz);
    // Absolute physical MHz on the selected point's captured clock phase.
    // Drag/nudges round half up; Set MHz floors. Bounds come from the C ABI.
    bool stage_frequency_mhz(std::uint32_t index, double target_mhz,
        std::uint32_t step_khz, std::int64_t max_abs_delta_khz, bool floor_target = false, const std::string& label = {});
    void begin_gesture(const std::string& label = {});
    void end_gesture();
    bool undo_edit();
    bool redo_edit();
    bool revert(); // rebase to the latest valid fresh observation
    // 1.9.1 vf_revert: the draft returns to its baseline as one undoable step
    // ("revert edits"), with no fresh read needed. A draft whose hardware curve
    // changed underneath is rebased instead, as revert() does.
    bool revert_undoable();
    // 1.9.1 clears its editor history only on vf_read. With this set, a fresh
    // read that finds an unchanged curve under a clean draft keeps the history.
    void keep_history_across_reads(bool keep) { keep_history_ = keep; }
    std::size_t undo_depth() const { return edits_.size(); }
    std::size_t redo_depth() const { return redo_.size(); }
    std::string undo_label() const { return edits_.empty() ? std::string() : edits_.back().label; }
    std::string redo_label() const { return redo_.empty() ? std::string() : redo_.back().label; }
    bool accept_applied(); // only after successful Apply; fresh readback must match this draft exactly
    std::uint64_t generation() const { return generation_; }
    std::uint32_t freq_div() const { return baseline_div_; }
    const std::vector<dc_vf_point>& baseline() const { return baseline_; }
    const std::vector<dc_vf_point>& draft() const { return draft_; }
    bool dirty() const;
    bool can_apply() const;
    bool can_undo() const { return !edits_.empty(); }
    bool can_redo() const { return !redo_.empty(); }
    // A fresh read found a different curve under a dirty draft: Apply refuses
    // until a revert rebases it.
    bool diverged() const { return diverged_; }
    const std::string& status() const { return status_; }
    std::vector<dc_vf_delta> changes() const;
private:
    struct Edit { std::vector<dc_vf_point> points; std::string label; };
    void remember_edit(const std::string& label = {});
    std::uint64_t generation_ = 0;
    bool initialized_ = false, fresh_ = false, diverged_ = false, keep_history_ = false;
    std::uint32_t baseline_div_ = 0, latest_div_ = 0;
    std::vector<dc_vf_point> baseline_, draft_, latest_;
    std::vector<Edit> edits_;
    std::vector<Edit> redo_;
    bool gesture_ = false;
    std::string status_ = "No fresh curve";
};

// Released 1.9.1 V/F readouts. Pure functions of the caller's points and cap,
// recomputed every frame so they always describe the curve on screen.
// "At or below the cap" carries 1.9.1's 0.01 mV tolerance.
bool curve_below_cap(double volt_mv, double cap_mv);
// Peak of the points at or below the cap, and the LOWEST voltage holding it:
// where the boost arbiter parks. No point under the cap leaves park_idx empty.
struct CurvePeak {
    double peak_mhz = 0;
    std::optional<std::uint32_t> park_idx;
    double park_mv = 0;
    std::size_t at_peak = 0;
};
CurvePeak curve_peak(const std::vector<dc_vf_point>& points, double cap_mv);
// Flat runs (a point no faster than the one before it) below the cap, walked in index order.
std::size_t curve_flat_runs(const std::vector<dc_vf_point>& points, double cap_mv);
// Draft points whose raw delta differs from the same index in the baseline.
std::size_t curve_pending_edits(const std::vector<dc_vf_point>& baseline, const std::vector<dc_vf_point>& draft);
// Selection for a typed or stepped point index: the exact index, else the next
// existing one past it in the direction the value moved (clamped to the ends).
std::optional<std::uint32_t> curve_nearest_index(const std::vector<dc_vf_point>& points,
    long long requested, std::uint32_t current);
// 1.9.1 clamps a typed or stepped Set-MHz entry to the adapter's supported
// graphics-clock range. A bound the adapter did not report clamps nothing.
double curve_clamp_entry_mhz(double mhz, std::optional<std::uint32_t> min_mhz, std::optional<std::uint32_t> max_mhz);
// Released 1.9.1 editor bounds. These are planner/input presets the released
// frontend ships for every card (VCAP_DEFAULT, RAMP_FLOOR_DEFAULT, HARD_FLOOR_MV,
// also core's dc_request_envelopes.hard_floor_mv), not limits of any adapter;
// curve_editor_seed() bounds them to the adapter's own curve.
inline constexpr double curve_voltage_grid_mv = 6.25;   // 1.9.1 VCAP_STEP
inline constexpr double curve_cap_default_mv = 1093.75; // on the grid: 175 x 6.25
inline constexpr double curve_ramp_floor_default_mv = 1000.0;
inline constexpr double curve_hard_floor_default_mv = 800.0;
inline constexpr double curve_cap_min_mv = 600.0, curve_cap_max_mv = 1300.0;
inline constexpr double curve_floor_min_mv = 300.0, curve_floor_max_mv = 1300.0;
// 1.9.1 vcap/rfloor/hdf_floor edit: snap DOWN onto the 6.25 mV grid (a value on
// the grid stays put), then clamp to [lo, hi]. Non-finite input returns lo.
double curve_snap_mv(double mv, double lo, double hi);
// 1.9.1 hdf_target edit: (v // step) * step with the whole-MHz grid
// max(1, round(step_khz / 1000)); unknown step_khz uses 1 MHz.
double curve_snap_target_mhz(double mhz, std::uint32_t step_khz);
// What 1.9.1 vf_read derives from a fresh read: the middle point as the default
// selection, the ramp/hard floors clamped to the curve's voltage range and the
// hard-de-flatten target seeded from the table's (uncapped) peak.
struct CurveEditorSeed {
    std::optional<std::uint32_t> middle_idx;
    std::optional<double> ramp_floor_mv, hard_floor_mv, hard_target_mhz;
};
CurveEditorSeed curve_editor_seed(const std::vector<dc_vf_point>& points, double ramp_floor_mv, double hard_floor_mv);
// 1.9.1 vf_read log line: "curve read: peak .. MHz, parks idx .. @ .. mV, N flat run(s) below CAP mV".
std::string curve_read_summary(const std::vector<dc_vf_point>& points, double cap_mv);
// 1.9.1 relayout: the plot and the plan banner share one budget,
// int(H * 0.34) + PLAN_H_IDLE(104 px), the banner and any open block above the
// plot served first; the plot keeps a 240 px floor. Pixel sizes are pre-scaled.
float curve_plot_budget(float client_height, float plan_idle_px, float banner_px, float blocks_px, float floor_px);
// 1.9.1 relayout takes its panel shares of H = client height - menu_h() -
// pinned_chrome_h(), not of the raw client height. pinned_chrome_h() is the
// 28 px tab selector plus the rects Dear PyGui reports for hdr_row and
// gate_bar; the recorded 1.9.1 tree at 1528x1261 (plot 386, log 153, menu 24)
// gives H ~ 1181, i.e. only the one-row unlock strip adds to the 28 px.
// Pixel sizes are pre-scaled; the result never drops below zero.
float curve_share_height(float client_height, float menu_px, float pinned_chrome_px);
// 1.9.1 relayout log box: max(90 px, int(H * 0.13)) of that share height.
float curve_log_height(float share_height, float floor_px);
// 1.9.1 update_vf_corner, the selected point in the plot corner:
// "idx 63   845.00 mV   1710 MHz   delta +0". An unknown request divisor
// leaves the delta unverified.
std::string curve_corner_text(const dc_vf_point& point, std::uint32_t freq_div);
// 1.9.1's key help above the plot. W/S stage one clock bin of step_khz (three
// with Shift); an unknown grid shows "--" for the amounts.
std::string curve_key_help(std::uint32_t step_khz);
// The statistics line under the editor rows ("" when there are no points).
std::string curve_info_text(const std::vector<dc_vf_point>& baseline,
    const std::vector<dc_vf_point>& draft, double cap_mv);

// The plan banner: what one click on Apply would write, before the click.
enum class CurvePlanBand { idle, warn, bad };
struct CurvePlanBannerInput {
    double cap_mv = 0;
    std::size_t table_rows = 0;     // delta rows Reset zeroes
    std::string apply_label;        // the Apply button's own wording
    std::string rephase_label;      // the staged re-phase button's wording
    std::string note;               // planner consequences ("" for none)
    bool note_hard = false;         // a staged hard de-flatten
};
struct CurvePlanBanner {
    CurvePlanBand band = CurvePlanBand::idle;
    std::string head, body, reset;
};
CurvePlanBanner curve_plan_banner(const std::vector<dc_vf_point>& baseline,
    const std::vector<dc_vf_point>& draft, const CurvePlanBannerInput& input);
}
