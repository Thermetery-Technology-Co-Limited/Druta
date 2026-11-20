// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_gating_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
#include <algorithm>
#include <cfloat>
#include <cstdio>
#include <string>
namespace druta::ui {
void sync_clock_gating_view(ClockGatingViewState& state,const ClockGatingData* data,uint64_t generation){if(!state.initialized||state.generation!=generation){state={};state.initialized=true;state.generation=generation;}if(data&&!state.pcie_dirty){const auto& pcie=data->families[3];state.pcie_generation=pcie.target_generation.value_or(1);state.pcie_baseline=pcie.target_generation;state.pcie_binding=pcie.binding;}}
bool stage_clock_gating_document(ClockGatingViewState& state,const ProfileJson& document,const ClockGatingData& data){try{state.staged=stage_clock_gating_profile(document,data);state.status="Profile staged only. Review targets, then Apply explicitly in this session.";return true;}catch(const std::exception& e){state.staged.clear();state.status=e.what();return false;}}
namespace {
enum class Scope {all,family,shared};
// Undo, Load and the staged list span every family, so classic draws Undo and Load
// once in a shared subsection and each family applies only its own staged request.
std::vector<ClockGatingIntent> draw_clock_gating(ClockGatingViewState& state,const ClockGatingData* data,uint64_t generation,bool busy,bool unlocked,Scope scope,std::optional<ClockGatingFamily> only){sync_clock_gating_view(state,data,generation);std::vector<ClockGatingIntent> out;ImGui::PushID("clock-gating");if(scope==Scope::family)ImGui::PushID(100+static_cast<int>(*only));else if(scope==Scope::shared)ImGui::PushID(200);
 const bool all=scope==Scope::all,in_family=scope==Scope::family,shared=scope==Scope::shared;
 const auto shown=[&](ClockGatingFamily f){return !in_family||*only==f;};
 if(all)ImGui::TextUnformatted("Clock gating & PCIe requests");
 if(all||(in_family&&*only!=ClockGatingFamily::pcie))ImGui::TextWrapped("Named gating requests own a profiler object. Independent engine/register witnesses are partial configuration evidence, not physical gating or power measurements. Release frees only this session's object.");
 if(all){ImGui::BeginDisabled(busy);if(ImGui::Button("Read clock gating / PCIe"))out.push_back({ClockGatingIntent::Operation::read,15,{},{},{}});ImGui::EndDisabled();}
 else if(in_family&&!data){ImGui::PushID(static_cast<int>(*only));ImGui::BeginDisabled(busy);if(ImGui::Button("Read this family"))out.push_back({ClockGatingIntent::Operation::read,uint32_t(1u<<static_cast<uint32_t>(*only)),*only,{},{}});ImGui::EndDisabled();ImGui::PopID();}
 if(data&&!shared)for(size_t i=0;i<4;++i){const auto& s=data->families[i];const auto family=ClockGatingFamily(i);if(!shown(family))continue;ImGui::PushID(static_cast<int>(i));if(all)ImGui::Separator();ImGui::TextWrapped("%s%s",s.label.c_str(),s.stale?" (stale)":"");
  ImGui::BeginDisabled(busy);if(ImGui::Button("Read this family"))out.push_back({ClockGatingIntent::Operation::read,uint32_t(1u<<i),family,{},{}});ImGui::EndDisabled();ImGui::SameLine();ImGui::Checkbox("Include in saved profile",&state.included[i]);
  if(i<3){ImGui::Text("Owned request: %s%s",s.owned?(s.mode==ClockGatingMode::enabled?"ENABLE":"DISABLE"):"none",s.uncertain?"; outcome uncertain":"");
   if(s.supported_engines)ImGui::Text("Supported engines: 0x%016llX",static_cast<unsigned long long>(*s.supported_engines));if(s.eligible_engines)ImGui::Text("Eligible engines: 0x%016llX",static_cast<unsigned long long>(*s.eligible_engines));
   ImGui::BeginDisabled(busy||!unlocked||!s.can_disable);if(ImGui::Button("Own DISABLE request"))out.push_back({ClockGatingIntent::Operation::apply,0,family,{{family,ClockGatingMode::disabled,0,s.binding,{}}},{}});ImGui::EndDisabled();
   if(family!=ClockGatingFamily::block){ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!s.can_enable);if(ImGui::Button("Own ENABLE request"))out.push_back({ClockGatingIntent::Operation::apply,0,family,{{family,ClockGatingMode::enabled,0,s.binding,{}}},{}});ImGui::EndDisabled();}
   ImGui::BeginDisabled(busy||!unlocked||!s.can_release);if(ImGui::Button("Release owned request"))out.push_back({ClockGatingIntent::Operation::release,0,family,{},{}});ImGui::SameLine();if(ImGui::Button("Recover / free owned object"))out.push_back({ClockGatingIntent::Operation::recover,0,family,{},{}});ImGui::EndDisabled();
   if(s.arbitration_verified)ImGui::TextWrapped("Opposing DISABLE was explicitly rejected. This is not proof of unique ownership.");
   if(!s.registers.empty()&&ImGui::TreeNode("Source READ32 witnesses")){for(const auto& r:s.registers)if(r.available)ImGui::Text("0x%08X = 0x%08X",r.address,r.value);else ImGui::Text("0x%08X unavailable (%s, status %u)",r.address,r.qualification.c_str(),r.status);ImGui::TreePop();}
   if(!s.unverified_source_peers.empty())ImGui::TextWrapped("The named Turing SLCG request also changes %u source peers whose prior/release values cannot be verified by this READ32 witness.",unsigned(s.unverified_source_peers.size()));
  }else{if(s.target_generation)ImGui::Text("Configured target: Gen%u; negotiated GPU/root: Gen%u / Gen%u; width x%u / x%u",*s.target_generation,s.generation.value_or(0),s.root_generation.value_or(0),s.width.value_or(0),s.root_width.value_or(0));
   ImGui::BeginDisabled(busy||!unlocked||!s.support||s.stale||s.uncertain);ImGui::SetNextItemWidth(100);if(ImGui::InputScalar("Target generation",ImGuiDataType_U32,&state.pcie_generation))state.pcie_dirty=true;
   if(ImGui::Button("Apply PCIe generation"))out.push_back({ClockGatingIntent::Operation::apply,0,family,{{family,ClockGatingMode::released,state.pcie_generation,state.pcie_binding,state.pcie_baseline}},{}});ImGui::EndDisabled();ImGui::SameLine();if(ImGui::Button("Use current target")){state.pcie_dirty=false;state.pcie_generation=s.target_generation.value_or(1);state.pcie_baseline=s.target_generation;state.pcie_binding=s.binding;}
   ImGui::BeginDisabled(busy||!unlocked||!s.initial_generation);if(ImGui::Button("Restore captured initial target"))out.push_back({ClockGatingIntent::Operation::restore_initial,0,family,{},{}});ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!s.uncertain);if(ImGui::Button("Recover prior PCIe target"))out.push_back({ClockGatingIntent::Operation::recover,0,family,{},{}});ImGui::EndDisabled();
  }
  if(!s.verification_scope.empty())ImGui::TextWrapped("%s",s.verification_scope.c_str());if(!s.message.empty())ImGui::TextWrapped("%s",s.message.c_str());ImGui::PopID();}
 if(shared)ImGui::TextWrapped("Undo reverses this session's last clock gating or PCIe request, whichever family it was in. Apply a staged profile under each family.");
 else ImGui::Separator();
 ImGui::BeginDisabled(busy);if(!shared&&ImGui::Button("Save selected clock profile...")){ClockGatingIntent intent;intent.operation=ClockGatingIntent::Operation::save_profile;for(size_t i=0;i<4;++i)if(state.included[i]&&shown(ClockGatingFamily(i)))intent.selection.push_back(ClockGatingFamily(i));out.push_back(std::move(intent));}if(all)ImGui::SameLine();if(!in_family&&ImGui::Button("Load clock profile..."))out.push_back({ClockGatingIntent::Operation::load_profile,0,{},{},{}});ImGui::EndDisabled();
 // Classic: each family's Save writes only that family, as 1.9.1 does; a profile holding
 // several families stays available here, folded away under the native-only subsection.
 if(shared&&ImGui::TreeNode("Save a selection of families")){
  constexpr const char* names[]={"Live engine clock gating","Live block clock gating","Live second-level clock gating (SLCG)","Manual PCIe speed request"};
  for(size_t i=0;i<4;++i)ImGui::Checkbox(names[i],&state.included[i]);
  ImGui::BeginDisabled(busy||!data||std::none_of(state.included.begin(),state.included.end(),[](bool b){return b;}));
  if(ImGui::Button("Save selected clock profile...")){ClockGatingIntent intent;intent.operation=ClockGatingIntent::Operation::save_profile;for(size_t i=0;i<4;++i)if(state.included[i])intent.selection.push_back(ClockGatingFamily(i));out.push_back(std::move(intent));}
  ImGui::EndDisabled();ImGui::TreePop();}
 std::vector<ClockGatingRequest> staged;for(const auto& r:state.staged)if(shown(r.family))staged.push_back(r);
 if(!staged.empty()){ImGui::Text("Staged: %u explicit family requests",unsigned(staged.size()));for(const auto& r:staged)if(r.family==ClockGatingFamily::pcie)ImGui::Text("PCIe target: Gen%u",r.generation);else ImGui::Text("Family %u: %s",unsigned(r.family),r.mode==ClockGatingMode::released?"release my object":r.mode==ClockGatingMode::enabled?"own ENABLE":"own DISABLE");if(!shared){ImGui::BeginDisabled(busy||!unlocked);if(ImGui::Button(in_family?"Apply staged request":"Apply staged clock profile"))out.push_back({ClockGatingIntent::Operation::apply,0,in_family?*only:ClockGatingFamily::engine,staged,{}});ImGui::EndDisabled();}}
 if(!in_family){ImGui::BeginDisabled(busy||!unlocked||!data||!data->undo_available);if(ImGui::Button("Undo clock / PCIe requests")){out.push_back({ClockGatingIntent::Operation::undo,0,{},{},{}});state.last={};}ImGui::EndDisabled();}if(!unlocked)ImGui::TextDisabled("Unlock for requests or release. Reading and staging remain available.");if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());ImGui::TextDisabled("Session only. No startup or post-reset replay; no raw register writes.");if(!all)ImGui::PopID();ImGui::PopID();return out;
}
}
namespace {
void readback_box(const std::string& text,float height){
 ImGui::InputTextMultiline("##readback",const_cast<char*>(text.c_str()),text.size()+1,ImVec2(-FLT_MIN,classic_px(height)),ImGuiInputTextFlags_ReadOnly);}
std::string hex64(uint64_t value){char text[32];std::snprintf(text,sizeof(text),"0x%016llX",static_cast<unsigned long long>(value));return text;}
bool read_ok(const ClockGatingState& s){return !s.binding.empty()&&s.availability==VoltagePolicyAvailability::available&&!s.stale;}
}
// engine_clock_gating_panel.py, block_clock_gating_panel.py, second_level_clock_gating_panel.py
// and pcie_speed_panel.py _format, from the fields the service reads. Native detail Python
// does not print (engine masks, register qualification, captured initial target, stale note)
// follows the released lines. Empty until a read, as Python's box is.
std::string clock_gating_readback(const ClockGatingState& s,ClockGatingFamily family){
 std::string lines;const auto add=[&](const std::string& text){lines+=(lines.empty()?"":"\n")+text;};
 if(family==ClockGatingFamily::pcie){
  const auto gen=[](const std::optional<uint32_t>& v){return v?"Gen "+std::to_string(*v):std::string("unknown");};
  const auto width=[](const std::optional<uint32_t>& v){return v?"x"+std::to_string(*v):std::string("unknown");};
  if(s.target_generation||s.generation){add("Programmed target: "+gen(s.target_generation));
   add("GPU link: "+gen(s.generation)+", "+width(s.width)+"; root link: "+gen(s.root_generation)+", "+width(s.root_width));
   add("Endpoint maximum: GPU "+gen(s.gpu_max)+"; root "+gen(s.root_max));
   if(s.initial_generation)add("Captured initial target: "+gen(s.initial_generation));}
  else if(!s.support&&!s.message.empty()&&s.availability!=VoltagePolicyAvailability::unknown)add("Unavailable: "+s.message);
 }else if(s.binding.empty()){
  // A layout the service does not understand reads as unsupported, as Python's support=False.
  if(s.availability==VoltagePolicyAvailability::unsupported)add("Request readback: unavailable");
 }else{
  const bool engine=family==ClockGatingFamily::engine,block=family==ClockGatingFamily::block;
  std::vector<const ClockGatingRegister*> ready,missing;for(const auto& r:s.registers)(r.available?ready:missing).push_back(&r);
  const bool partial=family==ClockGatingFamily::second_level&&s.support&&(!missing.empty()||s.layout=="tu10x");
  add(std::string("Request readback: ")+(partial?"partial":s.support?"available":"unavailable"));
  // The panels add "; use Release"; here only while this family's Release is enabled.
  if(s.uncertain)add(s.can_release?"This instance's request outcome is uncertain; use Release.":"This instance's request outcome is uncertain.");
  else if(block)add(std::string("This instance's disable request: ")+(s.owned?"held":"not held"));
  else add(std::string("This instance's request: ")+(!s.owned?"not held":s.mode==ClockGatingMode::enabled?"held enabled":s.mode==ClockGatingMode::disabled?"held disabled":"held (mode unknown)"));
  const auto address=[](uint32_t a){char text[24];std::snprintf(text,sizeof(text),"0x%x",a);return std::string(text);};
  const auto value=[](uint32_t v){char text[16];std::snprintf(text,sizeof(text),"0x%08X",v);return std::string(text);};
  if(engine){
   const auto count=[](uint64_t mask){unsigned n=0;for(;mask;mask&=mask-1)++n;return n;};
   add("Engines with no recorded disable reason: "+(s.eligible_engines?std::to_string(count(*s.eligible_engines)):std::string("unknown")));
   std::string bits;for(uint32_t bit=0;bit<64;++bit)if(s.observed_engines&(uint64_t{1}<<bit))bits+=(bits.empty()?"":", ")+std::to_string(bit);
   if(!bits.empty())add("Observed changes on engine bits: "+bits+" (partial readback)");
   if(s.arbitration_verified)add("Conflicting request rejection verified for this enabled hold.");
   const bool available=s.availability==VoltagePolicyAvailability::available;
   const bool enable_eligible=available&&s.supported_engines&&s.eligible_engines&&*s.supported_engines&&*s.eligible_engines==*s.supported_engines;
   if(s.support&&!s.owned&&available&&s.supported_engines&&s.eligible_engines&&*s.eligible_engines!=*s.supported_engines)add("Other disable reasons are active; release those before holding enabled.");
   else if(s.support&&!s.owned&&!enable_eligible)add("Complete engine eligibility readback is required before holding enabled.");
   if(s.supported_engines)add("Supported engine mask: "+hex64(*s.supported_engines));
   if(s.eligible_engines)add("Eligible engine mask: "+hex64(*s.eligible_engines));
  }else{
   for(const auto* r:ready)add("Register "+address(r->address)+": "+value(r->value));
   // second_level_clock_gating_panel.py lists unavailable registers; the block panel does
   // not, so a block register's missing readback is only in the native lines at the end.
   if(!block)for(const auto* r:missing)add("Register "+address(r->address)+": unavailable");
   if(block){
    // block_clock_gating_panel.py: no readback bit the disable request clears.
    if(s.support&&!s.owned&&std::none_of(ready.begin(),ready.end(),[](const auto* r){return (r->value&r->disabled_value)!=0;}))add("No register bits are available to verify a new request.");
    if(s.observed_registers)add("Registers with observed changes: "+std::to_string(s.observed_registers)+" (partial readback)");
   }else{
    if(!s.unverified_source_peers.empty()){add("Other source-table registers: "+std::to_string(s.unverified_source_peers.size())+" unverified.");
     add("The named request also changes those registers; their prior values are not verified.");}
    const bool any=!ready.empty();
    const bool enable_ok=any&&std::all_of(ready.begin(),ready.end(),[](const auto* r){return r->value==r->release_value||r->value==r->disabled_value;});
    const bool base_ok=any&&std::all_of(ready.begin(),ready.end(),[](const auto* r){return r->value==r->release_value;});
    if(s.support&&!s.owned&&!enable_ok&&!base_ok)add("Readbacks do not match the required starting values; a new request cannot be verified.");
    else if(s.support&&!s.owned&&!base_ok)add("Disabled hold needs the source release baseline; enabled hold may be available.");
    if(s.observed_registers)add("Registers with observed changes: "+std::to_string(s.observed_registers)+" (partial readback)");
    if(s.arbitration_verified)add("Conflicting request rejection verified for this enabled hold.");
   }
   for(const auto* r:missing)add("Register "+address(r->address)+" readback: "+r->qualification+", status "+std::to_string(r->status));
  }
 }
 // PCIe marks a kept readback with a heading instead (clock_gating_readback_box).
 if(s.stale&&!lines.empty()&&family!=ClockGatingFamily::pcie)add("Last successful read; stale.");
 return lines;
}
// pcie_speed_panel.py _failed / request: after a failed read or request the old box gets
// "Historical readback (refresh required):" above it; after a request the readback did
// not verify, "Historical or unverified readback (refresh required):". Other families'
// boxes are clock_gating_readback's.
std::string clock_gating_readback_box(const ClockGatingViewState& state,const ClockGatingData* data,ClockGatingFamily family){
 if(!data)return {};
 const auto& s=data->families[static_cast<size_t>(family)];
 auto box=clock_gating_readback(s,family);
 if(family!=ClockGatingFamily::pcie||box.empty())return box;
 using Kind=ClockGatingViewState::LastAction::Kind;
 const auto& last=state.last[static_cast<size_t>(family)];
 const bool published=last&&data->revision!=last->revision;
 if(s.stale)return "Historical readback (refresh required):\n"+box;
 if(published&&last->kind==Kind::request&&!last->matched
  &&!(s.target_generation==last->generation&&s.generation==last->generation&&s.root_generation==last->generation))
  // A request that failed before anything was sent (no prior target retained) is _failed's.
  return (!s.uncertain&&last->outcome&&!last->outcome->empty()?"Historical readback (refresh required):\n":"Historical or unverified readback (refresh required):\n")+box;
 return box;
}
std::string clock_gating_status(const ClockGatingViewState& state,const ClockGatingData* data,ClockGatingFamily family){
 using Kind=ClockGatingViewState::LastAction::Kind;
 const bool pcie=family==ClockGatingFamily::pcie;
 const std::string prompt=pcie?"Refresh status before requesting a speed.":"Refresh status before requesting a change.";
 const ClockGatingState* s=data?&data->families[static_cast<size_t>(family)]:nullptr;
 if(!s||(s->binding.empty()&&s->availability==VoltagePolicyAvailability::unknown&&s->message.empty()))return prompt;
 const auto& last=state.last[static_cast<size_t>(family)];
 // Until a later readback is published, the line still describes the previous one.
 const bool published=last&&data->revision!=last->revision;
 const std::optional<Kind> op=last?(published?std::optional<Kind>(last->kind):last->prior):std::nullopt;
 // The panels' _failed: "<family> <operation> failed: <reason>. <action>". Release is
 // suggested only while this view's Release is enabled for the family.
 const auto failed=[&](Kind kind,const std::string& reason){
  const char* title=family==ClockGatingFamily::engine?"Engine clock gating":family==ClockGatingFamily::block?"Block clock gating"
   :family==ClockGatingFamily::second_level?"Second-level clock gating":"PCIe speed";
  const char* operation=kind==Kind::read?"refresh":kind==Kind::hold_disabled?"hold":kind==Kind::hold_enabled?"hold enabled":kind==Kind::release?"release":"request";
  const char* action=pcie?"Refresh before requesting a speed.":kind==Kind::read||!s->can_release?"Refresh status.":"Use Release or refresh status.";
  return std::string(title)+" "+operation+" failed: "+reason+". "+action;};
 // A failed read or request leaves its reason on the family. Without an operation this
 // view sent (Undo, Recover, Restore captured initial), the reason alone.
 if(s->availability!=VoltagePolicyAvailability::available&&!s->message.empty())return op?failed(*op,s->message):s->message;
 // An unsupported PCIe readback is the status the read returned, not a failure.
 if(!read_ok(*s)||(pcie&&!s->support))return s->message.empty()?prompt:s->message;
 const char* refreshed=family==ClockGatingFamily::engine?"Engine clock-gating status refreshed.":family==ClockGatingFamily::block?"Block clock-gating status refreshed."
  :family==ClockGatingFamily::second_level?"Second-level clock-gating status refreshed.":"Status refreshed.";
 if(!last||last->kind==Kind::read||!published)return refreshed;
 switch(last->kind){
 case Kind::hold_disabled:if(s->owned&&s->mode==ClockGatingMode::disabled)return "Disable request held by this instance.";break;
 case Kind::hold_enabled:if(s->owned&&s->mode==ClockGatingMode::enabled)return "Enable request held by this instance.";break;
 case Kind::release:if(!s->owned&&!s->uncertain)return family==ClockGatingFamily::block?"This instance's disable request was released.":"This instance's request was released.";break;
 case Kind::request:{const auto g="Gen "+std::to_string(last->generation);
  if(last->matched)return g+" already matches the readback; nothing written.";
  if(s->target_generation==last->generation&&s->generation==last->generation&&s->root_generation==last->generation)return "Requested "+g+"; target and link readback verified.";
  // pcie_speed_panel.py _request: a request that raised before anything was sent is _failed's
  // "PCIe speed request failed: ..."; the service then keeps no prior target (not uncertain).
  // One that was sent but not verified keeps that prior target for Undo, and is unverified.
  if(!s->uncertain&&last->outcome&&!last->outcome->empty())return failed(Kind::request,*last->outcome);
  return "Requested "+g+"; target and link change was not verified. Refresh to check.";}
 case Kind::read:break;
 }
 // A hold or release the readback does not confirm failed: its own result says why.
 if(last->outcome&&!last->outcome->empty())return failed(last->kind,*last->outcome);
 return s->uncertain&&!s->message.empty()?s->message:refreshed;
}
namespace {
// Released 1.9.1 engine_clock_gating_panel.py, block_clock_gating_panel.py,
// second_level_clock_gating_panel.py and pcie_speed_panel.py: explainer, one button
// row, a status line and a framed readback. Every button sends the request the
// native family buttons sent; PCIe chooses among the generations the adapter allows.
std::vector<ClockGatingIntent> draw_clock_gating_family(ClockGatingViewState& state,const ClockGatingData* data,bool busy,bool unlocked,ClockGatingFamily family){
 std::vector<ClockGatingIntent> out;ImGui::PushID("clock-gating");ImGui::PushID(100+static_cast<int>(family));
 const size_t i=static_cast<size_t>(family);const ClockGatingState* s=data?&data->families[i]:nullptr;
 const bool pcie=family==ClockGatingFamily::pcie;const float wrap=classic_px(900);
 ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
 switch(family){
 case ClockGatingFamily::engine:classic_text("Hold enabled or disabled requests that state for engine clock gating in this session. "
  "An enabled request rejects conflicting profiler requests; other driver mechanisms can still interfere. "
  "Mask readback is a partial check and does not guarantee physical gating or residency. The request is not automatically reapplied after a driver restart.");break;
 case ClockGatingFamily::block:classic_text("Hold requests that the driver disable block clock gating. "
  "Register readback is a partial check, not physical gating or residency. The request is not automatically reapplied after a driver restart.");break;
 case ClockGatingFamily::second_level:classic_text("Hold enabled or disabled requests that state for second-level clock gating in this session. "
  "An enabled request rejects conflicting profiler requests; other driver mechanisms can still interfere. "
  "Register readback is partial and does not guarantee physical gating or residency. The request is not automatically reapplied after a driver restart.");break;
 case ClockGatingFamily::pcie:classic_text("Request a PCIe generation and retrain the link. This is not a permanent speed lock; "
  "the driver may change the link later. Refresh to see current readback.");break;
 }
 if(!pcie)classic_text("A saved profile describes only this instance's request. Load it manually to apply; requests are not replayed after a driver restart.");
 ImGui::PopTextWrapPos();
 using Kind=ClockGatingViewState::LastAction::Kind;
 // Each action also drops the host's note about the previous one.
 const auto remember=[&](Kind kind,uint32_t generation=0,bool matched=false){const auto prior=state.last[i]?std::optional<Kind>(state.last[i]->kind):std::nullopt;
  state.last[i]=ClockGatingViewState::LastAction{kind,data?data->revision:0,generation,matched,prior,{}};state.status.clear();};
 // Recover and Restore are not the panels' operations: a failure after them shows its reason alone.
 const auto forget=[&]{state.last[i].reset();state.status.clear();};
 // The first revision published after an action carries that action's result, which the
 // status line reports in place of the host's "queued" note.
 if(data)for(auto& l:state.last)if(l&&!l->outcome&&data->revision!=l->revision){l->outcome=data->message;state.status.clear();}
 ImGui::BeginDisabled(busy);if(ImGui::Button("Refresh")){out.push_back({ClockGatingIntent::Operation::read,uint32_t(1u<<i),family,{},{}});remember(Kind::read);}ImGui::EndDisabled();
 ImGui::SameLine();ImGui::BeginDisabled(busy||!data);
 if(ImGui::Button(pcie?"Save PCIe speed profile":"Save request profile")){ClockGatingIntent intent;intent.operation=ClockGatingIntent::Operation::save_profile;intent.selection.push_back(family);out.push_back(std::move(intent));}
 ImGui::EndDisabled();
 if(!pcie){
  ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!s||!s->can_disable);
  if(ImGui::Button("Hold disabled")){out.push_back({ClockGatingIntent::Operation::apply,0,family,{{family,ClockGatingMode::disabled,0,s->binding,{}}},{}});remember(Kind::hold_disabled);}ImGui::EndDisabled();
  if(family!=ClockGatingFamily::block){ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!s||!s->can_enable);
   if(ImGui::Button("Hold enabled")){out.push_back({ClockGatingIntent::Operation::apply,0,family,{{family,ClockGatingMode::enabled,0,s->binding,{}}},{}});remember(Kind::hold_enabled);}ImGui::EndDisabled();}
  ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!s||!s->can_release);
  if(ImGui::Button("Release")){out.push_back({ClockGatingIntent::Operation::release,0,family,{},{}});remember(Kind::release);}
  ImGui::SameLine();if(ImGui::Button("Recover / free owned object")){out.push_back({ClockGatingIntent::Operation::recover,0,family,{},{}});forget();}ImGui::EndDisabled();
 }else{
  std::vector<uint32_t> allowed=s?s->allowed_generations:std::vector<uint32_t>{};
  const bool can=!busy&&unlocked&&s&&s->support&&!s->stale&&!s->uncertain;
  // A request becomes "previous" only once a later readback shows the target and both
  // links at the requested generation, as pcie_speed_panel.py keeps it after a verified
  // request; any other outcome forgets it.
  if(s&&state.pcie_pending&&data->revision!=state.pcie_pending->revision){const auto p=*state.pcie_pending;state.pcie_pending.reset();
   if(!s->stale&&!s->uncertain&&s->target_generation==p.to&&s->generation==p.to&&s->root_generation==p.to)state.pcie_previous=p.from;else state.pcie_previous.reset();}
  const auto request=[&](uint32_t generation,const std::string& binding,const std::optional<uint32_t>& expected){
   out.push_back({ClockGatingIntent::Operation::apply,0,family,{{family,ClockGatingMode::released,generation,binding,expected}},{}});
   remember(Kind::request,generation,s->target_generation==generation&&s->generation==generation&&s->root_generation==generation);
   if(s->target_generation&&*s->target_generation!=generation)state.pcie_pending=ClockGatingViewState::PciePending{data->revision,*s->target_generation,generation};};
  char preview[16]="";if(s&&std::find(allowed.begin(),allowed.end(),state.pcie_generation)!=allowed.end())std::snprintf(preview,sizeof(preview),"Gen %u",state.pcie_generation);
  ImGui::SameLine();ImGui::SetNextItemWidth(classic_px(120));ImGui::BeginDisabled(!can||allowed.empty());
  if(ImGui::BeginCombo("##generation",preview)){for(uint32_t g:allowed){char name[16];std::snprintf(name,sizeof(name),"Gen %u",g);
    if(ImGui::Selectable(name,g==state.pcie_generation)){state.pcie_generation=g;state.pcie_dirty=true;}}ImGui::EndCombo();}
  ImGui::EndDisabled();
  ImGui::SameLine();ImGui::BeginDisabled(!can);
  if(ImGui::Button("Request speed"))request(state.pcie_generation,state.pcie_binding,state.pcie_baseline);
  ImGui::EndDisabled();
  // The same apply as Request speed, with the target the last verified request replaced.
  const auto previous=state.pcie_previous;
  ImGui::SameLine();ImGui::BeginDisabled(!can||!previous||std::find(allowed.begin(),allowed.end(),previous.value_or(0))==allowed.end());
  if(ImGui::Button("Request previous speed")&&previous)request(*previous,s->binding,s->target_generation);
  ImGui::EndDisabled();
  ImGui::BeginDisabled(busy||!unlocked||!s||!s->initial_generation);if(ImGui::Button("Restore captured initial target")){out.push_back({ClockGatingIntent::Operation::restore_initial,0,family,{},{}});forget();}ImGui::EndDisabled();
  ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!s||!s->uncertain);if(ImGui::Button("Recover prior PCIe target")){out.push_back({ClockGatingIntent::Operation::recover,0,family,{},{}});forget();}ImGui::EndDisabled();
  ImGui::SameLine();ImGui::BeginDisabled(!s);if(ImGui::Button("Use current target")&&s){state.pcie_dirty=false;state.pcie_generation=s->target_generation.value_or(1);state.pcie_baseline=s->target_generation;state.pcie_binding=s->binding;}ImGui::EndDisabled();
 }
 // The panels' status line, then the readback box. The status line already carries a
 // failure's reason; the service's verification scope is the box's tooltip; the service's
 // own account of the last operation is shown under Undo, recovery and profiles.
 ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
 classic_text(clock_gating_status(state,data,family).c_str());
 ImGui::PopTextWrapPos();
 readback_box(clock_gating_readback_box(state,data,family),pcie?110.0f:100.0f);
 if(s&&!s->verification_scope.empty()&&!s->binding.empty()&&ImGui::IsItemHovered())ImGui::SetTooltip("%s",s->verification_scope.c_str());
 ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
 if(!unlocked)ImGui::TextDisabled("Unlock for requests or release. Reading and staging remain available.");
 if(!state.status.empty())ImGui::TextUnformatted(state.status.c_str());
 ImGui::PopTextWrapPos();
 std::vector<ClockGatingRequest> staged;for(const auto& r:state.staged)if(r.family==family)staged.push_back(r);
 if(!staged.empty()){for(const auto& r:staged)if(pcie)ImGui::Text("Staged PCIe target: Gen %u",r.generation);else ImGui::Text("Staged: %s",r.mode==ClockGatingMode::released?"release my object":r.mode==ClockGatingMode::enabled?"hold enabled":"hold disabled");
  ImGui::BeginDisabled(busy||!unlocked);if(ImGui::Button("Apply staged request")){out.push_back({ClockGatingIntent::Operation::apply,0,family,staged,{}});
   const auto& r=staged.back();
   if(pcie)remember(Kind::request,r.generation,s&&s->target_generation==r.generation&&s->generation==r.generation&&s->root_generation==r.generation);
   else remember(r.mode==ClockGatingMode::released?Kind::release:r.mode==ClockGatingMode::enabled?Kind::hold_enabled:Kind::hold_disabled);}
  ImGui::EndDisabled();}
 ImGui::PopID();ImGui::PopID();return out;
}
}
std::vector<ClockGatingIntent> draw_clock_gating_controls(ClockGatingViewState& state,const ClockGatingData* data,uint64_t generation,bool busy,bool unlocked,bool classic,std::optional<ClockGatingFamily> only){(void)classic;
 if(only){sync_clock_gating_view(state,data,generation);return draw_clock_gating_family(state,data,busy,unlocked,*only);}
 return draw_clock_gating(state,data,generation,busy,unlocked,Scope::all,only);}
std::vector<ClockGatingIntent> draw_clock_gating_shared_controls(ClockGatingViewState& state,const ClockGatingData* data,uint64_t generation,bool busy,bool unlocked){return draw_clock_gating(state,data,generation,busy,unlocked,Scope::shared,{});}
}
