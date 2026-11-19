// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_monitor_view.h"
#include "clock_monitor_model.h"
#include "monitor_model.h"
#include "classic_layout.h"
#include <imgui.h>
#include <iterator>
namespace druta::ui {
namespace {
float u(float value){return value*ImGui::GetFontSize()/18.0f;}
ImVec4 tint(const std::string& band,bool stale){
    if(stale)return ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled);
    if(band=="bad")return {0.95f,0.30f,0.28f,1};
    if(band=="warn")return {0.96f,0.65f,0.25f,1};
    if(band=="ok")return ImGui::GetStyleColorVec4(ImGuiCol_Text);
    return ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled);
}
// Python REAL_COL / DOM_BAND_COL / GRADE_COL: TEXT, WARN, BAD, DIM. TEXT is 1.9.1's
// own (230,232,236), set explicitly on these items, not the theme's white.
ImVec4 classic_colour(const std::string& role){
    if(role=="bad")return {1,92/255.0f,92/255.0f,1};
    if(role=="warn")return {1,203/255.0f,71/255.0f,1};
    if(role=="text")return {230/255.0f,232/255.0f,236/255.0f,1};
    if(role=="accent")return {74/255.0f,163/255.0f,1,1};
    return ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled);
}
bool stale(const SessionSnapshot& s){return s.paused||!s.data.connected||(s.telemetry_stale_mask&DC_T_PRIV_CLOCKS);}
uint64_t tile_bit(const std::string& key){return key=="core"?DC_T_CORE:key=="xbar"?DC_T_XBAR:DC_T_MEM;}
// Python nvbackend PRIV_UNAVAIL, the error 1.9.1 shows when the private getter gives no payload.
constexpr const char* priv_unavailable="private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer";
// The whole snapshot is a retained one (paused, disconnected, or the telemetry read failed):
// Python then redraws its last snapshot, which carries no getter error.
bool retained(const SessionSnapshot& s){return s.paused||!s.data.connected||!s.data.telemetry_error.empty();}
// This snapshot holds no current private clock payload.
bool private_missing(const SessionSnapshot& s){
    return !s.data.telemetry_valid||!(s.data.telemetry.present&DC_T_PRIV_CLOCKS)||(s.telemetry_stale_mask&DC_T_PRIV_CLOCKS);
}
// Classic's wording for a classification that gave no current rows: Python's sentence when
// the getter gave no payload; the native cause (the classifier, which Python lacks) otherwise.
std::string classic_error(const SessionSnapshot& s){
    if(!s.data.clock_monitor||s.data.clock_monitor->message.empty())return {};
    return private_missing(s)?std::string(priv_unavailable):s.data.clock_monitor->message;
}
std::string classic_secondary(const SessionSnapshot& s,const char* key){
    // Python real_clock_view: "measured: --" before any private reading, "n/a" when the
    // getter reports an error, else the tile's paired row; colour only for a judged core.
    // The caller's wrap position applies (Python wraps the line at the tile's sub_wrap).
    ClassicClockTip tip;tip.key=key;
    const auto& t=s.data.telemetry;
    if(monitor_freshness(s,tile_bit(tip.key))!=MonitorFreshness::unavailable)
        tip.tile=tip.key=="mem"?memory_clock_text(s.data,static_cast<double>(t.mem)).value:std::to_string(tip.key=="core"?t.core:t.xbar);
    tip.memory_divided=s.data.memory_divisor&&*s.data.memory_divisor;
    std::string line="measured: --",role="dim";ClassicClockLine shown;
    if(s.data.clock_monitor){
        const auto& data=*s.data.clock_monitor;
        for(const auto& r:data.readings)if(r.key==tip.key&&r.available)tip.reading=&r;
        tip.rows=data.available&&data.message.empty();tip.error=classic_error(s);
        // Python: "n/a" once the getter has failed and no last reading is kept for this tile.
        if(!tip.reading){if(!data.available||!data.message.empty())line=tip.error.empty()?"measured: --":"measured: n/a";}
        else{
            const auto* found=tip.reading;
            tip.stale=s.paused||!s.data.connected||(s.telemetry_stale_mask&tile_bit(tip.key));
            tip.failed=data.stale||found->stale||(s.telemetry_stale_mask&DC_T_PRIV_CLOCKS);
            tip.divisor=found->key=="mem"&&tip.memory_divided?static_cast<double>(*s.data.memory_divisor):1.0;
            shown=classic_clock_line(found,tip.stale,tip.failed,tip.divisor);line=shown.line;
            const bool judged=!tip.stale&&!tip.failed&&found->trust=="measured"&&found->state=="judged"&&found->key=="core";
            if(judged&&(found->verdict=="ok"||found->verdict=="warn"||found->verdict=="bad"))role=found->verdict=="ok"?"text":found->verdict;
        }
    }
    ImGui::TextColored(classic_colour(role),"%s",line.c_str());
    if(ImGui::IsItemHovered()){ImGui::BeginTooltip();ImGui::PushTextWrapPos(classic_px(520));
        ImGui::TextUnformatted(classic_clock_tooltip(tip).c_str());ImGui::PopTextWrapPos();ImGui::EndTooltip();}
    return shown.token;
}
}
std::string draw_clock_secondary(const SessionSnapshot& s,const char* key,bool classic){
    if(classic)return classic_secondary(s,key);
    if(!s.data.clock_monitor){ImGui::TextDisabled("B: not read yet");return {};}
    const auto& data=*s.data.clock_monitor;
    for(const auto& r:data.readings)if(r.key==key){
        const uint64_t bit=tile_bit(r.key);
        const bool old=stale(s)||data.stale||r.stale||(s.telemetry_stale_mask&bit);
        const bool judged=!old&&r.state=="judged"&&r.trust=="measured"&&r.key=="core";
        const auto text=clock_secondary_text(r,old,1.0);
        ImGui::PushTextWrapPos(0);ImGui::TextColored(tint(judged?r.verdict:"",old),"%s",text.c_str());ImGui::PopTextWrapPos();
        if(ImGui::IsItemHovered()){ImGui::BeginTooltip();ImGui::PushTextWrapPos(u(500));
            ImGui::TextUnformatted(clock_secondary_explanation(r,1.0).c_str());ImGui::PopTextWrapPos();ImGui::EndTooltip();}
        return {};
    }
    ImGui::TextDisabled("B: --");
    return {};
}
namespace {
void focused_table(const SessionSnapshot& s){
    if(!ImGui::CollapsingHeader("All clock domains (A / B)",ImGuiTreeNodeFlags_DefaultOpen))return;
    ImGui::TextWrapped("A / B are driver-reported arrays. Names: plain = confirmed; ? = inferred; -- = unidentified. "
        "2CLK rows retain doubled MHz here; tile evidence divides by row scale. PCIe domain values retain generation/raw units. "
        "Gap colors compare B - A, adjusted by row scale, with this adapter's measured graphics bin (one / three bins). "
        "This is a difference policy, not proof that B is a physical measurement.");
    if(!s.data.clock_monitor){ImGui::TextDisabled("Waiting for clock classification.");return;}
    const auto& data=*s.data.clock_monitor;const bool old=stale(s)||data.stale;
    if(old)ImGui::TextDisabled("Last reported clock rows (stale); no current gap verdict.");
    if(!data.message.empty())ImGui::TextWrapped("%s",data.message.c_str());
    if(!data.available)return;
    const auto flags=ImGuiTableFlags_RowBg|ImGuiTableFlags_BordersInnerV|ImGuiTableFlags_ScrollX|ImGuiTableFlags_ScrollY;
    if(ImGui::BeginTable("clock-domain-rows",9,flags,ImVec2(0,u(220.0f)))){
        ImGui::TableSetupColumn("Dom",ImGuiTableColumnFlags_WidthFixed,u(36));
        ImGui::TableSetupColumn("Domain",ImGuiTableColumnFlags_WidthFixed,u(125));
        ImGui::TableSetupColumn("A (reported)",ImGuiTableColumnFlags_WidthFixed,u(102));
        ImGui::TableSetupColumn("B (reported)",ImGuiTableColumnFlags_WidthFixed,u(102));
        ImGui::TableSetupColumn("B - A (MHz)",ImGuiTableColumnFlags_WidthFixed,u(100));
        ImGui::TableSetupColumn("Grade",ImGuiTableColumnFlags_WidthFixed,u(100));
        ImGui::TableSetupColumn("Scale",ImGuiTableColumnFlags_WidthFixed,u(45));
        ImGui::TableSetupColumn("Flags",ImGuiTableColumnFlags_WidthFixed,u(90));
        ImGui::TableSetupColumn("Srcid",ImGuiTableColumnFlags_WidthFixed,u(90));
        ImGui::TableSetupScrollFreeze(2,1);ImGui::TableHeadersRow();
        for(const auto& row:data.rows){
            ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::Text("%u",row.raw.domain);
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.label.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.a.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.b.c_str());
            ImGui::TableNextColumn();ImGui::TextColored(tint(row.band,old),"%s",row.delta.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.raw.grade);
            ImGui::TableNextColumn();ImGui::Text("%u",row.raw.scale);
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.flags.c_str());
            ImGui::TableNextColumn();ImGui::TextUnformatted(row.source.c_str());
        }ImGui::EndTable();
    }
}
// One table cell on Python's frame-height text line; mono for figures compared row to row.
void cell(const char* text,const ImVec4& colour,bool mono){
    ImGui::TableNextColumn();ImGui::AlignTextToFramePadding();
    if(mono)push_classic_mono();
    ImGui::TextColored(colour,"%s",text);
    if(mono)ImGui::PopFont();
}
constexpr const char* domain_tooltip=
    "Two arrays cover 32 private clock domains:\n"
    "A: 2*d {frequency, flags}; B: 64+7*d {frequency, srcid, ...}.\n"
    "On TU102, A tracks the programmed target and B behaves as\n"
    "a measured counter. On GK104/GM107, A and B matched exactly in\n"
    "idle, boost and held-P0 samples; B is not independent proof\n"
    "of the physical clock. Other GPUs need their own validation.\n\n"
    "GPC2CLK and other 2CLK labels retain the doubled units\n"
    "returned by the driver. MEM retains the raw driver units;\n"
    "the memory tile converts these for the memory technology.\n\n"
    "Kepler/Maxwell names with '?' follow ROM/state correlation.\n"
    "GTX 770 and GM107: 16 matches XBAR, 17 matches SYS.\n"
    "GTX 770 also separates domain 25 as L2C.\n"
    "Domain 31, where populated, is shown as link generation\n"
    "with its unidentified B field displayed raw.";
float classic_table(const SessionSnapshot& s,float height){
    // Python build_domains: a bordered panel sized by relayout(), scrolling inside.
    float content=0;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,ImVec2(classic_px(8),classic_px(8)));
    if(ImGui::BeginChild("pan_dom",ImVec2(-1,height),ImGuiChildFlags_Borders,ImGuiWindowFlags_HorizontalScrollbar)){
        ImGui::AlignTextToFramePadding();
        ImGui::TextColored(classic_colour("accent"),"ALL CLOCK DOMAINS  \xC2\xB7  private NvAPI GetAllClocks (0x1BD69F49)");
        if(ImGui::IsItemHovered()){ImGui::BeginTooltip();ImGui::TextUnformatted(domain_tooltip);ImGui::EndTooltip();}
        ImGui::Separator();
        ImGui::PushStyleColor(ImGuiCol_Text,ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
        ImGui::TextWrapped("A / B = driver-reported clock arrays. TU102: target / measured counter; GK104/GM107: identical "
            "in tested states. 2CLK = doubled clock units. Delta colors: per reading, not gated by load or settling "
            "(dim = within one graphics bin, amber = one bin, red = three; 0.5 MHz tolerance in core MHz, 2CLK rows "
            "halved first; MEM rows use the graphics bin too).\n"
            "Names: plain = confirmed; '?' = inferred; '--' = unidentified.");
        ImGui::PopStyleColor();
        const ClockMonitorData* data=s.data.clock_monitor.get();
        const bool old=stale(s)||(data&&data->stale);
        // Python dom_err: the getter's error in red. A retained snapshot redraws as it was.
        const bool keep=retained(s);
        const std::string error=keep?std::string():classic_error(s);
        if(!error.empty()){ImGui::PushTextWrapPos(0);ImGui::TextColored(classic_colour("bad"),"%s",error.c_str());ImGui::PopTextWrapPos();}
        // Python hides every row when the getter gave no payload. The last rows stay, said to
        // be stale, for a retained snapshot or a classifier failure (native only).
        const bool hidden=!keep&&data&&!data->message.empty()&&private_missing(s);
        const bool rows=data&&data->available&&!hidden;
        if(old&&rows)ImGui::TextDisabled("Last reported clock rows (stale); no current gap verdict.");
        const auto flags=ImGuiTableFlags_SizingFixedFit|ImGuiTableFlags_NoHostExtendX|ImGuiTableFlags_BordersInnerH|ImGuiTableFlags_BordersInnerV;
        // Dear PyGui 2.3.1's default table colours (ImGui 1.92.5 dark): header (48,48,51),
        // header underline (79,79,89), inner grid (59,59,64). The vendored 1.93 defaults are
        // lighter. The borders are read in BeginTable and drawn in EndTable.
        const auto rgb=[](int r,int g,int b){return ImVec4(r/255.0f,g/255.0f,b/255.0f,1);};
        ImGui::PushStyleColor(ImGuiCol_TableHeaderBg,rgb(48,48,51));
        ImGui::PushStyleColor(ImGuiCol_TableBorderStrong,rgb(79,79,89));
        ImGui::PushStyleColor(ImGuiCol_TableBorderLight,rgb(59,59,64));
        // Python refresh_domains: the table and its header row are always there, with a row
        // for each domain the reading classified (none before the first reading).
        if(ImGui::BeginTable("dom_table",7,flags)){
            // Python DOM_COLS, unscaled px.
            const std::pair<const char*,float> columns[]={{"dom",46.0f},{"domain",165.0f},{"reported  A",145.0f},
                {"reported  B",145.0f},{"\xCE\x94  B-A",135.0f},{"flags",90.0f},{"srcid",80.0f}};
            for(const auto& c:columns)ImGui::TableSetupColumn(c.first,ImGuiTableColumnFlags_WidthFixed,classic_px(c.second));
            ImGui::TableHeadersRow();
            const auto text=classic_colour("text"),dim=classic_colour("dim");
            if(rows)for(const auto& row:data->rows){
                const auto c=classic_domain_cells(row);
                ImGui::TableNextRow();
                cell(c.index.c_str(),dim,true);
                cell(c.name.c_str(),old?dim:classic_colour(c.grade_tone),false);
                cell(c.a.c_str(),text,true);
                cell(c.b.c_str(),text,true);
                // Python DOM_BAND_COL: within one bin (or no usable bin) dim.
                cell(c.delta.c_str(),old||row.band.empty()||row.band=="ok"?dim:classic_colour(row.band),true);
                cell(c.flags.c_str(),text,true);
                cell(c.srcid.c_str(),text,true);
            }
            ImGui::EndTable();
        }
        ImGui::PopStyleColor(3);
        content=ImGui::GetCursorPosY()-ImGui::GetStyle().ItemSpacing.y+ImGui::GetStyle().WindowPadding.y;
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();
    return content;
}
}
float draw_clock_monitor_table(const SessionSnapshot& s,bool classic,float classic_height){
    if(!classic){focused_table(s);return 0;}
    return classic_table(s,classic_height>0?classic_height:classic_px(300));
}
}
