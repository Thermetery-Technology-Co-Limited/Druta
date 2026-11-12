/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "risk_policy.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #expr); std::abort(); } } while (false)
int main() {
    const auto on = RiskFlag::active, off = RiskFlag::inactive, unknown = RiskFlag::unknown;
    RiskGates gates{off, off, unknown, off, unknown};
    auto r = assess_risk(gates);
    CHECK(r.band == RiskBand::stock && r.score == 0 && r.possible_score == 0 && !r.incomplete);
    CHECK(r.name == "STOCK" && r.tint.border.r == 58 && r.tint.text.r == 230);
    gates.xoc_enabled = on; r = assess_risk(gates);
    CHECK(r.band == RiskBand::amber && r.score == 1 && r.name == "AMBER");
    CHECK(r.tint.background.r == 58 && r.tint.border.g == 108 && r.tint.text.b == 64);
    gates.rail_limits_enabled = on; gates.rail_limits_writable = on; r = assess_risk(gates);
    CHECK(r.band == RiskBand::red && r.score == 2 && r.name == "RED");
    CHECK(r.tint.background.g == 16 && r.tint.border.r == 208 && r.tint.text.g == 120);
    gates.i2c_enabled = on; gates.i2c_controller_writable = on; r = assess_risk(gates);
    CHECK(r.band == RiskBand::crimson && r.score == 4 && r.name == "CRIMSON");
    CHECK(r.tint.background.b == 26 && r.tint.border.b == 60 && r.tint.text.b == 140);
    CHECK(r.reason.find("board-controller") < r.reason.find("XOC"));
    gates = {off, off, unknown, on, on}; r = assess_risk(gates);
    CHECK(r.band == RiskBand::red && r.score == 2);
    gates.rail_limits_enabled = on; gates.rail_limits_writable = on; r = assess_risk(gates);
    CHECK(r.band == RiskBand::crimson && r.score == 3);
    gates.i2c_controller_writable = off; r = assess_risk(gates);
    CHECK(r.band == RiskBand::amber && r.score == 1 && r.i2c == off);
    gates = {off, off, off, off, unknown}; r = assess_risk(gates);
    CHECK(r.band == RiskBand::stock && !r.incomplete); // disabled mode cannot be active
    gates = {unknown, off, off, off, off}; r = assess_risk(gates);
    CHECK(r.band == RiskBand::unknown && r.score == 0 && r.possible_score == 1 && r.incomplete);
    CHECK(r.name == "UNKNOWN" && r.reason.find("unknown") != std::string::npos);
    gates = {on, on, unknown, on, unknown}; r = assess_risk(gates);
    CHECK(r.band == RiskBand::amber && r.score == 1 && r.possible_score == 4 && r.incomplete);
    CHECK(r.reason.find("XOC") != std::string::npos && r.reason.find("unknown") != std::string::npos);
    gates = {off, on, off, on, off}; r = assess_risk(gates);
    CHECK(r.band == RiskBand::stock && !r.incomplete); // inaccessible controller/rail
    gates = {off, off, on, on, on};
    const auto before = assess_risk(gates); // a transient presence read is not a gate input
    const auto after = assess_risk(gates);
    CHECK(before.band == RiskBand::red && after.band == RiskBand::red);
    std::printf("risk_policy_test: %u checks passed\n", checks);
}
