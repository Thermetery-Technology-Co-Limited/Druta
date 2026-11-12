// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "session.h"
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {
enum class MonitorFreshness { unavailable, current, stale };
struct MonitorReading {
    std::string label, text, unit;
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
// Consumes the worker's existing snapshot only; never polls or changes tuning.
MonitorFreshness monitor_freshness(const SessionSnapshot& snapshot, uint64_t bit);
MonitorModel build_monitor_model(const SessionSnapshot& snapshot);
}
