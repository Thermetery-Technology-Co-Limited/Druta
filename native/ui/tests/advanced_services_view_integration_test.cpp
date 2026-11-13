// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless integration of the actual Advanced pages; no backend or GPU.
#include "../ui_view.h"
#include "../session.h"
#include "imgui.h"
#include "imgui_internal.h"
#include "implot.h"
#include <cstdio>
#include <cstring>
#include <string>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* message){if(!ok){++failures;std::printf("FAIL: %s\n",message);}}
std::string frame(ViewState& state){
    auto& io=ImGui::GetIO();io.DisplaySize={1920,1600};ImGui::NewFrame();ImGui::LogToBuffer();draw(state);
    std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::Render();return text;
}
ImGuiWindow* advanced_window(Presentation presentation){
    auto* root=ImGui::FindWindowByName("Druta workspace");if(!root)return nullptr;
    ImGuiWindow* workspace=nullptr;
    for(auto* w:ImGui::GetCurrentContext()->Windows)
        if(w->ParentWindow==root && w->ChildId==root->GetID("workspace"))workspace=w;
    if(presentation==Presentation::focused)return workspace;
    if(workspace)for(auto* w:ImGui::GetCurrentContext()->Windows)
        if(w->ParentWindow==workspace && w->ChildId==workspace->GetID("classic-advanced-columns"))return w;
    return nullptr;
}
ImGuiID child_id(ImGuiWindow* w,const char* scope,const char* label){return ImHashStr(label,0,ImHashStr(scope,0,w->IDStack[0]));}
void open_header(ImGuiWindow* w,const char* scope,const char* label){
    const auto id=scope?child_id(w,scope,label):w->GetID(label);w->StateStorage.SetInt(id,1);
}
void activate(ViewState& state,ImGuiID id,Command::Operation expected,const char* why){
    state.commands.clear();ImGui::ActivateItemByID(id);frame(state);
    check(state.commands.size()==1 && state.commands[0].operation==expected,why);
}
void pages(Presentation presentation){
    ViewState state;state.preview=false;state.page=Page::advanced;state.presentation=presentation;
    auto live=std::make_shared<SessionSnapshot>();live->generation=1;live->selected_slot="0000:09:00.0";
    live->data.connected=true;std::strcpy(live->data.identity.uuid,"GPU-alternate");state.live=live;
    auto text=frame(state);auto* w=advanced_window(presentation);
    auto* root=ImGui::FindWindowByName("Druta workspace");
    check(root!=nullptr,"profile host exists");
    if(root){
        const auto catalog_id=root->GetID(presentation==Presentation::classic?"Profiles...":"Profile catalog...");
        ImGui::ActivateItemByID(catalog_id);text=frame(state);
        check(state.profile_catalog_view.open && text.find("Choose profile files...")!=std::string::npos,"header opens actual catalog in both skins");
        check(state.commands.empty() && state.profile_catalog_intents.empty(),"opening catalog neither reads hardware nor applies settings");
        state.profile_catalog_view.open=false;text=frame(state);
    }
    check(w!=nullptr,"Advanced content window exists");if(!w)return;
    check(text.find("Power feedback ratios")!=std::string::npos && text.find("Voltage admission and V/F policies")!=std::string::npos && text.find("Power arbitration")!=std::string::npos,"Advanced exposes all service headers");
    check(state.commands.empty(),"drawing does not dispatch commands");
    state.voltage_tables_view.initialized=true;state.voltage_tables_view.generation=1;
    state.voltage_tables_view.drafts.push_back({});state.voltage_tables_view.drafts.back().dirty=true;std::strcpy(state.voltage_tables_view.drafts.back().text.data(),"17.25");
    state.voltage_limits_view.initialized=true;state.voltage_limits_view.generation=1;state.voltage_limits_view.drafts.push_back({});state.voltage_limits_view.drafts.back().dirty=true;state.voltage_limits_view.drafts.back().value=321;
    state.open_voltage_tables_requested=true;text=frame(state);
    check(!state.open_voltage_tables_requested&&text.find("Read voltage requests")!=std::string::npos,"table navigation opens the actual panel");
    check(state.commands.empty()&&state.voltage_tables_intents.empty()&&state.voltage_limits_intents.empty()&&state.voltage_tables_view.drafts[0].dirty&&std::strcmp(state.voltage_tables_view.drafts[0].text.data(),"17.25")==0,"table navigation preserves drafts and issues no commands");
    state.open_voltage_limits_requested=true;text=frame(state);
    check(!state.open_voltage_limits_requested&&text.find("Read voltage device delays")!=std::string::npos,"definitions navigation opens the actual panel");
    check(state.commands.empty()&&state.voltage_tables_intents.empty()&&state.voltage_limits_intents.empty()&&state.voltage_limits_view.drafts[0].dirty&&state.voltage_limits_view.drafts[0].value==321,"definitions navigation preserves drafts and issues no commands");
    state.open_voltage_policy_requested=true;text=frame(state);check(!state.open_voltage_policy_requested&&text.find("Read voltage policies")!=std::string::npos,"policy navigation remains available");
    state.open_power_integral_requested=true;text=frame(state);check(!state.open_power_integral_requested&&text.find("Read feedback requests")!=std::string::npos&&state.commands.empty(),"integral navigation remains command-free");
    open_header(w,"advanced-semantic-controls","Power feedback ratios");
    open_header(w,"advanced-semantic-controls","Voltage admission and V/F policies");
    open_header(w,nullptr,"Power arbitration");
    text=frame(state);
    check(text.find("Read feedback requests")!=std::string::npos && text.find("Refresh power arbitration")!=std::string::npos && text.find("Read voltage policies")!=std::string::npos,"expanded panels draw actual read widgets");
    const auto sem=ImHashStr("advanced-semantic-controls",0,w->IDStack[0]);
    const auto feedback=ImHashStr("Read feedback requests",0,ImHashStr("power_integral_panel",0,sem));
    const auto arbitration=child_id(w,"power-arbitration","Refresh power arbitration");
    const auto voltage=ImHashStr("Read voltage policies",0,ImHashStr("voltage-policy",0,sem));
    check(feedback!=arbitration && feedback!=voltage && voltage!=arbitration,"service read widgets have distinct IDs");
    activate(state,feedback,Command::Operation::power_integral_read,"feedback read dispatches exactly once");
    activate(state,arbitration,Command::Operation::refresh_power_arbitration,"arbitration refresh dispatches exactly once");
    activate(state,voltage,Command::Operation::voltage_policy_read,"voltage read dispatches exactly once");
    for(int mode=0;mode<2;++mode){live->busy=mode==0;live->data.connected=mode!=1;state.commands.clear();
        ImGui::ActivateItemByID(feedback);frame(state);ImGui::ActivateItemByID(arbitration);frame(state);ImGui::ActivateItemByID(voltage);frame(state);
        check(state.commands.empty(),"busy or disconnected disables service reads");}
}
}
int main(){ImGui::CreateContext();ImPlot::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60.0f;
    unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);apply_theme(1);
    pages(Presentation::focused);pages(Presentation::classic);ImPlot::DestroyContext();ImGui::DestroyContext();
    std::printf("Advanced view integration: %d failures\n",failures);return failures?1:0;}
