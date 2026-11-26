/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "clock_effect_verify.h"
#include "drutacore/drutacore.h"
#include "vendor/json/json.hpp"

#include <charconv>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <windows.h>

namespace {
using namespace druta::ui;
using Json = nlohmann::ordered_json;

volatile LONG cancel_requested = 0;

BOOL WINAPI request_cancel(DWORD event) {
    if (event != CTRL_C_EVENT && event != CTRL_BREAK_EVENT) return FALSE;
    InterlockedExchange(&cancel_requested, 1);
    return TRUE;
}

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

unsigned parse_domain(std::string_view argument) {
    unsigned domain = 0;
    const auto end = argument.data() + argument.size();
    const auto parsed = std::from_chars(argument.data(), end, domain);
    require(parsed.ec == std::errc{} && parsed.ptr == end && domain < DC_CLKDOM_SLOTS,
            "Expected a decimal clock-control index in range");
    return domain;
}

dc_gpu_identity paired_identity(dc_gpu* gpu, const std::string& slot, const std::string& uuid) {
    dc_gpu_identity identity{};
    identity.size = sizeof(identity);
    require(dc_gpu_identity_get(gpu, &identity) == DC_OK && identity.nvml_ok && identity.nvapi_ok &&
            identity.nvapi_selected && identity.nvml_selected && !identity.pairing_error[0] &&
            slot == identity.nvapi_slot && slot == identity.nvml_slot && slot == identity.slot &&
            uuid == identity.uuid, "Slot and UUID must match the paired adapter exactly");
    return identity;
}

Json inventory(dc_gpu* gpu) {
    dc_clkdom_layout layout{};
    layout.size = sizeof(layout);
    require(dc_gpu_clkdom_layout(gpu, &layout) == DC_OK, "Clock layout is unavailable");
    dc_clkdom_requests requests{};
    requests.size = sizeof(requests);
    require(dc_gpu_clkdom_requests(gpu, &requests) == DC_OK && requests.count <= DC_CLKDOM_SLOTS,
            "Clock request rows are unavailable");
    Json out = {{"layout", layout.name}, {"layout_version", layout.version}, {"controls", Json::array()}};
    for (unsigned i = 0; i < requests.count; ++i) {
        const auto& row = requests.rows[i];
        char label[128]{};
        size_t needed = 0;
        dc_outcome capability{};
        require(dc_gpu_clkdom_control_label(gpu, row.domain, label, sizeof(label), &needed) == DC_OK &&
                needed <= sizeof(label), "Clock label lookup failed");
        require(dc_gpu_clkdom_offset_write_capability(gpu, row.domain, row.mode, &capability) == DC_OK,
                "Clock write capability lookup failed");
        uint32_t mask = 0;
        dc_outcome pattern{};
        require(dc_gpu_clkdom_known_offset_pattern(gpu, row.domain, row.mode, &mask, &pattern) == DC_OK,
                "Clock pattern lookup failed");
        Json groups = Json::array();
        for (uint32_t domain = 0; domain < DC_PRIV_DOMAINS; ++domain)
            if (mask & (uint32_t{1} << domain)) groups.push_back(domain);
        out["controls"].push_back({{"domain", row.domain}, {"label", label}, {"mode", row.mode},
            {"offset_khz", row.freq_khz}, {"structurally_writable", capability.ok == 1},
            {"reason", capability.message}, {"known_pattern", pattern.ok == 1},
            {"known_counter_domains", std::move(groups)}, {"pattern_detail", pattern.message}});
    }
    dc_telemetry snapshot{};
    snapshot.size = sizeof(snapshot);
    out["pstate"] = nullptr;
    if (dc_gpu_read_telemetry(gpu, &snapshot) == DC_OK && (snapshot.present & DC_T_PSTATE))
        out["pstate"] = snapshot.pstate;
    return out;
}

ClockEffectApi clock_api() {
    ClockEffectApi api{};
    api.identity = dc_gpu_identity_get;
    api.telemetry = dc_gpu_read_telemetry;
    api.classify = dc_clock_domains_classify;
    api.public_clocks = dc_gpu_public_clock_readings;
    api.measurements = dc_gpu_clkdom_measurements;
    api.requests = dc_gpu_clkdom_requests;
    api.layout = dc_gpu_clkdom_layout;
    api.capability = dc_gpu_clkdom_offset_write_capability;
    api.set = dc_gpu_clkdom_offset_set;
    api.lock = dc_gpu_lock;
    api.unlock = dc_gpu_unlock;
    api.wait_ms = [](unsigned ms) {
        Sleep(ms);
        if (InterlockedCompareExchange(&cancel_requested, 0, 0))
            throw std::runtime_error("Clock verification cancelled");
    };
    api.architecture = dc_gpu_architecture;
    return api;
}

Json request_rows(const dc_clkdom_requests& requests) {
    Json rows = Json::array();
    require(requests.count <= DC_CLKDOM_SLOTS, "Invalid recorded clock row count");
    for (unsigned i = 0; i < requests.count; ++i) {
        const auto& row = requests.rows[i];
        rows.push_back({{"domain", row.domain}, {"mode", row.mode}, {"offset_khz", row.freq_khz},
            {"msvdd_uv", row.has_msvdd_uv ? Json(row.msvdd_uv) : Json(nullptr)}});
    }
    return rows;
}

template<size_t N>
Json sample_windows(const std::array<std::vector<ClockEffectSample>, N>& phases, const char* const (&names)[N]) {
    Json out = Json::object();
    for (size_t i = 0; i < phases.size(); ++i) {
        auto& samples = out[names[i]];
        samples = Json::array();
        for (const auto& sample : phases[i]) {
            Json sources = Json::object();
            for (const auto& [domain, source_scale] : sample.private_source_scale)
                sources[std::to_string(domain)] = {{"source_id", source_scale.first}, {"scale", source_scale.second}};
            samples.push_back({{"pstate", sample.pstate}, {"counters_khz", sample.counters_khz},
                {"counter_groups", sample.groups}, {"private_counter_sources", std::move(sources)},
                {"requests", request_rows(sample.requests)}});
        }
    }
    return out;
}

Json verdicts(const std::vector<ClockEffectChange>& changes) {
    Json out = Json::array();
    for (const auto& change : changes) {
        out.push_back({{"counter", change.counter}, {"baseline_khz", change.baseline_khz},
            {"applied_khz", change.applied_khz}, {"restored_khz", change.restored_khz},
            {"delta_khz", change.delta_khz}, {"reversible", change.reversible},
            {"independent", change.independent}});
    }
    return out;
}

Json verification(const ClockEffectResult& result, const ClockEffectPending& pending) {
    const char* const phases[] = {"baseline", "applied", "restored"};
    const char* const transitions[] = {"after_apply", "after_restore"};
    Json out = {{"attempted", result.attempted}, {"restored", result.restored}, {"verified", result.verified},
        {"completed", result.completed},
        {"message", result.message}, {"phases", sample_windows(result.phases, phases)}, {"changes", verdicts(result.changes)},
        {"transitions", sample_windows(result.transitions, transitions)}, {"coupled", result.coupled},
        {"physical_groups", result.physical_groups}, {"programmed_groups", result.programmed_groups},
        {"affected_groups", result.affected_groups}, {"pending_restoration", nullptr}};
    if (!result.confirmation_phases[0].empty()) {
        out["confirmation_phases"] = sample_windows(result.confirmation_phases, phases);
        out["confirmation_transitions"] = sample_windows(result.confirmation_transitions, transitions);
        out["confirmation_changes"] = verdicts(result.confirmation_changes);
    }
    if (pending.active) {
        out["pending_restoration"] = {{"slot", pending.slot}, {"uuid", pending.uuid},
            {"domain", pending.domain}, {"original_khz", pending.original_khz}, {"test_khz", pending.test_khz},
            {"layout", pending.layout.name}, {"layout_version", pending.layout.version},
            {"original_requests", request_rows(pending.before)}};
    }
    return out;
}
} // namespace

int main(int argc, char** argv) {
    if ((argc != 3 && argc != 5) || (argc == 5 && std::string_view(argv[3]) != "--verify")) {
        std::cerr << "Usage: druta-ui-clock-effect-probe PCI_SLOT EXPECTED_UUID [--verify DOMAIN]\n";
        return 2;
    }
    const std::string slot = argv[1], uuid = argv[2];
    Json report = {{"slot", slot}, {"uuid", uuid}};
    int exit_code = 1;
    try {
        require(!slot.empty() && !uuid.empty(), "Explicit slot and UUID are required");
        const std::optional<unsigned> domain = argc == 5 ? std::optional<unsigned>{parse_domain(argv[4])}
                                                        : std::nullopt;
        dc_gpu* opened = nullptr;
        const auto status = dc_gpu_open(slot.c_str(), &opened);
        std::unique_ptr<dc_gpu, decltype(&dc_gpu_close)> gpu(opened, dc_gpu_close);
        require(status == DC_OK && gpu, "Cannot open requested adapter");
        const auto identity = paired_identity(gpu.get(), slot, uuid);
        report["name"] = identity.name;
        report["driver"] = identity.driver;
        report["vbios"] = identity.vbios;
        report["inventory"] = inventory(gpu.get());
        if (domain) {
            // Ctrl+C runs on a system thread. Only the transaction thread
            // touches the GPU, including restoration after the next wait.
            if (GetConsoleCP() != 0)
                require(SetConsoleCtrlHandler(request_cancel, TRUE) != 0, "Cannot install the cancellation handler");
            report["domain"] = *domain;
            ClockEffectPending pending{};
            ClockEffectResult result{}, recovery{};
            const auto api = clock_api();
            try {
                result = verify_clock_effect(api, gpu.get(), slot, uuid, *domain, pending);
            } catch (const std::exception& error) {
                result.message = error.what();
            } catch (...) {
                result.message = "Clock verification interrupted";
            }
            const bool retry_restore = pending.active;
            if (retry_restore) {
                // Keep the original handle alive for one guarded recovery even
                // when sampling or dispatch raised after the trial was armed.
                Sleep(500);
                try {
                    recovery = restore_clock_effect(api, gpu.get(), pending);
                } catch (const std::exception& error) {
                    recovery.message = error.what();
                } catch (...) {
                    recovery.message = "Clock restoration interrupted";
                }
            }
            report["verification"] = verification(result, pending);
            if (retry_restore) {
                report["restore_retry"] = {{"attempted", recovery.attempted}, {"restored", recovery.restored},
                    {"message", recovery.message}};
            }
            const bool restored = !pending.active && (result.restored || recovery.restored);
            report["restoration_confirmed"] = restored;
            exit_code = result.completed && restored ? 0 : 1;
        } else {
            exit_code = 0;
        }
    } catch (const std::exception& error) {
        report["error"] = error.what();
        exit_code = 1;
    }
    std::cout << report.dump(2) << '\n';
    return exit_code;
}
