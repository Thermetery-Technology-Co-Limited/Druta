/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_draft.h"
#include <algorithm>
#include <cmath>
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
        baseline_ = draft_ = points; baseline_div_ = freq_div;
        edits_.clear(); redo_.clear(); gesture_ = false;
        status_ = "Curve ready";
    }
}
bool CurveDraft::stage_khz(std::uint32_t index, double value) {
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
    if (!gesture_) remember_edit();
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
    std::uint32_t step_khz, std::int64_t max_abs_delta_khz, bool floor_target) {
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
    return stage_khz(index, static_cast<double>(base->delta_khz) + bins * wire_step);
}
void CurveDraft::remember_edit() {
    if (edits_.size() == 64) edits_.erase(edits_.begin());
    edits_.push_back({draft_});
    redo_.clear();
}
void CurveDraft::begin_gesture() {
    if (gesture_ || draft_.empty()) return;
    remember_edit();
    gesture_ = true;
}
void CurveDraft::end_gesture() { gesture_ = false; }
bool CurveDraft::undo_edit() {
    end_gesture();
    if (edits_.empty()) return false;
    redo_.push_back({draft_});
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
    edits_.push_back({draft_});
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
}
