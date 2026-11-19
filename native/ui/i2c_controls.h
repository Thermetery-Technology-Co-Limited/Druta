/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
// Implementations verify the paired adapter/handle and NVAPI packet geometry on
// every transfer. False after dispatch is an uncertain result, not a retry cue.
class I2cTransport {
public:
    virtual ~I2cTransport() = default;
    virtual bool bound() const = 0;
    virtual bool read(uint8_t port, uint8_t addr7, uint8_t reg, uint8_t width, uint32_t& value) = 0;
    virtual bool write(uint8_t port, uint8_t addr7, uint8_t reg, uint8_t width, uint32_t value) = 0;
};
struct I2cRoute { uint8_t port = 0, addr7 = 0; };
struct Mp2888Control {
    I2cRoute route{};
    uint32_t page = 0, address = 0, command = 0, mode = 0, operation = 0, pwm = 0;
    uint32_t offset = 0, sense = 0, fine = 0, minimum = 0, maximum = 0;
};
struct I2cCandidate {
    I2cRoute route{};
    double controller_vout_mv = 0;
    std::string description = "MP2888A candidate; fingerprint is not a unique model ID; physical rail unassigned";
    uint32_t family = 5; // I2cFamily::mp2888; retained last for existing aggregate callers.
};
struct I2cStatus {
    bool request_eligible = false; // understood current context, independently of the user gate
    bool readable = false, stale = false, capable = false, writable = false, enabled = false, owned = false, recovery_pending = false, response_verified = false;
    std::optional<double> requested_mv, controller_vout_mv, min_mv, max_mv;
    std::string message;
};
struct I2cResult { bool ok = false, may_have_changed = false, recovery_pending = false; std::string message; };
struct I2cVerifyCallbacks {
    std::function<std::optional<double>()> core_clock_mhz; // validates caller-owned low clock
    std::function<void(unsigned)> sleep_ms;
    std::function<bool()> cancelled;
};
// One service belongs to one selected GPU handle and ordered worker. Scan and
// selection do not write. The caller owns clock hold and response verification;
// release the rail and verify restoration before releasing that clock hold.
class I2cControls {
public:
    explicit I2cControls(I2cTransport& transport);
    static constexpr double nominal_step_mv = 6.25;
    // Presentation proposals on the existing 9-bit nominal-command grid;
    // these are not measured voltages or device defaults.
    static std::vector<double> nominal_presets(double low_mv, double high_mv);
    std::vector<I2cCandidate> scan();
    bool select(I2cRoute route);
    void set_gate(bool enabled) { gate_ = enabled; }
    I2cResult verify(bool clock_held, const I2cVerifyCallbacks& callbacks,
                     const std::function<void(const Mp2888Control&)>& before_write);
    I2cStatus status();
    I2cResult apply(double target_mv, bool clock_held, const std::function<void(const Mp2888Control&)>& before_write);
    I2cResult restore(bool clock_held);
    bool has_owned_control() const { return owned_; }
private:
    I2cTransport& transport_;
    std::optional<I2cRoute> route_;
    bool gate_ = false, verified_ = false, owned_ = false, recovery_pending_ = false;
    std::optional<Mp2888Control> initial_, current_owned_;
    std::optional<I2cStatus> last_status_;
    std::vector<Mp2888Control> permitted_;
    bool read(uint8_t reg, uint8_t width, uint32_t& value);
    bool fingerprint(I2cRoute route, double& vout);
    bool capture(Mp2888Control& out);
    I2cResult transition(const Mp2888Control& from, const Mp2888Control& target);
    I2cResult restore_impl(bool clock_held, bool release_ownership);
    I2cResult apply_impl(double target_mv, bool clock_held, const std::function<void(const Mp2888Control&)>& before_write, bool verification);
};
}
