// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weights_view.h"
#include <imgui.h>
#include <algorithm>
#include <cstdio>
namespace druta::ui {
void sync_power_weights_view(PowerWeightsViewState& view,const PowerWeightsData* data,uint64_t generation){
 if(!view.initialized||view.generation!=generation){view={};view.initialized=true;view.generation=generation;}
 if(!data)return;
 for(const auto& row:data->rows){auto it=std::find_if(view.drafts.begin(),view.drafts.end(),[&](const auto& d){return d.index==row.index;});
  if(it==view.drafts.end()){view.drafts.push_back({});it=view.drafts.end()-1;it->index=row.index;}
  if(!it->dirty&&!row.stale&&row.weight_raw){it->multiplier=*row.weight_raw/4096.0;it->expected=row.weight_raw;}
 }
}
std::vector<PowerWeightsIntent> draw_power_weights(PowerWeightsViewState& view,const PowerWeightsData* data,uint64_t generation,bool busy,bool classic,bool unlocked,bool connected){
 (void)classic;sync_power_weights_view(view,data,generation);std::vector<PowerWeightsIntent> intents;ImGui::PushID("power-weights");
 ImGui::BeginDisabled(busy||!connected);if(ImGui::Button("Read relationships"))intents.push_back({PowerWeightsIntent::Operation::read,{}});ImGui::EndDisabled();
 ImGui::TextWrapped("Relationship weights persist through a driver refresh. Preview changes no settings.");
 ImGui::SameLine();ImGui::BeginDisabled(busy);if(ImGui::Button("Inspect saved override"))intents.push_back({PowerWeightsIntent::Operation::inspect,{}});ImGui::EndDisabled();
 if(!view.status.empty())ImGui::TextWrapped("%s",view.status.c_str());
 if(data&&!data->receipt_message.empty())ImGui::TextWrapped("%s",data->receipt_message.c_str());
 if(data){if(!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());
  if(ImGui::BeginTable("relationships",5,ImGuiTableFlags_SizingStretchProp|ImGuiTableFlags_BordersInnerH)){
   ImGui::TableSetupColumn("Relationship / target");ImGui::TableSetupColumn("Current multiplier");ImGui::TableSetupColumn("Staged multiplier");ImGui::TableSetupColumn("Include");ImGui::TableSetupColumn("Status");ImGui::TableHeadersRow();
   for(const auto& row:data->rows){auto it=std::find_if(view.drafts.begin(),view.drafts.end(),[&](const auto& d){return d.index==row.index;});if(it==view.drafts.end())continue;
    ImGui::PushID(static_cast<int>(row.index));ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);ImGui::Text("%u -> policy %u",row.index,row.target);
    if(row.target_metadata)ImGui::TextDisabled("Channel %u | %s",row.target_metadata->channel,row.target_metadata->unit==0?"power":"current");
    ImGui::TableSetColumnIndex(1);if(row.weight_raw)ImGui::Text("%.14g%s",*row.weight_raw/4096.0,row.stale?" (stale)":"");else ImGui::TextUnformatted("Unavailable");
    ImGui::TableSetColumnIndex(2);ImGui::SetNextItemWidth(-1);ImGui::BeginDisabled(busy||!row.eligible_for_preview||row.stale);if(ImGui::InputDouble("##weight",&it->multiplier,0,0,"%.14g"))it->dirty=true;ImGui::EndDisabled();
    ImGui::TableSetColumnIndex(3);ImGui::Checkbox("Include",&it->include);ImGui::TableSetColumnIndex(4);ImGui::TextWrapped("%s",row.message.c_str());ImGui::PopID();
   }ImGui::EndTable();
  }
 }
 std::vector<PowerWeightRequest> requests;bool valid=true;std::string why;
 for(const auto& draft:view.drafts)if(draft.include){
  const auto* row=data?([&]()->const PowerWeightRow*{for(const auto& r:data->rows)if(r.index==draft.index)return &r;return nullptr;})():nullptr;
  if(!row||!row->eligible_for_preview||row->stale||!draft.expected){valid=false;why="Read current qualified relationships before Preview.";continue;}
  try{(void)power_weight_raw(draft.multiplier);requests.push_back({draft.index,draft.multiplier,draft.expected});}catch(const std::exception& e){valid=false;why=e.what();}
 }
 ImGui::BeginDisabled(busy||!connected||!valid||requests.empty());if(ImGui::Button("Preview selected weights"))intents.push_back({PowerWeightsIntent::Operation::preview,std::move(requests)});ImGui::EndDisabled();
 if(!why.empty())ImGui::TextWrapped("%s",why.c_str());
 if(data&&data->preview){const auto& preview=*data->preview;ImGui::Separator();ImGui::TextWrapped("%s",preview.message.c_str());
  if(preview.ok){for(const auto& selected:preview.selected)ImGui::Text("Relationship %u: %.14g -> %.14g",selected.index,selected.before_raw/4096.0,selected.after_raw/4096.0);
   ImGui::Text("Registry records to add: %zu",preview.registry_added.size());
   ImGui::TextWrapped("Apply closes Druta, refreshes this GPU and reopens a fresh session. Ordinary tuning is cleared.");
   ImGui::BeginDisabled(busy||!connected||!unlocked||preview.no_change||(data->receipt&&data->receipt->phase!="removed"));
   if(ImGui::Button("Apply preview and refresh GPU"))intents.push_back({PowerWeightsIntent::Operation::prepare,{}});
   ImGui::EndDisabled();
   if(ImGui::CollapsingHeader("Preview details")){ImGui::TextWrapped("Image before: %s",preview.image_sha256.c_str());ImGui::TextWrapped("Image after: %s",preview.expected_image_sha256.c_str());
    ImGui::TextWrapped("Adapter: %s",preview.windows.slot.c_str());for(const auto& r:preview.registry_added){std::string name;for(wchar_t c:r.name)name.push_back(c>=0&&c<128?static_cast<char>(c):'?');ImGui::Text("%s: %zu bytes",name.c_str(),r.bytes.size());}}
  }
 }
 if(data&&data->receipt){const auto& receipt=*data->receipt;ImGui::Separator();
  ImGui::TextWrapped("Saved override: %s | %s",receipt.id.c_str(),receipt.phase.c_str());
  ImGui::TextWrapped("%s",receipt.message.c_str());
  ImGui::BeginDisabled(busy||receipt.phase=="removed");
  if(ImGui::Button("Verify saved override"))intents.push_back({PowerWeightsIntent::Operation::verify,{}});
  ImGui::EndDisabled();ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||receipt.phase=="removed");
  if(connected&&receipt.phase=="prepared"&&!receipt.uncertain&&receipt.issued_names.empty()){
   if(ImGui::Button("Activate saved preview and refresh GPU"))intents.push_back({PowerWeightsIntent::Operation::activate_saved,{}});
   ImGui::SameLine();
  }
  if(ImGui::Button("Remove override and refresh GPU"))intents.push_back({PowerWeightsIntent::Operation::remove,{}});
  ImGui::EndDisabled();
  ImGui::TextWrapped("Recovery restores only this override's registry records. Refresh closes and reopens Druta without replaying tuning.");
 }
 if(data&&ImGui::CollapsingHeader("Configured relationship references")){
  for(const auto& r:data->report.source_references){ImGui::Text("Policy %u: %s | %u..%u",r.policy,r.enabled?(*r.enabled?"enabled":"disabled"):"unknown flags",r.first,r.last);if(!r.available)ImGui::TextWrapped("%s",r.reason.c_str());}
  for(const auto& r:data->report.linked_references){ImGui::Text("Policy %u -> relationship %u",r.policy,r.relationship);if(!r.available)ImGui::TextWrapped("%s",r.reason.c_str());}
  for(const auto& r:data->report.range_references){ImGui::Text("Policy %u -> %u..%u",r.policy,r.first,r.last);if(!r.available)ImGui::TextWrapped("%s",r.reason.c_str());}
 }
 ImGui::PopID();return intents;
}
}
