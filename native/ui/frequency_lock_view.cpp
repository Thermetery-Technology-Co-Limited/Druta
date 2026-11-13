// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "frequency_lock_view.h"
#include "imgui.h"
#include <cmath>
namespace druta::ui {
std::vector<FrequencyLockIntent> draw_frequency_lock(FrequencyLockViewState& state,const FrequencyLockData& data,
    const FrequencyLockViewContext& context) {
    std::vector<FrequencyLockIntent> out;
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
        const bool ready=context.connected && !context.busy && context.unlocked;
        ImGui::SeparatorText("NVML frequency request");
        ImGui::BeginDisabled(context.busy);
        ImGui::SetNextItemWidth(140);
        if(ImGui::InputScalar("Minimum MHz",ImGuiDataType_S64,&state.minimum_mhz))state.dirty=state.initialized=true;
        ImGui::SameLine();ImGui::SetNextItemWidth(140);
        if(ImGui::InputScalar("Maximum MHz",ImGuiDataType_S64,&state.maximum_mhz))state.dirty=state.initialized=true;
        ImGui::EndDisabled();
        const bool range=state.initialized && data.minimum_mhz && data.maximum_mhz && state.minimum_mhz>=*data.minimum_mhz
            && state.minimum_mhz<=state.maximum_mhz && state.maximum_mhz<=*data.maximum_mhz;
        const bool owned_or_free=data.current_known && (!data.current || data.ownership.mode==FrequencyLockMode::nvml || context.other_owned_clock);
        const bool capable=ready && data.nvml_capable && data.tables_current && owned_or_free;
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
        ImGui::BeginDisabled(!ready || !data.legacy_known || !data.legacy_available || !context.fan_manual_available);
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
