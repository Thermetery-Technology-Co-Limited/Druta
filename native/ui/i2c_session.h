/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "i2c_verify_hold.h"
#include "i2c_route_cache.h"
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
    // Called immediately before acquiring a new clock/verification hold.
    // A false result leaves the selected controller and clock untouched.
    std::function<bool(std::string&)> before_clock_acquire;
    // Worker read-only discovery only. Host owns cancellation atomic and
    // publishes progress tagged with its current session generation.
    std::function<bool()> discovery_cancelled;
    std::function<void(const I2cDiscoveryProgress&)> discovery_progress;
    std::wstring route_cache_path;
    I2cVerifyHoldApi verify_hold;
};
struct IcxProfilePlanState;
struct IcxProfileReceiptState;
struct IcxProfilePlan { std::shared_ptr<const IcxProfilePlanState> state; };
struct IcxProfileReceipt { std::shared_ptr<IcxProfileReceiptState> state; };
struct I2cProfilePlanState;
struct I2cProfileReceiptState;
struct I2cProfilePlan { std::shared_ptr<const I2cProfilePlanState> state; };
struct I2cProfileReceipt { std::shared_ptr<I2cProfileReceiptState> state; };
// Worker-owned: no bus polling from telemetry or rendering. Controller release
// precedes clock release, including close and failure recovery.
class I2cSession final : private I2cTransport, private ControllerIo {
public:
    I2cSession(I2cApi api, dc_gpu* gpu, dc_gpu_identity identity);
    const I2cViewData& data() const { return data_; }
    ActionResult execute(const Command& command);
    ActionResult release();
    // In-memory iCX transaction. The host qualifies composition dependencies;
    // session/driver receipts own only issued controller and clamp lifecycles.
    ActionResult preflight_icx_profile(const ProfileJson& document,IcxProfilePlan& out);
    ActionResult apply_icx_profile(const IcxProfilePlan&,const std::function<void(const IcxProfileReceipt&)>&);
    ActionResult verify_icx_profile(const IcxProfilePlan&);
    ActionResult verify_icx_profile(const IcxProfileReceipt&,bool original=false);
    ActionResult restore_icx_profile(const IcxProfileReceipt&);
    // Explicit selected legacy NCT profile. Opaque session/driver plans bind
    // exact output bytes and comparison-only configuration; no clock hold.
    ActionResult preflight_i2c_profile(const ProfileJson&,I2cProfilePlan&);
    ActionResult apply_i2c_profile(const I2cProfilePlan&,const std::function<void(const I2cProfileReceipt&)>&);
    ActionResult verify_i2c_profile(const I2cProfilePlan&);
    ActionResult verify_i2c_profile(const I2cProfileReceipt&,bool original=false);
    ActionResult restore_i2c_profile(const I2cProfileReceipt&);
    bool owned() const { return bool(icx_receipt_) || service_->has_owned_control() || (controller_ && controller_->owned()) || data_.clock_owned || (verify_hold_ && verify_hold_->owned()); }
private:
    I2cApi api_;
    dc_gpu* gpu_;
    dc_gpu_identity identity_;
    std::unique_ptr<I2cControls> service_;
    I2cViewData data_;
    std::unique_ptr<ControllerDriver> controller_;
    std::unique_ptr<I2cVerifyHold> verify_hold_;
    std::optional<std::string> captured_preset_, pending_preset_, prepared_preset_;
    std::shared_ptr<IcxProfileReceiptState> icx_receipt_;
    uint64_t clock_epoch_=0;
    bool icx_plan_bound(const IcxProfilePlanState&) const;
    bool release_icx_clock(IcxProfileReceiptState&,std::string&,bool&);
    bool profile_bound(const I2cProfilePlanState&) const;
    const std::shared_ptr<const uint8_t> profile_owner_=std::make_shared<const uint8_t>(uint8_t{0});
    std::shared_ptr<I2cProfileReceiptState> profile_receipt_;
    bool selection_write_attempted_ = false;
    bool discovery_readonly_ = false;
    bool discovery_cancel_allowed_ = true;
    I2cDiscoveryProgress discovery_;
    void cancellation_boundary() const;
    void progress();
    void remember_route();
    bool controller_bound() const override { return bound(); }
    bool read_bytes(I2cRoute, uint8_t, size_t, std::vector<uint8_t>&) override;
    bool write_bytes(I2cFamily, I2cRoute, uint8_t, const std::vector<uint8_t>&) override;
    std::optional<uint32_t> architecture() const override;
    std::optional<double> reference_mv() const override;
    std::optional<double> core_clock_mhz() const override;
    std::optional<ControllerOperatingPoint> operating_point() const override;
    void sleep_ms(unsigned ms) override;
    bool xoc() const;
    std::unique_ptr<ControllerDriver> driver(I2cFamily, I2cRoute);
    bool select(I2cFamily, I2cRoute, const std::string& recipe = {},bool cancellable=true);
    ActionResult scan(const Command&);
    ActionResult preset(const Command&);
    bool bound() const override;
    bool read(uint8_t port, uint8_t addr, uint8_t reg, uint8_t width, uint32_t& value) override;
    bool write(uint8_t port, uint8_t addr, uint8_t reg, uint8_t width, uint32_t value) override;
    bool clamp(std::string& reason,const std::function<void(uint32_t)>& before_request={});
    bool clock_request() const;
    std::optional<double> observed_clock() const;
    void refresh(bool read_controller=true);
    void publish_control_profiles();
};
}
