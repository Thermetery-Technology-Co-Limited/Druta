// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_workspace_view.h"
#include <imgui.h>
#include <algorithm>
#include <cstdio>
#include <exception>
namespace druta::ui {
PolicyWorkspaceOutput draw_policy_workspace(PolicyWorkspaceViewState& state,PolicyAnnotations& notes,const ScopedSnapshot* snapshot,uint64_t generation,bool busy,bool connected,bool unlocked){
 PolicyWorkspaceOutput out;if(state.generation!=generation){const bool open=state.open;state={};state.open=open;state.generation=generation;}
 if(!state.open)return out;ImGui::SetNextWindowSize({800,650},ImGuiCond_FirstUseEver);
 if(!ImGui::Begin("Policy workspace###druta_policy_workspace",&state.open)){ImGui::End();return out;}
 ImGui::TextWrapped("Annotations are display notes only. Maximum requests are selected current limits, not factory defaults.");
 ImGui::BeginDisabled(busy||!connected);if(ImGui::Button("Refresh policies"))out.refresh=true;ImGui::EndDisabled();
 if(snapshot){ImGui::SameLine();if(ImGui::Button("Select all policies")){state.selected.clear();for(const auto& row:snapshot->entries)if(row.key.kind==ScopedKey::Kind::policy)state.selected.push_back(row.key);state.preview.reset();}}
 if(snapshot){for(const auto& row:snapshot->entries)if(row.key.kind==ScopedKey::Kind::policy){
   ImGui::PushID(static_cast<int>(row.key.policy));bool selected=std::find(state.selected.begin(),state.selected.end(),row.key)!=state.selected.end();
   if(ImGui::Checkbox("Include",&selected)){if(selected)state.selected.push_back(row.key);else state.selected.erase(std::remove(state.selected.begin(),state.selected.end(),row.key),state.selected.end());state.preview.reset();}
   ImGui::SameLine();ImGui::Text("Policy %u | current %lld | limit %lld %s%s",row.key.policy,static_cast<long long>(row.value),static_cast<long long>(row.high),row.unit.c_str(),row.fresh?"":" (stale)");
   auto note=notes.get(snapshot->target,row);std::snprintf(state.label.data(),state.label.size(),"%s",note.c_str());
   ImGui::SetNextItemWidth(240);if(ImGui::InputText("Display note",state.label.data(),state.label.size())){try{notes.set(snapshot->target,row,state.label.data());out.save_annotations=true;}catch(const std::exception& e){state.status=e.what();}}
   ImGui::PopID();}
  ImGui::BeginDisabled(busy||!connected||state.selected.empty());if(ImGui::Button("Preview selected maxima"))state.preview=preview_policy_max(*snapshot,state.selected);ImGui::EndDisabled();
 }
 if(state.preview){
  bool unchanged=snapshot && snapshot->target.uuid==state.preview->target.uuid && snapshot->target.slot==state.preview->target.slot;
  if(unchanged)for(const auto& planned:state.preview->rows){if(!planned.included)continue;const ScopedCurrent* found=nullptr;unsigned count=0;
   for(const auto& current:snapshot->entries)if(current.key==planned.key){found=&current;++count;}
   if(count!=1 || !found->fresh || !found->writable || found->binding!=planned.binding || found->unit!=planned.unit || found->value!=planned.before || found->high!=planned.maximum){unchanged=false;break;}}
  if(!unchanged){state.preview.reset();state.status="Policy requests or limits changed; preview again.";}
 }
 if(state.preview){ImGui::TextWrapped("%s",state.preview->message.c_str());for(const auto& row:state.preview->rows){
   if(row.included)ImGui::Text("Policy %u: %lld -> %lld %s",row.key.policy,static_cast<long long>(row.before),static_cast<long long>(row.maximum),row.unit.c_str());
   else ImGui::TextWrapped("Policy %u skipped: %s",row.key.policy,row.reason.c_str());}
  ImGui::BeginDisabled(busy||!connected||!unlocked||!state.preview->document);if(ImGui::Button("Apply reviewed maxima")){out.apply=state.preview->document;state.preview.reset();}ImGui::EndDisabled();}
 if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());ImGui::End();return out;
}
}
