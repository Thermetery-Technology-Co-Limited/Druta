// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_setup_host.h"
#include "session.h"
#include "ui_view.h"
#include <atomic>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
struct Fake:IBackend {std::wstring helper;std::atomic<int> actions{0};std::vector<Device> enumerate()override{return {{"a","A","uuid"}};}bool open(const std::string&,std::string&)override{return true;}void close()override{}LiveData read(bool)override{LiveData d;d.connected=true;d.timings=std::make_shared<TimingViewData>();const_cast<TimingViewData&>(*d.timings).helper_path=helper;return d;}ActionResult execute(const Command& c)override{++actions;ActionResult out;if(c.operation==Command::Operation::timings_select_helper){out.ok=c.timing_helper_path!=L"bad";out.message=out.ok?"selected":"specific helper refusal";if(out.ok)helper=c.timing_helper_path;}else if(c.operation==Command::Operation::timings_forget_helper){out.ok=true;helper.clear();}return out;}};
int main(){SessionSnapshot sample;sample.generation=4;sample.completed_actions=2;const auto op=Command::Operation::timings_select_helper;CHECK(timing_setup_action_state(sample,5,2,op)==TimingSetupActionState::stale);CHECK(timing_setup_action_state(sample,4,2,op)==TimingSetupActionState::waiting);sample.completed_actions=4;CHECK(timing_setup_action_state(sample,4,2,op)==TimingSetupActionState::superseded);sample.completed_actions=3;sample.last_action_operation=op;CHECK(timing_setup_action_state(sample,4,2,op)==TimingSetupActionState::rejected);sample.last_action_ok=true;CHECK(timing_setup_action_state(sample,4,2,op)==TimingSetupActionState::accepted);
 auto b=std::make_unique<Fake>();auto* fake=b.get();Session session(std::move(b),[]{});session.start("a");for(int i=0;i<500&&(session.snapshot()->opening||!session.snapshot()->data.connected);++i)Sleep(5);CHECK(session.snapshot()->data.connected);std::atomic<int> discovery{0},saves{0},forgets{0},probes{0},execs{0};TimingSetupApi api;api.discover=[&]{++discovery;TimingsHelperSuggestion s;s.registration_present=true;s.registration_message="old registration";return s;};api.persist=[&](const std::wstring&){++saves;};api.forget=[&]{++forgets;throw std::runtime_error("config write failed");};api.probe=[&]{++probes;return TestSigningStatus{};};api.execute=[&](bool){++execs;return TestSigningResult{};};api.reboot=[](){return std::string();};std::atomic<int> driver_reads{0};api.driver=[&]{++driver_reads;return TimingDriverState{"STOPPED",false};};TimingSetupHost host(api);ViewState view;view.page=Page::timings;CHECK(view.presentation==Presentation::focused);
 auto pump=[&]{for(int i=0;i<600;++i){host.consume(session,view,nullptr);if(!host.pending()&&!session.snapshot()->busy)return;Sleep(5);}CHECK(false);};host.consume(session,view,nullptr);pump();CHECK(discovery==1&&saves==0&&forgets==0&&execs==0&&fake->actions==0&&view.timings_view.helper_registration_present);
 host.request_signing(TestSigningViewIntent::refresh);pump();CHECK(probes==1&&execs==0);host.request_helper(L"bad");pump();CHECK(saves==0&&view.timings_view.message=="specific helper refusal");host.request_helper(L"good");pump();CHECK(saves==1&&fake->helper==L"good");
 // Focused never shows the driver state: neither its discovery nor its Locate queries the service manager.
 CHECK(driver_reads==0&&!view.timings_view.helper_driver_running&&view.timings_view.helper_driver_state.empty());view.timing_forget_requested=true;host.consume(session,view,nullptr);pump();CHECK(forgets==1&&view.timings_view.helper_registration_present&&view.timings_view.message=="config write failed"&&driver_reads==0);
 // Classic locates nvtune at launch on any page (released 1.9.1), once per adapter
 // generation; Focused still probes only on its Timings page. Read-only file probe.
 std::atomic<int> classic_probes{0},focused_probes{0},classic_drivers{0},focused_drivers{0};
 TimingSetupApi classic_api;classic_api.discover=[&]{++classic_probes;return TimingsHelperSuggestion{};};classic_api.driver=[&]{++classic_drivers;return TimingDriverState{"not installed",false};};
 TimingSetupApi focused_api;focused_api.discover=[&]{++focused_probes;return TimingsHelperSuggestion{};};focused_api.driver=[&]{++focused_drivers;return TimingDriverState{"RUNNING",true};};
 TimingSetupHost classic_host(classic_api),focused_host(focused_api);
 ViewState classic_view;classic_view.presentation=Presentation::classic;classic_view.page=Page::daily;
 ViewState focused_view;focused_view.presentation=Presentation::focused;focused_view.page=Page::daily;
 const auto settle=[&](TimingSetupHost& h,ViewState& v){for(int i=0;i<600;++i){h.consume(session,v,nullptr);if(!h.pending()&&!session.snapshot()->busy)return;Sleep(5);}CHECK(false);};
 settle(classic_host,classic_view);settle(focused_host,focused_view);
 CHECK(classic_probes==1&&focused_probes==0&&fake->helper.empty()&&classic_view.timings_view.helper_missing);
 CHECK(classic_drivers==1&&classic_view.timings_view.helper_driver_running==std::optional<bool>(false)&&classic_view.timings_view.helper_driver_state=="not installed");
 for(auto page:{Page::monitor,Page::timings,Page::advanced,Page::timings}){classic_view.page=page;settle(classic_host,classic_view);}
 CHECK(classic_probes==1); // page changes never repeat it
 // Focused (539bb9d): no discovery at launch or on any other page; only its Timings page probes.
 for(auto page:{Page::monitor,Page::advanced,Page::rails,Page::i2c,Page::daily}){focused_view.page=page;settle(focused_host,focused_view);}
 CHECK(focused_probes==0&&!focused_view.timings_view.helper_missing&&focused_view.timings_view.message.empty());
 focused_view.page=Page::timings;settle(focused_host,focused_view);CHECK(focused_probes==1&&focused_drivers==0&&!focused_view.timings_view.helper_driver_running);
 // Focused launching on its default page with nvtune installed: no probe and no
 // session action (no helper selection, so nothing a read could follow).
 std::atomic<int> installed_probes{0};const int actions_before=fake->actions;
 TimingSetupApi installed_api;installed_api.discover=[&]{++installed_probes;TimingsHelperSuggestion s;s.path=L"installed";s.message="nvtune found in registration";return s;};
 TimingSetupHost installed_host(installed_api);ViewState launch;CHECK(launch.presentation==Presentation::focused&&launch.page==Page::daily);
 for(int frame=0;frame<20;++frame)settle(installed_host,launch);
 CHECK(installed_probes==0&&fake->actions==actions_before&&fake->helper.empty()&&launch.timings_view.message.empty());
 // Classic's working tab has no 'where nvtune was found' line (released 1.9.1 shows
 // discovery text only on its setup screen); the driver state still arrives.
 TimingSetupApi found_api;found_api.discover=[]{TimingsHelperSuggestion s;s.path=L"found";s.message="nvtune found in registration";return s;};found_api.driver=[]{return TimingDriverState{"RUNNING",true};};
 TimingSetupHost found_host(found_api);ViewState found_view;found_view.presentation=Presentation::classic;found_view.page=Page::monitor;
 settle(found_host,found_view);CHECK(found_view.timings_view.message.empty()&&found_view.timings_view.helper_driver_running==std::optional<bool>(true)&&!found_view.timings_view.helper_missing);
 // The discovery's own selection is marked automatic in the session log (Classic
 // leaves it out, as 1.9.1 finds the helper silently); a located helper is not.
 const auto selection=[&]{const auto snap=session.snapshot();for(auto it=snap->log.rbegin();it!=snap->log.rend();++it)if(it->operation==Command::Operation::timings_select_helper)return *it;return LogEntry{};};
 CHECK(fake->helper==L"found"&&selection().ok&&selection().automatic);
 // Classic's explicit Locate re-reads the driver service state (read only) for its setup screen.
 std::atomic<int> located_saves{0},located_drivers{0};
 TimingSetupApi located_api;located_api.discover=[]{return TimingsHelperSuggestion{};};located_api.persist=[&](const std::wstring&){++located_saves;};
 located_api.driver=[&]{++located_drivers;return located_drivers==1?TimingDriverState{"RUNNING",true}:TimingDriverState{"STOPPED",false};};
 TimingSetupHost located_host(located_api);ViewState located_view;located_view.presentation=Presentation::classic;located_view.page=Page::timings;
 settle(located_host,located_view);CHECK(located_drivers==1&&located_view.timings_view.helper_driver_running==std::optional<bool>(true));
 located_host.request_helper(L"located");settle(located_host,located_view);
 CHECK(located_saves==1&&located_drivers==2&&fake->helper==L"located"&&located_view.timings_view.helper_driver_running==std::optional<bool>(false)&&located_view.timings_view.helper_driver_state=="STOPPED");
 CHECK(selection().ok&&selection().message=="selected"&&!selection().automatic);
 session.begin_shutdown();for(int i=0;i<500&&!session.finished();++i)Sleep(5);CHECK(session.finished());std::puts("Timing setup host passed");}
