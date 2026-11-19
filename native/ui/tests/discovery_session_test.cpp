// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include <windows.h>
#include <atomic>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do {++checks;if(!(x)){std::fprintf(stderr,"line %d: %s\n",__LINE__,#x);std::abort();}}while(false)
static bool until(const std::function<bool()>& predicate){for(unsigned i=0;i<500;++i){if(predicate())return true;Sleep(5);}return false;}
struct Backend : IBackend {
    std::function<bool()> cancelled;
    std::function<void(const I2cDiscoveryProgress&)> progress;
    std::wstring cache_path;
    std::atomic<bool> entered{false}, late_progress{false}, saw_clear{false};
    std::atomic<unsigned> actions{0}, reads{0}, cleanup{0};
    std::string slot;
    void configure_i2c_discovery(std::function<bool()> c,std::function<void(const I2cDiscoveryProgress&)> p,const std::wstring& path) override {cancelled=std::move(c);progress=std::move(p);cache_path=path;}
    std::vector<Device> enumerate() override {return {{"a","GPU A","uuid-a"},{"b","GPU B","uuid-b"}};}
    bool open(const std::string& s,std::string&) override {slot=s;return true;}
    void close() override {}
    ActionResult prepare_close() override {++cleanup;ActionResult r;r.ok=true;return r;}
    LiveData read(bool) override {
        ++reads;
        if(late_progress.exchange(false)){I2cDiscoveryProgress p;p.running=true;p.label="late callback";progress(p);}
        LiveData data;data.connected=true;data.identity.nvapi_ok=1;
        std::snprintf(data.identity.slot,sizeof(data.identity.slot),"%s",slot.c_str());
        std::snprintf(data.identity.uuid,sizeof(data.identity.uuid),"uuid-%s",slot.c_str());
        return data;
    }
    ActionResult execute(const Command&) override {
        saw_clear=!cancelled();
        I2cDiscoveryProgress p;p.running=true;p.total=10;p.completed=2;p.label="probing";
        p.candidates.push_back({{1,0x20},700,"unverified"});progress(p);entered=true;
        for(unsigned i=0;i<500&&!cancelled();++i)Sleep(5);
        p.running=false;p.cancelled=cancelled();p.label=p.cancelled?"cancelled":"timed out";progress(p);
        ++actions;late_progress=true;ActionResult r;r.ok=false;r.message="Discovery cancelled; no writes";return r;
    }
};
int main(){
    auto owned=std::make_unique<Backend>();auto* backend=owned.get();
    Session session(std::move(owned),[]{},L"C:\\test-only\\routes.json");session.start("a");
    CHECK(until([&]{auto s=session.snapshot();return !s->opening&&s->data.connected;}));
    CHECK(backend->cache_path==L"C:\\test-only\\routes.json");
    auto generation=session.snapshot()->generation;Command scan;scan.operation=Command::Operation::i2c_scan;
    CHECK(!session.cancel_i2c_discovery(generation));
    CHECK(session.submit(scan,generation));CHECK(until([&]{return backend->entered.load();}));
    auto progress=session.snapshot();CHECK(progress->busy&&progress->data.i2c.discovery.running);
    CHECK(progress->data.i2c.discovery.completed==2&&progress->data.i2c.discovery.candidates.size()==1);
    CHECK(!session.cancel_i2c_discovery(generation+1));CHECK(!session.select_device("b"));
    CHECK(session.cancel_i2c_discovery(generation));
    CHECK(until([&]{auto s=session.snapshot();return s->completed_actions==1&&!s->busy;}));
    CHECK(backend->actions==1&&backend->saw_clear);
    CHECK(!session.snapshot()->data.i2c.discovery.running); // callback from post-action read ignored
    CHECK(progress->data.i2c.discovery.running); // prior immutable publication
    CHECK(!session.cancel_i2c_discovery(generation));
    CHECK(session.select_device("b"));CHECK(until([&]{return !session.snapshot()->opening;}));
    CHECK(!session.cancel_i2c_discovery(generation));generation=session.snapshot()->generation;
    backend->entered=false;scan.operation=Command::Operation::i2c_select;
    CHECK(session.submit(scan,generation));CHECK(until([&]{return backend->entered.load();}));
    CHECK(backend->saw_clear); // a previous Cancel must not poison the next operation
    session.begin_shutdown();CHECK(until([&]{return session.finished();}));
    CHECK(backend->actions==2&&backend->cleanup==1);
    std::printf("discovery_session_test: %u checks passed\n",checks);
}
