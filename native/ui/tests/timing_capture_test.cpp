// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_capture.h"
#include <cstdio>
#include <stdexcept>
using namespace druta::ui;
int failures=0;void check(bool b,const char* why){if(!b){++failures;std::printf("FAIL: %s\n",why);}}
static void fallback_tests(){
 TimingCaptureTarget target{9,"GPU-other","0000:07:00.0","helper"};
 TimingBandObservation observation{target,0,300,7000,0};
 TimingCaptureCallbacks api;int acquire=0,release=0,load=0,capture=0;bool release_ok=true,cancel=false;
 auto control=std::make_shared<TimingCaptureControl>();control->cancelled=[&]{return cancel;};
 TimingCaptureOptions options;options.allow_hold=false;options.control=control;
 api.observe=[&](){return std::optional<TimingBandObservation>{observation};};
 api.hold_state=[](){return TimingHoldState::none;};
 api.acquire=[&](){++acquire;return TimingHoldAcquire{false,false,"unavailable"};};
 api.release=[&](){++release;return TimingCaptureAction{release_ok,"cleanup"};};
 api.capture=api.capture_loaded=[&](){++capture;return TimingCaptureAction{true,"captured",true};};
 api.sleep_ms=[](unsigned){};
 api.workload=[&](const std::function<TimingCaptureAction()>& action){++load;observation.pstate=2;observation.memory_mhz=7000;return action();};
 auto result=capture_qualified_timings(target,api,options);
 check(result.ok&&result.method==TimingCaptureMethod::cuda&&capture==1&&acquire==0&&release==0,"locked tuning uses explicit loaded P2 at current top band without SET");
 observation.pstate=0;load=capture=0;result=capture_qualified_timings(target,api,options);
 check(result.ok&&result.method==TimingCaptureMethod::direct&&load==0&&capture==1,"direct P0 never starts available CUDA fallback");
 observation.memory_mhz=300;options.allow_hold=true;load=0;result=capture_qualified_timings(target,api,options);
 check(result.ok&&acquire==1&&load==1&&release==0,"unissued failed hold may fall back");
 observation.pstate=0;observation.memory_mhz=300;api.acquire=[&](){++acquire;return TimingHoldAcquire{false,true,"uncertain"};};release_ok=false;load=0;
 result=capture_qualified_timings(target,api,options);
 check(!result.ok&&result.cleanup_pending&&release==1&&load==0,"uncertain acquisition never runs load before verified cleanup; failed cleanup attempted once");
 release_ok=true;result=capture_qualified_timings(target,api,options);
 check(result.ok&&release==2&&load==1,"verified own cleanup precedes fallback");
 options.allow_hold=false;observation.memory_mhz=300;observation.pstate=0;
 api.workload=[&](const std::function<TimingCaptureAction()>& action){++load;observation.memory_mhz=7000;auto r=action();r.ok=false;r.message="load exited during capture";return r;};
 result=capture_qualified_timings(target,api,options);
 check(!result.ok&&result.diagnostic_captured,"early workload exit invalidates capture but preserves diagnostic evidence");
 observation.memory_mhz=300;api.workload=[](const std::function<TimingCaptureAction()>&){return TimingCaptureAction{true,"fake success"};};
 result=capture_qualified_timings(target,api,options);check(!result.ok,"workload cannot succeed without invoking capture");
 api.workload=[](const std::function<TimingCaptureAction()>&){return TimingCaptureAction{false,"CUDA export cuInit unavailable"};};
 result=capture_qualified_timings(target,api,options);check(!result.ok&&result.message.find("cuInit")!=std::string::npos,"unavailable workload preserves its specific diagnostic");
 const auto before=capture;cancel=true;result=capture_qualified_timings(target,api,options);
 check(!result.ok&&result.cancelled&&capture==before,"cancellation before operation prevents capture");
 cancel=false;control->deadline=TimingCaptureControl::Clock::now();result=capture_qualified_timings(target,api,options);
 check(!result.ok&&!result.cancelled&&capture==before,"expired shared deadline prevents capture");
 control->deadline=TimingCaptureControl::Clock::time_point::max();options.allow_hold=true;
 api.acquire=[](){return TimingHoldAcquire{true,true,"held"};};api.sleep_ms=[&](unsigned){cancel=true;};
 const auto prior_release=release;result=capture_qualified_timings(target,api,options);
 check(!result.ok&&result.cancelled&&release==prior_release+1&&!result.cleanup_pending,"cancellation during hold wait still releases only own hold");
 cancel=false;options.allow_hold=false;api.hold_state=[](){return TimingHoldState::foreign;};load=0;
 result=capture_qualified_timings(target,api,options);check(!result.ok&&load==0,"foreign ownership blocks workload fallback");
 api.hold_state=[](){return TimingHoldState::none;};api.workload=[&](const std::function<TimingCaptureAction()>& action){observation.pstate=2;observation.memory_mhz=5000;return action();};
 result=capture_qualified_timings(target,api,options);check(!result.ok&&result.diagnostic_captured,"below-floor P2 remains diagnostic, never qualified");
 observation.pstate=2;observation.memory_mhz=7000;observation.memory_offset_raw=100;
 check(!timing_capture_in_top_band(observation),"loaded P2 still subtracts current offset and uses current top floor");
}
// Released 1.9.1 timings_p0_watch / auto_wanted, as Classic gates auto-capture.
static void classic_gate_tests(){
 TimingCaptureTarget target{4,"GPU-gate","0000:0a:00.0","helper"};TimingBandObservation p0{target,0,7000,7000,0},low{target,8,405,7000,0};
 const auto t0=TimingAutoGate::Clock::time_point{}+std::chrono::hours(1);
 bool have=false;TimingAutoGate gate;gate.captured_top_band=[&](uint32_t mem){return have&&mem==7000;};gate.now=t0;
 // An entry seen while a read is in flight is consumed: no read on that entry, even once idle.
 TimingAutoCapture busy;gate.busy=true;check(!busy.observe(true,false,p0,&gate),"busy entry does not read");
 gate.busy=false;check(!busy.observe(true,false,p0,&gate)&&!busy.observe(true,false,p0,&gate),"busy entry is consumed, not deferred");
 // Re-entry with a top-band capture already filed for that clock stays quiet.
 have=true;check(!busy.observe(true,false,low,&gate)&&!busy.observe(true,false,p0,&gate)&&!busy.observe(true,false,p0,&gate),"captured clock is not re-read");
 // A clock without one reads on the entry sample itself (released 1.9.1 fires on
 // the first P0 sample); a re-entry inside 30 s does not, after 30 s it does.
 have=false;TimingAutoCapture gap;check(gap.observe(true,false,p0,&gate)&&!gap.observe(true,false,p0,&gate),"first wanted entry reads at once, once");
 gate.now=t0+std::chrono::seconds(29);check(!gap.observe(true,false,low,&gate)&&!gap.observe(true,false,p0,&gate)&&!gap.observe(true,false,p0,&gate),"AUTO_MIN_GAP holds a quick re-entry");
 // Toggling the box re-arms the edge but keeps the last automatic read's time.
 gap.observe(false,false,p0,&gate);check(!gap.observe(true,false,p0,&gate)&&!gap.observe(true,false,p0,&gate),"toggle keeps the gap");
 gate.now=t0+std::chrono::seconds(31);check(!gap.observe(true,false,low,&gate)&&gap.observe(true,false,p0,&gate),"after the gap a re-entry reads");
 // A new adapter session forgets the gap.
 auto next=p0;next.target.generation=5;gate.now=t0+std::chrono::seconds(32);
 check(gap.observe(true,false,next,&gate),"adapter switch clears the gap");
 // Released timings_auto_toggled re-arms on every toggle: ticking the box while
 // the card is already at P0 reads without waiting for a fresh entry.
 TimingAutoCapture ticked;TimingAutoGate fresh;fresh.captured_top_band=[](uint32_t){return false;};fresh.now=t0;
 for(int i=0;i<3;++i)check(!ticked.observe(false,false,p0,&fresh),"unticked never reads");
 check(ticked.observe(true,false,p0,&fresh),"ticking at P0 reads");
 // Without a gate (Focused) the previous behaviour is unchanged: two equal samples.
 TimingAutoCapture plain;check(!plain.observe(true,false,p0)&&plain.observe(true,false,p0),"ungated entry reads");
 // Released 1.9.1 hysteresis: leaving P0 keeps the entry until the clock falls
 // below the next enumerated state down (here 6801), so a wobble or a P2 load at
 // the top clock does not count as a new entry.
 check(timing_auto_exit_below({405,810,5001,6801,7000})==std::optional<uint32_t>(6801)&&timing_auto_exit_below({7000})==std::optional<uint32_t>(7000)&&
  !timing_auto_exit_below({}),"exit threshold: the next state down, the top when alone, unknown without states");
 TimingAutoGate band;band.captured_top_band=[](uint32_t){return false;};band.now=t0;band.exit_below_mhz=6801;
 TimingBandObservation p2_top{target,2,7000,7000,0},between{target,0,6900,7000,0},mid{target,5,5001,7000,0};
 TimingAutoCapture held;check(held.observe(true,false,p0,&band),"entry reads");
 band.now=t0+std::chrono::seconds(60);
 check(!held.observe(true,false,p2_top,&band)&&!held.observe(true,false,between,&band)&&!held.observe(true,false,p0,&band),"no new entry above the next state down");
 check(!held.observe(true,false,mid,&band)&&held.observe(true,false,p0,&band),"falling below the next state down re-arms the entry");
 // While the states are unknown the entry clears on leaving P0, as before.
 TimingAutoGate unknown=band;unknown.exit_below_mhz.reset();unknown.now=t0;TimingAutoCapture loose;
 check(loose.observe(true,false,p0,&unknown),"entry reads");unknown.now=t0+std::chrono::seconds(60);
 check(!loose.observe(true,false,p2_top,&unknown)&&loose.observe(true,false,p0,&unknown),"unknown states: leaving P0 re-arms");
 // Focused keeps its exit rule whatever the gate would say.
 TimingAutoCapture focused;check(!focused.observe(true,false,p0)&&focused.observe(true,false,p0)&&!focused.observe(true,false,p2_top)&&!focused.observe(true,false,p0)&&focused.observe(true,false,p0),"Focused re-arms on leaving P0");
}
int main(){TimingCaptureTarget target{2,"GPU-alt","0000:09:00.0","helper-A"};TimingBandObservation obs{target,0,7000,7000,0};
 fallback_tests();classic_gate_tests();
 // Released 1.9.1 _tim_busy: only a timing read of its own (the held capture or
 // a plain read; the host says which) consumes a P0 entry in Classic; an
 // unrelated command defers the sample, so the entry still captures once it
 // finishes. Focused defers on any busy, as before.
 check(timing_auto_busy(true,true,true)==TimingAutoBusy::consume&&timing_auto_busy(true,false,true)==TimingAutoBusy::consume,"Classic capture in flight consumes the entry");
 check(timing_auto_busy(true,true,false)==TimingAutoBusy::defer&&timing_auto_busy(true,false,false)==TimingAutoBusy::idle,"Classic unrelated command only defers");
 check(timing_auto_busy(false,true,true)==TimingAutoBusy::defer&&timing_auto_busy(false,false,true)==TimingAutoBusy::idle&&timing_auto_busy(false,true,false)==TimingAutoBusy::defer,"Focused gating unchanged");
 {TimingAutoGate idle_gate,busy_gate;busy_gate.busy=true;TimingAutoCapture deferred,consumed;
  // Deferred samples are never observed; the edge then fires on the first entry sample seen.
  check(deferred.observe(true,false,obs,&idle_gate),"an entry deferred past an unrelated command still captures");
  check(!consumed.observe(true,false,obs,&busy_gate)&&!consumed.observe(true,false,obs,&idle_gate)&&!consumed.observe(true,false,obs,&idle_gate),"an entry seen during a capture is consumed");}
 // The host's plain reads (startup, automatic, Re-read) count as timing reads
 // until the session's completed-action count moves past them.
 {TimingReadFlight reads;check(!reads.observe(4,10,false),"nothing queued");
  reads.queued(4,10);check(reads.observe(4,10,true)&&reads.observe(4,10,true),"a queued read runs while the count stands");
  check(timing_auto_busy(true,true,reads.observe(4,10,true))==TimingAutoBusy::consume,"Classic consumes an entry during a plain read");
  check(!reads.observe(4,11,true),"a moved count ends it, even while the session is still busy");
  // Re-read: queued while drawing, counted from the previous tick's count.
  check(!reads.observe(4,11,false),"idle");reads.queued_since_observed();
  check(reads.observe(4,11,true)&&!reads.observe(4,12,false),"Re-read runs until its action completes");
  // A refused queue (session idle at the next tick) or an adapter change is no read.
  reads.queued(4,12);check(!reads.observe(4,12,false),"a refused read never ran");
  reads.queued(4,12);check(!reads.observe(5,0,true),"another adapter session");
  TimingReadFlight unseen;unseen.queued_since_observed();check(!unseen.observe(4,0,true),"Re-read before any tick counts nothing");
  // Another command in flight is not a timing read: it only defers.
  check(timing_auto_busy(true,true,reads.observe(5,0,true))==TimingAutoBusy::defer,"an unrelated command defers");}
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
