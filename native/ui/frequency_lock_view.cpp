// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "frequency_lock_view.h"
#include "classic_layout.h"
#include "imgui.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
namespace druta::ui {
namespace {
void sync_frequency_lock(FrequencyLockViewState& state,const FrequencyLockData& data,const FrequencyLockViewContext& context) {
    if(state.generation!=context.generation || state.slot!=context.slot || state.uuid!=context.uuid) {
        const bool open=state.open;state={};state.open=open;state.generation=context.generation;state.slot=context.slot;state.uuid=context.uuid;
    }
    if(!state.initialized && data.minimum_mhz && data.maximum_mhz) {
        state.minimum_mhz=*data.minimum_mhz;state.maximum_mhz=*data.maximum_mhz;state.initialized=true;
    }
    if(data.ownership.mode==FrequencyLockMode::nvml && data.ownership.confirmed && data.ownership.range) {
        if(!state.dirty && (!state.observed_owned || !(*state.observed_owned==*data.ownership.range))) {
            state.minimum_mhz=data.ownership.range->minimum;state.maximum_mhz=data.ownership.range->maximum;state.initialized=true;
        }
        state.observed_owned=data.ownership.range;
    }
}
bool range_ok(const FrequencyLockViewState& state,const FrequencyLockData& data) {
    return state.initialized && data.minimum_mhz && data.maximum_mhz && state.minimum_mhz>=*data.minimum_mhz
        && state.minimum_mhz<=state.maximum_mhz && state.maximum_mhz<=*data.maximum_mhz;
}
bool lock_capable(const FrequencyLockData& data,const FrequencyLockViewContext& context) {
    const bool ready=context.connected && !context.busy && context.unlocked;
    const bool owned_or_free=data.current_known && (!data.current || data.ownership.mode==FrequencyLockMode::nvml || context.other_owned_clock);
    return ready && data.nvml_capable && data.tables_current && owned_or_free;
}
}
std::vector<FrequencyLockIntent> draw_frequency_lock_menu(FrequencyLockViewState& state,const FrequencyLockData& data,
    const FrequencyLockViewContext& context,int64_t step_mhz,bool vf_applicable,uint32_t grid_khz) {
    std::vector<FrequencyLockIntent> out;
    sync_frequency_lock(state,data,context);
    ImGui::TextColored(classic_accent_text,"GPU CLOCK LOCK");
    // NVML architecture numbers, as 1.9.1's GPU.ARCH_KEPLER / MAXWELL / PASCAL.
    const auto arch=data.architecture.value_or(0);
    if(arch==2)ImGui::TextColored(classic_dim_text,"NVML GPU and memory clock locks are unavailable on Kepler.");
    else if(arch==3)ImGui::TextColored(classic_dim_text,"NVML GPU frequency locks are unavailable on Maxwell.\nUse Lock P0 and max fan for a performance-state hold.");
    else if(arch==4)ImGui::TextColored(classic_dim_text,"NVML frequency locking is unavailable on Pascal.\nUse Ctrl+H on the V/F curve to hold a point.");
    else if(data.minimum_mhz && data.maximum_mhz)ImGui::TextColored(classic_dim_text,"%u-%u MHz lockable",*data.minimum_mhz,*data.maximum_mhz);
    else ImGui::TextColored(classic_dim_text,"%s",data.message.empty()?"Lockable range not read yet.":data.message.c_str());
    ImGui::Separator();
    const bool unlocked=context.unlocked && !context.busy;
    ImGui::BeginDisabled(!unlocked);
    const int64_t step=std::max<int64_t>(1,step_mhz);
    const auto clamp=[&](int64_t& value){
        if(data.minimum_mhz && data.maximum_mhz)value=std::clamp<int64_t>(value,*data.minimum_mhz,*data.maximum_mhz);
    };
    ImGui::SetNextItemWidth(classic_px(130));
    if(ImGui::InputScalar("min MHz",ImGuiDataType_S64,&state.minimum_mhz,&step)){clamp(state.minimum_mhz);state.dirty=state.initialized=true;}
    ImGui::SetNextItemWidth(classic_px(130));
    if(ImGui::InputScalar("max MHz",ImGuiDataType_S64,&state.maximum_mhz,&step)){clamp(state.maximum_mhz);state.dirty=state.initialized=true;}
    ImGui::EndDisabled();
    ImGui::Dummy({0,classic_px(4)});
    const bool capable=lock_capable(data,context);
    ImGui::BeginDisabled(!capable || !range_ok(state,data));
    if(ImGui::Button("Lock",ImVec2(classic_px(90),0))){out.push_back({FrequencyLockAction::apply,state.minimum_mhz,state.maximum_mhz});state.dirty=false;ImGui::CloseCurrentPopup();}
    ImGui::EndDisabled();ImGui::SameLine();
    ImGui::BeginDisabled(!unlocked || !data.ownership.needs_cleanup());
    if(ImGui::Button("Release",ImVec2(classic_px(90),0))){out.push_back({FrequencyLockAction::release});ImGui::CloseCurrentPopup();}
    ImGui::EndDisabled();ImGui::SameLine();
    ImGui::BeginDisabled(!capable || !data.maximum_mhz);
    if(ImGui::Button("Lock max",ImVec2(classic_px(100),0))){out.push_back({FrequencyLockAction::lock_max});state.dirty=false;ImGui::CloseCurrentPopup();}
    ImGui::EndDisabled();
    if(ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)){
        ImGui::BeginTooltip();
        ImGui::TextUnformatted(frequency_lock_max_tooltip(data.maximum_mhz,grid_khz).c_str());
        ImGui::EndTooltip();
    }
    if(vf_applicable)ImGui::TextColored(classic_dim_text,"%s",frequency_lock_menu_note);
    return out;
}
std::string frequency_lock_max_tooltip(const std::optional<uint32_t>& maximum_mhz,uint32_t grid_khz) {
    // Python writes the V/F clock as floor((base+delta)/15)*15; the step is the
    // adapter's own clock grid (the V/F editor's), or "step" while it is unknown.
    char grid[32]="step";
    if(grid_khz)std::snprintf(grid,sizeof(grid),"%.4g",grid_khz/1000.0);
    std::string text=maximum_mhz?"Pins both ends to "+std::to_string(*maximum_mhz)+" MHz - the top of the\ndriver's LOCKABLE table."
        :std::string("Pins both ends to the top of the driver's\nLOCKABLE table (not read yet).");
    return text+"\n\nThat table is not a boost ceiling: the V/F curve\nreaches clocks above it (it is floor((base+delta)\n/"+grid+")*"+grid
        +", never checked against this list). So if\nthe card is already boosting past it, locking max\nwill LOWER the clock. This is for holding one\n"
        "frequency steady, not for going fast.";
}
bool legacy_p0_capable(const FrequencyLockData& data,const FrequencyLockViewContext& context) {
    return context.connected && !context.busy && context.unlocked && data.legacy_known && data.legacy_available
        && context.fan_manual_available;
}
std::vector<FrequencyLockIntent> draw_legacy_p0_gate(const FrequencyLockData& data,const FrequencyLockViewContext& context) {
    std::vector<FrequencyLockIntent> out;
    const auto rgb=[](int r,int g,int b){return ImVec4(r/255.0f,g/255.0f,b/255.0f,1);};
    // Released 1.9.1 p0_th: yellow with hover / active shades and dark label text.
    ImGui::PushStyleColor(ImGuiCol_Button,rgb(230,200,35));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,rgb(250,222,60));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,rgb(205,174,20));
    ImGui::PushStyleColor(ImGuiCol_Text,rgb(24,16,6));
    ImGui::BeginDisabled(!legacy_p0_capable(data,context));
    if(ImGui::Button("Lock P0 and max fan",ImVec2(classic_px(250),classic_px(28))))out.push_back({FrequencyLockAction::legacy_p0_max_fan});
    ImGui::EndDisabled();
    ImGui::PopStyleColor(4);
    if(ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)){
        ImGui::BeginTooltip();ImGui::PushTextWrapPos(classic_px(540));
        // 1.9.1's text with its no-profile measurement line: this build keeps no
        // per-card held-clock measurement.
        ImGui::TextUnformatted("Holds P0, verifies the hold, then sets fan duty to 100%.\n"
            "Clock offsets, power and voltage settings stay as set.\n\n"
            "Maximum core boost is not guaranteed by a performance-state hold.\n"
            "This is a performance-state hold, not maximum\n"
            "boost. It keeps memory in its top band for timing work.\n\n"
            "Undo last write restores the fan but leaves P0 held.\n"
            "Release P0, Clocks > Release, Reset all, or exit drops\n"
            "the hold. Fan duty stays manual until Auto or Reset all.\n"
            "If setting the fan fails, a newly acquired hold is released.");
        if(!context.fan_manual_available)ImGui::TextUnformatted("\nManual fan control is unavailable; P0 + fan will not start.");
        ImGui::PopTextWrapPos();ImGui::EndTooltip();
    }
    ImGui::SameLine();
    const bool own_p0=data.ownership.mode==FrequencyLockMode::legacy_p0 && data.ownership.needs_cleanup();
    ImGui::BeginDisabled(!context.connected || context.busy || !context.unlocked || !own_p0);
    if(ImGui::Button("Release P0",ImVec2(classic_px(110),classic_px(28))))out.push_back({FrequencyLockAction::release});
    ImGui::EndDisabled();
    return out;
}
std::vector<FrequencyLockIntent> draw_frequency_lock(FrequencyLockViewState& state,const FrequencyLockData& data,
    const FrequencyLockViewContext& context) {
    std::vector<FrequencyLockIntent> out;
    sync_frequency_lock(state,data,context);
    if(!state.open)return out;
    ImGui::SetNextWindowSize({740,560},ImGuiCond_FirstUseEver);
    if(ImGui::Begin("Clock requests###druta_frequency_lock",&state.open,ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::Text("Selected GPU: %s | %s",context.slot.c_str(),context.uuid.empty()?"UUID unavailable":context.uuid.c_str());
        ImGui::TextWrapped("Session-only. Acquiring a lock first releases this app's other owned clock controls.");
        ImGui::BeginDisabled(context.busy);
        if(ImGui::Button("Read clock capabilities"))out.push_back({FrequencyLockAction::refresh});
        ImGui::EndDisabled();
        if(!data.message.empty())ImGui::TextWrapped("%s",data.message.c_str());
        if(!data.tables_current && !data.rows.empty())ImGui::TextWrapped("Captured lockable lists are retained; current capability read is unverified. Retry before applying.");
        if(data.current_known) {
            if(data.current)ImGui::Text("Current frequency request: [%u..%u] MHz",data.current->minimum,data.current->maximum);
            else ImGui::TextUnformatted("Current frequency request: unlocked");
        } else ImGui::TextDisabled("Current frequency request: unreadable (not assumed unlocked)");
        if(data.ownership.needs_cleanup())ImGui::Text("This session owns %s%s%s",
            data.ownership.mode==FrequencyLockMode::legacy_p0?"legacy P0":"an NVML frequency request",
            data.ownership.confirmed?" (confirmed)":" (unconfirmed)",data.ownership.recovery?"; recovery/release required":"");
        ImGui::SeparatorText("NVML frequency request");
        ImGui::BeginDisabled(context.busy);
        ImGui::SetNextItemWidth(140);
        if(ImGui::InputScalar("Minimum MHz",ImGuiDataType_S64,&state.minimum_mhz))state.dirty=state.initialized=true;
        ImGui::SameLine();ImGui::SetNextItemWidth(140);
        if(ImGui::InputScalar("Maximum MHz",ImGuiDataType_S64,&state.maximum_mhz))state.dirty=state.initialized=true;
        ImGui::EndDisabled();
        const bool range=range_ok(state,data);
        const bool capable=lock_capable(data,context);
        ImGui::BeginDisabled(!capable || !range);
        if(ImGui::Button("Apply frequency range")){out.push_back({FrequencyLockAction::apply,state.minimum_mhz,state.maximum_mhz});state.dirty=false;}
        ImGui::EndDisabled();ImGui::SameLine();
        ImGui::BeginDisabled(!capable || !data.maximum_mhz);
        if(ImGui::Button("Lock maximum reported clock")){out.push_back({FrequencyLockAction::lock_max});state.dirty=false;}
        ImGui::EndDisabled();
        if(data.minimum_mhz && data.maximum_mhz)ImGui::Text("Reported lockable bounds: %u..%u MHz",*data.minimum_mhz,*data.maximum_mhz);
        ImGui::TextWrapped("Requests snap DOWN to the top-memory table. Its maximum is not the V/F boost ceiling.");
        if(context.live_core_mhz && data.maximum_mhz && std::isfinite(*context.live_core_mhz) && *context.live_core_mhz>*data.maximum_mhz)
            ImGui::Text("Current core %.3f MHz is above the lockable maximum %u MHz; lock-max would step it down.",*context.live_core_mhz,*data.maximum_mhz);
        if(ImGui::CollapsingHeader("Lockable clocks by memory clock")) {
            for(const auto& row:data.rows) {
                ImGui::Text("Memory %u MHz: %zu graphics clocks",row.memory_mhz,row.graphics_mhz.size());
                std::string values;for(uint32_t clock:row.graphics_mhz){if(!values.empty())values+=", ";values+=std::to_string(clock);}
                ImGui::TextWrapped("%s",values.c_str());
            }
        }
        ImGui::SeparatorText("Legacy P0 and maximum fan");
        ImGui::TextWrapped("Verify P0 and its top memory band, then request 100% fan. Not a maximum-core lock.");
        if(!context.fan_manual_available)ImGui::TextDisabled("Manual fan policy is unavailable; P0 + fan will not start.");
        ImGui::BeginDisabled(!legacy_p0_capable(data,context));
        if(ImGui::Button("Lock P0 and max fan"))out.push_back({FrequencyLockAction::legacy_p0_max_fan});
        ImGui::EndDisabled();
        ImGui::BeginDisabled(context.busy || !data.ownership.needs_cleanup());
        if(ImGui::Button("Release owned clock / P0 request"))out.push_back({FrequencyLockAction::release});
        ImGui::EndDisabled();
        ImGui::TextWrapped("Release checks the exact tracked request. P0 release leaves fan policy unchanged; use fan Undo / Auto.");
        if(ImGui::CollapsingHeader("Clock request details")) {
            ImGui::TextWrapped("NVML frequency locking and V/F point holding are different mechanisms. A failed or uncertain handover blocks acquisition. Ownership follows this session's unchanged exact request; NVML exposes no client-owner ID. Observed replacement requests are left untouched.");
            ImGui::TextWrapped("Legacy P0 support is probed independently for Kepler/Maxwell. P0 plus its top memory band does not guarantee maximum core boost. Fan Undo is captured before either part of P0 + maximum fan.");
            ImGui::TextWrapped("Unreadable release readback retains recovery. No request is saved or automatically reapplied at startup or after driver reset.");
        }
    }
    ImGui::End();return out;
}
}
