/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "power_arbitration_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
#include <cfloat>
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
std::string power_arbitration_text(const PowerArbitrationModel& model,const std::string& slot) {
    std::string out;const auto line=[&](const std::string& text){out+=text;out+='\n';};
    if(model.freshness==ArbitrationFreshness::unavailable&&!model.report&&model.attempts==0&&model.error_kind==DC_ERR_NONE)
        return {};  // registry_panel.py: the box stays empty until the first read
    if(model.freshness!=ArbitrationFreshness::current){
        line("Arbitration read unavailable: "+(model.message.empty()?std::string("No decoded result"):model.message));
        if(model.report)line("Previous successful read follows; these values are historical.");
    }
    // Native detail Python's arbitration_lines does not print, kept after its lines.
    std::string extra;const auto more=[&](const std::string& text){extra+=text;extra+='\n';};
    if(model.status_observed&&!model.status_present)more("STATUS packet was not returned.");
    if(model.report){
        // registry_panel.py arbitration_lines.
        line("Adapter: "+(slot.empty()?std::string("unknown"):slot));
        line("Read sequentially; firmware votes and effective limits may still be updating after a change.");
        const auto& report=*model.report;
        for(uint32_t i=0;i<report.policy_count;++i){const auto& row=report.policies[i];
            const std::string head="Policy "+std::to_string(row.policy);
            line(head+": type "+std::to_string(row.type)+", channel "+std::to_string(row.channel));
            if(!row.available){line("  Unavailable: "+std::string(row.reason));
                for(uint32_t j=0;j<row.observation_count;++j)more(head+": "+row.observations[j]);continue;}
            line("  Requested "+amount(row.requested,row)+"; effective "+amount(row.effective,row)+"; adjustment "+delta(row.delta,row));
            more(head+": value "+amount(row.value,row));
            for(const auto* side:{&row.host,&row.pmu}){const auto& v=*side;const std::string label=side==&row.host?"Host":"Firmware";
                if(!v.available){line("  "+label+": unavailable ("+v.reason+")");continue;}
                std::string votes;for(uint32_t j=0;j<v.count;++j)votes+=(j?", ":"")+std::string("client ")+std::to_string(v.inputs[j].client)+": "+amount(v.inputs[j].value,row);
                line("  "+label+": "+v.method+" -> "+amount(v.output,row)+"; "+(votes.empty()?std::string("no active inputs"):votes));
                more("  "+label+": initial "+amount(v.initial_value,row)+(v.has_computed_output?"; computed "+amount(v.computed_output,row)+(v.consistent==0?" (differs from output)":""):std::string()));}
            for(uint32_t j=0;j<row.observation_count;++j)line("  "+std::string(row.observations[j]));
        }
        if(!report.policy_count)line("No registered power/current policies.");
    }
    if(!extra.empty()){if(!out.empty())line("");line("Additional native readback:");out+=extra;}
    while(!out.empty()&&out.back()=='\n')out.pop_back();
    return out;
}
bool draw_power_arbitration(const PowerArbitrationModel& model,bool classic,const std::string& slot) {
    if(classic){
        ImGui::PushID("power-arbitration");
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
        classic_text("Compare each request with host votes, firmware votes, and the effective limit. "
            "Firmware values can lag a recent change; refresh to check again. Client numbers have not been assigned physical names.");
        ImGui::PopTextWrapPos();
        const bool refresh=ImGui::Button("Read arbitration");
        const std::string text=power_arbitration_text(model,slot);
        ImGui::InputTextMultiline("##readback",const_cast<char*>(text.c_str()),text.size()+1,ImVec2(-FLT_MIN,classic_px(260)),ImGuiInputTextFlags_ReadOnly);
        ImGui::PopID();return refresh;
    }
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
