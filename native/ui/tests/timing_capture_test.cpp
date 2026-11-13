// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_capture.h"
#include <cstdio>
#include <stdexcept>
using namespace druta::ui;
int failures=0;void check(bool b,const char* why){if(!b){++failures;std::printf("FAIL: %s\n",why);}}
int main(){TimingCaptureTarget target{2,"GPU-alt","0000:09:00.0","helper-A"};TimingBandObservation obs{target,0,7000,7000,0};
 int reads=0,sleeps=0,acquires=0,releases=0,captures=0;bool qualify=true,foreign_target=false,release_ok=true,throw_acquire=false,transient=false;
 TimingHoldState state=TimingHoldState::none;TimingCaptureCallbacks api;
 api.observe=[&]()->std::optional<TimingBandObservation>{++reads;if(transient&&reads==2)return {};auto value=obs;if(!qualify)value.memory_mhz=400;if(foreign_target)value.target.uuid="GPU-new";return value;};
 api.hold_state=[&]{return state;};api.acquire=[&]()->TimingHoldAcquire{++acquires;if(throw_acquire)throw std::runtime_error("post-dispatch");return {true,true,"held"};};
 api.release=[&](){++releases;return TimingCaptureAction{release_ok,"release failed"};};api.capture=[&](){++captures;return TimingCaptureAction{true,"captured"};};api.sleep_ms=[&](unsigned ms){check(ms==250,"fixed bounded wait");++sleeps;};
 auto direct=capture_qualified_timings(target,api);check(direct.ok&&captures==1&&acquires==0&&releases==0,"already qualified reads directly");
 qualify=false;reads=sleeps=0;auto timeout=capture_qualified_timings(target,api);check(!timeout.ok&&sleeps==24&&releases==1&&captures==1,"timeout bounded and owned hold released");
 state=TimingHoldState::foreign;reads=0;auto blocked=capture_qualified_timings(target,api);check(!blocked.ok&&acquires==1&&releases==1,"foreign hold never released or replaced");
 state=TimingHoldState::owned;reads=sleeps=0;auto owned=capture_qualified_timings(target,api);check(!owned.ok&&releases==1&&acquires==1,"preexisting owned hold is not released");
 state=TimingHoldState::none;reads=0;throw_acquire=true;release_ok=false;auto uncertain=capture_qualified_timings(target,api);check(!uncertain.ok&&uncertain.cleanup_pending&&releases==2,"acquire exception and failed cleanup retain recovery");throw_acquire=false;release_ok=true;
 reads=0;foreign_target=true;auto changed=capture_qualified_timings(target,api);check(!changed.ok&&acquires==2,"changed target refuses before acquire");foreign_target=false;
 reads=0;qualify=false;transient=true;api.sleep_ms=[&](unsigned){++sleeps;if(sleeps==2)qualify=true;};sleeps=0;auto recovered=capture_qualified_timings(target,api);check(recovered.ok&&releases==3&&captures==2,"transient observation recovers within hold");
 TimingCaptureTarget no_uuid=target;no_uuid.uuid.clear();auto no_uuid_obs=obs;no_uuid_obs.target=no_uuid;
 check(timing_capture_in_p0_band(no_uuid_obs),"UUID absence does not change band qualification");
 TimingAutoCapture uuid_free;check(!uuid_free.observe(true,false,no_uuid_obs)&&uuid_free.observe(true,false,no_uuid_obs),"NVAPI-only target qualifies without UUID");
 auto appeared=no_uuid_obs;appeared.target.uuid="GPU-now-visible";TimingAutoCapture changed_uuid;
 check(!changed_uuid.observe(true,false,no_uuid_obs)&&!changed_uuid.observe(true,false,appeared),"UUID appearing changes target and restarts stability");
 TimingAutoCapture geometry;auto different=obs;check(!geometry.observe(true,false,obs),"first sample arms");different.memory_offset_raw=2;
 check(!geometry.observe(true,false,different),"changed offset is not stable");different.top_memory_mhz=7001;
 check(!geometry.observe(true,false,different)&&geometry.observe(true,false,different),"changed top clock needs a second matching sample");
 TimingAutoCapture automatic;check(!automatic.observe(true,false,obs)&&automatic.observe(true,false,obs)&&!automatic.observe(true,false,obs),"one auto capture after stable entry");
 check(!automatic.observe(true,true,obs)&&!automatic.observe(true,false,obs),"pause never polls or repeats existing entry");
 check(!automatic.observe(true,false,{})&&!automatic.observe(true,false,obs),"transient missing telemetry does not rearm a fired entry");
 auto low=obs;low.memory_mhz=400;check(!automatic.observe(true,false,low)&&!automatic.observe(true,false,obs)&&automatic.observe(true,false,obs),"leaving band rearms");
 automatic.observe(false,false,obs);check(!automatic.observe(true,false,obs)&&automatic.observe(true,false,obs),"toggle rearms");
 std::printf("Timing capture: %d failures\n",failures);return failures?1:0;}
