// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "backend.h"
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {
struct HistorySample {
    double time = 0;
    std::optional<double> core_mhz, temp_c, power_w;
};
struct LogEntry { std::uint64_t id = 0; std::string message; bool ok = false; };
struct SessionSnapshot {
    std::uint64_t generation = 0, revision = 0;
    std::uint64_t completed_reads = 0, completed_actions = 0;
    std::uint64_t last_action_log_id = 0; // exact result entry for last_action_operation
    std::optional<Command::Operation> last_action_operation;
    bool last_action_ok = false;
    std::vector<Device> devices;
    std::string selected_slot, status;
    LiveData data;
    bool busy = false, opening = false, paused = false;
    bool close_refused = false;
    bool i2c_ownership_uncertain = false;
    bool tuning_ownership_uncertain = false;
    std::uint64_t telemetry_stale_mask = 0;
    bool curve_stale = false;
    std::uint64_t curve_revision = 0;
    bool fan_readings_stale = false;
    // Per ABI fan index and independent field. The bool above is a summary,
    // never authority to mark a simultaneously fresh duty/RPM value stale.
    std::uint32_t fan_duty_stale_mask = 0, fan_rpm_stale_mask = 0;
    std::vector<HistorySample> history;
    std::vector<LogEntry> log;
};
class Session {
public:
    explicit Session(std::unique_ptr<IBackend> backend, std::function<void()> notify);
    ~Session();
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    // An expected UUID requires an explicit slot and remains enforced until
    // the user explicitly selects a device.
    void start(const std::string& slot = {}, const std::string& expected_uuid = {});
    bool select_device(const std::string& slot);
    // Retry the current slot while retaining any startup UUID constraint.
    bool reconnect();
    void request_poll(bool details = false);
    void set_paused(bool paused);
    bool submit(Command command, std::uint64_t expected_generation);
    std::shared_ptr<const SessionSnapshot> snapshot() const;
    void begin_shutdown();
    bool finished() const;
private:
    bool change_device(const std::string& slot, bool preserve_constraint);
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
