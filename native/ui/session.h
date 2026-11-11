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
    std::vector<Device> devices;
    std::string selected_slot, status;
    LiveData data;
    bool busy = false, opening = false, paused = false;
    std::uint64_t telemetry_stale_mask = 0;
    bool curve_stale = false;
    std::uint64_t curve_revision = 0;
    bool fan_readings_stale = false;
    std::vector<HistorySample> history;
    std::vector<LogEntry> log;
};
class Session {
public:
    explicit Session(std::unique_ptr<IBackend> backend, std::function<void()> notify);
    ~Session();
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    void start(const std::string& slot = {});
    bool select_device(const std::string& slot);
    void request_poll(bool details = false);
    void set_paused(bool paused);
    bool submit(Command command, std::uint64_t expected_generation);
    std::shared_ptr<const SessionSnapshot> snapshot() const;
    void begin_shutdown();
    bool finished() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
