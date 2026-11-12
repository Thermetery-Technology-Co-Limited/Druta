/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
#include "curve_planner.h"
#include "i2c_controls.h"
#include <array>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {
enum class ControlId { core, memory, power, fan, boost, count };
enum class Availability { unknown, available, unsupported, transient };
struct ControlState {
    Availability availability = Availability::unknown;
    bool writable = false;
    bool stock_writable = false;
    std::optional<double> current, low, high, stock, step;
    std::string unit, detail;
};
enum class AdvancedKind { clock_domain, nvvdd, msvdd };
struct AdvancedId { AdvancedKind kind = AdvancedKind::clock_domain; uint32_t domain = 0; };
struct AdvancedControl { AdvancedId id; std::string label; ControlState state; std::string binding; };
struct RailId { uint32_t rail = 0; uint32_t field = 0; };
struct RailControl { RailId id; ControlState state; std::optional<double> estimated_base_mv; std::string binding; };
struct PowerPolicy { uint32_t id = 0; std::string label; bool named_current = false; bool core_current = false; ControlState state; std::string binding; };
struct Device { std::string slot, name, uuid; };
struct I2cViewData {
    bool available = false, gate = false, verified = false, clock_owned = false;
    uint32_t clock_mhz = 0;
    std::vector<I2cCandidate> candidates;
    std::optional<I2cRoute> selected;
    I2cStatus status;
    std::string message;
};
struct LiveData {
    I2cViewData i2c;
    dc_gpu_identity identity{};
    dc_telemetry telemetry{};
    bool telemetry_valid = false;
    std::string telemetry_error;
    std::array<ControlState, static_cast<size_t>(ControlId::count)> controls{};
    std::optional<double> enforced_power_w;
    std::vector<AdvancedControl> advanced_controls;
    std::vector<PowerPolicy> power_policies;
    std::vector<RailControl> rail_controls;
    bool rail_mode_known = false, rail_mode_enabled = false;
    bool rail_writable_known = false, rail_writable = false;
    std::vector<dc_vf_point> curve;
    bool curve_writable = false;
    bool curve_reset_writable = false;
    uint32_t curve_step_khz = 0;
    uint32_t curve_freq_div = 0;
    int64_t curve_max_abs_delta_khz = 0;
    uint32_t curve_model_max_rise_khz = 0; // core software grid-derived bound, 0 when unavailable
    std::string curve_error;
    bool xoc_known = false;
    bool xoc_enabled = false;
    bool connected = false;
    std::string status;
    bool undo_available = false;
    std::string undo_label;
};
struct Command {
    enum class Operation { apply, stock, undo, save_profile, load_profile, apply_curve, reset_curve, set_xoc, set_rail_mode,
        i2c_scan, i2c_select, i2c_gate, i2c_read, i2c_verify, i2c_apply, i2c_release };
    Operation operation = Operation::apply;
    ControlId control = ControlId::memory;
    double value = 0;
    bool enabled = false;
    std::optional<I2cRoute> i2c_route;
    std::wstring profile_path;
    std::optional<AdvancedId> advanced;
    std::optional<uint32_t> power_policy;
    std::optional<RailId> rail;
    std::optional<std::string> expected_binding;
    std::vector<dc_vf_point> expected_curve;
    uint32_t expected_curve_freq_div = 0;
    std::vector<dc_vf_delta> curve_deltas;
};
struct ActionResult {
    bool ok = false;
    bool may_have_changed = false;
    std::string message;
    std::optional<double> requested, readback;
};
class IBackend {
public:
    virtual ~IBackend() = default;
    virtual PlannerApi planner_api() const { return {}; }
    virtual std::vector<Device> enumerate() = 0;
    virtual bool open(const std::string& slot, std::string& error) = 0;
    virtual void close() = 0;
    virtual ActionResult prepare_close() { ActionResult r; r.ok = true; return r; }
    virtual std::optional<I2cViewData> i2c_state() const { return {}; } // worker state only, no hardware reads
    virtual LiveData read(bool details) = 0;
    virtual ActionResult execute(const Command& command) = 0;
};
std::unique_ptr<IBackend> create_core_backend(const std::wstring& exact_dll_path);
}
