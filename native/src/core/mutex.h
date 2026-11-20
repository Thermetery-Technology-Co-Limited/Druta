// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// A recursive mutex without libstdc++'s thread model: CRITICAL_SECTION on
// Windows (XP and later, re-entrant like Python's RLock), std::recursive_mutex
// elsewhere. The MinGW-w64 toolchain's win32 thread model has no std::mutex.
//
// Ownership is per OS thread and survives between separate calls: a thread
// that took the mutex through one C ABI call (dc_gpu_lock) still owns it in
// the next one, and every entry's LockGuard on that thread only adds a level.
//
// The mutex counts two kinds of level separately, both written only by the
// holding thread under the native lock:
//   - guard levels (lock / try_lock / unlock): a LockGuard's, taken and given
//     back in one scope on one thread; unlock() releases only a guard level;
//   - exported levels (lock_exported / try_lock_exported / unlock_exported):
//     the ones a caller takes through dc_gpu_lock / dc_gpu_trylock.
// unlock_exported refuses (false, nothing changed) unless the calling thread
// owns the mutex and holds at least one exported level, so a stray
// dc_gpu_unlock inside a core callback can never give back the level of the
// entry that is running. Neither path ever calls the platform unlock without
// a level of its own kind, so neither can underflow a count or reach
// LeaveCriticalSection / std::recursive_mutex::unlock as a non-owner (that is
// undefined behaviour there).
//
// The owner is recorded with the identity the platform primitive itself uses,
// so the owner check and the primitive always agree: GetCurrentThreadId on
// Windows (CRITICAL_SECTION's OwningThread), the kernel thread id on Linux
// (glibc's recursive pthread mutex records it), std::thread::id elsewhere. It
// is written only by the holding thread and read without the lock. A thread
// reads its own id there only while it holds the mutex, because no other live
// thread has that id. A thread must not exit while it holds a level: the
// mutex then stays held for good, as any mutex would, and an operating system
// that gives a later thread the same id (Windows reuses ids) makes that
// thread the owner for the primitive and for this check alike.
#pragma once

#include <atomic>
#include <cstdint>

#if defined(_WIN32)
#include <windows.h>
#else
#include <mutex>
#if defined(__linux__)
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>
#else
#include <thread>
#endif
#endif

namespace druta {

class Mutex {
public:
#if defined(_WIN32)
    using ThreadId = DWORD;  // GetCurrentThreadId; 0 is never a thread's id
    Mutex() { ::InitializeCriticalSection(&section_); }
    ~Mutex() { ::DeleteCriticalSection(&section_); }
#elif defined(__linux__)
    using ThreadId = pid_t;  // the kernel thread id; 0 is never a thread's id
    Mutex() = default;
    ~Mutex() = default;
#else
    using ThreadId = std::thread::id;  // a default-constructed id is no thread's
    Mutex() = default;
    ~Mutex() = default;
#endif
    Mutex(const Mutex&) = delete;
    Mutex& operator=(const Mutex&) = delete;

    // Guard levels (LockGuard). Block until the calling thread holds the mutex; recursive per thread.
    void lock() {
        native_lock();
        acquired(guard_);
    }

    // Take a guard level when that does not wait: true when the calling thread now holds the mutex
    // (one more level when it already did), false when another thread holds it.
    bool try_lock() {
        if (!native_try_lock()) return false;
        acquired(guard_);
        return true;
    }

    // Release one guard level. A LockGuard holds one by construction; a call without one (which no
    // guard makes) changes nothing rather than reaching the platform unlock.
    void unlock() { (void)release(guard_); }

    // Exported levels (dc_gpu_lock / dc_gpu_trylock / dc_gpu_unlock), counted apart from the guards'.
    void lock_exported() {
        native_lock();
        acquired(exported_);
    }

    bool try_lock_exported() {
        if (!native_try_lock()) return false;
        acquired(exported_);
        return true;
    }

    // Release one exported level: false, with nothing changed, unless the calling thread holds the
    // mutex with at least one exported level (another thread holds it, none does, or the caller
    // holds only guard levels, as inside a core callback).
    bool unlock_exported() { return release(exported_); }

    // The calling thread holds the mutex at any level, guard or exported.
    bool owned_by_caller() const { return owner_.load(std::memory_order_acquire) == self(); }

private:
#if defined(_WIN32)
    static ThreadId self() { return ::GetCurrentThreadId(); }
    static ThreadId nobody() { return 0; }
    void native_lock() { ::EnterCriticalSection(&section_); }
    bool native_try_lock() { return ::TryEnterCriticalSection(&section_) != 0; }  // XP and later
    void native_unlock() { ::LeaveCriticalSection(&section_); }
#elif defined(__linux__)
    static ThreadId self() { return static_cast<ThreadId>(::syscall(SYS_gettid)); }
    static ThreadId nobody() { return 0; }
    void native_lock() { mutex_.lock(); }
    bool native_try_lock() { return mutex_.try_lock(); }
    void native_unlock() { mutex_.unlock(); }
#else
    static ThreadId self() { return std::this_thread::get_id(); }
    static ThreadId nobody() { return ThreadId(); }
    void native_lock() { mutex_.lock(); }
    bool native_try_lock() { return mutex_.try_lock(); }
    void native_unlock() { mutex_.unlock(); }
#endif

    // Under the native lock, on the holding thread.
    void acquired(std::uint64_t& kind) {
        if (guard_ + exported_ == 0) owner_.store(self(), std::memory_order_release);
        ++kind;
    }
    // The counts are read only after the owner check: only the owner writes them.
    bool release(std::uint64_t& kind) {
        if (!owned_by_caller() || kind == 0) return false;
        --kind;
        if (guard_ + exported_ == 0) owner_.store(nobody(), std::memory_order_release);
        native_unlock();
        return true;
    }

#if defined(_WIN32)
    CRITICAL_SECTION section_;
#else
    std::recursive_mutex mutex_;
#endif
    std::atomic<ThreadId> owner_{nobody()};
    std::uint64_t guard_ = 0;     // written only by the holding thread, under the native lock
    std::uint64_t exported_ = 0;  // likewise
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
