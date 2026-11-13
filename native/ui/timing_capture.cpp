// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_capture.h"
#include <exception>
namespace druta::ui {
bool operator==(const TimingCaptureTarget& a,const TimingCaptureTarget& b){return a.generation==b.generation&&a.uuid==b.uuid&&a.slot==b.slot&&a.helper_token==b.helper_token;}
namespace {bool target_valid(const TimingCaptureTarget& t){return t.generation&&!t.slot.empty()&&!t.helper_token.empty();}}
bool timing_capture_in_p0_band(const TimingBandObservation& o){
 return o.pstate && *o.pstate==0 && o.memory_mhz && *o.memory_mhz>0 && o.top_memory_mhz && *o.top_memory_mhz>0 && o.memory_offset_raw &&
     static_cast<double>(*o.memory_mhz)-static_cast<double>(*o.memory_offset_raw)/2.0>=static_cast<double>(*o.top_memory_mhz)-5.0;
}
TimingCaptureResult capture_qualified_timings(const TimingCaptureTarget& target,const TimingCaptureCallbacks& api){
 TimingCaptureResult result;bool cleanup=false;
 auto finish=[&](){if(cleanup){try{if(!api.release){result.cleanup_pending=true;result.ok=false;result.message+="; hold release unavailable";}
     else {auto released=api.release();if(!released.ok){result.cleanup_pending=true;result.ok=false;result.message+="; hold release failed: "+released.message;}}}
     catch(...){result.cleanup_pending=true;result.ok=false;result.message+="; hold release failed";}}return result;};
 try{
  if(!target_valid(target)||!api.observe||!api.capture){result.message="Selected GPU or timing helper is unavailable";return finish();}
  auto observation=api.observe();
  if(observation && !(observation->target==target)){result.message="GPU or helper changed before capture";return finish();}
  bool qualified=observation && timing_capture_in_p0_band(*observation);
  if(!qualified){
   if(!api.hold_state || !api.sleep_ms){result.message="No qualified P0 hold is available; capture manually at the top memory band";return finish();}
   const auto existing=api.hold_state();
   if(existing==TimingHoldState::foreign || existing==TimingHoldState::uncertain){result.message="A foreign or uncertain hold prevents automatic capture";return finish();}
   if(existing==TimingHoldState::none){
    if(!api.acquire || !api.release){result.message="Temporary P0 hold unavailable";return finish();}
    cleanup=true; // An exception after dispatch can still leave our temporary owner.
    const auto held=api.acquire();cleanup=held.cleanup_required;
    if(!held.confirmed || !held.cleanup_required){result.message=held.message.empty()?"Temporary P0 hold could not be confirmed":held.message;return finish();}
   }
   for(unsigned attempt=0;attempt<24&&!qualified;++attempt){
    api.sleep_ms(250);observation=api.observe();
    if(observation && !(observation->target==target)){result.message="GPU or helper changed while waiting";return finish();}
    qualified=observation && timing_capture_in_p0_band(*observation);
   }
   if(!qualified){result.message="P0 and the top memory band were not reached within six seconds";return finish();}
  }
  const auto captured=api.capture();result.ok=captured.ok;result.captured=captured.ok;result.message=captured.message;
  const auto after=api.observe();
  if(!after || !(after->target==target) || !timing_capture_in_p0_band(*after)) {result.ok=false;result.message+="; target or qualifying band unavailable after capture";}
  return finish();
 }catch(const std::exception& e){result.message=std::string("Timing capture failed: ")+e.what();return finish();}
 catch(...){result.message="Timing capture failed";return finish();}
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
