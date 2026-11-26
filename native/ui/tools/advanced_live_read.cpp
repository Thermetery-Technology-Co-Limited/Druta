/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Explicit read-only qualification of the Advanced services. No tuning command.
#include "backend.h"
#include "power_arbitration_service.h"
#include "power_integral_service.h"
#include "voltage_policy_service.h"
#include "vbios_override_inventory.h"
#include "model_input_inventory.h"
#include "profile_io.h"
#include <iostream>
#include <stdexcept>
using namespace druta::ui;
namespace {
std::string ascii(const wchar_t* text) {
    std::string result;
    for (; *text; ++text) {
        if (*text < 0x20 || *text > 0x7e) throw std::runtime_error("Expected ASCII slot / UUID");
        result.push_back(static_cast<char>(*text));
    }
    return result;
}
ProfileJson optional_bool(const std::optional<bool>& value) { return value ? ProfileJson(*value) : ProfileJson(nullptr); }
ProfileJson tuning_report(IBackend& backend, const LiveData& before) {
    Command read;
    read.operation = Command::Operation::read_tuning_support;
    const auto result = backend.execute(read);
    if (result.may_have_changed) throw std::runtime_error("Capability inspection reported a mutation");
    const auto data = backend.read(false);
    if (!data.connected || std::string(data.identity.uuid) != before.identity.uuid ||
        std::string(data.identity.slot) != before.identity.slot || data.xoc_known != before.xoc_known ||
        data.xoc_enabled != before.xoc_enabled) throw std::runtime_error("GPU or XOC mode changed during inspection");
    const auto& power = data.controls[static_cast<size_t>(ControlId::power)];
    const auto number = [](std::optional<double> value) { return value ? ProfileJson(*value) : ProfileJson(nullptr); };
    ProfileJson report = {{"slot", data.identity.slot}, {"uuid", data.identity.uuid}, {"name", data.identity.name},
        {"driver", data.identity.driver}, {"vbios", data.identity.vbios}, {"hardware_write_requested", false},
        {"max_it", {{"ready", data.max_it.ready()}, {"fan_percent", data.max_it.fan_percent ? ProfileJson(data.max_it.fan_percent) : ProfileJson(nullptr)},
            {"board_mw", data.max_it.board_mw ? ProfileJson(data.max_it.board_mw) : ProfileJson(nullptr)},
            {"fan_range_source", data.max_it.fan_range_note}, {"reason", data.max_it.reason}}},
        {"board_power", {{"xoc_enabled", data.xoc_known ? ProfileJson(data.xoc_enabled) : ProfileJson(nullptr)},
            {"configured_w", number(power.current)}, {"xoc_max_request_w", number(data.inspected_xoc_max_w)},
            {"discovery", data.inspected_xoc_note}, {"nvml_enforced_w", number(data.enforced_power_w)}}},
        {"firmware", {{"families", ProfileJson::array()}}}, {"ok", result.ok}, {"message", result.message}};
    if (data.model_inputs) {
        const auto& inputs = *data.model_inputs;
        auto& model = report["model_inputs"];
        model = {{"error", inputs.error}, {"units", "raw model units"}, {"rows", ProfileJson::array()}};
        if (inputs.error.empty()) for (uint32_t i = 0; i < DC_MODEL_INPUT_COUNT; ++i) {
            const auto& row = inputs.values.rows[i];
            ProfileJson item = {{"name", model_input_name(i)}, {"status", model_input_status(row.state)}};
            if (row.state == DC_MODEL_READY) {
                item["index"] = row.index; item["active"] = row.active; item["raw_hardware"] = row.raw_hardware;
                item["default"] = row.default_value; item["minimum"] = row.minimum; item["maximum"] = row.maximum;
                item["override"] = row.override_present ? ProfileJson(row.override_value) : ProfileJson(nullptr);
            } else item["reason"] = row.reason;
            model["rows"].push_back(std::move(item));
        }
    }
    if (data.firmware_inventory) {
        report["firmware"]["error"] = data.firmware_inventory->error;
        for (const auto& family : data.firmware_inventory->families)
            report["firmware"]["families"].push_back({{"name", family.name}, {"identified", family.identified}, {"observation", family.observation}});
    }
    return report;
}
}
int wmain(int argc, wchar_t** argv) {
    const bool tuning = argc == 5 && std::wstring(argv[4]) == L"--tuning";
    if (argc != 4 && !tuning) { std::cerr << "Usage: advanced-live-read ABSOLUTE_DLL PCI_SLOT EXPECTED_UUID [--tuning]\n"; return 2; }
    try {
        const auto slot = ascii(argv[2]), uuid = ascii(argv[3]);
        if (slot.empty() || uuid.empty()) throw std::runtime_error("Explicit target is required");
        auto backend = create_core_backend(argv[1]); std::string error;
        if (!backend->open(slot, error)) throw std::runtime_error(error);
        auto data = backend->read(false);
        if (!data.connected || data.identity.uuid != uuid || data.identity.slot != slot)
            throw std::runtime_error("Opened adapter does not match the requested slot and UUID");
        if (tuning) {
            const auto report = tuning_report(*backend, data);
            backend->close();
            std::cout << report.dump(2) << '\n';
            return report.at("ok").get<bool>() ? 0 : 1;
        }
        ProfileJson output = {{"slot", slot}, {"uuid", uuid}, {"actions", ProfileJson::array()}};
        for (auto operation : {Command::Operation::refresh_power_arbitration, Command::Operation::power_integral_read,
                               Command::Operation::voltage_policy_read}) {
            Command command; command.operation = operation;
            const auto result = backend->execute(command);
            if (result.may_have_changed) throw std::runtime_error("Read-only action reported a mutation");
            output["actions"].push_back({{"ok", result.ok}, {"message", result.message}});
        }
        data = backend->read(false);
        if (!data.connected || data.identity.uuid != uuid || data.identity.slot != slot)
            throw std::runtime_error("Target identity unavailable after reads");
        if (data.power_arbitration) {
            const auto& model = *data.power_arbitration;
            output["arbitration"] = {{"freshness", static_cast<int>(model.freshness)}, {"attempts", model.attempts},
                {"status_present", model.status_present}, {"message", model.message}};
            output["arbitration"]["policies"] = ProfileJson::array();
            if (model.report) for (uint32_t i = 0; i < model.report->policy_count; ++i) {
                const auto& row = model.report->policies[i];
                output["arbitration"]["policies"].push_back({{"policy", row.policy}, {"available", !!row.available},
                    {"reason", row.reason}, {"requested_raw", row.requested}, {"effective_raw", row.effective}, {"unit", row.unit}});
            }
        }
        if (data.power_integral) {
            const auto& model = *data.power_integral;
            output["feedback"] = {{"current", model.current}, {"message", model.message}, {"policies", ProfileJson::array()}};
            for (const auto& row : model.rows) output["feedback"]["policies"].push_back({{"policy", row.policy},
                {"available", row.available}, {"reason", row.reason}, {"lower_raw", row.lower}, {"upper_raw", row.upper}});
        }
        if (data.voltage_policies) {
            output["voltage_policies"] = {{"message", data.voltage_policies->message}, {"rows", ProfileJson::array()}};
            const char* names[] = {"admission", "monotonicity", "smoothing"};
            for (size_t i = 0; i < data.voltage_policies->rows.size(); ++i) {
                const auto& row = data.voltage_policies->rows[i];
                output["voltage_policies"]["rows"].push_back({{"name", names[i]}, {"current", optional_bool(row.current)},
                    {"availability", static_cast<int>(row.availability)}, {"writable", row.writable}, {"message", row.message}});
            }
        }
        backend->close(); std::cout << output.dump(2) << '\n'; return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
