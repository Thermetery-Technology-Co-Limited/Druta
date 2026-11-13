/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "i2c_controls.h"
#include <memory>
#include <array>
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
// Exact DAC command bytes. Configuration is an identity/scaling guard only;
// saved profiles never write it. Both zero encodings remain distinct.
struct Nct3933State { std::array<uint8_t,3> outputs{}; uint8_t configuration=0; };
struct Nct3933PlanState;
struct Nct3933ReceiptState;
struct Nct3933Plan { std::shared_ptr<const Nct3933PlanState> state; };
struct Nct3933Receipt { std::shared_ptr<Nct3933ReceiptState> state; };
// Semantic compound controls from released profiles. Disabled commands and
// independent performance-state offsets are retained, never scalarized.
struct Up9512rControl { std::array<int32_t,5> offsets_mv{}; bool enabled=false; };
struct Up9512rPlanState;
struct Up9512rReceiptState;
struct Up9512rPlan { std::shared_ptr<const Up9512rPlanState> state; };
struct Up9512rReceipt { std::shared_ptr<Up9512rReceiptState> state; };
struct Ncp4206State { bool enabled=false; uint8_t command=0; };
struct Ncp4206PlanState;
struct Ncp4206ReceiptState;
struct Ncp4206Plan { std::shared_ptr<const Ncp4206PlanState> state; };
struct Ncp4206Receipt { std::shared_ptr<Ncp4206ReceiptState> state; };
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
    virtual I2cResult capture_nct3933(Nct3933State&) { return {false,false,false,"Exact NCT3933 profile control unavailable"}; }
    virtual I2cResult preview_nct3933(const Nct3933State&,uint8_t,Nct3933Plan&) { return {false,false,false,"Exact NCT3933 profile control unavailable"}; }
    virtual I2cResult apply_nct3933(const Nct3933Plan&,const ControllerContext&,const std::function<void(const Nct3933Receipt&)>&) { return {false,false,false,"Exact NCT3933 profile control unavailable"}; }
    virtual I2cResult verify_nct3933(const Nct3933Plan&) { return {false,false,false,"Exact NCT3933 profile control unavailable"}; }
    virtual I2cResult verify_nct3933(const Nct3933Receipt&,bool=false) { return {false,false,false,"Exact NCT3933 profile control unavailable"}; }
    virtual I2cResult restore_nct3933(const Nct3933Receipt&) { return {false,false,false,"Exact NCT3933 profile control unavailable"}; }
    virtual I2cResult capture_up9512r(Up9512rControl&) { return {false,false,false,"Exact uP9512R profile control unavailable"}; }
    virtual I2cResult preview_up9512r(const Up9512rControl&,const ControllerContext&,Up9512rPlan&) { return {false,false,false,"Exact uP9512R profile control unavailable"}; }
    virtual I2cResult apply_up9512r(const Up9512rPlan&,const ControllerContext&,const std::function<void(const Up9512rReceipt&)>&) { return {false,false,false,"Exact uP9512R profile control unavailable"}; }
    virtual I2cResult verify_up9512r(const Up9512rPlan&) { return {false,false,false,"Exact uP9512R profile control unavailable"}; }
    virtual I2cResult verify_up9512r(const Up9512rReceipt&,bool=false) { return {false,false,false,"Exact uP9512R profile control unavailable"}; }
    virtual I2cResult restore_up9512r(const Up9512rReceipt&) { return {false,false,false,"Exact uP9512R profile control unavailable"}; }
    virtual I2cResult capture_ncp4206(Ncp4206State&) { return {false,false,false,"Exact NCP4206 profile control unavailable"}; }
    virtual I2cResult preview_ncp4206(const Ncp4206State&,bool,Ncp4206Plan&) { return {false,false,false,"Exact NCP4206 profile control unavailable"}; }
    virtual I2cResult apply_ncp4206(const Ncp4206Plan&,const ControllerContext&,const std::function<void(const Ncp4206Receipt&)>&) { return {false,false,false,"Exact NCP4206 profile control unavailable"}; }
    virtual I2cResult verify_ncp4206(const Ncp4206Plan&) { return {false,false,false,"Exact NCP4206 profile control unavailable"}; }
    virtual I2cResult verify_ncp4206(const Ncp4206Receipt&,bool=false) { return {false,false,false,"Exact NCP4206 profile control unavailable"}; }
    virtual I2cResult restore_ncp4206(const Ncp4206Receipt&) { return {false,false,false,"Exact NCP4206 profile control unavailable"}; }
};
std::unique_ptr<ControllerDriver> make_nct3933(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_ncp4206(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_up9512r(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_icx(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_mp29816(ControllerIo&, I2cRoute);
std::unique_ptr<ControllerDriver> make_mp2888_offset(ControllerIo&, I2cRoute);
}
