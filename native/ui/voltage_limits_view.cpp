// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_limits_view.h"
#include <imgui.h>
#include <algorithm>
namespace druta::ui {
void sync_voltage_limits_view(VoltageLimitsViewState& state,const VoltageLimitsData* data,uint64_t generation){
 if(!state.initialized||state.generation!=generation){state={};state.initialized=true;state.generation=generation;}
 if(!data)return;for(const auto& row:data->rows){auto found=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});
  if(found==state.drafts.end()){VoltageLimitsDraft draft;draft.key=row.key;state.drafts.push_back(draft);found=state.drafts.end()-1;}
  if(!found->dirty){found->value=row.value.value_or(0);found->lower=row.lower.value_or(0);found->binding=row.binding;found->expected_value=row.value;found->expected_lower=row.lower;}
 }
}
bool stage_voltage_limits_document(VoltageLimitsViewState& state,const ProfileJson& doc,const VoltageLimitsData& data){try{auto staged=stage_voltage_limits_profile(doc,data);state.staged=std::move(staged);state.status="Profile staged only. Review selected requests, then Apply explicitly.";return true;}catch(const std::exception& e){state.staged.clear();state.status=e.what();return false;}}
std::vector<VoltageLimitsIntent> draw_voltage_limits_controls(VoltageLimitsViewState& state,const VoltageLimitsData* data,uint64_t generation,bool busy,bool unlocked,bool classic){
 (void)classic;sync_voltage_limits_view(state,data,generation);std::vector<VoltageLimitsIntent> out;ImGui::PushID("voltage-limits");
 ImGui::TextUnformatted("Loaded voltage bounds, constants & switch delays");
 ImGui::TextWrapped("Equation definitions and switch-delay requests apply to this driver session. Reported policy is an independent observation; physical voltage and switching latency are not measured.");
 ImGui::BeginDisabled(busy);if(ImGui::Button("Read voltage definitions"))out.push_back({VoltageLimitsIntent::Operation::read,1,{},{}});ImGui::SameLine();if(ImGui::Button("Read voltage device delays"))out.push_back({VoltageLimitsIntent::Operation::read,2,{},{}});ImGui::EndDisabled();
 if(data){ImGui::TextWrapped("%s",data->equations_message.c_str());ImGui::TextWrapped("%s",data->devices_message.c_str());
  for(size_t i=0;i<data->rows.size();++i){const auto& row=data->rows[i];auto draft=std::find_if(state.drafts.begin(),state.drafts.end(),[&](const auto& d){return d.key==row.key;});if(draft==state.drafts.end())continue;ImGui::PushID(static_cast<int>(i));ImGui::Separator();ImGui::TextWrapped("%s",row.label.c_str());
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
  if(!data->definitions.empty()&&ImGui::TreeNode("Loaded VFE definitions (read-only ordinals)")){if(data->definitions_stale)ImGui::TextWrapped("Stale: these are retained last-good definitions; current equation capture failed.");ImGui::TextWrapped("Ordinals and numeric types do not identify a semantic REL/ALT/OV/VMIN role. Current ROM graph and independent policy receipts are required before semantic editing.");
   if(ImGui::BeginTable("definition-inventory",5,ImGuiTableFlags_ScrollY|ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg,{0,210})){for(auto name:{"Index","Type / next","Lower uV","Upper uV","Qualification"})ImGui::TableSetupColumn(name);ImGui::TableHeadersRow();for(const auto& d:data->definitions){ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::Text("%u",d.index);ImGui::TableNextColumn();ImGui::Text("%u / %u",d.type,d.next);ImGui::TableNextColumn();if(d.lower)ImGui::Text("%lld",static_cast<long long>(*d.lower));ImGui::TableNextColumn();if(d.value)ImGui::Text("%lld",static_cast<long long>(*d.value));ImGui::TableNextColumn();ImGui::TextWrapped("%s",d.message.c_str());}ImGui::EndTable();}ImGui::TreePop();}
 }
 ImGui::Separator();ImGui::BeginDisabled(busy);if(ImGui::Button("Save selected voltage profile...")){VoltageLimitsIntent intent;intent.operation=VoltageLimitsIntent::Operation::save_profile;for(const auto& d:state.drafts)if(d.include)intent.selection.push_back(d.key);out.push_back(std::move(intent));}ImGui::SameLine();if(ImGui::Button("Load voltage profile..."))out.push_back({VoltageLimitsIntent::Operation::load_profile,0,{},{}});ImGui::EndDisabled();
 if(!state.staged.empty()){ImGui::Text("Staged profile: %u selected requests",unsigned(state.staged.size()));for(const auto& r:state.staged){ImGui::Text("Target %u/%u/%u: %lld%s",unsigned(r.key.kind),r.key.rail,r.key.index,static_cast<long long>(r.value),r.key.kind==VoltageLimitKind::switch_delay?" us":" uV");if(r.lower)ImGui::Text("  Lower: %lld uV",static_cast<long long>(*r.lower));}
  ImGui::BeginDisabled(busy||!unlocked||!data||data->recovery_pending);if(ImGui::Button("Apply staged voltage profile"))out.push_back({VoltageLimitsIntent::Operation::apply,0,state.staged,{}});ImGui::EndDisabled();}
 ImGui::BeginDisabled(busy||!unlocked||!data||!data->undo_available||data->recovery_pending);if(ImGui::Button("Undo voltage limits"))out.push_back({VoltageLimitsIntent::Operation::undo,0,{},{}});ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!data||!data->recovery_pending);if(ImGui::Button("Recover selected voltage limit"))out.push_back({VoltageLimitsIntent::Operation::recover,0,{},{}});ImGui::EndDisabled();
 if(!unlocked)ImGui::TextDisabled("Unlock controls for writes. Inspection and profile staging remain read-only.");if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());ImGui::PopID();return out;
}
}
