/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "i2c_driver.h"
#include <drutacore/drutacore.h>
#include <functional>
namespace druta::ui {
struct I2cVerifyHoldApi {
    decltype(&dc_gpu_architecture) architecture=nullptr;
    decltype(&dc_gpu_vf_lock_status) vf_status=nullptr;
    decltype(&dc_gpu_clk_lock_status) clock_status=nullptr;
    decltype(&dc_gpu_vf_lock_entry_get) vf_entry=nullptr;
    decltype(&dc_gpu_vf_lock_set) vf_set=nullptr;
    decltype(&dc_gpu_vf_lock_recovery_pending) vf_pending=nullptr;
    decltype(&dc_gpu_vf_lock_recovery_get) vf_recovery_get=nullptr;
    decltype(&dc_gpu_vf_lock_recovery_set) vf_recovery_set=nullptr;
    decltype(&dc_gpu_vf_lock_recover) vf_recover=nullptr;
    decltype(&dc_gpu_vf_curve_read) curve=nullptr;
    decltype(&dc_gpu_read_vcore_mv) vcore=nullptr;
    decltype(&dc_legacy_p0_supported) legacy_supported=nullptr;
    decltype(&dc_legacy_p0_owned) legacy_owned=nullptr;
    decltype(&dc_legacy_p0_request) legacy_request=nullptr;
    decltype(&dc_legacy_p0_release) legacy_release=nullptr;
    decltype(&dc_gpu_read_telemetry) telemetry=nullptr;
    std::function<bool()> bound;
    std::function<void(unsigned)> sleep;
};
class I2cVerifyHold {
public:
    I2cVerifyHold(I2cVerifyHoldApi api, dc_gpu* gpu);
    // loaded_voltage_mv must come from a completed, settled workload on this GPU.
    bool prepare(std::string& reason, bool require_stable_clocks=true,
                 bool require_loaded_voltage=false, std::optional<double> loaded_voltage_mv={});
    std::optional<ControllerOperatingPoint> check() const;
    // Call only after controller restoration. A false result retains ownership.
    bool release(std::string& reason);
    bool owned() const { return owned_; }
private:
    enum class Kind { none, vf, legacy };
    I2cVerifyHoldApi api_; dc_gpu* gpu_; Kind kind_=Kind::none;
    bool owned_=false; dc_vf_point_lock held_{}; dc_vf_lock_recovery recovery_{};
    bool bound() const;
};
}
