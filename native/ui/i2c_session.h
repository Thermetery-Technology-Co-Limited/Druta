/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include <drutacore/i2c.h>
#include <functional>
namespace druta::ui {
struct I2cApi {
    decltype(&dc_gpu_identity_get) identity = nullptr;
    decltype(&dc_i2c_read) read = nullptr;
    decltype(&dc_i2c_write) write = nullptr;
    decltype(&dc_gpu_lock) lock = nullptr;
    decltype(&dc_gpu_unlock) unlock = nullptr;
    decltype(&dc_gpu_lockable_clocks) clocks = nullptr;
    decltype(&dc_gpu_clocks_lock) clamp = nullptr;
    decltype(&dc_gpu_clocks_reset) release = nullptr;
    decltype(&dc_gpu_clk_lock_status) clock_status = nullptr;
    decltype(&dc_gpu_vf_lock_status) vf_status = nullptr;
    decltype(&dc_gpu_read_telemetry) telemetry = nullptr;
    std::function<void(unsigned)> sleep;
};
// Worker-owned: no bus polling from telemetry or rendering. Controller release
// precedes clock release, including close and failure recovery.
class I2cSession final : private I2cTransport {
public:
    I2cSession(I2cApi api, dc_gpu* gpu, dc_gpu_identity identity);
    const I2cViewData& data() const { return data_; }
    ActionResult execute(const Command& command);
    ActionResult release();
    bool owned() const { return service_.has_owned_control() || data_.clock_owned; }
private:
    I2cApi api_;
    dc_gpu* gpu_;
    dc_gpu_identity identity_;
    I2cControls service_;
    I2cViewData data_;
    bool bound() const override;
    bool read(uint8_t port, uint8_t addr, uint8_t reg, uint8_t width, uint32_t& value) override;
    bool write(uint8_t port, uint8_t addr, uint8_t reg, uint8_t width, uint32_t value) override;
    bool clamp(std::string& reason);
    bool clock_request() const;
    std::optional<double> observed_clock() const;
    void refresh();
};
}
