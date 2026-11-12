// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "monitor_view.h"
#include <imgui.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
namespace druta::ui {
namespace {
float u(float value){return value*ImGui::GetFontSize()/18.0f;}
ImVec4 color(MonitorFreshness f){
    return f==MonitorFreshness::stale?ImVec4(0.96f,0.65f,0.25f,1):
        f==MonitorFreshness::unavailable?ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled):ImGui::GetStyleColorVec4(ImGuiCol_Text);
}
void reading(const MonitorReading& r){
    ImGui::PushTextWrapPos(0);
    ImGui::TextColored(color(r.freshness),"%s%s%s%s",r.text.empty()?"--":r.text.c_str(),
        r.unit.empty()?"":" ",r.unit.c_str(),r.freshness==MonitorFreshness::stale?" (stale)":"");
    ImGui::PopTextWrapPos();
}
void lamps(const char* title,const std::vector<MonitorReason>& reasons,bool classic){
    if(!ImGui::CollapsingHeader(title,classic?ImGuiTreeNodeFlags_DefaultOpen:0))return;
    const float right=ImGui::GetCursorScreenPos().x+ImGui::GetContentRegionAvail().x;
    bool first=true;
    for(const auto& r:reasons){
        const char* status=!r.active?"--":*r.active?"ACTIVE":"inactive";
        const auto tint=r.active && *r.active && r.freshness==MonitorFreshness::current?ImVec4(0.96f,0.65f,0.25f,1):color(r.freshness);
        const auto text=r.label+": "+status+(r.freshness==MonitorFreshness::stale?" (stale)":"");
        if(!first && ImGui::GetItemRectMax().x+u(18)+ImGui::CalcTextSize(text.c_str()).x<right)ImGui::SameLine(0,u(18));
        ImGui::PushStyleColor(ImGuiCol_Text,tint);ImGui::TextWrapped("%s",text.c_str());ImGui::PopStyleColor();first=false;
    }
}
void group(const MonitorGroup& data,bool open,bool classic){
    if(!ImGui::CollapsingHeader(data.label.c_str(),open?ImGuiTreeNodeFlags_DefaultOpen:0))return;
    if(!ImGui::BeginTable(data.label.c_str(),2,ImGuiTableFlags_SizingStretchProp|ImGuiTableFlags_RowBg))return;
    ImGui::TableSetupColumn("Reading",ImGuiTableColumnFlags_WidthStretch,1.1f);
    ImGui::TableSetupColumn("Value",ImGuiTableColumnFlags_WidthStretch,1.0f);
    for(const auto& r:data.readings){
        ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::TextWrapped("%s",r.label.c_str());ImGui::TableNextColumn();reading(r);
        if(r.percentage && std::isfinite(*r.percentage)){
            const float fraction=static_cast<float>(std::clamp(*r.percentage/100.0,0.0,1.0));
            ImGui::ProgressBar(fraction,ImVec2(-1,u(classic?3.0f:5.0f)),"");
        }
    }
    ImGui::EndTable();
}
}
void draw_monitor_tiles(const SessionSnapshot& snapshot,const ShuntCorrection& correction,bool classic){
    auto model=build_monitor_model(snapshot);
    // Fixed units are raw driver clock units; no inferred memory divisor or
    // private-array B physical-measurement claim is introduced here.
    const int columns=std::clamp(static_cast<int>(ImGui::GetContentRegionAvail().x/u(classic?135.0f:155.0f)),1,7);
    if(ImGui::BeginTable("monitor-sensor-tiles",columns,ImGuiTableFlags_SizingStretchSame)){
        for(auto& r:model.tiles){
            ImGui::TableNextColumn();ImGui::TextDisabled("%s",r.label.c_str());
            if(r.label=="BOARD POWER" && r.freshness!=MonitorFreshness::unavailable && correction.valid && correction.active){
                const auto watts=correction.apply(snapshot.data.telemetry.power_w);
                if(watts && std::isfinite(*watts)){
                    char text[80]{};std::snprintf(text,sizeof(text),"%.1f",*watts);
                    const std::string raw=r.text;r.text=text;r.unit=correction.exact?"W corrected":"W estimate";
                    reading(r);ImGui::TextDisabled("%s W raw",raw.c_str());continue;
                }
            }
            reading(r);
        }
        ImGui::EndTable();
    }
    if(snapshot.paused)ImGui::TextWrapped("Telemetry paused; displayed readings are last known.");
    if(!snapshot.data.telemetry_error.empty())ImGui::TextWrapped("Telemetry: %s",snapshot.data.telemetry_error.c_str());
}
void draw_monitor_details(const SessionSnapshot& snapshot,bool classic){
    const auto model=build_monitor_model(snapshot);ImGui::PushID("monitor-details");
    lamps("Throttle / clocks-event reasons",model.clock_reasons,classic);
    lamps("Performance-decrease reasons (NVAPI)",model.perf_reasons,classic);
    group(model.groups[0],true,classic);
    const bool paired=ImGui::GetContentRegionAvail().x>=u(700);
    if(paired && ImGui::BeginTable("monitor-paired-details",2,ImGuiTableFlags_SizingStretchSame)){
        ImGui::TableNextColumn();group(model.groups[1],classic,classic);
        ImGui::TableNextColumn();group(model.groups[2],classic,classic);
        ImGui::EndTable();
    }else if(!paired){group(model.groups[1],classic,classic);group(model.groups[2],classic,classic);}
    ImGui::PopID();
}
}
