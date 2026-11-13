/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "power_arbitration_view.h"
#include <imgui.h>
#include <cstdio>
#include <string>
namespace druta::ui {
namespace {
std::string amount(uint32_t value,const dc_arbitration_row& row) {
    if(value==UINT32_MAX)return "0xFFFFFFFF (sentinel)";
    if(row.has_unit && (std::string(row.unit)=="mW" || std::string(row.unit)=="mA")) {
        char out[64];std::snprintf(out,sizeof(out),"%.6g %s",value/1000.0,row.unit[1]=='W'?"W":"A");return out;
    }return std::to_string(value)+" (raw units)";
}
std::string delta(int32_t value,const dc_arbitration_row& row) {
    if(row.has_unit && (std::string(row.unit)=="mW" || std::string(row.unit)=="mA")) {
        char out[64];std::snprintf(out,sizeof(out),"%.6g %s",value/1000.0,row.unit[1]=='W'?"W":"A");return out;
    }return std::to_string(value)+" (raw units)";
}
void vector(const char* label,const dc_arbitration_vector& v,const dc_arbitration_row& row) {
    if(!v.available){ImGui::TextWrapped("%s: unavailable (%s)",label,v.reason);return;}
    ImGui::Text("%s: %s -> %s",label,v.method,amount(v.output,row).c_str());
    ImGui::Text("Initial: %s",amount(v.initial_value,row).c_str());
    if(v.has_computed_output)ImGui::Text("Computed: %s%s",amount(v.computed_output,row).c_str(),v.consistent==0?" (differs from output)":"");
    if(!v.count)ImGui::TextDisabled("No active inputs");
    for(uint32_t i=0;i<v.count;++i)ImGui::Text("Client %u: %s",v.inputs[i].client,amount(v.inputs[i].value,row).c_str());
}
}
bool draw_power_arbitration(const PowerArbitrationModel& model) {
    ImGui::PushID("power-arbitration");
    ImGui::TextWrapped("Read-only policy arbitration. Packets are read sequentially; firmware votes may change between reads.");
    const bool refresh=ImGui::Button("Refresh power arbitration");
    if(!model.message.empty())ImGui::TextWrapped("%s%s",model.message.c_str(),model.freshness==ArbitrationFreshness::stale?" (last successful read; stale)":"");
    if(model.freshness==ArbitrationFreshness::unavailable) {
        if(!model.report && model.attempts==0 && model.error_kind==DC_ERR_NONE)
            ImGui::TextDisabled("Not read yet. Select Refresh to inspect power arbitration.");
        else ImGui::TextDisabled("Unavailable (error kind %d)",model.error_kind);
    }
    if(model.status_observed && !model.status_present)ImGui::TextDisabled("STATUS packet was not returned.");
    if(model.report){
        const auto& report=*model.report;
        if(!report.policy_count)ImGui::TextDisabled("No registered power/current policies in this read.");
        for(uint32_t i=0;i<report.policy_count;++i){const auto& row=report.policies[i];ImGui::PushID(static_cast<int>(row.policy));
            char name[80];std::snprintf(name,sizeof(name),"Policy %u | type %u | channel %u",row.policy,row.type,row.channel);
            if(ImGui::CollapsingHeader(name)){
                if(!row.available)ImGui::TextWrapped("Unavailable: %s",row.reason);
                else {
                    ImGui::TextWrapped("Requested %s | effective %s | value %s | adjustment %s",
                        amount(row.requested,row).c_str(),amount(row.effective,row).c_str(),amount(row.value,row).c_str(),delta(row.delta,row).c_str());
                    vector("Host",row.host,row);vector("Firmware",row.pmu,row);
                }
                for(uint32_t j=0;j<row.observation_count;++j)ImGui::TextWrapped("%s",row.observations[j]);
            }ImGui::PopID();
        }
    }
    ImGui::PopID();return refresh;
}
}
