/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_view.h"
#include "ui_view.h"
#include "session.h"
#include <imgui.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
namespace druta::ui {
namespace {
bool ready(const ViewState& state) { return state.live && state.live->data.connected && !state.live->busy && !state.live->opening; }
void queue(ViewState& state,Command::Operation op,double value=0) { Command c; c.operation=op; c.value=value; if(state.live)c.i2c_route=state.live->data.i2c.selected; state.commands.push_back(c); }
}
void draw_i2c_gate(ViewState& state) {
    bool enabled=state.live && state.live->data.i2c.gate;
    ImGui::BeginDisabled(!ready(state) || !state.controls_unlocked || !state.live->data.i2c.selected);
    if (ImGui::Checkbox("I2C rail",&enabled)) { Command c; c.operation=Command::Operation::i2c_gate; c.enabled=enabled; c.i2c_route=state.live->data.i2c.selected; state.commands.push_back(c); }
    ImGui::EndDisabled();
}
void draw_i2c_panel(ViewState& state) {
    ImGui::PushID("i2c-controller");
    if (!state.live || !state.live->data.i2c.available) { ImGui::TextDisabled("I2C transport unavailable"); ImGui::PopID(); return; }
    const auto& data=state.live->data.i2c;
    const auto& status=data.status;
    const bool owned=status.owned || status.recovery_pending || data.clock_owned;
    const bool can=ready(state);
    const bool recover=state.live && !state.live->busy && !state.live->opening;
    ImGui::BeginDisabled(!can || owned);
    if (ImGui::Button("Scan controllers")) queue(state,Command::Operation::i2c_scan);
    ImGui::SameLine(); ImGui::SetNextItemWidth(95); ImGui::InputInt("Port",&state.i2c_port,0);
    ImGui::SameLine(); ImGui::SetNextItemWidth(95); ImGui::InputInt("Address",&state.i2c_address,0,0,ImGuiInputTextFlags_CharsHexadecimal);
    ImGui::SameLine();
    if (ImGui::Button("Probe route") && state.i2c_port>=0 && state.i2c_port<=7 && state.i2c_address>=8 && state.i2c_address<=0x77) {
        Command c; c.operation=Command::Operation::i2c_select;
        c.i2c_route=I2cRoute{static_cast<uint8_t>(state.i2c_port),static_cast<uint8_t>(state.i2c_address)}; state.commands.push_back(c);
    }
    for (const auto& candidate:data.candidates) {
        char label[128]{}; std::snprintf(label,sizeof(label),"Port %u / 0x%02X - MP2888A candidate",candidate.route.port,candidate.route.addr7);
        if (ImGui::Selectable(label,data.selected && data.selected->port==candidate.route.port && data.selected->addr7==candidate.route.addr7)) {
            Command c; c.operation=Command::Operation::i2c_select; c.i2c_route=candidate.route; state.commands.push_back(c);
        }
    }
    ImGui::EndDisabled();
    if (data.selected) {
        ImGui::Text("Port %u / 0x%02X | MP2888A candidate | physical rail unassigned",data.selected->port,data.selected->addr7);
        ImGui::BeginDisabled(!recover);
        if (ImGui::Button("Read controller")) queue(state,Command::Operation::i2c_read);
        ImGui::EndDisabled(); ImGui::SameLine();
        if (status.controller_vout_mv) ImGui::Text("VOUT %.0f mV%s",*status.controller_vout_mv,status.stale?" (last read)":" (controller)");
        else ImGui::TextDisabled("VOUT unavailable");
        ImGui::BeginDisabled(!can || !state.controls_unlocked || !data.gate || owned || !status.capable);
        if (ImGui::Button(data.verified?"Verify again":"Verify response")) queue(state,Command::Operation::i2c_verify);
        ImGui::EndDisabled(); ImGui::SameLine(); ImGui::TextDisabled("Brief A-B-A test at the driver's lowest clock");
        if (status.min_mv && status.max_mv) {
            const double low=*status.min_mv,high=*status.max_mv;
            ImGui::SetNextItemWidth(320);
            if (ImGui::SliderScalar("Manual command (mV)",ImGuiDataType_Double,&state.i2c_target_mv,&low,&high,"%.2f"))
                state.i2c_target_mv=std::clamp(std::round(state.i2c_target_mv/6.25)*6.25,low,high);
            ImGui::SameLine(); ImGui::SetNextItemWidth(110); ImGui::InputDouble("##manual-mv",&state.i2c_target_mv,0,0,"%.2f");
            ImGui::SameLine(); ImGui::BeginDisabled(!can || !state.controls_unlocked || !data.gate || !data.verified || !status.writable);
            if (ImGui::Button("Apply")) queue(state,Command::Operation::i2c_apply,state.i2c_target_mv);
            ImGui::EndDisabled();
        }
        ImGui::BeginDisabled(!recover || !owned);
        if (ImGui::Button("Release / restore captured state")) queue(state,Command::Operation::i2c_release);
        ImGui::EndDisabled();
        if (status.requested_mv) { ImGui::SameLine(); ImGui::Text("Command %.2f mV",*status.requested_mv); }
        if (data.clock_owned) ImGui::Text("Owned graphics clamp: %u MHz",data.clock_mhz);
    }
    if (!data.message.empty()) ImGui::TextWrapped("%s",data.message.c_str());
    ImGui::PopID();
}
}
