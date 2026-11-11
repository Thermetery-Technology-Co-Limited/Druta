/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
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
struct Device { std::string slot, name, uuid; };
struct LiveData {
    dc_gpu_identity identity{};
    dc_telemetry telemetry{};
    bool telemetry_valid = false;
    std::string telemetry_error;
    std::array<ControlState, static_cast<size_t>(ControlId::count)> controls{};
    std::optional<double> enforced_power_w;
    std::vector<dc_vf_point> curve;
    bool curve_writable = false;
    bool curve_reset_writable = false;
    uint32_t curve_step_khz = 0;
    uint32_t curve_freq_div = 0;
    std::string curve_error;
    bool connected = false;
    std::string status;
    bool undo_available = false;
    std::string undo_label;
};
struct Command {
    enum class Operation { apply, stock, undo, save_profile, load_profile, apply_curve, reset_curve };
    Operation operation = Operation::apply;
    ControlId control = ControlId::memory;
    double value = 0;
    std::wstring profile_path;
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
    virtual std::vector<Device> enumerate() = 0;
    virtual bool open(const std::string& slot, std::string& error) = 0;
    virtual void close() = 0;
    virtual LiveData read(bool details) = 0;
    virtual ActionResult execute(const Command& command) = 0;
};
std::unique_ptr<IBackend> create_core_backend(const std::wstring& exact_dll_path);
}
