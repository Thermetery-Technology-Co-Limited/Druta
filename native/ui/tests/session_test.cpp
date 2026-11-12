// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include <windows.h>
#include <cstdlib>
#include <cstdio>
#include <atomic>
#include <algorithm>
#include <functional>
#include <memory>
#include <string>
#include <vector>
using namespace druta::ui;
static unsigned checks = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #expr); std::abort(); } } while (false)

struct Fake : IBackend {
    HANDLE entered = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE release = CreateEventW(nullptr, TRUE, TRUE, nullptr);
    CRITICAL_SECTION lock{};
    std::vector<std::string> calls;
    std::atomic<bool> partial{false}, disconnected{false}, hold_read{false}, hold_action{false};
    std::atomic<bool> telemetry_failure{false}, throw_action{false}, undo_available{false};
    std::atomic<bool> throw_reads{false}, throw_after_action{false}, fail_gate{false};
    std::atomic<bool> i2c_owned{false}, fail_cleanup{false}, missing_i2c_state{false};
    std::atomic<bool> tuning_owned{false}, missing_tuning{false}, maintenance_fail{false};
    std::atomic<int> maintenance_count{0}, total_reads{0}, detail_reads{0}, board_w{200}, core_ma{100};
    bool xoc = false, rails = false;
    Fake() { InitializeCriticalSection(&lock); }
    ~Fake() override { CloseHandle(entered); CloseHandle(release); DeleteCriticalSection(&lock); }
    void record(const std::string& s) { EnterCriticalSection(&lock); calls.push_back(s); LeaveCriticalSection(&lock); }
    void block() { SetEvent(entered); WaitForSingleObject(release, INFINITE); }
    void stall_read() { ResetEvent(entered); ResetEvent(release); hold_read = true; }
    void stall_action() { ResetEvent(entered); ResetEvent(release); hold_action = true; }
    std::vector<Device> enumerate() override { record("enumerate"); return {{"a", "A", "1"}, {"b", "B", "2"}}; }
    bool open(const std::string& s, std::string&) override { record("open:" + s); return true; }
    void close() override { record("close"); }
    ActionResult prepare_close() override {
        record("prepare_close"); ActionResult r; r.ok=!fail_cleanup.load();
        r.message=r.ok ? "released" : "recovery required";
        if (r.ok) i2c_owned=false; return r;
    }
    bool maintenance_needed() const override { return tuning_owned.load(); }
    std::optional<TuningState> tuning_state() const override {
        if (missing_tuning.load()) return {}; TuningState state; state.owned=tuning_owned.load(); return state;
    }
    ActionResult maintain() override {
        ++maintenance_count; ActionResult r; r.ok=!maintenance_fail.load(); r.message=r.ok ? "" : "hold needs attention"; return r;
    }
    std::optional<I2cViewData> i2c_state() const override {
        if (missing_i2c_state.load()) return {};
        I2cViewData d; d.clock_owned=i2c_owned.load(); return d;
    }
    LiveData read(bool details) override {
        record(details ? "details" : "read"); ++total_reads; if (details) ++detail_reads;
        if (hold_read.exchange(false)) block();
        if (throw_reads.load()) throw 1;
        const bool is_partial = partial.load();
        LiveData d; d.connected = !disconnected.load(); d.telemetry_valid = !telemetry_failure.load(); d.status = "ok";
        d.undo_available = undo_available.load(); d.i2c.clock_owned = i2c_owned.load();
        d.tuning.owned=tuning_owned.load(); d.controls[2].current=board_w.load();
        PowerPolicy policy; policy.id=3; policy.state.current=core_ma.load(); d.power_policies.push_back(policy);
        d.xoc_known = d.rail_mode_known = true; d.xoc_enabled = xoc; d.rail_mode_enabled = rails;
        if (d.telemetry_valid) { d.telemetry.fan_count = 1; d.telemetry.fans[0].has_duty = 1; d.telemetry.fans[0].duty = 40; }
        d.telemetry.present = DC_T_CORE | (is_partial ? 0 : DC_T_POWER_W);
        d.telemetry.core = 100; d.telemetry.power_w = 80;
        auto& c = d.controls[0];
        c.availability = is_partial ? Availability::transient : Availability::available;
        c.writable = !is_partial; c.current = is_partial ? std::optional<double>{} : 12;
        AdvancedControl advanced;
        advanced.id = {AdvancedKind::clock_domain, 7}; advanced.label = "Alternate domain";
        advanced.state = c; d.advanced_controls.push_back(advanced);
        if (details) d.curve.resize(1);
        return d;
    }
    ActionResult execute(const Command& cmd) override {
        record("execute"); if (hold_action.exchange(false)) block();
        if (throw_action.exchange(false)) { undo_available = true; throw 1; }
        if (cmd.operation == Command::Operation::hold_curve_point) tuning_owned=true;
        if (cmd.operation == Command::Operation::release_hold) tuning_owned=false;
        if (cmd.operation == Command::Operation::apply && cmd.control == ControlId::power) {board_w=199;core_ma=87;}
        if (cmd.operation == Command::Operation::i2c_apply) i2c_owned=true;
        if (cmd.operation == Command::Operation::i2c_release) i2c_owned=false;
        if (cmd.operation == Command::Operation::set_xoc) xoc = cmd.enabled;
        if (cmd.operation == Command::Operation::set_rail_mode) rails = cmd.enabled;
        if (throw_after_action.load()) throw_reads = true;
        ActionResult r; r.ok = !fail_gate.load(); r.message = "done"; return r;
    }
};
static bool until(const std::function<bool()>& condition) {
    for (int i = 0; i < 500; ++i) { if (condition()) return true; Sleep(5); }
    return false;
}
int main() {
    auto owned = std::make_unique<Fake>(); auto* f = owned.get();
    f->stall_read();
    Session s(std::move(owned), [] {}); s.start("a");
    CHECK(WaitForSingleObject(f->entered, 2000) == WAIT_OBJECT_0);
    CHECK(s.snapshot()->opening);
    CHECK(!s.submit({}, s.snapshot()->generation));
    SetEvent(f->release);
    CHECK(until([&] { return !s.snapshot()->history.empty(); }));
    auto initial = s.snapshot(); CHECK(initial->selected_slot == "a");
    CHECK(initial->data.controls[0].writable);
    CHECK(initial->data.advanced_controls.size() == 1 && initial->data.advanced_controls[0].state.writable);
    CHECK(initial->data.curve.size() == 1); // initial read requested details
    CHECK(initial->data.telemetry.fan_count == 1 && !initial->fan_readings_stale);
    f->partial = true; s.request_poll();
    CHECK(until([&] { return s.snapshot()->history.size() == 2; }));
    auto partial = s.snapshot();
    CHECK((partial->telemetry_stale_mask & DC_T_POWER_W) != 0);
    CHECK(partial->data.telemetry.power_w == 80);
    CHECK(!partial->history.back().power_w);
    CHECK(partial->data.controls[0].writable && partial->data.controls[0].current == 12);
    CHECK(partial->data.controls[0].availability == Availability::transient);
    CHECK(partial->data.advanced_controls[0].id.domain == 7);
    CHECK(partial->data.advanced_controls[0].state.availability == Availability::transient);
    CHECK(!partial->data.advanced_controls[0].state.writable && partial->data.advanced_controls[0].state.current == 12);
    s.request_poll();
    CHECK(until([&] { return s.snapshot()->history.size() == 3; }));
    CHECK(s.snapshot()->data.controls[0].detail == partial->data.controls[0].detail);
    CHECK(initial->history.size() == 1); // immutable prior publication
    s.set_paused(true); auto revision = s.snapshot()->revision; s.request_poll(true); Sleep(40);
    CHECK(s.snapshot()->revision == revision);
    s.set_paused(false);
    CHECK((s.snapshot()->telemetry_stale_mask & DC_T_CORE) != 0);
    CHECK(s.snapshot()->fan_readings_stale);
    f->stall_read(); s.request_poll(); CHECK(WaitForSingleObject(f->entered, 2000) == WAIT_OBJECT_0);
    s.request_poll(); s.request_poll(true); s.request_poll();
    SetEvent(f->release);
    CHECK(until([&] { return s.snapshot()->history.size() == 5; }));
    Sleep(40); CHECK(s.snapshot()->history.size() == 5); // coalesced into one detail poll
    f->stall_read(); s.request_poll(); CHECK(WaitForSingleObject(f->entered, 2000) == WAIT_OBJECT_0);
    s.request_poll(true); s.set_paused(true); SetEvent(f->release);
    CHECK(until([&] { return s.snapshot()->history.size() == 6; }));
    Sleep(40); CHECK(s.snapshot()->history.size() == 6);
    s.set_paused(false);
    f->throw_action = true; f->stall_read();
    CHECK(s.submit({}, s.snapshot()->generation));
    CHECK(WaitForSingleObject(f->entered, 2000) == WAIT_OBJECT_0);
    CHECK(s.snapshot()->busy);
    CHECK(!s.submit({}, s.snapshot()->generation));
    SetEvent(f->release);
    CHECK(until([&] { auto p = s.snapshot(); return !p->busy && !p->log.empty(); }));
    auto after_uncertain = s.snapshot();
    CHECK(std::any_of(after_uncertain->log.begin(), after_uncertain->log.end(), [](const auto& entry) {
        return entry.message.find("state is uncertain") != std::string::npos;
    }));
    CHECK(after_uncertain->log.back().message == "Action not queued: Another operation is still running");
    CHECK(s.snapshot()->data.undo_available);
    f->undo_available = false;
    f->stall_action(); auto generation = s.snapshot()->generation;
    CHECK(s.submit({}, generation)); CHECK(WaitForSingleObject(f->entered, 2000) == WAIT_OBJECT_0);
    CHECK(!s.submit({}, generation));
    CHECK(s.select_device("b")); CHECK(!s.submit({}, generation));
    SetEvent(f->release);
    CHECK(until([&] { auto p = s.snapshot(); return p->selected_slot == "b" && !p->opening && !p->history.empty(); }));
    auto after_switch = s.snapshot();
    CHECK(std::any_of(after_switch->log.begin(), after_switch->log.end(), [](const auto& entry) {
        return entry.message == "Action not queued: Selected GPU session changed";
    }));
    CHECK(s.snapshot()->log.back().message.find("GPU a (session ") == 0);
    EnterCriticalSection(&f->lock); auto calls = f->calls; LeaveCriticalSection(&f->lock);
    auto action = std::find(calls.begin(), calls.end(), "execute");
    auto second = std::find(calls.begin(), calls.end(), "open:b");
    CHECK(action != calls.end() && second != calls.end() && action < second && std::find(action, second, "close") != second);
    f->stall_read(); s.request_poll(); CHECK(WaitForSingleObject(f->entered, 2000) == WAIT_OBJECT_0);
    auto old_generation = s.snapshot()->generation;
    CHECK(s.select_device("a"));
    SetEvent(f->release);
    CHECK(until([&] { auto p = s.snapshot(); return p->generation > old_generation && p->selected_slot == "a" && !p->opening && !p->history.empty(); }));
    CHECK(s.snapshot()->history.size() == 1); // the late old-device read was discarded
    f->telemetry_failure = true;
    auto before_telemetry_failure = s.snapshot()->history.size();
    s.request_poll();
    CHECK(until([&] { return s.snapshot()->history.size() == before_telemetry_failure + 1; }));
    CHECK(s.snapshot()->fan_readings_stale);
    CHECK(s.snapshot()->data.telemetry.fan_count == 1 && s.snapshot()->data.telemetry.fans[0].duty == 40);
    f->telemetry_failure = false; s.request_poll();
    CHECK(until([&] { return !s.snapshot()->fan_readings_stale; }));
    f->disconnected = true;
    auto before_failure = s.snapshot()->history.size();
    s.request_poll();
    CHECK(until([&] { return s.snapshot()->history.size() == before_failure + 4; }));
    CHECK((s.snapshot()->telemetry_stale_mask & DC_T_CORE) != 0);
    CHECK(!s.snapshot()->history.back().core_mhz);
    CHECK(s.snapshot()->data.advanced_controls[0].state.availability == Availability::transient);
    Sleep(50); s.request_poll(); Sleep(50);
    CHECK(s.snapshot()->history.size() == before_failure + 4);
    f->disconnected = false;
    CHECK(s.select_device(""));
    CHECK(until([&] { auto p = s.snapshot(); return !p->opening && p->selected_slot == "a" && !p->history.empty(); }));
    CHECK(s.snapshot()->data.connected);
    f->stall_read(); s.request_poll(); CHECK(WaitForSingleObject(f->entered, 2000) == WAIT_OBJECT_0);
    s.begin_shutdown(); CHECK(!s.finished()); SetEvent(f->release);
    CHECK(until([&] { return s.finished(); }));
    for (const auto operation : {Command::Operation::set_xoc, Command::Operation::set_rail_mode}) {
        for (const bool uncertain : {false, true}) {
            auto owned_gate = std::make_unique<Fake>(); auto* gate_fake = owned_gate.get();
            Session gates(std::move(owned_gate), [] {}); gates.start("a");
            CHECK(until([&] { return !gates.snapshot()->opening && gates.snapshot()->data.connected; }));
            gate_fake->throw_after_action = true; gate_fake->fail_gate = uncertain;
            Command enable; enable.operation = operation; enable.enabled = true;
            CHECK(gates.submit(enable, gates.snapshot()->generation));
            CHECK(until([&] { return !gates.snapshot()->busy && gates.snapshot()->completed_reads >= 5; }));
            const auto gate_snapshot = gates.snapshot(); const auto& data = gate_snapshot->data;
            if (operation == Command::Operation::set_xoc) {
                CHECK(data.xoc_known == !uncertain); CHECK(uncertain || data.xoc_enabled);
            } else { CHECK(data.rail_mode_known == !uncertain); CHECK(uncertain || data.rail_mode_enabled); }
            gates.begin_shutdown(); CHECK(until([&] { return gates.finished(); }));
        }
    }
    auto second_owned=std::make_unique<Fake>(); auto* second_backend=second_owned.get();
    Session protected_session(std::move(second_owned), [] {}); protected_session.start("a");
    CHECK(until([&] { return !protected_session.snapshot()->history.empty(); }));
    second_backend->stall_action(); Command i2c{}; i2c.operation=Command::Operation::i2c_apply;
    CHECK(protected_session.submit(i2c,protected_session.snapshot()->generation));
    CHECK(WaitForSingleObject(second_backend->entered,2000)==WAIT_OBJECT_0);
    CHECK(!protected_session.select_device("b")); // in-flight controller operation
    SetEvent(second_backend->release);
    CHECK(until([&] { auto p=protected_session.snapshot(); return !p->busy && p->data.i2c.clock_owned; }));
    CHECK(!protected_session.select_device("b")); // owned rail/clock cannot be abandoned
    second_backend->fail_cleanup=true;
    EnterCriticalSection(&second_backend->lock); auto prior=second_backend->calls; LeaveCriticalSection(&second_backend->lock);
    const auto close_count=std::count(prior.begin(),prior.end(),"close");
    protected_session.begin_shutdown();
    CHECK(until([&] { return protected_session.snapshot()->close_refused; }));
    CHECK(!protected_session.finished() && protected_session.snapshot()->data.connected && protected_session.snapshot()->data.i2c.clock_owned);
    EnterCriticalSection(&second_backend->lock); auto failed_close=second_backend->calls; LeaveCriticalSection(&second_backend->lock);
    CHECK(std::count(failed_close.begin(),failed_close.end(),"close")==close_count);
    second_backend->fail_cleanup=false; protected_session.begin_shutdown();
    CHECK(until([&] { return protected_session.finished(); }));
    EnterCriticalSection(&second_backend->lock); auto completed_close=second_backend->calls; LeaveCriticalSection(&second_backend->lock);
    CHECK(std::count(completed_close.begin(),completed_close.end(),"close")==close_count+1);    auto third_owned=std::make_unique<Fake>(); auto* third=third_owned.get();
    Session recovery(std::move(third_owned), [] {}); recovery.start("a");
    CHECK(until([&] { return !recovery.snapshot()->history.empty(); }));
    third->throw_after_action=true; Command apply_i2c{}; apply_i2c.operation=Command::Operation::i2c_apply;
    CHECK(recovery.submit(apply_i2c,recovery.snapshot()->generation));
    CHECK(until([&] { auto p=recovery.snapshot(); return !p->busy && !p->data.connected && p->data.i2c.clock_owned; }));
    CHECK(!recovery.select_device("b"));
    Command release_i2c{}; release_i2c.operation=Command::Operation::i2c_release;
    CHECK(recovery.submit(release_i2c,recovery.snapshot()->generation));
    CHECK(until([&] { auto p=recovery.snapshot(); return !p->busy && !p->data.i2c.clock_owned; }));
    third->throw_after_action=false; third->throw_reads=false; recovery.request_poll(true);
    CHECK(until([&] { return recovery.snapshot()->data.connected; }));
    third->missing_i2c_state=true; Command verify_i2c{}; verify_i2c.operation=Command::Operation::i2c_verify;
    CHECK(recovery.submit(verify_i2c,recovery.snapshot()->generation));
    CHECK(until([&] { auto p=recovery.snapshot(); return !p->busy && p->i2c_ownership_uncertain; }));
    CHECK(!recovery.select_device("b"));
    third->missing_i2c_state=false; CHECK(recovery.submit(release_i2c,recovery.snapshot()->generation));
    CHECK(until([&] { auto p=recovery.snapshot(); return !p->busy && !p->i2c_ownership_uncertain; }));
    recovery.begin_shutdown(); CHECK(until([&] { return recovery.finished(); }));    auto fourth_owned=std::make_unique<Fake>(); auto* fourth=fourth_owned.get();
    Session maintained(std::move(fourth_owned), [] {}); maintained.start("a");
    CHECK(until([&] { return !maintained.snapshot()->history.empty(); }));
    maintained.set_paused(true);
    const int initial_maintenance=fourth->maintenance_count.load(); Sleep(1100);
    CHECK(fourth->maintenance_count.load()==initial_maintenance); // no owned state, no timer work
    const int before_power_reads=fourth->total_reads.load(), before_power_details=fourth->detail_reads.load();
    Command power{}; power.control=ControlId::power; power.operation=Command::Operation::apply;
    CHECK(maintained.submit(power,maintained.snapshot()->generation));
    CHECK(until([&] { auto p=maintained.snapshot(); return !p->busy && p->data.controls[2].current==199; }));
    CHECK(fourth->total_reads.load()==before_power_reads+1 && fourth->detail_reads.load()==before_power_details+1);
    CHECK(maintained.snapshot()->data.power_policies.size()==1 && maintained.snapshot()->data.power_policies[0].state.current==87);
    fourth->maintenance_fail=true; fourth->throw_after_action=true;
    Command hold{}; hold.operation=Command::Operation::hold_curve_point;
    CHECK(maintained.submit(hold,maintained.snapshot()->generation));
    CHECK(until([&] { auto p=maintained.snapshot(); return !p->busy && !p->data.connected && p->data.tuning.owned; }));
    CHECK(!maintained.select_device("b"));
    const int reads_after_hold=fourth->total_reads.load();
    CHECK(until([&] { return fourth->maintenance_count.load()>=1; }));
    const auto log_size=maintained.snapshot()->log.size();
    CHECK(until([&] { return fourth->maintenance_count.load()>=2; }));
    CHECK(fourth->total_reads.load()==reads_after_hold && maintained.snapshot()->log.size()==log_size);
    Command release_hold{}; release_hold.operation=Command::Operation::release_hold;
    CHECK(maintained.submit(release_hold,maintained.snapshot()->generation));
    CHECK(until([&] { auto p=maintained.snapshot(); return !p->busy && !p->data.tuning.owned; }));
    const int count_after_release=fourth->maintenance_count.load(); Sleep(1100);
    CHECK(fourth->maintenance_count.load()==count_after_release);
    maintained.begin_shutdown(); CHECK(until([&] {return maintained.finished();}));    auto fifth_owned=std::make_unique<Fake>(); auto* fifth=fifth_owned.get();
    Session uncertain(std::move(fifth_owned), [] {}); uncertain.start("a");
    CHECK(until([&] { return !uncertain.snapshot()->history.empty(); }));
    fifth->missing_tuning=true; fifth->throw_after_action=true;
    CHECK(uncertain.submit(hold,uncertain.snapshot()->generation));
    CHECK(until([&] { auto p=uncertain.snapshot(); return !p->busy && p->tuning_ownership_uncertain; }));
    CHECK(!uncertain.select_device("b"));
    CHECK(until([&] { return fifth->maintenance_count.load()>=1; }));
    CHECK(uncertain.submit(release_hold,uncertain.snapshot()->generation));
    CHECK(until([&] { return !uncertain.snapshot()->busy; }));
    CHECK(uncertain.snapshot()->tuning_ownership_uncertain && !uncertain.select_device("b"));
    fifth->missing_tuning=false;
    CHECK(uncertain.submit(release_hold,uncertain.snapshot()->generation));
    CHECK(until([&] { auto p=uncertain.snapshot(); return !p->busy && !p->tuning_ownership_uncertain; }));
    uncertain.begin_shutdown(); CHECK(until([&] { return uncertain.finished(); }));    std::printf("session_test: %u checks passed\n", checks);
}
