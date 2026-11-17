// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_monitor_view.h"
#include "clock_monitor_model.h"
#include "clock_monitor_service.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cfloat>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static std::string frame(const SessionSnapshot& s,ImVec2 size,bool classic){
    ImGui::GetIO().DisplaySize=size;ImGui::NewFrame();ImGui::SetNextWindowSize(size);ImGui::Begin("clock monitor");ImGui::LogToBuffer();
    draw_clock_secondary(s,"core");draw_clock_monitor_table(s,classic);
    const std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return text;
}
// Python 1.9.1 real_clock_view lines and refresh_domains cells.
static void classic_lines(){
    ClockReading r;r.available=true;r.key="core";r.trust="measured";r.state="light";r.util=10.2;
    r.a_mhz=225;r.b_mhz=307.2;r.delta_mhz=82.15;
    auto shown=classic_clock_line(&r,false,false);
    CHECK(shown.line=="measured 307  \xCE\x94 +82.2" && shown.token==" \xC2\xB7 load 10 %");
    r.delta_mhz=-0.04;CHECK(classic_clock_line(&r,false,false).line=="measured 307  \xCE\x94 +0.0");
    r.delta_mhz=-123.6;CHECK(classic_clock_line(&r,false,false).line=="measured 307  \xCE\x94 -124");
    r.delta_mhz=82.1;r.state="judged";r.verdict="warn";CHECK(classic_clock_line(&r,false,false).token==" \xC2\xB7 steady at load");
    r.verdict="varying";CHECK(classic_clock_line(&r,false,false).token==" \xC2\xB7 varying");
    CHECK(classic_clock_line(&r,true,false).token==" \xC2\xB7 stale");
    shown=classic_clock_line(&r,false,true);CHECK(shown.line=="last 307  \xCE\x94 +82.1" && shown.token==" \xC2\xB7 read failed");
    r.state="unpaired";CHECK(classic_clock_line(&r,false,false).token==" \xC2\xB7 vs A 225");
    r.state="load start";CHECK(classic_clock_line(&r,false,false).token==" \xC2\xB7 settling");
    r.state="light";r.util.reset();CHECK(classic_clock_line(&r,false,false).token==" \xC2\xB7 load unread");
    r.trust="checking";shown=classic_clock_line(&r,false,false);
    CHECK(shown.line=="measured: checking B" && shown.token==" \xC2\xB7 checking");
    r.trust="mirror";CHECK(classic_clock_line(&r,false,false).line=="measured: none (B = A)");
    r.trust="same";CHECK(classic_clock_line(&r,false,false).line=="measured: same as A");
    r.trust="unproven";CHECK(classic_clock_line(&r,false,false).line=="unproven 307  \xCE\x94 +82.1");
    // MEM: divided by the adapter's memory divisor, as the tile is.
    ClockReading mem;mem.available=true;mem.key="mem";mem.trust="measured";mem.state="ungraded";mem.b_mhz=10491.5;mem.delta_mhz=-10.5;
    shown=classic_clock_line(&mem,false,false,8);CHECK(shown.line=="measured 1311  \xCE\x94 -1.3" && shown.token.empty());
    CHECK(classic_clock_line(nullptr,false,false).line=="measured: --");
    ClockReading missing;CHECK(classic_clock_line(&missing,false,false).line=="measured: --");
    // Domain cells: index, '?' name and grade colour, MHz delta, 2-digit flags, decimal srcid.
    dc_clock_domain_row row{};row.domain=5;row.scale=1;row.flags=1;row.srcid=32;
    std::snprintf(row.name,sizeof(row.name),"%s","LTC");std::snprintf(row.grade,sizeof(row.grade),"%s","likely");
    std::snprintf(row.kind,sizeof(row.kind),"%s","freq");
    row.has_prog_mhz=row.has_meas_mhz=row.has_delta_mhz=1;row.prog_mhz=1350;row.meas_mhz=1350;row.delta_mhz=0;
    auto c=classic_domain_cells(normalize_clock_domain(row,15));
    CHECK(c.index==" 5" && c.name=="LTC?" && c.grade_tone=="warn" && c.a=="1350.0 MHz" && c.delta=="+0.0 MHz");
    CHECK(c.flags=="0x01" && c.srcid=="32");
    std::snprintf(row.grade,sizeof(row.grade),"%s","unnamed");row.name[0]=0;row.flags=0x1234;row.domain=22;
    c=classic_domain_cells(normalize_clock_domain(row,15));
    CHECK(c.index=="22" && c.name=="--" && c.grade_tone=="dim" && c.flags=="0x00001234");
    std::snprintf(row.grade,sizeof(row.grade),"%s","unpopulated");c=classic_domain_cells(normalize_clock_domain(row,15));
    CHECK(c.a=="--" && c.delta=="--");
}
// Whether any vertex drawn by the named window (exact) or by a child whose name holds
// `part` carries `colour`.
static bool drew(const char* part,ImU32 colour,bool exact){
    for(auto* w:ImGui::GetCurrentContext()->Windows){
        if(!w->Active||(exact?std::strcmp(w->Name,part)!=0:std::strstr(w->Name,part)==nullptr))continue;
        for(const auto& v:w->DrawList->VtxBuffer)if(v.col==colour)return true;
    }
    return false;
}
// Classic's theme text is white; Python sets TEXT (230,232,236) on the domain cells and on
// a judged core measured line, and draws the table with Dear PyGui's darker table colours.
static void classic_colours(){
    SessionSnapshot s;s.data.connected=true;auto data=std::make_shared<ClockMonitorData>();data->available=true;
    auto& r=data->readings[0];r.available=true;r.key="core";r.trust="measured";r.state="judged";r.verdict="ok";
    r.a_mhz=1500;r.b_mhz=1500;r.delta_mhz=0;
    dc_clock_domain_row row{};row.domain=0;row.scale=1;row.flags=1;row.srcid=3;
    std::snprintf(row.name,sizeof(row.name),"%s","GPC");std::snprintf(row.grade,sizeof(row.grade),"%s","confirmed");
    std::snprintf(row.kind,sizeof(row.kind),"%s","freq");
    row.has_prog_mhz=row.has_meas_mhz=row.has_delta_mhz=1;row.prog_mhz=row.meas_mhz=1500;row.delta_mhz=0;
    data->rows.push_back(normalize_clock_domain(row,15));s.data.clock_monitor=data;
    ImGui::GetIO().DisplaySize={1400,1000};ImGui::NewFrame();ImGui::PushStyleColor(ImGuiCol_Text,ImVec4(1,1,1,1));
    ImGui::SetNextWindowSize({1400,1000});ImGui::Begin("clock colours");
    draw_clock_secondary(s,"core",true);draw_clock_monitor_table(s,true);
    ImGui::End();ImGui::PopStyleColor();ImGui::Render();
    const ImU32 text=IM_COL32(230,232,236,255);
    CHECK(drew("clock colours",text,true));   // measured line, verdict ok
    CHECK(drew("/pan_dom",text,false));       // name, reported A / B, flags and srcid cells
    CHECK(drew("/pan_dom",IM_COL32(48,48,51,255),false) && !drew("/pan_dom",IM_COL32(71,71,74,255),false));
    CHECK(drew("/pan_dom",IM_COL32(79,79,89,255),false) && drew("/pan_dom",IM_COL32(59,59,64,255),false));
}
static bool has(const std::string& text,const char* part){return text.find(part)!=std::string::npos;}
// Python 1.9.1 real_clock_view's tooltip, line by line (druta.py 1116-1136, 1170-1300).
static void classic_tooltips(){
    ClockReading r;r.available=true;r.key="core";r.name="GPC";r.domain=0;r.scale=1;r.trust="measured";r.state="light";
    r.util=8.2;r.events=11;r.samples=35;r.a_mhz=225;r.b_mhz=307.1;r.delta_mhz=82.1;r.bin_mhz=15;
    ClassicClockTip tip;tip.key="core";tip.reading=&r;tip.rows=true;tip.tile="225";
    auto text=classic_clock_tooltip(tip);
    CHECK(text.rfind("The number above is the driver's current clock, 225 MHz (public NVAPI, or private array A where "
        "that is missing).",0)==0);
    CHECK(has(text,"\nRow: private domain 0 GPC\nA (programmed)  225.0 MHz\nB (second array)  307.1 MHz\n"));
    CHECK(has(text,"\n\xCE\x94 B-A +82.1 MHz = +5.47 bins of 15.000 MHz (this card's clock step)\n"));
    CHECK(has(text,"\nB counts as measured on this card: while A held still it turned back on its own 11 times, to "
        "values A never reported (35 readings).\n"));
    CHECK(has(text,"\nNot judged below 90 % load (8 % now; a load once reached counts until it drops below 85 %) - "
        "chosen thresholds."));
    CHECK(has(text,"\nColour, from the median of up to 15 readings at load under one A: plain = within one clock bin"));
    // No measured clock step: the gap is not put in bins.
    r.bin_mhz.reset();text=classic_clock_tooltip(tip);
    CHECK(has(text,"\xCE\x94 B-A +82.1 MHz. Not judged: this card's clock step could not be measured") && !has(text,"bins of"));
    r.bin_mhz=15;
    // Judged: the window's load span and Δ range, and a held band says so.
    r.state="judged";r.verdict="warn";r.window={-15,-14,-14.5,-14.6,-14.4};r.window_util=std::make_pair(95.4,99.0);
    r.held=true;r.hold_mhz=0.8;text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nJudged over the last 5 readings at 95-99 % load with A unchanged: \xCE\x94 -15.0 to -14.0 MHz, "
        "median -14.5.\nAmber is HELD: the median crossed the 14.5 MHz edge earlier and has not fallen 0.8 MHz back past it"));
    r.window_util.reset();r.util=97;r.held=false;text=classic_clock_tooltip(tip);
    CHECK(has(text,"at 97-97 % load") && !has(text,"is HELD"));       // Python falls back to the current load
    r.verdict="varying";text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nNot coloured: within the last 5 readings \xCE\x94 ran -15.0 to -14.0 MHz, more than one bin apart."));
    // A failed private read keeps the last values, said as such; a stale snapshot is not judged.
    tip.failed=true;text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nThe private clock read failed this time; the last good values are shown as \"last\", not judged, "
        "for up to 3 s.\n") && !has(text,"Not coloured"));
    tip.failed=false;tip.stale=true;text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nNot judged: the latest driver read failed; these are the last good values.\n"));
    tip.stale=false;
    // Checking and unproven say what counts as evidence and how much there is.
    r.trust="checking";r.state="checking";r.events=1;r.samples=5;text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nChecking whether B is a counter on this card. What counts is B turning back on its own - up after "
        "down, or down after up - while A holds still, to a value A never reported, after its first 2 moves since A last "
        "changed (a copy of A a few reads late, offset or smoothed, only moves one way by then) (1 of 2 so far, 5 readings).\n"));
    r.trust="unproven";r.state="unproven";r.events=0;r.samples=14;text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nB differs from A, but has not yet shown what counts - B turning back") &&
        has(text,"(0 of 2 in 14 readings) - so it is shown as unproven, not as a measurement.\n"));
    r.b_mhz=r.a_mhz;text=classic_clock_tooltip(tip);CHECK(has(text,"\nB equals A right now, and has not yet shown what counts"));
    r.trust="mirror";r.mirror_run=6;text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nB has equalled A exactly in the last 6 readings, through a change of A, so on this card"));
    // A 2CLK row is halved to core MHz and both arrays say so.
    ClockReading two=r;two.trust="measured";two.state="settling";two.name="GPC2CLK";two.domain=15;two.scale=2;
    two.a_mhz=1500;two.b_mhz=1485;two.delta_mhz=-15;tip.reading=&two;text=classic_clock_tooltip(tip);
    CHECK(has(text,"\nRow: private domain 15 GPC2CLK (a 2CLK row, halved to core MHz here)\nA (programmed)  3000.0 MHz = "
        "2 x 1500.0\nB (second array)  2970.0 MHz = 2 x 1485.0\n"));
    CHECK(has(text,"\nNot judged yet: needs 5 readings at >= 90 % load"));
    // MEM: the tile's own figure and divisor, never judged, no colour rule.
    ClockReading mem;mem.available=true;mem.key="mem";mem.name="MEM";mem.domain=4;mem.trust="measured";mem.state="ungraded";
    mem.a_mhz=10502;mem.b_mhz=10491.5;mem.delta_mhz=-10.5;mem.bin_mhz=15;
    ClassicClockTip memory;memory.key="mem";memory.reading=&mem;memory.rows=true;memory.tile="1312.6";memory.memory_divided=true;
    memory.divisor=8;text=classic_clock_tooltip(memory);
    CHECK(text.rfind("The number above is the driver's current memory clock (1312.6 MHz).\nRow: private domain 4 MEM\n",0)==0);
    CHECK(has(text,"a memory \xCE\x94 is not by itself a lower memory clock. Shown divided by 8, as the tile is.\n"));
    CHECK(!has(text,"bins of") && !has(text,"Colour, from the median"));
    memory.tile="10502";memory.memory_divided=false;memory.divisor=1;text=classic_clock_tooltip(memory);
    CHECK(has(text,"memory clock (10502, raw driver units).") && !has(text,"Shown divided"));
    // XBAR: array A of its slot, shown and never judged.
    ClockReading xbar=mem;xbar.key="xbar";xbar.name="Crossbar";xbar.domain=1;xbar.a_mhz=xbar.b_mhz=1740;xbar.delta_mhz=0;
    ClassicClockTip x;x.key="xbar";x.reading=&xbar;x.rows=true;x.tile="1740";text=classic_clock_tooltip(x);
    CHECK(text.rfind("The number above is array A of private domain 1, 1740 MHz.\n",0)==0);
    CHECK(has(text,"\n\xCE\x94 B-A +0.0 MHz. Shown, never judged: XBAR follows the core clock, and its own clock bin is not "
        "known here.\n") && !has(text,"Colour, from the median"));
    // No reading: Python's reason for each case.
    ClassicClockTip none;none.key="core";
    CHECK(classic_clock_tooltip(none)=="No private clock reading yet.");
    none.error="private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer";
    CHECK(classic_clock_tooltip(none)=="private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer. The private clock "
        "getter is the only source these tiles use for a measured clock.");
    none.error.clear();none.rows=true;
    CHECK(classic_clock_tooltip(none)=="This tile has no figure on this card, so there is nothing to pair a measurement with.");
    none.tile="225";CHECK(has(classic_clock_tooltip(none),"No row on this card is CONFIRMED as the core clock (GPC or GPC2CLK)"));
    none.key="xbar";CHECK(classic_clock_tooltip(none)=="Private domain 1 is empty or absent in this reading.");
    none.key="mem";CHECK(classic_clock_tooltip(none)=="No row on this card is CONFIRMED as MEM with both arrays non-zero.");
}
namespace fake {
dc_clock_domains response{};dc_status result=DC_OK;
dc_status DC_CALL classify(const uint32_t*,size_t,int32_t,uint32_t,int32_t,uint32_t,int32_t,uint32_t,dc_clock_domains* out){
    *out=response;return result;
}
dc_clock_domain_row domain(uint32_t id,const char* name,uint32_t a,uint32_t b){
    dc_clock_domain_row row{};row.domain=id;row.scale=1;std::snprintf(row.name,sizeof(row.name),"%s",name);
    std::snprintf(row.kind,sizeof(row.kind),"%s","freq");std::snprintf(row.grade,sizeof(row.grade),"%s","confirmed");
    row.prog_khz=a;row.meas_khz=b;row.has_prog_mhz=row.has_meas_mhz=row.has_delta_mhz=1;
    row.prog_mhz=a/1000.0;row.meas_mhz=b/1000.0;row.delta_mhz=(static_cast<double>(b)-a)/1000;return row;
}
}
// Classic measured line and domain panel in one frame, the mouse resting on the measured line.
static std::string classic_frame(const SessionSnapshot& s,bool hover){
    std::string text;
    for(int pass=0;pass<2;++pass){
        ImGuiWindow* w=ImGui::FindWindowByName("classic clock");
        const ImVec2 at=hover&&w?ImVec2(w->DC.CursorStartPos.x+4,w->DC.CursorStartPos.y+4):ImVec2(-FLT_MAX,-FLT_MAX);
        ImGui::GetIO().AddMousePosEvent(at.x,at.y);
        ImGui::GetIO().DisplaySize={1400,1000};ImGui::NewFrame();ImGui::SetNextWindowPos({0,0});ImGui::SetNextWindowSize({1400,1000});
        ImGui::Begin("classic clock");ImGui::LogToBuffer();
        draw_clock_secondary(s,"core",true);draw_clock_monitor_table(s,true);
        text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();
    }
    return text;
}
// A failed private read, through the session's clock service: Python's getter sentence in the
// domain panel with its rows hidden, the last line for 3 s, then "measured: n/a" on the tile.
static void classic_failures(){
    ClockMonitorService service;ClockMonitorInput input;dc_telemetry t{};input.telemetry=&t;input.step_khz=15000;
    input.step_measured=true;input.architecture=6;
    t.present=DC_T_PRIV_CLOCKS|DC_T_CORE|DC_T_XBAR|DC_T_MEM|DC_T_UTIL_GPU;t.util_gpu=8;t.core=2115;t.xbar=2115;t.mem=2115;
    SessionSnapshot s;s.data.connected=true;s.data.telemetry_valid=true;
    const auto feed=[&](uint32_t b){
        fake::response={};fake::response.size=sizeof(fake::response);fake::response.count=2;
        fake::response.rows[0]=fake::domain(0,"GPC",2115000,b);fake::response.rows[1]=fake::domain(4,"MEM",2115000,b);
        input.now_seconds+=1;++input.sample_id;
        s.data.clock_monitor=std::make_shared<ClockMonitorData>(service.update(fake::classify,input));s.data.telemetry=t;
    };
    for(unsigned i=0;i<10;++i)feed(2087000+(i%2)*8);
    auto text=classic_frame(s,false);
    CHECK(has(text,"measured 2087") && has(text,"GPC") && !has(text,"did not answer"));
    // The getter stops answering; the rest of the telemetry is current (the session keeps
    // the last payload and marks it stale).
    t.present&=~DC_T_PRIV_CLOCKS;feed(0);s.data.telemetry.present|=DC_T_PRIV_CLOCKS;s.telemetry_stale_mask=DC_T_PRIV_CLOCKS;
    text=classic_frame(s,false);
    CHECK(has(text,"last 2087") && has(text,"private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer"));
    CHECK(!has(text,"Private clock read unavailable") && !has(text,"Last reported clock rows") && !has(text,"Waiting"));
    CHECK(has(text,"reported  A") && !has(text,"GPC") && !has(text,"2115.0 MHz")); // Python hides every row
    // Held past Python's 3 s READ_GAP_S: the reading is gone and the tile says n/a, with the reason.
    input.now_seconds+=2.5;feed(0);s.data.telemetry.present|=DC_T_PRIV_CLOCKS;
    CHECK(!s.data.clock_monitor->readings[0].available && s.data.clock_monitor->available);
    text=classic_frame(s,true);
    CHECK(has(text,"measured: n/a") && !has(text,"measured: --"));
    CHECK(has(text,"private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer. The private clock getter is the only "
        "source these tiles use for a measured clock."));
    // Focused keeps its own wording.
    text=frame(s,{1400,1000},false);CHECK(has(text,"Private clock read unavailable; last rows retained"));
    // A retained snapshot (the whole read failed) redraws its last rows, marked stale, no getter error.
    t.present|=DC_T_PRIV_CLOCKS;for(unsigned i=0;i<3;++i)feed(2087000+(i%2)*8);s.telemetry_stale_mask=0;
    input.telemetry=nullptr;++input.sample_id;input.now_seconds+=1;
    s.data.clock_monitor=std::make_shared<ClockMonitorData>(service.update(fake::classify,input));
    s.data.telemetry_error="Telemetry unavailable";s.telemetry_stale_mask=~uint64_t{0};
    text=classic_frame(s,false);
    CHECK(has(text,"Last reported clock rows (stale)") && has(text,"GPC") && !has(text,"did not answer"));
    // The classifier, which Python does not have, keeps the native cause.
    s.data.telemetry_error.clear();s.telemetry_stale_mask=0;input.telemetry=&t;fake::result=DC_E_UNAVAILABLE;
    ++input.sample_id;input.now_seconds+=1;
    s.data.clock_monitor=std::make_shared<ClockMonitorData>(service.update(fake::classify,input));
    text=classic_frame(s,false);
    CHECK(has(text,"Clock classification failed or returned malformed rows; last rows retained") && !has(text,"did not answer"));
    fake::result=DC_OK;
    service.reset();++input.sample_id;s.data.clock_monitor=std::make_shared<ClockMonitorData>(service.update(nullptr,input));
    text=classic_frame(s,true);
    CHECK(has(text,"Clock classifier export unavailable") && has(text,"measured: n/a") && has(text,"reported  A"));
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);
    SessionSnapshot s;s.data.connected=true;auto data=std::make_shared<ClockMonitorData>();data->available=true;
    auto& r=data->readings[0];r.available=true;r.key="core";r.name="GPC";r.trust="checking";r.state="checking";
    dc_clock_domain_row row{};row.domain=31;row.scale=1;row.prog_khz=4;row.meas_khz=17;
    std::snprintf(row.name,sizeof(row.name),"%s","PCIe link gen");std::snprintf(row.grade,sizeof(row.grade),"%s","likely");
    std::snprintf(row.kind,sizeof(row.kind),"%s","pcie_gen");
    data->rows.push_back(normalize_clock_domain(row,15));s.data.clock_monitor=data;
    auto text=frame(s,{1400,1000},true);
    CHECK(text.find("B: checking")!=std::string::npos);
    // Classic: Python's caps panel title, dim legend and seven columns; '?' joins an inferred name.
    CHECK(text.find("ALL CLOCK DOMAINS  \xC2\xB7  private NvAPI GetAllClocks (0x1BD69F49)")!=std::string::npos);
    CHECK(text.find("Names: plain = confirmed; '?' = inferred; '--' = unidentified.")!=std::string::npos);
    CHECK(text.find("PCIe link gen?")!=std::string::npos&&text.find("gen 4")!=std::string::npos&&text.find("raw 17")!=std::string::npos);
    CHECK(text.find("\xCE\x94  B-A")!=std::string::npos&&text.find("srcid")!=std::string::npos&&text.find("Grade")==std::string::npos);
    CHECK(text.find("0x00")!=std::string::npos);
    text=frame(s,{1400,1000},false);
    CHECK(text.find("PCIe link gen ?")!=std::string::npos&&text.find("not proof that B is a physical measurement")!=std::string::npos);
    r.trust="measured";r.state="judged";r.verdict="warn";r.b_mhz=1485;r.delta_mhz=-15;
    text=frame(s,{640,768},false);CHECK(text.find("measured 1485.0 MHz")!=std::string::npos);
    s.paused=true;text=frame(s,{640,768},false);
    CHECK(text.find("last B 1485.0 MHz")!=std::string::npos&&text.find("/ stale")!=std::string::npos);
    CHECK(text.find("Last reported clock rows (stale)")!=std::string::npos);
    CHECK(text.find("/ warn")==std::string::npos);
    // Python rounds before normalising the sign: a hair under target is "+0.0", never "-0.0".
    dc_clock_domain_row jitter{};jitter.domain=2;jitter.scale=1;jitter.prog_khz=1545000;jitter.meas_khz=1544960;
    std::snprintf(jitter.grade,sizeof(jitter.grade),"%s","confirmed");std::snprintf(jitter.kind,sizeof(jitter.kind),"%s","freq");
    jitter.has_prog_mhz=jitter.has_meas_mhz=jitter.has_delta_mhz=1;jitter.prog_mhz=1545;jitter.meas_mhz=1544.96;jitter.delta_mhz=-0.04;
    CHECK(normalize_clock_domain(jitter,15).delta=="+0.0");
    jitter.delta_mhz=-0.06;CHECK(normalize_clock_domain(jitter,15).delta=="-0.1");
    jitter.delta_mhz=0.04;CHECK(normalize_clock_domain(jitter,15).delta=="+0.0");
    // Exact binary ties round half-to-even, as Python round(v, 1) does.
    jitter.meas_mhz=307.25;jitter.delta_mhz=0.25;
    CHECK(normalize_clock_domain(jitter,15).b=="307.2 MHz");CHECK(normalize_clock_domain(jitter,15).delta=="+0.2");
    jitter.delta_mhz=-0.25;CHECK(normalize_clock_domain(jitter,15).delta=="-0.2");
    jitter.meas_mhz=1544.96;
    s.paused=false;jitter.delta_mhz=-0.04;data->rows={normalize_clock_domain(jitter,15)};
    text=frame(s,{1400,1000},true);CHECK(text.find("-0.0")==std::string::npos&&text.find("+0.0")!=std::string::npos);
    // Classic MEM tile line follows Python: divided by the adapter's memory divisor.
    ClockReading mem;mem.available=true;mem.key="mem";mem.trust="measured";mem.state="ungraded";mem.b_mhz=10500;mem.delta_mhz=-8;
    CHECK(clock_secondary_text(mem,false,8).find("measured 1312.5 MHz / delta -1.0")==0);
    CHECK(clock_secondary_text(mem).find("measured 10500.0 MHz / delta -8.0")==0);
    CHECK(clock_secondary_explanation(mem,8).find("Shown divided by 8")!=std::string::npos);
    classic_lines();
    classic_colours();
    classic_tooltips();
    classic_failures();
    ImGui::DestroyContext();std::printf("clock_monitor_view_test: %u checks passed\n",checks);
}
