/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timings_view.h"
#include "classic_layout.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <initializer_list>
#include <optional>
#include <cstdlib>
#include <windows.h>
#include <filesystem>
#include "profile_io.h"
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static bool has(const std::string& text,const char* part){return text.find(part)!=std::string::npos;}
static TimingCapture capture_at(uint32_t mhz,std::vector<std::pair<const char*,std::optional<uint32_t>>> fields,bool structural=false){
 TimingCapture c;c.memory_mhz=mhz;for(const auto& [name,cycles]:fields){TimingCaptureField f;f.name=name;f.reg="CONFIG0";f.bits="[7:0]";f.structural=structural;f.cycles=cycles;c.fields.push_back(f);}return c;
}
// Released 1.9.1 timings.compare / _verdict, ported as compare_timing_captures.
static void comparison_model(){
 // timings.py cycles_agree docstring: RFC 13 cycles at 405 and 210 at 7428 agree.
 CHECK(timing_cycles_agree(13,210,7428.0/405.0));CHECK(!timing_cycles_agree(10,40,10.0));
 auto one=compare_timing_captures({capture_at(7000,{{"RC",78}})});
 CHECK(one.order.size()==1&&one.rows.empty()&&one.clock_ratios.empty());
 CHECK(compare_timing_captures({}).order.empty());
 // Given fastest first: the slowest capture is still the baseline.
 auto fast=capture_at(4000,{{"TRACK",100},{"FLAT",7},{"PART",40},{"GONE",9},{"ZERO",0}});
 auto slow=capture_at(400,{{"TRACK",10},{"FLAT",7},{"PART",10},{"GONE",9},{"ZERO",0}});
 fast.fields.erase(fast.fields.begin()+3);
 const auto cmp=compare_timing_captures({fast,slow});
 CHECK(cmp.order.size()==2&&cmp.order[0]==1&&cmp.order[1]==0);
 CHECK(cmp.clock_ratios.size()==2&&cmp.clock_ratios[0]==1.0&&cmp.clock_ratios[1]==10.0);
 CHECK(cmp.rows.size()==5&&cmp.rows[0].name=="TRACK"&&cmp.rows[0].verdict=="tracks");
 CHECK(cmp.rows[0].cycles[0]==10u&&cmp.rows[0].cycles[1]==100u&&cmp.rows[0].ratios[1]&&*cmp.rows[0].ratios[1]==10.0);
 CHECK(cmp.rows[1].verdict=="flat"&&has(cmp.rows[1].note,"counted in cycles by design"));
 CHECK(cmp.rows[2].verdict=="partial"&&has(cmp.rows[2].note,"moved, but -60% off the clock ratio")&&has(cmp.rows[2].note,"VBIOS also relaxes"));
 CHECK(cmp.rows[3].name=="GONE"&&!cmp.rows[3].cycles[1]&&!cmp.rows[3].ratios[1]&&cmp.rows[3].verdict=="--");
 CHECK(cmp.rows[4].verdict=="--"&&!cmp.rows[4].ratios[0]); // a zero baseline has no ratio
 // A structural fragment that misses the ratio is expected, and said so.
 const auto split=compare_timing_captures({capture_at(400,{{"LO",10}},true),capture_at(4000,{{"LO",40}},true)});
 CHECK(split.rows[0].verdict=="partial"&&has(split.rows[0].note,"fragment of a split field"));
 // Clocks within the flat tolerance prove nothing either way.
 const auto close_clocks=compare_timing_captures({capture_at(1000,{{"RC",10}}),capture_at(1050,{{"RC",30}})});
 CHECK(close_clocks.rows[0].verdict=="--"&&has(close_clocks.rows[0].note,"effectively the same clock"));
 // A capture with no memory clock never enters the comparison.
 CHECK(compare_timing_captures({capture_at(0,{{"RC",1}}),capture_at(400,{{"RC",1}})}).order.size()==1);
}
static TimingSnapshot parity_snapshot(){
 TimingSnapshot s;s.ok=true;s.slot="0000:09:00.0";s.codename="GA102";s.pci_id="10de:2203";s.aperture="0x9A0000";s.boot0="0xB72000A1";
 s.memory_before_mhz=s.memory_after_mhz=10501;s.memory_clock_stable=true;s.pstate_before=s.pstate_after=0;s.offset_before=s.offset_after=0;
 s.memory_states={405,810,10501};s.memory_type="GDDR6X";s.top_band=true;
 s.diagnostic_evidence.memory_clock_divisor=8;s.diagnostic_evidence.divisor_provenance="adapter";
 s.diagnostic_evidence.partitions={{"fbpa0",{}},{"fbpa1",{}}};
 TimingField rc;rc.name="RC";rc.reg="CONFIG0";rc.lo=0;rc.hi=7;rc.maximum=255;rc.description="Row cycle time";
 TimingField train;train.name="TRAIN";train.reg="CONFIG0";train.lo=8;train.hi=15;train.maximum=255;train.structural=true;train.description="Training [structural]";
 s.fields={rc,train};s.broadcast={{"CONFIG0","DOCUMENTED",0x1000,0x2885A46A},{"CONFIG1","DOCUMENTED",0x1004,0x7A2A83A2}};
 s.diagnostic_evidence.units.push_back({"RC","CONFIG0","GA102",0,7,true,true,"test units"});
 return s;
}
static void header_and_tags(){
 auto s=parity_snapshot();auto h=timing_header(s);
 CHECK(h.ident=="GA102  \u00b7  PCI 10de:2203  \u00b7  slot 0000:09:00.0  \u00b7  FBPA aperture 0x9A0000  \u00b7  BOOT_0 0xB72000A1");
 CHECK(has(h.clock,"sampled at memory clock 10501 reported  =  1313 MHz true (GDDR6X)")&&has(h.clock,"2 partitions")&&h.clock_trustworthy);
 CHECK(h.words=="CONFIG0 0X2885A46A   CONFIG1 0X7A2A83A2"&&h.warnings.empty());
 s.saved_broadcast={{"CONFIG0","",0,0x2885A46A},{"TIMING22","",0,0x12400389}};CHECK(timing_header(s).words=="CONFIG0 0X2885A46A   TIMING22 0X12400389");s.saved_broadcast.clear();
 s.pci_id.clear();CHECK(has(timing_header(s).ident,"PCI --"));
 s.memory_after_mhz=810;s.memory_clock_stable=false;h=timing_header(s);
 CHECK(!h.clock_trustworthy&&h.warnings.size()==1&&has(h.warnings[0],"THE MEMORY CLOCK MOVED DURING CAPTURE: 10501 -> 810"));
 s=parity_snapshot();s.diagnostic_evidence.memory_clock_divisor.reset();s.offset_after.reset();h=timing_header(s);
 CHECK(!h.clock_trustworthy&&!has(h.clock,"MHz true")&&h.warnings.size()==2&&has(h.warnings[0],"memory type 'GDDR6X' has no known true-clock divisor"));
 s=parity_snapshot();s.codename="UNKNOWN_CHIP";CHECK(has(timing_header(s).warnings[0],"no confirmed timing layout for UNKNOWN_CHIP"));
 TimingSnapshot failed;failed.message="helper timed out";h=timing_header(failed);CHECK(h.ident=="no snapshot"&&h.clock=="helper timed out"&&h.words.empty());
 s=parity_snapshot();CHECK(timing_state_tag(s)=="P0");
 s.pstate_after=2;CHECK(timing_state_tag(s)=="P2 band");
 s.memory_before_mhz=s.memory_after_mhz=810;s.pstate_before=s.pstate_after=8;CHECK(timing_state_tag(s)=="idle");
 s.pstate_before=s.pstate_after=0;CHECK(timing_state_tag(s)=="idle");
 s.memory_states.clear();CHECK(timing_state_tag(s)=="?");
 s=parity_snapshot();s.pstate_before.reset();CHECK(timing_state_tag(s)=="?");
 s=parity_snapshot();auto c=timing_capture_for_comparison(s);
 CHECK(c&&c->memory_mhz==10501&&c->tag=="P0"&&c->top_band&&c->true_mhz&&*c->true_mhz>1312&&c->fields.size()==2&&c->fields[0].cycles==0x6Au&&c->fields[0].bits=="[7:0]");
 s.codename="GENERIC";c=timing_capture_for_comparison(s);CHECK(c&&!c->fields[0].cycles); // unknown layout: raw only
 s=parity_snapshot();s.memory_after_mhz=810;s.memory_clock_stable=false;CHECK(!timing_capture_for_comparison(s));
 s=parity_snapshot();s.ok=false;CHECK(!timing_capture_for_comparison(s));
}
static std::string draw_frame(TimingsViewState& state,const TimingViewData& data,bool classic,TimingsViewOutput* out=nullptr){
 ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1500,1200));ImGui::Begin("classic");ImGui::LogToBuffer();
 auto result=draw_timings(state,&data,30,false,true,true,classic);const std::string log=ImGui::GetCurrentContext()->LogBuffer.c_str();
 ImGui::LogFinish();ImGui::End();ImGui::Render();if(out)*out=result;return log;
}
static ImGuiWindow* child_of(const char* parent,const char* id);
// The logged row of a part; ImGui's log separates rows by a blank line, so
// a run of line breaks (CR LF on Windows) counts as one row break.
static size_t line_of(const std::string& log,const char* part){const auto at=log.find(part);if(at==std::string::npos)return SIZE_MAX;
 size_t rows=0;bool text=false;for(size_t i=0;i<at;++i){if(log[i]=='\n'){if(text)++rows;text=false;}else if(log[i]!='\r')text=true;}return rows;}
// Released 1.9.1 edit block rows: Apply then its modifier slot; the two ways
// back with native Undo; the profile pair; the dim save/load note alone.
static void edit_block_rows(TimingsViewState& state,const TimingViewData& data){
 CHECK(edit_timing_cell(state,data,"RC",0x72));
 const auto log=draw_frame(state,data,true);
 const auto apply=line_of(log,"[ Apply 1 change(s) to the memory controller ]"),back=line_of(log,"Revert edits"),save=line_of(log,"Save timing profile...");
 // Buttons by their logged form: the write warning names 'Restore captured initial' too.
 CHECK(apply!=SIZE_MAX&&line_of(log,"[ Preview all staged ]")==apply&&back==apply+1&&line_of(log,"[ Restore captured initial ]")==back&&line_of(log,"[ Undo timing write ]")==back);
 CHECK(save==back+1&&line_of(log,"Load timing profile...")==save);
 CHECK(line_of(log,"Save includes the captured values and your edits. Load replaces staged edits; review and Apply to write.")==save+1);
 CHECK(!has(log,"Apply all staged")&&!has(log,"Revert staged edits")&&!has(log,"Captured initial means")&&!has(log,"Restore stock")&&!has(log,"force"));
 auto* window=ImGui::FindWindowByName("classic");ImGui::ActivateItemByID(ImHashStr("Revert edits",0,window->ID));TimingsViewOutput o;draw_frame(state,data,true,&o);
 CHECK(state.staged.empty()&&o.commands.empty());
 // tw_profile_status: empty at rest, then the profile result, once, under the note.
 note_timing_profile(state,"Timing profile staged. Preview and Apply remain explicit.",true);
 auto shown=draw_frame(state,data,true);const auto note=line_of(shown,"Save includes the captured values");
 CHECK(line_of(shown,"Timing profile staged.")==note+1&&shown.find("Timing profile staged.")==shown.rfind("Timing profile staged."));
 // Any later message takes the read row's second result line; the profile result stays.
 state.message="Staged locally; preview the whole selection before Apply.";shown=draw_frame(state,data,true);
 CHECK(line_of(shown,"Timing profile staged.")==note+1&&line_of(shown,"Staged locally")>line_of(shown,"[ Re-read timings ]"));
 // Focused shows a profile result where it always did.
 note_timing_profile(state,"profile failed",false);CHECK(has(draw_frame(state,data,false),"profile failed"));
 CHECK(state.message=="profile failed"&&state.profile_status=="profile failed"&&!state.profile_status_ok);
 // tw_plan after a failed read only says nothing is staged.
 auto failed=data;failed.snapshot=TimingSnapshot{};failed.snapshot.message="helper timed out";TimingsViewState none;
 shown=draw_frame(none,failed,true);CHECK(has(shown,"no edits staged")&&!has(shown,"every cell matches"));
}
// Separator() rectangles drawn straight into a window, by top edge. Window and
// child borders share the colour; they lie on those windows' edges.
static std::vector<float> separator_tops(const ImGuiWindow* window){
 const ImU32 colour=ImGui::GetColorU32(ImGuiCol_Separator);std::vector<float> edges{window->Pos.y,window->InnerRect.Min.y,window->InnerRect.Max.y},ys;
 for(const auto* w:ImGui::GetCurrentContext()->Windows)if(w->ParentWindow==window&&w->Active){edges.push_back(w->Pos.y);edges.push_back(w->Pos.y+w->Size.y);}
 for(const auto& v:window->DrawList->VtxBuffer)
  if(v.col==colour&&std::none_of(edges.begin(),edges.end(),[&](float e){return std::fabs(v.pos.y-e)<2.5f;}))ys.push_back(v.pos.y);
 std::sort(ys.begin(),ys.end());std::vector<float> tops;for(const float y:ys)if(tops.empty()||y>tops.back()+1.5f)tops.push_back(y);return tops;
}
// The first glyph or shape the page draws in one of the released palette colours.
static std::optional<ImU32> first_palette_colour(const ImGuiWindow* window,std::initializer_list<ImVec4> palette){
 for(const auto& v:window->DrawList->VtxBuffer)for(const auto& c:palette)if(v.col==ImGui::ColorConvertFloat4ToU32(c))return v.col;
 return std::nullopt;
}
// Released 1.9.1 tim_state: loud only outside the top band; amber when the band is unknown.
static void state_headline(){
 auto s=parity_snapshot();CHECK(timing_state_headline(s).empty());
 s.memory_before_mhz=s.memory_after_mhz=810;s.pstate_before=s.pstate_after=8;
 CHECK(timing_state_headline(s)=="IDLE STATE (NVML 810 (enumerated state 810 + 0 memory offset), P-STATE 8) - says NOTHING about performance. Hold the card in its "
  "top band with Ctrl+H on the V/F curve, or capture under load with 'Read memory timings  (will hold P0)', in P0/P2 at or above nominal 10501 MHz after removing the memory offset.");
 // An offset is taken back off (half the raw request), and a moved clock is said.
 s.memory_before_mhz=1310;s.offset_before=s.offset_after=1000;s.memory_after_mhz=405;s.memory_clock_stable=false;
 CHECK(has(timing_state_headline(s),"IDLE STATE (NVML 1310 (enumerated state 810 + 500 memory offset), P-STATE 8), and it moved to 405 mid-capture - "));
 s.memory_before_mhz=999;CHECK(has(timing_state_headline(s),"IDLE STATE (NVML 999, P-STATE 8), and"));
 s.memory_states.clear();CHECK(has(timing_state_headline(s),"nominal -- MHz")); // no enumerated clocks: no floor to name
 s=parity_snapshot();s.offset_before.reset();CHECK(has(timing_state_headline(s),"MEMORY STATE UNKNOWN: a memory clock, offset"));
 // On the page: above the read-only notice, in Classic only.
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 data.snapshot.memory_before_mhz=data.snapshot.memory_after_mhz=810;data.snapshot.pstate_before=data.snapshot.pstate_after=8;data.snapshot.top_band=false;
 TimingsViewState state;auto log=draw_frame(state,data,true);
 CHECK(has(log,"\u26a0 IDLE STATE (NVML 810")&&log.find("IDLE STATE (NVML")<log.find("read-only subcommands")&&has(log,"IDLE STATE \u2014 NOT PERFORMANCE-RELEVANT"));
 // Released palette: the idle headline is the first thing on the page, in BAD red;
 // an unknown band is WARN amber; an in-band capture starts with the dim notice.
 const ImVec4 warn(1,203/255.f,71/255.f,1),bad(1,92/255.f,92/255.f,1),dim(139/255.f,144/255.f,153/255.f,1),accent(74/255.f,163/255.f,1,1);
 const auto first=[&]{return first_palette_colour(ImGui::FindWindowByName("classic"),{warn,bad,dim,accent});};
 CHECK(first()==ImGui::ColorConvertFloat4ToU32(bad));
 // The decoded panel's IDLE title is BAD red too; an in-band title is ACCENT blue.
 const auto title=[&]{return first_palette_colour(child_of("classic","timing_decoded"),{bad,accent,dim});};
 CHECK(title()==ImGui::ColorConvertFloat4ToU32(bad));
 {TimingsViewState in_band;auto top_band=data;top_band.snapshot=parity_snapshot();draw_frame(in_band,top_band,true);CHECK(title()==ImGui::ColorConvertFloat4ToU32(accent));}
 CHECK(!has(draw_frame(state,data,false),"IDLE STATE (NVML"));
 auto top=data;top.snapshot=parity_snapshot();log=draw_frame(state,top,true);CHECK(!has(log,"IDLE STATE")&&!has(log,"MEMORY STATE UNKNOWN"));
 CHECK(first()==ImGui::ColorConvertFloat4ToU32(dim));
 auto unknown=top;unknown.snapshot.pstate_after.reset();log=draw_frame(state,unknown,true);CHECK(has(log,"\u26a0 MEMORY STATE UNKNOWN"));
 CHECK(first()==ImGui::ColorConvertFloat4ToU32(warn)&&has(log,"DECODED TIMINGS  \u00b7  IDLE STATE")); // released: an unknown band is not titled in-band
 // Never read: the header names that, with no empty adapter fields.
 auto unread=data;unread.snapshot=TimingSnapshot{};unread.message.clear();TimingsViewState fresh;log=draw_frame(fresh,unread,true);
 CHECK(has(log,"no snapshot")&&has(log,"nvtune has not been read yet")&&!has(log,"Adapter")&&!has(log,"slot  ")&&!has(log,"IDLE STATE ("));
 CHECK(has(log,"no edits - every cell matches the register")&&!has(log,"no edits staged"));
 // A failed read names its error in the header, as released 1.9.1 does.
 auto failed=unread;failed.snapshot.message="helper timed out";log=draw_frame(fresh,failed,true);
 CHECK(has(log,"no snapshot")&&has(log,"helper timed out")&&!has(log,"nvtune has not been read yet")&&!has(log,"Adapter"));
}
// Released 1.9.1 add_text items sit on frame-height lines: the read row's two
// empty result lines and the decoded panel's title / subtitle.
static void frame_height_lines(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 data.snapshot.ok=false;
 TimingsViewState state;const auto unread=draw_frame(state,data,true);
 // The unread panel is released 1.9.1 pan_tim as built: the title and an empty
 // tim_sub on two frame-height lines, the separator, then tim_table's header row.
 auto* decoded=child_of("classic","timing_decoded");CHECK(decoded);
 const float frame=ImGui::GetFrameHeight(),spacing=ImGui::GetStyle().ItemSpacing.y;
 const auto panel_rule=separator_tops(decoded);
 CHECK(panel_rule.size()==1&&std::fabs(panel_rule[0]-(decoded->Pos.y+ImGui::GetStyle().WindowPadding.y+2*(frame+spacing)))<0.5f);
 CHECK(line_of(unread,"ns at the capture clock")>line_of(unread,"DECODED TIMINGS  ·  broadcast aperture")&&has(unread,"what it is")&&
  line_of(unread,"what it is")<line_of(unread,"CAPTURES COMPARED")&&!has(unread,"No current timing capture")&&!has(unread,"top clock band"));
 // The read row's separators: spacer, the button row and the two empty result
 // lines on frame-height lines, spacer (Python's 100 px, 615 to 715, not 89).
 // Released 1.9.1's ImGui 1.92.5 separator takes no layout height of its own.
 TimingsViewState rest;auto read=data;read.snapshot=parity_snapshot();draw_frame(rest,read,true);
 const auto tops=separator_tops(ImGui::FindWindowByName("classic"));
 CHECK(tops.size()==2);
 // ImGui truncates the cursor after each item; the test font makes s() fractional.
 const float s=ImGui::GetFontSize()/16.0f,expected=spacing+std::floor(6*s+spacing)+3*(frame+spacing)+std::floor(4*s+spacing);
 CHECK(std::fabs((tops[1]-tops[0])-expected)<0.5f);
 // Then the s(4) spacer and the decoded panel (Python 715 to 727); inside it the
 // table's header row starts one item spacing under the panel's rule (800 to 804).
 decoded=child_of("classic","timing_decoded");CHECK(decoded&&std::fabs(decoded->Pos.y-tops[1]-(spacing+std::floor(4*s+spacing)))<0.5f);
 const auto rule=separator_tops(decoded);auto* table=ImGui::TableFindByID(ImHashStr("timing_fields",0,decoded->ID));
 CHECK(rule.size()==1&&table&&std::fabs(table->OuterRect.Min.y-rule[0]-spacing)<0.5f);
 // Released 1.9.1 tim_hdr_h: the header box is three text lines plus s(28) tall...
 auto* header=child_of("classic","timing_header");CHECK(header&&header->Size.y==std::floor(ImGui::GetTextLineHeight()*3+28*s));
 // ...unless a warning would cut into the raw words line: then it fits its content.
 auto moved=read;moved.snapshot.memory_after_mhz=810;moved.snapshot.memory_clock_stable=false;draw_frame(rest,moved,true);draw_frame(rest,moved,true);
 header=child_of("classic","timing_header");
 CHECK(header&&header->Size.y>std::floor(ImGui::GetTextLineHeight()*3+28*s)&&header->Size.y+0.5f>=header->ContentSize.y+2*ImGui::GetStyle().WindowPadding.y);
}
// Released 1.9.1 after a failed read: tim_title / tim_sub keep the last
// capture's wording and only the rows go; tim_state is emptied, not re-shown.
static void failed_read_keeps_state(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 auto idle=data;idle.capture_revision=1;idle.snapshot.memory_before_mhz=idle.snapshot.memory_after_mhz=810;idle.snapshot.pstate_before=idle.snapshot.pstate_after=8;idle.snapshot.top_band=false;
 // The core keeps the earlier fields and marks the snapshot failed.
 const auto failed_after=[](TimingViewData d){d.capture_revision+=10;d.snapshot.ok=false;d.snapshot.message=d.message="helper timed out";return d;};
 const float line=ImGui::GetFrameHeight()+ImGui::GetStyle().ItemSpacing.y;
 const auto header_top=[]{auto* h=child_of("classic","timing_header");return h?h->Pos.y:-1.0f;};
 // Never read: the empty tim_state line stands (26 px).
 TimingsViewState fresh;auto log=draw_frame(fresh,failed_after(data),true);const float shown=header_top();
 CHECK(has(log,"DECODED TIMINGS  \u00b7  broadcast aperture")&&!has(log,"top clock band")&&has(log,"ns at the capture clock")&&!has(log,"Row cycle time"));
 // An in-band capture hides it; a failed read after it leaves it hidden.
 TimingsViewState in_band;draw_frame(in_band,data,true);log=draw_frame(in_band,failed_after(data),true);
 CHECK(std::fabs(shown-header_top()-line)<0.5f&&in_band.state_line_hidden);
 CHECK(has(log,"DECODED TIMINGS  \u00b7  broadcast aperture  \u00b7  P0, top clock band")&&has(log,"2 fields, bit ranges parsed from `nvtune fields`")&&
  has(log,"All 2 partitions (fbpa0, fbpa1) match")&&has(log,"ns at the capture clock")&&!has(log,"Row cycle time")&&!has(log,"Training [structural]"));
 // An idle capture shows it; a failed read after that keeps the (now empty) line and the IDLE title.
 TimingsViewState below;draw_frame(below,idle,true);log=draw_frame(below,failed_after(idle),true);
 CHECK(std::fabs(shown-header_top())<0.5f&&!below.state_line_hidden&&!has(log,"\u26a0 IDLE STATE (NVML"));
 CHECK(has(log,"DECODED TIMINGS  \u00b7  IDLE STATE \u2014 NOT PERFORMANCE-RELEVANT")&&has(log,"2 fields, bit ranges parsed")&&!has(log,"Row cycle time"));
 // A later in-band read draws its rows and title again.
 auto again=data;again.capture_revision=30;log=draw_frame(below,again,true);
 CHECK(has(log,"P0, top clock band")&&has(log,"Row cycle time")&&below.state_line_hidden);
 // Focused keeps its own page.
 log=draw_frame(below,failed_after(data),false);CHECK(has(log,"No current timing capture. Read timings to retry")&&!has(log,"DECODED TIMINGS"));
}
// Released 1.9.1 tw_button and tw_profile_status (Classic).
static TimingViewData profile_data(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="one";
 auto& s=data.snapshot;s.ok=s.top_band=s.memory_clock_stable=true;s.slot="0000:09:00.0";s.codename="GP102";s.helper_token="one";
 s.memory_before_mhz=s.memory_after_mhz=7000;s.pstate_before=s.pstate_after=0;s.offset_before=s.offset_after=0;
 TimingField faw;faw.name="FAW";faw.reg="CONFIG0";faw.lo=8;faw.hi=15;faw.maximum=255;
 TimingField train;train.name="TRAIN";train.reg="CONFIG0";train.lo=0;train.hi=7;train.maximum=255;train.structural=true;
 TimingField ras;ras.name="RAS";ras.reg="CONFIG0";ras.lo=16;ras.hi=23;ras.maximum=255;
 s.fields={faw,train,ras};s.broadcast={{"CONFIG0","DOCUMENTED",0x1000,0xABCD182D}}; // FAW 24, RAS 205
 data.identity.nvapi_ok=1;std::snprintf(data.identity.slot,sizeof(data.identity.slot),"%s","0000:09:00.0");
 std::snprintf(data.identity.uuid,sizeof(data.identity.uuid),"%s","GPU-timing");
 return data;
}
static void apply_button_and_profile_lines(){
 const auto data=profile_data();
 const auto colour=[](int r,int g,int b,float a){return ImGui::ColorConvertFloat4ToU32(ImVec4(r/255.f,g/255.f,b/255.f,a));};
 const auto drawn=[&](ImU32 c){for(const auto& v:ImGui::FindWindowByName("classic")->DrawList->VtxBuffer)if(v.col==c)return true;return false;};
 const float off=ImGui::GetStyle().DisabledAlpha;
 const auto red=[&]{return drawn(colour(122,30,34,1))||drawn(colour(122,30,34,off));};
 // Neutral with nothing to write.
 TimingsViewState state;auto log=draw_frame(state,data,true);
 CHECK(has(log,"[ Apply to memory controller ]")&&!has(log,"change(s)")&&!red());
 // Red and counting with an edit: the count is what would be written.
 CHECK(edit_timing_cell(state,data,"FAW",25)&&edit_timing_cell(state,data,"RAS",200));
 log=draw_frame(state,data,true);CHECK(has(log,"[ Apply 2 change(s) to the memory controller ]")&&red()&&(drawn(colour(255,235,235,1))||drawn(colour(255,235,235,off))));
 // Previewed: enabled at full colour, and the same ID presses it.
 auto planned=data;TimingBatchPlan batch;batch.ok=true;for(const auto& r:state.staged){TimingPlan f;f.ok=true;f.field=r.field;f.value=r.value;batch.fields.push_back(f);}planned.batch_preview=batch;
 draw_frame(state,planned,true);CHECK(drawn(colour(122,30,34,1)));
 auto* window=ImGui::FindWindowByName("classic");ImGui::ActivateItemByID(ImHashStr("###tw_apply",0,window->ID));TimingsViewOutput o;draw_frame(state,planned,true,&o);
 CHECK(o.commands.size()==1&&o.commands[0].operation==Command::Operation::timings_apply_batch&&o.commands[0].timing_editor->fields.size()==2);
 // Load: released wording, with the file and the capture the profile records.
 const auto document=capture_timing_editor_profile(data.identity,data.snapshot,{{"FAW",25}}); // FAW changed, RAS as captured
 TimingsViewState loaded;sync_timings_view(loaded,&data,30);CHECK(stage_timing_profile(document,data,loaded,true,"C:\\profiles\\timings.json")&&loaded.staged.size()==2);
 CHECK(loaded.profile_status=="Loaded 2 fields; 1 changes staged. Review the preview before Apply. C:\\profiles\\timings.json Captured on GP102 at 7000 MHz "
  "reported memory clock (P0)."&&loaded.profile_status_ok&&loaded.message==loaded.profile_status);
 log=draw_frame(loaded,data,true);CHECK(has(log,"[ Apply 1 change(s) to the memory controller ]")&&has(log,"Loaded 2 fields; 1 changes staged.")&&!has(log,"Timing profile staged."));
 // Python's own profile records its capture as capture.gpu / capture.band.
 ProfileJson python={{"schema","druta.timing-profile"},{"version",1},{"capture",{{"gpu",{{"codename","GP102"}}},{"band",{{"memory_mhz_reported",7000},{"pstate",0}}}}},
  {"fields",{{"FAW",26}}}};
 TimingsViewState legacy;CHECK(stage_timing_profile(python,data,legacy,true,"t.json"));
 CHECK(legacy.profile_status=="Loaded 1 fields; 1 changes staged. Review the preview before Apply. t.json Captured on GP102 at 7000 MHz reported memory clock (P0).");
 // A bare field list records nothing; values equal to the capture are no change, and Apply stays neutral.
 ProfileJson bare={{"fields",{{"FAW",24}}}};TimingsViewState same;sync_timings_view(same,&data,30);CHECK(stage_timing_profile(bare,data,same,true,"b.json")&&same.staged.size()==1);
 CHECK(same.profile_status=="Loaded 1 fields; 0 changes staged. Review the preview before Apply. b.json Source GPU and clock band were not recorded.");
 CHECK(timing_pending_changes(same,data)==0);log=draw_frame(same,data,true);CHECK(has(log,"[ Apply to memory controller ]")&&!red());
 // Focused keeps its sentence; a refused profile is red in both.
 TimingsViewState focused;CHECK(stage_timing_profile(document,data,focused)&&focused.profile_status=="Timing profile staged. Preview and Apply remain explicit.");
 auto stale=data;stale.snapshot.memory_before_mhz=stale.snapshot.memory_after_mhz=6801;
 CHECK(!stage_timing_profile(document,stale,focused,true,"x.json")&&!focused.profile_status_ok&&focused.staged.size()==2);
 // Save: the path once the session's save is done, the error otherwise.
 TimingsViewState saved;sync_timings_view(saved,&data,30);note_timing_profile_saved(saved,true,"C:\\profiles\\timings.json","");
 CHECK(saved.profile_status=="Saved timing profile: C:\\profiles\\timings.json"&&saved.profile_status_ok);
 log=draw_frame(saved,data,true);CHECK(line_of(log,"Saved timing profile: C:\\profiles\\timings.json")==line_of(log,"Save includes the captured values")+1);
 note_timing_profile_saved(saved,false,"C:\\profiles\\timings.json","Timing profile save failed: disk full");
 CHECK(saved.profile_status=="Timing profile save failed: disk full"&&!saved.profile_status_ok);
 // Unknown captured values count as changes (a read that never landed).
 TimingsViewState unknown;unknown.staged={{"GONE",3}};CHECK(timing_pending_changes(unknown,data)==1);
}
static void classic_page(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 TimingsViewState state;sync_timings_view(state,&data,30);
 // Filing: one capture per stable memory clock; a moved clock is not filed.
 CHECK(state.captures.size()==1&&state.captures[0].memory_mhz==10501);
 sync_timings_view(state,&data,30);CHECK(state.captures.size()==1);
 auto idle=data;idle.capture_revision=1;idle.snapshot.memory_before_mhz=idle.snapshot.memory_after_mhz=810;idle.snapshot.pstate_before=idle.snapshot.pstate_after=8;
 idle.snapshot.top_band=false;idle.snapshot.broadcast[0].value=0x2885A40A;
 sync_timings_view(state,&idle,30);CHECK(state.captures.size()==2&&state.captures[1].tag=="idle");
 auto moved=data;moved.capture_revision=2;moved.snapshot.memory_after_mhz=405;moved.snapshot.memory_clock_stable=false;
 sync_timings_view(state,&moved,30);CHECK(state.captures.size()==2);
 auto again=data;again.capture_revision=3;again.snapshot.broadcast[0].value=0x2885A46B;
 sync_timings_view(state,&again,30);CHECK(state.captures.size()==2&&state.captures[0].fields[0].cycles==0x6Bu);
 sync_timings_view(state,&data,30);
 // Inline editor: stages into the existing staged-field model, retires on match.
 CHECK(edit_timing_cell(state,data,"RC",0x70)&&state.staged.size()==1&&state.staged[0].value==0x70);
 CHECK(edit_timing_cell(state,data,"RC",0x71)&&state.staged.size()==1&&state.staged[0].value==0x71);
 CHECK(!edit_timing_cell(state,data,"TRAIN",3)&&state.staged.size()==1);
 CHECK(edit_timing_cell(state,data,"RC",0x6A)&&state.staged.empty());
 CHECK(!edit_timing_cell(state,data,"RC",0x6A));
 auto below=data;below.snapshot.top_band=false;CHECK(!edit_timing_cell(state,below,"RC",0x70)&&state.staged.empty());
 TimingsViewOutput out;auto log=draw_frame(state,data,true,&out);
 CHECK(out.commands.empty()&&!out.save_profile&&!out.load_profile);
 CHECK(has(log,"GA102  \u00b7  PCI 10de:2203")&&has(log,"CONFIG0 0X2885A46A")&&has(log,"1313 MHz true (GDDR6X)"));
 CHECK(has(log,"EDIT TIMINGS  \u00b7  writes to the memory controller")&&has(log,"CAN HANG THE MACHINE AND CORRUPT VRAM")&&has(log,"Restore captured initial"));
 CHECK(has(log,"no edits - every cell matches the register")&&has(log,"only red rows are written"));
 CHECK(has(log,"[7:0]")&&has(log,"106")&&has(log,"Row cycle time")&&has(log,"Training [structural]"));
 CHECK(has(log,"CAPTURES COMPARED  \u00b7  cycle ratio vs clock ratio")&&has(log,"Baseline is the slowest capture"));
 CHECK(has(log,"[810 reported = 101 MHz true, P8 \u2014 idle, read-only evidence, clock \u00d71.00]"));
 CHECK(has(log,"tracks")||has(log,"partial")||has(log,"flat"));
 CHECK(!has(log,"No staged timing fields")&&!has(log,"Field value")&&!has(log,"force"));
 CHECK(edit_timing_cell(state,data,"RC",0x70));log=draw_frame(state,data,true);CHECK(has(log,"1 field(s): RC 106->112"));
 // Writes still need the explicit batch Preview / Apply path.
 const auto press=[&](const char* label){auto* window=ImGui::FindWindowByName("classic");ImGui::ActivateItemByID(ImHashStr(label,0,window->ID));TimingsViewOutput o;draw_frame(state,data,true,&o);return o;};
 auto applied=press("###tw_apply");CHECK(applied.commands.empty());
 auto previewed=press("Preview all staged");
 CHECK(previewed.commands.size()==1&&previewed.commands[0].operation==Command::Operation::timings_preview_batch&&previewed.commands[0].timing_editor->fields.size()==1);
 {auto planned=data;TimingBatchPlan batch;batch.ok=true;TimingPlan f;f.ok=true;f.field="RC";f.value=0x70;batch.fields.push_back(f);planned.batch_preview=batch;
  auto* window=ImGui::FindWindowByName("classic");ImGui::ActivateItemByID(ImHashStr("###tw_apply",0,window->ID));TimingsViewOutput o;draw_frame(state,planned,true,&o);
  CHECK(o.commands.size()==1&&o.commands[0].operation==Command::Operation::timings_apply_batch&&o.commands[0].timing_editor->fields[0].value==0x70);}
 edit_block_rows(state,data);
 // One capture: the hint names this card's own enumerated states.
 TimingsViewState fresh;sync_timings_view(fresh,&data,31);log=draw_frame(fresh,data,true);
 CHECK(has(log,"1 capture. Two are needed")&&has(log,"~810 reported / ~101 MHz true")&&has(log,"~10501 reported / ~1313 MHz true"));
 // Focused keeps its own layout.
 log=draw_frame(fresh,data,false);CHECK(has(log,"Values originate from captured broadcast fields")&&!has(log,"EDIT TIMINGS"));
 // Header warnings carry the released U+26A0 prefix.
 auto moved_now=data;moved_now.snapshot.memory_after_mhz=810;moved_now.snapshot.memory_clock_stable=false;
 log=draw_frame(fresh,moved_now,true);CHECK(has(log,"\u26a0 THE MEMORY CLOCK MOVED DURING CAPTURE")&&!has(log,"! THE MEMORY"));
 // Native single-field Preview / Apply stays reachable in Classic under a collapsed header.
 TimingsViewState single;sync_timings_view(single,&data,30);log=draw_frame(single,data,true);
 CHECK(has(log,"Single-field preview and apply")&&!has(log,"Field value"));
 auto* window=ImGui::FindWindowByName("classic");window->StateStorage.SetInt(ImHashStr("Single-field preview and apply",0,window->ID),1);
 CHECK(select_timing_field(single,data,"RC")&&single.value==0x6A);single.value=0x70;
 const auto press_single=[&](const char* label,const TimingViewData& d){ImGui::ActivateItemByID(ImHashStr(label,0,window->ID));TimingsViewOutput o;draw_frame(single,d,true,&o);return o;};
 log=draw_frame(single,data,true);CHECK(has(log,"Selected: RC")&&has(log,"Field value"));
 auto p=press_single("Preview timing",data);
 CHECK(p.commands.size()==1&&p.commands[0].operation==Command::Operation::timings_preview&&p.commands[0].timing_field=="RC"&&p.commands[0].timing_value==0x70);
 CHECK(press_single("Apply timing",data).commands.empty()); // needs the backend preview
 auto previewed_single=data;TimingPlan plan;plan.ok=true;plan.field="RC";plan.value=0x70;plan.prior=0x6A;previewed_single.preview=plan;
 log=draw_frame(single,previewed_single,true);CHECK(has(log,"Preview RC: 106 -> 112; selected bits only"));
 auto a=press_single("Apply timing",previewed_single);
 CHECK(a.commands.size()==1&&a.commands[0].operation==Command::Operation::timings_apply&&a.commands[0].timing_field=="RC"&&a.commands[0].timing_value==0x70);
 single.value=0x71;CHECK(press_single("Apply timing",previewed_single).commands.empty()); // a changed value cannot replay the preview
 CHECK(press_single("Stage field",data).commands.empty()&&single.staged.size()==1&&single.staged[0].value==0x71);
 // Helper change drops captures taken through the previous helper.
 auto other=data;other.helper_path=L"C:\\other\\nvtune.exe";sync_timings_view(state,&other,30);
 CHECK(state.captures.size()==1); // only the current capture is re-filed
}
static std::string draw_classic(TimingsViewState& state,const TimingViewData* data,TimingsViewOutput* out=nullptr,const TimingLiveClock* live=nullptr,bool unlocked=true){
 ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1500,1200));ImGui::Begin("classic");ImGui::LogToBuffer();
 auto result=draw_timings(state,data,30,false,unlocked,true,true,live);const std::string log=ImGui::GetCurrentContext()->LogBuffer.c_str();
 ImGui::LogFinish();ImGui::End();ImGui::Render();if(out)*out=result;return log;
}
static ImGuiWindow* child_of(const char* parent,const char* id){
 auto* root=ImGui::FindWindowByName(parent);if(!root)return nullptr;
 for(auto* w:ImGui::GetCurrentContext()->Windows)if(w->ParentWindow==root&&w->ChildId==root->GetID(id)&&w->Active)return w;
 return nullptr;
}
// Released 1.9.1 Timings: setup screen, read row, decoded / compared / divergence panels.
static void classic_setup_and_panels(){
 const auto press=[](const char* label,TimingsViewState& s,const TimingViewData* d,bool unlocked=true){
  auto* window=ImGui::FindWindowByName("classic");ImGui::ActivateItemByID(ImHashStr(label,0,window->ID));TimingsViewOutput o;draw_classic(s,d,&o,nullptr,unlocked);return o;};
 // No adapter data, or discovery found no helper: the setup screen replaces the tab.
 TimingsViewState setup;auto log=draw_classic(setup,nullptr);
 CHECK(has(log,"NVTUNE NOT FOUND")&&has(log,"Point Druta at it:")&&has(log,"github.com/sebastianmarrufo/nvtune")&&!has(log,"EDIT TIMINGS")&&!has(log,"DECODED TIMINGS"));
 auto o=press("Locate nvtune.exe...",setup,nullptr);CHECK(o.choose_helper&&o.commands.empty());
 o=press("Enable test signing...",setup,nullptr);CHECK(o.test_signing_setup&&o.commands.empty());
 CHECK(!has(log,"Forget nvtune location"));
 TimingViewData empty;setup.helper_missing=true;setup.helper_registration_present=true;setup.message="The explicit nvtune environment path is unavailable";
 log=draw_classic(setup,&empty);CHECK(has(log,"NVTUNE NOT FOUND")&&has(log,"The explicit nvtune environment path is unavailable")&&has(log,"Forget nvtune location"));
 o=press("Forget nvtune location",setup,&empty);CHECK(o.forget_helper&&o.commands.empty());
 // While discovery is still running the working tab stays, with nothing to read.
 TimingsViewState searching;log=draw_classic(searching,&empty);
 CHECK(!has(log,"NVTUNE NOT FOUND")&&has(log,"locating nvtune")&&has(log,"Read memory timings  (will hold P0)"));
 CHECK(press("Re-read timings",searching,&empty).commands.empty()&&press("Read memory timings  (will hold P0)",searching,&empty).commands.empty());
 // Working tab: released wording and order, no always-on helper block.
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 auto& parts=data.snapshot.diagnostic_evidence.partitions;parts[0].words=data.snapshot.broadcast;parts[1].words=data.snapshot.broadcast;
 TimingsViewState state;TimingLiveClock live;live.memory_mhz=10501;live.pstate=0;live.top_memory_mhz=10501;live.divisor=8;
 log=draw_classic(state,&data,nullptr,&live);
 CHECK(has(log,"card is at 10501 (1313 MHz true) \u00b7 P0 \u00b7 P0 \u00b7 captured")&&has(log,"read-only subcommands"));
 CHECK(has(log,"Auto-capture at P0")&&state.classic_auto_capture&&!has(log,"Memory timings")&&!has(log,"Helper: ")&&!has(log,"Capture at performance band"));
 CHECK(has(log,"nvtune helper and capture options")&&has(log,"Single-field preview and apply"));
 CHECK(log.find("EDIT TIMINGS")<log.find("Read memory timings")&&log.find("Re-read timings")<log.find("DECODED TIMINGS")&&log.find("DECODED TIMINGS")<log.find("CAPTURES COMPARED"));
 CHECK(has(log,"DECODED TIMINGS  \u00b7  broadcast aperture  \u00b7  P0, top clock band")&&has(log,"2 fields, bit ranges parsed from `nvtune fields`")&&has(log,"1 convert to nanoseconds"));
 CHECK(has(log,"All 2 partitions (fbpa0, fbpa1) match the broadcast aperture exactly")&&!has(log,"PARTITION DIVERGENCE")&&!has(log,"partitions differ"));
 CHECK(has(log,"   80.75 ns")&&!has(log,"80.750"));
 // Panels have Python's split: decoded above its s(180) floor and taller than the comparison (s(150) floor).
 auto* decoded=child_of("classic","timing_decoded");auto* compare=child_of("classic","timing_compare");const float px=ImGui::GetFontSize()/16.0f;
 CHECK(decoded&&compare&&decoded->Size.y>=std::floor(180*px)&&compare->Size.y>=std::floor(150*px)&&decoded->Size.y>compare->Size.y&&!child_of("classic","timing_divergence"));
 // Read row: the blue read is the held capture, Re-read is the plain read; neither writes.
 o=press("Read memory timings  (will hold P0)",state,&data);
 CHECK(o.commands.size()==1&&o.commands[0].operation==Command::Operation::timings_capture_p0&&o.commands[0].timing_allow_hold&&o.commands[0].timing_generation==30);
 o=press("Read memory timings  (will hold P0)",state,&data,false);CHECK(o.commands.size()==1&&!o.commands[0].timing_allow_hold);
 o=press("Re-read timings",state,&data);CHECK(o.commands.size()==1&&o.commands[0].operation==Command::Operation::timings_read);
 // A partition that reads back differently gets its own panel, not a line per row.
 auto split=data;split.capture_revision=9;split.snapshot.diagnostic_evidence.partitions[1].words[0].value=0x2885A46B;
 log=draw_classic(state,&split,nullptr,&live);
 CHECK(has(log,"PARTITION DIVERGENCE")&&has(log,"1 field(s) differ from the broadcast aperture")&&!has(log,"match the broadcast aperture exactly")&&!has(log,"partitions differ"));
 CHECK(child_of("classic","timing_divergence")!=nullptr);
 // An idle capture is titled as such, in the released red.
 auto idle=data;idle.capture_revision=10;idle.snapshot.memory_before_mhz=idle.snapshot.memory_after_mhz=810;idle.snapshot.pstate_before=idle.snapshot.pstate_after=8;idle.snapshot.top_band=false;
 log=draw_classic(state,&idle,nullptr,&live);CHECK(has(log,"DECODED TIMINGS  \u00b7  IDLE STATE \u2014 NOT PERFORMANCE-RELEVANT"));
 // A moved clock says the conversions exist but are not shown.
 auto moved=data;moved.capture_revision=11;moved.snapshot.memory_after_mhz=810;moved.snapshot.memory_clock_stable=false;
 log=draw_classic(state,&moved,nullptr,&live);CHECK(has(log,"1 convert to nanoseconds - but NOT shown, because the clock moved")&&has(log,"clock moved \u2014 no ns"));
 // In-flight capture: cancel stays one click away; held resources stay releasable.
 // (The last blue read above was pressed locked, so no hold: released 'inducing GPU load…'.)
 auto running=data;running.capture_running=true;log=draw_classic(state,&running);
 CHECK(has(log,"inducing GPU load")&&press("Cancel timing capture",state,&running).cancel_capture);
 auto held=data;held.capture_hold_pending=true;o=press("Release timing capture resources",state,&held);
 CHECK(o.commands.size()==1&&o.commands[0].operation==Command::Operation::timings_release_capture_hold);
}
// Released 1.9.1 run()/select_gpu() startup read: once per adapter session, read only.
static void startup_read(){
 TimingViewData d;TimingStartupRead s;
 CHECK(!want_timing_startup_read(s,1,false,false,true,nullptr,true)); // no helper data yet
 CHECK(!want_timing_startup_read(s,1,false,false,true,&d,true));      // helper not located: nothing
 d.helper_path=L"C:\\tools\\nvtune.exe";
 CHECK(!want_timing_startup_read(s,1,true,false,true,&d,true));       // busy
 CHECK(!want_timing_startup_read(s,1,false,true,true,&d,true));       // opening
 CHECK(!want_timing_startup_read(s,1,false,false,false,&d,true));     // disconnected
 CHECK(want_timing_startup_read(s,1,false,false,true,&d,true));       // first chance: one read
 CHECK(!want_timing_startup_read(s,1,false,false,true,&d,true));      // never twice per session
 s.done=false;CHECK(want_timing_startup_read(s,1,false,false,true,&d,true)); // a refused submit retries
 CHECK(want_timing_startup_read(s,2,false,false,true,&d,true));       // card switch: one more read
 CHECK(!want_timing_startup_read(s,2,false,false,true,&d,true));
 for(int pending=0;pending<4;++pending){TimingStartupRead p;auto e=d;
  if(pending==0)e.snapshot.ok=true;else if(pending==1)e.capture_running=true;else if(pending==2)e.capture_hold_pending=true;else e.capture_workload_pending=true;
  CHECK(!want_timing_startup_read(p,5,false,false,true,&e,true)&&p.done);} // present or in-flight capture: skipped, not retried
 // Focused (539bb9d) opens its Timings page with no capture: never a startup read,
 // on any adapter, however long the helper has been known.
 TimingStartupRead f;
 for(std::uint64_t g=1;g<4;++g)for(int tick=0;tick<3;++tick)CHECK(!want_timing_startup_read(f,g,false,false,true,&d,false));
}
// Released 1.9.1 tim_auto default and the tim_busy live line.
static void auto_capture_and_status(){
 TimingsViewState v;sync_timings_view(v,nullptr,1);
 CHECK(timing_auto_capture_armed(v,true)&&!timing_auto_capture_armed(v,false)); // Classic armed, Focused off by default
 v.classic_auto_capture=false;v.auto_capture=true;sync_timings_view(v,nullptr,2);
 CHECK(!timing_auto_capture_armed(v,true)&&!timing_auto_capture_armed(v,false)); // the Classic choice survives a card switch; Focused resets
 TimingLiveClock live;std::vector<TimingCapture> caps;
 CHECK(timing_live_status(live,caps,true).empty()); // no memory clock: no line
 live.memory_mhz=10501;live.pstate=0;live.top_memory_mhz=10501;live.divisor=8;
 CHECK(timing_live_at_p0(live)&&timing_live_status(live,caps,true)=="  card is at 10501 (1313 MHz true) \u00b7 P0 \u00b7 P0 \u00b7 not captured");
 TimingCapture c;c.memory_mhz=10501;caps.push_back(c);
 CHECK(timing_live_status(live,caps,true)=="  card is at 10501 (1313 MHz true) \u00b7 P0 \u00b7 P0 \u00b7 captured");
 live.memory_mhz=810;live.pstate=8;
 CHECK(!timing_live_at_p0(live)&&timing_live_status(live,caps,true)=="  card is at 810 (101 MHz true) \u00b7 P8 \u00b7 below P0 \u00b7 not captured \u00b7 waiting for P0 (\u226510501, p-state 0)");
 CHECK(timing_live_status(live,caps,false)=="  card is at 810 (101 MHz true) \u00b7 P8 \u00b7 below P0 \u00b7 not captured");
 // A P2 load above the top enumerated clock is not P0 (released is_p0); unknown divisor and p-state stay unknown.
 live.memory_mhz=10700;live.pstate=2;CHECK(!timing_live_at_p0(live));
 live.pstate.reset();live.divisor.reset();CHECK(timing_live_at_p0(live)&&timing_live_status(live,caps,true)=="  card is at 10700 \u00b7 P? \u00b7 P0 \u00b7 not captured");
 live.top_memory_mhz.reset();CHECK(!timing_live_at_p0(live)&&timing_live_status(live,caps,true)=="  card is at 10700 \u00b7 P? \u00b7 below P0 \u00b7 not captured");
}
// Released 1.9.1 at rest: empty tw_result / tw_backup, the driver-stopped setup
// screen, Field.ns_ok for INFERRED registers and the comparison hint wording.
static void classic_rest_and_setup_variants(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 data.snapshot.message=data.message="Selected-card raw timing registers captured";
 CHECK(classic_timing_result(data).empty());
 TimingsViewState state;auto log=draw_classic(state,&data);
 CHECK(!has(log,"Selected-card raw timing registers captured")&&!has(log,"nvtune found"));
 auto written=data;written.message="Selected timing values and original geometry verified";
 CHECK(classic_timing_result(written)==written.message&&has(draw_classic(state,&written),"original geometry verified"));
 // Released wording of the one-capture hint.
 CHECK(has(log,"and press Capture, then load it to the top state")&&has(log,"and press Capture again."));
 // nvtune.exe present but its driver service known stopped: the driver variant replaces the tab.
 auto unread=data;unread.snapshot.ok=false;unread.message.clear();
 TimingsViewState stopped;stopped.helper_driver_running=false;stopped.helper_driver_state="STOPPED";
 log=draw_classic(stopped,&unread);
 CHECK(has(log,"NVTUNE'S DRIVER IS NOT RUNNING")&&has(log,"Let Windows load its driver:")&&has(log,"nvtune.exe was found, but its kernel driver is not running"));
 CHECK(has(log,"nvtune.exe is present (C:\\tools\\nvtune.exe)")&&has(log,"(state: STOPPED)")&&has(log,"sc start nvtunedrv"));
 CHECK(has(log,"Locate nvtune.exe...")&&has(log,"Enable test signing...")&&!has(log,"NVTUNE NOT FOUND")&&!has(log,"EDIT TIMINGS"));
 // A successful read proves the driver answers; an unknown state never hides the tab.
 log=draw_classic(stopped,&data);CHECK(!has(log,"NOT RUNNING")&&has(log,"EDIT TIMINGS"));
 TimingsViewState unknown;unknown.helper_driver_state="cannot query (err 5)";log=draw_classic(unknown,&unread);CHECK(!has(log,"NOT RUNNING")&&has(log,"EDIT TIMINGS"));
 // No nvtune.exe either: the released missing-exe screen names the driver too.
 TimingViewData none;TimingsViewState missing;sync_timings_view(missing,&none,30);missing.helper_missing=true;missing.helper_driver_running=false;missing.helper_driver_state="not installed";
 log=draw_classic(missing,&none);
 CHECK(has(log,"NVTUNE NOT FOUND")&&has(log,"Point Druta at it:")&&has(log,"also not running (state: not installed)."));
 // Released 1.9.1 keeps tim_busy (the live clock) and the empty tim_state line
 // above the setup screen; its texts sit on frame-height lines.
 TimingLiveClock live;live.memory_mhz=810;live.pstate=8;live.top_memory_mhz=10501;
 log=draw_classic(missing,&none,nullptr,&live);CHECK(has(log,"card is at 810")&&line_of(log,"card is at 810")<line_of(log,"NVTUNE NOT FOUND"));
 {const float line=ImGui::GetFrameHeight()+ImGui::GetStyle().ItemSpacing.y,s=ImGui::GetFontSize()/16.0f;
  auto* window=ImGui::FindWindowByName("classic");
  // The red heading starts below those two lines and the s(20) spacer.
  const auto red=ImGui::ColorConvertFloat4ToU32(ImVec4(1,92/255.f,92/255.f,1));float y=FLT_MAX;
  for(const auto& v:window->DrawList->VtxBuffer)if(v.col==red)y=std::min(y,v.pos.y);
  CHECK(y!=FLT_MAX&&y>=window->DC.CursorStartPos.y+2*line+std::floor(20*s));}
 // An INFERRED register still converts in Classic (released Field.ns_ok); Focused keeps its rule.
 auto inferred=data;TimingField rfc;rfc.name="RFCX";rfc.reg="CONFIG1";rfc.lo=0;rfc.hi=9;rfc.maximum=1023;rfc.inferred=true;rfc.description="Same-bank refresh";
 inferred.snapshot.fields.push_back(rfc);inferred.snapshot.diagnostic_evidence.units.push_back({"RFCX","CONFIG1","GA102",0,9,true,true,"test units"});
 const auto released=timing_diagnostics(inferred.snapshot,inferred.snapshot.diagnostic_evidence,true);
 const auto native=timing_diagnostics(inferred.snapshot,inferred.snapshot.diagnostic_evidence);
 CHECK(released.rows[2].nanoseconds&&!native.rows[2].nanoseconds&&native.rows[2].unit=="raw");
 // A register only `nvtune save` reports (TIMING22) decodes from the agreeing save words in Classic.
 auto saved=inferred;TimingField sb;sb.name="RFCSBA";sb.reg="TIMING22";sb.lo=0;sb.hi=9;sb.maximum=1023;sb.inferred=true;sb.description="Per-bank refresh";
 saved.snapshot.fields.push_back(sb);saved.snapshot.diagnostic_evidence.units.push_back({"RFCSBA","TIMING22","GA102",0,9,true,true,"test units"});
 saved.snapshot.saved_broadcast=saved.snapshot.broadcast;saved.snapshot.saved_broadcast.push_back({"TIMING22","",0,0x12400389});
 const auto from_save=timing_diagnostics(saved.snapshot,saved.snapshot.diagnostic_evidence,true);
 CHECK(from_save.rows[3].value==0x389u&&from_save.rows[3].nanoseconds&&!timing_diagnostics(saved.snapshot,saved.snapshot.diagnostic_evidence).rows[3].value);
 const auto filed=timing_capture_for_comparison(saved.snapshot);CHECK(filed&&filed->fields[3].cycles==0x389u);
 // A dump word always wins over the save word of the same register.
 auto conflict=saved;conflict.snapshot.saved_broadcast[0].value^=1;
 CHECK(timing_diagnostics(conflict.snapshot,conflict.snapshot.diagnostic_evidence,true).rows[0].value==released.rows[0].value);
 TimingsViewState conv;log=draw_classic(conv,&inferred);CHECK(has(log,"2 convert to nanoseconds"));
 log=draw_frame(conv,inferred,false);CHECK(has(log,"Same-bank refresh")&&has(log,"930 raw")&&!has(log,"930 cycles"));
}
// Second Classic parity audit of the Timings page.
static size_t vertices_in(const ImGuiWindow* window,const ImVec4& colour){
 const auto c=ImGui::ColorConvertFloat4ToU32(colour);size_t n=0;if(window)for(const auto& v:window->DrawList->VtxBuffer)n+=v.col==c;return n;
}
static void classic_audit(){
 const ImVec4 good(70/255.f,209/255.f,122/255.f,1),bad(1,92/255.f,92/255.f,1),dim(139/255.f,144/255.f,153/255.f,1),accent(74/255.f,163/255.f,1,1);
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 TimingLiveClock live;live.memory_mhz=10501;live.pstate=0;live.top_memory_mhz=10501;live.divisor=8;
 TimingsViewState state;auto log=draw_classic(state,&data,nullptr,&live);
 // Tables use Dear PyGui 2.3.1's colours: header (48,48,51), not the 1.93 default.
 auto* decoded=child_of("classic","timing_decoded");
 CHECK(vertices_in(decoded,ImVec4(48/255.f,48/255.f,51/255.f,1))>0&&vertices_in(decoded,ImGui::GetStyle().Colors[ImGuiCol_TableHeaderBg])==0);
 CHECK(vertices_in(decoded,ImVec4(59/255.f,59/255.f,64/255.f,1))>0);
 // Every row is a frame-height line: RC (with its input) and the text-only TRAIN row alike.
 auto* table=ImGui::TableFindByID(ImHashStr("timing_fields",0,decoded->ID));CHECK(table);
 const float pad=ImGui::GetStyle().CellPadding.y,header=ImGui::GetTextLineHeight()+2*pad,row=ImGui::GetFrameHeight()+2*pad;
 CHECK(std::fabs(table->OuterRect.GetHeight()-(header+2*row))<1.5f);
 // The ns column (released tim_ns_cell): a structural row says it does not convert.
 CHECK(has(log,"   80.75 ns")&&has(log,"encodes differently \u2014 not ns"));
 // Released ns_trustworthy: without a true clock no row prints ns or a refusal.
 auto no_div=data;no_div.capture_revision=5;no_div.snapshot.diagnostic_evidence.memory_clock_divisor.reset();
 log=draw_classic(state,&no_div,nullptr,&live);CHECK(has(log,"clock moved \u2014 no ns")&&!has(log,"encodes differently")&&!has(log,"80.75 ns"));
 // Busy line: a plain read says '  reading…' in the colour the live line last had (GOOD at P0).
 TimingsViewState busy;draw_classic(busy,&data,nullptr,&live);CHECK(busy.live_at_p0&&*busy.live_at_p0);
 busy.read_in_flight=true;log=draw_classic(busy,&data,nullptr,&live);
 CHECK(has(log,"  reading\u2026")&&!has(log,"card is at")&&first_palette_colour(ImGui::FindWindowByName("classic"),{good,dim,accent})==ImGui::ColorConvertFloat4ToU32(good));
 busy.read_in_flight=false;TimingsViewState never;sync_timings_view(never,&data,30);never.read_in_flight=true;draw_classic(never,&data,nullptr,&live);
 CHECK(first_palette_colour(ImGui::FindWindowByName("classic"),{good,dim,accent})==ImGui::ColorConvertFloat4ToU32(accent)); // before any live line: ACCENT
 // The blue read: '  reading at P0…' with a hold, '  inducing GPU load…' without one.
 auto running=data;running.capture_running=true;
 auto* window=ImGui::FindWindowByName("classic");ImGui::ActivateItemByID(ImHashStr("Read memory timings  (will hold P0)",0,window->ID));
 TimingsViewOutput o;draw_classic(busy,&data,&o,&live,false);CHECK(o.commands.size()==1&&!busy.capture_hold_requested);
 log=draw_classic(busy,&running,nullptr,&live);CHECK(has(log,"  inducing GPU load\u2026")&&!has(log,"reading at P0"));
 busy.capture_hold_requested=true;log=draw_classic(busy,&running,nullptr,&live);CHECK(has(log,"  reading at P0\u2026"));
 // Apply is never disabled at rest: a click writes nothing, and when locked says so in the log.
 TimingsViewState rest;draw_classic(rest,&data,nullptr,&live);
 const auto press=[&](const char* label,TimingsViewState& s,const TimingViewData& d,bool unlocked){auto* w=ImGui::FindWindowByName("classic");
  ImGui::ActivateItemByID(ImHashStr(label,0,w->ID));TimingsViewOutput out;draw_classic(s,&d,&out,&live,unlocked);return out;};
 o=press("###tw_apply",rest,data,true);CHECK(o.commands.empty()&&o.classic_log.empty());
 o=press("###tw_apply",rest,data,false);CHECK(o.commands.empty()&&o.classic_log.size()==1&&o.classic_log[0].first=="locked - tick 'Unlock controls' first"&&o.classic_log[0].second==false);
 {// Restore captured initial: one timings_restore_initial command while unlocked with a captured
  // initial, in Classic and Focused alike; nothing while locked, busy or without a captured initial.
  const auto press_restore=[&](TimingsViewState& s,const TimingViewData& d,bool classic,bool busy,bool unlocked){auto* w=ImGui::FindWindowByName("classic");
   ImGui::ActivateItemByID(ImHashStr("Restore captured initial",0,w->ID));ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1500,1200));ImGui::Begin("classic");
   auto out=draw_timings(s,&d,30,busy,unlocked,true,classic,classic?&live:nullptr);ImGui::End();ImGui::Render();return out.commands;};
  auto restorable=data;restorable.initial_available=true;
  for(bool classic:{true,false}){TimingsViewState s;
   const auto sent=press_restore(s,restorable,classic,false,true);
   CHECK(sent.size()==1&&sent[0].operation==Command::Operation::timings_restore_initial);
   CHECK(press_restore(s,restorable,classic,false,false).empty()&&press_restore(s,restorable,classic,true,true).empty()&&press_restore(s,data,classic,false,true).empty());}
 }
 // Staged but not previewed: still this build's explicit-preview gate.
 CHECK(edit_timing_cell(rest,data,"RC",0x70));o=press("###tw_apply",rest,data,true);CHECK(o.commands.empty()&&o.classic_log.empty());
 // Released tw_plan: one line, '1 field(s): RC 106->112', neutral until previewed.
 auto plan=classic_timing_plan(rest,data);CHECK(plan.text=="1 field(s): RC 106->112"&&plan.tone==TimingTone::text);
 // Native Remove stays reachable under a node that starts closed (a log expands every node).
 log=draw_classic(rest,&data,nullptr,&live);CHECK(has(log,"1 field(s): RC 106->112")&&has(log,"Staged fields")&&has(log,"Remove"));
 CHECK(ImGui::FindWindowByName("classic")->StateStorage.GetInt(ImHashStr("###tw_staged",0,ImGui::FindWindowByName("classic")->ID),0)==0);
 o=press("Preview all staged",rest,data,true);CHECK(o.commands.size()==1&&rest.previewed.size()==1);
 auto previewed=data;TimingBatchPlan batch;batch.ok=true;batch.message="Whole selected-field timing preview ready";
 TimingPlan p;p.ok=true;p.field="RC";p.reg="CONFIG0";p.prior=0x6A;p.value=0x70;p.prior_word=0x2885A46A;p.target_word=0x2885A470;batch.fields.push_back(p);
 previewed.batch_preview=batch;previewed.message=batch.message;previewed.message_ok=true;previewed.message_source=TimingViewData::Source::batch_preview;
 plan=classic_timing_plan(rest,previewed);CHECK(plan.text=="1 field(s): RC 106->112   [CONFIG0 0x2885A46A->0x2885A470]"&&plan.tone==TimingTone::good);
 log=draw_classic(rest,&previewed,nullptr,&live);CHECK(has(log,"[CONFIG0 0x2885A46A->0x2885A470]")&&!has(log,"Whole selected-field timing preview ready"));
 auto refused=previewed;refused.batch_preview->ok=false;refused.batch_preview->fields.clear();refused.batch_preview->message="RC: Timing helper refused the field";
 plan=classic_timing_plan(rest,refused);CHECK(plan.text=="REFUSED:\n  - RC: Timing helper refused the field\n\n1 field(s): RC 106->112"&&plan.tone==TimingTone::bad);
 CHECK(edit_timing_cell(rest,data,"RC",0x71));plan=classic_timing_plan(rest,refused);CHECK(plan.text=="1 field(s): RC 106->113"&&plan.tone==TimingTone::text); // not these edits
 auto unchanged=previewed;unchanged.batch_preview->fields[0].value=0x71;unchanged.batch_preview->fields[0].prior=0x71;
 plan=classic_timing_plan(rest,unchanged);CHECK(plan.text=="nothing to write - every field already holds that value"&&plan.tone==TimingTone::good);
 // tw_result follows the outcome: GOOD for a write that landed, BAD for one that did not.
 auto written=data;written.message="Selected timing values and original geometry verified";written.message_ok=true;
 TimingsViewState plain;draw_classic(plain,&written,nullptr,&live);const auto good_ok=vertices_in(ImGui::FindWindowByName("classic"),good),bad_ok=vertices_in(ImGui::FindWindowByName("classic"),bad);
 written.message_ok=false;draw_classic(plain,&written,nullptr,&live);
 CHECK(vertices_in(ImGui::FindWindowByName("classic"),good)<good_ok&&vertices_in(ImGui::FindWindowByName("classic"),bad)>bad_ok);
 CHECK(classic_timing_result_tone(written)==TimingTone::bad&&classic_timing_result(written)==written.message);
 // The blue read's note: amber, under tim_state and above the read-only notice; not tw_result.
 auto noted=data;noted.message=noted.capture_note="Timings captured in stable P0; temporary hold released";noted.message_ok=true;noted.message_source=TimingViewData::Source::capture_p0;
 CHECK(classic_timing_result(noted).empty());log=draw_classic(plain,&noted,nullptr,&live);
 CHECK(line_of(log,"Timings captured in stable P0")<line_of(log,"read-only subcommands")&&log.find("Timings captured in stable P0")==log.rfind("Timings captured in stable P0"));
 // Auto-capture toggles and the automatic capture leave released 1.9.1's log lines.
 o=press("Auto-capture at P0",plain,data,true);CHECK(!plain.classic_auto_capture&&o.classic_log.size()==1&&o.classic_log[0].first=="timings: auto-capture at P0 off"&&!o.classic_log[0].second);
 o=press("Auto-capture at P0",plain,data,true);CHECK(plain.classic_auto_capture&&o.classic_log.size()==1&&o.classic_log[0].first=="timings: auto-capture at P0 armed");
 CHECK(timing_auto_capture_receipt(10501,10501)=="timings: memory reached 10501 (P0, top enumerated 10501) - auto-capturing");
 CHECK(timing_auto_capture_receipt(7001,std::nullopt)=="timings: memory reached 7001 (P0, top enumerated ?) - auto-capturing");
 // Focused is unchanged: its result line and per-field staged list.
 log=draw_frame(rest,written,false);CHECK(has(log,"original geometry verified")&&has(log,"RC: 106 -> 113"));
}
// Closing audit: header lines, relayout height, tooltips, profile picker, Revert, other adapters.
static const ImVec4 kAccentT(74/255.f,163/255.f,1,1),kWarnT(1,203/255.f,71/255.f,1);
// The box around every vertex a window drew in one colour (a button frame, a text run).
static std::optional<ImRect> drawn_rect(const ImGuiWindow* window,const ImVec4& colour){
 const auto c=ImGui::ColorConvertFloat4ToU32(colour);std::optional<ImRect> r;
 if(window)for(const auto& v:window->DrawList->VtxBuffer)if(v.col==c){if(r)r->Add(v.pos);else r=ImRect(v.pos,v.pos);}
 return r;
}
// Rests the mouse on a point for a few frames: the last log, and the tooltip's content size if one shows.
struct Hovered { std::string log; std::optional<ImVec2> tooltip; };
static Hovered hover(TimingsViewState& state,const TimingViewData& data,ImVec2 at,const TimingLiveClock* live){
 auto& io=ImGui::GetIO();io.MousePos=at;Hovered h;for(int i=0;i<3;++i)h.log=draw_classic(state,&data,nullptr,live);
 if(auto* t=ImGui::FindWindowByName("##Tooltip_00");t&&t->Active)h.tooltip=t->ContentSize;
 io.MousePos=ImVec2(-FLT_MAX,-FLT_MAX);draw_classic(state,&data,nullptr,live);return h;
}
// Released 1.9.1 pan_tim_hdr: three mono add_text items, each a frame-height row of
// the mono font (14 + 6 at 1x) at the stock spacing, so 24 px apart (ink 258, 282, 307).
static void header_mono_lines(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 TimingsViewState state;draw_classic(state,&data);draw_classic(state,&data);
 const auto* header=child_of("classic","timing_header");CHECK(header);
 const auto& style=ImGui::GetStyle();
 ImGui::NewFrame();ImGui::Begin("probe");push_classic_mono();const float mono=ImGui::GetFrameHeight();ImGui::PopFont();ImGui::End();ImGui::Render();
 CHECK(std::fabs((header->DC.CursorMaxPos.y-header->DC.CursorStartPos.y)-(3*mono+2*style.ItemSpacing.y))<0.5f);
 // The text sits FramePadding.y into its row, so the first glyphs are at least that far down.
 const auto ident=drawn_rect(header,kAccentT);CHECK(ident&&ident->Min.y>=header->DC.CursorStartPos.y+style.FramePadding.y-0.01f);
 // A warning is one body-font item between the clock and the raw words.
 auto moved=data;moved.snapshot.memory_after_mhz=810;moved.snapshot.memory_clock_stable=false;
 const auto log=draw_classic(state,&moved);CHECK(line_of(log,"\u26a0 THE MEMORY CLOCK MOVED")>line_of(log,"sampled at memory clock")&&
  line_of(log,"CONFIG0 0X2885A46A")>line_of(log,"\u26a0 THE MEMORY CLOCK MOVED"));
}
// Released 1.9.1 relayout(): H = client height - menu_h() - s(28) selector - the s(28) gate bar.
static void share_height(){
 auto& io=ImGui::GetIO();const auto display=io.DisplaySize;io.DisplaySize=ImVec2(1528,1261);
 ImGui::NewFrame();ImGui::Begin("probe");
 ImGui::PushFont(nullptr,16.0f);const float at_1x=classic_timing_share_height();ImGui::PopFont();
 ImGui::PushFont(nullptr,24.0f);const float at_150=classic_timing_share_height();ImGui::PopFont();
 ImGui::End();ImGui::Render();io.DisplaySize=display;
 // 1181 is the recorded 1.9.1 tree's H at 1528x1261 (pan_tim 464, pan_cmp 335); every part scales with s().
 CHECK(std::fabs(at_1x-1181)<0.01f&&std::fabs(at_150-(1261-120))<0.01f);
}
// Released 1.9.1 read-row tooltips: unwrapped add_text items with hard line breaks.
// INFERRED field names: the helper's register note after the colon, wrapped at s(520).
static void tooltips(){
 auto& io=ImGui::GetIO();const auto display=io.DisplaySize;io.DisplaySize=ImVec2(1600,1300);
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 TimingField rfc;rfc.name="RFCX";rfc.reg="CONFIG1";rfc.lo=0;rfc.hi=9;rfc.maximum=1023;rfc.inferred=true;rfc.description="Same-bank refresh";
 rfc.note="INFERRED: Offset inferred from CONFIG0 + 4";data.snapshot.fields.push_back(rfc);
 TimingLiveClock live;live.memory_mhz=10501;live.pstate=0;live.top_memory_mhz=10501;live.divisor=8;
 TimingsViewState state;auto log=draw_classic(state,&data,nullptr,&live);
 // The native telemetry sentence left the Auto-capture tooltip for the (collapsed) helper options.
 CHECK(!has(log,"Auto-capture at P0 uses ordinary telemetry only"));
 {auto* window=ImGui::FindWindowByName("classic");const auto id=ImHashStr("nvtune helper and capture options",0,window->ID);window->StateStorage.SetInt(id,1);
  log=draw_classic(state,&data,nullptr,&live);window->StateStorage.SetInt(id,0);}
 CHECK(has(log,"Auto-capture at P0 uses ordinary telemetry only; Pause and minimized refresh settings still apply."));
 log=draw_classic(state,&data,nullptr,&live);
 const float line=ImGui::GetTextLineHeight(),s=ImGui::GetFontSize()/16.0f,gap=2*ImGui::GetStyle().ItemSpacing.x+std::floor(10*s);
 const auto blue=drawn_rect(ImGui::FindWindowByName("classic"),ImVec4(26/255.f,84/255.f,152/255.f,1));CHECK(blue);
 auto h=hover(state,data,blue->GetCenter(),&live);
 CHECK(h.tooltip&&std::fabs(h.tooltip->y-10*line)<0.5f);
 CHECK(line_of(h.log,"timing registers there - the only state whose timings")==line_of(h.log,"Puts the card in its top memory band and reads the")+1);
 CHECK(line_of(h.log,"a temporary P0 hold or the optional CUDA workload,")==line_of(h.log,"With controls unlocked it uses current P0 first, then")+1);
 h=hover(state,data,ImVec2(blue->Max.x+gap+75*s,blue->GetCenter().y),&live);
 CHECK(h.tooltip&&std::fabs(h.tooltip->y-15*line)<0.5f&&!has(h.log,"read held at P0"));
 CHECK(line_of(h.log,"decode: read at P0, let the card idle, re-read. The")==line_of(h.log,"It is also how you capture a SECOND state to prove the")+1);
 h=hover(state,data,ImVec2(blue->Max.x+2*gap+150*s+line/2,blue->GetCenter().y),&live);
 CHECK(h.tooltip&&std::fabs(h.tooltip->y-5*line)<0.5f&&line_of(h.log,"memory state, and again on each re-entry. Start a")==line_of(h.log,"Captures the first time the card reaches its top")+1);
 CHECK(has(h.log,"It reads. It never writes.")&&!has(h.log,"It never writes. Uses"));
 // The INFERRED name: its (amber) text carries the helper's note on the next line.
 const auto name=drawn_rect(child_of("classic","timing_decoded"),kWarnT);CHECK(name);
 h=hover(state,data,name->GetCenter(),&live);
 CHECK(h.tooltip&&line_of(h.log,"INFERRED: Offset inferred from CONFIG0 + 4")==line_of(h.log,"register offset is INFERRED, not established:")+1);
 io.DisplaySize=display;
}
// Released 1.9.1 open_timing_profile: never-disabled buttons, refusals in
// tw_profile_status, then a picker in the profile library's timings folder.
static void profile_picker(){
 const auto data=profile_data();TimingsViewState state;sync_timings_view(state,&data,30);
 CHECK(classic_timing_profile_refusal(state,&data,true).empty()&&classic_timing_profile_refusal(state,&data,false).empty());
 auto running=data;running.capture_running=true;
 CHECK(classic_timing_profile_refusal(state,&running,true)=="Wait for the timing capture to finish."&&classic_timing_profile_refusal(state,&running,false)=="Wait for the timing capture to finish.");
 state.read_in_flight=true;CHECK(classic_timing_profile_refusal(state,&data,false)=="Wait for the timing capture to finish.");state.read_in_flight=false;
 auto unread=data;unread.snapshot.ok=false;
 CHECK(classic_timing_profile_refusal(state,&unread,true)=="Read memory timings before saving or loading a profile."&&
  classic_timing_profile_refusal(state,nullptr,false)=="Read memory timings before saving or loading a profile.");
 // Save also refuses what make_profile refuses; Load does not.
 auto idle=data;idle.snapshot.top_band=false;
 CHECK(classic_timing_profile_refusal(state,&idle,true)=="capture is not in the top memory band; capture performance timings before saving"&&classic_timing_profile_refusal(state,&idle,false).empty());
 auto raw=data;raw.snapshot.codename="GENERIC";
 CHECK(classic_timing_profile_refusal(state,&raw,true)=="raw register snapshots have no confirmed timing layout; capture decoded timings first"&&classic_timing_profile_refusal(state,&raw,false).empty());
 // Classic never disables the pair: a press before a read or while busy reaches the host. Focused keeps its gate.
 const auto press=[&](const char* label,const TimingViewData& d,bool busy,bool classic){draw_frame(state,d,classic);auto* w=ImGui::FindWindowByName("classic");
  ImGui::ActivateItemByID(ImHashStr(label,0,w->ID));ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1500,1200));ImGui::Begin("classic");
  auto out=draw_timings(state,&d,30,busy,true,true,classic);ImGui::End();ImGui::Render();return out;};
 CHECK(press("Save timing profile...",unread,false,true).save_profile&&press("Load timing profile...",unread,true,true).load_profile);
 CHECK(press("Save timing profile...",data,true,true).save_profile&&press("Save timing profile...",data,false,true).commands.empty());
 CHECK(!press("Save timing profile...",unread,false,false).save_profile&&!press("Load timing profile...",data,true,false).load_profile);
 // The picker: the library's timings folder, created; timings.json for Save only.
 wchar_t tmp[MAX_PATH]{};CHECK(GetTempPathW(MAX_PATH,tmp));
 const auto root=std::filesystem::path(tmp)/(L"druta-timing-picker-"+std::to_wstring(GetCurrentProcessId()));std::filesystem::remove_all(root);
 const auto save=classic_timing_profile_dialog(true,(root/L"library").wstring());
 CHECK(save.title==L"Save timing profile"&&save.file==L"timings.json"&&std::filesystem::path(save.folder)==root/L"library"/L"timings"&&std::filesystem::is_directory(save.folder));
 const auto load=classic_timing_profile_dialog(false,(root/L"library").wstring());CHECK(load.title==L"Load timing profile"&&load.file.empty()&&load.folder==save.folder);
 // A library path that is a file cannot hold the folder: the host names that failure.
 {std::FILE* f=nullptr;CHECK(!_wfopen_s(&f,(root/L"blocked").c_str(),L"wb")&&f);std::fclose(f);}
 bool refused=false;try{classic_timing_profile_dialog(true,(root/L"blocked").wstring());}catch(const std::exception&){refused=true;}CHECK(refused);
 std::filesystem::remove_all(root);
}
// Released 1.9.1 tw_clear: Revert edits empties tw_result as well as the cells.
static void revert_clears_result(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=parity_snapshot();
 data.message="Selected timing values and original geometry verified";data.message_ok=true;data.capture_revision=4;
 TimingsViewState state;sync_timings_view(state,&data,30);
 CHECK(has(draw_classic(state,&data),"original geometry verified")&&edit_timing_cell(state,data,"RC",0x70));
 const auto revert=[&](const TimingViewData& d){auto* w=ImGui::FindWindowByName("classic");ImGui::ActivateItemByID(ImHashStr("Revert edits",0,w->ID));
  TimingsViewOutput o;draw_classic(state,&d,&o);CHECK(o.commands.empty());return draw_classic(state,&d);};
 auto log=revert(data);
 CHECK(state.staged.empty()&&!has(log,"original geometry verified")&&classic_timing_result(state,data).empty()&&classic_timing_result(data)==data.message);
 // Frames and other pages' actions leave it cleared; the next timing action shows its result, even the same sentence.
 CHECK(!has(draw_classic(state,&data),"original geometry verified"));
 ++state.timing_actions;CHECK(has(draw_classic(state,&data),"original geometry verified"));
 // A result the session publishes without a timing action (a recapturing Undo) shows too.
 revert(data);auto undone=data;undone.capture_revision=5;undone.message="Timing write undone";
 CHECK(!has(draw_classic(state,&data),"original geometry verified")&&has(draw_classic(state,&undone),"Timing write undone"));
 // A card switch forgets it.
 revert(undone);sync_timings_view(state,&undone,31);CHECK(!state.result_cleared&&classic_timing_result(state,undone)=="Timing write undone");
}
// The Classic page on adapters other than the developer's card: a GDDR6 board
// with a divisor of 4 and its own enumerated states, and an unconfirmed layout.
static TimingSnapshot gddr6_snapshot(){
 TimingSnapshot s;s.ok=true;s.slot="0000:41:00.0";s.codename="GA104";s.pci_id="10de:2484";s.aperture="0x9A0000";s.boot0="0xB74000A1";
 s.memory_before_mhz=s.memory_after_mhz=7001;s.memory_clock_stable=true;s.pstate_before=s.pstate_after=0;s.offset_before=s.offset_after=0;
 s.memory_states={405,7001};s.memory_type="GDDR6";s.top_band=true;
 s.diagnostic_evidence.memory_clock_divisor=4;s.diagnostic_evidence.divisor_provenance="adapter";
 s.diagnostic_evidence.partitions={{"fbpa0",{}},{"fbpa1",{}},{"fbpa2",{}}};
 TimingField rc;rc.name="RC";rc.reg="CONFIG0";rc.lo=0;rc.hi=7;rc.maximum=255;rc.description="Row cycle time";
 TimingField train;train.name="TRAIN";train.reg="CONFIG0";train.lo=8;train.hi=15;train.maximum=255;train.structural=true;train.description="Training [structural]";
 s.fields={rc,train};s.broadcast={{"CONFIG0","DOCUMENTED",0x1000,0x2885A46A}};
 s.diagnostic_evidence.units.push_back({"RC","CONFIG0","GA104",0,7,true,true,"test units"});
 return s;
}
static void other_adapters(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.helper_token="t";data.snapshot=gddr6_snapshot();
 TimingLiveClock live;live.memory_mhz=7001;live.pstate=0;live.top_memory_mhz=7001;live.divisor=4;
 TimingsViewState state;auto log=draw_classic(state,&data,nullptr,&live);
 CHECK(has(log,"GA104  \u00b7  PCI 10de:2484  \u00b7  slot 0000:41:00.0")&&has(log,"sampled at memory clock 7001 reported  =  1750 MHz true (GDDR6)")&&has(log,"3 partitions"));
 CHECK(has(log,"card is at 7001 (1750 MHz true) \u00b7 P0 \u00b7 P0 \u00b7 captured")&&has(log,"P0, top clock band"));
 // RC's 106 cycles against 7001 / 4 MHz: 60.56 ns, not a figure from another card's divisor.
 CHECK(has(log,"   60.56 ns")&&!has(log,"121.13")&&!has(log,"30.28")&&has(log,"1 convert to nanoseconds"));
 CHECK(has(log,"~405 reported / ~101 MHz true")&&has(log,"~7001 reported / ~1750 MHz true")&&has(log,"All 3 partitions (fbpa0, fbpa1, fbpa2)"));
 // A second capture at this card's idle state: the legend and ratios use its own clocks.
 auto idle=data;idle.capture_revision=1;idle.snapshot.memory_before_mhz=idle.snapshot.memory_after_mhz=405;
 idle.snapshot.pstate_before=idle.snapshot.pstate_after=8;idle.snapshot.top_band=false;idle.snapshot.broadcast[0].value=0x2885A407;
 log=draw_classic(state,&idle,nullptr,&live);
 CHECK(has(log,"[405 reported = 101 MHz true, P8 \u2014 idle, read-only evidence, clock \u00d71.00]")&&
  has(log,"[7001 reported = 1750 MHz true, P0 \u2014 top band, PERFORMANCE-RELEVANT, clock \u00d717.29]"));
 // An unconfirmed layout: the raw words and the reason, no cycle count and no nanoseconds.
 auto raw=data;raw.snapshot.codename="GENERIC";TimingsViewState fresh;log=draw_classic(fresh,&raw,nullptr,&live);
 CHECK(has(log,"nvtune has no confirmed timing layout for GENERIC; raw registers only")&&has(log,"CONFIG0 0X2885A46A")&&has(log,"0 convert to nanoseconds"));
 CHECK(has(log,"clock moved \u2014 no ns")&&!has(log,"60.56")&&!has(log,"106")&&has(log,"1750 MHz true (GDDR6)"));
}
int main(){
 TimingViewData data;data.helper_path=L"C:\\tools\\nvtune.exe";data.snapshot.ok=true;data.snapshot.top_band=true;
 TimingField a;a.name="FAW";a.reg="CONFIG0";a.lo=8;a.hi=15;a.maximum=255;
 TimingField b;b.name="TRAIN";b.reg="CONFIG0";b.lo=0;b.hi=7;b.maximum=255;b.structural=true;
 data.snapshot.fields={a,b};data.snapshot.broadcast={{"CONFIG0","DOCUMENTED",0x1000,0xABCD182D}};
 TimingsViewState state;sync_timings_view(state,&data,17);
 CHECK(can_capture_timing(&data,false)); // hold permission is independent of read-only workload availability
 CHECK(!can_capture_timing(&data,true)&&!can_capture_timing(nullptr,false));
 data.capture_workload_pending=true;CHECK(!can_capture_timing(&data,false));data.capture_workload_pending=false;
 data.capture_hold_pending=true;CHECK(!can_capture_timing(&data,false));data.capture_hold_pending=false;
 data.capture_running=true;CHECK(!can_capture_timing(&data,false));data.capture_running=false;
 CHECK(!can_preview_timing(state,&data,false,true));CHECK(select_timing_field(state,data,"FAW")&&state.value==24);
 CHECK(can_preview_timing(state,&data,false,true)&&!can_apply_timing(state,&data,false,true));
 state.value=25;TimingPlan plan;plan.ok=true;plan.field="FAW";plan.value=25;data.preview=plan;
 CHECK(can_apply_timing(state,&data,false,true));
 CHECK(!can_apply_timing(state,&data,true,true)&&!can_apply_timing(state,&data,false,false));
 state.value=26;CHECK(!can_apply_timing(state,&data,false,true));state.value=25;
 data.snapshot.top_band=false;CHECK(!can_preview_timing(state,&data,false,true));data.snapshot.top_band=true;
 CHECK(select_timing_field(state,data,"TRAIN")&&!can_preview_timing(state,&data,false,true));
 CHECK(select_timing_field(state,data,"FAW"));data.snapshot.fields[0].overlapping=true;CHECK(!can_preview_timing(state,&data,false,true));
 data.snapshot.fields[0].overlapping=false;state.value=256;CHECK(!can_preview_timing(state,&data,false,true));
 state.value=25;auto failed=data;failed.snapshot.ok=false;failed.snapshot.fields.clear();sync_timings_view(state,&failed,17);CHECK(state.field=="FAW" && state.value==25);
 sync_timings_view(state,&data,18);CHECK(state.field.empty()&&state.generation==18);
 data.snapshot.memory_clock_stable=true;data.snapshot.slot="0000:09:00.0";data.snapshot.codename="GP102";
 data.snapshot.memory_before_mhz=data.snapshot.memory_after_mhz=7000;data.snapshot.pstate_before=data.snapshot.pstate_after=0;
 data.snapshot.offset_before=data.snapshot.offset_after=0;data.identity.nvapi_ok=1;
 std::snprintf(data.identity.slot,sizeof(data.identity.slot),"%s","0000:09:00.0");
 std::snprintf(data.identity.uuid,sizeof(data.identity.uuid),"%s","GPU-timing");
 TimingField ras;ras.name="RAS";ras.reg="CONFIG0";ras.lo=16;ras.hi=23;ras.maximum=255;data.snapshot.fields.push_back(ras);
 data.helper_token="one";sync_timings_view(state,&data,18);
 CHECK(stage_timing_field(state,data,"FAW",25)&&stage_timing_field(state,data,"RAS",200));
 CHECK(state.staged.size()==2&&select_timing_field(state,data,"FAW")&&state.value==25);
 CHECK(!stage_timing_field(state,data,"TRAIN",5)&&state.staged.size()==2);
 const auto document=capture_timing_editor_profile(data.identity,data.snapshot,state.staged);
 CHECK(stage_timing_profile(document,data,state)&&state.staged.size()==2);
 CHECK(can_preview_timing_batch(state,&data,false));
 TimingBatchPlan batch;batch.ok=true;for(const auto& r:state.staged){TimingPlan f;f.ok=true;f.field=r.field;f.value=r.value;batch.fields.push_back(f);}data.batch_preview=batch;
 CHECK(can_apply_timing_batch(state,&data,false,true)&&!can_apply_timing_batch(state,&data,false,false));
 state.staged[0].value++;CHECK(!can_apply_timing_batch(state,&data,false,true));state.staged[0].value--;
 auto invalid=document;invalid["fields"][0]["value"]=true;CHECK(!stage_timing_profile(invalid,data,state)&&state.staged.size()==2);
 data.snapshot.ok=false;sync_timings_view(state,&data,18);CHECK(state.staged.size()==2&&!can_preview_timing_batch(state,&data,false));data.snapshot.ok=true;
 data.helper_token="two";sync_timings_view(state,&data,18);CHECK(state.staged.empty());
 ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DisplaySize=ImVec2(1024,768);io.DeltaTime=1.0f/60;
 unsigned char* pixels=nullptr;int w=0,h=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 auto raw=data.snapshot;raw.ok=true;raw.top_band=false;raw.memory_before_mhz=raw.memory_after_mhz=5000;raw.pstate_before=raw.pstate_after=2;
 raw.broadcast={{"DIAGNOSTIC_ONLY","RAW",0x1040,0xDEADBEEF}};raw.fields[0].name="DIAGFIELD";raw.message="Below current top band";data.capture_diagnostic=raw;
 CHECK(select_timing_field(state,data,"FAW"));const auto selected=state.field;const auto value=state.value;const auto main_word=data.snapshot.broadcast.front().value;
 ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1000,740));ImGui::Begin("test");
 ImGui::GetStateStorage()->SetInt(ImGui::GetID("Inspect raw capture diagnostic"),1);
 ImGui::LogToBuffer();auto result=draw_timings(state,&data,18,false,true);const std::string log=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();
 CHECK(result.commands.empty()&&!result.choose_helper&&!result.save_profile&&!result.load_profile&&!result.cancel_capture);
 CHECK(log.find("DIAGNOSTIC_ONLY")!=std::string::npos&&log.find("0xDEADBEEF")!=std::string::npos);
 CHECK(log.find("5000 -> 5000 MHz")!=std::string::npos&&log.find("P2 to P2")!=std::string::npos);
 CHECK(log.find("does not authorize timing writes")!=std::string::npos);
 CHECK(state.field==selected&&state.value==value&&state.staged.empty()&&data.snapshot.broadcast.front().value==main_word&&data.snapshot.top_band);
 CHECK(!select_timing_field(state,data,"DIAGFIELD"));
 data.capture_diagnostic.reset();
 const auto press_disconnected=[&](const char* label,bool busy,bool unlocked){
     const auto* window=ImGui::FindWindowByName("test");CHECK(window);
     ImGui::ActivateItemByID(ImHashStr(label,0,window->ID));
     ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1000,740));ImGui::Begin("test");ImGui::LogToBuffer();
     auto output=draw_timings(state,&data,18,busy,unlocked,false);ImGui::LogFinish();ImGui::End();ImGui::Render();return output;
 };
 data.capture_workload_pending=true;
 auto cleanup=press_disconnected("Release timing capture resources",false,false);
 CHECK(cleanup.commands.size()==1&&cleanup.commands.front().operation==Command::Operation::timings_release_capture_hold);
 CHECK(press_disconnected("Release timing capture resources",true,true).commands.empty());
 data.capture_workload_pending=false;data.capture_hold_pending=true;
 cleanup=press_disconnected("Release timing capture resources",false,false);
 CHECK(cleanup.commands.size()==1&&cleanup.commands.front().operation==Command::Operation::timings_release_capture_hold);
 data.capture_hold_pending=false;state.value=25;
 CHECK(press_disconnected("Read timings",false,true).commands.empty());
 CHECK(press_disconnected("Capture at performance band",false,true).commands.empty());
 CHECK(press_disconnected("Apply timing",false,true).commands.empty());
 data.capture_running=true;auto cancel=press_disconnected("Cancel timing capture",true,false);
 CHECK(cancel.cancel_capture&&cancel.commands.empty());
 state.helper_registration_present=true;state.helper_registration_message="Registered helper missing";
 const auto press_setup=[&](const char* label,bool busy){auto* window=ImGui::FindWindowByName("test");ImGui::ActivateItemByID(ImHashStr(label,0,window->ID));ImGui::NewFrame();ImGui::SetNextWindowSize(ImVec2(1000,740));ImGui::Begin("test");auto out=draw_timings(state,nullptr,18,busy,false,false);ImGui::End();ImGui::Render();return out;};
 CHECK(press_setup("Forget nvtune location",false).forget_helper);CHECK(!press_setup("Forget nvtune location",true).forget_helper);CHECK(press_setup("Test signing setup...",false).test_signing_setup);state.helper_registration_present=false;CHECK(!press_setup("Forget nvtune location",false).forget_helper);
 wchar_t tmp[MAX_PATH]{},temp[MAX_PATH]{};CHECK(GetTempPathW(MAX_PATH,tmp)&&GetTempFileNameW(tmp,L"nth",0,temp));CHECK(DeleteFileW(temp)&&CreateDirectoryW(temp,nullptr));const std::filesystem::path base(temp);const auto exe=(base/L"nvtune.exe").wstring();HANDLE fh=CreateFileW(exe.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);CHECK(fh!=INVALID_HANDLE_VALUE);CloseHandle(fh);TimingHelperConfigPaths paths{(base/L"config.json").wstring(),(base/L"legacy.json").wstring()};set_timing_helper_config(paths,exe);SetEnvironmentVariableW(L"DRUTA_NVTUNE",nullptr);SetEnvironmentVariableW(L"TITANTUNE_NVTUNE",nullptr);auto suggestion=suggest_timing_helper(&paths);CHECK(suggestion.path==exe&&suggestion.registration_present);SetEnvironmentVariableW(L"DRUTA_NVTUNE",L"C:\\missing-helper.exe");suggestion=suggest_timing_helper(&paths);CHECK(suggestion.path.empty()&&suggestion.registration_present);SetEnvironmentVariableW(L"DRUTA_NVTUNE",nullptr);DeleteFileW(exe.c_str());suggestion=suggest_timing_helper(&paths);CHECK(suggestion.registration_present&&suggestion.registration_message.find("unavailable")!=std::string::npos);std::filesystem::remove_all(base);
 comparison_model();header_and_tags();classic_page();classic_setup_and_panels();classic_rest_and_setup_variants();startup_read();auto_capture_and_status();state_headline();frame_height_lines();
 failed_read_keeps_state();apply_button_and_profile_lines();classic_audit();
 header_mono_lines();share_height();tooltips();profile_picker();revert_clears_result();other_adapters();
 ImGui::DestroyContext();
 std::printf("timings_view_test: %u checks passed\n",checks);
}
