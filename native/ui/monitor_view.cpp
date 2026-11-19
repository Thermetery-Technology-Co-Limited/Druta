// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "monitor_view.h"
#include "clock_monitor_view.h"
#include "clock_monitor_model.h"
#include "classic_layout.h"
#include <imgui.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
namespace druta::ui {
namespace {
float u(float value){return value*ImGui::GetFontSize()/18.0f;}
ImVec4 color(MonitorFreshness f){
    return f==MonitorFreshness::stale?ImVec4(0.96f,0.65f,0.25f,1):
        f==MonitorFreshness::unavailable?ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled):ImGui::GetStyleColorVec4(ImGuiCol_Text);
}
std::string shown(const MonitorReading& r){
    return (r.text.empty()?std::string("--"):r.text)+(r.unit.empty()?"":" ")+r.unit+(r.freshness==MonitorFreshness::stale?" (stale)":"");
}
void reading(const MonitorReading& r){
    ImGui::PushTextWrapPos(0);
    ImGui::TextColored(color(r.freshness),"%s",shown(r).c_str());
    ImGui::PopTextWrapPos();
}
void lamps(const char* title,const std::vector<MonitorReason>& reasons,bool classic){
    if(!ImGui::CollapsingHeader(title,classic?ImGuiTreeNodeFlags_DefaultOpen:0))return;
    const float right=ImGui::GetCursorScreenPos().x+ImGui::GetContentRegionAvail().x;
    bool first=true;
    for(const auto& r:reasons){
        const char* status=!r.active?"--":*r.active?"ACTIVE":"inactive";
        const auto tint=r.active && *r.active && r.freshness==MonitorFreshness::current?ImVec4(0.96f,0.65f,0.25f,1):color(r.freshness);
        const auto text=r.label+": "+status+(r.freshness==MonitorFreshness::stale?" (stale)":"");
        if(!first && ImGui::GetItemRectMax().x+u(18)+ImGui::CalcTextSize(text.c_str()).x<right)ImGui::SameLine(0,u(18));
        ImGui::PushStyleColor(ImGuiCol_Text,tint);ImGui::TextWrapped("%s",text.c_str());ImGui::PopStyleColor();first=false;
    }
}
void group(const MonitorGroup& data,bool open,bool classic){
    if(!ImGui::CollapsingHeader(data.label.c_str(),open?ImGuiTreeNodeFlags_DefaultOpen:0))return;
    if(!ImGui::BeginTable(data.label.c_str(),2,ImGuiTableFlags_SizingStretchProp|ImGuiTableFlags_RowBg))return;
    ImGui::TableSetupColumn("Reading",ImGuiTableColumnFlags_WidthStretch,1.1f);
    ImGui::TableSetupColumn("Value",ImGuiTableColumnFlags_WidthStretch,1.0f);
    for(const auto& r:data.readings){
        ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::TextWrapped("%s",r.label.c_str());ImGui::TableNextColumn();reading(r);
        if(r.percentage && std::isfinite(*r.percentage)){
            const float fraction=static_cast<float>(std::clamp(*r.percentage/100.0,0.0,1.0));
            ImGui::ProgressBar(fraction,ImVec2(-1,u(classic?3.0f:5.0f)),"");
        }
    }
    ImGui::EndTable();
}

// ---- Classic / Verbose: Python 1.9.1 build_monitor cards, lamps and bars ----
ImVec4 tone(MonitorTone t){
    // druta.py ACCENT, VIOLET, GOOD, WARN, BAD, IDLE_COL; dim is the classic TextDisabled (DIM).
    switch(t){
    case MonitorTone::accent:return {74/255.0f,163/255.0f,1,1};
    case MonitorTone::violet:return {160/255.0f,108/255.0f,1,1};
    case MonitorTone::good:return {70/255.0f,209/255.0f,122/255.0f,1};
    case MonitorTone::warn:return {1,203/255.0f,71/255.0f,1};
    case MonitorTone::bad:return {1,92/255.0f,92/255.0f,1};
    case MonitorTone::idle:return {58/255.0f,63/255.0f,75/255.0f,1};
    default:return ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled);
    }
}
// Page-level layout figures carried between frames (window state storage, keyed at
// the page's own ID level, never per panel).
float stored(const char* key,float fallback){return ImGui::GetStateStorage()->GetFloat(ImGui::GetID(key),fallback);}
void store(const char* key,float value){ImGui::GetStateStorage()->SetFloat(ImGui::GetID(key),value);}
float content_height(){return ImGui::GetCursorPosY()-ImGui::GetStyle().ItemSpacing.y+ImGui::GetStyle().WindowPadding.y;}
constexpr ImGuiWindowFlags fixed_child=ImGuiWindowFlags_NoScrollbar|ImGuiWindowFlags_NoScrollWithMouse;
// The client width Python's relayout() sizes from: the page and the window padding either side.
float client_width(){return ImGui::GetContentRegionAvail().x+2*ImGui::GetStyle().WindowPadding.x;}
// The width of each panel in the THROTTLE / POWER and PCIE / STATE rows: Python colw =
// max(300, (W - 30) // 2), never wider than two that fit side by side 8 px apart; one
// full-width column when two 300 px panels do not fit. `row` is where the row's right edge
// lands: ImGui truncates a child's size and position to whole pixels.
struct PanelColumns { float width=0,row=0; bool paired=false; };
PanelColumns panel_columns(){
    const float client=client_width(),gap=classic_px(8),avail=ImGui::GetContentRegionAvail().x;
    PanelColumns c;c.paired=avail>=classic_px(300)*2+gap;
    c.width=c.paired?std::min(std::max(classic_px(300),std::floor((client-classic_px(30))/2)),std::floor((avail-gap)/2)):avail;
    c.row=c.paired?2*std::floor(c.width)+std::floor(gap):std::floor(c.width);
    return c;
}
bool clock_tile(const MonitorReading& r){return r.key=="core"||r.key=="xbar"||r.key=="mem";}
// Python s_core: "P<pstate>", to which the measured line adds its state word.
std::string core_base(const SessionSnapshot& s){
    return monitor_freshness(s,DC_T_PSTATE)!=MonitorFreshness::unavailable?"P"+std::to_string(s.data.telemetry.pstate):std::string("P?");
}
// Python s_<key> other than the core's: the memory type, "+1515 vs core", the HOTSPOT delta,
// or the POWER tile's limit lines (raw draw and real limit too when shunt-corrected).
std::string tile_subtitle(const SessionSnapshot& s,const ShuntCorrection& correction,const MonitorReading& r){
    return r.key=="pwr"?classic_power_tile(s,r,correction).subtitle:r.detail;
}

void classic_tile(const SessionSnapshot& snapshot,const ShuntCorrection& correction,MonitorReading r,ImVec2 size,float wrap,float& tallest){
    // Python tile theme: WindowPadding 8/8, ItemSpacing 8x4, FramePadding 4x0.
    // Rows, top to bottom: title, number, unit, measured line (clock tiles), subtitle.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,ImVec2(classic_px(8),classic_px(8)));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,ImVec2(classic_px(8),classic_px(4)));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,ImVec2(classic_px(4),0));
    if(ImGui::BeginChild(r.label.c_str(),size,ImGuiChildFlags_Borders,fixed_child)){
        ImGui::TextDisabled("%s",r.label.c_str());
        std::optional<bool> exact;std::string subtitle=tile_subtitle(snapshot,correction,r);
        bool missing=r.freshness==MonitorFreshness::unavailable || r.text.empty();
        if(r.key=="pwr"){
            // Python t_pwr / s_pwr: shunt-corrected number, raw draw and the enforced limit.
            const auto power=classic_power_tile(snapshot,r,correction);
            r.text=power.value;exact=power.exact;missing=power.value=="--";
        }
        const bool stale=!missing && r.freshness==MonitorFreshness::stale;
        // A retained reading is dimmed and marked so it never reads as live.
        ImGui::PushFont(classic_fonts.heading,26.0f);
        ImGui::TextColored(stale?tone(MonitorTone::dim):tone(classic_tile_tone(snapshot,r,exact)),"%s",missing?"--":r.text.c_str());
        ImGui::PopFont();
        // Python wraps the unit, measured line and subtitle at sub_wrap(tw).
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+wrap);
        ImGui::TextDisabled("%s%s",r.unit.c_str(),stale?" (stale)":"");
        if(clock_tile(r)){
            const auto token=draw_clock_secondary(snapshot,r.key.c_str(),true);
            if(r.key=="core")subtitle=core_base(snapshot)+token;
        }
        if(!subtitle.empty())ImGui::TextDisabled("%s",subtitle.c_str());
        ImGui::PopTextWrapPos();
        tallest=std::max(tallest,content_height());
    }
    ImGui::EndChild();
    ImGui::PopStyleVar(3);
}
void classic_tiles(const SessionSnapshot& snapshot,const ShuntCorrection& correction){
    auto model=build_monitor_model(snapshot,true);
    // The page budget (Python relayout H) is the height from the top of the tiles
    // to the bottom of the page; the tile band is measured below.
    store("monitor-page-h",ImGui::GetContentRegionAvail().y);
    const float top=ImGui::GetCursorPosY();
    store("monitor-top",top);
    ImGui::PushID("monitor-sensor-tiles");
    // The row ends flush with the panel rows below, not at Python relayout()'s card width
    // (tw = max(120, (W - 10 * (n + 2)) // n)), which ends short of them on this page with
    // no scrollbar: the cards 8 px apart, at least 120 px each, the pixels left over one
    // each to the last cards. A window too narrow for one row wraps them onto further rows.
    // Whole pixels throughout, as ImGui places and sizes the cards.
    const float gap=std::floor(classic_px(8));
    const int count=static_cast<int>(model.tiles.size());
    const float n=static_cast<float>(std::max(count,1));
    const float row=panel_columns().row;
    int columns=std::max(count,1);
    if(row<classic_px(120)*n+gap*(n-1))
        columns=std::clamp(static_cast<int>((row+gap)/(classic_px(120)+gap)),1,std::max(count,1));
    const float spare=row-gap*static_cast<float>(columns-1);
    const float width=std::max(1.0f,std::floor(spare/static_cast<float>(columns)));
    const int wider=std::clamp(static_cast<int>(spare-width*static_cast<float>(columns)),0,columns);
    const float wrap=std::max(classic_px(80),width-classic_px(26));  // Python sub_wrap(tw)
    // Python tile_height(): the title, unit, measured line and subtitle, each as tall as
    // the tallest card needs at the wrap width, the big number's line, 4 px between the
    // items, 8 px padding above and below and the 2 px border. The measured line and the
    // core subtitle count every shape they take, so a card never grows when one changes.
    const float line=ImGui::GetTextLineHeight();
    ImGui::PushFont(classic_fonts.heading,26.0f);const float big=ImGui::GetTextLineHeight();ImGui::PopFont();
    const auto tall=[&](const std::string& text){return ImGui::CalcTextSize(text.empty()?"Ag":text.c_str(),nullptr,false,wrap).y;};
    const bool raw_memory=!snapshot.data.memory_divisor||!*snapshot.data.memory_divisor;
    float units=line,real=0,subs=line;
    for(const auto& r:model.tiles){
        units=std::max(units,tall(r.unit+(r.freshness==MonitorFreshness::stale?" (stale)":"")));
        subs=std::max(subs,tall(tile_subtitle(snapshot,correction,r)));
        if(clock_tile(r))for(const auto& shape:classic_clock_line_templates(raw_memory))real=std::max(real,tall(shape));
        if(r.key=="core")for(const auto& token:classic_core_tokens())subs=std::max(subs,tall(core_base(snapshot)+token));
    }
    const float items=real>0?5.0f:4.0f;
    const float estimate=line+units+big+real+subs+(items-1)*classic_px(4)+2*classic_px(8)+classic_px(2);
    const float height=std::max(estimate,stored("tile-height",0));
    float tallest=0;
    for(int i=0;i<count;++i){
        if(i%columns)ImGui::SameLine(0,gap);
        const float extra=i%columns>=columns-wider?1.0f:0.0f;
        classic_tile(snapshot,correction,model.tiles[static_cast<size_t>(i)],ImVec2(width+extra,height),wrap,tallest);
    }
    store("tile-height",tallest);
    ImGui::PopID();
    if(snapshot.paused)ImGui::TextWrapped("Telemetry paused; displayed readings are last known.");
    if(!snapshot.data.telemetry_error.empty())ImGui::TextWrapped("Telemetry: %s",snapshot.data.telemetry_error.c_str());
    store("monitor-tiles-h",ImGui::GetCursorPosY()-top-ImGui::GetStyle().ItemSpacing.y);
}
void panel_title(const char* title,const std::vector<MonitorReason>* reasons=nullptr){
    ImGui::AlignTextToFramePadding();ImGui::TextColored(tone(MonitorTone::accent),"%s",title);
    if(reasons){
        // Python has no unread state; an unread or retained mask is said, not shown as all clear.
        const auto f=reasons->empty()?MonitorFreshness::unavailable:reasons->front().freshness;
        if(f!=MonitorFreshness::current){ImGui::SameLine();ImGui::TextDisabled(f==MonitorFreshness::stale?"(stale)":"(not read)");}
    }
}
void lamp_rows(const std::vector<MonitorReason>& reasons){
    for(const auto& r:reasons){ImGui::AlignTextToFramePadding();ImGui::TextColored(tone(classic_lamp_tone(r)),"  \xE2\x97\x8F  %s",r.label.c_str());}
}
void classic_reason_panel(const MonitorModel& model){
    panel_title("THROTTLE / CLOCKS-EVENT REASONS",&model.clock_reasons);
    ImGui::Separator();
    lamp_rows(model.clock_reasons);
    ImGui::Dummy(ImVec2(0,classic_px(4)));
    ImGui::AlignTextToFramePadding();ImGui::TextDisabled("perf-decrease (NVAPI)");
    if(!model.perf_reasons.empty() && model.perf_reasons.front().freshness!=MonitorFreshness::current){
        ImGui::SameLine();ImGui::TextDisabled(model.perf_reasons.front().freshness==MonitorFreshness::stale?"(stale)":"(not read)");
    }
    lamp_rows(model.perf_reasons);
}
void classic_power_panel(const SessionSnapshot& snapshot,const MonitorModel& model){
    panel_title("POWER SPLIT & UTILIZATION");
    ImGui::Separator();
    // Python BARS colours, in model order: GPU, Board, TDP used, then four utilization bars.
    const MonitorTone base[]={MonitorTone::accent,MonitorTone::violet,MonitorTone::warn,
        MonitorTone::good,MonitorTone::good,MonitorTone::good,MonitorTone::good};
    const auto& bars=model.groups[0].readings;
    for(size_t i=0;i<bars.size();++i){
        const auto& r=bars[i];
        ImGui::AlignTextToFramePadding();ImGui::TextDisabled("%s",r.label.c_str());
        const float fraction=r.percentage && std::isfinite(*r.percentage)?
            static_cast<float>(std::clamp(*r.percentage/100.0,0.0,1.0)):0.0f;
        const auto overlay=classic_bar_overlay(snapshot,r,i==2);
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram,tone(classic_bar_tone(r,i<std::size(base)?base[i]:MonitorTone::good)));
        ImGui::ProgressBar(fraction,ImVec2(-1,0),overlay.c_str());
        ImGui::PopStyleColor();
    }
}
// Native-only readings behind Python's summary lines: hovering the summary shows them, so
// the panel itself holds only Python's text and empty space, with no inner scrollbar.
void classic_rows_tooltip(const MonitorGroup& data,const char* heading){
    if(!ImGui::IsItemHovered())return;
    ImGui::BeginTooltip();
    ImGui::TextDisabled("%s",heading);
    if(ImGui::BeginTable(data.label.c_str(),2,ImGuiTableFlags_SizingFixedFit)){
        for(const auto& r:data.readings){
            ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::TextDisabled("%s",r.label.c_str());
            ImGui::TableNextColumn();ImGui::TextColored(color(r.freshness),"%s",shown(r).c_str());
            if(r.percentage && std::isfinite(*r.percentage))
                ImGui::ProgressBar(static_cast<float>(std::clamp(*r.percentage/100.0,0.0,1.0)),ImVec2(classic_px(120),u(3.0f)),"");
        }
        ImGui::EndTable();
    }
    ImGui::EndTooltip();
}
void classic_pcie_panel(const SessionSnapshot& snapshot,const MonitorModel& model){
    panel_title("PCIE LINK");
    ImGui::Separator();
    const auto summary=classic_pcie_summary(snapshot);
    // Python add_text: on a frame-height line, the text 3 px down.
    ImGui::AlignTextToFramePadding();ImGui::TextColored(tone(summary.tone),"%s",summary.text.c_str());
    classic_rows_tooltip(model.groups[1],"link and AER counters");
}
void classic_state_panel(const SessionSnapshot& snapshot,const MonitorModel& model){
    panel_title("STATE");
    ImGui::Separator();
    ImGui::AlignTextToFramePadding();ImGui::TextUnformatted(classic_state_summary(snapshot).c_str());
    classic_rows_tooltip(model.groups[2],"state and sensor readings");
}
// Python pan_thr | pan_pwr and pan_pcie | pan_state: two bordered panels of equal
// width at the height relayout() gives the row, stacked (each at that height) when
// the page is narrower than two 300 px columns. Longer content scrolls inside.
// Returns the height the row took (two heights and a gap when stacked).
template<class Left,class Right>
float classic_pair(const char* left_id,const char* right_id,float height,Left left,Right right){
    const auto columns=panel_columns();const float width=columns.width;const bool paired=columns.paired;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,ImVec2(classic_px(8),classic_px(8)));
    const float gap=classic_px(8);
    if(ImGui::BeginChild(left_id,ImVec2(width,height),ImGuiChildFlags_Borders))left();
    ImGui::EndChild();
    if(paired)ImGui::SameLine(0,gap);
    if(ImGui::BeginChild(right_id,ImVec2(width,height),ImGuiChildFlags_Borders))right();
    ImGui::EndChild();
    ImGui::PopStyleVar();
    return paired?height:height*2+ImGui::GetStyle().ItemSpacing.y;
}
void classic_details(const SessionSnapshot& snapshot){
    const auto model=build_monitor_model(snapshot,true);
    // Python relayout(): the page shares its height between the tile band, the
    // domain panel, the throttle / power row and the PCIe / state row.
    const float tiles=stored("monitor-tiles-h",0);
    const float page=stored("monitor-page-h",ImGui::GetContentRegionAvail().y+tiles);
    // Python subtracts a fixed 102 px for the chrome between and below the bands; the
    // native gaps and the trailing History header are measured on the previous frame.
    const float details=stored("monitor-chrome-details",-1);
    const float chrome=details<0?-1:details+stored("monitor-trailer",0);
    const auto layout=classic_monitor_layout(page,tiles,stored("monitor-dom-content",0),classic_px(1),chrome);
    ImGui::Dummy(ImVec2(0,classic_px(6)));
    // Directly under the tiles, as in Python: the panel qualifies their numbers.
    store("monitor-dom-content",draw_clock_monitor_table(snapshot,true,layout.dom));
    ImGui::PushID("monitor-details");
    ImGui::Dummy(ImVec2(0,classic_px(6)));
    float bands=layout.dom+classic_pair("monitor-reasons","monitor-power",layout.mid,
        [&]{classic_reason_panel(model);},[&]{classic_power_panel(snapshot,model);});
    ImGui::Dummy(ImVec2(0,classic_px(6)));
    bands+=classic_pair("monitor-pcie","monitor-state",layout.bot,
        [&]{classic_pcie_panel(snapshot,model);},[&]{classic_state_panel(snapshot,model);});
    ImGui::PopID();
    const float end=ImGui::GetCursorPosY();
    store("monitor-details-end",end);
    store("monitor-trailer",0); // until the page reports what it draws below the bands
    store("monitor-chrome-details",std::max(0.0f,
        end-ImGui::GetStyle().ItemSpacing.y-stored("monitor-top",end)-tiles-bands));
}
}
void draw_monitor_tiles(const SessionSnapshot& snapshot,const ShuntCorrection& correction,bool classic){
    if(classic){classic_tiles(snapshot,correction);return;}
    auto model=build_monitor_model(snapshot,classic);
    // Core/XBAR stay in driver clock units. Classic MEM follows Python mem_fmt: true MHz
    // only from the adapter's reported memory divisor, else raw and marked
    // "(raw)". No private-array B physical-measurement claim is introduced here.
    const int columns=std::clamp(static_cast<int>(ImGui::GetContentRegionAvail().x/u(classic?135.0f:155.0f)),1,7);
    if(ImGui::BeginTable("monitor-sensor-tiles",columns,ImGuiTableFlags_SizingStretchSame)){
        for(auto& r:model.tiles){
            ImGui::TableNextColumn();ImGui::TextDisabled("%s",r.label.c_str());
            if(r.key=="pwr" && r.freshness!=MonitorFreshness::unavailable && correction.valid && correction.active){
                const auto watts=correction.apply(snapshot.data.telemetry.power_w);
                if(watts && std::isfinite(*watts)){
                    char text[80]{};std::snprintf(text,sizeof(text),"%.1f",*watts);
                    const std::string raw=r.text;r.text=text;r.unit=correction.exact?"W corrected":"W estimate";
                    reading(r);ImGui::TextDisabled("%s W raw",raw.c_str());continue;
                }
            }
            reading(r);
            if(r.key=="core")draw_clock_secondary(snapshot,"core",classic);
            else if(r.key=="xbar")draw_clock_secondary(snapshot,"xbar",classic);
            else if(r.key=="mem"){
                draw_clock_secondary(snapshot,"mem",classic);
                if(!r.detail.empty()){ImGui::PushTextWrapPos(0);ImGui::TextDisabled("%s",r.detail.c_str());ImGui::PopTextWrapPos();}
            }
        }
        ImGui::EndTable();
    }
    if(snapshot.paused)ImGui::TextWrapped("Telemetry paused; displayed readings are last known.");
    if(!snapshot.data.telemetry_error.empty())ImGui::TextWrapped("Telemetry: %s",snapshot.data.telemetry_error.c_str());
}
void note_classic_monitor_page_end(){
    const float end=ImGui::GetCursorPosY();
    store("monitor-trailer",std::max(0.0f,end-stored("monitor-details-end",end)));
}
void draw_monitor_details(const SessionSnapshot& snapshot,bool classic){
    if(classic){classic_details(snapshot);return;}
    const auto model=build_monitor_model(snapshot,classic);ImGui::PushID("monitor-details");
    draw_clock_monitor_table(snapshot,classic);
    lamps("Throttle / clocks-event reasons",model.clock_reasons,classic);
    lamps("Performance-decrease reasons (NVAPI)",model.perf_reasons,classic);
    group(model.groups[0],true,classic);
    const bool paired=ImGui::GetContentRegionAvail().x>=u(700);
    if(paired && ImGui::BeginTable("monitor-paired-details",2,ImGuiTableFlags_SizingStretchSame)){
        ImGui::TableNextColumn();group(model.groups[1],classic,classic);
        ImGui::TableNextColumn();group(model.groups[2],classic,classic);
        ImGui::EndTable();
    }else if(!paired){group(model.groups[1],classic,classic);group(model.groups[2],classic,classic);}
    ImGui::PopID();
}
}
