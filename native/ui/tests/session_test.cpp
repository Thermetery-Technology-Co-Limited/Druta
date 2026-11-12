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
    HANDLE maintenance_entered = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE maintenance_release = CreateEventW(nullptr, TRUE, TRUE, nullptr);
    CRITICAL_SECTION lock{};
    std::vector<std::string> calls;
    std::atomic<bool> partial{false}, disconnected{false}, hold_read{false}, hold_action{false};
    std::atomic<bool> telemetry_failure{false}, throw_action{false}, undo_available{false};
    std::atomic<bool> throw_reads{false}, throw_after_action{false}, fail_gate{false};
    std::atomic<bool> i2c_owned{false}, fail_cleanup{false}, missing_i2c_state{false};
    std::atomic<bool> tuning_owned{false}, missing_tuning{false}, maintenance_fail{false}, hold_maintenance{false};
    std::atomic<int> maintenance_count{0}, total_reads{0}, detail_reads{0}, board_w{200}, core_ma{100};
    std::atomic<int> fan_mode{0};
    bool xoc = false, rails = false;
    Fake() { InitializeCriticalSection(&lock); }
    ~Fake() override { CloseHandle(entered); CloseHandle(release); CloseHandle(maintenance_entered); CloseHandle(maintenance_release); DeleteCriticalSection(&lock); }
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
        ++maintenance_count;
        if (hold_maintenance.exchange(false)) { SetEvent(maintenance_entered); WaitForSingleObject(maintenance_release, INFINITE); }
        ActionResult r; r.ok=!maintenance_fail.load(); r.message=r.ok ? "" : "hold needs attention"; return r;
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
        const int fans=fan_mode.load();
        if(fans){
            std::fill(std::begin(d.telemetry.fans),std::end(d.telemetry.fans),dc_fan_reading{});
            d.telemetry.fan_count=0;
            if(fans==1){
                d.telemetry.present|=DC_T_NUM_FANS;d.telemetry.num_fans=d.telemetry.fan_count=2;
                d.telemetry.fans[0]={1,45,1,1000};d.telemetry.fans[1]={1,60,1,2000};
            }else if(fans==2){
                d.telemetry.present|=DC_T_NUM_FANS;d.telemetry.num_fans=d.telemetry.fan_count=2;
                d.telemetry.fans[0]={1,0,0,0};d.telemetry.fans[1]={0,0,1,2100};
            }else if(fans==3){d.telemetry.fan_count=1;d.telemetry.fans[0]={0,0,1,1100};}
            else if(fans==4){
                d.telemetry.present|=DC_T_NUM_FANS;d.telemetry.num_fans=d.telemetry.fan_count=1;
                d.telemetry.fans[0]={1,50,1,1300};
            }else if(fans==5){d.telemetry.present|=DC_T_NUM_FANS;d.telemetry.num_fans=0;}
            // Mode 6: no count or field reading, while unrelated telemetry works.
        }
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
        if (cmd.operation == Command::Operation::i2c_zero_nct_outputs) i2c_owned=true;
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
    recovery.begin_shutdown(); CHECK(until([&] { return recovery.finished(); }));
    auto zero_owned=std::make_unique<Fake>(); auto* zero_fake=zero_owned.get();
    Session zero_session(std::move(zero_owned), [] {}); zero_session.start("a");
    CHECK(until([&] { return !zero_session.snapshot()->history.empty(); }));
    Command zero_outputs{}; zero_outputs.operation=Command::Operation::i2c_zero_nct_outputs;
    zero_fake->stall_action(); CHECK(zero_session.submit(zero_outputs,zero_session.snapshot()->generation));
    CHECK(WaitForSingleObject(zero_fake->entered,2000)==WAIT_OBJECT_0);
    CHECK(!zero_session.select_device("b")); // Batch intent is I2C work while in flight.
    SetEvent(zero_fake->release);
    CHECK(until([&] { auto p=zero_session.snapshot(); return !p->busy && p->last_action_operation==zero_outputs.operation && p->data.i2c.clock_owned; }));
    CHECK(!zero_session.snapshot()->i2c_ownership_uncertain && !zero_session.select_device("b"));
    CHECK(zero_session.submit(release_i2c,zero_session.snapshot()->generation));
    CHECK(until([&] { auto p=zero_session.snapshot(); return !p->busy && !p->data.i2c.clock_owned; }));
    // An exception after a possible write and unavailable ownership readback
    // must retain the uncertainty gate until explicit recovery succeeds.
    zero_fake->missing_i2c_state=true; zero_fake->throw_action=true;
    CHECK(zero_session.submit(zero_outputs,zero_session.snapshot()->generation));
    CHECK(until([&] { auto p=zero_session.snapshot(); return !p->busy && p->last_action_operation==zero_outputs.operation && !p->last_action_ok && p->i2c_ownership_uncertain; }));
    CHECK(!zero_session.select_device("b"));
    zero_fake->missing_i2c_state=false;
    CHECK(zero_session.submit(release_i2c,zero_session.snapshot()->generation));
    CHECK(until([&] { auto p=zero_session.snapshot(); return !p->busy && !p->i2c_ownership_uncertain && !p->data.i2c.clock_owned; }));
    zero_session.begin_shutdown(); CHECK(until([&] { return zero_session.finished(); }));
    auto fourth_owned=std::make_unique<Fake>(); auto* fourth=fourth_owned.get();
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
    ResetEvent(fourth->maintenance_release); fourth->hold_maintenance=true;
    Command hold{}; hold.operation=Command::Operation::hold_curve_point;
    CHECK(maintained.submit(hold,maintained.snapshot()->generation));
    CHECK(until([&] { auto p=maintained.snapshot(); return !p->busy && !p->data.connected && p->data.tuning.owned; }));
    CHECK(!maintained.select_device("b"));
    const int reads_after_hold=fourth->total_reads.load();
    CHECK(WaitForSingleObject(fourth->maintenance_entered, 2000)==WAIT_OBJECT_0);
    const auto has_maintenance_error = [](const auto& snapshot) {
        return std::any_of(snapshot->log.begin(), snapshot->log.end(), [](const auto& entry) {
            return entry.message == "hold needs attention";
        });
    };
    CHECK(fourth->maintenance_count.load()==1 && !has_maintenance_error(maintained.snapshot()));
    SetEvent(fourth->maintenance_release);
    CHECK(until([&] { return has_maintenance_error(maintained.snapshot()); }));
    const auto log_size=maintained.snapshot()->log.size();
    // The worker is serial: entry into the third pass proves the second pass
    // finished and had its opportunity to publish a duplicate error.
    CHECK(until([&] { return fourth->maintenance_count.load()>=3; }));
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
    uncertain.begin_shutdown(); CHECK(until([&] { return uncertain.finished(); }));
    // Independent transient duty/RPM failures retain only previously observed
    // fields. Fresh fields, including a stopped fan's zero, remain current.
    auto fan_owned=std::make_unique<Fake>();auto* fan_fake=fan_owned.get();fan_fake->fan_mode=6;
    Session fans(std::move(fan_owned),[]{});fans.start("a");
    CHECK(until([&]{return !fans.snapshot()->opening&&fans.snapshot()->completed_reads>0;}));
    CHECK(fans.snapshot()->data.telemetry.fan_count==0&&!fans.snapshot()->fan_readings_stale);
    const auto fan_poll=[&](int mode){
        const auto before=fans.snapshot()->completed_reads;fan_fake->fan_mode=mode;fans.request_poll();
        CHECK(until([&]{return fans.snapshot()->completed_reads>before;}));return fans.snapshot();
    };
    auto fan_full=fan_poll(1);CHECK(fan_full->data.telemetry.fan_count==2&&!fan_full->fan_readings_stale);
    auto fan_partial=fan_poll(2);
    CHECK(fan_partial->fan_duty_stale_mask==2&&fan_partial->fan_rpm_stale_mask==1&&fan_partial->fan_readings_stale);
    CHECK(fan_partial->data.telemetry.fans[0].has_duty&&fan_partial->data.telemetry.fans[0].duty==0);
    CHECK(fan_partial->data.telemetry.fans[0].rpm==1000&&fan_partial->data.telemetry.fans[1].duty==60);
    CHECK(fan_partial->data.telemetry.fans[1].rpm==2100&&fan_partial->data.controls[0].writable);
    auto no_count=fan_poll(3);
    CHECK(no_count->data.telemetry.fan_count==2&&no_count->fan_duty_stale_mask==3&&no_count->fan_rpm_stale_mask==2);
    CHECK(no_count->data.telemetry.fans[0].duty==0&&no_count->data.telemetry.fans[0].rpm==1100);
    CHECK(no_count->data.telemetry.fans[1].rpm==2100);
    auto recovered=fan_poll(1);CHECK(!recovered->fan_readings_stale&&recovered->fan_duty_stale_mask==0&&recovered->fan_rpm_stale_mask==0);
    fan_fake->telemetry_failure=true;auto failed_fans=fan_poll(1);
    CHECK(failed_fans->fan_duty_stale_mask==3&&failed_fans->fan_rpm_stale_mask==3&&failed_fans->data.telemetry.fan_count==2);
    fan_fake->telemetry_failure=false;auto smaller=fan_poll(4);
    CHECK(smaller->data.telemetry.fan_count==1&&!smaller->fan_readings_stale&&!smaller->data.telemetry.fans[1].has_duty);
    fans.set_paused(true);fans.set_paused(false);
    CHECK(fans.snapshot()->fan_duty_stale_mask==1&&fans.snapshot()->fan_rpm_stale_mask==1);
    auto zero_fans=fan_poll(5);CHECK(zero_fans->data.telemetry.fan_count==0&&!zero_fans->fan_readings_stale);
    auto still_unknown=fan_poll(6);CHECK(still_unknown->data.telemetry.fan_count==0&&!still_unknown->fan_readings_stale);
    fan_poll(1);fan_fake->fan_mode=6;const auto old_fan_generation=fans.snapshot()->generation;
    CHECK(fans.select_device("b"));CHECK(until([&]{auto p=fans.snapshot();return !p->opening&&p->generation>old_fan_generation;}));
    CHECK(fans.snapshot()->data.telemetry.fan_count==0&&fans.snapshot()->fan_duty_stale_mask==0&&fans.snapshot()->fan_rpm_stale_mask==0);
    CHECK(fan_full->data.telemetry.fans[0].duty==45&&!fan_full->fan_readings_stale); // old publications immutable
    fans.begin_shutdown();CHECK(until([&]{return fans.finished();}));
    std::printf("session_test: %u checks passed\n", checks);
}
