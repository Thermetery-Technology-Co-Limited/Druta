// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include <windows.h>
#include <atomic>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"line %d: %s\n",__LINE__,#x);std::abort();}}while(false)
static bool until(const std::function<bool()>& p){for(int i=0;i<500;++i){if(p())return true;Sleep(5);}return false;}
struct Backend:IBackend {
 std::atomic<bool> entered{false},proceed{false},owned{false};std::atomic<int> actions{0},opens{0},writes{0};std::string slot;
 std::vector<Device> enumerate()override{return {{"a","A","uuid-a"},{"b","B","uuid-b"}};}
 bool open(const std::string& s,std::string&)override{slot=s;++opens;return true;}void close()override{}
 LiveData read(bool)override{LiveData d;d.connected=true;d.identity.nvapi_ok=1;std::snprintf(d.identity.slot,sizeof(d.identity.slot),"%s",slot.c_str());std::snprintf(d.identity.uuid,sizeof(d.identity.uuid),"uuid-%s",slot.c_str());return d;}
 std::optional<FrequencyLockData> frequency_state()const override{FrequencyLockData f;if(owned){f.ownership.mode=FrequencyLockMode::legacy_p0;f.ownership.recovery=true;}return f;}
 ActionResult execute(const Command& c)override{entered=true;while(!proceed)Sleep(1);proceed=false;entered=false;++actions;ActionResult r;
  if(c.operation==Command::Operation::timings_capture_p0){owned=true;++writes;r.message="hold release failed; recovery retained";}
  else if(c.operation==Command::Operation::timings_release_capture_hold){owned=false;r.ok=true;r.message="released";}return r;}
};
int main(){auto owned=std::make_unique<Backend>();auto* b=owned.get();Session session(std::move(owned),[]{});session.start("a");
 CHECK(until([&]{return session.snapshot()->data.connected&&!session.snapshot()->opening;}));auto generation=session.snapshot()->generation;
 Command c;c.operation=Command::Operation::timings_capture_p0;CHECK(session.submit(c,generation));CHECK(until([&]{return b->entered.load();}));
 CHECK(!session.select_device("b")&&!session.reconnect());b->proceed=true;
 CHECK(until([&]{auto s=session.snapshot();return !s->busy&&s->completed_actions==1;}));
 CHECK(session.snapshot()->data.frequency.ownership.needs_cleanup());CHECK(!session.select_device("b")&&!session.reconnect());
 c.operation=Command::Operation::timings_release_capture_hold;CHECK(session.submit(c,generation));CHECK(until([&]{return b->entered.load();}));
 CHECK(!session.select_device("b")&&!session.reconnect());b->proceed=true;
 CHECK(until([&]{auto s=session.snapshot();return !s->busy&&s->completed_actions==2;}));CHECK(!session.snapshot()->data.frequency.ownership.needs_cleanup());
 CHECK(session.reconnect());CHECK(until([&]{return !session.snapshot()->opening&&b->opens==2;}));CHECK(b->writes==1&&b->actions==2);
 session.begin_shutdown();CHECK(until([&]{return session.finished();}));std::printf("timing capture session: passed\n");}
