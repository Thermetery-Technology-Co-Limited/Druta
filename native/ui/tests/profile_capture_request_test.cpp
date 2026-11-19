// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_capture_request.h"
#include <cstdio>
#include <stdexcept>
using namespace druta::ui;
namespace {
unsigned checks=0,failed=0;
#define CHECK(x) do{++checks;if(!(x)){++failed;std::fprintf(stderr,"Capture request line %d: %s\n",__LINE__,#x);}}while(false)
template<class F>void rejects(F fn){bool did=false;try{fn();}catch(const std::exception&){did=true;}CHECK(did);}
void run(){
 ViewState view;SessionSnapshot snapshot;snapshot.generation=7;snapshot.data.connected=true;
 view.controls_unlocked=false;view.timings_view.generation=7;
 auto full=profile_capture_request(ProfileScope::full_tune,view,snapshot);CHECK(full.operation==Command::Operation::save_full_profile&&full.annotation_snapshot&&full.curve_deltas.empty());
 CHECK(profile_capture_request(ProfileScope::daily,view,snapshot).operation==Command::Operation::save_profile);
 CHECK(profile_capture_request(ProfileScope::curve,view,snapshot).operation==Command::Operation::save_curve_profile);
 CHECK(profile_capture_request(ProfileScope::fan,view,snapshot).operation==Command::Operation::save_fan_profile);
 snapshot.busy=true;rejects([&]{profile_capture_request(ProfileScope::full_tune,view,snapshot);});snapshot.busy=false;snapshot.data.connected=false;rejects([&]{profile_capture_request(ProfileScope::full_tune,view,snapshot);});snapshot.data.connected=true;
 view.scoped_profile_view.generation=6;view.scoped_profile_view.selection={ScopedKey::board_power()};rejects([&]{profile_capture_request(ProfileScope::scoped,view,snapshot);});view.scoped_profile_view.generation=7;
 auto scoped=profile_capture_request(ProfileScope::scoped,view,snapshot);CHECK(scoped.scoped_profile&&scoped.scoped_profile->selection.size()==1&&scoped.scoped_profile->document.is_null());
 view.voltage_policy_view.generation=7;rejects([&]{profile_capture_request(ProfileScope::voltage_policy,view,snapshot);});view.voltage_policy_view.included[1]=true;view.voltage_policy_view.drafts[1]=true;auto policy=profile_capture_request(ProfileScope::voltage_policy,view,snapshot);CHECK(policy.voltage_policy->selection==2&&policy.voltage_policy->values==0&&!policy.voltage_policy->expected);
 view.clock_gating_view.generation=7;view.clock_gating_view.included[3]=true;view.clock_gating_view.pcie_generation=5;auto clock=profile_capture_request(ProfileScope::clock_gating,view,snapshot);CHECK(clock.clock_gating_selection->size()==1&&clock.clock_gating_selection->front()==ClockGatingFamily::pcie&&!clock.clock_gating_requests);
 view.power_integral_view.generation=7;view.power_integral_view.included[4][1]=true;auto integral=profile_capture_request(ProfileScope::power_integral,view,snapshot);CHECK(integral.integral_selection->size()==1&&integral.integral_selection->front().policy==4&&integral.integral_selection->front().field==PowerIntegralField::upper&&!integral.integral_requests);
 rejects([&]{profile_capture_request(ProfileScope::icx,view,snapshot);});snapshot.data.i2c.family=I2cFamily::nct3933;snapshot.data.i2c.selected=I2cRoute{1,0x2d};rejects([&]{profile_capture_request(ProfileScope::icx,view,snapshot);});snapshot.data.i2c.family=I2cFamily::icx;snapshot.data.i2c.captured_preset=false;snapshot.data.i2c.selection_revision=9;snapshot.data.i2c.prepared_preset_revision=12;auto icx=profile_capture_request(ProfileScope::icx,view,snapshot);CHECK(icx.i2c_selection_revision==9&&icx.i2c_preset_revision==12&&icx.operation==Command::Operation::i2c_save_profile);
 view.voltage_tables_view.generation=7;VoltageTablesDraft offset;offset.key={VoltageTableFamily::offsets,1,2};offset.include=true;offset.text[0]='9';view.voltage_tables_view.drafts.push_back(offset);auto offsets=profile_capture_request(ProfileScope::voltage_tables,view,snapshot);CHECK(offsets.voltage_families==2&&offsets.voltage_table_selection->size()==1&&!offsets.voltage_table_requests);
 VoltageTablesDraft request;request.key={VoltageTableFamily::requests,0,0};request.include=true;view.voltage_tables_view.drafts.push_back(request);rejects([&]{profile_capture_request(ProfileScope::voltage_tables,view,snapshot);});view.voltage_tables_view.drafts[0].include=false;auto tables=profile_capture_request(ProfileScope::voltage_tables,view,snapshot);CHECK(tables.voltage_families==1&&tables.voltage_table_selection->size()==1&&tables.voltage_table_selection->front().family==VoltageTableFamily::requests); view.timings_view.staged={{"tRCD",5}};auto timing=profile_capture_request(ProfileScope::timing,view,snapshot);CHECK(timing.operation==Command::Operation::timings_save_profile&&timing.timing_editor->fields.empty()&&view.timings_view.staged.size()==1);
 rejects([&]{profile_capture_request(ProfileScope::unsupported,view,snapshot);});CHECK(view.scoped_profile_view.selection.size()==1&&view.voltage_policy_view.drafts[1]);
}
}
int main(){run();std::printf("%u checks, %u failures\n",checks,failed);return failed?1:0;}
