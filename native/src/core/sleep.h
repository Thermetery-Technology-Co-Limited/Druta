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
// Windows (kernel32, XP and up), CLOCK_MONOTONIC elsewhere.
double monotonic_seconds();

}  // namespace druta
