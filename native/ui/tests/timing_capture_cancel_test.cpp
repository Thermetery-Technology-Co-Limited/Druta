// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include "timing_model.h"
#include <windows.h>
#include <atomic>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"line %d: %s\n",__LINE__,#x);std::abort();}}while(false)
static bool until(const std::function<bool()>& test){for(unsigned n=0;n<500;++n){if(test())return true;Sleep(5);}return false;}
struct CaptureBackend final:IBackend {
    std::function<bool()> cancelled;
    std::atomic<unsigned> entered{0},exited{0};
    void configure_timing_capture_cancellation(std::function<bool()> callback)override{cancelled=std::move(callback);}
    std::vector<Device> enumerate()override{return {{"a","A","id"}};}
    bool open(const std::string&,std::string&)override{return true;}
    void close()override{}
    LiveData read(bool)override{LiveData data;data.connected=true;data.timings=std::make_shared<TimingViewData>();return data;}
    ActionResult execute(const Command& command)override{
        CHECK(command.operation==Command::Operation::timings_capture_p0);CHECK(cancelled&&!cancelled());++entered;
        CHECK(until([&]{return cancelled();}));++exited;ActionResult out;out.message="Capture cancelled and resources cleaned";return out;
    }
};
// Models a failed capture whose child exit still needs explicit confirmation,
// while unrelated telemetry is unavailable. Maintenance retains the failure;
// only the selected recovery command resolves it.
struct DisconnectedCaptureBackend final:IBackend {
    std::atomic<bool> connected{true},resources{false};std::atomic<unsigned> releases{0};
    std::vector<Device> enumerate()override{return {{"a","A","id"}};}
    bool open(const std::string&,std::string&)override{return true;}
    void close()override{}
    LiveData read(bool)override{LiveData data;data.connected=connected;data.identity.nvapi_ok=1;
        std::snprintf(data.identity.uuid,sizeof(data.identity.uuid),"id");std::snprintf(data.identity.slot,sizeof(data.identity.slot),"a");
        auto timing=std::make_shared<TimingViewData>();timing->capture_workload_pending=resources;data.timings=std::move(timing);return data;}
    bool auxiliary_cleanup_needed()const override{return resources;}
    bool maintenance_needed()const override{return resources;}
    ActionResult maintain()override{ActionResult out;out.ok=!resources;out.message="Capture resource exit remains unconfirmed";return out;}
    ActionResult prepare_close()override{ActionResult out;out.ok=!resources;return out;}
    ActionResult execute(const Command& command)override{CHECK(command.operation==Command::Operation::timings_release_capture_hold);
        ++releases;resources=false;ActionResult out;out.ok=true;out.message="Owned capture resources released without telemetry";return out;}
};
int main(){
    auto backend=std::make_unique<CaptureBackend>();auto* fake=backend.get();Session session(std::move(backend),[]{});session.start("a");
    CHECK(until([&]{return !session.snapshot()->opening&&session.snapshot()->data.connected;}));const auto generation=session.snapshot()->generation;
    Command capture;capture.operation=Command::Operation::timings_capture_p0;
    CHECK(session.submit(capture,generation));CHECK(until([&]{return fake->entered==1;}));CHECK(session.snapshot()->data.timings->capture_running);
    CHECK(!session.cancel_timing_capture(generation+1));CHECK(session.cancel_timing_capture(generation));
    CHECK(until([&]{return !session.snapshot()->busy&&fake->exited==1;}));CHECK(!session.snapshot()->data.timings->capture_running);
    CHECK(session.submit(capture,generation));CHECK(until([&]{return fake->entered==2;}));session.set_paused(true);
    CHECK(until([&]{return !session.snapshot()->busy&&fake->exited==2;}));CHECK(!session.submit(capture,generation));
    session.set_paused(false);Sleep(25);CHECK(fake->entered==2); // Resume never restarts the workload.
    CHECK(session.submit(capture,generation));CHECK(until([&]{return fake->entered==3;}));session.begin_shutdown();
    CHECK(until([&]{return session.finished();}));CHECK(fake->exited==3);
    {
        auto recovery_backend=std::make_unique<DisconnectedCaptureBackend>();auto* recovery=recovery_backend.get();Session disconnected(std::move(recovery_backend),[]{});disconnected.start("a");
        CHECK(until([&]{auto state=disconnected.snapshot();return !state->opening&&state->data.connected;}));const auto recovery_generation=disconnected.snapshot()->generation;
        recovery->resources=true;recovery->connected=false;disconnected.request_poll(true);
        CHECK(until([&]{auto state=disconnected.snapshot();return !state->data.connected&&state->auxiliary_cleanup_pending&&!state->busy;}));
        Command ordinary;ordinary.operation=Command::Operation::apply;ordinary.control=ControlId::memory;ordinary.value=10;
        CHECK(!disconnected.submit(capture,recovery_generation)&&!disconnected.submit(ordinary,recovery_generation)&&recovery->releases==0);
        disconnected.set_paused(true);Command release;release.operation=Command::Operation::timings_release_capture_hold;
        CHECK(disconnected.submit(release,recovery_generation));
        CHECK(until([&]{auto state=disconnected.snapshot();return !state->busy&&state->completed_actions==1&&recovery->releases==1&&!state->auxiliary_cleanup_pending;}));
        CHECK(!disconnected.snapshot()->data.connected&&!recovery->resources);
        disconnected.set_paused(false);CHECK(!disconnected.submit(capture,recovery_generation)&&!disconnected.submit(ordinary,recovery_generation));
        disconnected.begin_shutdown();CHECK(until([&]{return disconnected.finished();}));
    }
    std::puts("Timing capture immediate cancellation passed");
}
