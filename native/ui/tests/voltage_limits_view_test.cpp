// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_limits_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static std::vector<VoltageLimitsIntent> frame(VoltageLimitsViewState& state,const VoltageLimitsData* data,bool unlocked,bool busy=false,const char* activate=nullptr,int row=-1){
 ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1500});ImGui::Begin("limits-test");ImGui::PushID("voltage-limits");if(row>=0)ImGui::PushID(row);if(activate){auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;}if(row>=0)ImGui::PopID();ImGui::PopID();auto out=draw_voltage_limits_controls(state,data,1,busy,unlocked);ImGui::End();ImGui::Render();return out;
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1600,1700};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 VoltageLimitsViewState state;VoltageLimitsData data;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.uuid,"GPU-other");std::strcpy(data.identity.slot,"0000:07:00.0");
 VoltageLimitRow device;device.key={VoltageLimitKind::switch_delay,0,4};device.label="PWM device 4";device.binding="current-device-proof";device.value=10;device.initial_value=10;device.writable=true;device.availability=VoltagePolicyAvailability::available;device.voltage_domain=2;device.operation_type=3;data.rows={device};
 CHECK(frame(state,&data,false).empty());auto out=frame(state,&data,false,false,"Read voltage definitions");CHECK(out.size()==1&&out[0].families==1);out=frame(state,&data,false,false,"Read voltage device delays");CHECK(out.size()==1&&out[0].families==2);
 CHECK(frame(state,&data,true,true,"Read voltage device delays").empty());CHECK(frame(state,&data,false,false,"Apply selected",0).empty());
 state.drafts[0].dirty=true;state.drafts[0].value=25;data.rows[0].value=12;out=frame(state,&data,true,false,"Apply selected",0);CHECK(out.size()==1&&out[0].requests[0].value==25&&out[0].requests[0].expected_value==10);CHECK(state.drafts[0].value==25);
 out=frame(state,&data,true,false,"Use current",0);CHECK(out.empty()&&!state.drafts[0].dirty&&state.drafts[0].value==12);
 out=frame(state,&data,false,false,"Save selected voltage profile...");CHECK(out.size()==1&&out[0].selection.size()==1);out=frame(state,&data,false,false,"Load voltage profile...");CHECK(out.size()==1&&out[0].operation==VoltageLimitsIntent::Operation::load_profile);
 auto doc=capture_voltage_limits_profile(data,{device.key});doc["entries"][0]["value"]=35;CHECK(stage_voltage_limits_document(state,doc,data)&&state.staged.size()==1);CHECK(frame(state,&data,false,false,"Apply staged voltage profile").empty());out=frame(state,&data,true,false,"Apply staged voltage profile");CHECK(out.size()==1&&out[0].requests[0].value==35&&out[0].requests[0].expected_value==12);
 data.recovery_pending=true;data.undo_available=true;CHECK(frame(state,&data,true,false,"Apply selected",0).empty());CHECK(frame(state,&data,true,false,"Undo voltage limits").empty());out=frame(state,&data,true,false,"Recover selected voltage limit");CHECK(out.size()==1&&out[0].operation==VoltageLimitsIntent::Operation::recover);
 data.recovery_pending=false;out=frame(state,&data,true,false,"Restore captured initial",0);CHECK(out.size()==1&&out[0].selection[0]==device.key);
 data.rows[0].stale=true;CHECK(!stage_voltage_limits_document(state,doc,data)&&state.staged.empty());sync_voltage_limits_view(state,&data,2);CHECK(state.staged.empty()&&state.generation==2);
 {// Classic's released 1.9.1 subsections: REL bounds, constants and switch delays draw and save apart.
  VoltageLimitsData parts=data;parts.recovery_pending=false;
  VoltageLimitRow rel;rel.key={VoltageLimitKind::rel,0,1};rel.label="NVVDD REL bound";rel.binding="rel-proof";rel.value=900000;rel.lower=800000;rel.writable=true;rel.availability=VoltagePolicyAvailability::available;
  VoltageLimitRow ov=rel;ov.key={VoltageLimitKind::ov,0,2};ov.label="NVVDD OV constant";ov.lower.reset();
  VoltageLimitRow delay=device;delay.stale=false;parts.rows={rel,ov,delay};
  VoltageLimitsViewState part;std::string text;
  const auto frame_part=[&](VoltageLimitsPart which,const char* activate,bool busy=false,bool unlocked=true){ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1500});ImGui::Begin("limits-part");
   if(activate){ImGui::PushID("voltage-limits");ImGui::PushID(static_cast<int>(which));auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;ImGui::PopID();ImGui::PopID();}
   ImGui::LogToBuffer();auto result=draw_voltage_limits_controls(part,&parts,1,busy,unlocked,true,which);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  const auto has=[&](const char* s){return text.find(s)!=std::string::npos;};
  CHECK(voltage_limits_mv_text(900000)=="900"&&voltage_limits_mv_text(912345)=="912.345"&&voltage_limits_mv_text(-12500)=="-12.5"&&voltage_limits_mv_text(0)=="0");
  // registry_panel.py: Read, Save ... profile, a selector and one mV input, Apply / Restore, framed inventory.
  CHECK(frame_part(VoltageLimitsPart::rel,nullptr).empty());
  // Selector and inventory names follow _show_rel_live: "<rail> (rail N)"; REL lists reliability, alt_reliability and effective.
  CHECK(has("NVVDD (rail 0): loaded bounds 800 to 900 mV; stored bounds unavailable to unavailable mV; reliability: unavailable, alt_reliability: unavailable, effective: unavailable")&&!has("OV constant"));
  CHECK(has("NVVDD: loaded bounds 800 to 900 mV.")&&!has("Read live bounds before changing them."));  // select_rel_live's status
  CHECK(!has("Loaded voltage bounds, constants & switch delays"));  // no inner title under the subsection bar
  CHECK(has("NVVDD (rail 0)")&&!has("NVVDD OV constant")&&!has("PWM device 4")&&!has("Read device delays")&&has("Save bounds profile")&&has("Bound value (mV)")
      &&has("Apply upper")&&has("Restore initial upper")&&has("Fix both bounds")&&has("Restore BIOS bounds")&&has("loaded bounds 800 to 900 mV")&&!has("uV")&&!has("Include in saved profile"));
  CHECK(!std::strcmp(part.drafts[0].value_text.data(),"900")&&!std::strcmp(part.drafts[0].lower_text.data(),"800"));
  frame_part(VoltageLimitsPart::constants,nullptr);
  CHECK(has("NVVDD (rail 0) OV constant: current 900 mV; stored unavailable mV; reported OV unavailable"));
  CHECK(has("NVVDD (rail 0) OV constant")&&!has("NVVDD REL bound")&&has("Read")&&has("Save constants profile")&&has("Value (mV)")&&has("Apply now")&&has("Restore initial value")&&has("current 900 mV"));
  frame_part(VoltageLimitsPart::switch_delay,nullptr);
  // _show_voltage_devices: "<index>: <DOMAIN> / <OPERATION>", and the full 1.9.1 explainer.
  CHECK(has("Device 4: SRAM / LPWR_SLEEP_STATE: 10 us request")&&has("DEFAULT and IPC_VMIN are separate operations.")&&has("the separate power-up/down delays are not controlled here."));
  CHECK(has("4: SRAM / LPWR_SLEEP_STATE")&&!has("PWM device 4")&&!has("OV constant")&&has("Read device delays")&&has("Save delays profile")&&has("Delay request (us)")&&has("Apply delay now")&&has("Restore initial delay")&&has("10 us request"));
  {// registry_panel.py inventories: the rail's reported policy, VMIN's maximum input, and a row that is
   // not ready as "<label>: <status>; <reason>", with any kept last-good reading after it.
   using A=VoltagePolicyAvailability;auto inv=parts;
   inv.rows[0].rail_policy={1000000,1050000,1100000,700000,990000};inv.rows[0].stored_lower=800000;inv.rows[0].stored_value=950000;
   const std::string rel_body="loaded bounds 800 to 900 mV; stored bounds 800 to 950 mV; reliability: 1000 mV, alt_reliability: 1050 mV, effective: 990 mV";
   CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::rel)=="NVVDD (rail 0): "+rel_body);
   VoltageLimitRow vmin=inv.rows[1];vmin.key={VoltageLimitKind::vmin,1,3};vmin.label="MSVDD VMIN constant";vmin.value=vmin.stored_value=650000;vmin.reported=700000;vmin.vmin_maximum=925000;inv.rows.push_back(vmin);
   CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::constants).find("\nMSVDD (rail 1) VMIN constant: current 650 mV; stored 650 mV; reported VMIN 700 mV; current maximum VMIN input 925 mV")!=std::string::npos);
   inv.rows[1].availability=A::unsupported;inv.rows[1].value.reset();inv.rows[1].message="Equation role needs a complete, independent ROM graph binding";
   CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::constants).find("NVVDD (rail 0) OV constant: unsupported; Equation role needs a complete, independent ROM graph binding\n")==0);
   inv.rows[0].stale=true;inv.rows[0].availability=A::transient;inv.rows[0].message="Bounded voltage read failed";
   CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::rel)=="NVVDD (rail 0): transient; Bounded voltage read failed\n  Last successful read: "+rel_body);
   inv.rows[2].availability=A::unsupported;inv.rows[2].value.reset();inv.rows[2].device_type=2;inv.rows[2].message="Only PWM type3 switch-delay semantics are qualified";
   CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::switch_delay)=="Device 4, type 2: unsupported; Only PWM type3 switch-delay semantics are qualified");
   // Python's per-row words: a malformed device record, and "unsupported" for any constant row that is not ready.
   inv.rows[2].availability=A::invalid;inv.rows[2].device_type=3;inv.rows[2].message="Device voltage range or loaded positive delay is malformed";
   CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::switch_delay)=="Device 4, type 3: malformed; Device voltage range or loaded positive delay is malformed");
   inv.rows[1].availability=A::invalid;CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::constants).find("NVVDD (rail 0) OV constant: unsupported; ")==0);
   // A failed read keeps the last good reading and names the failure kind (native; Python clears the box).
   inv.rows[1].stale=true;inv.rows[1].message="Equation capture changed between paired reads";
   CHECK(voltage_limits_inventory(&inv,VoltageLimitsPart::constants).find("NVVDD (rail 0) OV constant: invalid; Equation capture changed between paired reads")==0);
   // Status lines (select_rel_live / select_constants_live / select_voltage_device), and none ready.
   CHECK(voltage_limits_status(&parts,VoltageLimitsPart::rel,&parts.rows[0])=="NVVDD: loaded bounds 800 to 900 mV.");
   CHECK(voltage_limits_status(&parts,VoltageLimitsPart::constants,&parts.rows[1])=="NVVDD OV constant: 900 mV.");
   CHECK(voltage_limits_status(&parts,VoltageLimitsPart::switch_delay,&parts.rows[2])=="Device 4: 10 us reported request.");
   CHECK(voltage_limits_status(&inv,VoltageLimitsPart::switch_delay,&inv.rows[2])=="No supported switch-delay requests are available. Read again to retry.");
   CHECK(voltage_limits_status(nullptr,VoltageLimitsPart::rel,nullptr)=="Read live bounds before changing them."&&voltage_limits_inventory(nullptr,VoltageLimitsPart::rel).empty());
   // refresh_rel_live / refresh_constants_live / refresh_voltage_devices: a failed whole Read. A
   // transient failure that kept the rows adds the stale sentence; a row picked afterwards is
   // prefixed "Previous read is stale. " until a good read.
   auto failed=parts;failed.equations_read_error="Bounded voltage read failed";failed.equations_read_retained=true;
   failed.rows[0].stale=true;failed.rows[0].availability=A::transient;
   CHECK(voltage_limits_status(&failed,VoltageLimitsPart::rel,&failed.rows[0])=="Live bound read unavailable: Bounded voltage read failed Previous read is stale; refresh to verify.");
   CHECK(voltage_limits_status(&failed,VoltageLimitsPart::constants,&failed.rows[1])=="Live value read unavailable: Bounded voltage read failed Previous read is stale; refresh to verify.");
   CHECK(voltage_limits_status(&failed,VoltageLimitsPart::rel,&failed.rows[0],true)=="Previous read is stale. NVVDD: loaded bounds 800 to 900 mV.");
   CHECK(voltage_limits_status(&failed,VoltageLimitsPart::constants,&failed.rows[1],true)=="Previous read is stale. NVVDD OV constant: 900 mV.");
   failed.equations_read_retained=false;failed.equations_read_error="Equation role needs a complete, independent ROM graph binding";
   CHECK(voltage_limits_status(&failed,VoltageLimitsPart::rel,&failed.rows[0],true)=="Live bound read unavailable: Equation role needs a complete, independent ROM graph binding");
   failed.devices_read_error="Voltage device GET rejected";
   CHECK(voltage_limits_status(&failed,VoltageLimitsPart::switch_delay,&failed.rows[2],true)=="Device delay read unavailable: Voltage device GET rejected");
   CHECK(voltage_limits_status(&failed,VoltageLimitsPart::rel,&failed.rows[0]).find("Live bound read unavailable: Equation role")==0); // the families fail apart
  }
  out=frame_part(VoltageLimitsPart::switch_delay,"Read device delays");CHECK(out.size()==1&&out[0].families==2);
  out=frame_part(VoltageLimitsPart::rel,"Read");CHECK(out.size()==1&&out[0].families==1);
  out=frame_part(VoltageLimitsPart::constants,"Save constants profile");CHECK(out.size()==1&&out[0].selection.size()==1&&out[0].selection[0]==ov.key);
  // mV entry: the request still carries whole microvolts; a sub-microvolt entry blocks Apply.
  auto& rel_draft=part.drafts[0];std::strcpy(rel_draft.value_text.data(),"905.5");rel_draft.dirty=true;rel_draft.value=905500;
  out=frame_part(VoltageLimitsPart::rel,"Apply upper");CHECK(out.size()==1&&out[0].requests[0].key==rel.key&&out[0].requests[0].value==905500&&!out[0].requests[0].lower&&out[0].requests[0].expected_value==900000&&out[0].requests[0].binding=="rel-proof");
  out=frame_part(VoltageLimitsPart::rel,"Fix both bounds");CHECK(out.size()==1&&out[0].requests[0].value==905500&&out[0].requests[0].lower==905500);
  rel_draft.value_invalid=true;CHECK(frame_part(VoltageLimitsPart::rel,"Apply upper").empty());rel_draft.value_invalid=false;
  {// Every Classic Apply and Restore is inert while locked, busy, recovering or on a row that is not writable.
   parts.rows[0].initial_value=880000;parts.rows[1].initial_value=890000;
   struct Case{VoltageLimitsPart part;const char* label;size_t row;};
   const Case cases[]{{VoltageLimitsPart::rel,"Apply upper",0},{VoltageLimitsPart::rel,"Fix both bounds",0},{VoltageLimitsPart::rel,"Apply both bounds",0},
    {VoltageLimitsPart::rel,"Restore initial upper",0},{VoltageLimitsPart::constants,"Apply now",1},{VoltageLimitsPart::constants,"Restore initial value",1},
    {VoltageLimitsPart::switch_delay,"Apply delay now",2},{VoltageLimitsPart::switch_delay,"Restore initial delay",2}};
   for(const auto& c:cases){
    CHECK(frame_part(c.part,c.label).size()==1);  // enabled, so each refusal below is the gate's
    CHECK(frame_part(c.part,c.label,false,false).empty()&&frame_part(c.part,c.label,true,true).empty());
    parts.recovery_pending=true;CHECK(frame_part(c.part,c.label).empty());parts.recovery_pending=false;
    parts.rows[c.row].writable=false;CHECK(frame_part(c.part,c.label).empty());parts.rows[c.row].writable=true;
   }
   parts.rows[0].initial_value.reset();parts.rows[1].initial_value.reset();
  }
  out=frame_part(VoltageLimitsPart::rel,"Restore BIOS bounds");CHECK(out.empty()); // no stored ROM value read
  out=frame_part(VoltageLimitsPart::rel,"Use current");CHECK(out.empty()&&!rel_draft.dirty);frame_part(VoltageLimitsPart::rel,nullptr);CHECK(rel_draft.value==900000&&!std::strcmp(rel_draft.value_text.data(),"900"));
  // A part applies only its own staged rows; Undo, Recover and Load span every part and stay off its heading.
  VoltageLimitRequest staged_rel;staged_rel.key=rel.key;staged_rel.value=910000;VoltageLimitRequest staged_delay;staged_delay.key=delay.key;staged_delay.value=20;part.staged={staged_rel,staged_delay};
  parts.undo_available=true;frame_part(VoltageLimitsPart::rel,nullptr);
  CHECK(text.find("Undo voltage limits")==std::string::npos&&text.find("Recover selected voltage limit")==std::string::npos&&text.find("Load voltage profile...")==std::string::npos&&text.find(": 20 us")==std::string::npos);
  out=frame_part(VoltageLimitsPart::rel,"Apply staged requests");CHECK(out.size()==1&&out[0].requests.size()==1&&out[0].requests[0].key==rel.key);
  frame_part(VoltageLimitsPart::constants,nullptr);CHECK(text.find("Apply staged")==std::string::npos);
  parts.recovery_pending=true;frame_part(VoltageLimitsPart::switch_delay,nullptr);CHECK(text.find("Recover it under Undo, recovery and profiles")!=std::string::npos);
  const auto frame_shared=[&](const char* activate){ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1500});ImGui::Begin("limits-shared");
   if(activate){ImGui::PushID("voltage-limits");ImGui::PushID(200);auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;ImGui::PopID();ImGui::PopID();}
   ImGui::LogToBuffer();auto result=draw_voltage_limits_shared_controls(part,&parts,1,false,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  out=frame_shared("Recover selected voltage limit");CHECK(out.size()==1&&out[0].operation==VoltageLimitsIntent::Operation::recover);
  // Rows appear only as names in the folded saved selection (logging expands tree nodes).
  CHECK(text.find("Load voltage profile...")!=std::string::npos&&text.find("Apply staged")==std::string::npos&&text.find("Bound value (mV)")==std::string::npos
      &&text.find("Save bounds profile")==std::string::npos&&text.find("Save a selection of requests")<text.find("NVVDD REL bound"));
  // A subset, or rows of several subsections, is still saved from the shared subsection's folded selection.
  const auto frame_tree=[&](const char* activate){ImGui::NewFrame();ImGui::SetNextWindowSize({1400,1500});ImGui::Begin("limits-shared");
   ImGui::PushID("voltage-limits");ImGui::PushID(200);const auto tree=ImGui::GetID("Save a selection of requests");ImGui::GetStateStorage()->SetInt(tree,1);
   if(activate){ImGui::PushOverrideID(tree);auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;ImGui::PopID();}
   ImGui::PopID();ImGui::PopID();
   ImGui::LogToBuffer();auto result=draw_voltage_limits_shared_controls(part,&parts,1,false,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  for(auto& d:part.drafts)d.include=false;
  CHECK(frame_tree("Save selected voltage profile...").empty()&&has("NVVDD REL bound")&&has("PWM device 4"));
  for(auto& d:part.drafts)d.include=d.key==rel.key||d.key==delay.key;out=frame_tree("Save selected voltage profile...");
  CHECK(out.size()==1&&out[0].operation==VoltageLimitsIntent::Operation::save_profile&&out[0].selection.size()==2&&out[0].selection[0]==rel.key&&out[0].selection[1]==delay.key);
  {// registry_panel.py's single status line: no service text under it, and a request's published
   // result takes the line as _set_*_live words it ("Applied live. " or "... change failed: ").
   parts.recovery_pending=false;part.staged.clear();part.last={};part.status.clear();
   parts.message="Voltage inspection refreshed; evaluate each family's independent status";parts.equations_message="Read 3 loaded equations with current ROM role evidence";
   parts.rows[1].message="Stored OV constant, not measured voltage";
   frame_part(VoltageLimitsPart::constants,nullptr);
   CHECK(has("NVVDD OV constant: 900 mV.")&&!has("Voltage inspection refreshed")&&!has("Read 3 loaded equations")&&!has("Stored OV constant"));
   parts.rows[1].message.clear();
   {// A Read that returned no row of the part: _show_constants_live finds no choice.
    auto none=parts;none.rows={parts.rows[0]};
    CHECK(voltage_limits_status(&none,VoltageLimitsPart::constants,nullptr)=="No supported live values are available.");
    none.equations_message.clear();CHECK(voltage_limits_status(&none,VoltageLimitsPart::constants,nullptr)=="Read live values before changing them.");
    none.rows.clear();none.devices_message="Read 0 voltage devices; only qualified PWM delays are writable";
    CHECK(voltage_limits_status(&none,VoltageLimitsPart::switch_delay,nullptr)=="No supported switch-delay requests are available. Read again to retry.");}
   const auto count=[&](const std::string& s){size_t n=0;for(auto at=text.find(s);at!=std::string::npos;at=text.find(s,at+1))++n;return n;};
   auto& ov_draft=*std::find_if(part.drafts.begin(),part.drafts.end(),[&](const auto& x){return x.key==ov.key;});
   std::strcpy(ov_draft.value_text.data(),"905");ov_draft.value=905000;ov_draft.dirty=true;
   out=frame_part(VoltageLimitsPart::constants,"Apply now");CHECK(out.size()==1&&out[0].requests[0].value==905000);
   part.status="Operation queued; see the operation log.";parts.message="Selected voltage requests read back exactly; physical voltage/latency effects are not measured";
   frame_part(VoltageLimitsPart::constants,nullptr);CHECK(has("NVVDD OV constant: 900 mV.")&&has("Operation queued"));  // not yet published
   parts.rows[1].value=905000;++parts.revision;frame_part(VoltageLimitsPart::constants,nullptr);
   CHECK(has("Applied live. NVVDD OV constant: 905 mV.")&&!has("Operation queued")&&!has("Selected voltage requests read back exactly"));
   frame_part(VoltageLimitsPart::rel,nullptr);CHECK(!has("Applied live.")&&has("NVVDD: loaded bounds 800 to 900 mV."));  // the result stays with its part
   // A request whose control does not read back the requested value failed: its reason, once.
   std::strcpy(ov_draft.value_text.data(),"910");ov_draft.value=910000;ov_draft.dirty=true;
   CHECK(frame_part(VoltageLimitsPart::constants,"Apply now").size()==1);
   parts.message="Voltage constant SET rejected (RM 31)";++parts.revision;frame_part(VoltageLimitsPart::constants,nullptr);
   CHECK(has("Live value change failed: Voltage constant SET rejected (RM 31)")&&count("RM 31")==1&&!has("Applied live."));
   // Restore initial value is _set_constants_live with the captured value.
   parts.rows[1].initial_value=890000;CHECK(frame_part(VoltageLimitsPart::constants,"Restore initial value").size()==1);
   parts.rows[1].value=890000;parts.message="Selected voltage requests read back exactly; physical voltage/latency effects are not measured";++parts.revision;
   frame_part(VoltageLimitsPart::constants,nullptr);CHECK(has("Applied live. NVVDD OV constant: 890 mV."));
   // Fix both bounds sets both; the REL line names the rail and both bounds.
   std::strcpy(rel_draft.value_text.data(),"905.5");rel_draft.value=905500;rel_draft.dirty=true;
   out=frame_part(VoltageLimitsPart::rel,"Fix both bounds");CHECK(out.size()==1&&out[0].requests[0].value==905500&&out[0].requests[0].lower==905500);
   parts.rows[0].value=parts.rows[0].lower=905500;++parts.revision;frame_part(VoltageLimitsPart::rel,nullptr);
   CHECK(has("Applied live. NVVDD: loaded bounds 905.5 to 905.5 mV."));
   // A delay request that leaves its device unready failed, in the device panel's words.
   CHECK(frame_part(VoltageLimitsPart::switch_delay,"Apply delay now").size()==1);
   parts.rows[2].stale=true;parts.rows[2].availability=VoltagePolicyAvailability::transient;parts.message="Selected write unconfirmed; recovery pending: readback differs";++parts.revision;
   frame_part(VoltageLimitsPart::switch_delay,nullptr);CHECK(has("Device delay change failed: Selected write unconfirmed; recovery pending: readback differs"));
   parts.rows[2].stale=false;parts.rows[2].availability=VoltagePolicyAvailability::available;
   // Undo from the shared subsection returns every part's line to its readback once published.
   parts.undo_available=true;CHECK(frame_shared("Undo voltage limits").size()==1);
   part.status="Operation queued; see the operation log.";parts.message="Original selected voltage requests verified";++parts.revision;
   frame_part(VoltageLimitsPart::constants,nullptr);CHECK(has("NVVDD OV constant: 890 mV.")&&!has("Applied live.")&&!has("Operation queued")&&!has("Original selected voltage requests verified"));
   frame_part(VoltageLimitsPart::switch_delay,nullptr);CHECK(has("Device 4: 10 us reported request.")&&!has("change failed"));
   // A Read brings back the read line; an entry that does not parse is the line until corrected.
   CHECK(frame_part(VoltageLimitsPart::rel,"Read").size()==1);++parts.revision;frame_part(VoltageLimitsPart::rel,nullptr);CHECK(has("NVVDD: loaded bounds 905.5 to 905.5 mV.")&&!has("Applied live."));
   ov_draft.value_invalid=true;frame_part(VoltageLimitsPart::constants,nullptr);
   CHECK(has("Enter a finite mV value with whole-microvolt precision.")&&!has("NVVDD OV constant: 890 mV."));ov_draft.value_invalid=false;
  }
 }
 ImGui::DestroyContext();std::printf("Voltage limits view: %u checks\n",checks);return 0;
}
