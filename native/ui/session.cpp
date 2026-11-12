// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include <windows.h>
#include <algorithm>
#include <utility>

namespace druta::ui {
namespace {
struct Guard {
    CRITICAL_SECTION* cs;
    explicit Guard(CRITICAL_SECTION& c) : cs(&c) { EnterCriticalSection(cs); }
    ~Guard() { LeaveCriticalSection(cs); }
};
double now_seconds() {
    LARGE_INTEGER count{}, frequency{};
    QueryPerformanceCounter(&count);
    QueryPerformanceFrequency(&frequency);
    return frequency.QuadPart ? static_cast<double>(count.QuadPart) / frequency.QuadPart : 0;
}
// Keep the last reported scalar for absent summary fields and mark it stale.
void merge_telemetry(const LiveData& old, LiveData& next, std::uint64_t& stale) {
    auto& n = next.telemetry;
    const auto& o = old.telemetry;
    const std::uint64_t fresh = next.telemetry_valid ? n.present : 0;
    const std::uint64_t previous = old.telemetry_valid ? o.present : 0;
    stale = previous & ~fresh;
#define KEEP(bit, field) if ((stale & (bit)) != 0) n.field = o.field
    KEEP(DC_T_CORE, core); KEEP(DC_T_MEM, mem); KEEP(DC_T_VIDEO, video); KEEP(DC_T_XBAR, xbar);
    KEEP(DC_T_CORE_OFF, core_off); KEEP(DC_T_MEM_OFF, mem_off);
    if (stale & DC_T_PRIV_CLOCKS) std::copy(std::begin(o.priv_clocks), std::end(o.priv_clocks), std::begin(n.priv_clocks));
    KEEP(DC_T_TEMP_EDGE, temp_edge); KEEP(DC_T_TEMP_HOTSPOT, temp_hotspot); KEEP(DC_T_TEMP_DELTA, temp_delta);
    KEEP(DC_T_PL_REQUESTED, pl_requested_mw); KEEP(DC_T_PL_REQUESTED, pl_from_live);
    KEEP(DC_T_POWER_W, power_w); KEEP(DC_T_PL_NOW, pl_now_mw); KEEP(DC_T_PWR_GPU_PCT, pwr_gpu_pct);
    KEEP(DC_T_PWR_BOARD_PCT, pwr_board_pct); KEEP(DC_T_PL_TARGET_PCT, pl_target_pct); KEEP(DC_T_VCORE_MV, vcore_mv);
    KEEP(DC_T_NUM_FANS, num_fans); KEEP(DC_T_UTIL_GPU, util_gpu); KEEP(DC_T_UTIL_FB, util_fb);
    KEEP(DC_T_UTIL_VID, util_vid); KEEP(DC_T_UTIL_BUS, util_bus); KEEP(DC_T_EVENT_MASK, event_mask);
    KEEP(DC_T_PERF_DECREASE, perf_decrease); KEEP(DC_T_PCIE_GEN, pcie_gen); KEEP(DC_T_PCIE_WIDTH, pcie_width);
    if (stale & DC_T_PCIE_ERR) { std::copy(std::begin(o.pcie_err), std::end(o.pcie_err), std::begin(n.pcie_err)); n.pcie_err_total = o.pcie_err_total; n.pcie_err_since = o.pcie_err_since; }
    KEEP(DC_T_PSTATE, pstate); KEEP(DC_T_VBOOST, vboost_pct); KEEP(DC_T_CORE_P0MAX, core_p0max);
    KEEP(DC_T_MEM_P0MAX, mem_p0max); KEEP(DC_T_ENERGY_J, energy_j); KEEP(DC_T_VF_LOCK_MV, vf_lock_mv);
    if (stale & DC_T_VF_LOCKED) { n.vf_locked_count = o.vf_locked_count; std::copy(std::begin(o.vf_locked_domains), std::end(o.vf_locked_domains), std::begin(n.vf_locked_domains)); }
    if (stale & DC_T_CLK_LOCK) { n.clk_lock_min_mhz = o.clk_lock_min_mhz; n.clk_lock_max_mhz = o.clk_lock_max_mhz; }
#undef KEEP
    n.present = fresh | stale;
    next.telemetry_valid = next.telemetry_valid || old.telemetry_valid;
}
void merge_controls(const LiveData& old, LiveData& next) {
    for (size_t i = 0; i < next.controls.size(); ++i) {
        auto& c = next.controls[i];
        const auto& previous = old.controls[i];
        if ((c.availability == Availability::unknown || c.availability == Availability::transient) &&
            (previous.availability == Availability::available ||
             previous.availability == Availability::transient) && previous.current) {
            const auto reason = c.detail;
            c = previous;
            c.availability = Availability::transient;
            c.detail = "Read unavailable; showing last known state";
            if (!reason.empty() && reason.find("Read unavailable; showing last known state") != 0)
                c.detail += ": " + reason;
        }
    }
    for (auto& row : next.advanced_controls) {
        const auto found = std::find_if(old.advanced_controls.begin(), old.advanced_controls.end(),
            [&](const AdvancedControl& prior) { return prior.id.kind == row.id.kind && prior.id.domain == row.id.domain; });
        if (found == old.advanced_controls.end()) continue;
        if (!row.binding.empty() && row.binding != found->binding) continue;
        auto& c = row.state;
        const auto& previous = found->state;
        if ((c.availability == Availability::unknown || c.availability == Availability::transient) && previous.current) {
            const auto reason = c.detail;
            const bool writable = c.writable, stock = c.stock_writable;
            c = previous;
            c.writable = writable; c.stock_writable = stock;
            c.availability = Availability::transient;
            c.detail = "Read unavailable; showing last known state";
            if (!reason.empty() && reason.find("Read unavailable; showing last known state") != 0)
                c.detail += ": " + reason;
        }
    }
}
}
struct Session::Impl {
    mutable CRITICAL_SECTION lock{};
    HANDLE wake = nullptr, thread = nullptr;
    std::unique_ptr<IBackend> backend;
    std::function<void()> notify;
    std::shared_ptr<const SessionSnapshot> published = std::make_shared<SessionSnapshot>();
    SessionSnapshot state;
    bool started = false, stopping = false, done = false, switch_pending = false;
    bool poll_pending = false, details_pending = false, command_pending = false;
    bool backend_opened = false;
    unsigned read_retries = 0;
    Command command{};
    std::uint64_t next_log = 0;
    std::string desired_slot;
    explicit Impl(std::unique_ptr<IBackend> b, std::function<void()> n) : backend(std::move(b)), notify(std::move(n)) {
        InitializeCriticalSection(&lock);
        wake = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    }
    ~Impl() { if (thread) CloseHandle(thread); if (wake) CloseHandle(wake); DeleteCriticalSection(&lock); }
    void publish_locked() { ++state.revision; published = std::make_shared<SessionSnapshot>(state); }
    void signal() { if (wake) SetEvent(wake); }
    void announce() noexcept { try { if (notify) notify(); } catch (...) {} }
    static DWORD WINAPI entry(void* p) { static_cast<Impl*>(p)->run(); return 0; }
    void log_locked(bool ok, const std::string& message) {
        state.log.push_back({++next_log, message, ok});
        if (state.log.size() > 200) state.log.erase(state.log.begin());
    }
    bool read_and_publish(std::uint64_t generation, bool details, bool initial = false, bool finish_action = false) {
        LiveData fresh;
        bool failed = false;
        try { fresh = backend->read(details); }
        catch (...) { failed = true; }
        Guard guard(lock);
        if (stopping || generation != state.generation) return false;
        ++state.completed_reads;
        if (failed) {
            fresh = state.data;
            fresh.connected = false;
            fresh.telemetry_valid = false;
            fresh.telemetry_error = "Read failed";
            fresh.status = "Read failed";
            if (details) fresh.curve_error = "Read failed";
            for (auto& c : fresh.controls) c.availability = Availability::transient;
            for (auto& row : fresh.advanced_controls) row.state.availability = Availability::transient;
            for (auto& row : fresh.power_policies) row.state.availability = Availability::transient;
            for (auto& row : fresh.rail_controls) row.state.availability = Availability::transient;
        }
        if (!fresh.connected) {
            fresh.telemetry_valid = false;
            for (auto& c : fresh.controls)
                if (c.availability == Availability::available) c.availability = Availability::transient;
            for (auto& row : fresh.advanced_controls)
                if (row.state.availability == Availability::available) row.state.availability = Availability::transient;
            for (auto& row : fresh.power_policies)
                if (row.state.availability == Availability::available) row.state.availability = Availability::transient;
            for (auto& row : fresh.rail_controls)
                if (row.state.availability == Availability::available) row.state.availability = Availability::transient;
            if (details && fresh.curve_error.empty()) fresh.curve_error = "Read unavailable";
        }
        const auto raw = fresh.telemetry;
        const bool valid = fresh.telemetry_valid;
        if (!valid && state.data.telemetry_valid) {
            fresh.telemetry.fan_count = state.data.telemetry.fan_count;
            std::copy(std::begin(state.data.telemetry.fans), std::end(state.data.telemetry.fans),
                      std::begin(fresh.telemetry.fans));
            state.fan_readings_stale = true;
        } else if (valid) state.fan_readings_stale = false;
        if (initial) state.opening = false;
        if (finish_action) state.busy = false;
        merge_telemetry(state.data, fresh, state.telemetry_stale_mask);
        merge_controls(state.data, fresh);
        if (!details || !fresh.curve_error.empty()) {
            fresh.curve = state.data.curve;
            if (!details) fresh.curve_error = state.data.curve_error;
            else state.curve_stale = !fresh.curve.empty();
        } else { state.curve_stale = false; ++state.curve_revision; }
        if (fresh.connected) read_retries = 0;
        else if (backend_opened && !state.paused && read_retries < 3) {
            ++read_retries; poll_pending = true; details_pending |= details; signal();
        }
        state.data = std::move(fresh);
        state.status = state.data.status;
        HistorySample sample; sample.time = now_seconds();
        if (valid && (raw.present & DC_T_CORE)) sample.core_mhz = raw.core;
        if (valid && (raw.present & DC_T_TEMP_EDGE)) sample.temp_c = raw.temp_edge;
        if (valid && (raw.present & DC_T_POWER_W)) sample.power_w = raw.power_w;
        state.history.push_back(sample);
        if (state.history.size() > 360) state.history.erase(state.history.begin());
        publish_locked();
        return true;
    }
    void run() noexcept {
        for (;;) {
            enum class Work { none, change, poll, action, stop } work = Work::none;
            std::uint64_t generation = 0;
            std::string slot;
            bool details = false;
            Command task;
            {
                Guard guard(lock);
                if (stopping) work = Work::stop;
                else if (switch_pending) { work = Work::change; switch_pending = false; slot = desired_slot; generation = state.generation; }
                else if (command_pending) { work = Work::action; command_pending = false; task = command; generation = state.generation; slot = state.selected_slot; }
                else if (poll_pending && !state.paused) { work = Work::poll; poll_pending = false; details = details_pending; details_pending = false; generation = state.generation; }
            }
            if (work == Work::stop) break;
            if (work == Work::none) { WaitForSingleObject(wake, INFINITE); continue; }
            if (work == Work::change) {
                std::vector<Device> devices; std::string error; bool opened = false;
                try {
                    backend->close();
                    devices = backend->enumerate();
                    if (slot.empty() && !devices.empty()) slot = devices.front().slot;
                    if (!slot.empty()) opened = backend->open(slot, error);
                    else error = "No GPU available";
                } catch (...) { error = "Unable to open GPU"; }
                bool current;
                {
                    Guard guard(lock);
                    current = !stopping && generation == state.generation;
                    if (current) {
                        state.devices = std::move(devices);
                        backend_opened = opened; read_retries = 0;
                        state.selected_slot = slot; state.opening = opened;
                        state.status = opened ? "Reading GPU" : error;
                        state.data.connected = false;
                        publish_locked();
                    }
                }
                if (current) { announce(); if (opened) { if (read_and_publish(generation, true, true)) announce(); } }
            } else if (work == Work::poll) {
                if (read_and_publish(generation, details)) announce();
            } else {
                ActionResult result;
                try { result = backend->execute(task); } catch (...) { result.message = "Action failed; state is uncertain, checking readback and Undo availability"; result.may_have_changed = true; }
                bool current;
                {
                    Guard guard(lock);
                    current = !stopping && generation == state.generation;
                    if (stopping && generation == state.generation) state.busy = false;
                    ++state.completed_actions;
                    if (generation == state.generation) {
                        state.last_action_operation = task.operation; state.last_action_ok = result.ok;
                        // A verified process gate is authoritative even if the following
                        // telemetry refresh throws. An uncertain change cannot look off.
                        if (task.operation == Command::Operation::set_xoc) {
                            state.data.xoc_known = result.ok;
                            if (result.ok) state.data.xoc_enabled = task.enabled;
                        } else if (task.operation == Command::Operation::set_rail_mode) {
                            state.data.rail_mode_known = result.ok;
                            if (result.ok) state.data.rail_mode_enabled = task.enabled;
                        }
                    }
                    const std::string message = generation == state.generation ? result.message :
                        "GPU " + slot + " (session " + std::to_string(generation) + "): " + result.message;
                    log_locked(result.ok, message); publish_locked();
                }
                announce();
                if (current && read_and_publish(generation, true, false, true)) announce();
            }
        }
        try { backend->close(); } catch (...) {}
        { Guard guard(lock); done = true; state.busy = false; state.opening = false; publish_locked(); }
        announce();
    }
};
Session::Session(std::unique_ptr<IBackend> backend, std::function<void()> notify) : impl_(std::make_unique<Impl>(std::move(backend), std::move(notify))) {}
Session::~Session() { begin_shutdown(); if (impl_->thread) WaitForSingleObject(impl_->thread, INFINITE); }
void Session::start(const std::string& slot) {
    { Guard guard(impl_->lock);
      if (impl_->started || impl_->stopping) return;
      impl_->started = true; impl_->state.generation++; impl_->state.opening = true;
      impl_->desired_slot = slot; impl_->switch_pending = true;
      if (impl_->backend && impl_->wake) impl_->thread = CreateThread(nullptr, 0, &Impl::entry, impl_.get(), 0, nullptr);
      if (!impl_->thread) { impl_->done = true; impl_->state.opening = false; impl_->state.status = "Unable to start session worker"; }
      impl_->publish_locked(); }
    impl_->signal(); impl_->announce();
}
bool Session::select_device(const std::string& slot) {
    { Guard guard(impl_->lock);
      if (!impl_->started || impl_->stopping || impl_->done) return false;
      impl_->backend_opened = false; impl_->read_retries = 0;
      ++impl_->state.generation; impl_->desired_slot = slot; impl_->switch_pending = true;
      impl_->poll_pending = impl_->details_pending = impl_->command_pending = false;
      impl_->state.selected_slot = slot; impl_->state.opening = true; impl_->state.busy = false;
      impl_->state.data = {}; impl_->state.history.clear(); impl_->state.telemetry_stale_mask = 0; impl_->state.curve_stale = false; impl_->state.fan_readings_stale = false;
      impl_->state.last_action_operation.reset(); impl_->state.last_action_ok = false;
      impl_->publish_locked(); }
    impl_->signal(); impl_->announce(); return true;
}
void Session::request_poll(bool details) {
    { Guard guard(impl_->lock); if (!impl_->started || impl_->stopping || impl_->done || impl_->state.paused || impl_->state.opening || !impl_->backend_opened || (!impl_->state.data.connected && impl_->read_retries >= 3)) return;
      impl_->poll_pending = true; impl_->details_pending |= details; }
    impl_->signal();
}
void Session::set_paused(bool paused) {
    { Guard guard(impl_->lock); if (impl_->stopping || impl_->state.paused == paused) return;
      impl_->state.paused = paused;
      if (paused) impl_->poll_pending = impl_->details_pending = false;
      else {
          impl_->state.telemetry_stale_mask |= impl_->state.data.telemetry.present;
          impl_->state.fan_readings_stale = impl_->state.data.telemetry.fan_count != 0;
      }
      impl_->publish_locked(); }
    impl_->signal(); impl_->announce();
}
bool Session::submit(Command command, std::uint64_t expected_generation) {
    bool accepted = false;
    { Guard guard(impl_->lock);
      const char* reason = nullptr;
      if (!impl_->started || impl_->stopping || impl_->done) reason = "Session is closing or unavailable";
      else if (expected_generation != impl_->state.generation) reason = "Selected GPU session changed";
      else if (impl_->state.opening || !impl_->state.data.connected) reason = "GPU is not ready";
      else if (impl_->state.busy) reason = "Another operation is still running";
      if (reason) impl_->log_locked(false, std::string("Action not queued: ") + reason);
      else { impl_->command = std::move(command); impl_->command_pending = true; impl_->state.busy = true; accepted = true; }
      impl_->publish_locked(); }
    impl_->signal(); impl_->announce(); return accepted;
}
std::shared_ptr<const SessionSnapshot> Session::snapshot() const { Guard guard(impl_->lock); return impl_->published; }
void Session::begin_shutdown() {
    { Guard guard(impl_->lock); if (impl_->stopping) return; impl_->stopping = true;
      impl_->poll_pending = impl_->details_pending = impl_->command_pending = impl_->switch_pending = false;
      if (!impl_->thread) impl_->done = true; }
    impl_->signal();
}
bool Session::finished() const { Guard guard(impl_->lock); return impl_->done; }
}
