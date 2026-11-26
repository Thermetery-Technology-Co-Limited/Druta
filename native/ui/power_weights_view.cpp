// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_weights_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
#include <algorithm>
#include <cfloat>
#include <cstdio>
#include <cstring>
#include <initializer_list>
namespace druta::ui {
void sync_power_weights_view(PowerWeightsViewState& view,const PowerWeightsData* data,uint64_t generation){
 if(!view.initialized||view.generation!=generation){view={};view.initialized=true;view.generation=generation;}
 if(!data)return;
 for(const auto& row:data->rows){auto it=std::find_if(view.drafts.begin(),view.drafts.end(),[&](const auto& d){return d.index==row.index;});
  if(it==view.drafts.end()){view.drafts.push_back({});it=view.drafts.end()-1;it->index=row.index;}
  if(!it->dirty&&!row.stale&&row.weight_raw){it->multiplier=*row.weight_raw/4096.0;it->expected=row.weight_raw;}
 }
}
namespace {
// power_relationships.py words its decoder's reasons as sentences; the native decoder's
// shorter reasons (also shown in Focused) are reworded here for the released box only.
std::string released_reason(const std::string& reason){
 static constexpr const char* words[][2]={
  {"Policy INFO geometry/command unrecognized","The policy INFO geometry or command is not understood."},
  {"Policy INFO packet invalid/incomplete","The policy INFO packet is invalid or incomplete."},
  {"Target policy absent from INFO mask","The target policy is absent from the INFO mask."},
  {"Target policy row format unrecognized","The target policy row format is not understood."},
  {"Target policy unit unrecognized","The target policy unit is not understood."},
  {"Referenced relationship or target unavailable","A referenced relationship or target is unavailable."},
  {"Source policy row format unrecognized","The source policy row format is not understood."},
  {"Source relationship range exceeds INFO capacity","The source relationship range exceeds INFO capacity."},
  {"Linked relationship absent from INFO mask","The linked relationship is absent from the INFO mask."},
  {"Linked relationship or target unavailable","The linked relationship or target is unavailable."},
  {"Source relationship flags unrecognized","The source relationship flags are not understood."},
  {"Source relationship range invalid","The source relationship range is invalid."}};
 for(const auto& w:words)if(reason==w[0])return w[1];
 return reason;}

std::string policy_amount(uint32_t raw, uint32_t unit) {
 if (raw == UINT32_MAX) return "unavailable (sentinel)";
 if (unit > 1) return std::to_string(raw)+" raw";
 char value[48];
 std::snprintf(value,sizeof(value),"%.10g %s",raw/1000.0,unit == 0 ? "W" : "A");
 return value;
}

void draw_firmware_power_policies(const PowerWeightsData& data) {
 if (!data.image.ok || !ImGui::TreeNode("BIOS power and current definitions")) return;
 ImGui::TextWrapped("Read-only minimum, rated and maximum values stored in the selected BIOS.");
 if (data.image_stale) ImGui::TextWrapped("Showing the previous BIOS capture. %s",data.image_message.c_str());
 if (data.info_stale) ImGui::TextWrapped("The driver readback is stale. %s",data.info_message.c_str());
 if (ImGui::BeginTable("bios-power-definitions",5,ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_SizingStretchProp)) {
  for (const auto* label : {"Policy / channel","Minimum","Rated","Maximum","Driver readback"})
   ImGui::TableSetupColumn(label);
  ImGui::TableHeadersRow();
  for (const auto& policy : data.image.policies) {
   const auto& bios=policy.metadata;
   const bool driver_row=policy.index < data.report.policies.size() && (data.report.policy_mask & (uint32_t{1} << policy.index));
   if (!bios && !policy.bios_type && !driver_row) continue;
   ImGui::TableNextRow();
   ImGui::TableSetColumnIndex(0);
   if (bios) ImGui::Text("%u / %u",policy.index,bios->channel);
   else ImGui::Text("%u",policy.index);
   unsigned col=1;
   for (uint32_t raw : {bios ? bios->minimum : UINT32_MAX,bios ? bios->default_value : UINT32_MAX,
                        bios ? bios->maximum : UINT32_MAX}) {
    ImGui::TableSetColumnIndex(col++);
    if (bios) ImGui::TextUnformatted(policy_amount(raw,bios->unit).c_str());
    else ImGui::TextDisabled("--");
   }
   ImGui::TableSetColumnIndex(4);
   ImGui::TextWrapped("%s",data.image_stale || data.info_stale ? "Awaiting fresh comparison" :
                       firmware_power_policy_evidence(policy,data.report));
   if (!data.info_stale && policy.index < data.report.policies.size() && data.report.policies[policy.index] &&
       ImGui::IsItemHovered()) {
    const auto& live=*data.report.policies[policy.index];
    ImGui::SetTooltip("Driver type %u, channel %u\nMinimum %s\nRated %s\nMaximum %s",
                      live.type,live.channel,policy_amount(live.minimum,live.unit).c_str(),
                      policy_amount(live.default_value,live.unit).c_str(),policy_amount(live.maximum,live.unit).c_str());
   }
  }
  ImGui::EndTable();
 }
 ImGui::TreePop();
}
}
std::string power_relationships_text(const PowerWeightsData* data){
 if(!data)return {};  // registry_panel.py: the box stays empty until the first read
 std::string out;const auto line=[&](const std::string& text){out+=text;out+='\n';};
 const auto& report=data->report;
 const auto first=[](std::initializer_list<const std::string*> texts){for(const auto* t:texts)if(!t->empty())return released_reason(*t);return std::string("No decoded result");};
 if(!report.available){line("Configured relationships read unavailable: "+first({&report.reason,&data->info_message,&data->message}));while(!out.empty()&&out.back()=='\n')out.pop_back();return out;}
 if(data->info_stale){line("Configured relationships read unavailable: "+first({&data->info_message,&data->message}));line("Previous successful read follows; these values are historical.");}
 const auto join=[](const std::vector<uint32_t>& v){std::string s;for(auto x:v)s+=(s.empty()?"":", ")+std::to_string(x);return s;};
 const std::string slot(data->identity.slot,strnlen(data->identity.slot,sizeof(data->identity.slot)));
 line("Configured relationships for adapter "+(slot.empty()?std::string("unknown"):slot));
 const auto why=[](const std::string& reason){return reason.empty()?std::string("unknown reason"):released_reason(reason);};
 for(const auto& s:report.source_references){const auto policy=std::to_string(s.policy);
  if(!s.available)line("Source policy "+policy+": unavailable ("+why(s.reason)+")");
  else if(!s.enabled.value_or(false))line("Source policy "+policy+": configured relationship range disabled");
  else line("Source policy "+policy+" -> configured relationships: "+(s.relationships.empty()?std::string("none"):join(s.relationships)));}
 for(const auto& s:report.linked_references){const auto head="Source policy "+std::to_string(s.policy)+" -> configured relationship "+std::to_string(s.relationship);
  if(s.available&&s.target)line(head+" -> target policy "+std::to_string(*s.target));else line(head+": unavailable ("+why(s.reason)+")");}
 for(const auto& s:report.range_references){const auto head="Source policy "+std::to_string(s.policy)+" -> configured relationship range "+std::to_string(s.first)+".."+std::to_string(s.last);
  if(!s.available)line(head+": unavailable ("+why(s.reason)+")");else if(s.relationships.empty())line(head+": empty");else line(head+": "+join(s.relationships));}
 if(!report.source_uninterpreted_policies.empty())line("Source mapping uninterpreted for policy types at indices: "+join(report.source_uninterpreted_policies));
 for(const auto& r:report.relationships){
  line("Relationship "+std::to_string(r.index)+" -> target policy "+std::to_string(r.target)+" (type "+std::to_string(r.type)+")");
  if(r.type==0&&r.weight_raw){char b[48];std::snprintf(b,sizeof(b),"%.12f",*r.weight_raw/4096.0);std::string v=b;while(!v.empty()&&v.back()=='0')v.pop_back();if(!v.empty()&&v.back()=='.')v.pop_back();
   line("  Weight: "+v+" (raw "+std::to_string(*r.weight_raw)+")");}
  else line("  Relationship type is uninterpreted.");
  if(!r.target_metadata){line("  Target details unavailable: "+why(r.target_reason));continue;}
  const auto& m=*r.target_metadata;
  line("  Target policy type "+std::to_string(m.type)+", channel "+std::to_string(m.channel)+": minimum "+policy_amount(m.minimum,m.unit)
   +"; nominal "+policy_amount(m.default_value,m.unit)+"; maximum "+policy_amount(m.maximum,m.unit));
 }
 if(report.relationships.empty())line("No configured relationships.");
 while(!out.empty()&&out.back()=='\n')out.pop_back();return out;
}
bool draw_power_relationships(const PowerWeightsData* data,bool busy,bool connected){
 ImGui::PushID("power-relationships");
 ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
 classic_text("Configured relationships describe policy targets, weights and available source-policy ranges. "
  "This partial mapping does not establish the producer of an active vote. "
  "Qualified weights can be edited through the BIOS field preview and require a driver refresh.");
 ImGui::PopTextWrapPos();
 ImGui::BeginDisabled(busy||!connected);const bool read=ImGui::Button("Read relationships");ImGui::EndDisabled();
 const auto text=power_relationships_text(data);
 ImGui::InputTextMultiline("##relationships",const_cast<char*>(text.c_str()),text.size()+1,ImVec2(-FLT_MIN,classic_px(180)),ImGuiInputTextFlags_ReadOnly);
 ImGui::PopID();return read;
}
std::vector<PowerWeightsIntent> draw_power_weights(PowerWeightsViewState& view,const PowerWeightsData* data,uint64_t generation,bool busy,bool classic,bool unlocked,bool connected){
 sync_power_weights_view(view,data,generation);std::vector<PowerWeightsIntent> intents;ImGui::PushID("power-weights");
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
  draw_firmware_power_policies(*data);
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
 if(data&&data->receipt&&!data->receipt->is_pascal_clocks()){const auto& receipt=*data->receipt;ImGui::Separator();
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
 // Classic shows the configured references read-only under Diagnostics.
 if(!classic&&data&&ImGui::CollapsingHeader("Configured relationship references")){
  for(const auto& r:data->report.source_references){ImGui::Text("Policy %u: %s | %u..%u",r.policy,r.enabled?(*r.enabled?"enabled":"disabled"):"unknown flags",r.first,r.last);if(!r.available)ImGui::TextWrapped("%s",r.reason.c_str());}
  for(const auto& r:data->report.linked_references){ImGui::Text("Policy %u -> relationship %u",r.policy,r.relationship);if(!r.available)ImGui::TextWrapped("%s",r.reason.c_str());}
  for(const auto& r:data->report.range_references){ImGui::Text("Policy %u -> %u..%u",r.policy,r.first,r.last);if(!r.available)ImGui::TextWrapped("%s",r.reason.c_str());}
 }
 ImGui::PopID();return intents;
}
}
