// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include <windows.h>
#include <algorithm>
#include <cstring>
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
void merge_fans(const LiveData& old, LiveData& next, SessionSnapshot& state) {
    auto& n=next.telemetry;const auto& o=old.telemetry;
    const uint32_t current=next.telemetry_valid?std::min(n.fan_count,uint32_t{32}):0;
    const uint32_t previous=old.telemetry_valid?std::min(o.fan_count,uint32_t{32}):0;
    // The C ABI keeps fan indices, including empty rows when the count was
    // read. A short/empty vector without a successful count is not evidence
    // that established fans disappeared. A fresh actual count may shrink it.
    const bool counted=next.telemetry_valid&&(n.present&DC_T_NUM_FANS)&&n.num_fans<=32&&n.num_fans>=current;
    const uint32_t count=counted?n.num_fans:std::max(current,previous);
    state.fan_duty_stale_mask=state.fan_rpm_stale_mask=0;
    for(uint32_t i=0;i<count;++i){
        auto& f=n.fans[i];const auto fresh=i<current?f:dc_fan_reading{};
        const auto prior=i<previous?o.fans[i]:dc_fan_reading{};
        f={};
        if(fresh.has_duty){f.has_duty=1;f.duty=fresh.duty;}
        else if(prior.has_duty){f.has_duty=1;f.duty=prior.duty;state.fan_duty_stale_mask|=UINT32_C(1)<<i;}
        if(fresh.has_rpm){f.has_rpm=1;f.rpm=fresh.rpm;}
        else if(prior.has_rpm){f.has_rpm=1;f.rpm=prior.rpm;state.fan_rpm_stale_mask|=UINT32_C(1)<<i;}
    }
    for(uint32_t i=count;i<32;++i)n.fans[i]={};
    n.fan_count=count;
    state.fan_readings_stale=(state.fan_duty_stale_mask|state.fan_rpm_stale_mask)!=0;
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
    bool maintenance_active = false, maintenance_inflight = false;
    ULONGLONG maintenance_due = 0;
    std::string maintenance_error;
    unsigned read_retries = 0;
    Command command{};
    std::uint64_t next_log = 0;
    std::string desired_slot, expected_uuid;
    bool identity_mismatch = false;
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
        std::optional<TuningState> tuning;
        std::optional<bool> needs_maintenance;
        std::optional<FrequencyLockData> frequency;
        try { tuning = backend->tuning_state(); needs_maintenance = backend->maintenance_needed(); } catch (...) {}
        try { frequency = backend->frequency_state(); } catch (...) {}
        Guard guard(lock);
        if (stopping || generation != state.generation) return false;
        ++state.completed_reads;
        bool wrong_identity = false;
        if (!expected_uuid.empty() && !failed && fresh.connected) {
            const auto& id=fresh.identity;
            const bool uuid_valid=std::memchr(id.uuid,0,sizeof(id.uuid)) && id.uuid[0];
            const bool slot_valid=std::memchr(id.slot,0,sizeof(id.slot)) && id.slot[0];
            const bool pairing_valid=std::memchr(id.pairing_error,0,sizeof(id.pairing_error)) && !id.pairing_error[0];
            const bool api_valid=id.nvapi_ok || id.nvml_ok;
            wrong_identity=(uuid_valid && expected_uuid!=id.uuid) || (slot_valid && state.selected_slot!=id.slot) ||
                (std::memchr(id.pairing_error,0,sizeof(id.pairing_error)) && id.pairing_error[0]);
            if (wrong_identity || !uuid_valid || !slot_valid || !pairing_valid || !api_valid) {
                fresh=state.data; fresh.connected=false; fresh.telemetry_valid=false;
                fresh.telemetry_error="GPU identity unavailable";
                fresh.status=wrong_identity ? "GPU identity does not match the requested adapter" : "GPU identity unavailable; retrying read";
                if (details) fresh.curve_error=fresh.status;
            }
            if (wrong_identity) identity_mismatch=true;
            else if (uuid_valid && slot_valid && pairing_valid && api_valid) identity_mismatch=false;
        }
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
        if (tuning) {
            fresh.tuning = *tuning;
            if (needs_maintenance) state.tuning_ownership_uncertain = *needs_maintenance && !tuning->maintenance_needed();
        }
        else fresh.tuning=state.data.tuning;
        if (frequency) { fresh.frequency=*frequency; state.frequency_ownership_uncertain=false; }
        else {
            fresh.frequency=state.data.frequency;
            if (state.data.frequency.ownership.needs_cleanup()) state.frequency_ownership_uncertain=true;
        }
        if ((needs_maintenance && *needs_maintenance) || fresh.tuning.maintenance_needed() || state.tuning_ownership_uncertain) {
            maintenance_active = true;
            if (!maintenance_due) maintenance_due = GetTickCount64() + 1000;
        }
        const auto raw = fresh.telemetry;
        const bool valid = fresh.telemetry_valid;
        merge_fans(state.data,fresh,state);
        if (initial) state.opening = false;
        if (finish_action) state.busy = false;
        merge_telemetry(state.data, fresh, state.telemetry_stale_mask);
        merge_controls(state.data, fresh);
        if (wrong_identity) {
            for (auto& c:fresh.controls) c.writable=c.stock_writable=false;
            for (auto& c:fresh.advanced_controls) c.state.writable=c.state.stock_writable=false;
            for (auto& c:fresh.power_policies) c.state.writable=c.state.stock_writable=false;
            for (auto& c:fresh.rail_controls) c.state.writable=c.state.stock_writable=false;
            fresh.curve_writable=fresh.curve_reset_writable=false;
        }
        if (!details || !fresh.curve_error.empty()) {
            fresh.curve = state.data.curve;
            fresh.curve_layout = state.data.curve_layout;
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
            enum class Work { none, change, poll, action, maintain, stop } work = Work::none;
            std::uint64_t generation = 0;
            std::string slot, required_uuid;
            bool details = false;
            DWORD wait_ms = INFINITE;
            Command task;
            {
                Guard guard(lock);
                if (stopping) work = Work::stop;
                else if (switch_pending) { work = Work::change; switch_pending = false; slot = desired_slot; required_uuid = expected_uuid; generation = state.generation; }
                else if (command_pending) { work = Work::action; command_pending = false; task = command; generation = state.generation; slot = state.selected_slot; }
                else if (maintenance_active && GetTickCount64() >= maintenance_due) { work = Work::maintain; generation = state.generation; maintenance_inflight = true; maintenance_due = GetTickCount64() + 1000; }
                else if (poll_pending && !state.paused) { work = Work::poll; poll_pending = false; details = details_pending; details_pending = false; generation = state.generation; }
                if (work == Work::none && maintenance_active) {
                    const auto now = GetTickCount64();
                    wait_ms = maintenance_due <= now ? 0 : static_cast<DWORD>(std::min<ULONGLONG>(maintenance_due - now, 1000));
                }
            }
            if (work == Work::stop) {
                ActionResult cleanup;
                try { cleanup=backend->prepare_close(); }
                catch (...) { cleanup.message="Owned control restoration failed; session kept open for recovery"; }
                if (cleanup.ok) break;
                {
                    Guard guard(lock); stopping=false; state.close_refused=true; state.busy=false;
                    state.status=cleanup.message; log_locked(false,cleanup.message); publish_locked();
                }
                announce(); continue;
            }
            if (work == Work::none) { WaitForSingleObject(wake, wait_ms); continue; }
            if (work == Work::change) {
                std::vector<Device> devices; std::string error; bool opened = false;
                try {
                    backend->close();
                    devices = backend->enumerate();
                    if (!required_uuid.empty()) {
                        const auto matching_slot=std::count_if(devices.begin(),devices.end(),[&](const auto& d){return d.slot==slot;});
                        const auto matched=std::find_if(devices.begin(),devices.end(),[&](const auto& d){return d.slot==slot && d.uuid==required_uuid;});
                        if (slot.empty()) error="Expected GPU UUID requires an explicit slot";
                        else if (matching_slot!=1 || matched==devices.end()) error="Requested GPU slot and UUID were not found together";
                        else opened=backend->open(slot,error);
                    } else {
                        if (slot.empty() && !devices.empty()) slot = devices.front().slot;
                        if (!slot.empty()) opened = backend->open(slot, error);
                        else error = "No GPU available";
                    }
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
            } else if (work == Work::maintain) {
                ActionResult result;
                try { result = backend->maintain(); } catch (...) { result.message = "Owned control maintenance failed"; }
                std::optional<TuningState> tuning; bool needed = true;
                try { tuning = backend->tuning_state(); needed = backend->maintenance_needed(); } catch (...) {}
                bool changed = false;
                {
                    Guard guard(lock); maintenance_inflight = false;
                    if (generation == state.generation) {
                        const auto old = state.data.tuning;
                        const bool old_uncertain = state.tuning_ownership_uncertain;
                        if (tuning) { state.data.tuning = *tuning; state.tuning_ownership_uncertain = needed && !tuning->maintenance_needed(); }
                        else state.tuning_ownership_uncertain = true;
                        maintenance_active = needed || state.data.tuning.maintenance_needed() || state.tuning_ownership_uncertain;
                        if (!maintenance_active) maintenance_due = 0;
                        const auto& now = state.data.tuning;
                        const std::string next_error = result.ok ? "" : (result.message.empty() ? "Owned control maintenance failed" : result.message);
                        if (next_error != maintenance_error) { maintenance_error = next_error; if (!next_error.empty()) log_locked(false, next_error); changed = true; }
                        if (old_uncertain != state.tuning_ownership_uncertain || old.owned != now.owned || old.confirmed != now.confirmed || old.headroom != now.headroom || old.recovery != now.recovery || old.tripped != now.tripped || old.domain != now.domain || old.voltage_uv != now.voltage_uv) changed = true;
                        if (changed) publish_locked();
                    }
                }
                if (changed) announce();
            } else {
                ActionResult result;
                try { result = backend->execute(task); } catch (...) { result.message = "Action failed; state is uncertain, checking readback and Undo availability"; result.may_have_changed = true; }
                std::optional<TuningState> owned_tuning; bool needed=false, needed_known=false;
                try { owned_tuning=backend->tuning_state(); needed=backend->maintenance_needed(); needed_known=true; } catch (...) {}
                const bool tuning_action=task.operation==Command::Operation::max_it || task.operation==Command::Operation::reset_controls || task.operation==Command::Operation::hold_curve_point || task.operation==Command::Operation::release_hold;
                const bool i2c_action=is_i2c_operation(task.operation);
                const bool frequency_action=is_frequency_operation(task.operation);
                const bool frequency_mutation=frequency_action && task.operation!=Command::Operation::frequency_lock_refresh;
                std::optional<FrequencyLockData> owned_frequency;
                try { owned_frequency=backend->frequency_state(); } catch (...) {}
                std::optional<I2cViewData> owned_i2c;
                if (i2c_action) { try { owned_i2c=backend->i2c_state(); } catch (...) {} }
                bool current;
                {
                    Guard guard(lock);
                    current = !stopping && generation == state.generation;
                    if (stopping && generation == state.generation) state.busy = false;
                    ++state.completed_actions;
                    if (generation == state.generation) {
                        if (owned_tuning) {
                            state.data.tuning = *owned_tuning;
                            state.tuning_ownership_uncertain = needed_known && needed && !owned_tuning->maintenance_needed();
                        } else if (tuning_action || state.data.tuning.maintenance_needed() || (needed_known && needed)) {
                            state.tuning_ownership_uncertain = true;
                        }
                        maintenance_active = (needed_known && needed) || state.data.tuning.maintenance_needed() || state.tuning_ownership_uncertain;
                        if (maintenance_active && !maintenance_due) maintenance_due = GetTickCount64() + 1000;
                        if (!maintenance_active) { maintenance_due = 0; maintenance_error.clear(); }
                        if (owned_frequency) { state.data.frequency=*owned_frequency; state.frequency_ownership_uncertain=false; }
                        else if (frequency_mutation || state.data.frequency.ownership.needs_cleanup()) state.frequency_ownership_uncertain=true;
                        if (i2c_action) {
                            if (owned_i2c) { state.data.i2c=*owned_i2c; state.i2c_ownership_uncertain=false; }
                            else if (task.operation==Command::Operation::i2c_apply || task.operation==Command::Operation::i2c_verify || task.operation==Command::Operation::i2c_release || task.operation==Command::Operation::i2c_zero_nct_outputs)
                                state.i2c_ownership_uncertain=true;
                        }
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
                    log_locked(result.ok, message);
                    if (generation == state.generation) state.last_action_log_id=state.log.back().id;
                    publish_locked();
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
void Session::start(const std::string& slot, const std::string& expected_uuid) {
    { Guard guard(impl_->lock);
      if (impl_->started || impl_->stopping) return;
      impl_->started = true; impl_->state.generation++; impl_->state.opening = true;
      impl_->desired_slot = slot; impl_->expected_uuid = expected_uuid; impl_->switch_pending = true;
      if (impl_->backend && impl_->wake) impl_->thread = CreateThread(nullptr, 0, &Impl::entry, impl_.get(), 0, nullptr);
      if (!impl_->thread) { impl_->done = true; impl_->state.opening = false; impl_->state.status = "Unable to start session worker"; }
      impl_->publish_locked(); }
    impl_->signal(); impl_->announce();
}
bool Session::select_device(const std::string& slot) { return change_device(slot,false); }
bool Session::reconnect() { return change_device({},true); }
bool Session::change_device(const std::string& slot, bool preserve_constraint) {
    { Guard guard(impl_->lock);
      if (!impl_->started || impl_->stopping || impl_->done) return false;
      const auto& i2c=impl_->state.data.i2c;
      const bool tuning_busy=impl_->state.busy && (impl_->command.operation==Command::Operation::max_it || impl_->command.operation==Command::Operation::reset_controls || impl_->command.operation==Command::Operation::hold_curve_point || impl_->command.operation==Command::Operation::release_hold);
      const bool i2c_busy=impl_->state.busy && is_i2c_operation(impl_->command.operation);
      const bool frequency_busy=impl_->state.busy && is_frequency_operation(impl_->command.operation);
      if (frequency_busy || impl_->state.frequency_ownership_uncertain || impl_->state.data.frequency.ownership.needs_cleanup() || i2c.clock_owned || i2c.status.owned || i2c.status.recovery_pending || i2c_busy || impl_->state.i2c_ownership_uncertain || tuning_busy || impl_->maintenance_inflight || impl_->state.data.tuning.maintenance_needed() || impl_->state.tuning_ownership_uncertain) {
          impl_->log_locked(false,"Release owned controls before switching GPUs"); impl_->publish_locked(); return false;
      }
      impl_->backend_opened = false; impl_->read_retries = 0;
      // Only an explicit picker selection clears the startup constraint.
      const auto target_slot=preserve_constraint ? impl_->state.selected_slot : slot;
      if (!preserve_constraint) { impl_->expected_uuid.clear(); impl_->identity_mismatch=false; }
      ++impl_->state.generation; impl_->desired_slot = target_slot; impl_->switch_pending = true;
      impl_->poll_pending = impl_->details_pending = impl_->command_pending = false;
      impl_->state.selected_slot = target_slot; impl_->state.opening = true; impl_->state.busy = false;
      impl_->state.data = {}; impl_->state.history.clear(); impl_->state.telemetry_stale_mask = 0; impl_->state.curve_stale = false; impl_->state.fan_readings_stale = false;
      impl_->state.fan_duty_stale_mask = impl_->state.fan_rpm_stale_mask = 0;
      impl_->state.last_action_operation.reset(); impl_->state.last_action_ok = false; impl_->state.last_action_log_id = 0;
      impl_->state.frequency_ownership_uncertain = false;
      impl_->publish_locked(); }
    impl_->signal(); impl_->announce(); return true;
}
void Session::request_poll(bool details) {
    { Guard guard(impl_->lock); if (!impl_->started || impl_->stopping || impl_->done || impl_->state.paused || impl_->state.opening) return;
      if (!impl_->backend_opened) {
          // An explicit details refresh may retry a failed constrained startup,
          // without dropping its slot/UUID binding or silently selecting another GPU.
          if (!details || impl_->expected_uuid.empty()) return;
          impl_->desired_slot=impl_->state.selected_slot; impl_->switch_pending=true;
          impl_->state.opening=true; impl_->state.status="Retrying GPU enumeration"; impl_->publish_locked();
      } else {
          if (!details && !impl_->state.data.connected && impl_->read_retries>=3) return;
          if (details) impl_->read_retries=0;
          impl_->poll_pending = true; impl_->details_pending |= details;
      } }
    impl_->signal();
}
void Session::set_paused(bool paused) {
    { Guard guard(impl_->lock); if (impl_->stopping || impl_->state.paused == paused) return;
      impl_->state.paused = paused;
      if (paused) impl_->poll_pending = impl_->details_pending = false;
      else {
          impl_->state.telemetry_stale_mask |= impl_->state.data.telemetry.present;
          for(uint32_t i=0;i<std::min(impl_->state.data.telemetry.fan_count,uint32_t{32});++i){
              const auto& f=impl_->state.data.telemetry.fans[i];
              if(f.has_duty)impl_->state.fan_duty_stale_mask|=UINT32_C(1)<<i;
              if(f.has_rpm)impl_->state.fan_rpm_stale_mask|=UINT32_C(1)<<i;
          }
          impl_->state.fan_readings_stale=(impl_->state.fan_duty_stale_mask|impl_->state.fan_rpm_stale_mask)!=0;
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
      else if (impl_->identity_mismatch) reason = "GPU identity does not match the requested adapter";
      else if (impl_->state.opening || (!impl_->state.data.connected && !(impl_->backend_opened && (command.operation==Command::Operation::i2c_release || command.operation==Command::Operation::i2c_read || command.operation==Command::Operation::release_hold || command.operation==Command::Operation::frequency_lock_release || command.operation==Command::Operation::frequency_lock_refresh)))) reason = "GPU is not ready";
      else if (impl_->state.busy) reason = "Another operation is still running";
      if (reason) impl_->log_locked(false, std::string("Action not queued: ") + reason);
      else { impl_->command = std::move(command); impl_->command_pending = true; impl_->state.busy = true; accepted = true; }
      impl_->publish_locked(); }
    impl_->signal(); impl_->announce(); return accepted;
}
std::shared_ptr<const SessionSnapshot> Session::snapshot() const { Guard guard(impl_->lock); return impl_->published; }
void Session::begin_shutdown() {
    { Guard guard(impl_->lock); if (impl_->stopping) return; impl_->stopping = true; impl_->state.close_refused=false;
      impl_->poll_pending = impl_->details_pending = impl_->command_pending = impl_->switch_pending = false;
      if (!impl_->thread) impl_->done = true; }
    impl_->signal();
}
bool Session::finished() const { Guard guard(impl_->lock); return impl_->done; }
}
