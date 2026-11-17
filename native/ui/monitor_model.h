// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "session.h"
#include "shunt_model.h"
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace druta::ui {
enum class MonitorFreshness { unavailable, current, stale };
struct MonitorReading {
    std::string key;  // classic tile key (Python TILES): core xbar mem edge hot pwr vcore
    std::string label, text, unit;
    std::string detail; // dim tile subtitle, e.g. the memory type and data rate
    MonitorFreshness freshness = MonitorFreshness::unavailable;
    std::optional<double> percentage;
};
struct MonitorReason {
    std::string label;
    std::optional<bool> active;
    MonitorFreshness freshness = MonitorFreshness::unavailable;
};
struct MonitorGroup { std::string label; std::vector<MonitorReading> readings; };
struct MonitorModel {
    std::vector<MonitorReading> tiles;
    std::vector<MonitorReason> clock_reasons, perf_reasons;
    std::vector<MonitorGroup> groups;
};
// Python mem_fmt: true MHz ("%.1f", trailing zeros stripped) and
// "<type> · <rate> Gbps" when the adapter's memory divisor is known; otherwise
// the raw driver figure ("%.0f") and a rate marked "(raw)". "--" when absent.
struct MemoryClockText { std::string value, detail; };
MemoryClockText memory_clock_text(const LiveData& data, std::optional<double> reported);
// Consumes the worker's existing snapshot only; never polls or changes tuning.
MonitorFreshness monitor_freshness(const SessionSnapshot& snapshot, uint64_t bit);
// classic: the MEM CLOCK tile follows Python mem_fmt (memory_clock_text); groups[0]
// holds exactly Python's seven BARS (GPU, Board, TDP used, GPU/FB/VID/BUS util) and
// the other power readings lead groups[2] (State & sensors).
MonitorModel build_monitor_model(const SessionSnapshot& snapshot, bool classic = false);

// Classic colour roles from Python 1.9.1 (druta.py TILES, BARS, lamps).
enum class MonitorTone { dim, idle, accent, violet, good, warn, bad };
// Python refresh_monitor: an asserted "Idle" reason is green, any other asserted
// reason red; a clear or unread reason stays the dark idle colour.
MonitorTone classic_lamp_tone(const MonitorReason& reason);
// Python BARS escalate from the bar's own colour to amber at >= 80 % and red at >= 95 %.
MonitorTone classic_bar_tone(const MonitorReading& bar, MonitorTone base);
// Python overlay: "9%", TDP "9%   77 / 900 W" (raw draw / enforced limit) or
// "power / limit unavailable"; an unread bar shows "--"; a retained one "(stale)".
std::string classic_bar_overlay(const SessionSnapshot& snapshot, const MonitorReading& bar, bool tdp);
// Tile value colour: CORE/MEM/VCORE accent, XBAR violet, EDGE green, HOTSPOT
// green / amber >= 80 C / red >= 90 C, POWER accent, or amber (exact) / red
// (estimate) when shunt-corrected. power_corrected_exact is set only while a
// correction is shown on the POWER tile.
MonitorTone classic_tile_tone(const SessionSnapshot& snapshot, const MonitorReading& tile,
    std::optional<bool> power_corrected_exact = std::nullopt);
// Python refresh_monitor s_pwr / t_pwr: the POWER tile number ("%.0f" W, shunt-corrected
// when a correction is active) and its dim lines: "limit 900 W" (or "limit unavailable"),
// corrected "raw 77 W x3.499[ est]" and "limit 900 W = 3149 W real" on two lines.
// exact is set only while a correction is shown. A missing draw keeps "--" and the limit line.
struct ClassicPowerTile { std::string value, subtitle; std::optional<bool> exact; };
ClassicPowerTile classic_power_tile(const SessionSnapshot& snapshot, const MonitorReading& tile,
    const ShuntCorrection& correction);
// nvbackend mem_offset_scale: NVML memory-offset units per shown MHz and that unit,
// 2 x the adapter's memory divisor "MHz true", or 2 "MHz eff" without a divisor.
std::pair<double, std::string> classic_memory_offset_scale(const LiveData& data);
// Python PCIE LINK text: "Gen 4  x16" / "errors: 0  (since launch 0)", then the non-zero
// AER counters when the total is not zero; good when the total is zero, bad otherwise.
// An incomplete or unread total is "--" and dim, never an all-clear.
struct ClassicPcieSummary { std::string text; MonitorTone tone = MonitorTone::dim; };
ClassicPcieSummary classic_pcie_summary(const SessionSnapshot& snapshot);
// Python STATE text: energy / fans / offsets (memory in the slider's true or effective MHz)
// / volt-boost, V/F and clock locks. Unread values are "--", not zero or "none".
std::string classic_state_summary(const SessionSnapshot& snapshot);
// Python relayout() budget for the Monitor page, in pixels (px = classic_px(1)):
// bot_h = max(110, 12 % of the page); dom_h = 34 % of the page (or the domain panel's
// content when that is taller) but leaving the middle row its 220 px floor, never
// under 240; mid_h = whatever is left after tiles, domains, bottom row and the chrome
// between and below the bands, never under 220. Panels scroll inside past these heights.
// Python subtracts a fixed 102 px of chrome; `chrome` is the native page's measured
// gaps and trailing History header (negative or non-finite: not measured, use 102 px).
struct ClassicMonitorLayout { float dom = 0, mid = 0, bot = 0; };
ClassicMonitorLayout classic_monitor_layout(float page, float tiles, float dom_content, float px, float chrome = -1);
}
