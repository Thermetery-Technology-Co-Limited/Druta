// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Bounded waits inside readback loops. Tests replace the sleeper so a loop
// that gives a driver two seconds to settle does not take two seconds.
#pragma once

#include <cstdint>

#include "core/calling.h"

namespace druta {

using SleepFn = void(DRUTA_CDECL*)(std::uint32_t milliseconds);
void sleep_ms(std::uint32_t milliseconds);
void set_sleep_hook(SleepFn hook);  // nullptr restores the real sleep

// A monotonic clock in seconds (Python's time.monotonic()): QueryPerformanceCounter on
// Windows (kernel32, XP and up), CLOCK_MONOTONIC elsewhere. Tests replace it so a retry
// cadence can be stepped instead of waited for (Python patches time.monotonic).
using ClockFn = double(DRUTA_CDECL*)();
double monotonic_seconds();
void set_clock_hook(ClockFn hook);  // nullptr restores the real clock

}  // namespace druta
