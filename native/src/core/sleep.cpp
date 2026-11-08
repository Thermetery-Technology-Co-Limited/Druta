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
}

void set_sleep_hook(SleepFn fn) { hook = fn; }

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

}  // namespace druta
