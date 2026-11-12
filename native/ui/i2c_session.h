/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "i2c_verify_hold.h"
#include <drutacore/i2c.h>
#include <functional>
namespace druta::ui {
struct I2cApi {
    decltype(&dc_gpu_identity_get) identity = nullptr;
    decltype(&dc_i2c_read) read = nullptr;
    decltype(&dc_i2c_write) write = nullptr;
    decltype(&dc_i2c_read_bytes) read_bytes = nullptr;
    decltype(&dc_i2c_controller_write) controller_write = nullptr;
    decltype(&dc_gpu_architecture) architecture = nullptr;
    decltype(&dc_gpu_clkdom_flags_get) flags = nullptr;
    decltype(&dc_gpu_lock) lock = nullptr;
    decltype(&dc_gpu_unlock) unlock = nullptr;
    decltype(&dc_gpu_lockable_clocks) clocks = nullptr;
    decltype(&dc_gpu_clocks_lock) clamp = nullptr;
    decltype(&dc_gpu_clocks_reset) release = nullptr;
    decltype(&dc_gpu_clk_lock_status) clock_status = nullptr;
    decltype(&dc_gpu_vf_lock_status) vf_status = nullptr;
    decltype(&dc_gpu_read_telemetry) telemetry = nullptr;
    std::function<void(unsigned)> sleep;
    I2cVerifyHoldApi verify_hold;
};
// Worker-owned: no bus polling from telemetry or rendering. Controller release
// precedes clock release, including close and failure recovery.
class I2cSession final : private I2cTransport, private ControllerIo {
public:
    I2cSession(I2cApi api, dc_gpu* gpu, dc_gpu_identity identity);
    const I2cViewData& data() const { return data_; }
    ActionResult execute(const Command& command);
    ActionResult release();
    bool owned() const { return service_.has_owned_control() || (controller_ && controller_->owned()) || data_.clock_owned || (verify_hold_ && verify_hold_->owned()); }
private:
    I2cApi api_;
    dc_gpu* gpu_;
    dc_gpu_identity identity_;
    I2cControls service_;
    I2cViewData data_;
    std::unique_ptr<ControllerDriver> controller_;
    std::unique_ptr<I2cVerifyHold> verify_hold_;
    std::optional<std::string> captured_preset_, pending_preset_, prepared_preset_;
    bool selection_write_attempted_ = false;
    bool controller_bound() const override { return bound(); }
    bool read_bytes(I2cRoute, uint8_t, size_t, std::vector<uint8_t>&) override;
    bool write_bytes(I2cFamily, I2cRoute, uint8_t, const std::vector<uint8_t>&) override;
    std::optional<uint32_t> architecture() const override;
    std::optional<double> reference_mv() const override;
    std::optional<double> core_clock_mhz() const override;
    std::optional<ControllerOperatingPoint> operating_point() const override;
    void sleep_ms(unsigned ms) override { api_.sleep(ms); }
    bool xoc() const;
    std::unique_ptr<ControllerDriver> driver(I2cFamily, I2cRoute);
    bool select(I2cFamily, I2cRoute, const std::string& recipe = {});
    ActionResult scan(const Command&);
    ActionResult preset(const Command&);
    bool bound() const override;
    bool read(uint8_t port, uint8_t addr, uint8_t reg, uint8_t width, uint32_t& value) override;
    bool write(uint8_t port, uint8_t addr, uint8_t reg, uint8_t width, uint32_t value) override;
    bool clamp(std::string& reason);
    bool clock_request() const;
    std::optional<double> observed_clock() const;
    void refresh();
    void publish_control_profiles();
};
}
