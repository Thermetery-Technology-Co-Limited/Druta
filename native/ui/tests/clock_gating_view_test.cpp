// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_gating_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static std::vector<ClockGatingIntent> frame(ClockGatingViewState& state,const ClockGatingData& data,bool unlocked,bool busy=false,const char* activate=nullptr,int family=-1){ImGui::NewFrame();ImGui::SetNextWindowSize({1600,3000});ImGui::Begin("gating-test");ImGui::PushID("clock-gating");if(family>=0)ImGui::PushID(family);if(activate){auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;}if(family>=0)ImGui::PopID();ImGui::PopID();auto out=draw_clock_gating_controls(state,&data,1,busy,unlocked);ImGui::End();ImGui::Render();return out;}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1800,3300};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);ClockGatingViewState state;ClockGatingData data;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.uuid,"GPU-alternate");std::strcpy(data.identity.slot,"0000:06:00.0");
 for(size_t i=0;i<4;++i){auto& s=data.families[i];s.label="Test family";s.binding="binding-"+std::to_string(i);s.availability=VoltagePolicyAvailability::available;s.support=true;s.can_disable=true;s.can_enable=i!=1;}auto& p=data.families[3];p.target_generation=p.generation=p.root_generation=p.initial_generation=1;p.width=p.root_width=16;p.allowed_generations={1,2,3};
 CHECK(frame(state,data,false).empty());auto out=frame(state,data,false,false,"Read clock gating / PCIe");CHECK(out.size()==1&&out[0].families==15);out=frame(state,data,false,false,"Read this family",2);CHECK(out.size()==1&&out[0].families==4);
 CHECK(frame(state,data,false,false,"Own DISABLE request",0).empty());out=frame(state,data,true,false,"Own DISABLE request",0);CHECK(out.size()==1&&out[0].requests[0].family==ClockGatingFamily::engine&&out[0].requests[0].mode==ClockGatingMode::disabled);CHECK(frame(state,data,true,true,"Own DISABLE request",0).empty());
 data.families[0].can_release=true;data.families[0].stale=true;data.families[0].availability=VoltagePolicyAvailability::transient;out=frame(state,data,true,false,"Release owned request",0);CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::release);out=frame(state,data,true,false,"Recover / free owned object",0);CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::recover);
 state.pcie_generation=2;state.pcie_dirty=true;p.target_generation=3;out=frame(state,data,true,false,"Apply PCIe generation",3);CHECK(out.size()==1&&out[0].requests[0].generation==2&&out[0].requests[0].expected_generation==1);frame(state,data,true,false,"Use current target",3);CHECK(!state.pcie_dirty&&state.pcie_baseline==3&&state.pcie_generation==3);
 state.included[3]=true;out=frame(state,data,false,false,"Save selected clock profile...");CHECK(out.size()==1&&out[0].selection.size()==1&&out[0].selection[0]==ClockGatingFamily::pcie);out=frame(state,data,false,false,"Load clock profile...");CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::load_profile);
 auto document=capture_clock_gating_profile(data,{ClockGatingFamily::pcie});document["entries"][0]["generation"]=2;CHECK(stage_clock_gating_document(state,document,data)&&state.staged.size()==1);CHECK(frame(state,data,false,false,"Apply staged clock profile").empty());out=frame(state,data,true,false,"Apply staged clock profile");CHECK(out.size()==1&&out[0].requests[0].generation==2);
 data.undo_available=true;out=frame(state,data,true,false,"Undo clock / PCIe requests");CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::undo);sync_clock_gating_view(state,&data,2);CHECK(state.staged.empty()&&!state.pcie_dirty&&state.generation==2);
 {// Classic draws one family per released 1.9.1 subsection; Save stays scoped to that family.
  ClockGatingViewState part;std::string text;
  const auto frame_part=[&](const ClockGatingData* d,ClockGatingFamily family,const char* activate,bool in_family,bool busy=false,bool unlocked=true){ImGui::NewFrame();ImGui::SetNextWindowSize({1600,3000});ImGui::Begin("gating-part");
   if(activate){ImGui::PushID("clock-gating");ImGui::PushID(100+static_cast<int>(family));if(in_family)ImGui::PushID(static_cast<int>(family));auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;if(in_family)ImGui::PopID();ImGui::PopID();ImGui::PopID();}
   ImGui::LogToBuffer();auto result=draw_clock_gating_controls(part,d,1,busy,unlocked,true,family);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  const auto has=[&](const char* s){return text.find(s)!=std::string::npos;};
  // pcie_speed_panel.py: Refresh, Save PCIe speed profile, a generation selector, Request speed.
  CHECK(frame_part(&data,ClockGatingFamily::pcie,nullptr,false).empty());
  CHECK(has("Request speed")&&has("Save PCIe speed profile")&&has("Refresh")&&has("not a permanent speed lock")&&has("Gen 3")&&has("Programmed target: Gen 3")&&has("GPU link: Gen 1, x16; root link: Gen 1, x16")&&has("retrain the link")&&has("Request previous speed")
      &&!has("Apply PCIe generation")&&!has("Target generation")&&!has("Hold disabled")&&!has("Read clock gating / PCIe")&&!has("Include in saved profile"));
  out=frame_part(&data,ClockGatingFamily::pcie,"Save PCIe speed profile",false);
  CHECK(out.size()==1&&out[0].selection.size()==1&&out[0].selection[0]==ClockGatingFamily::pcie);
  part.pcie_generation=2;part.pcie_dirty=true;
  {// Request speed and Restore captured initial target are inert while locked or busy; Request speed
   // also on a stale, uncertain or unsupported readback, Restore without a captured initial target.
   const auto inert=[&](const char* label,bool busy=false,bool unlocked=true){return frame_part(&data,ClockGatingFamily::pcie,label,false,busy,unlocked).empty();};
   CHECK(inert("Request speed",false,false)&&inert("Request speed",true,true));
   p.stale=true;CHECK(inert("Request speed"));p.stale=false;
   p.uncertain=true;CHECK(inert("Request speed"));p.uncertain=false;
   p.support=false;CHECK(inert("Request speed"));p.support=true;
   CHECK(inert("Restore captured initial target",false,false)&&inert("Restore captured initial target",true,true));
   p.initial_generation.reset();CHECK(inert("Restore captured initial target"));p.initial_generation=1;
   CHECK(part.pcie_generation==2&&!part.pcie_pending&&!part.last[3]);
  }
  out=frame_part(&data,ClockGatingFamily::pcie,"Request speed",false);
  CHECK(out.size()==1&&out[0].requests.size()==1&&out[0].requests[0].generation==2&&out[0].requests[0].expected_generation==3&&out[0].requests[0].binding=="binding-3");
  out=frame_part(&data,ClockGatingFamily::pcie,"Restore captured initial target",false);CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::restore_initial);
  // Request previous speed (pcie_speed_panel.py): nothing until a later readback verifies the
  // requested target and both links; then it sends the same apply with the replaced target.
  CHECK(!part.pcie_previous&&frame_part(&data,ClockGatingFamily::pcie,"Request previous speed",false).empty());
  p.target_generation=p.generation=p.root_generation=2;++data.revision;
  // With a known previous target it is still inert while locked, busy, stale or uncertain.
  CHECK(frame_part(&data,ClockGatingFamily::pcie,"Request previous speed",false,false,false).empty()&&part.pcie_previous==3u);
  CHECK(frame_part(&data,ClockGatingFamily::pcie,"Request previous speed",false,true,true).empty());
  p.stale=true;CHECK(frame_part(&data,ClockGatingFamily::pcie,"Request previous speed",false).empty());p.stale=false;
  p.uncertain=true;CHECK(frame_part(&data,ClockGatingFamily::pcie,"Request previous speed",false).empty());p.uncertain=false;
  CHECK(part.pcie_previous==3u&&!part.pcie_pending);
  out=frame_part(&data,ClockGatingFamily::pcie,"Request previous speed",false);
  CHECK(part.pcie_previous==3u&&out.size()==1&&out[0].operation==ClockGatingIntent::Operation::apply&&out[0].requests.size()==1
      &&out[0].requests[0].generation==3&&out[0].requests[0].expected_generation==2&&out[0].requests[0].binding=="binding-3");
  ++data.revision;frame_part(&data,ClockGatingFamily::pcie,nullptr,false);  // readback did not reach Gen 3: forgotten
  CHECK(!part.pcie_previous&&frame_part(&data,ClockGatingFamily::pcie,"Request previous speed",false).empty());
  p.target_generation=3;p.generation=p.root_generation=1;
  // engine_clock_gating_panel.py: Refresh, Save request profile, Hold disabled, Hold enabled, Release.
  frame_part(&data,ClockGatingFamily::engine,nullptr,false);
  CHECK(has("Hold enabled or disabled requests that state for engine clock gating")&&has("A saved profile describes only this instance's request")
      &&has("Hold disabled")&&has("Hold enabled")&&has("Release")&&has("This instance's request: not held")&&!has("Owned request:")&&!has("Source READ32")
      &&!has("Own DISABLE request")&&!has("Read this family")&&!has("Clock gating & PCIe requests"));  // no inner title under the subsection bar
  out=frame_part(&data,ClockGatingFamily::engine,"Hold enabled",false);
  CHECK(out.size()==1&&out[0].requests[0].mode==ClockGatingMode::enabled&&out[0].requests[0].binding=="binding-0");
  out=frame_part(&data,ClockGatingFamily::engine,"Hold disabled",false);
  CHECK(out.size()==1&&out[0].requests[0].mode==ClockGatingMode::disabled);
  {// Hold disabled, Hold enabled, Release and Recover are inert while locked or busy, and each hold
   // or release also when the readback does not offer it.
   auto& eng=data.families[0];const bool release_before=eng.can_release;eng.can_release=true;
   const auto inert=[&](const char* label,bool busy=false,bool unlocked=true){return frame_part(&data,ClockGatingFamily::engine,label,false,busy,unlocked).empty();};
   for(const char* label:{"Hold disabled","Hold enabled","Release","Recover / free owned object"})CHECK(inert(label,false,false)&&inert(label,true,true));
   eng.can_disable=false;CHECK(inert("Hold disabled"));eng.can_disable=true;
   eng.can_enable=false;CHECK(inert("Hold enabled"));eng.can_enable=true;
   eng.can_release=false;CHECK(inert("Release")&&inert("Recover / free owned object"));
   eng.can_release=release_before;
  }
  frame_part(&data,ClockGatingFamily::block,nullptr,false);CHECK(has("Hold disabled")&&!has("Hold enabled"));
  out=frame_part(nullptr,ClockGatingFamily::second_level,"Refresh",false);
  CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::read&&out[0].families==4&&out[0].family==ClockGatingFamily::second_level);
  CHECK(frame_part(nullptr,ClockGatingFamily::second_level,"Hold disabled",false).empty());
  {// engine_clock_gating_panel.py _failed after a hold: the hold's own result, kept from when it was published.
   auto d2=data;auto& eng=d2.families[0];eng.availability=VoltagePolicyAvailability::available;eng.stale=eng.owned=eng.can_release=false;eng.message="Fresh source-qualified inspection";
   CHECK(frame_part(&d2,ClockGatingFamily::engine,"Hold disabled",false).size()==1);
   d2.message="ELCG request produced no independent engine eligibility transition";++d2.revision;frame_part(&d2,ClockGatingFamily::engine,nullptr,false);
   d2.message="Explicit clock/PCIe inspection completed; each family reports independent status";frame_part(&d2,ClockGatingFamily::engine,nullptr,false);
   CHECK(has("Engine clock gating hold failed: ELCG request produced no independent engine eligibility transition. Refresh status."));
   // Recover is not one of the panel's operations; the line no longer names the hold.
   eng.can_release=true;CHECK(frame_part(&d2,ClockGatingFamily::engine,"Recover / free owned object",false).size()==1&&!part.last[0]);
  }
  {// The panels print nothing under the status line and box: neither the service's account nor its
   // verification scope, and the host's queued note goes once the action's result is published.
   auto d3=data;auto& eng=d3.families[0];eng.availability=VoltagePolicyAvailability::available;eng.stale=eng.owned=eng.can_release=false;
   eng.verification_scope="Partial independent engine eligibility witness; not physical gating";
   d3.message="Explicit clock/PCIe inspection completed; each family reports independent status";
   CHECK(frame_part(&d3,ClockGatingFamily::engine,"Refresh",false).size()==1);
   part.status="Operation queued; see operation log.";frame_part(&d3,ClockGatingFamily::engine,nullptr,false);
   CHECK(has("Operation queued")&&!has("Explicit clock/PCIe inspection completed")&&!has("Partial independent engine eligibility witness"));
   ++d3.revision;frame_part(&d3,ClockGatingFamily::engine,nullptr,false);
   CHECK(has("Engine clock-gating status refreshed.")&&!has("Operation queued")&&!has("Explicit clock/PCIe inspection completed"));
  }
  // A family applies only its own staged request; Undo and Load span every family and stay off its heading.
  part.staged={{ClockGatingFamily::engine,ClockGatingMode::disabled,0,"binding-0",{}},{ClockGatingFamily::pcie,ClockGatingMode::released,2,"binding-3",std::optional<uint32_t>(1)}};
  data.undo_available=true;frame_part(&data,ClockGatingFamily::engine,nullptr,false);
  CHECK(text.find("Undo clock / PCIe requests")==std::string::npos&&text.find("Load clock profile...")==std::string::npos&&text.find("PCIe target: Gen2")==std::string::npos);
  CHECK(frame_part(&data,ClockGatingFamily::engine,"Apply staged request",false,false,false).empty()
      &&frame_part(&data,ClockGatingFamily::engine,"Apply staged request",false,true,true).empty()&&part.staged.size()==2);
  out=frame_part(&data,ClockGatingFamily::engine,"Apply staged request",false);
  CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::apply&&out[0].requests.size()==1&&out[0].requests[0].family==ClockGatingFamily::engine);
  frame_part(&data,ClockGatingFamily::block,nullptr,false);CHECK(text.find("Apply staged")==std::string::npos);
  const auto frame_shared=[&](const char* activate){ImGui::NewFrame();ImGui::SetNextWindowSize({1600,3000});ImGui::Begin("gating-shared");
   if(activate){ImGui::PushID("clock-gating");ImGui::PushID(200);auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;ImGui::PopID();ImGui::PopID();}
   ImGui::LogToBuffer();auto result=draw_clock_gating_shared_controls(part,&data,1,false,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  out=frame_shared("Undo clock / PCIe requests");CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::undo);
  // Families appear only as names in the folded saved selection (logging expands tree nodes).
  CHECK(text.find("Load clock profile...")!=std::string::npos&&text.find("Apply staged")==std::string::npos&&text.find("Own DISABLE request")==std::string::npos
      &&text.find("Hold disabled")==std::string::npos&&text.find("Save a selection of families")<text.find("Save selected clock profile..."));
  // A profile holding several families is still saved from the shared subsection's folded selection.
  const auto frame_tree=[&](const char* activate){ImGui::NewFrame();ImGui::SetNextWindowSize({1600,3000});ImGui::Begin("gating-shared");
   ImGui::PushID("clock-gating");ImGui::PushID(200);const auto tree=ImGui::GetID("Save a selection of families");ImGui::GetStateStorage()->SetInt(tree,1);
   if(activate){ImGui::PushOverrideID(tree);auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;ImGui::PopID();}
   ImGui::PopID();ImGui::PopID();
   ImGui::LogToBuffer();auto result=draw_clock_gating_shared_controls(part,&data,1,false,true);text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return result;};
  part.included={};CHECK(frame_tree("Save selected clock profile...").empty()&&text.find("Manual PCIe speed request")!=std::string::npos);
  part.included[0]=part.included[3]=true;out=frame_tree("Save selected clock profile...");
  CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::save_profile&&out[0].selection.size()==2&&out[0].selection[0]==ClockGatingFamily::engine&&out[0].selection[1]==ClockGatingFamily::pcie);
 }
 {// The readback boxes carry the panels' _format lines; native detail only follows them.
  using A=VoltagePolicyAvailability;ClockGatingState e;e.binding="engine";e.availability=A::available;e.support=true;e.supported_engines=0xF;e.eligible_engines=0x7;
  CHECK(clock_gating_readback(e,ClockGatingFamily::engine)=="Request readback: available\nThis instance's request: not held\nEngines with no recorded disable reason: 3\n"
   "Other disable reasons are active; release those before holding enabled.\nSupported engine mask: 0x000000000000000F\nEligible engine mask: 0x0000000000000007");
  e.owned=true;e.mode=ClockGatingMode::enabled;e.eligible_engines=0xF;e.observed_engines=0x5;e.arbitration_verified=true;
  CHECK(clock_gating_readback(e,ClockGatingFamily::engine).find("This instance's request: held enabled\nEngines with no recorded disable reason: 4\n"
   "Observed changes on engine bits: 0, 2 (partial readback)\nConflicting request rejection verified for this enabled hold.\nSupported engine mask")!=std::string::npos);
  e.owned=false;e.uncertain=true;e.eligible_engines.reset();e.can_release=true;
  CHECK(clock_gating_readback(e,ClockGatingFamily::engine).find("Request readback: available\nThis instance's request outcome is uncertain; use Release.\nEngines with no recorded disable reason: unknown")==0);
  // Release is not offered when no owned object is left to release (an unproved allocation).
  e.can_release=false;CHECK(clock_gating_readback(e,ClockGatingFamily::engine).find("Request readback: available\nThis instance's request outcome is uncertain.\nEngines with")==0);
  ClockGatingState b;b.binding="block";b.availability=A::available;b.support=true;
  b.registers={{0x20200,0x1,0,0x1,0,true,"available"},{0x20204,0x0,0,0x2,0,true,"available"}};
  CHECK(clock_gating_readback(b,ClockGatingFamily::block)=="Request readback: available\nThis instance's disable request: not held\nRegister 0x20200: 0x00000001\nRegister 0x20204: 0x00000000");
  b.registers[0].value=0;CHECK(clock_gating_readback(b,ClockGatingFamily::block).find("Register 0x20204: 0x00000000\nNo register bits are available to verify a new request.")!=std::string::npos);
  b.owned=true;CHECK(clock_gating_readback(b,ClockGatingFamily::block).find("This instance's disable request: held")!=std::string::npos&&clock_gating_readback(b,ClockGatingFamily::block).find("No register bits")==std::string::npos);
  // block_clock_gating_panel.py has no "Register X: unavailable" line; the native qualification line ends the box.
  b.registers[1]={0x20204,0,8,0x2,0,false,"unsupported_operation"};
  CHECK(clock_gating_readback(b,ClockGatingFamily::block)=="Request readback: available\nThis instance's disable request: held\nRegister 0x20200: 0x00000000\nRegister 0x20204 readback: unsupported_operation, status 8");
  // block_clock_gating_panel.py: the registers whose bits the held request cleared.
  b.observed_registers=1;
  CHECK(clock_gating_readback(b,ClockGatingFamily::block)=="Request readback: available\nThis instance's disable request: held\nRegister 0x20200: 0x00000000\n"
   "Registers with observed changes: 1 (partial readback)\nRegister 0x20204 readback: unsupported_operation, status 8");
  b.observed_registers=0;
  ClockGatingState slcg;slcg.binding="slcg";slcg.availability=A::available;slcg.support=true;slcg.layout="tu10x";slcg.unverified_source_peers={0x248018,0x246018};
  slcg.registers={{0x30000,1,0,0,1,true,"available"},{0x30004,0,8,0,1,false,"unsupported_operation"}};
  CHECK(clock_gating_readback(slcg,ClockGatingFamily::second_level)=="Request readback: partial\nThis instance's request: not held\nRegister 0x30000: 0x00000001\nRegister 0x30004: unavailable\n"
   "Other source-table registers: 2 unverified.\nThe named request also changes those registers; their prior values are not verified.\nRegister 0x30004 readback: unsupported_operation, status 8");
  slcg.registers[0].value=0;CHECK(clock_gating_readback(slcg,ClockGatingFamily::second_level).find("Disabled hold needs the source release baseline; enabled hold may be available.")!=std::string::npos);
  slcg.registers[0].value=5;CHECK(clock_gating_readback(slcg,ClockGatingFamily::second_level).find("Readbacks do not match the required starting values; a new request cannot be verified.")!=std::string::npos);
  // second_level_clock_gating_panel.py: observed registers after the starting-value lines, before arbitration.
  slcg.owned=true;slcg.mode=ClockGatingMode::enabled;slcg.observed_registers=2;slcg.arbitration_verified=true;
  CHECK(clock_gating_readback(slcg,ClockGatingFamily::second_level).find("their prior values are not verified.\nRegisters with observed changes: 2 (partial readback)\n"
   "Conflicting request rejection verified for this enabled hold.")!=std::string::npos);
  slcg.owned=false;slcg.mode=ClockGatingMode::released;slcg.observed_registers=0;slcg.arbitration_verified=false;
  ClockGatingState unread;CHECK(clock_gating_readback(unread,ClockGatingFamily::engine).empty());
  unread.availability=A::unsupported;unread.message="Native clock-gating source layout is unsupported";
  CHECK(clock_gating_readback(unread,ClockGatingFamily::second_level)=="Request readback: unavailable");
  // The status line: the prompt before a read, the panel's refreshed line after one, the
  // request's own wording once a later readback confirms it, and a failure's reason.
  ClockGatingData d;ClockGatingViewState st;d.revision=5;
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Refresh status before requesting a change."&&clock_gating_status(st,&d,ClockGatingFamily::pcie)=="Refresh status before requesting a speed.");
  d.families[0]=e;d.families[0].uncertain=false;d.families[0].message="Fresh source-qualified inspection";d.families[1]=b;d.families[1].owned=false;
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Engine clock-gating status refreshed."&&clock_gating_status(st,&d,ClockGatingFamily::block)=="Block clock-gating status refreshed.");
  using Kind=ClockGatingViewState::LastAction::Kind;
  st.last[0]=ClockGatingViewState::LastAction{Kind::hold_disabled,5,0,false};CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Engine clock-gating status refreshed.");  // not yet published
  d.revision=6;d.families[0].owned=true;d.families[0].mode=ClockGatingMode::disabled;CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Disable request held by this instance.");
  st.last[1]=ClockGatingViewState::LastAction{Kind::release,5,0,false};CHECK(clock_gating_status(st,&d,ClockGatingFamily::block)=="This instance's disable request was released.");
  d.families[0].stale=true;d.families[0].availability=A::transient;d.families[0].message="Bounded clock witness read failed";
  // The panels' _failed: "<family> <operation> failed: <reason>. <action>", Release offered only while it is enabled.
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Engine clock gating hold failed: Bounded clock witness read failed. Refresh status.");
  d.families[0].can_release=true;CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Engine clock gating hold failed: Bounded clock witness read failed. Use Release or refresh status.");
  st.last[0]=ClockGatingViewState::LastAction{Kind::read,6,0,false,Kind::hold_enabled,{}};  // a Refresh not yet published: still the hold's failure
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Engine clock gating hold enabled failed: Bounded clock witness read failed. Use Release or refresh status.");
  st.last[0]->revision=5;CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Engine clock gating refresh failed: Bounded clock witness read failed. Refresh status.");
  st.last[0].reset();CHECK(clock_gating_status(st,&d,ClockGatingFamily::engine)=="Bounded clock witness read failed");  // after Recover or Undo
  // A hold or release the readback does not confirm reports its own result.
  st.last[1]=ClockGatingViewState::LastAction{Kind::hold_disabled,5,0,false,{},std::string("Block request changed no readback bit")};
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::block)=="Block clock gating hold failed: Block request changed no readback bit. Refresh status.");
  d.families[1].owned=d.families[1].can_release=true;st.last[1]=ClockGatingViewState::LastAction{Kind::release,5,0,false,{},std::string("Owned cleanup/verification: FREE refused")};
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::block)=="Block clock gating release failed: Owned cleanup/verification: FREE refused. Use Release or refresh status.");
  d.families[1].owned=d.families[1].can_release=false;
  auto& pc=d.families[3];pc.binding="pcie";pc.availability=A::available;pc.support=true;pc.target_generation=pc.generation=pc.root_generation=3;
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="Status refreshed.");
  const auto fresh_box=clock_gating_readback_box(st,&d,ClockGatingFamily::pcie);
  CHECK(fresh_box.find("Programmed target: Gen 3")==0&&fresh_box.find("refresh required")==std::string::npos&&fresh_box.find("stale")==std::string::npos);
  st.last[3]=ClockGatingViewState::LastAction{Kind::request,6,2,false};d.revision=7;
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="Requested Gen 2; target and link change was not verified. Refresh to check.");
  // pcie_speed_panel.py: an unverified request heads the box until a refresh.
  CHECK(clock_gating_readback_box(st,&d,ClockGatingFamily::pcie)=="Historical or unverified readback (refresh required):\n"+clock_gating_readback(pc,ClockGatingFamily::pcie));
  pc.target_generation=pc.generation=pc.root_generation=2;CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="Requested Gen 2; target and link readback verified.");
  CHECK(clock_gating_readback_box(st,&d,ClockGatingFamily::pcie).find("Programmed target: Gen 2")==0);
  st.last[3]->matched=true;CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="Gen 2 already matches the readback; nothing written.");
  {// pcie_speed_panel.py _request: a request that raised before anything was sent is _failed's; the
   // service keeps no prior target for it. One it sent but could not verify keeps that target for
   // Undo (uncertain) and stays "not verified".
   const auto kept=st.last[3];pc.target_generation=pc.generation=pc.root_generation=3;
   st.last[3]=ClockGatingViewState::LastAction{Kind::request,6,2,false,{},std::string("PCIe target changed since the draft")};
   CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="PCIe speed request failed: PCIe target changed since the draft. Refresh before requesting a speed.");
   CHECK(clock_gating_readback_box(st,&d,ClockGatingFamily::pcie)=="Historical readback (refresh required):\n"+clock_gating_readback(pc,ClockGatingFamily::pcie));
   pc.uncertain=true;st.last[3]->outcome="PCIe request dispatched but target/links/width/preserved bits were not verified; typed Undo retained";
   CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="Requested Gen 2; target and link change was not verified. Refresh to check.");
   CHECK(clock_gating_readback_box(st,&d,ClockGatingFamily::pcie)=="Historical or unverified readback (refresh required):\n"+clock_gating_readback(pc,ClockGatingFamily::pcie));
   pc.uncertain=false;pc.target_generation=pc.generation=pc.root_generation=2;st.last[3]=kept;}
  // pcie_speed_panel.py _failed.
  pc.stale=true;pc.availability=A::transient;pc.message="PCIe link readback changed during capture";
  CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="PCIe speed request failed: PCIe link readback changed during capture. Refresh before requesting a speed.");
  st.last[3]->kind=Kind::read;CHECK(clock_gating_status(st,&d,ClockGatingFamily::pcie)=="PCIe speed refresh failed: PCIe link readback changed during capture. Refresh before requesting a speed.");
  // _failed heads the kept box; the native stale note is not added under it.
  const auto old_box=clock_gating_readback_box(st,&d,ClockGatingFamily::pcie);
  CHECK(old_box.find("Historical readback (refresh required):\nProgrammed target: Gen 2")==0&&old_box.find("Last successful read; stale.")==std::string::npos);
  CHECK(clock_gating_readback(b,ClockGatingFamily::block).find("stale")==std::string::npos);
  auto stale_block=b;stale_block.stale=true;CHECK(clock_gating_readback(stale_block,ClockGatingFamily::block).find("Last successful read; stale.")!=std::string::npos); // other families keep the native note
 }
 ImGui::DestroyContext();std::printf("Clock gating view: %u checks\n",checks);return 0;}
