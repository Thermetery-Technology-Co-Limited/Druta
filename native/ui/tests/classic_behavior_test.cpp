// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Production rendering and command intents only: no Session, DLL or GPU.
#include "ui_view.h"
#include "session.h"
#include "i2c_view.h"
#include "classic_layout.h"
#include "clock_monitor_model.h"
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
    // 1.9.1 reset_all: the arm is one log line naming the scope; nothing is
    // added under the gate row and the arm has no timeout.
    check(!v.classic_log_lines.empty() && has(v.classic_log_lines.back().text,"press again to confirm")
        && has(v.classic_log_lines.back().text,"resets the V/F curve") && v.classic_log_lines.back().ok==false,
        "armed Reset logs 1.9.1's press-again line");
    check(!has(text,"RESET ARMED") && !has(text,"Cancel reset"),"armed Reset adds no banner or Cancel row under the gate row");
    ImGui::GetIO().DeltaTime=11; frame(v); ImGui::GetIO().DeltaTime=1.0f/60;
    check(v.classic_reset_deadline>ImGui::GetTime() && v.commands.empty(),"the Reset arm does not expire and writes nothing while armed");
    {   // Native-only, kept reachable: Cancel reset in the Reset button's right-click menu disarms
        // without unlocking and writes nothing; the next press arms again.
        ImGui::GetIO().DisplaySize={1900,1400}; ImGui::NewFrame(); ImGui::OpenPopupEx(root->GetID("##reset-arm")); draw(v); ImGui::Render();
        const auto& popups=ImGui::GetCurrentContext()->OpenPopupStack;
        ImGuiWindow* menu=popups.empty()?nullptr:popups.back().Window;
        check(menu!=nullptr && v.classic_reset_deadline>ImGui::GetTime(),"the Reset button has a right-click menu and opening it keeps the arm");
        if(menu) ImGui::ActivateItemByID(menu->GetID("Cancel reset"));
        frame(v); frame(v);
        check(v.classic_reset_deadline==0 && v.commands.empty() && has(v.classic_log_lines.back().text,"reset disarmed"),"Cancel reset disarms and writes nothing");
        activate(v,root,"Reset controls");
        check(v.commands.empty() && v.classic_reset_deadline>ImGui::GetTime(),"after a cancel the next press arms again");
    }
    activate(v,root,"Reset controls");
    check(v.commands.size()==1 && v.commands[0].operation==Command::Operation::reset_controls && v.classic_reset_deadline==0,
        "second matching Reset press dispatches exactly once and disarms");
    v.commands.clear();
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
    // 1.9.1 opens on the middle point of the read curve (idx 2 of 0/2/7).
    check(v.selected_curve_point==2,"Classic selects the middle point of the adapter's curve");
    press(ImGuiKey_W);
    check(v.curve_draft.draft()[1].delta_khz==25314,"Classic W edits retained point from Monitor with no editor window");
    press(ImGuiKey_W,true);
    check(v.curve_draft.draft()[1].delta_khz==101256,"Classic global Shift uses three adapter-reported bins");
    press(ImGuiKey_Z,false,true);
    check(v.curve_draft.draft()[1].delta_khz==25314,"Classic global Undo moves local history while Monitor is selected");
    v.page=Page::daily; frame(v);
    auto* body=child_with("/workspace_");
    if(body)body->StateStorage.SetInt(body->GetID("V/F curve editor"),0);
    frame(v); check(!v.curve_inline_active,"fixture collapses the inline editor without deleting its draft");
    ImGui::FocusWindow(root);press(ImGuiKey_W);
    check(v.curve_draft.draft()[1].delta_khz==50628,"Classic global W works with inline editor collapsed exactly once");
    v.presentation=Presentation::focused;v.page=Page::monitor;frame(v);ImGui::FocusWindow(root);press(ImGuiKey_W);
    check(v.curve_draft.draft()[1].delta_khz==50628,"Focused Monitor cannot edit a background curve");
    v.curve_window_open=v.curve_window_focus_requested=true;frame(v);
    auto* editor=ImGui::FindWindowByName("V/F curve editor###druta_curve_editor");
    check(editor!=nullptr,"explicit editor exists for Focused and typing checks");if(!editor)return;
    ImGui::FocusWindow(editor);press(ImGuiKey_W);
    check(v.curve_draft.draft()[1].delta_khz==75942,"Focused explicit focused editor retains its local keyboard path");
    v.presentation=Presentation::classic;frame(v);
    ImGui::ActivateItemByID(editor->GetID("##MHz"));
    ImGui::GetCurrentContext()->NavNextActivateFlags=ImGuiActivateFlags_PreferInput;frame(v);
    check(v.curve_entry_editing,"real numeric entry is active before global typing guard test");
    press(ImGuiKey_W);
    check(v.curve_draft.draft()[1].delta_khz==75942,"Classic global W does not steal text-entry input");
    ImGui::ClearActiveID();ImGui::FocusWindow(editor);frame(v);
    activate(v,editor,"Revert edits");
    auto nudge=[&](int bins) {
        char label[64]{};std::snprintf(label,sizeof(label),"%+.9g",static_cast<double>(bins)*12657/1000);
        ImGui::ActivateItemByID(ImHashStr(label,0,ImHashData(&bins,sizeof(bins),editor->ID)));frame(v);
    };
    nudge(5);
    check(v.curve_draft.draft()[1].delta_khz==126570,"visible +5-bin nudge uses 12.657 MHz adapter grid and wire divisor");
    nudge(-1);
    check(v.curve_draft.draft()[1].delta_khz==101256,"visible negative one-bin nudge stages the exact opposite step");
    const auto plot=[&]() { return ImPlot::GetCurrentContext()->Plots.GetByKey(editor->GetID("##live_curve")); };
    auto legend_has=[](ImPlotPlot* p,const char* name) {
        if(p)for(int i=0;i<p->Items.GetLegendCount();++i)if(std::string(p->Items.GetLegendLabel(i))==name)return true;
        return false;
    };
    // 1.9.1 always lists its guides; whether one is drawn shows as a vertical
    // line of its colour spanning the plot area at its voltage.
    auto guide_drawn=[&](ImPlotPlot* p,double mv,ImU32 col) {
        if(!p)return false;
        const float x=p->Axes[ImAxis_X1].PlotToPixels(mv);bool top=false,bottom=false;
        for(const auto& vtx:editor->DrawList->VtxBuffer)if(vtx.col==col && std::abs(vtx.pos.x-x)<=4) {
            top=top||std::abs(vtx.pos.y-p->PlotRect.Min.y)<=2;bottom=bottom||std::abs(vtx.pos.y-p->PlotRect.Max.y)<=2;
        }
        return top&&bottom;
    };
    const ImU32 hard_col=IM_COL32(255,92,92,255),held_col=IM_COL32(70,209,122,255);
    v.curve_cap_mv=1000;v.curve_floor_mv=800;v.curve_fit_requested=true;frame(v);
    auto* p=plot();check(p && legend_has(p,"cap") && legend_has(p,"ramp floor") && guide_drawn(p,1000,IM_COL32(255,203,71,255))
        && guide_drawn(p,800,IM_COL32(160,108,255,255)),"Classic plot exposes cap and ramp-floor guides");
    check(legend_has(p,"hard floor") && legend_has(p,"held") && !guide_drawn(p,v.hard_floor_mv,hard_col) && !guide_drawn(p,900,held_col),
        "unarmed and unowned states list the hard/hold entries but draw no misleading guide");
    if(p) {
        const auto prior=p->Axes[ImAxis_X1].Range;
        v.curve_cap_mv=10000;v.curve_floor_mv=-1000;v.curve_fit_requested=true;frame(v);p=plot();
        check(std::abs(p->Axes[ImAxis_X1].Range.Min-prior.Min)<0.001 && std::abs(p->Axes[ImAxis_X1].Range.Max-prior.Max)<0.001,
            "guide positions do not expand curve fit or masquerade as device limits");
    }
    v.hard_mod_ack=true;s->data.tuning.owned=s->data.tuning.confirmed=true;s->data.tuning.voltage_uv=900000;frame(v);p=plot();
    check(guide_drawn(p,v.hard_floor_mv,hard_col) && guide_drawn(p,900,held_col),"acknowledgement and confirmed ownership reveal their distinct guides");
    v.hard_mod_ack=false;s->data.tuning.recovery=true;frame(v);p=plot();
    check(!guide_drawn(p,v.hard_floor_mv,hard_col) && !guide_drawn(p,900,held_col) && legend_has(p,"hard floor") && legend_has(p,"held"),
        "cleared acknowledgement and uncertain hold remove guides on next frame");
    check(v.commands.empty(),"keyboard, nudges and visual guides dispatch no hardware commands");
}
// Python refresh_control: the readback column prints what the card reports now;
// the request stays in the input. Fixture values are arbitrary, not one board's.
void readback_contract() {
    auto s=snapshot(); auto& d=s->data; auto& t=d.telemetry;
    d.telemetry_valid=true;
    t.present=DC_T_CORE|DC_T_MEM|DC_T_POWER_W|DC_T_VCORE_MV|DC_T_CORE_P0MAX|DC_T_MEM_P0MAX|DC_T_VBOOST|DC_T_PSTATE|DC_T_PRIV_CLOCKS;
    t.core=345;t.mem=9001;t.power_w=81.6;t.vcore_mv=912.5;t.core_p0max=2010;t.mem_p0max=9500;t.vboost_pct=40;t.pstate=2;
    t.fan_count=1;t.fans[0].has_duty=1;t.fans[0].duty=37;
    d.memory_divisor=4;d.memory_type="GDDR6";
    d.controls[static_cast<size_t>(ControlId::power)].current=250;d.controls[static_cast<size_t>(ControlId::power)].unit="W";
    d.controls[static_cast<size_t>(ControlId::boost)].unit="%";
    // Two per-domain controls: one paired to a classified private row, one unpaired.
    AdvancedControl paired;paired.id={AdvancedKind::clock_domain,3};paired.label="Paired domain";paired.state=d.controls[0];paired.state.current=-15;
    AdvancedControl unpaired=paired;unpaired.id.domain=9;unpaired.label="Unpaired domain";
    AdvancedControl nvvdd=paired;nvvdd.id={AdvancedKind::nvvdd,0};nvvdd.label="NVVDD offset";nvvdd.state.current=25;nvvdd.state.unit="mV";
    AdvancedControl msvdd=nvvdd;msvdd.id={AdvancedKind::msvdd,4};msvdd.label="MSVDD offset";msvdd.state.current=-30;
    d.advanced_controls={paired,unpaired,nvvdd,msvdd};
    d.clock_domain_pairs={{3,6}};
    auto monitor=std::make_shared<ClockMonitorData>();monitor->available=true;
    ClockDomainViewRow row;row.raw.domain=6;std::snprintf(row.raw.name,sizeof(row.raw.name),"SYS");
    row.raw.has_prog_mhz=1;row.raw.prog_mhz=1612.4;monitor->rows.push_back(row);d.clock_monitor=monitor;
    PowerPolicy current;current.id=7;current.core_current=true;current.state=d.controls[0];current.state.unit="A";
    current.state.current=612.5;current.state.low=100;current.state.high=700;current.observed_amps=31.27;current.limit_amps=612.5;
    d.power_policies={current};
    RailControl limit;limit.id={0,0};limit.state=d.controls[0];limit.state.current=40;limit.state.unit="mV";limit.estimated_base_mv=1000;
    d.rail_controls={limit};
    d.controls[static_cast<size_t>(ControlId::memory)].unit="MHz true";
    auto v=view(s);auto text=frame(v);text=frame(v);
    check(has(text,"Memory offset (MHz true)"),"memory label takes its unit from the adapter's offset scale");
    {   // Python build_domain_offset_rows: per-domain rows inside the Clock offsets table, before Limits.
        const auto memory=text.find("Memory offset (MHz true)"),paired_row=text.find("SYS clock offset (MHz)"),
            unpaired_row=text.find("control domain 9 offset (MHz)"),limits=text.find("Limits");
        check(memory!=std::string::npos && paired_row!=std::string::npos && unpaired_row!=std::string::npos && limits!=std::string::npos
            && memory<paired_row && paired_row<unpaired_row && unpaired_row<limits,
            "domain rows follow Memory in the Clock offsets table, named from the pairing, in Python's knob order");
        check(has(text,"Configured request: 250 W. NVML enforced limit unavailable.") && !has(text,"Configured power:"),
            "the Limits footer reads as Python's power request status");
        check(has(text,"NVVDD offset (mV)") && has(text,"MSVDD requested offset (mV)"),"rail offsets carry Python's labels");
    }
    const auto count=[](const std::string& in,const char* what){size_t n=0,at=0;while((at=in.find(what,at))!=std::string::npos){++n;++at;}return n;};
    const auto unavailable_before=count(text,"unavailable");
    check(has(text,"core 345 MHz (P0 max 2010)   VRAM 2250.2 MHz (P0 max 2375)"),"read-out line 1 follows Python with true-MHz VRAM and P0 maxima");
    // Python f"{vc:.0f}" rounds the exact tie 912.5 half to even.
    check(has(text,"Vcore 912 mV   volt-boost 40%   P2") && !has(text,"Vcore 913"),"read-out line 2 follows Python wording and rounding");
    check(has(text,"2250.2 MHz") && !has(text,"MHz driver"),"memory readback is one line of true MHz from the adapter divisor");
    check(has(text,"82 W") && !has(text,"81.6") && !has(text,"corrected"),"power readback is the raw live draw as a whole number");
    check(has(text,"37%") && !has(text,"37 %"),"fan readback uses Python's format");
    check(has(text,"1612 MHz") && !has(text,"-15 MHz"),"paired domain shows the paired private row's programmed clock");
    check(has(text,"912.50 mV"),"NVVDD offset shows live Vcore with two decimals");
    check(!has(text,"1040 ~mV") && !has(text,"-30 mV"),"rail-limit and MSVDD readbacks do not echo the request");
    check(has(text,"31.3 A") && has(text,"\xE2\x89\xA4" "612.5 A") && has(text,"Stock"),"core current shows live amps over the effective ceiling with Stock");
    d.clock_domain_requests_only=true;text=frame(v);
    check(has(text,"-15 MHz") && !has(text,"1612 MHz"),"requests-only generation shows the signed requested offset as Python does");
    d.clock_domain_requests_only=false;d.memory_divisor.reset();d.memory_type="unknown";text=frame(v);
    check(has(text,"VRAM 9001 MHz (P0 max 9500)") && !has(text,"2250.2"),"unknown divisor shows the raw driver figure");
    d.controls[static_cast<size_t>(ControlId::memory)].unit="MHz eff";text=frame(v);
    check(has(text,"Memory offset (MHz eff)") && !has(text,"MHz true"),"unknown divisor labels the memory offset in effective MHz");
    d.controls[static_cast<size_t>(ControlId::memory)].unit.clear();text=frame(v);
    check(has(text,"Memory offset (MHz eff)"),"unreadable scale falls back to Python's divisor rule for the label");
    t.present&=~(DC_T_POWER_W|DC_T_CORE);current.observed_amps.reset();d.power_policies={current};text=frame(v);
    check(has(text,"core ? MHz") && !has(text,"82 W") && !has(text,"31.3 A"),"missing readings are unavailable, never a stale request");
    check(count(text,"unavailable")==unavailable_before+1 && !has(text,"\xE2\x89\xA4" "612.5 A"),"missing current reading prints Python's unavailable");
    check(!has(text,"Estimated limits;") && !has(text,"Link reliability"),"a rail block that is not writable shows no Ceilings row");
    d.rail_writable_known=d.rail_writable=true;text=frame(v);
    check(has(text,"Ceilings") && has(text,"Link reliability + alt-reliability") && has(text,"first stable driver reading")
        && !has(text,"NVVDD estimated limits"),
        "Classic Rails column leads with Python's Ceilings and Reference rows; a partial rail gets no summary");
    // Which rails the driver lists comes only from the rail readout; established
    // controls alone do not prove MSVDD is absent, so the NVVDD-only note waits for it.
    check(!has(text,"Driver reports NVVDD only"),"without a rail readout the NVVDD-only note is not claimed");
    // The backend's rail readout reaches the Classic Control page (Python refresh_rail_live).
    std::array<RailReadout,DC_RAILS_COUNT> rails{};rails[0].listed=true;dc_rails_state_row rail_state{};rail_state.live=917.5;rail_state.effective=1081.25;rails[0].state=rail_state;
    d.rail_readouts=rails;text=frame(v);
    check(has(text,"live 917.5 mV") && has(text,"eff 1081.25") && has(text,"Driver reports NVVDD only"),
        "the Classic Rails column shows the live and effective readout and the NVVDD-only note once the driver lists only NVVDD");
    (*d.rail_readouts)[0].stale=true;text=frame(v);
    check(has(text,"live 917.5 mV (stale)"),"a failed rail read keeps the last reading, marked stale");
    d.rail_readouts.reset();
    check(v.commands.empty(),"readbacks dispatch no hardware command");
}
}
void hold_line_contract();
void knobs_low_contract() {
    // ctl-19: Python's amber i2c_scan_status instruction shows only in Classic, idle, with a transport.
    auto s=snapshot(); auto v=view(s); s->data.i2c.available=true;
    auto text=frame(v,true);
    check(has(text,"Enable I2C rail, choose an IC, then press Connect / Scan.") && has(text,"IC to scan"),"Classic idle I2C panel shows the instruction");
    check(v.commands.empty(),"showing the I2C instruction sends nothing");
    s->data.i2c.available=false; text=frame(v,true);
    check(!has(text,"Enable I2C rail"),"no instruction without a transport");
    s->data.i2c.available=true; s->data.i2c.discovery.total=4; text=frame(v,true);
    check(!has(text,"Enable I2C rail"),"instruction goes away once a scan has produced progress");
    s->data.i2c.discovery.total=0; v.presentation=Presentation::focused; text=frame(v,true);
    check(!has(text,"Enable I2C rail"),"Focused I2C panel is unchanged");
    hold_line_contract();
}
// ctl-12: Python draw_hold_banner. The line under the read-out describes this
// session's own lock record: empty while it holds nothing, whatever the
// telemetry lock count says, GOOD only for a confirmed hold on the asked point.
void hold_line_contract() {
    auto h=snapshot(); auto hv=view(h); h->data.telemetry.present|=DC_T_VF_LOCKED; h->data.telemetry.vf_locked_count=3;
    frame(hv);
    // The inline editor's own held guide is GOOD too: collapse it so only the line counts.
    if(auto* body=child_with("/workspace_"))body->StateStorage.SetInt(body->GetID("V/F curve editor"),0);
    const ImU32 good=IM_COL32(70,209,122,255),warn=IM_COL32(255,203,71,255);
    const auto colour=[](ImU32 col) {
        size_t n=0; for(auto* w:ImGui::GetCurrentContext()->Windows) if(w->Active) for(const auto& vtx:w->DrawList->VtxBuffer) n+=vtx.col==col;
        return n;
    };
    auto text=frame(hv); const auto idle_good=colour(good),idle_warn=colour(warn);
    check(!has(text,"V/F point lock") && !has(text,"V/F HOLD") && !has(text,"V/F hold entries") && !has(text,"Hold state unverified"),
        "a lock this session does not own leaves the hold line empty");
    // Arbitrary fixture curve with a two-point flat at 1740 MHz.
    h->data.curve={{70,887.5,1710,0},{71,893.75,1740,0},{72,900,1740,0},{80,950,1830,0}};
    auto& t=h->data.tuning; t.owned=t.confirmed=true; t.domain=6; t.voltage_uv=950000;
    text=frame(hv);
    check(has(text,"HOLD  V/F point lock on domain 6  \xE2\x80\xA2  point 80 @ 950.00 mV, 1830 MHz   \xE2\x80\xA2   Ctrl+H releases"),
        "a confirmed hold names its domain, point, voltage and clock");
    check(colour(good)>idle_good,"a hold on the asked point is GOOD");
    t.voltage_uv=900000; text=frame(hv);
    check(has(text,"HOLD  V/F point lock on domain 6  \xE2\x80\xA2  asked for point 72 @ 900.00 mV, the card holds the point at or below it: "
        "point 71 @ 893.75 mV, 1740 MHz   \xE2\x80\xA2   Ctrl+H releases"),"a request inside a flat names the point the card runs");
    check(colour(good)==idle_good && colour(warn)>idle_warn,"a hold off the asked point is WARN");
    h->data.curve.clear(); text=frame(hv);
    check(has(text,"HOLD  V/F point lock on domain 6  \xE2\x80\xA2  900.00 mV   \xE2\x80\xA2   Ctrl+H releases") && !has(text,"point 72"),
        "without a read curve the line names only the held voltage");
    t.confirmed=false; text=frame(hv);
    check(has(text,"V/F HOLD readback unavailable  \xE2\x80\xA2  requested 900.00 mV; current state is unconfirmed  \xE2\x80\xA2  Release retries cleanup"),
        "an unconfirmed hold says so");
    check(colour(good)==idle_good && colour(warn)>idle_warn,"an unconfirmed hold is WARN");
    t.recovery=true; text=frame(hv);
    check(has(text,"V/F HOLD RECOVERY REQUIRED  \xE2\x80\xA2  requested 900.00 mV; current state is unconfirmed"),"a hold in recovery says so");
    t={}; auto& clock=h->data.frequency.ownership;
    clock.mode=FrequencyLockMode::legacy_p0; clock.confirmed=true; text=frame(hv);
    check(has(text,"HOLD  legacy P0 hold  \xE2\x80\xA2  Maximum core boost is not guaranteed by a performance-state hold  \xE2\x80\xA2  Release P0 drops the hold"),
        "an owned legacy P0 hold has Python's line");
    clock.confirmed=false; clock.recovery=true; text=frame(hv);
    check(has(text,"HOLD  legacy P0 request remains after failed verification / release"),"an unverified legacy P0 hold says so");
    clock.mode=FrequencyLockMode::nvml; clock.confirmed=true; clock.recovery=false; clock.range=FrequencyRange{1200,1800}; text=frame(hv);
    check(has(text,"clock lock requested at [1200..1800] MHz from the Clocks menu (NVML locked clocks) - no V/F point is held"),
        "an owned frequency request names its range");
    clock.recovery=true; text=frame(hv);
    check(has(text,"GPU clock lock accepted; its submitted range is unavailable  \xE2\x80\xA2  Release retries cleanup"),
        "an unconfirmed frequency request says so");
    clock={}; text=frame(hv);
    check(!has(text,"clock lock") && !has(text,"HOLD  "),"released ownership empties the line again");
    // 1.9.1 _headroom_note "raised": a raise still in force after its hold ended
    // is shown whatever the lock says, in WARN.
    const auto quiet_warn=colour(warn);
    t.headroom=true; text=frame(hv);
    check(has(text,"No V/F hold") && has(text,"VOLTAGE LIMITS STILL RAISED - the headroom raise could not be taken off; Druta retries (see log)")
        && colour(warn)>quiet_warn && colour(good)==idle_good,"a raise left after the hold ended reads 1.9.1's WARN note");
    h->data.curve={{80,950,1830,0}}; t.owned=t.confirmed=true; t.domain=6; t.voltage_uv=950000; text=frame(hv);
    check(has(text,"point 80 @ 950.00 mV") && !has(text,"STILL RAISED") && colour(good)>idle_good,"a hold carrying its own raise adds no note");
    t={}; h->data.curve.clear(); frame(hv);
    check(hv.commands.empty(),"the hold line sends nothing");
}
// 1.9.1 _timings_worker logs a timing capture only when it fails: the Classic
// log, the V/F status line and Advanced's status line skip a successful capture
// or read and word a failure as 1.9.1 does. Focused keeps every result.
void timing_log_contract() {
    auto s=snapshot(); auto v=view(s); s->data.telemetry_valid=true; s->completed_reads=1;
    s->data.curve={{0,750,1500,0},{2,900,1800,0},{7,1050,2100,0}}; s->data.curve_freq_div=1; s->curve_revision=1;
    for(int n=0;n<3;++n) frame(v);
    // The session names the operation and whether the host submitted it on its own.
    const auto result=[&](uint64_t id,const char* message,Command::Operation operation,bool ok,bool automatic=false) {
        s->log.push_back({id,message,ok,operation,automatic}); s->last_action_log_id=id; s->completed_actions=id;
        s->last_action_operation=operation; s->last_action_ok=ok; frame(v);
    };
    // The log box sits at the end of the Control page.
    const auto control_end=[&] {
        if(auto* body=child_with("/workspace_")) ImGui::SetScrollY(body,body->ScrollMax.y);
        frame(v); return frame(v);
    };
    // The launch-time helper discovery, then the startup read.
    result(1,"Timing helper selected; Read fields captures this adapter",Command::Operation::timings_select_helper,true,true);
    result(2,"Selected-card raw timing registers captured",Command::Operation::timings_read,true);
    auto text=control_end();
    check(has(text,"[ok] curve read: peak 2100 MHz") && !has(text,"raw timing registers captured") && !has(text,"Timing helper selected"),
        "a discovered helper and a successful timing read are not logged in Classic; the curve read stays the newest line");
    v.page=Page::advanced; text=frame(v);
    check(has(text,"[ok] curve read: peak 2100 MHz") && !has(text,"raw timing registers captured"),
        "Advanced's status line skips the successful timing read too");
    result(3,"Selected-card raw timing registers captured",Command::Operation::timings_capture_p0,true);
    result(4,"P0 workload did not start (fixture)",Command::Operation::timings_capture_p0,false);
    text=frame(v);
    check(has(text,"[!!] timings capture failed: P0 workload did not start (fixture)") && !has(text,"raw timing registers captured"),
        "a failed capture reads 1.9.1's 'timings capture failed' line on the status line");
    v.page=Page::daily; text=control_end();
    check(has(text,"[!!] timings capture failed: P0 workload did not start (fixture)") && !has(text,"raw timing registers captured"),
        "the Classic log box has the failure and no successful capture");
    // A helper the user located is logged, as 1.9.1's Locate nvtune is. The
    // mark travels with the entry: several results finishing between two
    // frames keep their own (a minimized window draws no frame).
    s->log.push_back({5,"Selected-card raw timing registers captured",true,Command::Operation::timings_read,true});
    s->log.push_back({6,"Timing helper selected; Read fields captures this adapter",true,Command::Operation::timings_select_helper,true});
    result(7,"Timing helper selected; Read fields captures this adapter",Command::Operation::timings_select_helper,true);
    v.page=Page::advanced; text=frame(v);
    check(has(text,"[ok] Timing helper selected; Read fields captures this adapter"),"a located helper's selection is logged");
    // A later result takes the status lines, so only the log box holds the selections.
    v.page=Page::daily; result(8,"Fixture later result",Command::Operation::apply,true); text=control_end();
    size_t selections=0; for(size_t at=0;(at=text.find("Timing helper selected",at))!=std::string::npos;++at) ++selections;
    check(selections==1 && !has(text,"raw timing registers captured"),
        "only the located selection is logged; the automatic one and a capture that finished with it are not");
    v.page=Page::daily;
    v.presentation=Presentation::focused; text=frame(v);
    check(has(text,"Selected-card raw timing registers captured") && has(text,"P0 workload did not start (fixture)")
        && !has(text,"timings capture failed"),"Focused keeps its own timing log lines");
    check(v.commands.empty(),"log filtering writes nothing");
}
// 1.9.1 set_stale / log_once("read"): a failed telemetry read fills the
// header's empty line with the BAD "TELEMETRY STALE" text and logs the error
// once per failure episode; the read-out keeps its two lines, nothing moves.
void stale_header_contract() {
    auto s=snapshot(); auto v=view(s); s->data.telemetry_valid=true; s->completed_reads=1;
    s->history.push_back({10,1500,40,100});
    for(int n=0;n<3;++n) frame(v);
    auto* knobs=child_with("classic-knob-columns"); const float knobs_y=knobs?knobs->Pos.y:-1;
    auto text=frame(v);
    check(!has(text,"TELEMETRY STALE") && !has(text,"read error"),"a good read leaves the header's stale line empty");
    const auto read_errors=[&] {
        size_t n=0; for(const auto& line:v.classic_log_lines) n+=line.text=="read error: Telemetry unavailable" && line.ok==false; return n;
    };
    const auto fail=[&](double t) {
        s->data.telemetry_valid=false; s->data.telemetry_error="Telemetry unavailable"; ++s->completed_reads;
        s->history.push_back({t,std::nullopt,std::nullopt,std::nullopt}); frame(v); return frame(v);
    };
    fail(16); text=fail(22);
    check(has(text,"   \xE2\x9A\xA0 TELEMETRY STALE - last good read 12s ago"),"a failed read shows 1.9.1's stale line with the age of the last good read");
    check(read_errors()==1,"the read error is logged once per failure episode");
    check(!has(text,"Some displayed readings are stale") && !has(text,"Read failed"),"no extra line is added under the read-out");
    knobs=child_with("classic-knob-columns");
    check(knobs && knobs->Pos.y==knobs_y,"the stale text takes the header's existing line: the page below does not move");
    s->data.telemetry_valid=true; s->data.telemetry_error.clear(); ++s->completed_reads; s->history.push_back({23,1500,40,100});
    frame(v); text=frame(v);
    check(!has(text,"TELEMETRY STALE"),"a good read clears the stale line");
    fail(24);
    check(read_errors()==2,"a new failure episode logs the error again");
    s->history.clear(); s->history.push_back({30,std::nullopt,std::nullopt,std::nullopt}); text=frame(v);
    check(has(text,"TELEMETRY STALE - no reading yet"),"without any good read the stale line says so");
    check(v.commands.empty(),"the stale line writes nothing");
}
// Released 1.9.1 gate row: the risk banner above it only above score zero, the
// (?) tab help at its end, and Lock P0 / Release P0 in Max it's place on an
// adapter that reports legacy-P0 support. Fixture capabilities are arbitrary.
void gate_row_contract() {
    auto s=snapshot(); auto v=view(s); v.controls_unlocked=true;
    s->data.max_it_available=true; s->data.xoc_known=s->data.rail_mode_known=s->data.rail_writable_known=true;
    auto text=frame(v);
    check(!has(text,"(risk ") && !has(text,"GREEN / 0 pts") && !has(text,"LOCKED") && has(text,"(?)"),
        "score zero shows no risk text; the row ends in the (?) tab help");
    check(has(text,"Timings  (needs nvtune)"),"the Timings tab carries 1.9.1's label");
    // 1.9.1 banner text: the band's sentence, then each live feature's, heaviest
    // first. Native yellow and amber are one software envelope off (1.9.1 AMBER).
    const char* amber="A software envelope has been removed. Requests still go through the driver, so the GPU's own reliability ceiling is still underneath you.";
    const char* xoc="XOC: expanded voltage and clock ranges; current limits use the API ceilings. Supported GPUs also expose live extended power requests; "
        "the 1999 W software request cap does not raise firmware limits. Native policy limits can wrap at high requests.";
    const char* rails="RAIL LIMITS: the driver-held per-rail voltage ceilings are being written directly. EXPERIMENTAL.";
    const char* i2c="I2C: direct board-controller writes bypass the GPU's voltage request limits. Register readback is not a physical "
        "rail-voltage measurement; changes may persist through a reboot.";
    s->data.xoc_enabled=true; text=frame(v);
    check(has(text,(std::string("YELLOW (risk 1) - ")+amber+"  "+xoc).c_str()),"a live feature raises the banner with band, score, 1.9.1's band and feature sentences");
    s->data.xoc_enabled=false; s->data.rail_mode_enabled=s->data.rail_writable=true; text=frame(v);
    check(has(text,(std::string("AMBER (risk 2) - ")+amber+"  "+rails).c_str()),"Rail limits alone is one software envelope off");
    s->data.xoc_enabled=true; text=frame(v);
    check(has(text,(std::string("RED (risk 3) - A write on this path reaches hardware with no firmware clamp on it, or two software envelopes are off at once.  ")
        +rails+"  "+xoc).c_str()),"two software envelopes off is 1.9.1's RED; the banner lists every live feature, heaviest first");
    s->data.rail_mode_enabled=false; s->data.i2c.status.recovery_pending=true; text=frame(v);
    check(has(text,(std::string("CRIMSON (risk 4) - NOTHING MEANINGFUL IS BETWEEN THESE SLIDERS AND YOUR HARDWARE. A wrong number here kills the card.  ")
        +i2c+"  "+xoc).c_str()),"I2C with another feature is 1.9.1's CRIMSON, I2C's sentence first");
    s->data.i2c.status.recovery_pending=false;
    s->data.xoc_enabled=s->data.rail_mode_enabled=false; s->data.xoc_known=false; text=frame(v);
    check(has(text,"UNKNOWN (risk 0) - ") && has(text,"Some control states are unknown."),"a mode state the adapter left unknown stays visible");
    s->opening=true; text=frame(v);
    check(!has(text,"UNKNOWN (risk 0)"),"an opening adapter has not established its modes yet: no banner");
    s->opening=false; s->data.xoc_known=true;
    text=frame(v);
    check(has(text,"Max it  (fan + power + volts + curve)") && !has(text,"Lock P0 and max fan") && !has(text,"Release P0"),
        "an adapter without legacy P0 keeps Max it");
    auto& clock=s->data.frequency; clock.legacy_known=clock.legacy_available=true;
    text=frame(v);
    check(has(text,"Lock P0 and max fan") && has(text,"Release P0") && !has(text,"Max it  (fan"),
        "an adapter reporting legacy P0 gets Lock P0 and max fan and Release P0 in Max it's place");
    auto* root=ImGui::FindWindowByName("Druta workspace");
    activate(v,root,"Lock P0 and max fan");
    check(v.commands.size()==1 && v.commands[0].operation==Command::Operation::frequency_legacy_p0_max_fan,
        "Lock P0 and max fan queues the existing legacy P0 + fan command");
    v.commands.clear(); activate(v,root,"Release P0");
    check(v.commands.empty(),"Release P0 is inert while this session holds no P0 request");
    clock.ownership.mode=FrequencyLockMode::nvml; activate(v,root,"Release P0");
    check(v.commands.empty(),"Release P0 never releases an NVML range lock");
    clock.ownership.mode=FrequencyLockMode::legacy_p0; activate(v,root,"Release P0");
    check(v.commands.size()==1 && v.commands[0].operation==Command::Operation::frequency_lock_release,"Release P0 releases this session's P0 request");
    v.commands.clear(); clock.legacy_known=false; text=frame(v);
    check(has(text,"Lock P0 and max fan") && !has(text,"Max it  (fan"),"a transient probe failure keeps the established slot");
    activate(v,root,"Lock P0 and max fan");
    check(v.commands.empty(),"until the capability is read again the action stays disabled");
    clock.legacy_known=true; s->data.controls[static_cast<size_t>(ControlId::fan)].writable=false; activate(v,root,"Lock P0 and max fan");
    check(v.commands.empty(),"no manual fan control: P0 + fan does not start");
    s->data.controls[static_cast<size_t>(ControlId::fan)].writable=true; v.controls_unlocked=false; activate(v,root,"Lock P0 and max fan");
    activate(v,root,"Release P0");
    check(v.commands.empty(),"read-only mode disables both P0 buttons");
    v.controls_unlocked=true; v.presentation=Presentation::focused; text=frame(v);
    check(has(text,"Max it (fan + power + volts + curve)") && !has(text,"Release P0"),"Focused keeps its gate row");
}
int main() {
    ImGui::CreateContext(); ImPlot::CreateContext(); auto& io=ImGui::GetIO(); io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    ImFontConfig config;config.SizePixels=18;io.Fonts->AddFontDefault(&config);
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);apply_theme(1);
    reset_contract(); floating_contract(); i2c_contract(); discovery_contract();curve_parity_contract();readback_contract();knobs_low_contract();gate_row_contract();
    timing_log_contract();stale_header_contract();
    ImPlot::DestroyContext();ImGui::DestroyContext();std::printf("Classic behavior: %d failures\n",failures);return failures?1:0;
}
