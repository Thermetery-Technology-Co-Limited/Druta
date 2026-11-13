// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_capture.h"
#include <exception>
#include <stdexcept>
namespace druta::ui {
bool operator==(const TimingCaptureTarget& a,const TimingCaptureTarget& b){return a.generation==b.generation&&a.uuid==b.uuid&&a.slot==b.slot&&a.helper_token==b.helper_token;}
namespace {bool target_valid(const TimingCaptureTarget& t){return t.generation&&!t.slot.empty()&&!t.helper_token.empty();}}
bool timing_capture_in_p0_band(const TimingBandObservation& o){
 return o.pstate && *o.pstate==0 && o.memory_mhz && *o.memory_mhz>0 && o.top_memory_mhz && *o.top_memory_mhz>0 && o.memory_offset_raw &&
     static_cast<double>(*o.memory_mhz)-static_cast<double>(*o.memory_offset_raw)/2.0>=static_cast<double>(*o.top_memory_mhz)-5.0;
}
bool timing_capture_in_top_band(const TimingBandObservation& o){
 if(!o.pstate || (*o.pstate!=0 && *o.pstate!=2))return false;
 auto p0=o;p0.pstate=0;return timing_capture_in_p0_band(p0);
}
TimingCaptureResult capture_qualified_timings(const TimingCaptureTarget& target,const TimingCaptureCallbacks& api,const TimingCaptureOptions& options){
 TimingCaptureResult result;bool cleanup=false;
 auto stopped=[&](){if(!options.control||!options.control->stopped())return false;
  result.cancelled=options.control->cancellation_requested();result.ok=false;
  result.message=result.cancelled?"Timing capture cancelled":"Timing capture deadline expired";return true;};
 auto release=[&](){if(!cleanup)return true;try{if(api.release){const auto action=api.release();if(action.ok){cleanup=false;return true;}result.message+="; hold release failed: "+action.message;}
  else result.message+="; hold release unavailable";}catch(...){result.message+="; hold release failed";}
  result.cleanup_pending=true;result.ok=false;return false;};
 auto finish=[&](){if(!result.cleanup_pending)release();return result;};
 auto observe=[&](){auto o=api.observe();if(o&&!(o->target==target))throw std::runtime_error("GPU or helper changed during capture");return o;};
 auto capture=[&](bool loaded){
  TimingCaptureAction action;
  if(stopped())return TimingCaptureAction{false,result.message};
  const auto before=observe();
  const bool band=before&&(loaded?timing_capture_in_top_band(*before):timing_capture_in_p0_band(*before));
  // Loaded captures retain raw diagnostics even below the accepted band.
  if(!loaded&&!band)return TimingCaptureAction{false,"Qualifying P0 band changed before capture"};
  action=loaded&&api.capture_loaded?api.capture_loaded():api.capture();
  result.diagnostic_captured|=action.diagnostic;
  result.captured|=action.ok;
  const auto after=observe();
  const bool after_band=after&&(loaded?timing_capture_in_top_band(*after):timing_capture_in_p0_band(*after));
  const bool stable=before&&after&&before->memory_mhz==after->memory_mhz&&before->top_memory_mhz==after->top_memory_mhz&&
      before->memory_offset_raw==after->memory_offset_raw&&before->pstate==after->pstate;
  if(!band||!after_band||!stable){action.ok=false;action.message+="; target or stable qualifying band unavailable around capture";}
  if(stopped()){action.ok=false;action.message=result.message;}
  return action;
 };
 try{
  if(!target_valid(target)||!api.observe||!api.capture){result.message="Selected GPU or timing helper is unavailable";return finish();}
  if(stopped())return finish();
  auto observation=observe();if(stopped())return finish();
  if(observation&&timing_capture_in_p0_band(*observation)){
   result.method=TimingCaptureMethod::direct;const auto action=capture(false);result.ok=action.ok;result.message=action.message;return finish();
  }
  const auto existing=api.hold_state?api.hold_state():TimingHoldState::none;
  if(stopped())return finish();
  if(existing==TimingHoldState::foreign||existing==TimingHoldState::uncertain){result.message="A foreign or uncertain hold prevents automatic capture";return finish();}
  bool wait_hold=existing==TimingHoldState::owned;
  if(wait_hold)result.method=TimingCaptureMethod::existing_hold;
  if(!wait_hold&&options.allow_hold&&api.acquire&&api.release){
   cleanup=true;const auto held=api.acquire();cleanup=held.cleanup_required;
   wait_hold=held.confirmed&&held.cleanup_required;
   result.message=held.message;
   if(wait_hold)result.method=TimingCaptureMethod::temporary_hold;
   else if(!release())return finish();
  }
  if(stopped())return finish();
  if(wait_hold&&api.sleep_ms){
   for(unsigned attempt=0;attempt<24;++attempt){
    if(stopped())return finish();
    api.sleep_ms(options.control?options.control->remaining_ms(250):250);
    if(stopped())return finish();observation=observe();
    if(observation&&timing_capture_in_p0_band(*observation)){
     const auto action=capture(false);result.ok=action.ok;result.message=action.message;return finish();
    }
   }
   result.message="P0 and the top memory band were not reached within six seconds";
  }
  if(!release()||stopped())return finish();
  // Cleanup can itself leave the adapter qualified. Never create CUDA first.
  observation=observe();if(stopped())return finish();
  if(observation&&timing_capture_in_p0_band(*observation)){
   result.method=TimingCaptureMethod::direct;const auto action=capture(false);result.ok=action.ok;result.message=action.message;return finish();
  }
  if(!api.workload){if(result.message.empty())result.message="Temporary hold unavailable; capture manually at the top memory band";return finish();}
  result.method=TimingCaptureMethod::cuda;
  bool invoked=false,callback_ok=false,duplicate=false;auto action=api.workload([&]{if(invoked){duplicate=true;return TimingCaptureAction{false,"Duplicate workload capture refused"};}invoked=true;auto captured=capture(true);callback_ok=captured.ok;return captured;});
  result.ok=action.ok&&invoked&&callback_ok&&!duplicate;result.message=action.message.empty()?"Workload ended without a qualified timing capture":action.message;result.diagnostic_captured|=action.diagnostic;
  if(stopped())return finish();return finish();
 }catch(const std::exception& e){result.ok=false;result.message=std::string("Timing capture failed: ")+e.what();return finish();}
 catch(...){result.ok=false;result.message="Timing capture failed";return finish();}
}
void TimingAutoCapture::reset(){target_.reset();previous_clock_.reset();previous_top_.reset();previous_offset_.reset();fired_=false;}
bool TimingAutoCapture::observe(bool enabled,bool paused,const std::optional<TimingBandObservation>& o){
 if(!enabled){reset();return false;}if(paused){previous_clock_.reset();return false;}
 if(!o || !target_valid(o->target)){previous_clock_.reset();return false;}
 if(!target_ || !(*target_==o->target)){reset();target_=o->target;}
 if(!o->pstate || !o->memory_mhz || !o->top_memory_mhz || !o->memory_offset_raw){previous_clock_.reset();return false;}
 if(!timing_capture_in_p0_band(*o)){reset();return false;}
 if(fired_)return false;
 if(!previous_clock_ || !previous_top_ || !previous_offset_ || *previous_clock_!=*o->memory_mhz || *previous_top_!=*o->top_memory_mhz || *previous_offset_!=*o->memory_offset_raw){previous_clock_=o->memory_mhz;previous_top_=o->top_memory_mhz;previous_offset_=o->memory_offset_raw;return false;}
 fired_=true;return true;
}
}
