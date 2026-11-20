// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "clock_monitor_model.h"
#include <memory>
namespace druta::ui {
using ClockClassifier=decltype(&dc_clock_domains_classify);
struct ClockMonitorInput {
    // nullptr means this poll failed. Never pass previously retained telemetry
    // as a fresh sample. sample_id must identify a distinct worker read.
    const dc_telemetry* telemetry=nullptr;
    uint64_t sample_id=0;
    double now_seconds=0;
    std::optional<uint32_t> architecture;
    uint32_t step_khz=0;
    bool step_measured=false;
};
class ClockMonitorService {
public:
    ClockMonitorService();~ClockMonitorService();
    ClockMonitorService(ClockMonitorService&&) noexcept;
    ClockMonitorService& operator=(ClockMonitorService&&) noexcept;
    // The classifier receives only the existing telemetry payload; no getter.
    const ClockMonitorData& update(ClockClassifier classifier,const ClockMonitorInput& input);
    void reset();
private:
    struct Impl;std::unique_ptr<Impl> impl_;
};
}
