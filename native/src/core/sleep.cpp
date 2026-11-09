// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/sleep.h"

#if defined(_WIN32)
#include <windows.h>
#else
#include <time.h>
#endif

namespace druta {
namespace {
SleepFn hook = nullptr;
ClockFn clock_hook = nullptr;
}

void set_sleep_hook(SleepFn fn) { hook = fn; }
void set_clock_hook(ClockFn fn) { clock_hook = fn; }

void sleep_ms(std::uint32_t milliseconds) {
    if (hook) {
        hook(milliseconds);
        return;
    }
#if defined(_WIN32)
    ::Sleep(milliseconds);
#else
    timespec ts{};
    ts.tv_sec = milliseconds / 1000;
    ts.tv_nsec = static_cast<long>(milliseconds % 1000) * 1000000L;
    ::nanosleep(&ts, nullptr);
#endif
}

double monotonic_seconds() {
    if (clock_hook) return clock_hook();
#if defined(_WIN32)
    LARGE_INTEGER frequency, counter;
    if (!::QueryPerformanceFrequency(&frequency) || frequency.QuadPart == 0 || !::QueryPerformanceCounter(&counter))
        return static_cast<double>(::GetTickCount()) / 1000.0;
    return static_cast<double>(counter.QuadPart) / static_cast<double>(frequency.QuadPart);
#else
    timespec ts{};
    ::clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
#endif
}

}  // namespace druta
