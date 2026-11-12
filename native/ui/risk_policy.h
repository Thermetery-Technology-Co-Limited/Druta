/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <cstdint>
#include <string>

namespace druta::ui {
// Unknown is distinct from off; use established session gate state, never a
// momentary telemetry read, to report an already-armed direct write path.
enum class RiskFlag { inactive, active, unknown };
enum class RiskBand { unknown, stock, amber, red, crimson };
struct RiskGates {
    RiskFlag xoc_enabled = RiskFlag::unknown;
    RiskFlag rail_limits_enabled = RiskFlag::unknown;
    RiskFlag rail_limits_writable = RiskFlag::unknown;
    RiskFlag i2c_enabled = RiskFlag::unknown;
    RiskFlag i2c_controller_writable = RiskFlag::unknown;
};
struct RiskColor { std::uint8_t r, g, b; };
struct RiskTint { RiskColor background, border, text; };
struct RiskAssessment {
    unsigned score = 0;             // known active features only
    unsigned possible_score = 0;    // includes features whose state is unknown
    RiskBand band = RiskBand::unknown; // highest known minimum; unknown if none known active
    bool incomplete = true;
    RiskFlag xoc = RiskFlag::unknown, rail_limits = RiskFlag::unknown, i2c = RiskFlag::unknown;
    RiskTint tint{};
    std::string name, reason;
};
RiskAssessment assess_risk(const RiskGates& gates);
}
