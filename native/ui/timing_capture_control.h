// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <chrono>
#include <functional>
namespace druta::ui {
struct TimingCaptureControl {
    using Clock = std::chrono::steady_clock;
    Clock::time_point deadline = Clock::time_point::max();
    std::function<bool()> cancelled;
    bool cancellation_requested() const { return cancelled && cancelled(); }
    bool expired() const { return Clock::now() >= deadline; }
    bool stopped() const { return cancellation_requested() || expired(); }
    unsigned remaining_ms(unsigned cap) const {
        const auto now = Clock::now();
        if (now >= deadline) return 0;
        if (deadline == Clock::time_point::max()) return cap;
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline-now).count();
        return left >= static_cast<std::chrono::milliseconds::rep>(cap) ? cap : static_cast<unsigned>(left);
    }
};
}
