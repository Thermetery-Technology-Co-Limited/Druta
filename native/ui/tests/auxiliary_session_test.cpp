// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "session.h"
#include <windows.h>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"line %d: %s\n",__LINE__,#x);std::abort();}}while(false)
static bool until(const std::function<bool()>& p){for(int i=0;i<500;++i){if(p())return true;Sleep(5);}return false;}
struct Backend:IBackend {
 std::atomic<bool> entered{false},proceed{false},owned{false},getter_throws{false},connected{true};
 std::atomic<int> actions{0},opens{0},reads{0},maintenance{0};std::string slot;
 std::vector<Device> enumerate()override{return {{"a","A","uuid-a"},{"b","B","uuid-b"}};}
 bool open(const std::string& s,std::string&)override{slot=s;++opens;return true;}void close()override{}
 bool auxiliary_cleanup_needed()const override{if(getter_throws)throw std::runtime_error("ownership unreadable");return owned;}
 LiveData read(bool)override{++reads;LiveData d;d.connected=connected;d.identity.nvapi_ok=1;std::snprintf(d.identity.slot,sizeof(d.identity.slot),"%s",slot.c_str());std::snprintf(d.identity.uuid,sizeof(d.identity.uuid),"uuid-%s",slot.c_str());return d;}
 ActionResult maintain()override{++maintenance;ActionResult r;r.ok=true;return r;}
 ActionResult execute(const Command& c)override{entered=true;while(!proceed)Sleep(1);proceed=false;entered=false;++actions;ActionResult r;
  if(c.operation==Command::Operation::power_saving_apply){owned=true;getter_throws=true;r.may_have_changed=true;r.message="write uncertain";}
  else if(c.operation==Command::Operation::power_saving_release){owned=false;getter_throws=false;r.ok=true;r.message="released";}return r;}
};
struct RestartCounters {std::atomic<int> opens{0},closes{0},prepare{0},destructs{0},actions{0},reads{0},getters{0},tuning_getters{0},maintenance_getters{0},frequency_getters{0};std::atomic<bool> entered{false},proceed{false},block_tuning{false},tuning_entered{false},tuning_proceed{false};};
struct RestartBackend:IBackend {
 std::shared_ptr<RestartCounters> counts;explicit RestartBackend(std::shared_ptr<RestartCounters> c):counts(std::move(c)){}
 ~RestartBackend()override{++counts->destructs;}
 std::vector<Device> enumerate()override{return {{"a","A","uuid-a"}};}
 bool open(const std::string&,std::string&)override{++counts->opens;return true;}
 void close()override{++counts->closes;}
 bool auxiliary_cleanup_needed()const override{++counts->getters;return true;}
 std::optional<TuningState> tuning_state()const override{++counts->tuning_getters;if(counts->block_tuning){counts->tuning_entered=true;while(!counts->tuning_proceed)Sleep(1);}return TuningState{};}
 bool maintenance_needed()const override{++counts->maintenance_getters;return false;}
 std::optional<FrequencyLockData> frequency_state()const override{++counts->frequency_getters;return FrequencyLockData{};}
 ActionResult prepare_close()override{++counts->prepare;ActionResult r;r.message="owned request cannot be released";return r;}
 LiveData read(bool)override{++counts->reads;LiveData d;d.connected=true;d.identity.nvapi_ok=1;std::snprintf(d.identity.slot,sizeof(d.identity.slot),"a");return d;}
 ActionResult execute(const Command&)override{++counts->actions;counts->entered=true;while(!counts->proceed)Sleep(1);ActionResult r;r.ok=true;return r;}
};
void restart_shutdown_test(){auto counts=std::make_shared<RestartCounters>();{
 Session session(std::make_unique<RestartBackend>(counts),[]{});session.start("a");CHECK(until([&]{return session.snapshot()->data.connected&&!session.snapshot()->opening;}));
 session.begin_shutdown();CHECK(until([&]{return session.snapshot()->close_refused;}));CHECK(!session.finished()&&counts->prepare==1);
 auto generation=session.snapshot()->generation;Command c;c.operation=Command::Operation::power_saving_release;CHECK(session.submit(c,generation));CHECK(until([&]{return counts->entered.load();}));
 const int closes=counts->closes,gets=counts->getters,reads=counts->reads;session.begin_device_restart_shutdown();CHECK(!session.submit(c,generation));CHECK(!session.finished());counts->proceed=true;
 CHECK(until([&]{return session.finished();}));CHECK(counts->prepare==1&&counts->closes==closes&&counts->actions==1&&counts->getters==gets&&counts->reads==reads);
 }CHECK(counts->destructs==0&&counts->prepare==1&&counts->actions==1);
}
void restart_during_getter_test(){auto counts=std::make_shared<RestartCounters>();{
 Session session(std::make_unique<RestartBackend>(counts),[]{});session.start("a");CHECK(until([&]{return session.snapshot()->data.connected&&!session.snapshot()->opening;}));
 const auto generation=session.snapshot()->generation;counts->block_tuning=true;counts->proceed=true;Command command;command.operation=Command::Operation::power_saving_release;
 CHECK(session.submit(command,generation));CHECK(until([&]{return counts->tuning_entered.load();}));
 const int maintenance=counts->maintenance_getters,frequency=counts->frequency_getters,auxiliary=counts->getters,reads=counts->reads,closes=counts->closes;
 session.begin_device_restart_shutdown();CHECK(!session.finished());counts->tuning_proceed=true;
 CHECK(until([&]{return session.finished();}));CHECK(counts->maintenance_getters==maintenance&&counts->frequency_getters==frequency&&counts->getters==auxiliary&&counts->reads==reads&&counts->closes==closes&&counts->prepare==0);
 }CHECK(counts->destructs==0);
}
int main(){auto owner=std::make_unique<Backend>();auto* b=owner.get();Session session(std::move(owner),[]{});session.start("a");
 CHECK(until([&]{auto s=session.snapshot();return s->data.connected&&!s->opening;}));auto generation=session.snapshot()->generation;
 Command c;c.operation=Command::Operation::power_saving_apply;CHECK(session.submit(c,generation));CHECK(until([&]{return b->entered.load();}));
 CHECK(!session.select_device("b")&&!session.reconnect());b->proceed=true;
 CHECK(until([&]{auto s=session.snapshot();return !s->busy&&s->completed_actions==1;}));CHECK(session.snapshot()->auxiliary_cleanup_pending);
 CHECK(!session.select_device("b")&&!session.reconnect());
 b->getter_throws=false;const auto completed=session.snapshot()->completed_reads;session.request_poll(true);
 CHECK(until([&]{return session.snapshot()->completed_reads>completed;}));CHECK(session.snapshot()->auxiliary_cleanup_pending && !session.select_device("b"));
 session.set_paused(true);const int reads=b->reads;Sleep(1100);CHECK(b->reads==reads&&b->maintenance==0);
 b->connected=false;session.set_paused(false);session.request_poll(true);CHECK(until([&]{return !session.snapshot()->data.connected;}));
 c.operation=Command::Operation::power_saving_release;CHECK(session.submit(c,generation));CHECK(until([&]{return b->entered.load();}));
 CHECK(!session.select_device("b")&&!session.reconnect());b->proceed=true;
 CHECK(until([&]{auto s=session.snapshot();return !s->busy&&s->completed_actions==2;}));CHECK(!session.snapshot()->auxiliary_cleanup_pending);
 b->connected=true;CHECK(session.reconnect());CHECK(until([&]{return !session.snapshot()->opening&&b->opens==2;}));CHECK(b->actions==2);
 session.begin_shutdown();CHECK(until([&]{return session.finished();}));restart_shutdown_test();restart_during_getter_test();std::printf("auxiliary session: passed\n");}
