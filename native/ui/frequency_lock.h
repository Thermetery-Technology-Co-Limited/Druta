// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <drutacore/drutacore.h>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct FrequencyLockApi {
    decltype(&dc_gpu_lock) lock=nullptr;
    decltype(&dc_gpu_unlock) unlock=nullptr;
    decltype(&dc_gpu_identity_get) identity=nullptr;
    decltype(&dc_gpu_architecture) architecture=nullptr;
    decltype(&dc_export_address) export_address=nullptr;
    decltype(&dc_gpu_capabilities_get) capabilities=nullptr;
    decltype(&dc_gpu_lockable_clocks) lockable_clocks=nullptr;
    decltype(&dc_gpu_clk_lock_status) clock_status=nullptr;
    decltype(&dc_gpu_clocks_lock) clocks_lock=nullptr;
    decltype(&dc_gpu_clocks_last_lock) last_lock=nullptr;
    decltype(&dc_gpu_clocks_reset) clocks_reset=nullptr;
    decltype(&dc_legacy_p0_supported) legacy_supported=nullptr;
    decltype(&dc_legacy_p0_owned) legacy_owned=nullptr;
    decltype(&dc_legacy_p0_hold) legacy_hold=nullptr;
    decltype(&dc_legacy_p0_release) legacy_release=nullptr;
};
struct FrequencyClockRow { uint32_t memory_mhz=0;std::vector<uint32_t> graphics_mhz; };
struct FrequencyRange {
    uint32_t minimum=0,maximum=0;
    bool operator==(const FrequencyRange& other) const {return minimum==other.minimum && maximum==other.maximum;}
};
enum class FrequencyLockMode { none, nvml, legacy_p0 };
struct FrequencyLockOwnership {
    FrequencyLockMode mode=FrequencyLockMode::none;
    bool confirmed=false,recovery=false;
    std::optional<FrequencyRange> range;
    bool needs_cleanup() const {return mode!=FrequencyLockMode::none || recovery;}
};
struct FrequencyLockData {
    std::optional<uint32_t> architecture,minimum_mhz,maximum_mhz;
    std::vector<FrequencyClockRow> rows;
    bool tables_current=false,nvml_capable=false;
    bool legacy_known=false,legacy_available=false;
    bool current_known=false;
    std::optional<FrequencyRange> current;
    FrequencyLockOwnership ownership;
    std::string message;
};
struct FrequencyLockResult {bool ok=false,may_have_changed=false;std::string message;};
// One worker-owned service per opened GPU handle. The caller must release its
// I2C clamp/controller and V/F hold/headroom before acquiring a different lock;
// abort handover if either owner has uncertain release/recovery. This service
// handles its own NVML <-> legacy-P0 transition, never foreign lock restoration.
// No destructor writes, persistence, saved-profile replay or automatic reapply.
class FrequencyLock {
public:
    FrequencyLock(FrequencyLockApi api,dc_gpu* gpu,const dc_gpu_identity& target);
    FrequencyLockData read();
    FrequencyLockResult apply(int64_t minimum_mhz,int64_t maximum_mhz);
    FrequencyLockResult lock_max();
    FrequencyLockResult hold_legacy_p0();
    FrequencyLockResult release();
    const FrequencyLockData& data() const {return data_;}
    bool needs_cleanup() const {return data_.ownership.needs_cleanup();}
private:
    FrequencyLockApi api_;dc_gpu* gpu_;dc_gpu_identity target_{};
    FrequencyLockData data_;
    std::vector<FrequencyRange> candidates_;
    bool pending_dispatch_=false,unexpected_acceptance_=false;
    std::optional<FrequencyRange> dispatched_;
    bool identity_ok();
    bool read_clock(dc_clk_lock_status& status);
    bool read_legacy_owned(int32_t& owned);
    bool learn_accepted(FrequencyRange& accepted);
    bool discover();
    void observe_clock(const dc_clk_lock_status& status);
    FrequencyLockResult release_locked();
};
}
