// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless production view, intents only; no service, backend or GPU.
#include "frequency_lock_view.h"
#include "imgui_internal.h"
#include <cstdio>
#include <string>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* why){if(!ok){++failures;std::printf("FAIL: %s\n",why);}}
std::vector<FrequencyLockIntent> frame(FrequencyLockViewState& view,FrequencyLockData& data,FrequencyLockViewContext& context,std::string* text=nullptr) {
    if(ImGui::FindWindowByName("Clock requests###druta_frequency_lock"))ImGui::SetWindowSize("Clock requests###druta_frequency_lock",{1000,980});
    ImGui::GetIO().DisplaySize={1400,1100};ImGui::NewFrame();ImGui::LogToBuffer(0);
    auto out=draw_frequency_lock(view,data,context);
    if(text)*text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::Render();return out;
}
std::vector<FrequencyLockIntent> press(FrequencyLockViewState& v,FrequencyLockData& d,FrequencyLockViewContext& c,const char* label) {
    auto* window=ImGui::FindWindowByName("Clock requests###druta_frequency_lock");check(window!=nullptr,"clock request window exists");
    if(window)ImGui::ActivateItemByID(window->GetID(label));return frame(v,d,c);
}
void run() {
    FrequencyLockViewState v;v.open=true;
    FrequencyLockViewContext c;c.generation=8;c.slot="0000:0b:02.0";c.uuid="GPU-alternate";
    c.connected=c.unlocked=c.fan_manual_available=true;c.live_core_mhz=1700;
    FrequencyLockData d;d.architecture=7;d.minimum_mhz=300;d.maximum_mhz=1500;d.tables_current=d.nvml_capable=d.current_known=true;
    d.rows={{500,{300,600,900}},{1000,{300,600,900,1200,1500}}};
    check(frame(v,d,c).empty() && v.minimum_mhz==300 && v.maximum_mhz==1500,"view seeds from current adapter bounds without a write intent");
    v.minimum_mhz=355;v.maximum_mhz=1499;v.dirty=true;auto out=press(v,d,c,"Apply frequency range");
    check(out.size()==1 && out[0].action==FrequencyLockAction::apply && out[0].minimum_mhz==355 && out[0].maximum_mhz==1499,
        "Apply emits exact visible drafts; service owns snapping/readback");
    d.ownership={FrequencyLockMode::nvml,true,false,FrequencyRange{300,1200}};d.current=FrequencyRange{300,1200};frame(v,d,c);
    check(v.minimum_mhz==300 && v.maximum_mhz==1200,"fresh accepted snapped request updates an unchanged submitted draft");
    v.minimum_mhz=600;v.dirty=true;d.tables_current=false;frame(v,d,c);
    check(v.minimum_mhz==600 && press(v,d,c,"Apply frequency range").empty(),"transient capability failure preserves unfinished edits and blocks stale SET");
    d.tables_current=true;c.unlocked=false;check(press(v,d,c,"Lock maximum reported clock").empty(),"lock-max requires explicit unlock");
    check(press(v,d,c,"Release owned clock / P0 request").size()==1,"owned release remains available while edits are locked");
    c.connected=false;check(press(v,d,c,"Release owned clock / P0 request").size()==1,"owned release can reach worker recovery when telemetry is disconnected");
    c.busy=true;check(press(v,d,c,"Release owned clock / P0 request").empty(),"busy worker cannot queue overlapping release");
    c.busy=false;c.connected=c.unlocked=true;out=press(v,d,c,"Lock maximum reported clock");
    check(out.size()==1 && out[0].action==FrequencyLockAction::lock_max,"lock-max is a distinct explicit worker intent");
    d.ownership={};d.current=FrequencyRange{600,900};check(press(v,d,c,"Apply frequency range").empty(),"foreign current request blocks acquisition in the view");
    c.other_owned_clock=true;out=press(v,d,c,"Apply frequency range");
    check(out.size()==1 && out[0].action==FrequencyLockAction::apply,"coordinator-proven owned I2C clamp permits explicit handover intent");
    c.other_owned_clock=false;
    d.current.reset();d.legacy_known=d.legacy_available=true;c.fan_manual_available=false;
    check(press(v,d,c,"Lock P0 and max fan").empty(),"legacy P0+fan requires independent manual-fan capability before either write");
    c.fan_manual_available=true;out=press(v,d,c,"Lock P0 and max fan");
    check(out.size()==1 && out[0].action==FrequencyLockAction::legacy_p0_max_fan,"P0+fan emits one coordinated intent, not an unchecked fan write");
    std::string text;frame(v,d,c,&text);
    check(text.find("step it down")!=std::string::npos && text.find("Not a maximum-core lock")!=std::string::npos,
        "UI distinguishes lockable ceiling and P0 from measured/boost performance");
    ++c.generation;c.uuid="GPU-next";d.minimum_mhz=450;d.maximum_mhz=1800;frame(v,d,c);
    check(v.minimum_mhz==450 && v.maximum_mhz==1800 && !v.dirty,"adapter generation/identity change clears old target's numeric drafts");
    check(press(v,d,c,"Read clock capabilities").size()==1,"read-only capability retry is explicit and independent of staged requests");
}
// Released 1.9.1 Clocks menu: the Lock max tooltip's V/F clock formula on the
// adapter's own grid, never a fixed 15 MHz, and the note under the lock.
void menu_text() {
    const auto has=[](const std::string& t,const char* s){return t.find(s)!=std::string::npos;};
    auto tip=frequency_lock_max_tooltip(2205u,15000);
    check(has(tip,"Pins both ends to 2205 MHz - the top of the\ndriver's LOCKABLE table.\n\nThat table is not a boost ceiling")
        &&has(tip,"(it is floor((base+delta)\n/15)*15, never checked against this list). So if\nthe card is already boosting past it")
        &&has(tip,"frequency steady, not for going fast."),"a 15 MHz grid reads as Python's own tooltip");
    tip=frequency_lock_max_tooltip(1911u,12657);
    check(has(tip,"/12.66)*12.66, never checked against this list)")&&!has(tip,"/15)*15"),"another adapter's grid replaces Python's 15");
    tip=frequency_lock_max_tooltip(std::nullopt,0);
    check(has(tip,"LOCKABLE table (not read yet).")&&has(tip,"/step)*step, never checked against this list)")&&!has(tip,"/15)"),
        "an unknown grid keeps the formula without a number");
    const std::string note=frequency_lock_menu_note;
    check(note.rfind("the result is one line in the Control tab log.\n",0)==0&&has(note,"taking one releases the\nother.")&&!has(note,"810"),
        "the note carries 1.9.1's log sentence and leaves out the one-card memory clock");
}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);run();menu_text();ImGui::DestroyContext();
    std::printf("Frequency lock view: %d failures\n",failures);return failures?1:0;}
