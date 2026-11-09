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

class RmTransport;

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

// One lock-table entry as the driver's getter reports it (verification_p0's target read).
using VfLockEntry = PointLock;

// GPU._vf_lock_recovery: the rollback a change still owes, as (lockMode, volt_uV) pairs.
struct RecoveryRecord {
    std::int64_t domain = 0;
    std::pair<std::uint32_t, std::uint32_t> previous;   // (lockMode, volt_uV) before this request
    std::pair<std::uint32_t, std::uint32_t> requested;
    bool verification = false;  // the record's "verification" key: a verification_p0 hold owns it
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
    // rm: the RM escape transport cache for the Turing R472 fallback; nullptr makes the
    // fallback answer "not readable", as Python does when its capture fails.
    VfLock(Gpu& gpu, Telemetry& telemetry, RmTransport* rm = nullptr) : gpu_(gpu), telemetry_(telemetry), rm_(rm) {}

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

    // verification_p0's accessors. entry(): that domain's entry among the first `count` entries of
    // the driver's own buffer, or nullopt when the getter fails or the domain is absent (the caller
    // treats both alike). recovery() / set_recovery(): read or replace the pending record, as Python
    // marks an existing record "verification" or installs its pending record before
    // recover_vf_lock; nullopt clears it. recover_vf_lock keeps the flag while the record stays
    // pending.
    std::optional<VfLockEntry> entry(std::int64_t domain);
    std::optional<RecoveryRecord> recovery() const { return recovery_; }
    void set_recovery(std::optional<RecoveryRecord> record) { recovery_ = std::move(record); }

private:
    using Recovery = RecoveryRecord;
    // The Turing R472 fallback: RM performance-limit records (GET 0x20802077) through
    // the escape transport captured once from VoltRailsCtlGet; never its paired SET.
    std::optional<ClkLockRange> legacy_clk_lock();  // _read_legacy_clk_lock

public:
    // _legacy_clk_limit_records: [2][82] words, or nullopt (the cached transport is then dropped).
    std::optional<std::vector<std::vector<std::uint32_t>>> legacy_clk_limit_records();

private:
    bool vf_curve_applicable();
    Outcome set_vf_lock_impl(std::int64_t volt_uv, std::optional<std::int64_t> domain);

    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport* rm_;
    std::optional<Recovery> recovery_;
};

// nvbackend's VF_LOCK_* envelope, in microvolts.
inline constexpr std::int64_t kVfLockMinUv = 400000;
inline constexpr std::int64_t kVfLockMaxUv = 1300000;
inline constexpr std::uint32_t kVfLockDomain = 6;
inline constexpr std::uint32_t kVfLockModeOff = 0;

}  // namespace druta::backend
