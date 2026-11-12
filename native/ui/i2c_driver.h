/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "i2c_controls.h"
#include <memory>
#include <utility>

namespace druta::ui {
enum class I2cFamily : uint32_t { unknown, nct3933, ncp4206, up9512r, icx, mp2888, mp29816 };
struct I2cField {
    std::string key, label, unit;
    std::optional<double> value, minimum, maximum, step;
    bool writable = false, stale = false, resettable = false;
    std::string reset_label = "Reset", detail;
    std::vector<std::pair<std::string, double>> choices;
};
struct ControllerView {
    bool readable = false, stale = false, capable = false, verified = false;
    bool owned = false, recovery = false, needs_verification = true;
    std::string name, message;
    std::vector<I2cField> fields;
    std::vector<std::pair<std::string, std::string>> readings;
};
struct ControllerOperatingPoint {
    int pstate = -1;
    std::optional<double> core_mhz, memory_mhz;
};
// Implemented by the selected-GPU session. Every transaction revalidates target
// pairing and packet geometry; the family-specific writers enforce their own
// volatile register contracts. No automatic retry of an uncertain SET.
class ControllerIo {
public:
    virtual ~ControllerIo() = default;
    virtual bool controller_bound() const = 0;
    virtual bool read_bytes(I2cRoute, uint8_t reg, size_t count, std::vector<uint8_t>& out) = 0;
    virtual bool write_bytes(I2cFamily, I2cRoute, uint8_t reg, const std::vector<uint8_t>& bytes) = 0;
    virtual std::optional<uint32_t> architecture() const = 0; // NVML generation enum (Ampere = 7).
    virtual std::optional<double> reference_mv() const = 0;
    virtual std::optional<double> core_clock_mhz() const = 0;
    // Present only while the selected GPU's V/F or clock hold is freshly
    // confirmed. P-state is mandatory; clocks may be diagnostic-only.
    virtual std::optional<ControllerOperatingPoint> operating_point() const { return {}; }
    virtual void sleep_ms(unsigned) = 0;
};
struct ControllerContext { bool gate = false, xoc = false; };
struct ControllerRequest {
    std::string key;
    double value = 0;
    bool reset = false;
};
class ControllerDriver {
public:
    virtual ~ControllerDriver() = default;
    virtual bool probe() = 0; // Read-only identity/layout check.
    virtual ControllerView read(bool xoc) = 0;
    virtual I2cResult verify(const ControllerContext&) = 0;
    virtual I2cResult apply(const ControllerRequest&, const ControllerContext&) = 0;
    virtual I2cResult restore() = 0;
    virtual bool owned() const = 0;
    virtual bool needs_clock_clamp() const { return false; }
};
std::unique_ptr<ControllerDriver> make_nct3933(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_ncp4206(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_up9512r(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_icx(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_mp29816(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_mp2888_offset(ControllerIo&, I2cRoute);
}
