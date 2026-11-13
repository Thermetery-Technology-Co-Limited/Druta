// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "i2c_view.h"
#include "ui_view.h"
#include "session.h"
#include <imgui_internal.h>
#include <cstdio>
#include <string>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool value,const char* label){if(!value){++failures;std::printf("FAIL: %s\n",label);}}
std::string frame(ViewState& state,const char* activate=nullptr){
    ImGui::GetIO().DisplaySize={1400,1100};ImGui::NewFrame();ImGui::SetNextWindowSize({1200,1000});ImGui::Begin("Discovery host");
    if(activate){auto* w=ImGui::GetCurrentWindow();const auto scope=ImHashStr("i2c-controller",0,w->ID);const auto id=ImHashStr(activate,0,scope);
        auto& context=*ImGui::GetCurrentContext();context.NavActivateId=context.NavActivateDownId=context.NavActivatePressedId=id;}
    ImGui::LogToBuffer();draw_i2c_panel(state);std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish();ImGui::End();ImGui::Render();return text;
}
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);
    ViewState state;auto live=std::make_shared<SessionSnapshot>();live->data.connected=true;live->busy=true;live->generation=8;
    state.live=live;live->data.i2c.available=true;auto& discovery=live->data.i2c.discovery;
    discovery.running=true;discovery.total=48;discovery.completed=7;discovery.label="NCT3933U read-only probe";
    I2cCandidate candidate;candidate.family=1;candidate.route={2,0x12};discovery.candidates={candidate};
    for(auto skin:{Presentation::classic,Presentation::focused}) {
        state.presentation=skin;state.i2c_cancel_requested=false;state.commands.clear();frame(state);
        const auto text=frame(state,"Cancel discovery");
        check(state.i2c_cancel_requested && state.commands.empty(),"Cancel reaches dedicated host flag while worker busy; no queued hardware command");
        check(text.find("7 / 48 read-only probes")!=std::string::npos && text.find("read-only candidates")!=std::string::npos,"both skins show bounded progress and unverified candidate status");
        state.commands.clear();frame(state,"Connect / Scan");check(state.commands.empty(),"busy progress cannot start overlapping discovery");
    }
    discovery.running=false;discovery.cancelled=true;discovery.label="Cancelled; partial candidates are unverified";live->busy=false;
    state.i2c_cancel_requested=false;frame(state,"Cancel discovery");
    check(!state.i2c_cancel_requested && state.commands.empty(),"finished/cancelled scan has no stale Cancel action");
    frame(state,"Connect / Scan");check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_scan && !state.commands.back().i2c_full_scan,"Connect remains distinct scoped explicit action");
    state.commands.clear();frame(state,"Full scan");check(state.commands.size()==1 && state.commands.back().i2c_full_scan,"Full scan remains explicit all-family action");
    ImGui::DestroyContext();std::printf("I2C discovery view: %d failures\n",failures);return failures?1:0;
}
