// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_catalog_view.h"
#include <imgui.h>
#include <algorithm>
#include <stdexcept>
namespace druta::ui {
namespace {
void invalidate(ProfileCatalogViewState& state){state.staged.reset();state.preflight_ok=false;state.status="Selection changed; compose and stage to review";}
}
void sync_profile_catalog_generation(ProfileCatalogViewState& state,uint64_t generation){
 if(!state.initialized||state.generation!=generation){state.generation=generation;state.initialized=true;state.staged.reset();state.preflight_ok=false;
  for(auto& item:state.items)item.included=false;state.status="Choose files or capture configured settings. Loading only stages; Apply is explicit.";}
}
void add_profile_catalog_item(ProfileCatalogViewState& state,const std::string& id,const std::string& name,const ProfileJson& document){
 auto found=std::find_if(state.items.begin(),state.items.end(),[&](const auto& item){return item.id==id;});
 ProfileCatalogItem item;item.id=id;item.name=name;item.document=document;item.description=inspect_profile(document);item.selected_targets=item.description.targets;
 if(found==state.items.end())state.items.push_back(std::move(item));else *found=std::move(item);invalidate(state);
}
bool stage_profile_catalog_selection(ProfileCatalogViewState& state){
 try{std::vector<ProfileComponent> components;for(const auto& item:state.items)if(item.included){
   if(!item.description.valid)throw std::runtime_error(item.name+": "+item.description.error);
   components.push_back({item.name,select_profile_targets(item.document,item.selected_targets)});
  }
  state.staged=compose_profiles(components);state.preflight_ok=false;
  state.status=inspect_profile(*state.staged).summary+". No requests applied.";return true;
 }catch(const std::exception& e){state.staged.reset();state.preflight_ok=false;state.status=e.what();return false;}
}
void record_profile_catalog_preflight(ProfileCatalogViewState& state,uint64_t generation,const ProfileJson& document,bool ok,const std::string& message){
 if(state.generation!=generation||!state.staged||*state.staged!=document)return;
 state.preflight_ok=ok;state.status=message;
}
ProfileCatalogViewOutput draw_profile_catalog_workspace(ProfileCatalogViewState& state,uint64_t generation,bool busy,bool unlocked){
 sync_profile_catalog_generation(state,generation);ProfileCatalogViewOutput out;if(!state.open)return out;
 ImGui::SetNextWindowSize({780,680},ImGuiCond_FirstUseEver);
 if(!ImGui::Begin("Profile catalog###druta_profile_catalog",&state.open)){ImGui::End();return out;}
 ImGui::PushID("profile-catalog");
 if(ImGui::Button("Choose profile files..."))out.intents.push_back({ProfileCatalogIntent::Operation::choose_files,{},{}});
 ImGui::SameLine();if(ImGui::Button("Clear list")){state.items.clear();invalidate(state);}
 ImGui::TextWrapped("Choose files and controls, then compose, review and apply. Loading and saving do not change GPU settings.");
 constexpr ProfileScope scopes[]={ProfileScope::daily,ProfileScope::curve,ProfileScope::scoped,ProfileScope::timing,ProfileScope::icx,ProfileScope::fan,ProfileScope::voltage_policy,ProfileScope::power_integral,ProfileScope::voltage_tables,ProfileScope::voltage_limits};
 ImGui::SetNextItemWidth(230);
 if(ImGui::BeginCombo("Capture scope",profile_scope_name(state.capture_scope))){for(auto scope:scopes)if(ImGui::Selectable(profile_scope_name(scope),scope==state.capture_scope))state.capture_scope=scope;ImGui::EndCombo();}
 ImGui::SameLine();ImGui::BeginDisabled(busy);if(ImGui::Button("Save configured scope..."))out.intents.push_back({ProfileCatalogIntent::Operation::save_capture,state.capture_scope,{}});ImGui::EndDisabled();
 ImGui::PushStyleColor(ImGuiCol_Text,ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
 ImGui::TextWrapped("Read-only capture. Select rail/power, policy, feedback and timing fields in their editors.");
 ImGui::PopStyleColor();
 ImGui::Separator();
 for(size_t i=0;i<state.items.size();++i){auto& item=state.items[i];ImGui::PushID(static_cast<int>(i));
  ImGui::BeginDisabled(!item.description.valid);if(ImGui::Checkbox("Include",&item.included))invalidate(state);ImGui::EndDisabled();ImGui::SameLine();ImGui::TextWrapped("%s",item.name.c_str());
  ImGui::TextWrapped("%s",item.description.summary.c_str());
  if(item.description.uuid)ImGui::TextDisabled("GPU: %s",item.description.uuid->c_str());if(item.description.slot)ImGui::TextDisabled("Slot: %s",item.description.slot->c_str());
  if(item.description.valid&&ImGui::TreeNode("Included controls")){
   for(const auto& target:item.description.targets){bool selected=std::find(item.selected_targets.begin(),item.selected_targets.end(),target)!=item.selected_targets.end();
    if(ImGui::Checkbox(target.c_str(),&selected)){if(selected)item.selected_targets.push_back(target);else item.selected_targets.erase(std::remove(item.selected_targets.begin(),item.selected_targets.end(),target),item.selected_targets.end());invalidate(state);}}
   ImGui::TreePop();
  }ImGui::Separator();ImGui::PopID();
 }
 if(state.items.empty())ImGui::TextDisabled("No files loaded.");
 if(ImGui::Button("Compose and stage selection"))stage_profile_catalog_selection(state);
 ImGui::SameLine();ImGui::BeginDisabled(!state.staged);if(ImGui::Button("Save composed profile..."))out.intents.push_back({ProfileCatalogIntent::Operation::save_composed,ProfileScope::combined,*state.staged});ImGui::EndDisabled();
 if(state.staged){
  ImGui::TextWrapped("%s",inspect_profile(*state.staged).summary.c_str());
  ImGui::BeginDisabled(busy);if(ImGui::Button("Preflight staged profile"))out.intents.push_back({ProfileCatalogIntent::Operation::preflight,ProfileScope::combined,*state.staged});ImGui::EndDisabled();
  ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!state.preflight_ok);
  if(ImGui::Button("Apply staged profile")){out.intents.push_back({ProfileCatalogIntent::Operation::apply,ProfileScope::combined,*state.staged});state.preflight_ok=false;state.status="Apply queued; worker repeats whole preflight before any write";}
  ImGui::EndDisabled();
 }
 ImGui::TextWrapped("%s",state.status.c_str());
 ImGui::TextDisabled("Session only. No automatic startup or driver-reset application.");
 ImGui::PopID();ImGui::End();return out;
}
}
