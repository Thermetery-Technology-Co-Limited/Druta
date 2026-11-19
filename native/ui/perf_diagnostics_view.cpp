/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "perf_diagnostics_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
#include <cfloat>
#include <cstdio>
#include <string>
namespace druta::ui {
namespace {
std::string hex_bytes(const uint8_t* data,size_t size) {
    static const char* digits="0123456789abcdef"; std::string out; out.reserve(size*2);
    for(size_t i=0;i<size;++i){out.push_back(digits[data[i]>>4]);out.push_back(digits[data[i]&15]);}return out;
}
}
std::string perf_diagnostics_text(const PerfViewModel& model) {
    std::string out;
    const auto line=[&](const std::string& text){out+=text;out+='\n';};
    const auto group_name=[](const std::string& name){
        return name=="limits"?std::string("Performance-limit slots"):name=="cf_controller"?std::string("Feedback controllers"):name=="cf_policy"?std::string("Feedback policies"):name;};
    bool any=false;for(const auto& g:model.groups)any=any||g.snapshot||g.attempts||g.error_kind!=DC_ERR_NONE;
    // registry_panel.py: the box stays empty until the first read.
    if(!any)return {};
    // perf_diagnostics.py: a failed group's status is its error kind.
    const auto kind=[](int32_t error){switch(error){case DC_ERR_TRANSIENT:return "transient";case DC_ERR_INVALID:return "invalid";case DC_ERR_UNSUPPORTED:return "unsupported";
        case DC_ERR_IDENTITY:return "identity";case DC_ERR_MALFORMED:return "malformed";case DC_ERR_UNSTABLE:return "unstable";case DC_ERR_READBACK:return "readback";default:return "failed";}};
    for(const auto& g:model.groups) {
        const bool current=g.freshness==PerfFreshness::current;
        line(group_name(g.name)+": "+(current?(g.snapshot&&g.snapshot->entry_count?"ready":"empty"):kind(g.error_kind)));
        if(!current&&!g.message.empty())line("  "+g.message);
        // registry_panel.py refresh_diagnostics: a group with no snapshot ends without a blank line.
        if(!g.snapshot)continue;
        if(!current)line("  Previous successful read follows; these values are historical.");
        const auto& s=*g.snapshot;
        line("  Registered entries: "+std::to_string(s.entry_count));
        for(uint32_t i=0;i<s.flag_count;++i)line("  "+std::string(s.flags[i].label)+": "+std::to_string(s.flags[i].raw)+" (raw flag)");
        for(uint32_t i=0;i<s.target_count;++i){const auto& t=s.targets[i];
            line("  RmPerfCfPolicyOverrides "+std::string(t.field)+": "+(t.has_index?"policy "+std::to_string(t.index)+", raw flag "+(t.has_flag_raw?std::to_string(t.flag_raw):std::string("unavailable")):std::string("absent")));
            if(t.has_issue)line("    "+std::string(t.issue));}
        for(uint32_t i=0;i<s.entry_count;++i){const auto& e=s.entries[i];
            if(e.has_driver_label)line("  "+std::to_string(e.index)+": "+e.driver_label);
            if(e.has_policy){std::string indices;for(uint32_t bit=0;bit<32;++bit)if(e.controller_mask&(1u<<bit))indices+=(indices.empty()?"":", ")+std::to_string(bit);
                line("  Policy "+std::to_string(e.index)+": role "+std::to_string(e.role_id)+", controller selection "+(indices.empty()?std::string("empty"):indices));}
            if(e.issue_count)line("  Entry "+std::to_string(e.index)+": "+e.issue);}
        for(uint32_t i=0;i<s.issue_count;++i)line("  "+std::string(s.issues[i]));
        line("");
    }
    while(!out.empty()&&out.back()=='\n')out.pop_back();
    return out;
}
std::string perf_diagnostics_raw_text(const PerfViewModel& model) {
    std::string out;
    const auto line=[&](const std::string& text){out+=text;out+='\n';};
    const auto group_name=[](const std::string& name){
        return name=="limits"?std::string("Performance-limit slots"):name=="cf_controller"?std::string("Feedback controllers"):name=="cf_policy"?std::string("Feedback policies"):name;};
    bool any=false;for(const auto& g:model.groups)any=any||g.snapshot;
    if(!any)return {};
    // Native capture details, kept apart from the released summary.
    line("Groups are read separately, not as one simultaneous measurement.");
    for(const auto& g:model.groups){if(!g.snapshot)continue;const auto& s=*g.snapshot;
        line(group_name(g.name)+": architecture "+std::to_string(g.architecture)+", "+std::to_string(g.attempts)+" attempts");
        line("  INFO SHA-256: "+std::string(s.info_sha256));
        if(s.has_control_sha256)line("  CONTROL SHA-256: "+std::string(s.control_sha256));
        line("  Mask (raw): "+hex_bytes(s.mask,s.mask_bytes));
        for(uint32_t i=0;i<s.entry_count;++i){const auto& e=s.entries[i];
            line("  Entry "+std::to_string(e.index)+": INFO type "+std::to_string(e.type)+", CONTROL type "+std::to_string(e.control_type)
                +(e.has_policy?", role raw "+std::to_string(e.role_bytes_raw[0])+" / "+std::to_string(e.role_bytes_raw[1]):std::string())
                +(e.has_registry_flag?", registry flag raw "+std::to_string(e.registry_controlled_flag_raw):std::string()));
            line("    INFO raw: "+hex_bytes(e.info_raw,e.info_bytes));
            line("    CONTROL raw: "+hex_bytes(e.control_raw,e.control_bytes));}}
    while(!out.empty()&&out.back()=='\n')out.pop_back();
    return out;
}
bool draw_perf_diagnostics(const PerfViewModel& model,bool classic) {
    bool refresh=false;
    if(classic){
        ImGui::PushID("perf-diagnostics");
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
        classic_text("Read registered performance limits and feedback-policy flags. "
            "Registered slots do not identify which limits are currently active.");
        ImGui::PopTextWrapPos();
        refresh=ImGui::Button("Read driver policies");
        const std::string text=perf_diagnostics_text(model);
        ImGui::InputTextMultiline("##readback",const_cast<char*>(text.c_str()),text.size()+1,ImVec2(-FLT_MIN,classic_px(220)),ImGuiInputTextFlags_ReadOnly);
        // Native extra: the raw INFO/CONTROL capture, in its own closed section
        // after the released box so the box holds only the released text.
        const std::string raw=perf_diagnostics_raw_text(model);
        if(!raw.empty()){
            if(ClassicSection section{"Raw capture##perf-raw",2})
                ImGui::InputTextMultiline("##raw",const_cast<char*>(raw.c_str()),raw.size()+1,ImVec2(-FLT_MIN,classic_px(220)),ImGuiInputTextFlags_ReadOnly);
        }
        ImGui::PopID();return refresh;
    }
    ImGui::PushID("perf-diagnostics");
    ImGui::TextWrapped("Read-only PERF diagnostics. Groups are captured separately; results are not a simultaneous hardware measurement.");
    if(ImGui::Button("Refresh PERF diagnostics"))refresh=true;
    for(const auto& group:model.groups) {
        ImGui::PushID(group.name.c_str());
        if(ImGui::CollapsingHeader(group.name.c_str())) {
            ImGui::TextWrapped("%s%s",group.message.c_str(),group.freshness==PerfFreshness::stale?" (last successful read; stale)":"");
            if(group.freshness==PerfFreshness::unavailable)ImGui::TextDisabled("Unavailable (error kind %d)",group.error_kind);
            if(group.snapshot) {
                const auto& s=*group.snapshot;
                ImGui::Text("Architecture %u | %u active entries | %u attempts",group.architecture,s.entry_count,group.attempts);
                ImGui::TextWrapped("INFO SHA-256: %s",s.info_sha256);
                if(s.has_control_sha256)ImGui::TextWrapped("CONTROL SHA-256: %s",s.control_sha256);
                ImGui::TextWrapped("Mask (raw): %s",hex_bytes(s.mask,s.mask_bytes).c_str());
                for(uint32_t i=0;i<s.issue_count;++i)ImGui::TextWrapped("%s",s.issues[i]);
                for(uint32_t i=0;i<s.flag_count;++i) {
                    const auto& f=s.flags[i];
                    ImGui::Text("%s: raw %u%s",f.label,f.raw,f.has_boolean?(f.boolean?" (true)":" (false)"):" (meaning unknown)");
                }
                for(uint32_t i=0;i<s.target_count;++i) {
                    const auto& t=s.targets[i]; ImGui::Text("%s: %s",t.field,t.present?"present":"not present");
                    if(t.has_index)ImGui::Text("  index %u",t.index);
                    if(t.has_flag_raw)ImGui::Text("  raw flag %u",t.flag_raw);
                    if(t.has_issue)ImGui::TextWrapped("  %s",t.issue);
                }
                for(uint32_t i=0;i<s.entry_count;++i) {
                    const auto& e=s.entries[i];ImGui::PushID(static_cast<int>(e.index));
                    char label[64]; std::snprintf(label,sizeof(label),"Entry %u",e.index);
                    if(ImGui::TreeNode(label)) {
                        ImGui::Text("INFO type %u | CONTROL type %u",e.type,e.control_type);
                        if(e.has_driver_label)ImGui::Text("Driver label: %s",e.driver_label);
                        if(e.has_policy)ImGui::Text("Role raw %u / %u | role %u | controller mask 0x%08x",e.role_bytes_raw[0],e.role_bytes_raw[1],e.role_id,e.controller_mask);
                        if(e.has_registry_flag)ImGui::Text("Registry flag raw %u",e.registry_controlled_flag_raw);
                        if(e.issue_count)ImGui::TextWrapped("%s",e.issue);
                        ImGui::TextWrapped("INFO raw: %s",hex_bytes(e.info_raw,e.info_bytes).c_str());
                        ImGui::TextWrapped("CONTROL raw: %s",hex_bytes(e.control_raw,e.control_bytes).c_str());
                        ImGui::TreePop();
                    } ImGui::PopID();
                }
            }
        } ImGui::PopID();
    }
    ImGui::PopID();return refresh;
}
}
