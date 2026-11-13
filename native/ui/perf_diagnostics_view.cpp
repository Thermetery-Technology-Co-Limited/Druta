/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "perf_diagnostics_view.h"
#include <imgui.h>
#include <cstdio>
#include <string>
namespace druta::ui {
namespace {
std::string hex_bytes(const uint8_t* data,size_t size) {
    static const char* digits="0123456789abcdef"; std::string out; out.reserve(size*2);
    for(size_t i=0;i<size;++i){out.push_back(digits[data[i]>>4]);out.push_back(digits[data[i]&15]);}return out;
}
}
bool draw_perf_diagnostics(const PerfViewModel& model) {
    bool refresh=false;
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
