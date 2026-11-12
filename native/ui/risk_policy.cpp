/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "risk_policy.h"
#include <array>

namespace druta::ui {
namespace {
RiskFlag both(RiskFlag a, RiskFlag b) {
    if (a == RiskFlag::inactive || b == RiskFlag::inactive) return RiskFlag::inactive;
    if (a == RiskFlag::active && b == RiskFlag::active) return RiskFlag::active;
    return RiskFlag::unknown;
}
RiskBand band(unsigned score) {
    if (!score) return RiskBand::stock;
    if (score == 1) return RiskBand::amber;
    if (score == 2) return RiskBand::red;
    return RiskBand::crimson;
}
RiskTint tint(RiskBand b) {
    switch (b) {
    case RiskBand::amber: return {{58,34,10}, {196,108,24}, {255,176,64}};
    case RiskBand::red: return {{60,16,16}, {208,56,56}, {255,120,120}};
    case RiskBand::crimson: return {{72,6,26}, {220,20,60}, {255,96,140}};
    default: return {{0,0,0}, {58,63,75}, {230,232,236}};
    }
}
const char* name(RiskBand b) {
    switch (b) {
    case RiskBand::stock: return "STOCK";
    case RiskBand::amber: return "AMBER";
    case RiskBand::red: return "RED";
    case RiskBand::crimson: return "CRIMSON";
    default: return "UNKNOWN";
    }
}
}
RiskAssessment assess_risk(const RiskGates& gates) {
    RiskAssessment result;
    result.xoc = gates.xoc_enabled;
    result.rail_limits = both(gates.rail_limits_enabled, gates.rail_limits_writable);
    result.i2c = both(gates.i2c_enabled, gates.i2c_controller_writable);
    struct Feature { RiskFlag state; unsigned weight; const char* reason; };
    // Descending weights, matching the existing UI's feature ordering.
    const std::array<Feature, 3> features{{
        {result.i2c, 2, "Direct board-controller writes bypass GPU voltage request limits"},
        {result.xoc, 1, "XOC expands software request ranges"},
        {result.rail_limits, 1, "Rail voltage limits can be written directly"},
    }};
    result.incomplete = false;
    for (const auto& feature : features) {
        if (feature.state == RiskFlag::active) {
            result.score += feature.weight;
            if (!result.reason.empty()) result.reason += "; ";
            result.reason += feature.reason;
        } else if (feature.state == RiskFlag::unknown) {
            result.incomplete = true;
            result.possible_score += feature.weight;
        }
    }
    result.possible_score += result.score;
    result.band = result.score ? band(result.score) : result.incomplete ? RiskBand::unknown : RiskBand::stock;
    result.tint = tint(result.band);
    result.name = name(result.band);
    if (result.incomplete) {
        if (!result.reason.empty()) result.reason += "; ";
        result.reason += "Some control states are unknown";
    } else if (result.reason.empty()) result.reason = "No expanded-control modes reported";
    return result;
}
}
