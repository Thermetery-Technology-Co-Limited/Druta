// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_limits_view.h"
#include "voltage_tables_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
#include <algorithm>
#include <cfloat>
#include <cstdio>
namespace druta::ui {
void sync_voltage_limits_view(VoltageLimitsViewState& state,const VoltageLimitsData* data,uint64_t generation){
 if(!state.initialized||state.generation!=generation){state={};state.initialized=true;state.generation=generation;}
 if(!data)return;
 // Classic: the first revision published after an action carries that action's result,
 // which replaces the host's "queued" note for it.
 for(auto& last:state.last)if(last&&!last->outcome&&data->revision!=last->revision){last->outcome=data->message;state.status.clear();}
 for(const auto& row:data->rows){auto found=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});
  if(found==state.drafts.end()){VoltageLimitsDraft draft;draft.key=row.key;state.drafts.push_back(draft);found=state.drafts.end()-1;}
  if(!found->dirty){found->value=row.value.value_or(0);found->lower=row.lower.value_or(0);found->binding=row.binding;found->expected_value=row.value;found->expected_lower=row.lower;
   const bool delay=row.key.kind==VoltageLimitKind::switch_delay;
   std::snprintf(found->value_text.data(),found->value_text.size(),"%s",row.value?(delay?std::to_string(*row.value):voltage_limits_mv_text(*row.value)).c_str():"");
   std::snprintf(found->lower_text.data(),found->lower_text.size(),"%s",row.lower?voltage_limits_mv_text(*row.lower).c_str():"");
   found->value_invalid=found->lower_invalid=false;}
 }
}
std::string voltage_limits_mv_text(int64_t value){
 const uint64_t magnitude=value<0?static_cast<uint64_t>(-(value+1))+1:static_cast<uint64_t>(value);
 std::string text=(value<0?"-":"")+std::to_string(magnitude/1000);
 if(const auto fraction=magnitude%1000){char digits[8];std::snprintf(digits,sizeof(digits),".%03u",static_cast<unsigned>(fraction));text+=digits;while(text.back()=='0')text.pop_back();}
 return text;
}
bool stage_voltage_limits_document(VoltageLimitsViewState& state,const ProfileJson& doc,const VoltageLimitsData& data){try{auto staged=stage_voltage_limits_profile(doc,data);state.staged=std::move(staged);state.status="Profile staged only. Review selected requests, then Apply explicitly.";return true;}catch(const std::exception& e){state.staged.clear();state.status=e.what();return false;}}
bool voltage_limits_part_has(VoltageLimitsPart part,VoltageLimitKind kind){
 switch(part){
 case VoltageLimitsPart::all:return true;
 case VoltageLimitsPart::rel:return kind==VoltageLimitKind::rel;
 case VoltageLimitsPart::constants:return kind==VoltageLimitKind::alt||kind==VoltageLimitKind::ov||kind==VoltageLimitKind::vmin;
 case VoltageLimitsPart::switch_delay:return kind==VoltageLimitKind::switch_delay;
 }
 return false;
}
namespace {
// Undo, Recover and Load span REL bounds, constants and switch delays, so classic
// draws them once in a shared subsection; each part applies only its own staged rows.
std::vector<VoltageLimitsIntent> draw_voltage_limits(VoltageLimitsViewState& state,const VoltageLimitsData* data,uint64_t generation,bool busy,bool unlocked,VoltageLimitsPart part,bool shared){
 sync_voltage_limits_view(state,data,generation);std::vector<VoltageLimitsIntent> out;ImGui::PushID("voltage-limits");if(shared)ImGui::PushID(200);else if(part!=VoltageLimitsPart::all)ImGui::PushID(static_cast<int>(part));
 const bool all=!shared&&part==VoltageLimitsPart::all,in_part=!shared&&!all;
 if(shared)ImGui::TextWrapped("Undo and Recover act on this session's last REL bound, constant or switch-delay request, whichever subsection it was in. Apply a staged profile under each subsection.");
 else {
 const bool equations=part!=VoltageLimitsPart::switch_delay,devices=part==VoltageLimitsPart::all||part==VoltageLimitsPart::switch_delay;
 if(all)ImGui::TextUnformatted("Loaded voltage bounds, constants & switch delays");
 ImGui::TextWrapped("Equation definitions and switch-delay requests apply to this driver session. Reported policy is an independent observation; physical voltage and switching latency are not measured.");
 ImGui::BeginDisabled(busy);if(equations&&ImGui::Button("Read voltage definitions"))out.push_back({VoltageLimitsIntent::Operation::read,1,{},{}});if(equations&&devices)ImGui::SameLine();if(devices&&ImGui::Button("Read voltage device delays"))out.push_back({VoltageLimitsIntent::Operation::read,2,{},{}});ImGui::EndDisabled();
 if(data){if(equations)ImGui::TextWrapped("%s",data->equations_message.c_str());if(devices)ImGui::TextWrapped("%s",data->devices_message.c_str());
  for(size_t i=0;i<data->rows.size();++i){const auto& row=data->rows[i];if(!voltage_limits_part_has(part,row.key.kind))continue;auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(draft==state.drafts.end())continue;ImGui::PushID(static_cast<int>(i));ImGui::Separator();ImGui::TextWrapped("%s",row.label.c_str());
   if(row.key.kind==VoltageLimitKind::switch_delay)ImGui::TextDisabled("Device %u, domain %u, operation %u",row.key.index,row.voltage_domain,row.operation_type);
   if(row.value)ImGui::Text("Loaded: %lld %s%s",static_cast<long long>(*row.value),row.key.kind==VoltageLimitKind::switch_delay?"us":"uV",row.stale?" (stale)":"");else ImGui::TextUnformatted("Loaded: unavailable");
   if(row.lower)ImGui::Text("Loaded lower bound: %lld uV",static_cast<long long>(*row.lower));if(row.reported)ImGui::Text("Independently reported policy: %.3f mV",double(*row.reported)/1000.0);
   ImGui::Checkbox("Include in saved profile",&draft->include);
   const bool ready=!busy&&unlocked&&row.writable&&row.value&&!(data->recovery_pending);
   ImGui::BeginDisabled(!ready);ImGui::SetNextItemWidth(180);
   if(ImGui::InputScalar(row.key.kind==VoltageLimitKind::switch_delay?"Delay (us)":"Upper / constant (uV)",ImGuiDataType_S64,&draft->value))draft->dirty=true;
   if(row.key.kind==VoltageLimitKind::rel){ImGui::SetNextItemWidth(180);if(ImGui::InputScalar("Lower (uV)",ImGuiDataType_S64,&draft->lower))draft->dirty=true;}
   const auto request=[&](bool lower){VoltageLimitRequest r;r.key=row.key;r.value=draft->value;if(lower)r.lower=draft->lower;r.binding=draft->binding;r.expected_value=draft->expected_value;r.expected_lower=draft->expected_lower;return r;};
   if(ImGui::Button(row.key.kind==VoltageLimitKind::rel?"Apply upper":"Apply selected"))out.push_back({VoltageLimitsIntent::Operation::apply,0,{request(false)},{}});
   if(row.key.kind==VoltageLimitKind::rel){ImGui::SameLine();if(ImGui::Button("Apply both bounds"))out.push_back({VoltageLimitsIntent::Operation::apply,0,{request(true)},{}});ImGui::SameLine();if(ImGui::Button("Fix both bounds")){auto r=request(true);r.lower=r.value;out.push_back({VoltageLimitsIntent::Operation::apply,0,{r},{}});}}
   ImGui::EndDisabled();ImGui::SameLine();if(ImGui::Button("Use current")){draft->dirty=false;draft->value=row.value.value_or(0);draft->lower=row.lower.value_or(0);draft->binding=row.binding;draft->expected_value=row.value;draft->expected_lower=row.lower;}
   ImGui::BeginDisabled(!ready||!row.initial_value);if(ImGui::Button("Restore captured initial"))out.push_back({VoltageLimitsIntent::Operation::restore_initial,0,{}, {row.key}});ImGui::EndDisabled();
   if(row.key.kind!=VoltageLimitKind::switch_delay){ImGui::SameLine();ImGui::BeginDisabled(!ready||!row.stored_value);if(ImGui::Button("Restore current ROM definition"))out.push_back({VoltageLimitsIntent::Operation::restore_rom,0,{}, {row.key}});ImGui::EndDisabled();}
   if(!row.message.empty())ImGui::TextWrapped("%s",row.message.c_str());if(draft->dirty&&draft->binding!=row.binding)ImGui::TextWrapped("Draft binding changed. Use current before editing this control again.");ImGui::PopID();
  }
  if((part==VoltageLimitsPart::all||part==VoltageLimitsPart::rel)&&!data->definitions.empty()&&ImGui::TreeNode("Loaded VFE definitions (read-only ordinals)")){if(data->definitions_stale)ImGui::TextWrapped("Stale: these are retained last-good definitions; current equation capture failed.");ImGui::TextWrapped("Ordinals and numeric types do not identify a semantic REL/ALT/OV/VMIN role. Current ROM graph and independent policy receipts are required before semantic editing.");
   if(ImGui::BeginTable("definition-inventory",5,ImGuiTableFlags_ScrollY|ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg,{0,210})){for(auto name:{"Index","Type / next","Lower uV","Upper uV","Qualification"})ImGui::TableSetupColumn(name);ImGui::TableHeadersRow();for(const auto& d:data->definitions){ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::Text("%u",d.index);ImGui::TableNextColumn();ImGui::Text("%u / %u",d.type,d.next);ImGui::TableNextColumn();if(d.lower)ImGui::Text("%lld",static_cast<long long>(*d.lower));ImGui::TableNextColumn();if(d.value)ImGui::Text("%lld",static_cast<long long>(*d.value));ImGui::TableNextColumn();ImGui::TextWrapped("%s",d.message.c_str());}ImGui::EndTable();}ImGui::TreePop();}
 }
 if(in_part&&data&&data->recovery_pending)ImGui::TextWrapped("A voltage limit request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
 ImGui::Separator();
 }
 ImGui::BeginDisabled(busy);if(!shared&&ImGui::Button("Save selected voltage profile...")){VoltageLimitsIntent intent;intent.operation=VoltageLimitsIntent::Operation::save_profile;for(const auto& d:state.drafts)if(d.include&&voltage_limits_part_has(part,d.key.kind))intent.selection.push_back(d.key);out.push_back(std::move(intent));}if(all)ImGui::SameLine();if(!in_part&&ImGui::Button("Load voltage profile..."))out.push_back({VoltageLimitsIntent::Operation::load_profile,0,{},{}});ImGui::EndDisabled();
 // Classic: each subsection's Save writes all its rows, as 1.9.1 does; a profile holding a
 // chosen subset, or rows of several subsections, stays available here, folded away.
 if(shared&&ImGui::TreeNode("Save a selection of requests")){VoltageLimitsIntent intent;intent.operation=VoltageLimitsIntent::Operation::save_profile;
  if(data)for(size_t i=0;i<data->rows.size();++i){const auto& row=data->rows[i];auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(draft==state.drafts.end())continue;
   ImGui::PushID(static_cast<int>(i));ImGui::Checkbox(row.label.c_str(),&draft->include);ImGui::PopID();if(draft->include)intent.selection.push_back(row.key);}
  ImGui::BeginDisabled(busy||intent.selection.empty());if(ImGui::Button("Save selected voltage profile..."))out.push_back(std::move(intent));ImGui::EndDisabled();ImGui::TreePop();}
 std::vector<VoltageLimitRequest> staged;for(const auto& r:state.staged)if(shared||voltage_limits_part_has(part,r.key.kind))staged.push_back(r);
 if(!staged.empty()){ImGui::Text("Staged profile: %u selected requests",unsigned(staged.size()));for(const auto& r:staged){ImGui::Text("Target %u/%u/%u: %lld%s",unsigned(r.key.kind),r.key.rail,r.key.index,static_cast<long long>(r.value),r.key.kind==VoltageLimitKind::switch_delay?" us":" uV");if(r.lower)ImGui::Text("  Lower: %lld uV",static_cast<long long>(*r.lower));}
  if(!shared){ImGui::BeginDisabled(busy||!unlocked||!data||data->recovery_pending);if(ImGui::Button(all?"Apply staged voltage profile":"Apply staged requests"))out.push_back({VoltageLimitsIntent::Operation::apply,0,staged,{}});ImGui::EndDisabled();}}
 if(!in_part){ImGui::BeginDisabled(busy||!unlocked||!data||!data->undo_available||data->recovery_pending);if(ImGui::Button("Undo voltage limits"))out.push_back({VoltageLimitsIntent::Operation::undo,0,{},{}});ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!data||!data->recovery_pending);if(ImGui::Button("Recover selected voltage limit"))out.push_back({VoltageLimitsIntent::Operation::recover,0,{},{}});ImGui::EndDisabled();}
 if(!unlocked)ImGui::TextDisabled("Unlock controls for writes. Inspection and profile staging remain read-only.");if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());if(!all)ImGui::PopID();ImGui::PopID();return out;
}
}
namespace {
std::optional<int64_t> parse_us(const char* text){
 std::string s(text);const auto begin=s.find_first_not_of(" \t"),end=s.find_last_not_of(" \t");if(begin==std::string::npos)return {};
 size_t n=begin;bool negative=false;if(s[n]=='+'||s[n]=='-'){negative=s[n]=='-';++n;}if(n>end)return {};
 int64_t value=0;for(;n<=end;++n){if(s[n]<'0'||s[n]>'9'||value>INT64_MAX/10-9)return {};value=value*10+(s[n]-'0');}return negative?-value:value;
}
void readback_box(const std::string& text,float height){
 ImGui::InputTextMultiline("##readback",const_cast<char*>(text.c_str()),text.size()+1,ImVec2(-FLT_MIN,classic_px(height)),ImGuiInputTextFlags_ReadOnly);}
// Selector names as registry_panel.py builds them: "NVVDD (rail 0)" for REL bounds,
// "NVVDD (rail 0) OV constant" for constants, "1: LOGIC / DEFAULT" for devices. The
// domain and operation names are the voltage_device_runtime.py enum labels.
std::string classic_label(const VoltageLimitRow& row){
 if(row.key.kind==VoltageLimitKind::switch_delay){
  constexpr const char* domains[]={nullptr,"LOGIC","SRAM","MSVDD"};
  constexpr const char* operations[]={nullptr,"DEFAULT","LPWR_STEADY_STATE","LPWR_SLEEP_STATE","IPC_VMIN"};
  const auto name=[](const char* const* names,size_t count,uint32_t value){return value<count&&names[value]?std::string(names[value]):std::to_string(value);};
  return std::to_string(row.key.index)+": "+name(domains,4,row.voltage_domain)+" / "+name(operations,5,row.operation_type);}
 // The service labels rows "<rail> <role>", e.g. "NVVDD OV constant".
 const auto space=row.label.find(' ');
 const std::string rail=row.label.substr(0,space)+" (rail "+std::to_string(row.key.rail)+")";
 if(row.key.kind==VoltageLimitKind::rel||space==std::string::npos)return rail;
 return rail+row.label.substr(space);}
const char* role_name(VoltageLimitKind kind){return kind==VoltageLimitKind::alt?"ALT":kind==VoltageLimitKind::ov?"OV":kind==VoltageLimitKind::vmin?"VMIN":"REL";}
bool row_ready(const VoltageLimitRow& r){return r.value&&!r.stale&&r.availability==VoltagePolicyAvailability::available;}
const char* availability_name(VoltagePolicyAvailability a){switch(a){case VoltagePolicyAvailability::available:return "ready";case VoltagePolicyAvailability::transient:return "transient";
 case VoltagePolicyAvailability::unsupported:return "unsupported";case VoltagePolicyAvailability::invalid:return "invalid";case VoltagePolicyAvailability::unknown:break;}return "unknown";}
// The released per-row statuses: a REL or constant row that is not ready is "unsupported"
// (voltage_rel_live.py / voltage_constants_live.py _summary), a device record "malformed" or
// "unsupported" (voltage_device_runtime.py). A failed read is native-only (Python clears the
// box); its rows keep their last good reading and name the failure kind instead.
const char* row_status(const VoltageLimitRow& r,bool delay){
 if(r.stale||r.availability==VoltagePolicyAvailability::transient)return availability_name(r.availability);
 return delay&&r.availability==VoltagePolicyAvailability::invalid?"malformed":"unsupported";}
std::vector<const VoltageLimitRow*> part_rows(const VoltageLimitsData* data,VoltageLimitsPart part){
 std::vector<const VoltageLimitRow*> rows;if(data)for(const auto& row:data->rows)if(voltage_limits_part_has(part,row.key.kind))rows.push_back(&row);return rows;}
// select_rel_live / select_constants_live / select_voltage_device: one ready row's status.
std::string selected_text(const VoltageLimitRow& r,VoltageLimitsPart part){
 if(part==VoltageLimitsPart::switch_delay)return "Device "+std::to_string(r.key.index)+": "+std::to_string(*r.value)+" us reported request.";
 // The service labels rows "<rail> <role>", e.g. "NVVDD OV constant"; REL shows the rail alone.
 const auto space=r.label.find(' ');const std::string rail=r.label.substr(0,space);
 if(part==VoltageLimitsPart::rel)return rail+": loaded bounds "+voltage_limits_mv_text(r.lower.value_or(0))+" to "+voltage_limits_mv_text(*r.value)+" mV.";
 return r.label+": "+voltage_limits_mv_text(*r.value)+" mV.";}
size_t part_index(VoltageLimitsPart part){return static_cast<size_t>(part);}
}
// registry_panel.py _show_rel_live / _show_constants_live / _show_voltage_devices. A row
// that is not ready prints its status and reason; a last-good reading the service kept
// follows that line, marked as the last successful read.
std::string voltage_limits_inventory(const VoltageLimitsData* data,VoltageLimitsPart part){
 const bool rel=part==VoltageLimitsPart::rel,delay=part==VoltageLimitsPart::switch_delay;
 const auto mv=[](const std::optional<int64_t>& v){return v?voltage_limits_mv_text(*v):std::string("unavailable");};
 const auto body=[&](const VoltageLimitRow& r){
  if(delay)return std::to_string(*r.value)+" us request";
  if(rel){std::string text="loaded bounds "+mv(r.lower)+" to "+mv(r.value)+" mV; stored bounds "+mv(r.stored_lower)+" to "+mv(r.stored_value)+" mV";
   const char* names[]={"reliability","alt_reliability","effective"};const size_t fields[]={0,1,4};
   for(size_t i=0;i<3;++i)text+=std::string(i?", ":"; ")+names[i]+": "+(r.rail_policy[fields[i]]?mv(r.rail_policy[fields[i]])+" mV":std::string("unavailable"));
   return text;}
  std::string text="current "+mv(r.value)+" mV; stored "+mv(r.stored_value)+" mV; reported "+role_name(r.key.kind)+" "+(r.reported?mv(r.reported)+" mV":std::string("unavailable"));
  if(r.key.kind==VoltageLimitKind::vmin&&r.vmin_maximum)text+="; current maximum VMIN input "+mv(r.vmin_maximum)+" mV";
  return text;};
 std::string lines;const auto add=[&](const std::string& text){lines+=(lines.empty()?"":"\n")+text;};
 const auto rows=part_rows(data,part);
 for(const auto* r:rows){
  const std::string label=delay?"Device "+classic_label(*r):classic_label(*r);
  if(row_ready(*r)){add(label+": "+body(*r));continue;}
  const std::string head=delay?"Device "+std::to_string(r->key.index)+", type "+(r->device_type?std::to_string(*r->device_type):std::string("unknown")):classic_label(*r);
  add(head+": "+row_status(*r,delay)+"; "+(r->message.empty()?std::string("no reason reported"):r->message));
  if(r->value)add("  Last successful read: "+body(*r));
 }
 // The box stays empty until this subsection's family has been read, as Python's does.
 if(lines.empty()&&data&&!(delay?data->devices_message:data->equations_message).empty())lines=rel?"No rails returned.":delay?"No voltage devices returned.":"No values returned.";
 return lines;
}
// registry_panel.py select_rel_live / select_constants_live / select_voltage_device: the
// selected row once a read made it ready, "No supported ..." when none is, else the prompt.
std::string voltage_limits_status(const VoltageLimitsData* data,VoltageLimitsPart part,const VoltageLimitRow* selected,bool picked_after_failure){
 const bool rel=part==VoltageLimitsPart::rel,delay=part==VoltageLimitsPart::switch_delay;
 const auto rows=part_rows(data,part);
 const auto row_text=[&](const VoltageLimitRow& r){return selected_text(r,part);};
 // refresh_rel_live / refresh_constants_live / refresh_voltage_devices: a failed Read.
 // A transient failure of an established read keeps the rows, stale; the devices panel
 // keeps nothing.
 const std::string& failure=data?(delay?data->devices_read_error:data->equations_read_error):std::string();
 if(!failure.empty()){
  const bool kept=!delay&&(data->equations_read_retained);
  if(kept&&picked_after_failure&&selected&&selected->value)return "Previous read is stale. "+row_text(*selected);
  return std::string(rel?"Live bound read unavailable: ":delay?"Device delay read unavailable: ":"Live value read unavailable: ")+failure
   +(kept?" Previous read is stale; refresh to verify.":"");}
 // A Read that returned no row of this part: _show_* finds no choice, as for none ready.
 const bool read=data&&!(delay?data->devices_message:data->equations_message).empty();
 if(rows.empty()&&!read)return rel?"Read live bounds before changing them.":delay?"Read devices before changing a delay.":"Read live values before changing them.";
 if(selected&&row_ready(*selected))return row_text(*selected);
 if(std::none_of(rows.begin(),rows.end(),[](const auto* r){return row_ready(*r);}))
  return rel?"No supported live bounds are available.":delay?"No supported switch-delay requests are available. Read again to retry.":"No supported live values are available.";
 return rel?"Read live bounds before changing them.":delay?"Read devices before changing a delay.":"Read live values before changing them.";
}
// _set_rel_live / _set_constants_live / _set_voltage_device_delay: "Applied live. " before the
// control's status, or "<kind> change failed: <reason>". The native result decides which: the
// request held only when its control reads back ready at the requested value.
std::string voltage_limits_request_status(const VoltageLimitsViewState& state,const VoltageLimitsData* data,VoltageLimitsPart part){
 if(part==VoltageLimitsPart::all)return {};
 const auto& last=state.last[part_index(part)];
 if(!last||last->read||!last->outcome)return {};
 if(!last->key)return *last->outcome;  // a staged profile: the service's own account
 const VoltageLimitRow* row=nullptr;if(data)for(const auto& r:data->rows)if(r.key==*last->key)row=&r;
 if(row&&row_ready(*row)&&row->value==last->value&&(!last->lower||row->lower==last->lower))return "Applied live. "+selected_text(*row,part);
 if(last->outcome->empty())return {};
 return std::string(part==VoltageLimitsPart::rel?"Live bound change failed: ":part==VoltageLimitsPart::switch_delay?"Device delay change failed: ":"Live value change failed: ")+*last->outcome;
}
namespace {
// Classic: what a part's controls last sent, with the single control's requested value. The
// shared Undo and Recover act on whichever part was last written, so they reset every part's
// line; a profile save or load is the host's.
void remember(VoltageLimitsViewState& state,const VoltageLimitsData* data,VoltageLimitsPart part,const std::vector<VoltageLimitsIntent>& out){
 using O=VoltageLimitsIntent::Operation;
 for(const auto& intent:out){
  if(intent.operation==O::save_profile||intent.operation==O::load_profile)continue;
  state.status.clear();  // the host's note for the previous action no longer applies
  // Undo / Recover: each part's line goes back to describing its readback once published.
  if(intent.operation==O::undo||intent.operation==O::recover||part==VoltageLimitsPart::all){
   for(auto& l:state.last)l=VoltageLimitsLastAction{true,data?data->revision:0,{},{},{},{}};continue;}
  VoltageLimitsLastAction action{intent.operation==O::read,data?data->revision:0,{},{},{},{}};
  const auto row=[&](const VoltageLimitKey& key)->const VoltageLimitRow*{if(data)for(const auto& r:data->rows)if(r.key==key)return &r;return nullptr;};
  if(intent.operation==O::apply&&intent.requests.size()==1){action.key=intent.requests[0].key;action.value=intent.requests[0].value;action.lower=intent.requests[0].lower;}
  else if((intent.operation==O::restore_initial||intent.operation==O::restore_rom)&&intent.selection.size()==1)
   if(const auto* r=row(intent.selection[0])){const bool initial=intent.operation==O::restore_initial;
    action.key=r->key;action.value=initial?r->initial_value:r->stored_value;action.lower=initial?r->initial_lower:r->stored_lower;}
  state.last[part_index(part)]=action;}
}
}
namespace {
// Released 1.9.1 registry_panel.py build_live_voltage: "Live REL equation bounds",
// "Live voltage constants" and "Voltage switch-delay requests". Explainer, one row
// with Read, Save, a selector and a single value input in mV (us for delays), Apply
// and Restore, then the status line and a framed inventory. Each button sends the
// request the per-row buttons sent; only the presentation and the unit differ.
std::vector<VoltageLimitsIntent> draw_voltage_limits_part(VoltageLimitsViewState& state,const VoltageLimitsData* data,bool busy,bool unlocked,VoltageLimitsPart part){
 std::vector<VoltageLimitsIntent> out;ImGui::PushID("voltage-limits");ImGui::PushID(static_cast<int>(part));
 const bool rel=part==VoltageLimitsPart::rel,delay=part==VoltageLimitsPart::switch_delay;
 const float wrap=classic_px(900);
 ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
 classic_text(rel?"Change the loaded REL equation bounds for this driver session without restarting the GPU. "
   "Fix both bounds uses the entered value for both bounds; linked equations can still affect REL and ALT. "
   "These are equation settings, not measured rail voltage."
  :delay?"Read and change a voltage device's switch-delay request in microseconds. "
   "DEFAULT and IPC_VMIN are separate operations. Changes apply to this driver session "
   "without a GPU restart. Physical switching latency has not been measured; "
   "the separate power-up/down delays are not controlled here."
  :"Change an OV or VMIN constant, or an ALT addend, for this driver session without restarting the GPU. "
   "Reported policy values can differ. Physical enforcement has not been measured.");
 ImGui::PopTextWrapPos();
 std::vector<const VoltageLimitRow*> rows;if(data)for(const auto& row:data->rows)if(voltage_limits_part_has(part,row.key.kind)&&std::any_of(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;}))rows.push_back(&row);
 auto& choice=state.choice[static_cast<size_t>(part)];
 // A new read failure forgets an earlier pick; a pick after it shows the stale row.
 const std::string failure=data?(delay?data->devices_read_error:data->equations_read_error):std::string();
 auto& picked=state.picked_after_failure[static_cast<size_t>(part)];
 if(state.failure_seen[static_cast<size_t>(part)]!=failure){state.failure_seen[static_cast<size_t>(part)]=failure;picked=false;}
 const auto chosen=std::find_if(rows.begin(),rows.end(),[&](const auto* r){return choice&&r->key==*choice;});
 const VoltageLimitRow* row=chosen!=rows.end()?*chosen:rows.empty()?nullptr:rows.front();
 auto draft=row?std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row->key;}):state.drafts.end();
 VoltageLimitsDraft* d=draft!=state.drafts.end()?&*draft:nullptr;
 ImGui::BeginDisabled(busy);if(ImGui::Button(delay?"Read device delays":"Read"))out.push_back({VoltageLimitsIntent::Operation::read,delay?2u:1u,{},{}});ImGui::EndDisabled();
 ImGui::SameLine();ImGui::BeginDisabled(busy||rows.empty());
 if(ImGui::Button(rel?"Save bounds profile":delay?"Save delays profile":"Save constants profile")){VoltageLimitsIntent intent;intent.operation=VoltageLimitsIntent::Operation::save_profile;for(const auto* r:rows)intent.selection.push_back(r->key);out.push_back(std::move(intent));}
 ImGui::EndDisabled();
 ImGui::SameLine();ImGui::SetNextItemWidth(classic_px(rel?220.0f:delay?310.0f:240.0f));ImGui::BeginDisabled(rows.empty());
 if(ImGui::BeginCombo("##select",row?classic_label(*row).c_str():"")){for(size_t i=0;i<rows.size();++i){ImGui::PushID(static_cast<int>(i));if(ImGui::Selectable(classic_label(*rows[i]).c_str(),rows[i]==row)){choice=rows[i]->key;picked=!failure.empty();}ImGui::PopID();}ImGui::EndCombo();}
 ImGui::EndDisabled();
 const bool ready=row&&d&&!busy&&unlocked&&row->writable&&row->value&&!data->recovery_pending;
 static std::array<char,32> empty{};
 ImGui::SameLine();ImGui::SetNextItemWidth(classic_px(130));ImGui::BeginDisabled(!ready);
 if(ImGui::InputText(rel?"Bound value (mV)":delay?"Delay request (us)":"Value (mV)",d?d->value_text.data():empty.data(),d?d->value_text.size():empty.size())&&d){
  d->dirty=true;std::optional<int64_t> parsed;if(delay)parsed=parse_us(d->value_text.data());else if(const auto uv=parse_voltage_table_mv(d->value_text.data()))parsed=*uv;
  d->value_invalid=!parsed;if(parsed)d->value=*parsed;}
 ImGui::EndDisabled();
 if(row&&!row->message.empty()&&ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))ImGui::SetTooltip("%s",row->message.c_str());
 const auto request=[&](bool lower){VoltageLimitRequest r;r.key=row->key;r.value=d->value;if(lower)r.lower=d->lower;r.binding=d->binding;r.expected_value=d->expected_value;r.expected_lower=d->expected_lower;return r;};
 const bool value_ok=d&&!d->value_invalid;
 if(!delay)ImGui::SameLine();
 ImGui::BeginDisabled(!ready||!value_ok);if(ImGui::Button(rel?"Apply upper":delay?"Apply delay now":"Apply now"))out.push_back({VoltageLimitsIntent::Operation::apply,0,{request(false)},{}});ImGui::EndDisabled();
 ImGui::SameLine();ImGui::BeginDisabled(!ready||!row->initial_value);
 if(ImGui::Button(rel?"Restore initial upper":delay?"Restore initial delay":"Restore initial value"))out.push_back({VoltageLimitsIntent::Operation::restore_initial,0,{},{row->key}});ImGui::EndDisabled();
 if(rel){ImGui::BeginDisabled(!ready||!value_ok);if(ImGui::Button("Fix both bounds")){auto r=request(true);r.lower=r.value;out.push_back({VoltageLimitsIntent::Operation::apply,0,{r},{}});}ImGui::EndDisabled();ImGui::SameLine();}
 if(!delay){if(!rel)ImGui::SameLine();ImGui::BeginDisabled(!ready||!row->stored_value);if(ImGui::Button(rel?"Restore BIOS bounds":"Restore BIOS value"))out.push_back({VoltageLimitsIntent::Operation::restore_rom,0,{},{row->key}});ImGui::EndDisabled();}
 if(rel){// The lower bound alone is native only; Python edits it only through Fix both bounds.
  ImGui::SetNextItemWidth(classic_px(130));ImGui::BeginDisabled(!ready);
  if(ImGui::InputText("Lower bound (mV)",d?d->lower_text.data():empty.data(),d?d->lower_text.size():empty.size())&&d){d->dirty=true;const auto uv=parse_voltage_table_mv(d->lower_text.data());d->lower_invalid=!uv;if(uv)d->lower=*uv;}
  ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(!ready||!value_ok||d->lower_invalid);if(ImGui::Button("Apply both bounds"))out.push_back({VoltageLimitsIntent::Operation::apply,0,{request(true)},{}});ImGui::EndDisabled();}
 ImGui::SameLine();ImGui::BeginDisabled(!d);
 if(ImGui::Button("Use current")&&d){d->dirty=false;d->value=row->value.value_or(0);d->lower=row->lower.value_or(0);d->binding=row->binding;d->expected_value=row->value;d->expected_lower=row->lower;}
 ImGui::EndDisabled();
 ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
 // registry_panel.py's single status line: an entry that does not parse (apply_*_live's own
 // sentence), else the last request's result, else the selected control or the read's state.
 // The service's account of the read is the line's tooltip; a row's own reason is the input's.
 const std::string result=voltage_limits_request_status(state,data,part);
 const std::string line=d&&(d->value_invalid||d->lower_invalid)?std::string(delay?"Enter a whole number of microseconds.":"Enter a finite mV value with whole-microvolt precision.")
  :!result.empty()?result:voltage_limits_status(data,part,row,picked);
 classic_text(line);
 if(data&&ImGui::IsItemHovered()){const auto& account=delay?data->devices_message:data->equations_message;if(!account.empty()&&account!=line)ImGui::SetTooltip("%s",account.c_str());}
 if(d&&d->dirty&&d->binding!=row->binding)ImGui::TextUnformatted("Draft binding changed. Use current before editing this control again.");
 if(data&&data->recovery_pending)ImGui::TextUnformatted("A voltage limit request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
 if(!unlocked)ImGui::TextDisabled("Unlock controls for writes. Inspection and profile staging remain read-only.");
 if(!state.status.empty())ImGui::TextUnformatted(state.status.c_str());
 ImGui::PopTextWrapPos();
 std::vector<VoltageLimitRequest> staged;for(const auto& r:state.staged)if(voltage_limits_part_has(part,r.key.kind))staged.push_back(r);
 if(!staged.empty()){ImGui::Text("Staged profile: %u selected requests",unsigned(staged.size()));
  for(const auto& r:staged){const auto label=std::find_if(rows.begin(),rows.end(),[&](const auto* x){return x->key==r.key;});const std::string name=label!=rows.end()?classic_label(**label):"Target "+std::to_string(r.key.rail)+"/"+std::to_string(r.key.index);
   if(delay)ImGui::Text("%s: %lld us",name.c_str(),static_cast<long long>(r.value));
   else ImGui::Text("%s: %s mV%s%s",name.c_str(),voltage_limits_mv_text(r.value).c_str(),r.lower?", lower ":"",r.lower?(voltage_limits_mv_text(*r.lower)+" mV").c_str():"");}
  ImGui::BeginDisabled(busy||!unlocked||!data||data->recovery_pending);if(ImGui::Button("Apply staged requests"))out.push_back({VoltageLimitsIntent::Operation::apply,0,staged,{}});ImGui::EndDisabled();}
 readback_box(voltage_limits_inventory(data,part),rel?110.0f:delay?100.0f:130.0f);
 if(rel&&data&&!data->definitions.empty()&&ImGui::TreeNode("Loaded VFE definitions (read-only ordinals)")){if(data->definitions_stale)ImGui::TextWrapped("Stale: these are retained last-good definitions; current equation capture failed.");ImGui::TextWrapped("Ordinals and numeric types do not identify a semantic REL/ALT/OV/VMIN role. Current ROM graph and independent policy receipts are required before semantic editing.");
  if(ImGui::BeginTable("definition-inventory",5,ImGuiTableFlags_ScrollY|ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg,{0,210})){for(auto name:{"Index","Type / next","Lower uV","Upper uV","Qualification"})ImGui::TableSetupColumn(name);ImGui::TableHeadersRow();for(const auto& def:data->definitions){ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::Text("%u",def.index);ImGui::TableNextColumn();ImGui::Text("%u / %u",def.type,def.next);ImGui::TableNextColumn();if(def.lower)ImGui::Text("%lld",static_cast<long long>(*def.lower));ImGui::TableNextColumn();if(def.value)ImGui::Text("%lld",static_cast<long long>(*def.value));ImGui::TableNextColumn();ImGui::TextWrapped("%s",def.message.c_str());}ImGui::EndTable();}ImGui::TreePop();}
 ImGui::PopID();ImGui::PopID();remember(state,data,part,out);return out;
}
}
std::vector<VoltageLimitsIntent> draw_voltage_limits_controls(VoltageLimitsViewState& state,const VoltageLimitsData* data,uint64_t generation,bool busy,bool unlocked,bool classic,VoltageLimitsPart part){(void)classic;
 if(part!=VoltageLimitsPart::all){sync_voltage_limits_view(state,data,generation);return draw_voltage_limits_part(state,data,busy,unlocked,part);}
 return draw_voltage_limits(state,data,generation,busy,unlocked,part,false);}
std::vector<VoltageLimitsIntent> draw_voltage_limits_shared_controls(VoltageLimitsViewState& state,const VoltageLimitsData* data,uint64_t generation,bool busy,bool unlocked){
 auto out=draw_voltage_limits(state,data,generation,busy,unlocked,VoltageLimitsPart::all,true);remember(state,data,VoltageLimitsPart::all,out);return out;}
}
