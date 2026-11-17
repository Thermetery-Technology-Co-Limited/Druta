// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <drutacore/clkdom.h>
#include <array>
#include <optional>
#include <string>
#include <utility>
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
    // Python Reading.window_util: the lowest and highest load over a judged window.
    std::optional<std::pair<double,double>> window_util;
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
// divisor: Python divides the MEM tile's measured line by the adapter's memory
// divisor, exactly as the tile value is; 1 leaves driver units.
std::string clock_secondary_text(const ClockReading& reading,bool stale=false,double divisor=1);
std::string clock_secondary_explanation(const ClockReading& reading,double divisor=1);

// Classic / Verbose, Python 1.9.1 real_clock_view. The measured line under a clock
// tile: "measured 307  Δ +82.1" (B and Δ divided by divisor, Δ whole above 100),
// "unproven ...", "last ..." after a failed private read, or the word forms
// "measured: checking B" / "none (B = A)" / "same as A" / "--". The core subtitle
// token follows Python REAL_STATE_TOKEN: " · load 10 %", " · steady at load",
// " · stale", " · read failed", ... Empty when no reading exists.
struct ClassicClockLine { std::string line, token; };
// stale: telemetry for this tile is retained (paused, stale bit). failed: the
// private read failed and the last good row is kept.
ClassicClockLine classic_clock_line(const ClockReading* reading,bool stale,bool failed,double divisor=1);
// Python REAL_TEMPLATES (with REAL_TEMPLATES_RAW when the memory clock has no divisor)
// and REAL_SUB_TOKENS: every shape the measured line and the core subtitle's token
// take, so the tiles are sized for all of them and never grow when a line changes.
std::vector<std::string> classic_clock_line_templates(bool raw_memory);
const std::vector<std::string>& classic_core_tokens();
// Python real_clock_view's tooltip on a clock tile's measured line: the tile's figure,
// the private row's A and B, Δ in clock bins, what B has shown (trust), why the line is
// or is not judged (state), and for the core the colour rule; or why there is no line.
struct ClassicClockTip {
    std::string key;                      // "core", "xbar" or "mem"
    const ClockReading* reading=nullptr;  // the tile's reading; nullptr when it has none
    bool rows=false;                      // this snapshot classified private clock rows
    std::string error;                    // why it did not (empty: nothing read yet)
    std::optional<std::string> tile;      // the tile's figure as shown; nullopt: none
    bool memory_divided=false;            // MEM figure in MHz (adapter divisor), else raw units
    double divisor=1;                     // MEM: the measured line's divisor
    bool stale=false, failed=false;       // as for classic_clock_line
};
std::string classic_clock_tooltip(const ClassicClockTip& tip);
// Python refresh_domains cells: "%2u" index, name with '?' for an inferred grade
// ("--" unnamed), "+82.1 MHz" delta, 2-digit (or 8-digit) hex flags, decimal srcid;
// grade "confirmed" -> text, "likely" -> warn, otherwise dim.
struct ClassicDomainCells { std::string index, name, a, b, delta, flags, srcid, grade_tone; };
ClassicDomainCells classic_domain_cells(const ClockDomainViewRow& row);
}
