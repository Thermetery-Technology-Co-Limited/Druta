/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
// Headless: this exercises command intents, never a GPU or bus transport.
#include "../i2c_view.h"
#include "../i2c_catalog.h"
#include "../ui_view.h"
#include "../session.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cmath>
#include <cstdio>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* message) { if(!ok) { ++failures; std::printf("FAIL: %s\n",message); } }
std::string frame(ViewState& state) {
    ImGui::GetIO().DisplaySize={1300,1000}; ImGui::NewFrame();
    ImGui::SetNextWindowPos({0,0}); ImGui::SetNextWindowSize({1300,1000});
    ImGui::Begin("I2C test",nullptr,ImGuiWindowFlags_NoSavedSettings);
    ImGui::LogToBuffer(); draw_i2c_gate(state); draw_i2c_panel(state);
    std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish(); ImGui::End(); ImGui::Render(); return text;
}
ImGuiID button(const char* label) {
    auto* w=ImGui::FindWindowByName("I2C test");
    return ImHashStr(label,0,ImHashStr("i2c-controller",0,w->ID));
}
ImGuiID row_button(const char* field,const char* label) {
    const auto table=ImHashStr("i2c-knobs",0,button(""));
    return ImHashStr(label,0,ImHashStr(field,0,table));
}
void activate(ViewState& state,ImGuiID id) { ImGui::ActivateItemByID(id); frame(state); }
void bound(const Command& c,const I2cViewData& data,const char* message) {
    check(c.i2c_route && c.i2c_route->port==data.selected->port && c.i2c_route->addr7==data.selected->addr7 &&
        c.i2c_family==data.family && c.i2c_selection_revision==data.selection_revision,message);
}
}
int main() {
    IMGUI_CHECKVERSION(); ImGui::CreateContext(); auto& io=ImGui::GetIO(); io.IniFilename=nullptr;
    unsigned char* pixels; int width,height; io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);
    ViewState state; auto live=std::make_shared<SessionSnapshot>(); state.live=live;
    live->generation=4; live->data.connected=true; auto& data=live->data.i2c; data.available=true;
    frame(state);
    activate(state,ImGui::FindWindowByName("I2C test")->GetID("I2C rail"));
    check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_gate && state.commands.back().enabled,
        "gate can be enabled before selecting any route");
    state.commands.clear(); state.i2c_scan_scope=2; frame(state);
    activate(state,button("Connect / Scan"));
    check(state.commands.size()==1 && state.commands.back().i2c_family==I2cFamily::ncp4206 && !state.commands.back().i2c_full_scan,
        "Connect preserves the requested family scope");
    state.commands.clear(); activate(state,button("Full scan"));
    check(state.commands.size()==1 && state.commands.back().i2c_full_scan,"Full scan is a distinct explicit command");
    data.gate=true; data.selected=I2cRoute{3,0x29}; data.selection_revision=9; data.family=I2cFamily::icx;
    data.controller.capable=true; data.controller.verified=true; data.controller.needs_verification=false;
    I2cField nv; nv.key="nvvdd"; nv.label="NVVDD request"; nv.unit="mV"; nv.value=950; nv.minimum=750; nv.maximum=1150; nv.step=5; nv.writable=true; nv.resettable=true; nv.reset_label="Auto";
    auto fb=nv; fb.key="fbvdd"; fb.label="FBVDD request"; fb.value=1300; fb.minimum=1250; fb.maximum=1450; fb.resettable=false;
    data.controller.fields={nv,fb}; data.captured_preset=true; data.pending_preset=true; data.preset_preview="NVVDD frequency: 600 kHz";
    state.commands.clear(); auto text=frame(state);
    check(text.find("Check iCX identity")!=std::string::npos && text.find("Read settings")!=std::string::npos && text.find(data.preset_preview)!=std::string::npos,
        "iCX identity/read actions and separate preset preview are visible");
    check(state.commands.empty(),"drawing controller fields and presets never probes or applies");
    activate(state,row_button("nvvdd","Apply"));
    check(state.commands.empty(),"iCX Apply requires an explicitly staged row rather than an initial slider value");
    state.i2c_field_drafts[0].second.value=975; state.i2c_field_drafts[0].second.dirty=true;
    activate(state,row_button("nvvdd","Apply"));
    check(state.commands.size()==1 && state.commands.back().i2c_field=="nvvdd" && state.commands.back().value==975,
        "NVVDD Apply emits only that row's staged request");
    if(!state.commands.empty()) bound(state.commands.back(),data,"field command binds route, family and selection revision");
    state.commands.clear(); data.controller.fields={fb}; frame(state);
    check(state.i2c_field_cache.size()==2 && state.i2c_field_drafts[0].second.value==975,
        "partial read retains omitted field and staged edit");
    data.controller.fields.clear(); data.controller.stale=true; frame(state);
    check(state.i2c_field_cache.size()==2 && state.i2c_field_drafts[0].second.value==975,
        "failed read retains rows and staged values");
    activate(state,button("Save current iCX preset"));
    check(state.i2c_save_profile_requested && state.commands.empty(),"Save requests a real file dialog without applying settings");
    activate(state,button("Load iCX preset"));
    check(state.i2c_load_profile_requested && state.commands.empty(),"Load requests a real file dialog without applying settings");
    activate(state,button("Restore captured iCX settings"));
    check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_stage_captured,
        "Restore captured settings stages the connection capture");
    state.commands.clear(); activate(state,button("Apply iCX preset"));
    check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_apply_profile,
        "preset Apply is a separate explicit action");
    state.commands.clear(); state.controls_unlocked=false; activate(state,button("Check iCX identity"));
    check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_verify && !state.commands.back().enabled,
        "read-only iCX identity check never enables the write gate");
    state.commands.clear(); live->data.connected=false; data.available=false; data.controller.owned=true; frame(state);
    activate(state,button("Release / restore captured state"));
    check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_release,
        "Release remains reachable when locked and disconnected");
    state.commands.clear(); data.controller.owned=false; data.available=true; live->data.connected=true;
    ++data.selection_revision; data.controller.stale=false; data.controller.fields={nv}; frame(state);
    check(state.i2c_field_cache.size()==1 && state.i2c_field_drafts.size()==1 && state.i2c_field_drafts[0].second.value==950 && !state.i2c_field_drafts[0].second.dirty,
        "new selection revision clears previous route's staged edits");
    check(state.commands.empty(),"selection observation does not emit a command");
    state.controls_unlocked=true; data.family=I2cFamily::mp2888; ++data.selection_revision;
    data.control_profile="mp2888a-offset";
    data.control_profiles={{"mp2888a-offset","MP2888A (ASUS Turing)","",true,6.25,{}},
        {"mp2888a-pmbus","MP2888A (ASUS Ampere)","",true,6.25,{}}};
    data.controller.fields.clear(); frame(state); activate(state,button("Control recipe"));
    auto* context=ImGui::GetCurrentContext();
    check(!context->OpenPopupStack.empty(),"MP2888A exposes both selectable legacy recipes");
    if(!context->OpenPopupStack.empty() && context->OpenPopupStack.back().Window) {
        auto* popup=context->OpenPopupStack.back().Window;
        activate(state,popup->GetID("MP2888A (ASUS Ampere)"));
        check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_recipe &&
            state.commands.back().i2c_recipe=="mp2888a-pmbus","choosing the Ampere recipe emits its recipe ID without selecting another route");
        if(!state.commands.empty()) bound(state.commands.back(),data,"recipe command binds route, family and revision");
    }
    state.commands.clear(); data.candidates={{{2,0x2a},0,"",uint32_t(I2cFamily::up9512r)}};
    frame(state); activate(state,button("##controller-candidate"));
    if(!context->OpenPopupStack.empty() && context->OpenPopupStack.back().Window) {
        auto* popup=context->OpenPopupStack.back().Window;
        activate(state,popup->GetID("1: uPI uP9512R - port 2, 0x2A (unverified candidate)"));
        check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_select &&
            state.commands.back().i2c_family==I2cFamily::up9512r && state.commands.back().i2c_route
            && state.commands.back().i2c_route->port==2 && state.commands.back().i2c_route->addr7==0x2a,
            "candidate selection carries that candidate's family and route");
    } else check(false,"candidate list opens for explicit selection");
    state.commands.clear(); frame(state); activate(state,button("IC to scan"));
    text=frame(state);
    for(const auto* family : i2c_family_names) check(text.find(family)!=std::string::npos,"scan list contains every catalog family name");
    check(state.commands.empty(),"opening the family selector does not scan");
    // A board display is an explicit presentation choice, never a new DAC contract.
    if(!context->OpenPopupStack.empty() && context->OpenPopupStack.back().Window)
        { const int index=0; const auto* popup=context->OpenPopupStack.back().Window;
          activate(state,ImHashStr(i2c_family_names[0],0,ImHashData(&index,sizeof(index),popup->ID))); }
    data.family=I2cFamily::nct3933; ++data.selection_revision; data.controller.needs_verification=false;
    I2cField out; out.key="out1"; out.label="OUT1"; out.unit="\xc2\xb5" "A"; out.value=-20;
    out.minimum=-1270; out.maximum=1270; out.step=10; out.writable=true; out.resettable=true; out.reset_label="Zero";
    auto out2=out; out2.key="out2"; out2.label="OUT2"; out2.step=20; out2.minimum=-2540; out2.maximum=2540;
    auto out3=out; out3.key="out3"; out3.label="OUT3"; data.controller.fields={out,out2,out3};
    state.commands.clear(); text=frame(state);
    check(state.i2c_nct_mapping==0 && text.find("Raw outputs")!=std::string::npos && text.find("Memory offset")==std::string::npos,
        "an alternate controller starts with raw nominal currents");
    activate(state,button("Zero all outputs"));
    check(state.commands.size()==1 && state.commands.back().operation==Command::Operation::i2c_zero_nct_outputs,
        "NCT zero all queues one batch intent");
    if(!state.commands.empty()) bound(state.commands.back(),data,"zero-all intent binds controller route and revision");
    state.commands.clear(); data.controller.recovery=true; activate(state,button("Zero all outputs"));
    check(state.commands.empty(),"recovery blocks zero all"); data.controller.recovery=false;
    data.gate=false; activate(state,button("Zero all outputs"));
    check(state.commands.empty(),"I2C gate blocks zero all"); data.gate=true;
    activate(state,button("Output mapping"));
    if(!context->OpenPopupStack.empty() && context->OpenPopupStack.back().Window)
        { const int index=1; const auto* popup=context->OpenPopupStack.back().Window;
          activate(state,ImHashStr("GPU / memory / PEX-PLL (mV)",0,ImHashData(&index,sizeof(index),popup->ID))); }
    text=frame(state);
    check(state.i2c_nct_mapping==1 && text.find("GPU offset (OUT3)")!=std::string::npos &&
        text.find("Memory offset (OUT1)")!=std::string::npos && text.find("PEX-PLL offset (OUT2)")!=std::string::npos &&
        text.find("132.00 mV")!=std::string::npos && text.find("Step 132 mV")!=std::string::npos,
        "explicit board mapping uses the documented sign/gain and the adapter's live 20 uA step");
    check(state.commands.empty(),"changing output units never reads or writes the bus");
    activate(state,row_button("out2","##number"));
    io.AddInputCharactersUTF8("264"); frame(state);
    io.AddKeyEvent(ImGuiKey_Enter,true); frame(state); io.AddKeyEvent(ImGuiKey_Enter,false); frame(state);
    activate(state,row_button("out2","Apply"));
    check(state.commands.size()==1 && state.commands.back().i2c_field=="out2" && std::abs(state.commands.back().value+40)<1e-8,
        "typing a mapped 264 mV request converts back to -40 nominal uA for the selected output");
    state.commands.clear(); data.gate=false; frame(state);
    const auto table_id=ImHashStr("i2c-knobs",0,button(""));
    auto* table=ImGui::TableFindByID(table_id);
    check(table!=nullptr,"knob table provides its actual item geometry");
    if(table) {
        const ImVec2 point(table->Columns[4].WorkMinX+12,table->RowPosY1+ImGui::GetFrameHeight()*0.5f);
        io.AddMousePosEvent(point.x,point.y); frame(state);
        io.AddMouseButtonEvent(ImGuiMouseButton_Left,true); frame(state);
        io.AddMouseButtonEvent(ImGuiMouseButton_Left,false); frame(state);
        check(state.commands.empty() && state.i2c_gate_flash_until>ImGui::GetTime(),
            "clicking a grey gated Apply flashes the checkbox without issuing a command");
        const auto deadline=state.i2c_gate_flash_until;
        for(int n=0;n<60;++n) frame(state);
        check(ImGui::GetTime()>deadline && state.i2c_gate_flash_until==deadline,
            "gate flash expires after a finite interval without background work");
    }
    data.controller.fields.clear(); data.controller.stale=true; frame(state);
    activate(state,button("Output mapping"));
    check(context->OpenPopupStack.empty() && state.i2c_nct_mapping==1,"a failed read preserves mapped units and cannot relabel stale input");
    ++data.selection_revision; data.controller.fields={out,out2,out3}; data.controller.stale=false; frame(state);
    check(state.i2c_nct_mapping==0,"a new controller selection returns to raw current without applying or persisting the mapping");
    ImGui::DestroyContext(); std::printf("I2C catalog view: %d failures\n",failures); return failures?1:0;
}
