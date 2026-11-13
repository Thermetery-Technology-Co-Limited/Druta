/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
#include "curve_planner.h"
#include "i2c_controls.h"
#include "i2c_driver.h"
#include "i2c_route_cache.h"
#include "tuning_actions.h"
#include "frequency_lock.h"
#include <array>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {
struct TimingViewData;
struct ClockMonitorData;
struct ScopedSnapshot;
struct ScopedProfileRequest;
struct PerfViewModel;
struct PowerArbitrationModel;
struct PowerIntegralData;
struct VoltagePolicyData;
struct VoltagePolicyRequest;
struct VoltageTablesData;
struct VoltageTableRequest;
struct VoltageTableKey;
struct VoltageLimitsData;
struct VoltageLimitRequest;
struct VoltageLimitKey;
struct TimingEditorRequest;
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
struct I2cControlProfile {
    std::string id, label, description;
    bool available = false;
    double step_mv = 0;
    std::vector<double> presets_mv;
};
struct I2cViewData {
    I2cDiscoveryProgress discovery;
    bool available = false, gate = false, verified = false, clock_owned = false;
    uint32_t clock_mhz = 0;
    std::vector<I2cCandidate> candidates;
    std::optional<I2cRoute> selected;
    I2cStatus status;
    std::string message;
    std::vector<I2cControlProfile> control_profiles;
    std::string control_profile;
    I2cFamily family = I2cFamily::mp2888;
    ControllerView controller;
    uint64_t selection_revision = 0;
    bool needs_clock_clamp = true;
    bool captured_preset = false, pending_preset = false;
    std::string preset_preview;
    uint64_t prepared_preset_revision = 0;
};
struct LiveData {
    FrequencyLockData frequency;
    std::shared_ptr<const PerfViewModel> perf_diagnostics;
    std::shared_ptr<const PowerArbitrationModel> power_arbitration;
    std::shared_ptr<const PowerIntegralData> power_integral;
    std::shared_ptr<const VoltagePolicyData> voltage_policies;
    std::shared_ptr<const VoltageTablesData> voltage_tables;
    std::shared_ptr<const VoltageLimitsData> voltage_limits;
    // Explicitly captured profile catalog; ordinary polling does not recapture it.
    std::shared_ptr<const ScopedSnapshot> scoped_profiles;
    std::shared_ptr<const ClockMonitorData> clock_monitor;
    TuningState tuning;
    bool max_it_available = false, reset_controls_available = false, hold_available = false;
    std::shared_ptr<const TimingViewData> timings;
    std::optional<uint32_t> timing_top_memory_mhz;
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
    dc_vf_layout curve_layout{};
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
        i2c_scan, i2c_select, i2c_gate, i2c_read, i2c_verify, i2c_apply,
        i2c_recipe, i2c_reset, i2c_prepare_profile, i2c_save_profile, i2c_load_profile, i2c_stage_captured, i2c_apply_profile, i2c_release,
        timings_select_helper, timings_read, timings_preview, timings_apply, timings_undo,
        max_it, reset_controls, hold_curve_point, release_hold, save_curve_profile, i2c_zero_nct_outputs,
        prepare_scoped_profile, save_scoped_profile, apply_scoped_profile, refresh_perf_diagnostics,
        frequency_lock_apply, frequency_lock_max, frequency_legacy_p0_max_fan, frequency_lock_release, frequency_lock_refresh,
        timings_preview_batch, timings_apply_batch, timings_restore_initial, timings_save_profile,
        refresh_power_arbitration, power_integral_read, power_integral_apply, power_integral_undo, power_integral_restore,
        voltage_policy_read, voltage_policy_apply, voltage_policy_restore, voltage_policy_undo, voltage_policy_recover,
        preflight_catalog_profile, apply_catalog_profile,
        voltage_tables_read, voltage_tables_apply, voltage_tables_restore, voltage_tables_undo, voltage_tables_recover, voltage_tables_save,
        voltage_limits_read, voltage_limits_apply, voltage_limits_restore, voltage_limits_rom, voltage_limits_undo, voltage_limits_recover, voltage_limits_save,
        timings_capture_p0, timings_release_capture_hold };
    Operation operation = Operation::apply;
    ControlId control = ControlId::memory;
    double value = 0;
    bool enabled = false;
    std::optional<I2cRoute> i2c_route;
    I2cFamily i2c_family = I2cFamily::unknown;
    std::string i2c_recipe, i2c_field;
    bool i2c_full_scan = false;
    uint64_t i2c_selection_revision = 0;
    uint64_t i2c_preset_revision = 0;
    std::wstring profile_path;
    std::shared_ptr<const ScopedProfileRequest> scoped_profile;
    std::shared_ptr<const ProfileJson> catalog_profile;
    std::wstring timing_helper_path;
    std::shared_ptr<const TimingEditorRequest> timing_editor;
    std::string timing_field;
    uint32_t timing_value = 0;
    uint64_t timing_generation = 0;
    int64_t frequency_min_mhz = 0, frequency_max_mhz = 0;
    uint32_t integral_policy = 0, integral_raw = 0;
    bool integral_upper = false;
    std::shared_ptr<const VoltagePolicyRequest> voltage_policy;
    uint32_t voltage_families = 3;
    std::shared_ptr<const std::vector<VoltageTableRequest>> voltage_table_requests;
    std::shared_ptr<const std::vector<VoltageTableKey>> voltage_table_selection;
    std::shared_ptr<const std::vector<VoltageLimitRequest>> voltage_limit_requests;
    std::shared_ptr<const std::vector<VoltageLimitKey>> voltage_limit_selection;
    double curve_cap_mv = 1100, curve_floor_mv = 1000;
    uint32_t hold_point_index = 0;
    bool hold_headroom = true;
    double headroom_margin_mv = 25;
    std::optional<AdvancedId> advanced;
    std::optional<uint32_t> power_policy;
    std::optional<RailId> rail;
    std::optional<std::string> expected_binding;
    // Comparison-only preconditions for a multi-control transaction, never
    // state to replay. Existing individual commands leave them unset.
    std::optional<int64_t> expected_request_raw;
    std::optional<std::string> expected_power_state;
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
inline bool is_i2c_operation(Command::Operation operation) {
    return (operation >= Command::Operation::i2c_scan && operation <= Command::Operation::i2c_release)
        || operation == Command::Operation::i2c_zero_nct_outputs;
}
inline bool is_frequency_operation(Command::Operation operation) {
    return operation >= Command::Operation::frequency_lock_apply && operation <= Command::Operation::frequency_lock_refresh;
}
inline bool is_timing_operation(Command::Operation operation) {
    return (operation >= Command::Operation::timings_select_helper && operation <= Command::Operation::timings_undo) ||
        (operation >= Command::Operation::timings_preview_batch && operation <= Command::Operation::timings_save_profile) ||
        operation==Command::Operation::timings_capture_p0 || operation==Command::Operation::timings_release_capture_hold;
}
class IBackend {
public:
    virtual ~IBackend() = default;
    virtual PlannerApi planner_api() const { return {}; }
    // Installed before the worker starts. Only read-only discovery observes
    // cancellation; restoration and hardware writes always finish normally.
    virtual void configure_i2c_discovery(std::function<bool()>, std::function<void(const I2cDiscoveryProgress&)>, const std::wstring&) {}
    virtual std::vector<Device> enumerate() = 0;
    virtual bool open(const std::string& slot, std::string& error) = 0;
    virtual void close() = 0;
    virtual ActionResult prepare_close() { ActionResult r; r.ok = true; return r; }
    virtual std::optional<I2cViewData> i2c_state() const { return {}; } // worker state only, no hardware reads
    virtual bool maintenance_needed() const { return false; }
    virtual std::optional<TuningState> tuning_state() const { return {}; }
    virtual std::optional<FrequencyLockData> frequency_state() const { return {}; }
    virtual ActionResult maintain() { ActionResult r; r.ok = true; return r; }
    virtual LiveData read(bool details) = 0;
    virtual ActionResult execute(const Command& command) = 0;
};
std::unique_ptr<IBackend> create_core_backend(const std::wstring& exact_dll_path);
}
