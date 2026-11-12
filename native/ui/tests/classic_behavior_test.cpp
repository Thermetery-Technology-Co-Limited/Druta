// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Production rendering and command intents only: no Session, DLL or GPU.
#include "ui_view.h"
#include "session.h"
#include "i2c_view.h"
#include "classic_layout.h"
#include "imgui_internal.h"
#include "implot.h"
#include "implot_internal.h"
#include <cmath>
#include <cstdio>
#include <string>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* message) { if(!ok){++failures;std::printf("FAIL: %s\n",message);} }
bool has(const std::string& text,const char* value) { return text.find(value)!=std::string::npos; }
std::shared_ptr<SessionSnapshot> snapshot() {
    auto s=std::make_shared<SessionSnapshot>(); s->generation=31; s->selected_slot="fixture-slot-A";
    s->data.connected=true; s->data.reset_controls_available=true;
    for(auto& c:s->data.controls) {
        c.current=10; c.low=-100; c.high=100; c.writable=c.stock_writable=true;
        c.availability=Availability::available; c.unit="MHz";
    }
    return s;
}
ViewState view(std::shared_ptr<SessionSnapshot> s) {
    ViewState v; v.preview=false; v.presentation=Presentation::classic; v.live=s; return v;
}
std::string frame(ViewState& v,bool panel=false) {
    ImGui::GetIO().DisplaySize={1900,1400}; ImGui::NewFrame(); ImGui::LogToBuffer(0);
    if(panel) {
        ImGui::SetNextWindowPos({0,0}); ImGui::SetNextWindowSize({1900,1400});
        ImGui::Begin("Classic I2C test",nullptr,ImGuiWindowFlags_NoSavedSettings);
        ImGui::PushFont(nullptr,16); ImGui::PushStyleVar(ImGuiStyleVar_CellPadding,ImVec2(34.0f/12,2));
        draw_i2c_panel(v); ImGui::PopStyleVar(); ImGui::PopFont(); ImGui::End();
    } else draw(v);
    std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str(); ImGui::LogFinish(); ImGui::Render(); return text;
}
void activate(ViewState& v,ImGuiWindow* window,const char* label,bool panel=false) {
    check(window!=nullptr,"intent target window exists");
    if(window) ImGui::ActivateItemByID(window->GetID(label)); frame(v,panel);
}
ImGuiWindow* child_with(const char* label) {
    for(auto* w:ImGui::GetCurrentContext()->Windows)
        if(w->Active && w->ParentWindow && std::string(w->Name).find(label)!=std::string::npos) return w;
    return nullptr;
}
void reset_contract() {
    auto s=snapshot(); auto v=view(s); frame(v);
    auto* root=ImGui::FindWindowByName("Druta workspace");
    activate(v,root,"Reset controls");
    check(v.commands.empty() && v.classic_reset_deadline>ImGui::GetTime(),"first Reset press arms without writes");
    auto text=frame(v);
    check(has(text,"RESET ARMED") && has(text,"Registry overrides are unchanged"),"armed Reset displays scope before execution");
    activate(v,root,"Reset controls");
    check(v.commands.size()==1 && v.commands[0].operation==Command::Operation::reset_controls && v.classic_reset_deadline==0,
        "second matching Reset press dispatches exactly once and disarms");
    v.commands.clear(); activate(v,root,"Reset controls"); activate(v,root,"Cancel reset");
    check(v.commands.empty() && v.classic_reset_deadline==0,"Cancel reset performs no operation");
    activate(v,root,"Reset controls"); ImGui::GetIO().DeltaTime=11; frame(v); ImGui::GetIO().DeltaTime=1.0f/60;
    check(v.classic_reset_deadline==0 && v.commands.empty(),"expired Reset arm cannot execute");
    activate(v,root,"Reset controls"); ++s->generation; frame(v);
    check(v.classic_reset_deadline==0 && v.commands.empty(),"adapter generation change cancels Reset");
    activate(v,root,"Reset controls"); v.select_slot="fixture-slot-B"; frame(v);
    check(v.classic_reset_deadline==0 && v.commands.empty(),"pending adapter selection cancels Reset before reopen");
    v.select_slot.reset(); activate(v,root,"Reset controls"); v.controls_unlocked=false; frame(v);
    check(v.classic_reset_deadline==0 && v.commands.empty(),"locking controls cancels Reset");
    v.controls_unlocked=true; v.presentation=Presentation::focused; frame(v); activate(v,root,"Reset controls");
    check(v.commands.size()==1 && v.commands[0].operation==Command::Operation::reset_controls,"Focused reset retains its existing explicit behavior");
}
void floating_contract() {
    auto s=snapshot(); auto v=view(s); for(int n=0;n<5;++n)frame(v);
    auto* root=ImGui::FindWindowByName("Druta workspace");
    auto* bar=ImGui::GetCurrentContext()->TabBars.GetByKey(root->GetID("legacy-tabs"));
    const auto id=ImHashStr("  Control  ",0,root->GetID("legacy-tabs"));
    auto* tab=bar?ImGui::TabBarFindTabByID(bar,id):nullptr;
    check(tab!=nullptr,"Control is an actual tearable tab"); if(!tab)return;
    const ImVec2 pos{bar->BarRect.Min.x+tab->Offset+tab->Width/2,bar->BarRect.GetCenter().y};
    auto& io=ImGui::GetIO(); io.AddMousePosEvent(pos.x,pos.y); frame(v);
    io.AddMouseButtonEvent(0,true); frame(v);
    io.AddMousePosEvent(pos.x,pos.y+70); frame(v); io.AddMouseButtonEvent(0,false); frame(v);
    check(v.classic_floating[0],"drag starting on Control and leaving selector floats the page");
    auto* floating=ImGui::FindWindowByName("Control###druta_float_control");
    check(floating && floating->Active,"floating Control is a real same-viewport window");
    v.drafts[1].value=37.5; v.drafts[1].dirty=true;
    auto text=frame(v); size_t count=0,at=0;
    while((at=text.find("Core clock offset (MHz)",at))!=std::string::npos){++count;++at;}
    check(count==1,"Control contents render once while floating");
    check(v.commands.empty() && v.drafts[1].dirty && v.drafts[1].value==37.5,"floating preserves draft without writes");
    activate(v,floating,"Dock back"); frame(v);
    check(!v.classic_floating[0] && v.page==Page::daily && v.drafts[1].value==37.5,"Dock back restores selector and the same edited draft");
    for(int i=0;i<4;++i)v.classic_floating[i]=true;
    frame(v); check(v.commands.empty(),"all pages may float without creating a backend operation");
    for(const auto* title:{"Control###druta_float_control","Advanced###druta_float_advanced","Monitor###druta_float_monitor","Timings###druta_float_timings"}) {
        auto* w=ImGui::FindWindowByName(title); check(w && w->Active,"each page has its own floating surface");
    }
    auto* timings=ImGui::FindWindowByName("Timings###druta_float_timings"); activate(v,timings,"#CLOSE"); frame(v);
    check(!v.classic_floating[3] && v.page==Page::timings,"closing a floated page docks it");
    v.presentation=Presentation::focused; frame(v);
    check(!ImGui::FindWindowByName("Control###druta_float_control")->Active,"Focused does not render Classic floating windows");
}
I2cField field(const char* key) {
    I2cField f; f.key=key; f.label=key; f.unit="mV"; f.value=10; f.minimum=0; f.maximum=2000;
    f.step=5; f.writable=f.resettable=true; f.reset_label="Zero"; return f;
}
ImGuiID row_button(const char* key,const char* label) {
    auto* child=child_with("classic-i2c-fields");
    return child?ImHashStr(label,0,ImHashStr(key,0,child->GetID("i2c-knobs"))):0;
}
void i2c_contract() {
    auto s=snapshot(); auto v=view(s); auto& d=s->data.i2c;
    d.available=d.gate=d.verified=true; d.selected=I2cRoute{3,0x28}; d.selection_revision=1;
    d.controller.verified=true; d.controller.needs_verification=true;
    for(auto family:{I2cFamily::nct3933,I2cFamily::ncp4206,I2cFamily::up9512r,I2cFamily::icx,I2cFamily::mp2888,I2cFamily::mp29816}) {
        d.family=family; ++d.selection_revision; d.controller.fields={field("selected")};
        if(family==I2cFamily::nct3933)d.controller.fields={field("out2"),field("out1"),field("out3")};
        if(family==I2cFamily::icx)d.controller.fields={field("loadline"),field("fbvdd"),field("nvvdd"),field("ocp_nvvdd")};
        for(int n=0;n<4;++n)frame(v,true); auto text=frame(v,true);
        auto* w=child_with("classic-i2c-fields"); auto* t=w?ImGui::TableFindByID(w->GetID("i2c-knobs")):nullptr;
        check(t && t->ColumnsCount==6,"each Classic controller uses separate six-column request/readback/Apply/reset cells");
        if(t)check(t->Columns[4].WidthGiven>=77 && t->Columns[5].WidthGiven>=73,"I2C actions retain released widths");
        check(has(text,"readback is separate") && v.commands.empty(),"family guidance is visible and presentation performs no bus operation");
        if(family==I2cFamily::nct3933)check(text.find("out3 (")<text.find("out1 (") && text.find("out1 (")<text.find("out2 ("),"NCT output order is OUT3/OUT1/OUT2");
        if(family==I2cFamily::icx)check(text.find("NVVDD voltage request")<text.find("FBVDD voltage request"),"iCX voltage requests precede independent settings");
        auto& draft=v.i2c_field_drafts[0].second; draft.value=20; draft.dirty=true;
        const auto key=v.i2c_field_drafts[0].first;
        ImGui::ActivateItemByID(row_button(key.c_str(),"Apply")); frame(v,true);
        check(v.commands.size()==1 && v.commands[0].i2c_field==key && v.commands[0].i2c_selection_revision==d.selection_revision
            && v.commands[0].i2c_route && v.commands[0].i2c_route->addr7==0x28,"Classic Apply retains exact field/route/revision binding");
        v.commands.clear(); d.controller.fields.clear(); frame(v,true);
        check(!v.i2c_field_cache.empty() && v.i2c_field_drafts[0].second.value==20,"transient partial read retains established row and draft");
        v.controls_unlocked=false; ImGui::ActivateItemByID(row_button(key.c_str(),"Apply")); frame(v,true);
        check(v.commands.empty(),"locked Classic controller does not dispatch Apply"); v.controls_unlocked=true;
    }
    d.family=I2cFamily::mp2888; ++d.selection_revision; d.control_profile="mp2888a-pmbus";
    d.status.min_mv=600; d.status.max_mv=1200; d.status.requested_mv=900; d.status.writable=true;
    d.control_profiles={{"mp2888a-pmbus","Manual","",true,6.25,{}}};
    frame(v,true); auto text=frame(v,true);
    check(has(text,"Manual voltage command") && child_with("classic-i2c-manual"),"manual PMBus recipe also has a Classic request/readback row");
    check(v.commands.empty(),"manual recipe display does not apply its initial command");
}
void discovery_contract() {
    auto s=snapshot(); auto v=view(s); for(int n=0;n<4;++n)frame(v);
    s->data.i2c.candidates={{{2,0x2a},0,"fixture candidate",uint32_t(I2cFamily::up9512r)}};
    auto text=frame(v); check(has(text,"Connect / Scan"),"first discovered candidate opens Classic I2C section");
    auto* body=child_with("/workspace_");
    check(body!=nullptr,"docked body exists for collapse test");
    if(body)body->StateStorage.SetInt(body->GetID("I2C regulator"),0);
    text=frame(v); check(!has(text,"Connect / Scan"),"polling does not repeatedly reopen a user-collapsed section");
    ++s->generation; text=frame(v); check(has(text,"Connect / Scan"),"new adapter generation can reveal its own discovered controller");
}
void curve_parity_contract() {
    auto s=snapshot(); auto v=view(s);
    s->data.curve={{0,750,1500,0},{2,900,1800,0},{7,1050,2100,0}};
    s->data.curve_freq_div=2; s->data.curve_step_khz=12657; s->data.curve_max_abs_delta_khz=2000000;
    s->curve_revision=1; v.page=Page::monitor;
    for(int n=0;n<3;++n)frame(v);
    auto* root=ImGui::FindWindowByName("Druta workspace"); ImGui::FocusWindow(root);
    auto press=[&](ImGuiKey key,bool shift=false,bool ctrl=false) {
        auto& io=ImGui::GetIO(); io.DeltaTime=ctrl?0.5f:1.0f/60;
        io.AddKeyEvent(ImGuiMod_Shift,shift); io.AddKeyEvent(ImGuiMod_Ctrl,ctrl);io.AddKeyEvent(key,true);frame(v);
        io.AddKeyEvent(key,false);io.AddKeyEvent(ImGuiMod_Shift,false);io.AddKeyEvent(ImGuiMod_Ctrl,false);frame(v);
        io.DeltaTime=1.0f/60;
    };
    press(ImGuiKey_W);
    check(v.curve_draft.draft()[0].delta_khz==25314,"Classic W edits retained point from Monitor with no editor window");
    press(ImGuiKey_W,true);
    check(v.curve_draft.draft()[0].delta_khz==101256,"Classic global Shift uses three adapter-reported bins");
    press(ImGuiKey_Z,false,true);
    check(v.curve_draft.draft()[0].delta_khz==25314,"Classic global Undo moves local history while Monitor is selected");
    v.page=Page::daily; frame(v);
    auto* body=child_with("/workspace_");
    if(body)body->StateStorage.SetInt(body->GetID("V/F curve editor"),0);
    frame(v); check(!v.curve_inline_active,"fixture collapses the inline editor without deleting its draft");
    ImGui::FocusWindow(root);press(ImGuiKey_W);
    check(v.curve_draft.draft()[0].delta_khz==50628,"Classic global W works with inline editor collapsed exactly once");
    v.presentation=Presentation::focused;v.page=Page::monitor;frame(v);ImGui::FocusWindow(root);press(ImGuiKey_W);
    check(v.curve_draft.draft()[0].delta_khz==50628,"Focused Monitor cannot edit a background curve");
    v.curve_window_open=v.curve_window_focus_requested=true;frame(v);
    auto* editor=ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(editor!=nullptr,"explicit editor exists for Focused and typing checks");if(!editor)return;
    ImGui::FocusWindow(editor);press(ImGuiKey_W);
    check(v.curve_draft.draft()[0].delta_khz==75942,"Focused explicit focused editor retains its local keyboard path");
    v.presentation=Presentation::classic;frame(v);
    ImGui::ActivateItemByID(editor->GetID("MHz"));
    ImGui::GetCurrentContext()->NavNextActivateFlags=ImGuiActivateFlags_PreferInput;frame(v);
    check(v.curve_entry_editing,"real numeric entry is active before global typing guard test");
    press(ImGuiKey_W);
    check(v.curve_draft.draft()[0].delta_khz==75942,"Classic global W does not steal text-entry input");
    ImGui::ClearActiveID();ImGui::FocusWindow(editor);frame(v);
    activate(v,editor,"Revert local curve");
    auto nudge=[&](int bins) {
        char label[64]{};std::snprintf(label,sizeof(label),"%+.9g",static_cast<double>(bins)*12657/1000);
        ImGui::ActivateItemByID(ImHashStr(label,0,ImHashData(&bins,sizeof(bins),editor->ID)));frame(v);
    };
    nudge(5);
    check(v.curve_draft.draft()[0].delta_khz==126570,"visible +5-bin nudge uses 12.657 MHz adapter grid and wire divisor");
    nudge(-1);
    check(v.curve_draft.draft()[0].delta_khz==101256,"visible negative one-bin nudge stages the exact opposite step");
    const auto plot=[&]() { return ImPlot::GetCurrentContext()->Plots.GetByKey(editor->GetID("##live_curve")); };
    auto legend_has=[](ImPlotPlot* p,const char* name) {
        if(p)for(int i=0;i<p->Items.GetLegendCount();++i)if(std::string(p->Items.GetLegendLabel(i))==name)return true;
        return false;
    };
    v.curve_cap_mv=1000;v.curve_floor_mv=800;v.curve_fit_requested=true;frame(v);
    auto* p=plot();check(p && legend_has(p,"Cap") && legend_has(p,"Ramp floor"),"Classic plot exposes cap and ramp-floor guides");
    check(!legend_has(p,"Hard floor (armed)") && !legend_has(p,"Held"),"unarmed and unowned states add no misleading hard/hold guide");
    if(p) {
        const auto prior=p->Axes[ImAxis_X1].Range;
        v.curve_cap_mv=10000;v.curve_floor_mv=-1000;v.curve_fit_requested=true;frame(v);p=plot();
        check(std::abs(p->Axes[ImAxis_X1].Range.Min-prior.Min)<0.001 && std::abs(p->Axes[ImAxis_X1].Range.Max-prior.Max)<0.001,
            "guide positions do not expand curve fit or masquerade as device limits");
    }
    v.hard_mod_ack=true;s->data.tuning.owned=s->data.tuning.confirmed=true;s->data.tuning.voltage_uv=900000;frame(v);p=plot();
    check(legend_has(p,"Hard floor (armed)") && legend_has(p,"Held"),"acknowledgement and confirmed ownership reveal their distinct guides");
    v.hard_mod_ack=false;s->data.tuning.recovery=true;frame(v);p=plot();
    check(!legend_has(p,"Hard floor (armed)") && !legend_has(p,"Held"),"cleared acknowledgement and uncertain hold remove guides on next frame");
    check(v.commands.empty(),"keyboard, nudges and visual guides dispatch no hardware commands");
}
}
int main() {
    ImGui::CreateContext(); ImPlot::CreateContext(); auto& io=ImGui::GetIO(); io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    ImFontConfig config;config.SizePixels=18;io.Fonts->AddFontDefault(&config);
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);apply_theme(1);
    reset_contract(); floating_contract(); i2c_contract(); discovery_contract();curve_parity_contract();
    ImPlot::DestroyContext();ImGui::DestroyContext();std::printf("Classic behavior: %d failures\n",failures);return failures?1:0;
}
