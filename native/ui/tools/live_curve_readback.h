/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once

#include "backend.h"
#include <cmath>
#include <map>
#include <optional>
#include <utility>

namespace druta::ui::live_check {

enum class CurveMatch { evaluated, requests };
struct CurveRow { double voltage; std::int64_t frequency_minus_delta_khz; std::int32_t delta; };

inline bool curve_signature(const LiveData& d, std::map<uint32_t, CurveRow>& out) {
    if (!d.curve_freq_div || !d.curve_error.empty() || d.curve.empty() || d.curve.size() > DC_VFP_POINTS) return false;
    for (const auto& p : d.curve) {
        const double khz = p.freq_mhz * 1000.0 * d.curve_freq_div;
        if (!std::isfinite(p.volt_mv) || !std::isfinite(khz) || khz < 0 || khz > UINT32_MAX ||
            std::abs(khz - std::round(khz)) > 0.000001) return false;
        const auto frequency_minus_delta = static_cast<std::int64_t>(std::llround(khz)) - p.delta_khz;
        if (!out.emplace(p.idx, CurveRow{p.volt_mv, frequency_minus_delta, p.delta_khz}).second) return false;
    }
    return true;
}

inline bool same_curve(const LiveData& before, const LiveData& after,
                       std::optional<std::pair<uint32_t, int32_t>> edit = {},
                       CurveMatch scope = CurveMatch::evaluated) {
    std::map<uint32_t, CurveRow> a, b;
    if (before.curve_freq_div != after.curve_freq_div || !curve_signature(before, a) ||
        !curve_signature(after, b) || a.size() != b.size()) return false;
    for (const auto& entry : a) {
        const auto it = b.find(entry.first);
        const auto expected = edit && edit->first == entry.first ? edit->second : entry.second.delta;
        if (it == b.end() || it->second.voltage != entry.second.voltage || it->second.delta != expected ||
            (scope == CurveMatch::evaluated && it->second.frequency_minus_delta_khz != entry.second.frequency_minus_delta_khz))
            return false;
    }
    return true;
}

} // namespace druta::ui::live_check
