// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_monitor_view.h"
#include "clock_monitor_model.h"
#include <imgui.h>
namespace druta::ui {
namespace {
float u(float value){return value*ImGui::GetFontSize()/18.0f;}
ImVec4 tint(const std::string& band,bool stale){
    if(stale)return ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled);
    if(band=="bad")return {0.95f,0.30f,0.28f,1};
    if(band=="warn")return {0.96f,0.65f,0.25f,1};
    if(band=="ok")return ImGui::GetStyleColorVec4(ImGuiCol_Text);
    return ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled);
}
bool stale(const SessionSnapshot& s){return s.paused||!s.data.connected||(s.telemetry_stale_mask&DC_T_PRIV_CLOCKS);}
}
void draw_clock_secondary(const SessionSnapshot& s,const char* key){
    if(!s.data.clock_monitor){ImGui::TextDisabled("B: not read yet");return;}
    const auto& data=*s.data.clock_monitor;
    for(const auto& r:data.readings)if(r.key==key){
        const uint64_t bit=r.key=="core"?DC_T_CORE:r.key=="xbar"?DC_T_XBAR:DC_T_MEM;
        const bool old=stale(s)||data.stale||r.stale||(s.telemetry_stale_mask&bit);
        const bool judged=!old&&r.state=="judged"&&r.trust=="measured"&&r.key=="core";
        const auto text=clock_secondary_text(r,old);
        ImGui::PushTextWrapPos(0);ImGui::TextColored(tint(judged?r.verdict:"",old),"%s",text.c_str());ImGui::PopTextWrapPos();
        if(ImGui::IsItemHovered()){ImGui::BeginTooltip();ImGui::PushTextWrapPos(u(500));
            ImGui::TextUnformatted(clock_secondary_explanation(r).c_str());ImGui::PopTextWrapPos();ImGui::EndTooltip();}
        return;
    }
    ImGui::TextDisabled("B: --");
}
void draw_clock_monitor_table(const SessionSnapshot& s,bool classic){
    if(!ImGui::CollapsingHeader("All clock domains (A / B)",ImGuiTreeNodeFlags_DefaultOpen))return;
    ImGui::TextWrapped("A / B are driver-reported arrays. Names: plain = confirmed; ? = inferred; -- = unidentified. "
        "2CLK rows retain doubled MHz here; tile evidence divides by row scale. PCIe domain values retain generation/raw units. "
        "Gap colors compare B - A, adjusted by row scale, with this adapter's measured graphics bin (one / three bins). "
        "This is a difference policy, not proof that B is a physical measurement.");
    if(!s.data.clock_monitor){ImGui::TextDisabled("Waiting for clock classification.");return;}
    const auto& data=*s.data.clock_monitor;const bool old=stale(s)||data.stale;
    if(old)ImGui::TextDisabled("Last reported clock rows (stale); no current gap verdict.");
    if(!data.message.empty())ImGui::TextWrapped("%s",data.message.c_str());
    if(!data.available)return;
    const auto flags=ImGuiTableFlags_RowBg|ImGuiTableFlags_BordersInnerV|ImGuiTableFlags_ScrollX|ImGuiTableFlags_ScrollY;
    if(ImGui::BeginTable("clock-domain-rows",9,flags,ImVec2(0,u(classic?250.0f:220.0f)))){
        ImGui::TableSetupColumn("Dom",ImGuiTableColumnFlags_WidthFixed,u(36));
        ImGui::TableSetupColumn("Domain",ImGuiTableColumnFlags_WidthFixed,u(125));
        ImGui::TableSetupColumn("A (reported)",ImGuiTableColumnFlags_WidthFixed,u(102));
        ImGui::TableSetupColumn("B (reported)",ImGuiTableColumnFlags_WidthFixed,u(102));
        ImGui::TableSetupColumn("B - A (MHz)",ImGuiTableColumnFlags_WidthFixed,u(100));
        ImGui::TableSetupColumn("Grade",ImGuiTableColumnFlags_WidthFixed,u(100));
        ImGui::TableSetupColumn("Scale",ImGuiTableColumnFlags_WidthFixed,u(45));
        ImGui::TableSetupColumn("Flags",ImGuiTableColumnFlags_WidthFixed,u(90));
        ImGui::TableSetupColumn("Srcid",ImGuiTableColumnFlags_WidthFixed,u(90));
        ImGui::TableSetupScrollFreeze(2,1);ImGui::TableHeadersRow();
        for(const auto& row:data.rows){
            ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::Text("%u",row.raw.domain);
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.label.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.a.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.b.c_str());
            ImGui::TableNextColumn();ImGui::TextColored(tint(row.band,old),"%s",row.delta.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.raw.grade);
            ImGui::TableNextColumn();ImGui::Text("%u",row.raw.scale);
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.flags.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.source.c_str());
        }ImGui::EndTable();
    }
}
}
