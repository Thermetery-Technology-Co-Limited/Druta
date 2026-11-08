// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The per-domain V/F point lock and the NVML frequency-lock records that
// share its 780-byte table. Port of nvbackend.GPU.read_vf_lock_status,
// read_vf_lock, read_clk_lock, read_clk_lock_status, set_vf_lock,
// recover_vf_lock, clear_vf_lock and vf_lock_self_test.
//
// Every write is a read-modify-write of the buffer the driver's own getter
// produced: one entry's lockMode and volt_uV change, every other byte stays.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "backend/gpu.h"
#include "backend/outcome.h"
#include "backend/structs.h"
#include "backend/telemetry.h"

namespace druta::backend {

struct PointLock {
    std::uint32_t domain = 0;
    std::uint32_t lockMode = 0;
    std::uint32_t volt_uV = 0;  // the REQUESTED voltage; the hardware holds the point at or below it
    std::uint32_t count = 0;    // the table's count, as the driver reports it
};

struct ClkLockRange {
    std::uint32_t min_mhz = 0;
    std::uint32_t max_mhz = 0;
};

// (value, error): value and error are never both set; both empty means
// "none, and that is known" (unlocked), as nvbackend's (None, None).
template <typename T>
struct Status {
    std::optional<T> value;
    std::optional<std::string> error;
};

class VfLock {
public:
    VfLock(Gpu& gpu, Telemetry& telemetry) : gpu_(gpu), telemetry_(telemetry) {}

    bool available();                      // _vf_lock_available: both ids and a V/F-curve generation
    std::optional<ClockLock> read_raw();   // _vf_lock_read_raw: the driver's own buffer, version 2

    Status<PointLock> read_vf_lock_status(std::optional<std::int64_t> domain);
    std::optional<PointLock> read_vf_lock();
    std::optional<ClkLockRange> read_clk_lock();
    Status<ClkLockRange> read_clk_lock_status();

    Outcome set_vf_lock(std::int64_t volt_uv, std::optional<std::int64_t> domain);
    bool recovery_pending() const { return recovery_.has_value(); }
    Outcome recover_vf_lock();
    Outcome clear_vf_lock(std::optional<std::int64_t> domain, std::optional<std::int64_t> expected_uv);
    Outcome self_test();

private:
    struct Recovery {
        std::int64_t domain;
        std::pair<std::uint32_t, std::uint32_t> previous;   // (lockMode, volt_uV) before this request
        std::pair<std::uint32_t, std::uint32_t> requested;
    };
    // The Turing R472 fallback reads RM performance-limit records through the
    // escape transport, which the core does not have yet: both answer "not
    // readable" until it lands, exactly as Python does when its capture fails.
    std::optional<ClkLockRange> legacy_clk_lock();
    std::optional<std::vector<std::vector<std::uint32_t>>> legacy_clk_limit_records();
    bool vf_curve_applicable();
    Outcome set_vf_lock_impl(std::int64_t volt_uv, std::optional<std::int64_t> domain);

    Gpu& gpu_;
    Telemetry& telemetry_;
    std::optional<Recovery> recovery_;
};

// nvbackend's VF_LOCK_* envelope, in microvolts.
inline constexpr std::int64_t kVfLockMinUv = 400000;
inline constexpr std::int64_t kVfLockMaxUv = 1300000;
inline constexpr std::uint32_t kVfLockDomain = 6;
inline constexpr std::uint32_t kVfLockModeOff = 0;

}  // namespace druta::backend
