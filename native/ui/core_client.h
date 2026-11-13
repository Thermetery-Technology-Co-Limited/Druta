/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "rail_controls.h"
#include "i2c_session.h"
#include "perf_view_model.h"
#include "power_arbitration_service.h"
#include "power_integral_service.h"
#include "voltage_policy_service.h"
#include "voltage_tables_service.h"
#include "voltage_limits_service.h"
#include "power_saving_service.h"
#include "clock_gating_service.h"
namespace druta::ui {
// Injected table also permits deterministic tests without loading a driver.
struct CoreApi {
    FrequencyLockApi frequency{};
    PerfViewApi perf{};
    PowerArbitrationApi arbitration{};
    PowerIntegralApi power_integral{};
    VoltagePolicyApi voltage_policy{};
    VoltageTablesApi voltage_tables{};
    VoltageLimitsApi voltage_limits{};
    PowerSavingApi power_saving{};
    ClockGatingApi clock_gating{};
    TuningApi tuning{};
    decltype(&dc_gpu_reset_all) reset_all = nullptr;
    I2cApi i2c{};
    PlannerApi planner{};
    RailApi rails{};
    decltype(&dc_abi_version) abi_version = nullptr;
    decltype(&dc_enumerate_gpus) enumerate = nullptr;
    decltype(&dc_gpu_open) open = nullptr;
    decltype(&dc_gpu_close) close = nullptr;
    decltype(&dc_gpu_identity_get) identity = nullptr;
    decltype(&dc_gpu_read_telemetry) telemetry = nullptr;
    decltype(&dc_clock_domains_classify) clock_classify = nullptr;
    decltype(&dc_gpu_capabilities_get) capabilities = nullptr;
    decltype(&dc_gpu_clock_offset_range_get) range = nullptr;
    decltype(&dc_gpu_mem_offset_scale) scale = nullptr;
    decltype(&dc_gpu_memory_offset_step_units) step = nullptr;
    decltype(&dc_gpu_clock_step_khz) clock_step = nullptr;
    decltype(&dc_gpu_max_rise_khz) max_rise = nullptr;
    decltype(&dc_gpu_clock_offset_set) set_offset = nullptr;
    decltype(&dc_request_envelopes_get) envelopes = nullptr;
    decltype(&dc_gpu_vf_curve_reset) reset_curve = nullptr;
    decltype(&dc_gpu_vf_delta_snapshot) snapshot = nullptr;
    decltype(&dc_gpu_vf_deltas_apply) apply_deltas = nullptr;
    decltype(&dc_gpu_fan_set) fan_set = nullptr;
    decltype(&dc_gpu_fan_reset) fan_reset = nullptr;
    decltype(&dc_gpu_fan_restore) fan_restore = nullptr;
    decltype(&dc_gpu_voltage_boost_set) boost_set = nullptr;
    decltype(&dc_gpu_fan_state_get) fan_state = nullptr;
    decltype(&dc_gpu_fan_capabilities_get) fan_caps = nullptr;
    decltype(&dc_gpu_voltage_boost_get) boost = nullptr;
    decltype(&dc_gpu_vf_layout) curve_layout = nullptr;
    decltype(&dc_gpu_clkdom_ok) clkdom_ok = nullptr;
    decltype(&dc_gpu_clkdom_layout) clkdom_layout = nullptr;
    decltype(&dc_gpu_clkdom_domains) clkdom_domains = nullptr;
    decltype(&dc_gpu_clkdom_controls_for_ui) clkdom_controls = nullptr;
    decltype(&dc_gpu_clkdom_offset_slider_offered) clkdom_offered = nullptr;
    decltype(&dc_gpu_clkdom_control_label) clkdom_label = nullptr;
    decltype(&dc_gpu_clkdom_requests) clkdom_requests = nullptr;
    decltype(&dc_gpu_clkdom_offset_set) clkdom_set = nullptr;
    decltype(&dc_gpu_rail_offset_capability) rail_cap = nullptr;
    decltype(&dc_gpu_rail_offset_read) rail_read = nullptr;
    decltype(&dc_gpu_rail_offset_set) rail_set = nullptr;
    decltype(&dc_gpu_msvdd_offset_reset) msvdd_reset = nullptr;
    decltype(&dc_gpu_clkdom_flags_set) clkdom_flags_set = nullptr;
    decltype(&dc_gpu_clkdom_flags_get) clkdom_flags = nullptr;
    decltype(&dc_gpu_vf_curve_read) curve = nullptr;
    decltype(&dc_gpu_lock) lock = nullptr;
    decltype(&dc_gpu_unlock) unlock = nullptr;
    decltype(&dc_pl_bounds) power_bounds = nullptr;
    decltype(&dc_pl_set) power_set = nullptr;
    decltype(&dc_pl_message_get) power_message = nullptr;
    decltype(&dc_cl_get_current_limits) current_limits = nullptr;
    decltype(&dc_cpc_live_read) cpc_live_read = nullptr;
    decltype(&dc_cl_power_policies_read) policy_read = nullptr;
    decltype(&dc_cl_set_power_policy) policy_set = nullptr;
    decltype(&dc_cl_policy_message_get) policy_message = nullptr;
    decltype(&dc_cl_pins_get) pins_get = nullptr;
    decltype(&dc_cl_pins_set) pins_set = nullptr;
    decltype(&dc_pl_read) power = nullptr;
    decltype(&dc_pl_enforced_read) enforced_power = nullptr;
};
std::unique_ptr<IBackend> create_core_backend_for_test(CoreApi api);
}
