/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_draft.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <unordered_set>

namespace druta::ui {
namespace {
bool valid(const std::vector<dc_vf_point>& points, std::uint32_t div) {
    if (!div) return false;
    if (points.empty() || points.size() > DC_VFP_POINTS) return false;
    std::unordered_set<std::uint32_t> indices;
    for (const auto& p : points) {
        if (!std::isfinite(p.volt_mv) || !std::isfinite(p.freq_mhz) ||
            !std::isfinite(p.freq_mhz - static_cast<double>(p.delta_khz) / (1000.0 * div)) ||
            !indices.insert(p.idx).second) return false;
    }
    return true;
}
const dc_vf_point* find(const std::vector<dc_vf_point>& points, std::uint32_t index) {
    const auto it = std::find_if(points.begin(), points.end(), [index](const auto& p) { return p.idx == index; });
    return it == points.end() ? nullptr : &*it;
}
bool same_curve(const std::vector<dc_vf_point>& a, const std::vector<dc_vf_point>& b, std::uint32_t div) {
    if (a.size() != b.size()) return false;
    for (const auto& p : a) {
        const auto* q = find(b, p.idx);
        if (!q || p.volt_mv != q->volt_mv || p.delta_khz != q->delta_khz ||
            p.freq_mhz - static_cast<double>(p.delta_khz) / (1000.0 * div) !=
            q->freq_mhz - static_cast<double>(q->delta_khz) / (1000.0 * div)) return false;
    }
    return true;
}
void set_delta(dc_vf_point& point, const dc_vf_point& base, std::int32_t value, std::uint32_t div) {
    point.delta_khz = value;
    point.freq_mhz = base.freq_mhz + (static_cast<double>(value) - base.delta_khz) / (1000.0 * div);
}
}
void CurveDraft::observe(std::uint64_t generation, const std::vector<dc_vf_point>& points, bool fresh, std::uint32_t freq_div) {
    if (!initialized_ || generation != generation_) {
        initialized_ = true; generation_ = generation;
        baseline_.clear(); draft_.clear(); latest_.clear(); edits_.clear(); redo_.clear(); gesture_ = false;
        fresh_ = false; diverged_ = false; baseline_div_ = latest_div_ = 0;
    }
    if (!fresh || !valid(points, freq_div)) {
        fresh_ = false;
        status_ = fresh ? "Curve is malformed or incomplete" : "Curve is not fresh";
        return;
    }
    fresh_ = true;
    latest_ = points; latest_div_ = freq_div;
    if (baseline_.empty()) {
        baseline_ = draft_ = points; baseline_div_ = freq_div;
        status_ = "Curve ready";
    } else if (dirty() || diverged_ || gesture_) {
        if (baseline_div_ != freq_div || !same_curve(baseline_, points, baseline_div_)) diverged_ = true;
        status_ = diverged_ ? "Hardware curve changed; revert to rebase before applying" : "Draft ready";
    } else {
        const bool unchanged = baseline_div_ == freq_div && same_curve(baseline_, points, baseline_div_);
        baseline_ = draft_ = points; baseline_div_ = freq_div;
        if (!keep_history_ || !unchanged) { edits_.clear(); redo_.clear(); }
        gesture_ = false;
        status_ = "Curve ready";
    }
}
bool CurveDraft::stage_khz(std::uint32_t index, double value, const std::string& label) {
    if (!std::isfinite(value) || std::trunc(value) != value ||
        value < std::numeric_limits<std::int32_t>::min() || value > std::numeric_limits<std::int32_t>::max()) {
        status_ = "Delta must be an exact signed 32-bit kHz value"; return false;
    }
    auto it = std::find_if(draft_.begin(), draft_.end(), [index](const auto& p) { return p.idx == index; });
    const auto* base = find(baseline_, index);
    if (it == draft_.end() || !base) { status_ = "Point is absent from the baseline"; return false; }
    const auto delta = static_cast<std::int32_t>(value);
    if (it->delta_khz == delta) return true;
    const double freq = base->freq_mhz + (static_cast<double>(delta) - base->delta_khz) / (1000.0 * baseline_div_);
    if (!std::isfinite(freq)) { status_ = "Resulting frequency is not finite"; return false; }
    if (!gesture_) remember_edit(label);
    set_delta(*it, *base, delta, baseline_div_);
    status_ = diverged_ ? "Hardware curve changed; revert to rebase before applying" :
              fresh_ ? "Draft ready" : "Draft retained; fresh curve required";
    return true;
}
bool CurveDraft::stage_mhz(std::uint32_t index, double value) {
    if (!std::isfinite(value)) { status_ = "Delta must be a finite MHz value"; return false; }
    if (!baseline_div_) { status_ = "Curve frequency divisor unavailable"; return false; }
    const double scale = 1000.0 * baseline_div_;
    const double scaled = value * scale;
    const double rounded = std::round(scaled);
    if (!std::isfinite(scaled) || rounded / scale != value) {
        status_ = "Delta must be an exact whole number of kHz"; return false;
    }
    return stage_khz(index, rounded);
}
bool CurveDraft::stage_frequency_mhz(std::uint32_t index, double target_mhz,
    std::uint32_t step_khz, std::int64_t max_abs_delta_khz, bool floor_target, const std::string& label) {
    const auto* base = find(baseline_, index);
    if (!base || !baseline_div_ || !step_khz || max_abs_delta_khz <= 0
        || max_abs_delta_khz > std::numeric_limits<std::int32_t>::max()
        || !std::isfinite(target_mhz) || !std::isfinite(target_mhz * 1000.0)) {
        status_ = "Verified point, clock step and request envelope are required";
        return false;
    }
    const double wire_step = static_cast<double>(step_khz) * baseline_div_;
    const double low = std::ceil((-static_cast<double>(max_abs_delta_khz) - base->delta_khz) / wire_step);
    const double high = std::floor((static_cast<double>(max_abs_delta_khz) - base->delta_khz) / wire_step);
    if (low > high) { status_ = "No representable clock bin inside the request envelope"; return false; }
    const double offset = (target_mhz * 1000.0 - base->freq_mhz * 1000.0) / step_khz;
    double bins = std::floor(offset + (floor_target ? 0.0 : 0.5));
    bins = std::max(low, std::min(high, bins));
    return stage_khz(index, static_cast<double>(base->delta_khz) + bins * wire_step, label);
}
void CurveDraft::remember_edit(const std::string& label) {
    if (edits_.size() == 64) edits_.erase(edits_.begin());
    edits_.push_back({draft_, label});
    redo_.clear();
}
void CurveDraft::begin_gesture(const std::string& label) {
    if (gesture_ || draft_.empty()) return;
    remember_edit(label);
    gesture_ = true;
}
void CurveDraft::end_gesture() { gesture_ = false; }
bool CurveDraft::undo_edit() {
    end_gesture();
    if (edits_.empty()) return false;
    redo_.push_back({draft_, edits_.back().label});
    draft_ = std::move(edits_.back().points);
    edits_.pop_back();
    status_ = diverged_ ? "Hardware curve changed; revert to rebase before applying" :
              fresh_ ? "Edit undone" : "Edit undone; fresh curve required";
    return true;
}
bool CurveDraft::redo_edit() {
    end_gesture();
    if (redo_.empty()) return false;
    if (edits_.size() == 64) edits_.erase(edits_.begin());
    edits_.push_back({draft_, redo_.back().label});
    draft_ = std::move(redo_.back().points);
    redo_.pop_back();
    status_ = diverged_ ? "Hardware curve changed; revert to rebase before applying" :
              fresh_ ? "Edit redone" : "Edit redone; fresh curve required";
    return true;
}
bool CurveDraft::revert() {
    if (!fresh_ || latest_.empty()) { status_ = "A fresh curve is required to rebase"; return false; }
    baseline_ = draft_ = latest_; baseline_div_ = latest_div_; edits_.clear(); redo_.clear(); gesture_ = false; diverged_ = false;
    status_ = "Draft reverted to current curve"; return true;
}
bool CurveDraft::revert_undoable() {
    end_gesture();
    // Edits measured from a curve the card no longer has cannot come back.
    if (diverged_ && fresh_ && !latest_.empty()) return revert();
    if (baseline_.empty()) return false;
    if (dirty()) remember_edit("revert edits");
    draft_ = baseline_;
    status_ = "Draft reverted"; return true;
}
bool CurveDraft::accept_applied() {
    if (!fresh_ || gesture_ || baseline_div_ != latest_div_ || !same_curve(draft_, latest_, baseline_div_)) return false;
    if (!revert()) return false;
    status_ = "Applied curve verified"; return true;
}
bool CurveDraft::dirty() const {
    if (draft_.size() != baseline_.size()) return false;
    for (const auto& point : draft_) {
        const auto* base = find(baseline_, point.idx);
        if (!base || base->delta_khz != point.delta_khz) return true;
    }
    return false;
}
bool CurveDraft::can_apply() const { return fresh_ && !diverged_ && !baseline_.empty() && dirty(); }
std::vector<dc_vf_delta> CurveDraft::changes() const {
    std::vector<dc_vf_delta> result;
    for (const auto& point : draft_) {
        const auto* base = find(baseline_, point.idx);
        if (base && point.delta_khz != base->delta_khz) result.push_back({point.idx, point.delta_khz});
    }
    return result;
}

namespace {
template <typename... Args> std::string text_of(const char* pattern, Args... args) {
    char buffer[512]{};
    std::snprintf(buffer, sizeof(buffer), pattern, args...);
    return buffer;
}
std::vector<dc_vf_point> by_index(const std::vector<dc_vf_point>& points) {
    auto sorted = points;
    std::stable_sort(sorted.begin(), sorted.end(), [](const auto& a, const auto& b) { return a.idx < b.idx; });
    return sorted;
}
std::string park_text(const CurvePeak& peak) {
    return peak.park_idx ? std::to_string(*peak.park_idx) : std::string("--");
}
constexpr const char* kLessEqual = "\xE2\x89\xA4"; // U+2264
constexpr const char* kDot = "\xC2\xB7";          // U+00B7
}

bool curve_below_cap(double volt_mv, double cap_mv) { return volt_mv <= cap_mv + 0.01; }

CurvePeak curve_peak(const std::vector<dc_vf_point>& points, double cap_mv) {
    CurvePeak result;
    bool any = false;
    for (const auto& p : points) if (curve_below_cap(p.volt_mv, cap_mv) && (!any || p.freq_mhz > result.peak_mhz)) {
        result.peak_mhz = p.freq_mhz; any = true;
    }
    if (!any) return {};
    for (const auto& p : points) {
        if (!curve_below_cap(p.volt_mv, cap_mv) || p.freq_mhz != result.peak_mhz) continue;
        ++result.at_peak;
        if (!result.park_idx || p.volt_mv < result.park_mv) { result.park_idx = p.idx; result.park_mv = p.volt_mv; }
    }
    return result;
}

std::size_t curve_flat_runs(const std::vector<dc_vf_point>& points, double cap_mv) {
    const auto sorted = by_index(points);
    std::size_t flats = 0, run = 1;
    for (std::size_t i = 1; i < sorted.size(); ++i) {
        if (curve_below_cap(sorted[i].volt_mv, cap_mv) && sorted[i].freq_mhz <= sorted[i - 1].freq_mhz) ++run;
        else { if (run > 1) ++flats; run = 1; }
    }
    return flats + (run > 1 ? 1 : 0);
}

std::size_t curve_pending_edits(const std::vector<dc_vf_point>& baseline, const std::vector<dc_vf_point>& draft) {
    std::size_t pending = 0;
    for (const auto& point : draft) {
        const auto* base = find(baseline, point.idx);
        if (base && base->delta_khz != point.delta_khz) ++pending;
    }
    return pending;
}

std::optional<std::uint32_t> curve_nearest_index(const std::vector<dc_vf_point>& points,
    long long requested, std::uint32_t current) {
    if (points.empty()) return std::nullopt;
    const bool upward = requested > static_cast<long long>(current);
    std::optional<std::uint32_t> beyond, lowest, highest;
    for (const auto& p : points) {
        const long long idx = p.idx;
        if (idx == requested) return p.idx;
        if (!lowest || p.idx < *lowest) lowest = p.idx;
        if (!highest || p.idx > *highest) highest = p.idx;
        if (upward ? idx > requested && (!beyond || p.idx < *beyond)
                   : idx < requested && (!beyond || p.idx > *beyond)) beyond = p.idx;
    }
    return beyond ? beyond : upward ? highest : lowest;
}

double curve_clamp_entry_mhz(double mhz, std::optional<std::uint32_t> min_mhz, std::optional<std::uint32_t> max_mhz) {
    if (min_mhz && max_mhz && *min_mhz > *max_mhz) return mhz; // inconsistent bounds: no clamp
    if (min_mhz && mhz < *min_mhz) mhz = *min_mhz;
    if (max_mhz && mhz > *max_mhz) mhz = *max_mhz;
    return mhz;
}

double curve_snap_mv(double mv, double lo, double hi) {
    if (!std::isfinite(mv)) return lo;
    // epsilon: float slop must not drop a value that IS on the grid to the point below it
    const double snapped = std::floor(mv / curve_voltage_grid_mv + 1e-9) * curve_voltage_grid_mv;
    return std::clamp(std::abs(snapped - mv) > 1e-6 ? snapped : mv, lo, hi);
}

double curve_snap_target_mhz(double mhz, std::uint32_t step_khz) {
    if (!std::isfinite(mhz)) return mhz;
    const double step = std::max(1.0, std::round(step_khz / 1000.0));
    return std::floor(std::floor(mhz) / step) * step;
}

CurveEditorSeed curve_editor_seed(const std::vector<dc_vf_point>& points, double ramp_floor_mv, double hard_floor_mv) {
    CurveEditorSeed seed;
    if (points.empty()) return seed;
    seed.middle_idx = points[points.size() / 2].idx;
    double lo = points.front().volt_mv, hi = lo, peak = points.front().freq_mhz;
    for (const auto& p : points) {
        lo = std::min(lo, p.volt_mv); hi = std::max(hi, p.volt_mv); peak = std::max(peak, p.freq_mhz);
    }
    if (!std::isfinite(lo) || !std::isfinite(hi)) return seed;
    seed.ramp_floor_mv = std::clamp(std::isfinite(ramp_floor_mv) ? ramp_floor_mv : lo, lo, hi);
    seed.hard_floor_mv = std::clamp(std::isfinite(hard_floor_mv) ? hard_floor_mv : lo, lo, hi);
    if (std::isfinite(peak) && peak > 0) seed.hard_target_mhz = std::floor(peak);
    return seed;
}

std::string curve_read_summary(const std::vector<dc_vf_point>& points, double cap_mv) {
    const auto sorted = by_index(points);
    const auto peak = curve_peak(sorted, cap_mv);
    return text_of("curve read: peak %.0f MHz, parks idx %s @ %.2f mV, %zu flat run(s) below %.0f mV",
        peak.peak_mhz, park_text(peak).c_str(), peak.park_mv, curve_flat_runs(sorted, cap_mv), cap_mv);
}

float curve_plot_budget(float client_height, float plan_idle_px, float banner_px, float blocks_px, float floor_px) {
    const float budget = std::floor(client_height * 0.34f) + plan_idle_px;
    return std::max(floor_px, budget - std::max(0.0f, banner_px) - std::max(0.0f, blocks_px));
}

float curve_share_height(float client_height, float menu_px, float pinned_chrome_px) {
    return std::max(0.0f, client_height - std::max(0.0f, menu_px) - std::max(0.0f, pinned_chrome_px));
}

float curve_log_height(float share_height, float floor_px) {
    return std::max(floor_px, std::floor(std::max(0.0f, share_height) * 0.13f));
}

std::string curve_corner_text(const dc_vf_point& point, std::uint32_t freq_div) {
    std::string text = text_of("idx %u   %.2f mV   %.0f MHz   ", point.idx, point.volt_mv, point.freq_mhz);
    return text + (freq_div ? text_of("delta %+.0f", point.delta_khz / (1000.0 * freq_div)) : std::string("delta unverified"));
}

std::string curve_key_help(std::uint32_t step_khz) {
    constexpr const char* bullet = "  \xE2\x80\xA2  "; // U+2022
    constexpr const char* plus_minus = "\xC2\xB1";    // U+00B1
    const auto amount = [&](int bins) {
        return step_khz ? plus_minus + text_of("%.9g", bins * (step_khz / 1000.0)) : std::string("--");
    };
    return std::string("drag a dot to move it") + bullet + "drag anywhere else to pan" + bullet + "A/D select" + bullet
        + "W/S move " + amount(1) + " MHz" + bullet + "hold Shift for " + amount(3) + bullet
        + "Ctrl+H hold the selected point (again to release)" + bullet + "Ctrl+Z / Ctrl+Y undo and redo staged edits";
}

std::string curve_info_text(const std::vector<dc_vf_point>& baseline,
    const std::vector<dc_vf_point>& draft, double cap_mv) {
    if (draft.empty()) return {};
    const auto sorted = by_index(draft);
    const auto peak = curve_peak(sorted, cap_mv);
    const dc_vf_point* top = nullptr;
    for (const auto& p : sorted) if (curve_below_cap(p.volt_mv, cap_mv)) top = &p;
    const auto pending = curve_pending_edits(baseline, draft);
    std::string text = text_of("%zu points   ", sorted.size());
    if (top) text += text_of("top %scap: idx %u @ %.2f mV = %.0f MHz   ", kLessEqual, top->idx, top->volt_mv, top->freq_mhz);
    text += text_of("peak %.0f MHz held by %zu point(s), lowest = idx %s @ %.2f mV (where the card parks)   ",
        peak.peak_mhz, peak.at_peak, park_text(peak).c_str(), peak.park_mv);
    text += text_of("flat runs below cap: %zu", curve_flat_runs(sorted, cap_mv));
    if (pending) text += text_of("   [staged curve, %zu edit(s) not yet written]", pending);
    return text;
}

CurvePlanBanner curve_plan_banner(const std::vector<dc_vf_point>& baseline,
    const std::vector<dc_vf_point>& draft, const CurvePlanBannerInput& input) {
    CurvePlanBanner banner;
    const auto pending = curve_pending_edits(baseline, draft);
    // Reset re-reads the curve, which rebases the editor and drops the staged edits (1.9.1 vf_read).
    banner.reset = text_of("'Reset curve to stock' is one click too: it zeroes all %zu deltas back to the factory curve"
        " and discards %zu staged edit(s).", input.table_rows, pending);
    if (!pending) {
        banner.head = text_of("APPLY TO GPU  %s  nothing staged", kDot);
        banner.body = "Drag a dot, nudge with W/S, Set MHz, De-flatten or Hard de-flatten, and this box says exactly what one click on '"
            + input.apply_label + "' will write - before the click, not after it. That click writes the staged table as shown and does not "
              "re-phase it: '" + input.rephase_label + "' stages that correction for review first.";
        return banner;
    }
    double top = 0, peak = 0, hardware_peak = 0;
    for (const auto& p : draft) {
        if (curve_below_cap(p.volt_mv, input.cap_mv)) top = std::max(top, p.freq_mhz);
        peak = std::max(peak, p.freq_mhz);
    }
    for (const auto& p : baseline) hardware_peak = std::max(hardware_peak, p.freq_mhz);
    const auto park = curve_peak(draft, input.cap_mv);
    const bool lowers_peak = peak < hardware_peak;
    banner.band = lowers_peak || input.note_hard ? CurvePlanBand::bad : CurvePlanBand::warn;
    banner.head = text_of("APPLY TO GPU  %s  ", kDot) + (lowers_peak ? "ONE CLICK LOWERS THE PEAK"
        : input.note_hard ? "HARD DE-FLATTEN - WITHOUT THE VOLTAGE MOD THIS WILL CRASH" : "ONE CLICK WRITES THIS");
    banner.body = (lowers_peak ? text_of("WARNING: this LOWERS the curve's peak from %.0f to %.0f MHz. ", hardware_peak, peak) : std::string())
        + text_of("Writes %zu edited point(s): top %s%.0f mV becomes %.0f MHz, peak %.0f MHz held by %zu point(s), the card parks at idx %s @ %.2f mV.\n",
            pending, kLessEqual, input.cap_mv, top, peak, park.at_peak, park_text(park).c_str(), park.park_mv)
        + (input.note.empty() ? std::string() : input.note + "\n")
        + "An undo point is recorded immediately before the write and 'Undo hardware write' puts this state back; the status line "
          "reports the readback, and a write that does not read back exactly keeps that undo point. Zeroing the table is always "
          "available via 'Reset curve to stock' or a reboot.";
    return banner;
}
}
