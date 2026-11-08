// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// A recursive mutex without libstdc++'s thread model: CRITICAL_SECTION on
// Windows (XP and later, re-entrant like Python's RLock), std::recursive_mutex
// elsewhere. The MinGW-w64 toolchain's win32 thread model has no std::mutex.
#pragma once

#if defined(_WIN32)
#include <windows.h>
#else
#include <mutex>
#endif

namespace druta {

class Mutex {
public:
#if defined(_WIN32)
    Mutex() { ::InitializeCriticalSection(&section_); }
    ~Mutex() { ::DeleteCriticalSection(&section_); }
    void lock() { ::EnterCriticalSection(&section_); }
    void unlock() { ::LeaveCriticalSection(&section_); }
#else
    Mutex() = default;
    ~Mutex() = default;
    void lock() { mutex_.lock(); }
    void unlock() { mutex_.unlock(); }
#endif
    Mutex(const Mutex&) = delete;
    Mutex& operator=(const Mutex&) = delete;

private:
#if defined(_WIN32)
    CRITICAL_SECTION section_;
#else
    std::recursive_mutex mutex_;
#endif
};

class LockGuard {
public:
    explicit LockGuard(Mutex& mutex) : mutex_(mutex) { mutex_.lock(); }
    ~LockGuard() { mutex_.unlock(); }
    LockGuard(const LockGuard&) = delete;
    LockGuard& operator=(const LockGuard&) = delete;

private:
    Mutex& mutex_;
};

}  // namespace druta
