// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "monitor_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <utility>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static ImGuiWindow* child(const char* part){
    // only windows submitted in the latest frame
    for(auto* w:ImGui::GetCurrentContext()->Windows)if(w->Active && std::string(w->Name).find(part)!=std::string::npos)return w;
    return nullptr;
}
static ImVec2 mouse{-FLT_MAX,-FLT_MAX};
static std::string frame(const SessionSnapshot& s,const ShuntCorrection& correction,bool classic,ImVec2 display,bool history=false,bool open_lamps=false){
    // Two frames: classic cards and panels size to the content measured on the previous frame.
    // `history` ends the page the way Classic does, with the History header under the bands.
    // `open_lamps` expands Focused's collapsed reasons header, as a click would.
    std::string text;
    for(int pass=0;pass<2;++pass){
        ImGui::GetIO().AddMousePosEvent(mouse.x,mouse.y);
        ImGui::GetIO().DisplaySize=display;ImGui::NewFrame();ImGui::SetNextWindowSize(display);
        ImGui::Begin("monitor test",nullptr,ImGuiWindowFlags_NoSavedSettings);ImGui::LogToBuffer();
        if(open_lamps){ImGui::PushID("monitor-details");ImGui::GetStateStorage()->SetInt(ImGui::GetID("Throttle / clocks-event reasons"),1);ImGui::PopID();}
        draw_monitor_tiles(s,correction,classic);draw_monitor_details(s,classic);
        if(history){ImGui::CollapsingHeader("History");note_classic_monitor_page_end();}
        text=ImGui::GetCurrentContext()->LogBuffer.c_str();
        ImGui::LogFinish();ImGui::End();ImGui::Render();
    }
    return text;
}
static bool has(const std::string& text,const char* part){return text.find(part)!=std::string::npos;}
// Rests the mouse on each line of a Classic panel in turn until `heading` (its tooltip's
// first line) is drawn; returns that frame's text, or "" when no line opens it.
static std::string hover(const SessionSnapshot& s,const ShuntCorrection& correction,const char* panel,const char* heading){
    std::string found;
    for(float dy=0;found.empty();dy+=3){
        auto* w=child(panel);if(!w||dy>w->Size.y)break;
        mouse={w->Pos.x+w->WindowPadding.x+6,w->Pos.y+dy};
        const auto text=frame(s,correction,true,{1500,2200});
        if(has(text,heading))found=text;
    }
    mouse={-FLT_MAX,-FLT_MAX};
    return found;
}
// Top of the glyphs a panel draws in its title's accent colour, and of every other text it
// draws (its frame, separator and anti-aliased fringes left out).
static std::pair<float,float> glyph_tops(const char* panel){
    const ImU32 accent=IM_COL32(74,163,255,255),border=ImGui::GetColorU32(ImGuiCol_Border),line=ImGui::GetColorU32(ImGuiCol_Separator);
    float title=FLT_MAX,text=FLT_MAX;
    if(auto* w=child(panel))for(const auto& v:w->DrawList->VtxBuffer){
        if(v.col==accent)title=std::min(title,v.pos.y);
        else if(v.col!=border && v.col!=line && (v.col&IM_COL32_A_MASK))text=std::min(text,v.pos.y);
    }
    return {title,text};
}
// Font metrics Python's tile_height() adds up: classic_px(1), the body line and the 26 px number line.
struct Metrics { float px=0,line=0,big=0; };
static Metrics metrics(){
    ImGui::GetIO().DisplaySize={400,400};ImGui::NewFrame();ImGui::Begin("metrics");
    Metrics m;m.px=ImGui::GetFontSize()/16;m.line=ImGui::GetTextLineHeight();
    ImGui::PushFont(nullptr,26.0f);m.big=ImGui::GetTextLineHeight();ImGui::PopFont();
    ImGui::End();ImGui::Render();return m;
}
// The logged line that follows the first line holding `value` after `card`, trimmed.
static std::string line_after(const std::string& text,const char* card,const char* value){
    const auto at=text.find(value,text.find(card));if(at==std::string::npos)return {};
    auto start=text.find('\n',at);if(start==std::string::npos)return {};
    start=text.find_first_not_of(" \r\n",start);if(start==std::string::npos)return {};
    const auto end=text.find_first_of("\r\n",start);
    return text.substr(start,end==std::string::npos?std::string::npos:end-start);
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);
    SessionSnapshot s;s.data.telemetry_valid=true;auto& t=s.data.telemetry;
    t.present=DC_T_XBAR|DC_T_TEMP_HOTSPOT|DC_T_POWER_W|DC_T_PCIE_WIDTH|DC_T_EVENT_MASK|DC_T_PL_NOW|DC_T_UTIL_GPU|DC_T_ENERGY_J;
    t.xbar=1700;t.temp_hotspot=87.5;t.power_w=100;t.pcie_width=8;t.event_mask=4|1;t.pl_now_mw=400000;t.util_gpu=97;t.energy_j=12.5;
    s.telemetry_stale_mask=DC_T_XBAR;ShuntCorrection correction;correction.valid=correction.active=correction.exact=true;correction.factor=2;
    const auto m=metrics();
    const auto revision=s.revision;auto text=frame(s,correction,true,{1500,2200});
    // Seven bordered cards in one row, equal to within a pixel and 8 px apart; the number and
    // its unit on separate lines.
    const char* cards[]={"CORE CLOCK","XBAR CLOCK","MEM CLOCK","EDGE TEMP","HOTSPOT","POWER","VCORE"};
    float card_w=0,card_h=0,last_right=-1;
    for(const char* name:cards){
        auto* w=child(name);CHECK(w && w->Size.x>100);
        if(card_w==0){card_w=w->Size.x;card_h=w->Size.y;}
        else CHECK(w->Pos.x==last_right+std::floor(8*m.px));
        last_right=w->Pos.x+w->Size.x;
        CHECK(w->Size.x>=card_w && w->Size.x<=card_w+1 && w->Size.y==card_h && w->Pos.y==child("CORE CLOCK")->Pos.y);
        CHECK(w->ContentSize.y<=w->Size.y); // every line fits; nothing clipped out of sight
    }
    // The row ends flush with the THROTTLE / POWER panels' right edge (Python colw =
    // (1500 - 30) // 2 twice, 8 px apart), not at Python relayout()'s tw = (W - 10 * 9) // 7,
    // which would end it 23 px short. tile_height(): title, unit, measured line and the two-line
    // shunt-corrected POWER subtitle, the 26 px number line, four 4 px gaps, 8 px padding
    // twice and the 2 px border.
    {auto* reasons=child("monitor-reasons");auto* power=child("monitor-power");CHECK(reasons && power);
     const float left=reasons->Pos.x,right=power->Pos.x+power->Size.x;
     CHECK(last_right==right && child("CORE CLOCK")->Pos.x==left && reasons->Size.x==std::floor((1500-30*m.px)/2));
     CHECK(card_w==std::floor((right-left-6*std::floor(8*m.px))/7));}
    CHECK(std::abs(card_h-(5*m.line+m.big+4*4*m.px+2*8*m.px+2*m.px))<1);
    // Each big number is followed by its own unit line.
    CHECK(line_after(text,"XBAR CLOCK","1700")=="MHz programmed (stale)");
    CHECK(line_after(text,"HOTSPOT","88")=="\xC2\xB0" "C");
    // Python s_pwr: shunt-corrected whole watts, then raw draw with the factor and the real ceiling.
    CHECK(line_after(text,"POWER","200")=="W" && line_after(text,"200","W")=="raw 100 W x2");
    CHECK(has(text,"limit 400 W = 800 W real") && !has(text,"W corrected"));
    // Clock tiles: the measured line and Python's subtitles (P-state, XBAR vs core).
    CHECK(line_after(text,"CORE CLOCK","MHz programmed")=="measured: --" && has(text,"P?"));
    CHECK(line_after(text,"XBAR CLOCK","measured: --")=="--"); // no core clock to compare with
    // A missing channel keeps its card with Python's "--" placeholder and its unit under it
    // (the fixture reads no core clock, edge temperature or VCORE).
    CHECK(line_after(text,"CORE CLOCK","--")=="MHz programmed");
    CHECK(line_after(text,"EDGE TEMP","--")=="\xC2\xB0" "C" && line_after(text,"VCORE","--")=="mV");
    // ALL CLOCK DOMAINS is a bordered panel straight under the tiles, before the lamps.
    auto* domains=child("pan_dom");
    CHECK(domains && domains->Pos.y>child("CORE CLOCK")->Pos.y+child("CORE CLOCK")->Size.y);
    CHECK(has(text,"ALL CLOCK DOMAINS  \xC2\xB7  private NvAPI GetAllClocks (0x1BD69F49)") && !has(text,"All clock domains (A / B)"));
    // Throttle lamps and power bars are paired bordered panels with Python's caps titles.
    auto* reasons=child("monitor-reasons");auto* power=child("monitor-power");
    CHECK(reasons && power && reasons->Pos.y==power->Pos.y && power->Pos.x>reasons->Pos.x && reasons->Size.y==power->Size.y);
    // Python colw = max(300, (W - 30) // 2), 8 px apart.
    CHECK(reasons->Size.x==std::floor((1500-30*m.px)/2) && power->Size.x==reasons->Size.x);
    CHECK(std::abs(power->Pos.x-(reasons->Pos.x+reasons->Size.x+8*m.px))<1);
    CHECK(has(text,"THROTTLE / CLOCKS-EVENT REASONS") && has(text,"\xE2\x97\x8F  SW power cap") && has(text,"\xE2\x97\x8F  Idle"));
    CHECK(has(text,"perf-decrease (NVAPI)") && has(text,"(not read)")); // perf mask unread is said, not shown clear
    CHECK(!has(text,"ACTIVE") && !has(text,"inactive"));
    CHECK(has(text,"POWER SPLIT & UTILIZATION") && has(text,"GPU util") && has(text,"97%") && has(text,"VID util"));
    CHECK(has(text,"25%   100 / 400 W")); // TDP overlay uses raw draw over the enforced limit
    // PCIe link and State are Python's bordered compact panels, paired under the lamps,
    // one bounded height, holding only Python's summary lines: no inner scrollbar.
    auto* pcie=child("monitor-pcie");auto* state=child("monitor-state");
    CHECK(pcie && state && pcie->Pos.y==state->Pos.y && state->Pos.x>pcie->Pos.x && pcie->Size.y==state->Size.y);
    CHECK(pcie->Pos.y>reasons->Pos.y+reasons->Size.y && pcie->ScrollMax.y==0 && state->ScrollMax.y==0);
    CHECK(has(text,"PCIE LINK") && has(text,"Gen ?  x8") && has(text,"errors: --  (since launch --)"));
    CHECK(has(text,"STATE") && has(text,"energy 12 J") && !has(text,"State & sensors") && !has(text,"PCIe link & errors"));
    CHECK(!has(text,"link and AER counters") && !has(text,"Link width") && !has(text,"Energy counter"));
    // Python add_text puts the summary on a frame-height line, as the title above it: one
    // title line and the spacing either side of the separator apart, never 3 px higher.
    {const auto tops=glyph_tops("monitor-pcie");const auto& style=ImGui::GetStyle();
     CHECK(tops.first<FLT_MAX && tops.second<FLT_MAX);
     CHECK(std::abs(tops.second-tops.first-(ImGui::GetFrameHeight()+2*style.ItemSpacing.y))<=1);}
    // Native-only readings stay reachable: hovering a panel's summary shows them. The power
    // readings moved out of the bar panel into the State panel's.
    text=hover(s,correction,"monitor-state","state and sensor readings");
    for(const char* moved:{"Energy counter","Power policy target","Configured board request","Reported power limit"})
        CHECK(text.find(moved)!=std::string::npos && text.find(moved)>text.find("energy 12 J"));
    CHECK(has(text,"Memory offset") && !has(text,"Link width"));
    text=hover(s,correction,"monitor-pcie","link and AER counters");
    CHECK(has(text,"Link width") && has(text,"PCIE LINK") && text.find("Link width")>text.find("PCIE LINK") && text.find("Lane")!=std::string::npos && !has(text,"Energy counter"));
    // Without a shunt factor the POWER subtitle is one line, and Python's tiles one line shorter.
    frame(s,ShuntCorrection{},true,{1500,2200});
    CHECK(std::abs(child("POWER")->Size.y-(card_h-m.line))<1);
    // Python relayout(): on a page too short for every lamp the two panels keep one
    // bounded height and scroll inside, so PCIe / State stay on the page.
    frame(s,correction,true,{1500,900});
    reasons=child("monitor-reasons");power=child("monitor-power");auto* page=ImGui::FindWindowByName("monitor test");
    const float short_h=reasons->Size.y;    CHECK(short_h==power->Size.y && short_h>=220 && reasons->ScrollMax.y>0);
    CHECK(page->ScrollMax.y==0);
    // Taller page: the panels take the extra height instead of leaving it below the page.
    frame(s,correction,true,{1500,1150});
    CHECK(child("monitor-reasons")->Size.y>short_h+100 && page->ScrollMax.y==0);
    CHECK(child("monitor-pcie")->Size.y>=110*ImGui::GetFontSize()/16-1);
    // The bands take the page down to the measured History header: no page scroll and
    // no empty strip below it (Python's fixed 102 px chrome would leave one).
    for(const float h:{1150.0f,1261.0f}){
        // Twice: the header is measured on one frame and scroll extents trail by one.
        frame(s,correction,true,{1500,h},true);frame(s,correction,true,{1500,h},true);
        const float tall=child("monitor-reasons")->Size.y;
        auto* last=child("monitor-pcie");
        const float bottom=page->Pos.y+page->Size.y-page->WindowPadding.y;
        const float gap=bottom-(last->Pos.y+last->Size.y+ImGui::GetStyle().ItemSpacing.y+ImGui::GetFrameHeight());
        CHECK(page->ScrollMax.y==0 && gap>=0 && gap<2 && tall>220);
        CHECK(last->ScrollMax.y==0 && child("monitor-state")->ScrollMax.y==0); // only Python's lines in them
    }
    t.fan_count=1;t.fans[0]={1,0,1,1234};s.fan_readings_stale=true;s.fan_rpm_stale_mask=1;
    frame(s,correction,true,{1500,2200});text=hover(s,correction,"monitor-state","state and sensor readings");
    CHECK(has(text,"1234 RPM (stale)"));
    CHECK(has(text,"0 %") && !has(text,"0 % (stale)")); // current duty zero is independent of stale RPM
    // Every telemetry read failed: cards, lamps and bars stay, with placeholders.
    SessionSnapshot failed;failed.data.telemetry_valid=false;failed.data.telemetry_error="Read temporarily unavailable";
    text=frame(failed,ShuntCorrection{},true,{1500,2200});
    for(const char* name:cards)CHECK(child(name)!=nullptr);
    CHECK(has(text,"power / limit unavailable") && has(text,"BUS util") && has(text,"Display clock") && has(text,"(not read)"));
    // Narrow window: cards wrap onto further rows instead of shrinking below Python's minimum.
    text=frame(s,correction,true,{420,2200});
    CHECK(child("VCORE")->Pos.y>child("CORE CLOCK")->Pos.y && child("CORE CLOCK")->Size.x>=110);
    CHECK(child("monitor-power")->Pos.y>child("monitor-reasons")->Pos.y);
    text=frame(s,correction,false,{640,768});
    CHECK(has(text,"XBAR CLOCK") && has(text,"Throttle / clocks-event reasons"));
    text=frame(s,correction,false,{640,2200}); // Focused keeps its inline reasons and power rows
    CHECK(has(text,"Performance-decrease reasons (NVAPI)") && has(text,"Power policy target") && text.find("Power policy target")<text.find("PCIe link"));
    // Focused tiles: a retained reading is marked stale, one decimal, corrected power with
    // the raw reading kept; its reasons header lists each bit as ACTIVE / inactive.
    CHECK(has(text,"1700 MHz driver (stale)") && has(text,"87.5 C") && !has(text,"\xC2\xB0" "C"));
    CHECK(has(text,"BOARD POWER") && has(text,"200.0 W corrected") && has(text,"100.0 W raw"));
    CHECK(!has(text,"SW power cap: ACTIVE")); // collapsed by default in Focused
    text=frame(s,correction,false,{640,2200},false,true);
    CHECK(has(text,"SW power cap: ACTIVE") && has(text,"inactive") && !has(text,"\xE2\x97\x8F"));
    CHECK(s.revision==revision && t.power_w==100 && t.event_mask==5);
    ImGui::DestroyContext();std::printf("monitor_view_test: %u checks passed\n",checks);
}
