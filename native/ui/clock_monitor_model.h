// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <drutacore/clkdom.h>
#include <array>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct ClockDomainViewRow {
    dc_clock_domain_row raw{};
    std::string label, a, b, delta, flags, source, band;
};
struct ClockReading {
    bool available=false, stale=false, paired=false, held=false;
    uint32_t domain=0, scale=1;
    std::string key, name, trust, state, verdict;
    unsigned samples=0, events=0, mirror_run=0, fresh=0;
    double a_mhz=0,b_mhz=0,delta_mhz=0;
    std::optional<double> util, bin_mhz, hold_mhz;
    std::vector<double> window;
};
struct ClockMonitorData {
    bool available=false, stale=false;
    std::string message;
    std::vector<ClockDomainViewRow> rows;
    // Core, XBAR, memory. Units match the existing raw-driver MHz tiles.
    std::array<ClockReading,3> readings{};
};
std::string clock_gap_band(double gap_mhz,std::optional<double> bin_mhz);
double clock_median_hold(const std::vector<double>& deltas);
std::string clock_window_verdict(const std::vector<double>& deltas,std::optional<double> bin_mhz,
    const std::string& previous={},std::optional<double> hold={});
ClockDomainViewRow normalize_clock_domain(const dc_clock_domain_row& row,std::optional<double> bin_mhz);
std::string clock_secondary_text(const ClockReading& reading,bool stale=false);
std::string clock_secondary_explanation(const ClockReading& reading);
}
