// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_policy_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static VoltagePolicyViewOutput frame(VoltagePolicyViewState& state,const VoltagePolicyData* data,bool unlocked,bool busy=false,const char* activate=nullptr,int row=-1,bool connected=true){
 ImGui::NewFrame();ImGui::SetNextWindowSize({1300,1300});ImGui::Begin("test");ImGui::PushID("voltage-policy");if(row>=0)ImGui::PushID(row);
 if(activate){auto& ctx=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;}
 if(row>=0)ImGui::PopID();ImGui::PopID();auto out=draw_voltage_policy_controls(state,data,1,busy,unlocked,false,connected);ImGui::End();ImGui::Render();return out;
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1400,1400};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 VoltagePolicyData data;for(auto& r:data.rows){r.current=true;r.initial=false;r.writable=true;r.availability=VoltagePolicyAvailability::available;}
 VoltagePolicyViewState state;CHECK(frame(state,&data,false).intents.empty()&&state.drafts[0]);
 auto out=frame(state,&data,false,false,"Read voltage policies");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::refresh);
 CHECK(frame(state,&data,false,false,"Apply",0).intents.empty());CHECK(frame(state,&data,true,true,"Read voltage policies").intents.empty());
 state.drafts[1]=false;state.dirty[1]=true;out=frame(state,&data,true,false,"Apply",1);CHECK(out.intents.size()==1&&out.intents[0].request.selection==2&&out.intents[0].request.values==0&&out.intents[0].request.expected==2);
 state.drafts[1]=false;state.dirty[1]=true;sync_voltage_policy_view(state,&data,1);data.rows[1].current=false;
 out=frame(state,&data,true,false,"Apply",1);CHECK(out.intents.size()==1&&out.intents[0].request.expected==2); // retain original edit baseline across refresh
 out=frame(state,&data,true,false,"Restore initial",2);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial);
 data.rows[0].writable=false;CHECK(frame(state,&data,true,false,"Apply",0).intents.empty());
 data.recovery_pending=true;data.undo_available=true;CHECK(frame(state,&data,true,false,"Apply",1).intents.empty());CHECK(frame(state,&data,true,false,"Undo voltage policy").intents.empty());
 out=frame(state,&data,true,false,"Recover selected policy");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::recover);
 CHECK(frame(state,&data,true,false,"Read voltage policies",-1,false).intents.empty());
 CHECK(frame(state,&data,true,false,"Apply",1,false).intents.empty());
 CHECK(frame(state,&data,true,false,"Load policy profile...",-1,false).intents.empty());
 out=frame(state,&data,true,false,"Recover selected policy",-1,false);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::recover);
 CHECK(frame(state,&data,true,true,"Recover selected policy",-1,false).intents.empty());
 CHECK(frame(state,&data,false,false,"Recover selected policy",-1,false).intents.empty());
 data.recovery_prior_unknown=true;data.recovery_selection=1;out=frame(state,&data,true,false,"Request captured initial",0);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial);
 out=frame(state,&data,true,false,"Request captured initial",0,false);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial);
 data.recovery_pending=false;out=frame(state,&data,true,false,"Undo voltage policy",-1,false);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::undo);
 state.dirty[2]=true;state.drafts[2]=false;sync_voltage_policy_view(state,&data,1);CHECK(!state.drafts[2]);sync_voltage_policy_view(state,nullptr,2);CHECK(!state.dirty[2]&&!state.drafts[2]);

 data.recovery_pending=false;data.recovery_prior_unknown=false;data.recovery_selection=0;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.slot,"0000:ab:03.0");std::strcpy(data.identity.uuid,"GPU-policy-profile-view");data.identity.nvapi_ok=data.identity.nvapi_selected=1;data.identity.nvapi_handle=99;data.identity.nvapi_devid=0x2345;data.identity.nvapi_subsys=0x12345678;
 dc_target target{};target.size=sizeof(target);std::strcpy(target.slot,data.identity.slot);target.architecture=7;target.device_id=data.identity.nvapi_devid;target.subsystem_id=data.identity.nvapi_subsys;target.handle=data.identity.nvapi_handle;data.target=target;
 for(auto& r:data.rows){r.availability=VoltagePolicyAvailability::available;r.writable=true;}data.rows[0].current=false;data.rows[1].current=true;data.rows[2].current=true;
 frame(state,&data,false);state.included={true,false,false};out=frame(state,&data,false,false,"Save selected policy profile...");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::save_profile&&out.intents[0].request.selection==1);
 state.included[1]=true;CHECK(frame(state,&data,true,false,"Save selected policy profile...").intents.empty());state.included={false,true,true};out=frame(state,&data,false,false,"Save selected policy profile...");CHECK(out.intents.size()==1&&out.intents[0].request.selection==6);CHECK(frame(state,&data,false,true,"Save selected policy profile...").intents.empty());
 out=frame(state,&data,false,false,"Load policy profile...");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::load_profile);
 auto document=capture_voltage_policy_profile(data,6);document["values"]=0;CHECK(stage_voltage_policy_document(state,document,data)&&state.staged&&state.staged->selection==6&&state.staged->expected==6);CHECK(frame(state,&data,false,false,"Apply staged policy profile").intents.empty());out=frame(state,&data,true,false,"Apply staged policy profile");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::apply&&out.intents[0].request.selection==6&&out.intents[0].request.values==0&&out.intents[0].request.expected==6);
 data.rows[1].current=false;sync_voltage_policy_view(state,&data,1);CHECK(state.staged->expected==6);auto bad=document;bad["values"]=9;CHECK(!stage_voltage_policy_document(state,bad,data)&&state.staged->expected==6&&!state.status.empty());
 data.rows[0].writable=false;CHECK(stage_voltage_policy_document(state,document,data));out=frame(state,&data,true,false,"Apply staged policy profile");CHECK(out.intents.size()==1&&out.intents[0].request.selection==6); // admission Off does not block VF flags
 sync_voltage_policy_view(state,&data,2);CHECK(!state.staged&&state.status.empty()&&!state.included[1]);
 {// Classic's two released 1.9.1 subsections: each draws and saves only its own flags.
  VoltagePolicyViewState part;std::string text;for(auto& r:data.rows)r.writable=true;
  const auto frame_part=[&](VoltagePolicyPart which,const char* activate,int row,bool busy=false,bool unlocked=true){ImGui::NewFrame();ImGui::SetNextWindowSize({1300,1300});ImGui::Begin("part");
   if(activate){ImGui::PushID("voltage-policy");ImGui::PushID(static_cast<int>(which));if(row>=0)ImGui::PushID(row);auto& ctx=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;if(row>=0)ImGui::PopID();ImGui::PopID();ImGui::PopID();}
   ImGui::LogToBuffer();auto result=draw_voltage_policy_controls(part,&data,2,busy,unlocked,true,true,which);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  // registry_panel.py: explainer, then Read / Save / checkbox / Apply / Restore with Python's labels.
  const auto has=[&](const char* s){return text.find(s)!=std::string::npos;};
  CHECK(frame_part(VoltagePolicyPart::admission,nullptr,-1).intents.empty());
  CHECK(has("Allow or suppress the driver's voltage-request processing")&&has("Read voltage-request gate")&&has("Save gate profile")&&has("Allow voltage requests")
      &&has("Apply gate now")&&has("Restore initial gate")&&has("Driver voltage requests: suppressed.")&&!has("Enforce monotonicity")&&!has("Voltage requests & VF policies")&&!has("Include in policy profile"));
  CHECK(text.find("Allow or suppress")<text.find("Read voltage-request gate"));
  auto read=frame_part(VoltagePolicyPart::admission,"Read voltage-request gate",-1);
  CHECK(read.intents.size()==1&&read.intents[0].operation==VoltagePolicyIntent::Operation::refresh&&read.intents[0].request.selection==voltage_all_policies);
  frame_part(VoltagePolicyPart::vf_policies,nullptr,-1);
  CHECK(!has("Allow voltage requests")&&has("Read VF policies")&&has("Save VF policies profile")&&has("Enforce monotonicity")&&has("Enforce smoothing")
      &&has("Apply policies now")&&has("Restore initial policies")&&has("Monotonicity: off, smoothing: on. Startup mode and registry settings are retained.")
      &&has("Some startup modes reject disabling a check; use the registry setting first in that case.")&&has("Live changes retain the startup processing mode")
      &&!has("Both checks differ"));
  // Restore initial gate is offered only while the gate differs from its captured initial (registry_panel.py).
  CHECK(!*data.rows[0].current&&!*data.rows[0].initial&&frame_part(VoltagePolicyPart::admission,"Restore initial gate",0).intents.empty());
  data.rows[0].initial=true;auto gate_restore=frame_part(VoltagePolicyPart::admission,"Restore initial gate",0);
  CHECK(gate_restore.intents.size()==1&&gate_restore.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial&&gate_restore.intents[0].request.selection==1);
  data.rows[0].initial=false;
  // With both VF checks away from their initial values, each Restore still sends one flag, and the note says so.
  data.rows[1].initial=true;auto first_restore=frame_part(VoltagePolicyPart::vf_policies,"Restore initial policies",1);
  CHECK(has("Both checks differ from their initial values")&&first_restore.intents.size()==1&&first_restore.intents[0].request.selection==2);
  data.rows[1].initial=false;
  // Apply policies now still sends one flag per request, as the per-flag Apply did.
  part.dirty[1]=true;part.drafts[1]=!*data.rows[1].current;auto applied=frame_part(VoltagePolicyPart::vf_policies,"Apply policies now",1);
  CHECK(applied.intents.size()==1&&applied.intents[0].request.selection==2&&applied.intents[0].request.values==2&&applied.intents[0].request.expected==0);
  sync_voltage_policy_view(part,&data,2);CHECK(!part.dirty[1]);
  part.dirty[1]=part.dirty[2]=true;part.drafts[1]=true;part.drafts[2]=false;
  frame_part(VoltagePolicyPart::vf_policies,nullptr,-1);CHECK(has("Both checks changed"));
  applied=frame_part(VoltagePolicyPart::vf_policies,"Apply policies now",1);
  CHECK(applied.intents.size()==1&&applied.intents[0].request.selection==2);
  applied=frame_part(VoltagePolicyPart::vf_policies,"Apply policies now",2);
  CHECK(applied.intents.size()==1&&applied.intents[0].request.selection==4&&applied.intents[0].request.values==0&&applied.intents[0].request.expected==4);
  sync_voltage_policy_view(part,&data,2);
  CHECK(frame_part(VoltagePolicyPart::vf_policies,"Apply policies now",1).intents.empty()&&frame_part(VoltagePolicyPart::vf_policies,"Apply policies now",2).intents.empty());
  // Restore initial policies restores the check whose reading differs from its captured initial.
  auto restored=frame_part(VoltagePolicyPart::vf_policies,"Restore initial policies",2);
  CHECK(restored.intents.size()==1&&restored.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial&&restored.intents[0].request.selection==4);
  {// Apply and Restore are inert while locked, busy, recovering (with no recovery naming the flag)
   // or on a flag that is not writable; each is enabled otherwise, so every refusal is the gate's.
   data.rows[0].initial=!*data.rows[0].current;data.rows[1].initial=!*data.rows[1].current;
   struct Case{VoltagePolicyPart part;const char* label;int row;size_t flag;};
   const Case cases[]{{VoltagePolicyPart::admission,"Apply gate now",0,0},{VoltagePolicyPart::admission,"Restore initial gate",0,0},
    {VoltagePolicyPart::vf_policies,"Apply policies now",1,1},{VoltagePolicyPart::vf_policies,"Restore initial policies",1,1}};
   const auto arm=[&]{part.dirty[1]=true;part.drafts[1]=!*data.rows[1].current;};
   for(const auto& c:cases){
    arm();CHECK(frame_part(c.part,c.label,c.row).intents.size()==1);
    arm();CHECK(frame_part(c.part,c.label,c.row,false,false).intents.empty()&&frame_part(c.part,c.label,c.row,true,true).intents.empty());
    data.recovery_pending=true;CHECK(frame_part(c.part,c.label,c.row).intents.empty());data.recovery_pending=false;
    data.rows[c.flag].writable=false;CHECK(frame_part(c.part,c.label,c.row).intents.empty());data.rows[c.flag].writable=true;
   }
   data.rows[0].initial=data.rows[1].initial=false;part.dirty[1]=false;part.drafts[1]=*data.rows[1].current;
  }
  // A subsection's Save writes its own flags without include checkboxes.
  part.included={};auto saved=frame_part(VoltagePolicyPart::admission,"Save gate profile",-1);
  CHECK(saved.intents.size()==1&&saved.intents[0].operation==VoltagePolicyIntent::Operation::save_profile&&saved.intents[0].request.selection==1);
  saved=frame_part(VoltagePolicyPart::vf_policies,"Save VF policies profile",-1);
  CHECK(saved.intents.size()==1&&saved.intents[0].request.selection==6);
  data.rows[2].current.reset();CHECK(frame_part(VoltagePolicyPart::vf_policies,"Save VF policies profile",-1).intents.empty());data.rows[2].current=true;
  // Undo, Recover, Load and Discard act on both parts, so neither part's heading carries them.
  for(auto which:{VoltagePolicyPart::admission,VoltagePolicyPart::vf_policies}){frame_part(which,nullptr,-1);
   CHECK(text.find("Undo voltage policy")==std::string::npos&&text.find("Recover selected policy")==std::string::npos&&text.find("Load policy profile...")==std::string::npos);}
  data.recovery_pending=true;frame_part(VoltagePolicyPart::admission,nullptr,-1);CHECK(text.find("Recover it under Undo, recovery and profiles")!=std::string::npos);data.recovery_pending=false;
  // A staged VF profile is offered and applied only under the VF part.
  CHECK(stage_voltage_policy_document(part,document,data)&&part.staged->selection==6);
  frame_part(VoltagePolicyPart::admission,nullptr,-1);CHECK(text.find("Apply staged policy profile")==std::string::npos);
  auto staged=frame_part(VoltagePolicyPart::vf_policies,"Apply staged policy profile",-1);
  CHECK(staged.intents.size()==1&&staged.intents[0].operation==VoltagePolicyIntent::Operation::apply&&staged.intents[0].request.selection==6);
  // registry_panel.py refresh_voltage_gate / refresh_clock_vf_live: a failed Read is the status line, said once.
  const auto count=[&](const std::string& s){size_t n=0;for(auto at=text.find(s);at!=std::string::npos;at=text.find(s,at+1))++n;return n;};
  data.gate_read_error=data.rows[0].message="Voltage policy rejected in current mode (RM 31)";data.rows[0].availability=VoltagePolicyAvailability::unsupported;
  frame_part(VoltagePolicyPart::admission,nullptr,-1);
  CHECK(has("Gate read unavailable: Voltage policy rejected in current mode (RM 31)")&&!has("Driver voltage requests:")&&count("RM 31")==1);
  data.gate_read_error.clear();data.rows[0].message.clear();data.rows[0].availability=VoltagePolicyAvailability::available;
  data.vf_read_error=data.rows[1].message=data.rows[2].message="Bounded voltage-policy read failed";
  frame_part(VoltagePolicyPart::vf_policies,nullptr,-1);
  CHECK(has("VF policy read unavailable: Bounded voltage-policy read failed")&&!has("Monotonicity:")&&count("Bounded voltage-policy read failed")==1);
  data.vf_read_error.clear();data.rows[1].message.clear();data.rows[2].message.clear();
  // Python's single status line carries no captured initial and no service text; after this part's own
  // request the published result takes the line, as _set_voltage_gate / _set_clock_vf_live set it.
  part.last={};
  data.rows[0].initial=true;data.message=data.read_message="Voltage policies read: 3 / 3 selected flags available";
  frame_part(VoltagePolicyPart::admission,nullptr,-1);CHECK(has("Driver voltage requests: suppressed.")&&!has("Initial:")&&!has("Voltage policies read"));
  data.rows[1].initial=data.rows[2].initial=true;frame_part(VoltagePolicyPart::vf_policies,nullptr,-1);CHECK(!has("Initial:"));
  data.message="Selected host policy request read back exactly; physical effect is not established";
  frame_part(VoltagePolicyPart::admission,nullptr,-1);CHECK(!has("Selected host policy request")&&has("Driver voltage requests: suppressed."));  // not this part's request
  {const bool gate_before=*data.rows[0].current,monotonicity_before=*data.rows[1].current;
   part.dirty[0]=true;part.drafts[0]=true;auto gate_apply=frame_part(VoltagePolicyPart::admission,"Apply gate now",0);
   CHECK(gate_apply.intents.size()==1&&gate_apply.intents[0].request.selection==1&&gate_apply.intents[0].request.values==1);
   frame_part(VoltagePolicyPart::admission,nullptr,-1);CHECK(has("Driver voltage requests: suppressed.")&&!has("Applied and verified"));  // not yet published
   data.rows[0].current=true;++data.revision;frame_part(VoltagePolicyPart::admission,nullptr,-1);
   CHECK(has("Applied and verified without a GPU restart. Driver voltage requests: allowed.")&&!has("Selected host policy request"));
   frame_part(VoltagePolicyPart::vf_policies,nullptr,-1);CHECK(!has("Applied")&&has("Monotonicity: off, smoothing: on."));  // the result stays with its part
   // A request that leaves the flag elsewhere failed: its reason once, without the row's copy of it.
   part.dirty[0]=true;part.drafts[0]=false;CHECK(frame_part(VoltagePolicyPart::admission,"Apply gate now",0).intents.size()==1);
   data.message=data.rows[0].message="Voltage policy SET rejected (RM 31)";data.rows[0].availability=VoltagePolicyAvailability::unsupported;++data.revision;
   frame_part(VoltagePolicyPart::admission,nullptr,-1);CHECK(has("Gate change failed: Voltage policy SET rejected (RM 31)")&&count("RM 31")==1);
   data.rows[0].message.clear();data.rows[0].availability=VoltagePolicyAvailability::available;
   // A VF check that reads back its request: "Applied live. " and the VF line.
   part.dirty[1]=true;part.drafts[1]=!*data.rows[1].current;auto vf_apply=frame_part(VoltagePolicyPart::vf_policies,"Apply policies now",1);
   CHECK(vf_apply.intents.size()==1&&vf_apply.intents[0].request.selection==2);
   data.rows[1].current=part.drafts[1];data.message="Selected host policy request read back exactly; physical effect is not established";++data.revision;
   frame_part(VoltagePolicyPart::vf_policies,nullptr,-1);
   CHECK(has("Applied live. Monotonicity: on, smoothing: on. Startup mode and registry settings are retained.")&&!has("Selected host policy request"));
   // Read brings back both readback lines.
   CHECK(frame_part(VoltagePolicyPart::admission,"Read voltage-request gate",-1).intents.size()==1);data.message=data.read_message;++data.revision;
   frame_part(VoltagePolicyPart::vf_policies,nullptr,-1);CHECK(has("Monotonicity: on, smoothing: on.")&&!has("Applied live."));
   frame_part(VoltagePolicyPart::admission,nullptr,-1);CHECK(has("Driver voltage requests: allowed.")&&!has("Gate change failed")&&!has("Applied and verified"));
   data.rows[0].current=gate_before;data.rows[1].current=monotonicity_before;part.dirty={};part.last={};}
  data.rows[0].initial=data.rows[1].initial=data.rows[2].initial=false;data.message.clear();data.read_message.clear();
  const auto frame_shared=[&](const char* activate){ImGui::NewFrame();ImGui::SetNextWindowSize({1300,1300});ImGui::Begin("shared");
   if(activate){ImGui::PushID("voltage-policy");ImGui::PushID(200);auto& ctx=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;ImGui::PopID();ImGui::PopID();}
   ImGui::LogToBuffer();auto result=draw_voltage_policy_shared_controls(part,&data,2,false,true,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  data.undo_available=true;auto shared=frame_shared("Undo voltage policy");
  CHECK(shared.intents.size()==1&&shared.intents[0].operation==VoltagePolicyIntent::Operation::undo);
  // Flags appear only as names in the folded saved selection (logging expands tree nodes).
  CHECK(text.find("Apply gate now")==std::string::npos&&text.find("Read voltage policies")==std::string::npos&&text.find("Apply staged policy profile")==std::string::npos&&text.find("Load policy profile...")!=std::string::npos
      &&text.find("Save a selection of policies")<text.find("Allow voltage requests"));
  frame_shared("Discard staged policy profile");CHECK(!part.staged);
  // A profile holding one VF check alone is still saved from the shared subsection's folded selection.
  const auto frame_tree=[&](const char* activate){ImGui::NewFrame();ImGui::SetNextWindowSize({1300,1300});ImGui::Begin("shared");
   ImGui::PushID("voltage-policy");ImGui::PushID(200);const auto tree=ImGui::GetID("Save a selection of policies");ImGui::GetStateStorage()->SetInt(tree,1);
   if(activate){ImGui::PushOverrideID(tree);auto& ctx=*ImGui::GetCurrentContext();const auto id=ImGui::GetID(activate);ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;ImGui::PopID();}
   ImGui::PopID();ImGui::PopID();
   ImGui::LogToBuffer();auto result=draw_voltage_policy_shared_controls(part,&data,2,false,true,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  part.included={false,false,true};auto subset=frame_tree("Save selected policy profile...");
  CHECK(text.find("Enforce VF smoothing")!=std::string::npos&&subset.intents.size()==1&&subset.intents[0].operation==VoltagePolicyIntent::Operation::save_profile&&subset.intents[0].request.selection==4);
  part.included={true,false,true};CHECK(frame_tree("Save selected policy profile...").intents.empty()&&text.find("Save admission separately")!=std::string::npos);
 }
 ImGui::DestroyContext();std::printf("voltage policy view: %u checks\n",checks);return 0;}
