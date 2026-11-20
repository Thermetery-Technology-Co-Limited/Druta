/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timings_view.h"
#include "classic_layout.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <windows.h>
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <vector>
namespace druta::ui {
namespace {
std::wstring env(const wchar_t* key) {
    const DWORD needed=GetEnvironmentVariableW(key,nullptr,0); if (!needed) return {};
    std::vector<wchar_t> value(needed); const DWORD used=GetEnvironmentVariableW(key,value.data(),needed);
    return used && used<needed ? std::wstring(value.data(),used) : std::wstring{};
}
std::wstring executable(std::wstring path) {
    if (path.empty()) return {};
    const DWORD kind=GetFileAttributesW(path.c_str());
    if (kind!=INVALID_FILE_ATTRIBUTES && (kind&FILE_ATTRIBUTE_DIRECTORY)) {
        if (path.back()!=L'\\' && path.back()!=L'/') path+=L'\\'; path+=L"nvtune.exe";
    }
    const DWORD flags=GetFileAttributesW(path.c_str());
    if (flags==INVALID_FILE_ATTRIBUTES || (flags&FILE_ATTRIBUTE_DIRECTORY)) return {};
    const DWORD needed=GetFullPathNameW(path.c_str(),0,nullptr,nullptr); if (!needed) return {};
    std::vector<wchar_t> absolute(needed); const DWORD n=GetFullPathNameW(path.c_str(),needed,absolute.data(),nullptr);
    return n && n<needed ? std::wstring(absolute.data(),n) : std::wstring{};
}
std::string utf8(const std::wstring& value) {
    if(value.empty())return {};
    const int n=WideCharToMultiByte(CP_UTF8,0,value.data(),static_cast<int>(value.size()),nullptr,0,nullptr,nullptr);
    if(n<=0)return {};
    std::string result(static_cast<size_t>(n),'\0');
    if(!WideCharToMultiByte(CP_UTF8,0,value.data(),static_cast<int>(value.size()),&result[0],n,nullptr,nullptr))return {};
    return result;
}const TimingField* field(const TimingViewData& data,const std::string& name) {
    const auto& rows=data.snapshot.fields;
    auto it=std::find_if(rows.begin(),rows.end(),[&](const auto& f){return f.name==name;});
    return it==rows.end()?nullptr:&*it;
}
const TimingWord* word(const TimingViewData& data,const TimingField& f) {
    const auto& rows=data.snapshot.broadcast;
    auto it=std::find_if(rows.begin(),rows.end(),[&](const auto& w){return w.name==f.reg;});
    return it==rows.end()?nullptr:&*it;
}
bool editable(const TimingViewData& data,const TimingField& f) {
    const auto* w=word(data,f);
    return data.snapshot.ok && data.snapshot.top_band && w && !f.structural && !f.inferred && !f.overlapping &&
        (w->confidence.empty() || w->confidence=="DOCUMENTED");
}
}
TimingDriverState timing_driver_state(){
    TimingDriverState out;
    SC_HANDLE manager=OpenSCManagerW(nullptr,nullptr,SC_MANAGER_CONNECT);
    if(!manager){out.state="service manager unavailable (err "+std::to_string(GetLastError())+")";return out;}
    SC_HANDLE service=OpenServiceW(manager,L"nvtunedrv",SERVICE_QUERY_STATUS);
    if(!service){const auto error=GetLastError();CloseServiceHandle(manager);
        if(error==ERROR_SERVICE_DOES_NOT_EXIST){out.state="not installed";out.running=false;}
        else out.state="cannot query (err "+std::to_string(error)+")";
        return out;}
    SERVICE_STATUS status{};
    if(QueryServiceStatus(service,&status)){
        static constexpr const char* names[]{"","STOPPED","START_PENDING","STOP_PENDING","RUNNING","CONTINUE_PENDING","PAUSE_PENDING","PAUSED"};
        const auto s=status.dwCurrentState;
        out.state=s>=1&&s<=7?names[s]:"state "+std::to_string(s);out.running=s==SERVICE_RUNNING;
    }else out.state="cannot query (err "+std::to_string(GetLastError())+")";
    CloseServiceHandle(service);CloseServiceHandle(manager);
    return out;
}
TimingsHelperSuggestion suggest_timing_helper(const TimingHelperConfigPaths* paths) {
    TimingHelperConfigRead registration;try{registration=read_timing_helper_config(paths?*paths:timing_helper_config_paths());}catch(const std::exception& e){registration.message=e.what();}
    const bool registered=registration.registered_path.has_value()||registration.message=="Registered nvtune helper is unavailable";
    const auto result=[&](std::wstring path,std::string message){return TimingsHelperSuggestion{std::move(path),std::move(message),registered,registration.message,{}};};
    const auto current=env(L"DRUTA_NVTUNE"), legacy=env(L"TITANTUNE_NVTUNE");
    if (!current.empty() || !legacy.empty()) {
        auto path=executable(!current.empty()?current:legacy);
        return result(path,path.empty()?"The explicit nvtune environment path is unavailable":"nvtune found from the environment");
    }
    if(registration.registered_path)return result(*registration.registered_path,registration.legacy?"nvtune found in legacy registration":"nvtune found in registration");
    std::vector<wchar_t> self(32768); const DWORD n=GetModuleFileNameW(nullptr,self.data(),static_cast<DWORD>(self.size()));
    if (n && n<self.size()) {
        std::wstring candidate(self.data(),n); const auto slash=candidate.find_last_of(L"\\/");
        if (slash!=std::wstring::npos) { auto path=executable(candidate.substr(0,slash+1)+L"nvtune.exe"); if (!path.empty()) return result(path,"nvtune found beside the application"); }
    }
    const auto home=env(L"USERPROFILE");
    for(const auto& base:{env(L"LOCALAPPDATA"),env(L"PROGRAMFILES"),env(L"PROGRAMFILES(X86)"),home.empty()?std::wstring():home+L"\\Desktop",home.empty()?std::wstring():home+L"\\Downloads",home})if(!base.empty()){auto path=executable(base+L"\\nvtune\\nvtune.exe");if(!path.empty())return result(path,"nvtune found in an install location");}
    std::vector<wchar_t> found(32768); const DWORD count=SearchPathW(nullptr,L"nvtune.exe",nullptr,static_cast<DWORD>(found.size()),found.data(),nullptr);
    if (count && count<found.size()) { auto path=executable(std::wstring(found.data(),count)); if (!path.empty()) return result(path,"nvtune found on PATH"); }
    return result({},"Choose an installed nvtune.exe to read memory timings");
}
void sync_timings_view(TimingsViewState& state,const TimingViewData* data,std::uint64_t generation) {
    if (!state.initialized || state.generation!=generation) { const auto registered=state.helper_registration_present;const auto armed=state.classic_auto_capture;auto note=std::move(state.helper_registration_message);
        // The driver service is machine-wide, not per adapter: kept like the registration.
        const auto driver=state.helper_driver_running;auto driver_state=std::move(state.helper_driver_state);
        state={};state.helper_registration_present=registered;state.helper_registration_message=std::move(note);state.classic_auto_capture=armed;
        state.helper_driver_running=driver;state.helper_driver_state=std::move(driver_state);state.initialized=true;state.generation=generation; }
    if(data){
        if((!state.observed_helper.empty()&&state.observed_helper!=data->helper_path)||
            (!state.observed_token.empty()&&!data->helper_token.empty()&&state.observed_token!=data->helper_token)){
            state.staged.clear();state.field.clear();state.message="Timing helper changed; staged fields cleared.";
            state.captures.clear();state.filed_signature.clear();
        }
        state.observed_helper=data->helper_path;state.observed_token=data->helper_token;
    }
    if (data && data->snapshot.ok && !state.field.empty() && !field(*data,state.field)) state.field.clear();
    if (data && data->snapshot.ok) {
        // Released 1.9.1 draw_timings: tim_state shows only for a capture
        // outside the top band (show=not band); only a successful read sets it.
        const auto band=timing_perf_band(data->snapshot);state.state_line_hidden=band&&*band;
        // Released 1.9.1 _timings_worker: a capture is filed under its memory
        // clock only when that clock held still; a later one replaces it.
        std::string signature=std::to_string(data->capture_revision)+"|"+data->snapshot.slot+"|"+
            (data->snapshot.captured_unix?std::to_string(*data->snapshot.captured_unix):std::string("-"))+"|"+
            (data->snapshot.memory_before_mhz?std::to_string(*data->snapshot.memory_before_mhz):std::string("-"));
        for(const auto& w:data->snapshot.broadcast)signature+="|"+w.name+"="+std::to_string(w.value);
        if(signature!=state.filed_signature||!state.titled){
            state.filed_signature=std::move(signature);state.titled=data->snapshot;
            if(auto capture=timing_capture_for_comparison(data->snapshot)){
                const auto existing=std::find_if(state.captures.begin(),state.captures.end(),[&](const TimingCapture& c){return c.memory_mhz==capture->memory_mhz;});
                if(existing!=state.captures.end())*existing=std::move(*capture);else state.captures.push_back(std::move(*capture));
            }
        }
    }
}
bool select_timing_field(TimingsViewState& state,const TimingViewData& data,const std::string& name) {
    const auto* f=field(data,name); if(!f || !data.snapshot.ok || f->lo>f->hi || f->hi>=32)return false;
    const auto* w=word(data,*f); if(!w)return false;
    state.field=name;state.value=(w->value>>f->lo)&f->maximum;
    const auto staged=std::find_if(state.staged.begin(),state.staged.end(),[&](const auto& r){return r.field==name;});
    if(staged!=state.staged.end())state.value=staged->value;return true;
}
bool stage_timing_field(TimingsViewState& state,const TimingViewData& data,const std::string& name,uint32_t value){
    const auto* f=field(data,name);if(!f||!editable(data,*f)||value>f->maximum){state.message="Field cannot be staged from this capture.";return false;}
    auto staged=state.staged;const auto found=std::find_if(staged.begin(),staged.end(),[&](const auto& r){return r.field==name;});
    if(found==staged.end())staged.push_back({name,value});else found->value=value;
    std::sort(staged.begin(),staged.end(),[](const auto& a,const auto& b){return a.field<b.field;});state.staged=std::move(staged);state.message="Staged locally; preview the whole selection before Apply.";return true;
}
bool edit_timing_cell(TimingsViewState& state,const TimingViewData& data,const std::string& name,uint32_t value){
    const auto* f=field(data,name);const auto* w=f?word(data,*f):nullptr;
    if(!f||!w||!editable(data,*f)||f->lo>f->hi||f->hi>=32){state.message="Field cannot be staged from this capture.";return false;}
    if(value==((w->value>>f->lo)&f->maximum)){
        const auto found=std::find_if(state.staged.begin(),state.staged.end(),[&](const auto& r){return r.field==name;});
        if(found==state.staged.end())return false;
        state.staged.erase(found);state.message.clear();return true;
    }
    return stage_timing_field(state,data,name,value);
}
void note_timing_profile(TimingsViewState& state,std::string message,bool ok){
    state.profile_status=message;state.profile_status_ok=ok;state.message=std::move(message);
}
void note_timing_profile_saved(TimingsViewState& state,bool ok,const std::string& path,const std::string& error){
    note_timing_profile(state,ok?"Saved timing profile: "+path:error,ok);
}
std::size_t timing_pending_changes(const TimingsViewState& state,const TimingViewData& data){
    return static_cast<std::size_t>(std::count_if(state.staged.begin(),state.staged.end(),[&](const TimingRequest& r){
        const auto* f=field(data,r.field);const auto* w=f?word(data,*f):nullptr;
        return !f||!w||f->lo>f->hi||f->hi>=32||((w->value>>f->lo)&f->maximum)!=r.value;}));
}
namespace {
// Released 1.9.1 stage_timing_profile's source clause, from the profile's own
// capture record: Python's capture.gpu / capture.band, or this build's
// codename / band.
std::string profile_source(const ProfileJson& document){
    // Python formats a recorded null as None.
    const auto text=[](const ProfileJson& v){return v.is_string()?v.get<std::string>():v.is_number_integer()?std::to_string(v.get<long long>()):
        v.is_null()?std::string("None"):std::string("?");};
    const auto at=[&](const ProfileJson& o,const char* key){return o.is_object()&&o.contains(key)?text(o[key]):std::string("?");};
    if(document.is_object()&&document.contains("capture")&&document["capture"].is_object()){
        const auto& capture=document["capture"];
        if(capture.contains("gpu")&&capture["gpu"].is_object()&&capture.contains("band")&&capture["band"].is_object())
            return " Captured on "+at(capture["gpu"],"codename")+" at "+at(capture["band"],"memory_mhz_reported")+
                " MHz reported memory clock (P"+at(capture["band"],"pstate")+").";
    }
    if(document.is_object()&&document.contains("codename")&&document.contains("band")&&document["band"].is_object()){
        // Released 1.9.1 Snapshot.pstate: the worse of the two p-states sampled around the read.
        const auto& band=document["band"];std::string pstate="?";
        if(band.contains("pstate_before")&&band.contains("pstate_after")&&band["pstate_before"].is_number_unsigned()&&band["pstate_after"].is_number_unsigned())
            pstate=std::to_string(std::max(band["pstate_before"].get<std::uint64_t>(),band["pstate_after"].get<std::uint64_t>()));
        return " Captured on "+text(document["codename"])+" at "+at(band,"memory_mhz")+" MHz reported memory clock (P"+pstate+").";
    }
    return " Source GPU and clock band were not recorded.";
}
}
bool stage_timing_profile(const ProfileJson& document,const TimingViewData& data,TimingsViewState& state,bool classic,const std::string& path){
    try{auto fields=validate_timing_profile(document,data.identity,data.snapshot);state.staged=std::move(fields);
        if(!classic){note_timing_profile(state,"Timing profile staged. Preview and Apply remain explicit.",true);return true;}
        note_timing_profile(state,"Loaded "+std::to_string(state.staged.size())+" fields; "+std::to_string(timing_pending_changes(state,data))+
            " changes staged. Review the preview before Apply. "+path+profile_source(document),true);
        return true;
    }catch(const std::exception& error){note_timing_profile(state,error.what(),false);return false;}
}
bool can_preview_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy){
    if(!data||busy||state.staged.empty())return false;
    for(const auto& request:state.staged){const auto* f=field(*data,request.field);if(!f||!editable(*data,*f)||request.value>f->maximum)return false;}return true;
}
bool can_apply_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked){
    if(!unlocked||!can_preview_timing_batch(state,data,busy)||!data->batch_preview||!data->batch_preview->ok||data->batch_preview->no_change||data->batch_preview->fields.size()!=state.staged.size())return false;
    for(const auto& r:state.staged){const auto& fields=data->batch_preview->fields;
        if(std::none_of(fields.begin(),fields.end(),[&](const auto& f){return f.field==r.field&&f.value==r.value;}))return false;}
    return true;
}
bool can_preview_timing(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked) {
    if(!data||busy||!unlocked||state.field.empty())return false;
    const auto* f=field(*data,state.field);
    return f && editable(*data,*f) && state.value<=f->maximum;
}
bool can_apply_timing(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked) {
    return can_preview_timing(state,data,busy,unlocked) && data->preview && data->preview->ok &&
        !data->preview->no_change && data->preview->field==state.field && data->preview->value==state.value;
}
bool can_capture_timing(const TimingViewData* data,bool busy){return data&&!busy&&!data->helper_path.empty()&&!data->capture_hold_pending&&!data->capture_workload_pending&&!data->capture_running;}
bool want_timing_startup_read(TimingStartupRead& state,std::uint64_t generation,bool busy,bool opening,bool connected,const TimingViewData* timings,bool classic){
    if(state.generation!=generation){state.generation=generation;state.done=false;}
    if(!classic||state.done||busy||opening||!connected||!timings||timings->helper_path.empty())return false;
    state.done=true;
    // A capture already present (or one in flight) fills the tab already.
    return !timings->snapshot.ok&&!timings->capture_running&&!timings->capture_hold_pending&&!timings->capture_workload_pending;
}
bool timing_auto_capture_armed(const TimingsViewState& state,bool classic){return classic?state.classic_auto_capture:state.auto_capture;}
std::string classic_timing_result(const TimingViewData& data){
    return data.message==data.snapshot.message||data.message_source!=TimingViewData::Source::other?std::string():data.message;
}
float classic_timing_share_height(){
    // menu_h() is text_h('Ag') + s(8) (the 24 px menu_pad at 1x); the tab
    // selector is s(28); pinned_chrome_h()'s gate_bar is one row of s(28)
    // buttons, and hdr_row adds no measured height: 1261 - 80 = 1181 at 1x.
    return std::max(0.0f,ImGui::GetIO().DisplaySize.y-(ImGui::GetTextLineHeight()+classic_px(8))-classic_px(28)-classic_px(28));
}
std::string classic_timing_result(const TimingsViewState& state,const TimingViewData& data){
    const auto& c=state.result_cleared;
    if(c&&c->timing_actions==state.timing_actions&&c->capture_revision==data.capture_revision&&c->message==data.message&&
       c->ok==data.message_ok&&c->source==data.message_source)return {};
    return classic_timing_result(data);
}
std::string classic_timing_profile_refusal(const TimingsViewState& state,const TimingViewData* data,bool save){
    if(state.read_in_flight||(data&&data->capture_running))return "Wait for the timing capture to finish.";
    if(!data||!data->snapshot.ok)return "Read memory timings before saving or loading a profile.";
    if(!save)return {};
    // Released 1.9.1 timingprofiles.make_profile, on this build's band rule
    // (the one the session's save checks again on its fresh capture).
    if(!timing_layout_known(data->snapshot.codename))return "raw register snapshots have no confirmed timing layout; capture decoded timings first";
    if(!data->snapshot.top_band)return "capture is not in the top memory band; capture performance timings before saving";
    return {};
}
TimingProfileDialog classic_timing_profile_dialog(bool save,const std::wstring& library){
    TimingProfileDialog dialog;dialog.title=save?L"Save timing profile":L"Load timing profile";
    dialog.folder=(std::filesystem::path(library)/L"timings").wstring();if(save)dialog.file=L"timings.json";
    std::filesystem::create_directories(dialog.folder);
    return dialog;
}
TimingTone classic_timing_result_tone(const TimingViewData& data){return data.message_ok?TimingTone::good:TimingTone::bad;}
TimingPlanLine classic_timing_plan(const TimingsViewState& state,const TimingViewData& data){
    // A failed read says only that nothing is staged.
    if(state.staged.empty())return {!data.snapshot.ok&&!data.snapshot.message.empty()?"no edits staged":"no edits - every cell matches the register",TimingTone::text};
    if(!data.snapshot.ok)return {"Timing readback unavailable; staged edits are preserved. Capture again before Apply.",TimingTone::warn};
    const auto& preview=data.batch_preview;
    const auto same=[](const TimingRequest& a,const TimingRequest& b){return a.field==b.field&&a.value==b.value;};
    // The explicit preview checked exactly these edits (the same test Apply uses).
    const bool checked=preview&&preview->ok&&preview->fields.size()==state.staged.size()&&std::all_of(state.staged.begin(),state.staged.end(),[&](const TimingRequest& r){
        return std::any_of(preview->fields.begin(),preview->fields.end(),[&](const TimingPlan& p){return p.field==r.field&&p.value==r.value;});});
    const bool refused=preview&&!preview->ok&&state.previewed.size()==state.staged.size()&&
        std::equal(state.previewed.begin(),state.previewed.end(),state.staged.begin(),same);
    // Released 1.9.1 timingwrite.Plan.summary: the fields that change, then each register word.
    std::vector<std::string> bits;std::string regs;
    if(checked){
        std::vector<std::pair<std::string,std::pair<uint32_t,uint32_t>>> words;
        for(const auto& p:preview->fields){
            if(p.prior!=p.value)bits.push_back(p.field+" "+std::to_string(p.prior)+"->"+std::to_string(p.value));
            auto at=std::find_if(words.begin(),words.end(),[&](const auto& w){return w.first==p.reg;});
            if(at==words.end())words.push_back({p.reg,{p.prior_word,p.target_word}});else at->second.second=p.target_word;
        }
        for(const auto& [reg,word]:words)if(word.first!=word.second){char b[64];std::snprintf(b,sizeof(b)," 0x%08X->0x%08X",word.first,word.second);regs+=(regs.empty()?"":", ")+reg+b;}
    }else for(const auto& r:state.staged){
        const auto* f=field(data,r.field);const auto* w=f?word(data,*f):nullptr;
        if(!f||!w||f->lo>f->hi||f->hi>=32){bits.push_back(r.field+" ?->"+std::to_string(r.value));continue;}
        const auto old=(w->value>>f->lo)&f->maximum;if(old!=r.value)bits.push_back(r.field+" "+std::to_string(old)+"->"+std::to_string(r.value));
    }
    std::string summary;
    if(bits.empty())summary="nothing to write - every field already holds that value";
    else{summary=std::to_string(bits.size())+" field(s): ";for(size_t i=0;i<bits.size();++i)summary+=(i?", ":"")+bits[i];
        if(!regs.empty())summary+="   ["+regs+"]";}
    if(refused)return {"REFUSED:\n  - "+preview->message+"\n\n"+summary,TimingTone::bad};
    return {summary,checked?TimingTone::good:TimingTone::text};
}
std::string timing_auto_capture_receipt(std::uint32_t memory_mhz,std::optional<std::uint32_t> top_memory_mhz){
    return "timings: memory reached "+std::to_string(memory_mhz)+" (P0, top enumerated "+(top_memory_mhz?std::to_string(*top_memory_mhz):std::string("?"))+") - auto-capturing";
}
bool timing_live_at_p0(const TimingLiveClock& live){
    if(!live.top_memory_mhz||!*live.top_memory_mhz||!live.memory_mhz)return false;
    if(live.pstate)return *live.pstate==0&&*live.memory_mhz>=*live.top_memory_mhz;
    return *live.memory_mhz>=*live.top_memory_mhz;
}
std::string timing_live_status(const TimingLiveClock& live,const std::vector<TimingCapture>& captures,bool armed){
    if(!live.memory_mhz)return {};
    const auto mem=*live.memory_mhz;
    std::string text="  card is at "+std::to_string(mem);
    if(live.divisor&&*live.divisor){char b[48];std::snprintf(b,sizeof(b)," (%.0f MHz true)",static_cast<double>(mem)/ *live.divisor);text+=b;}
    const bool at_p0=timing_live_at_p0(live);
    const bool have=std::any_of(captures.begin(),captures.end(),[&](const TimingCapture& c){return c.memory_mhz==mem;});
    text+=" · P"+(live.pstate?std::to_string(*live.pstate):std::string("?"))+" · "+(at_p0?"P0":"below P0")+" · "+(have?"captured":"not captured");
    if(!at_p0&&armed&&live.top_memory_mhz&&*live.top_memory_mhz)text+=" · waiting for P0 (≥"+std::to_string(*live.top_memory_mhz)+", p-state 0)";
    return text;
}
namespace {
// Released 1.9.1 palette (druta.py TEXT, DIM, ACCENT, GOOD, WARN, BAD).
const ImVec4 kText(230/255.f,232/255.f,236/255.f,1),kDim(139/255.f,144/255.f,153/255.f,1),kAccent(74/255.f,163/255.f,1,1),
    kGood(70/255.f,209/255.f,122/255.f,1),kWarn(1,203/255.f,71/255.f,1),kBad(1,92/255.f,92/255.f,1);
// Released 1.9.1 TIM_WRITEWARN. The recovery sentence names this build's
// own action: a captured initial, not a stock backup.
constexpr const char* kWriteWarn=
    "WRITING A TIMING REGISTER CAN HANG THE MACHINE AND CORRUPT VRAM. Measured on our hardware: GP102 (Pascal) accepts these "
    "writes; TU102 (Turing) rejects every one of them at the hardware. This connection's first qualified capture of THIS card is "
    "kept, and 'Restore captured initial' puts it back. Timings are selected per memory CLOCK BAND, so a write edits the band the "
    "card is in right now.";
void wrapped(const ImVec4& colour,const std::string& text){ImGui::PushStyleColor(ImGuiCol_Text,colour);ImGui::TextWrapped("%s",text.c_str());ImGui::PopStyleColor();}
// A Dear PyGui add_text item: drawn on a frame-height line (22 px at 1x, the
// text 3 px down), so stacked items sit 26 px apart, empty ones included.
// wrap is DPG's wrap width, kept inside the window; 0 wraps at its edge.
void frame_wrapped(const ImVec4& colour,const std::string& text,float wrap=0){
    ImGui::AlignTextToFramePadding();
    const float edge=ImGui::GetContentRegionMax().x;
    ImGui::PushTextWrapPos(wrap>0?std::min(edge,ImGui::GetCursorPosX()+wrap):0.0f);
    ImGui::TextColored(colour,"%s",text.c_str());ImGui::PopTextWrapPos();
}
// Released 1.9.1 relayout(): tim_ro, tim_reason, tim_state, tw_warn, tw_hint,
// tw_plan, tw_result, tw_backup, tim_sub, cmp_hint, cmp_legend and div_sub
// wrap at the viewport client width less s(40), not at their built s(1100).
float page_wrap(){return std::max(1.0f,ImGui::GetIO().DisplaySize.x-classic_px(40));}
// Released 1.9.1 runs Dear ImGui 1.92.5, whose 1 px Separator() takes no
// layout height; the vendored 1.93 one takes its thickness. Classic draws the
// 1.92.5 one so the rows below sit where Python's do.
void rule(){
    ImGuiWindow* window=ImGui::GetCurrentWindow();if(window->SkipItems)return;
    const ImRect bb(ImVec2(window->DC.CursorPos.x,window->DC.CursorPos.y),ImVec2(window->WorkRect.Max.x,window->DC.CursorPos.y+1.0f));
    ImGui::ItemSize(ImVec2(0.0f,0.0f));
    if(!ImGui::ItemAdd(bb,0))return;
    window->DrawList->AddRectFilled(bb.Min,bb.Max,ImGui::GetColorU32(ImGuiCol_Separator));
    if(ImGui::GetCurrentContext()->LogEnabled)ImGui::LogRenderedText(&bb.Min,"--------------------------------\n");
}
void mono(const ImVec4& colour,const char* text){push_classic_mono();ImGui::TextColored(colour,"%s",text);ImGui::PopFont();}
// A Dear PyGui tooltip text: wrapped at s(wrap), or with wrap 0 drawn as
// written, its own line breaks only (an add_text with no wrap).
void tip(const std::string& text,float wrap=460){
    if(!ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)||!ImGui::BeginTooltip())return;
    if(wrap>0)ImGui::PushTextWrapPos(classic_px(wrap));
    ImGui::TextUnformatted(text.c_str());
    if(wrap>0)ImGui::PopTextWrapPos();
    ImGui::EndTooltip();
}
std::string printf_text(const char* format,double value){char b[64];std::snprintf(b,sizeof(b),format,value);return b;}
ImVec4 tone_colour(TimingTone tone){return tone==TimingTone::good?kGood:tone==TimingTone::warn?kWarn:tone==TimingTone::bad?kBad:kText;}
// Dear PyGui 2.3.1's table colours (Dear ImGui 1.92.5 dark): header (48,48,51),
// header underline (79,79,89), inner grid (59,59,64); the vendored 1.93 ones are
// lighter. Held from before BeginTable to after EndTable, which draws the borders.
struct ReleasedTableColours {
    ReleasedTableColours(){
        ImGui::PushStyleColor(ImGuiCol_TableHeaderBg,ImVec4(48/255.f,48/255.f,51/255.f,1));
        ImGui::PushStyleColor(ImGuiCol_TableBorderStrong,ImVec4(79/255.f,79/255.f,89/255.f,1));
        ImGui::PushStyleColor(ImGuiCol_TableBorderLight,ImVec4(59/255.f,59/255.f,64/255.f,1));
    }
    ~ReleasedTableColours(){ImGui::PopStyleColor(3);}
    ReleasedTableColours(const ReleasedTableColours&)=delete;ReleasedTableColours& operator=(const ReleasedTableColours&)=delete;
};
// Native raw capture diagnostic; shared by Focused and Classic unchanged.
void draw_raw_diagnostic(const TimingSnapshot& diagnostic){
    ImGui::TextWrapped("Raw capture diagnostic (does not authorize timing writes): %s",diagnostic.message.c_str());
    if(diagnostic.pstate_before&&diagnostic.pstate_after)ImGui::Text("Observed P-state: P%u to P%u",*diagnostic.pstate_before,*diagnostic.pstate_after);
    if(ImGui::CollapsingHeader("Inspect raw capture diagnostic")){
        ImGui::Text("Captured adapter: %s | %s",diagnostic.slot.c_str(),diagnostic.codename.c_str());
        ImGui::Text("Memory stable: %s | current top band: %s",diagnostic.memory_clock_stable?"yes":"unconfirmed",diagnostic.top_band?"yes":"not established");
        const auto observation=[](const char* label,const auto& before,const auto& after,const char* units){
            if(before&&after)ImGui::Text("%s: %lld -> %lld %s",label,static_cast<long long>(*before),static_cast<long long>(*after),units);
            else ImGui::Text("%s: unavailable",label);
        };
        observation("Memory clock",diagnostic.memory_before_mhz,diagnostic.memory_after_mhz,"MHz");
        observation("Memory offset request",diagnostic.offset_before,diagnostic.offset_after,"raw driver units");
        ImGui::TextWrapped("Read-only captured values. This panel never replaces the qualified editor capture or stages fields.");
        if(ImGui::BeginTable("capture_diagnostic_words",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,160))){
            ImGui::TableSetupColumn("Register");ImGui::TableSetupColumn("Address");ImGui::TableSetupColumn("Captured raw value");ImGui::TableSetupColumn("Evidence");ImGui::TableHeadersRow();
            for(const auto& row:diagnostic.broadcast){ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);ImGui::TextUnformatted(row.name.c_str());
                ImGui::TableSetColumnIndex(1);ImGui::Text("0x%08X",row.offset);ImGui::TableSetColumnIndex(2);ImGui::Text("0x%08X",row.value);
                ImGui::TableSetColumnIndex(3);ImGui::TextUnformatted(row.confidence.c_str());}
            ImGui::EndTable();
        }
    }
}
// Released 1.9.1 mem_hint: an enumerated state of THIS card, or a phrase.
std::string memory_hint(const TimingSnapshot& s,int from_top){
    if(s.memory_states.empty())return "the top memory state";
    const auto n=static_cast<int>(s.memory_states.size());
    const auto clock=s.memory_states[static_cast<size_t>(n-from_top>=0?n-from_top:n-1)];
    const auto& div=s.diagnostic_evidence.memory_clock_divisor;
    if(!div||!(*div>0)||s.diagnostic_evidence.divisor_provenance.empty())return "~"+std::to_string(clock)+" reported";
    return "~"+std::to_string(clock)+" reported / ~"+printf_text("%.0f",clock / *div)+" MHz true";
}
ImVec4 verdict_colour(const std::string& v){return v=="tracks"?kGood:v=="partial"?kWarn:v=="flat"||v=="--"?kDim:kText;}
// height: released 1.9.1 relayout() sizes pan_tim_hdr to tim_hdr_h(), the
// estimate from the text, not to the drawn mono lines. With a warning shown
// that estimate cuts into the raw words line, so the box then fits its content.
void draw_header_box(const TimingSnapshot& snapshot,float height){
    const auto header=timing_header(snapshot);const bool fit=!header.warnings.empty();
    if(ImGui::BeginChild("timing_header",ImVec2(-FLT_MIN,fit?0.0f:height),ImGuiChildFlags_Borders|(fit?ImGuiChildFlags_AutoResizeY:0),
                         ImGuiWindowFlags_NoScrollbar|ImGuiWindowFlags_NoScrollWithMouse)){
        // Released 1.9.1 pan_tim_hdr: three mono add_text items, each on a
        // frame-height line of the mono font (14 + 6, text 3 px down), so
        // they sit 24 px apart with the stock item spacing.
        const auto line=[](const ImVec4& colour,const std::string& text){
            push_classic_mono();ImGui::AlignTextToFramePadding();ImGui::TextColored(colour,"%s",text.c_str());ImGui::PopFont();};
        line(kAccent,header.ident);
        line(header.clock_trustworthy||!snapshot.ok?kText:kWarn,header.clock);
        // Released 1.9.1 tim_warn: one body-font item, the warnings one per
        // line with a U+26A0 prefix (the same glyph lookup and fallback apply).
        std::string warn;for(const auto& w:header.warnings)warn+=(warn.empty()?"":"\n")+("⚠ "+w);
        if(!warn.empty())frame_wrapped(kBad,warn,page_wrap());
        if(!header.words.empty())line(kDim,header.words);
    }
    ImGui::EndChild();
}
void draw_edit_block(TimingsViewState& state,const TimingViewData& data,bool busy,bool unlocked,TimingsViewOutput& out){
    // Released 1.9.1 add_text items: frame-height lines.
    ImGui::Dummy(ImVec2(0,classic_px(6)));
    frame_wrapped(kBad,"EDIT TIMINGS  \u00b7  writes to the memory controller");
    frame_wrapped(kBad,kWriteWarn,page_wrap());
    frame_wrapped(kDim,"Edit the 'new value' column in the table below. A cell is green while it matches the register and red once it does "
        "not; only red rows are written.",page_wrap());
    ImGui::Dummy(ImVec2(0,classic_px(4)));
    // tw_plan: one line in the released format, the preview's verdict on it.
    const auto plan=classic_timing_plan(state,data);
    frame_wrapped(tone_colour(plan.tone),plan.text,page_wrap());
    // Native-only: drop one staged field, kept under a closed node.
    if(!state.staged.empty()&&ImGui::TreeNode("Staged fields###tw_staged")){
        for(size_t i=0;i<state.staged.size();++i){const auto& r=state.staged[i];ImGui::PushID(static_cast<int>(i));
            const auto* f=field(data,r.field);const auto* w=f?word(data,*f):nullptr;
            if(f&&w&&f->lo<=f->hi&&f->hi<32)ImGui::Text("%s: %u -> %u",r.field.c_str(),(w->value>>f->lo)&f->maximum,r.value);
            else ImGui::TextColored(kWarn,"%s: -- -> %u (refresh required)",r.field.c_str(),r.value);
            ImGui::SameLine();if(ImGui::SmallButton("Remove")){state.staged.erase(state.staged.begin()+static_cast<std::ptrdiff_t>(i));ImGui::PopID();break;}ImGui::PopID();
        }
        ImGui::TreePop();
    }
    ImGui::Dummy(ImVec2(0,classic_px(4)));
    // Released 1.9.1 rows: Apply (260) with its modifier beside it, then the
    // two ways back (150 each), then the profile pair. This build's modifier
    // slot holds the explicit batch preview Apply requires (no force box).
    const float gap=ImGui::GetStyle().ItemSpacing.x*2+classic_px(10);
    // Released 1.9.1 tw_button: red, and counting, exactly while it would
    // write; neutral otherwise. The label keeps one ID as its count changes.
    const auto changes=timing_pending_changes(state,data);
    const std::string apply_label=(changes?"Apply "+std::to_string(changes)+" change(s) to the memory controller":
        std::string("Apply to memory controller"))+"###tw_apply";
    if(changes){ImGui::PushStyleColor(ImGuiCol_Button,ImVec4(122/255.f,30/255.f,34/255.f,1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,ImVec4(160/255.f,40/255.f,45/255.f,1));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,ImVec4(190/255.f,50/255.f,55/255.f,1));
        ImGui::PushStyleColor(ImGuiCol_Text,ImVec4(1,235/255.f,235/255.f,1));}
    // Released 1.9.1 never disables tw_apply: with nothing staged a click
    // writes nothing (its guard still says when controls are locked). Staged
    // edits keep this build's rule: enabled once previewed.
    const bool idle=state.staged.empty();
    ImGui::BeginDisabled(!idle&&!can_apply_timing_batch(state,&data,busy,unlocked));
    if(ImGui::Button(apply_label.c_str(),ImVec2(classic_px(260),0))){
        if(idle){if(!unlocked)out.classic_log.push_back({"locked - tick 'Unlock controls' first",false});}
        else{auto request=std::make_shared<TimingEditorRequest>();request->fields=state.staged;
            Command c{};c.operation=Command::Operation::timings_apply_batch;c.timing_editor=std::move(request);out.commands.push_back(std::move(c));}}
    ImGui::EndDisabled();
    if(changes)ImGui::PopStyleColor(4);
    tip("Writes the staged (red) rows. Enabled once 'Preview all staged' has checked exactly these values; controls must be unlocked.");
    ImGui::SameLine(0,gap);
    ImGui::BeginDisabled(!can_preview_timing_batch(state,&data,busy));
    if(ImGui::Button("Preview all staged")){auto request=std::make_shared<TimingEditorRequest>();request->fields=state.staged;state.previewed=state.staged;
        Command c{};c.operation=Command::Operation::timings_preview_batch;c.timing_editor=std::move(request);out.commands.push_back(std::move(c));}ImGui::EndDisabled();
    ImGui::Dummy(ImVec2(0,classic_px(2)));
    const auto way_back=[](const char* label){return ImVec2(std::max(classic_px(150),ImGui::CalcTextSize(label).x+ImGui::GetStyle().FramePadding.x*2),0);};
    // Released 1.9.1 tw_clear: the cells go back and tw_result empties.
    if(ImGui::Button("Revert edits",way_back("Revert edits"))){state.staged.clear();
        state.result_cleared=TimingsViewState::ClearedResult{data.message,data.message_ok,data.message_source,data.capture_revision,state.timing_actions};}
    ImGui::SameLine(0,gap);
    ImGui::BeginDisabled(busy||!unlocked||!data.initial_available);
    if(ImGui::Button("Restore captured initial",way_back("Restore captured initial"))){Command c{};c.operation=Command::Operation::timings_restore_initial;out.commands.push_back(c);}ImGui::EndDisabled();
    tip("Captured initial means this connection's first qualified capture, not factory defaults.");
    ImGui::SameLine(0,gap);
    ImGui::BeginDisabled(busy||!unlocked||!data.undo_available);
    if(ImGui::Button("Undo timing write")){Command c{};c.operation=Command::Operation::timings_undo;out.commands.push_back(c);}ImGui::EndDisabled();
    // Released 1.9.1 never disables these: the host refuses in tw_profile_status
    // (classic_timing_profile_refusal) while a read runs or before one.
    if(ImGui::Button("Save timing profile..."))out.save_profile=true;ImGui::SameLine();
    if(ImGui::Button("Load timing profile..."))out.load_profile=true;
    frame_wrapped(kDim,"Save includes the captured values and your edits. Load replaces staged edits; review and Apply to write.",classic_px(1100));
    // tw_profile_status: an empty line at rest.
    frame_wrapped(state.profile_status_ok?kText:kBad,state.profile_status,classic_px(1100));
}
void draw_new_value(TimingsViewState& state,const TimingViewData& data,const TimingField& f,const std::optional<uint32_t>& cycles){
    const auto* w=word(data,f);
    if(!cycles||f.structural||f.inferred||f.overlapping||!w){
        mono(kDim,"--");
        tip(std::string("not writable: ")+(f.inferred?"this register's offset is INFERRED, not observed":
            f.structural?"structural - a training/phase value with no safe direction to nudge":
            f.overlapping?"its bit range overlaps another field":"no captured value"));
        return;
    }
    const auto staged=std::find_if(state.staged.begin(),state.staged.end(),[&](const auto& r){return r.field==f.name;});
    uint32_t value=staged!=state.staged.end()?staged->value:*cycles;
    const bool allowed=editable(data,f);
    ImGui::PushID(f.name.c_str());ImGui::SetNextItemWidth(classic_px(92));
    ImGui::PushStyleColor(ImGuiCol_Text,value!=*cycles?kBad:kGood);
    ImGui::BeginDisabled(!allowed);
    const ImU32 step=1,step_fast=100;
    if(ImGui::InputScalar("##new_value",ImGuiDataType_U32,&value,&step,&step_fast,"%u")&&allowed)edit_timing_cell(state,data,f.name,std::min(value,f.maximum));
    ImGui::EndDisabled();ImGui::PopStyleColor();ImGui::PopID();
    if(!allowed)tip(!data.snapshot.top_band?"Capture in the top memory band to edit; a write edits the band the card is in.":
        "Register geometry is unconfirmed by the helper; this field cannot be written.");
}
// Rows whose partition value reads back differently from the broadcast word.
struct TimingDivergence { std::string scope, field, reg; uint32_t broadcast=0, value=0; };
std::vector<TimingDivergence> timing_divergence(const TimingSnapshot& s,const TimingDiagnostics& d){
    std::vector<TimingDivergence> out;
    for(size_t i=0;i<d.rows.size()&&i<s.fields.size();++i)for(const auto& p:d.rows[i].partitions)
        if(p.differs&&p.value&&d.rows[i].value)out.push_back({p.scope,s.fields[i].name,s.fields[i].reg,*d.rows[i].value,*p.value});
    return out;
}
// Released 1.9.1 tim_sub, plus draw_divergence's agreement sentence.
std::string decoded_subtitle(const TimingSnapshot& s,const TimingDiagnostics& d,size_t diverging){
    // Count what WOULD convert at a steady clock, so a moved clock is said
    // as 'not shown' rather than as 'none convert'.
    auto steady=s;steady.memory_clock_stable=true;steady.memory_after_mhz=steady.memory_before_mhz;
    const auto converted=timing_diagnostics(steady,steady.diagnostic_evidence,true);
    const auto n_ns=std::count_if(converted.rows.begin(),converted.rows.end(),[](const TimingDiagnosticRow& r){return r.nanoseconds.has_value();});
    const bool stable=s.memory_clock_stable&&s.memory_before_mhz&&s.memory_after_mhz&&*s.memory_before_mhz==*s.memory_after_mhz;
    std::string text=std::to_string(s.fields.size())+" fields, bit ranges parsed from `nvtune fields` at runtime so the decode cannot drift from "
        "the installed tool. "+std::to_string(n_ns)+" convert to nanoseconds"+(stable?"":" - but NOT shown, because the clock moved during this capture")+
        ". A cycle count is only a time when you know the clock it was counted against; that clock is on the line above.";
    if(diverging)return text;
    const auto& parts=s.diagnostic_evidence.partitions;
    if(parts.empty())return text+"\nPartition words were not captured, so partition agreement is unknown.";
    std::string names;for(const auto& p:parts)names+=(names.empty()?"":", ")+p.scope;
    bool complete=true;for(const auto& r:d.rows)if(r.value)for(const auto& p:r.partitions)complete=complete&&p.value.has_value();
    text+="\nAll "+std::to_string(parts.size())+" partitions ("+names+") match the broadcast aperture"+(complete?" exactly":" where read")+", so they are not drawn again.";
    if(!complete)text+=" Fields absent from a partition capture are unknown.";
    if(s.diagnostic_evidence.partitions_stale)text+=" The partition words come from an earlier capture.";
    return text;
}
// rows=false: the header row alone, as released 1.9.1 tim_table stands
// before a capture and after a failed read.
void draw_field_table(TimingsViewState& state,const TimingViewData& data,const TimingDiagnostics& diagnostics,bool rows=true){
    const auto& snapshot=data.snapshot;
    // Released 1.9.1 TIM_COLS.
    static constexpr struct {const char* label;float width;} columns[]{{"field",118},{"register",88},{"bits",76},{"cycles",62},
        {"new value",104},{"ns at the capture clock",250},{"what it is",560}};
    const ReleasedTableColours colours;
    if(!ImGui::BeginTable("timing_fields",7,ImGuiTableFlags_BordersInnerH|ImGuiTableFlags_BordersInnerV|ImGuiTableFlags_SizingFixedFit|
        ImGuiTableFlags_NoHostExtendX))return;
    for(const auto& c:columns)ImGui::TableSetupColumn(c.label,ImGuiTableColumnFlags_WidthFixed,classic_px(c.width));
    ImGui::TableHeadersRow();
    if(!rows){ImGui::EndTable();return;}
    const bool known=timing_layout_known(snapshot.codename);
    // Released 1.9.1 Snapshot.ns_trustworthy: a known layout, a clock that held
    // still and its true MHz. Otherwise no row may print nanoseconds.
    const bool trusted=timing_header(snapshot).clock_trustworthy;
    for(size_t i=0;i<snapshot.fields.size();++i){
        const auto& f=snapshot.fields[i];const auto& d=diagnostics.rows[i];
        const std::optional<uint32_t> cycles=known?d.value:std::nullopt;
        ImGui::TableNextRow();
        // Every cell is a DPG add_text on a frame-height line: the row is the
        // input's height and its texts share the input's baseline.
        ImGui::TableSetColumnIndex(0);ImGui::AlignTextToFramePadding();ImGui::TextColored(f.inferred?kWarn:kText,"%s",f.name.c_str());
        // Released 1.9.1: the helper's own notes on the register follow the colon.
        if(f.inferred)tip("register offset is INFERRED, not established:\n"+f.note,520);
        ImGui::TableSetColumnIndex(1);ImGui::TextColored(kDim,"%s",f.reg.c_str());
        ImGui::TableSetColumnIndex(2);{char bits[24];std::snprintf(bits,sizeof(bits),"[%u:%u]",f.hi,f.lo);mono(kDim,bits);}
        ImGui::TableSetColumnIndex(3);
        if(cycles){mono(kText,std::to_string(*cycles).c_str());if(!d.provenance.empty())tip("Units: "+d.provenance);}
        else{mono(kText,"--");tip(known?"no captured value for this field":"unknown timing layout; raw registers only");}
        ImGui::TableSetColumnIndex(4);draw_new_value(state,data,f,cycles);
        ImGui::TableSetColumnIndex(5);
        // Released 1.9.1 tim_ns_cell: a number only when the field converts and
        // the clock held still; otherwise which of those failed, in the cell.
        if(!trusted){ImGui::TextColored(kBad,"clock moved \u2014 no ns");
            tip("The memory clock changed while these registers were being read, so there is no single clock to count them against. Capture again once it settles.");}
        else if(d.nanoseconds)mono(snapshot.top_band?kText:kWarn,printf_text("%8.2f ns",*d.nanoseconds).c_str());
        else{ImGui::TextColored(f.name=="RFC"||f.name=="WL"?kWarn:kDim,"encodes differently \u2014 not ns");tip(timing_ns_refusal(snapshot,f,d));}
        ImGui::TableSetColumnIndex(6);
        // Released 1.9.1 wraps the description at the column width less 14.
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(560-14));
        ImGui::TextColored(f.structural?kDim:kText,"%s",f.description.c_str());ImGui::PopTextWrapPos();
    }
    ImGui::EndTable();
}
// Released 1.9.1 pan_tim: bordered, titled by the capture's clock band.
void draw_decoded_panel(TimingsViewState& state,const TimingViewData& data,const TimingDiagnostics& diagnostics,size_t diverging,float height){
    const auto& s=data.snapshot;
    if(ImGui::BeginChild("timing_decoded",ImVec2(-FLT_MIN,height),ImGuiChildFlags_Borders)){
        // Title and subtitle are DPG add_text items: frame-height lines. Before
        // any capture they read as built (the title, an empty subtitle); a
        // failed read keeps the last capture's pair and empties only the rows.
        const TimingSnapshot* titled=s.ok?&s:state.titled?&*state.titled:nullptr;
        if(!titled){frame_wrapped(kAccent,"DECODED TIMINGS  ·  broadcast aperture");frame_wrapped(kDim,"",page_wrap());}
        else{
            const auto band=timing_perf_band(*titled);
            if(band&&*band)frame_wrapped(kAccent,"DECODED TIMINGS  ·  broadcast aperture  ·  "+timing_state_tag(*titled)+", top clock band");
            else frame_wrapped(kBad,"DECODED TIMINGS  ·  IDLE STATE — NOT PERFORMANCE-RELEVANT");
            if(titled==&s)frame_wrapped(kDim,decoded_subtitle(s,diagnostics,diverging),page_wrap());
            else{const auto earlier=timing_diagnostics(*titled,titled->diagnostic_evidence,true);
                frame_wrapped(kDim,decoded_subtitle(*titled,earlier,timing_divergence(*titled,earlier).size()),page_wrap());}
        }
        rule();
        draw_field_table(state,data,diagnostics,s.ok);
    }
    ImGui::EndChild();
}
// Released 1.9.1 pan_div: shown only while a partition disagrees.
void draw_divergence(const std::vector<TimingDivergence>& rows,float height){
    if(!ImGui::BeginChild("timing_divergence",ImVec2(-FLT_MIN,height),ImGuiChildFlags_Borders)){ImGui::EndChild();return;}
    frame_wrapped(kBad,"PARTITION DIVERGENCE");
    frame_wrapped(kDim,std::to_string(rows.size())+" field(s) differ from the broadcast aperture. The broadcast window writes all partitions at once, "
        "so a partition reading back differently means one of them is not taking the same timings - worth knowing before trusting the "
        "table above as 'the' memory timing.",page_wrap());
    rule();
    // Pushed and popped inside the panel: a child checks its style stack at EndChild.
    if(const ReleasedTableColours colours;ImGui::BeginTable("timing_divergence_rows",5,ImGuiTableFlags_BordersInnerH|ImGuiTableFlags_BordersInnerV|ImGuiTableFlags_SizingFixedFit|ImGuiTableFlags_NoHostExtendX)){
        static constexpr struct {const char* label;float width;} columns[]{{"partition",110},{"field",118},{"register",88},{"broadcast",90},{"this partition",110}};
        for(const auto& c:columns)ImGui::TableSetupColumn(c.label,ImGuiTableColumnFlags_WidthFixed,classic_px(c.width));
        ImGui::TableHeadersRow();
        for(const auto& r:rows){ImGui::TableNextRow();
            // DPG add_text cells: frame-height rows, one baseline.
            ImGui::TableSetColumnIndex(0);ImGui::AlignTextToFramePadding();ImGui::TextColored(kWarn,"%s",r.scope.c_str());
            ImGui::TableSetColumnIndex(1);ImGui::TextUnformatted(r.field.c_str());
            ImGui::TableSetColumnIndex(2);ImGui::TextColored(kDim,"%s",r.reg.c_str());
            ImGui::TableSetColumnIndex(3);mono(kText,std::to_string(r.broadcast).c_str());
            ImGui::TableSetColumnIndex(4);mono(kBad,std::to_string(r.value).c_str());}
        ImGui::EndTable();
    }
    ImGui::EndChild();
}
void draw_comparison(const TimingsViewState& state,const TimingSnapshot& current,float height){
    const auto& caps=state.captures;
    const auto cmp=compare_timing_captures(caps);
    if(!ImGui::BeginChild("timing_compare",ImVec2(-FLT_MIN,height),ImGuiChildFlags_Borders)){ImGui::EndChild();return;}
    // Title, hint and legend are DPG add_text items: frame-height lines.
    frame_wrapped(kAccent,"CAPTURES COMPARED  \u00b7  cycle ratio vs clock ratio");
    if(cmp.order.size()<2){
        frame_wrapped(kDim,std::to_string(caps.size())+" capture"+(caps.size()==1?"":"s")+". Two are needed, at DIFFERENT memory clocks. Let the card "
            "idle until the memory clock drops ("+memory_hint(current,2)+") and press Capture, then load it to the top state ("+
            memory_hint(current,1)+") and press Capture again. The same registers decode to different cycle counts at the two states, and this "
            "table is where that stops looking like noise and starts being the proof.",page_wrap());
        rule();ImGui::EndChild();return;
    }
    size_t tracks=0,flat=0,partial=0,none=0;
    for(const auto& r:cmp.rows){if(r.verdict=="tracks")++tracks;else if(r.verdict=="flat")++flat;else if(r.verdict=="partial")++partial;else ++none;}
    frame_wrapped(kDim,"Baseline is the slowest capture. Each column is one captured memory state: the cycle count, then \u00d7 its ratio to the "
        "baseline. The column heading carries what the CLOCK did over the same two states. A field that is a cycle count of a fixed time "
        "has to move by the clock ratio - the two numbers agreeing is the decode being right. Cycle counts are integers, so small ones "
        "round hard and their ratios are coarse; that is rounding, not disagreement.\n"+std::to_string(tracks)+" of "+std::to_string(cmp.rows.size())+
        " fields moved by the memory-clock ratio (within the rounding a whole-cycle count carries)   \u00b7   "+std::to_string(flat)+
        " flat   \u00b7   "+std::to_string(partial)+" partial   \u00b7   "+std::to_string(none)+" no data.",page_wrap());
    std::string legend;
    for(size_t k=0;k<cmp.order.size();++k){const auto& c=caps[cmp.order[k]];
        legend+=(legend.empty()?"":"   ")+std::string("[")+std::to_string(c.memory_mhz)+" reported = "+
            (c.true_mhz?printf_text("%.0f",*c.true_mhz)+" MHz true":std::string("? MHz"))+(c.pstate?", P"+std::to_string(*c.pstate):std::string())+
            (c.top_band?" \u2014 top band, PERFORMANCE-RELEVANT":" \u2014 idle, read-only evidence")+", clock \u00d7"+printf_text("%.2f",cmp.clock_ratios[k])+"]";}
    frame_wrapped(kText,legend,page_wrap());
    rule();
    const int count=static_cast<int>(cmp.order.size())+4;
    // Pushed and popped inside the panel: a child checks its style stack at EndChild.
    if(const ReleasedTableColours colours;count<=64&&ImGui::BeginTable("timing_comparison",count,ImGuiTableFlags_BordersInnerH|ImGuiTableFlags_BordersInnerV|ImGuiTableFlags_SizingFixedFit|
        ImGuiTableFlags_NoHostExtendX|ImGuiTableFlags_ScrollY|ImGuiTableFlags_ScrollX,ImVec2(0,std::max(classic_px(120),ImGui::GetContentRegionAvail().y)))){
        ImGui::TableSetupColumn("field",ImGuiTableColumnFlags_WidthFixed,classic_px(118));
        ImGui::TableSetupColumn("register",ImGuiTableColumnFlags_WidthFixed,classic_px(88));
        for(size_t k=0;k<cmp.order.size();++k){const auto& c=caps[cmp.order[k]];
            auto label=std::to_string(c.memory_mhz)+" "+c.tag;if(cmp.clock_ratios[k]!=1.0)label+="  (clock \u00d7"+printf_text("%.2f",cmp.clock_ratios[k])+")";
            label+="##capture"+std::to_string(k);
            ImGui::TableSetupColumn(label.c_str(),ImGuiTableColumnFlags_WidthFixed,classic_px(168));}
        ImGui::TableSetupColumn("verdict",ImGuiTableColumnFlags_WidthFixed,classic_px(90));
        ImGui::TableSetupColumn("##note",ImGuiTableColumnFlags_WidthFixed,classic_px(430));
        ImGui::TableSetupScrollFreeze(0,1);ImGui::TableHeadersRow();
        for(const auto& r:cmp.rows){ImGui::TableNextRow();
            // DPG add_text cells: frame-height rows, one baseline.
            ImGui::TableSetColumnIndex(0);ImGui::AlignTextToFramePadding();ImGui::TextUnformatted(r.name.c_str());
            ImGui::TableSetColumnIndex(1);ImGui::TextColored(kDim,"%s",r.reg.c_str());
            for(size_t k=0;k<r.cycles.size();++k){ImGui::TableSetColumnIndex(static_cast<int>(k)+2);char cell[48];
                if(!r.cycles[k]){mono(kDim,"--");continue;}
                if(k==0){std::snprintf(cell,sizeof(cell),"%5u",*r.cycles[k]);mono(kText,cell);continue;}
                if(r.ratios[k])std::snprintf(cell,sizeof(cell),"%5u   \u00d7%5.2f",*r.cycles[k],*r.ratios[k]);
                else std::snprintf(cell,sizeof(cell),"%5u   \u00d7   --",*r.cycles[k]);
                mono(verdict_colour(r.verdict),cell);}
            ImGui::TableSetColumnIndex(count-2);ImGui::TextColored(verdict_colour(r.verdict),"%s",r.verdict.c_str());
            ImGui::TableSetColumnIndex(count-1);wrapped(kDim,r.note);
        }
        ImGui::EndTable();
    }
    ImGui::EndChild();
}
// Native single-field Preview / Apply for the selected field.
void draw_single_field(TimingsViewState& state,const TimingViewData& data,bool busy,bool unlocked,TimingsViewOutput& out){
    if(!state.field.empty()) {
        ImGui::Text("Selected: %s",state.field.c_str()); ImGui::SetNextItemWidth(140);
        ImGui::InputScalar("Field value",ImGuiDataType_U32,&state.value);
        const auto* selected=field(data,state.field);
        ImGui::BeginDisabled(busy||!selected||!editable(data,*selected)||state.value>selected->maximum);
        if(ImGui::Button("Stage field"))stage_timing_field(state,data,state.field,state.value);ImGui::EndDisabled();
        ImGui::SameLine();
        ImGui::BeginDisabled(!can_preview_timing(state,&data,busy,unlocked));
        if(ImGui::Button("Preview timing")){Command c{};c.operation=Command::Operation::timings_preview;c.timing_field=state.field;c.timing_value=state.value;out.commands.push_back(c);} ImGui::EndDisabled();
        ImGui::SameLine();ImGui::BeginDisabled(!can_apply_timing(state,&data,busy,unlocked));
        if(ImGui::Button("Apply timing")){Command c{};c.operation=Command::Operation::timings_apply;c.timing_field=state.field;c.timing_value=state.value;out.commands.push_back(c);} ImGui::EndDisabled();
    }
    if(data.preview && data.preview->ok) ImGui::TextWrapped("Preview %s: %u -> %u; selected bits only",data.preview->field.c_str(),data.preview->prior,data.preview->value);
}
// Released 1.9.1 TIM_READONLY, worded for this build: there is no separate
// read module here, but the read/write boundary it states is the same.
constexpr const char* kReadOnly=
    "Reading goes through nvtune's read-only subcommands: a read cannot carry a writing command line at all. Writing lives in one "
    "other path, and THIS TAB DRIVES IT - the Apply, Restore and Undo buttons below are that path.";
void text_line(const ImVec4& colour,const std::string& text){ImGui::AlignTextToFramePadding();ImGui::TextColored(colour,"%s",text.c_str());}
// Released 1.9.1 tim_needs: shown INSTEAD of the tab while nvtune is missing
// or its driver is not running; the missing half decides the wording.
void draw_setup_screen(TimingsViewState& state,const TimingViewData* data,bool worker_busy,bool busy,bool no_exe,TimingsViewOutput& out){
    // Every text here is a DPG add_text item on a frame-height line.
    ImGui::Dummy(ImVec2(0,classic_px(20)));
    ImGui::PushFont(classic_fonts.heading,26.0f);text_line(kBad,no_exe?"NVTUNE NOT FOUND":"NVTUNE'S DRIVER IS NOT RUNNING");ImGui::PopFont();
    ImGui::Dummy(ImVec2(0,classic_px(6)));
    const float wrap=classic_px(980);
    frame_wrapped(kText,no_exe?"This register editor uses nvtune to read and write the framebuffer-partition memory timings. nvtune is a separate "
        "program with its own kernel driver.":
        "nvtune.exe was found, but its kernel driver is not running. The register editor needs that driver. The driver is signed with a self-signed "
        "test certificate, which Windows will not load unless the machine is in test signing mode.",wrap);
    ImGui::Dummy(ImVec2(0,classic_px(10)));
    // tim_reason is one of the texts relayout() rewraps at the page width.
    const std::string driver_state=state.helper_driver_state.empty()?std::string("unknown"):state.helper_driver_state;
    std::string reason;
    if(no_exe){
        reason=state.message.empty()?std::string("Choose an installed nvtune.exe to read memory timings"):state.message;
        if(!state.helper_registration_message.empty())reason+="\n"+state.helper_registration_message;
        if(data&&!data->message.empty())reason+="\n"+data->message;
        if(state.helper_driver_running&&!*state.helper_driver_running)
            reason+="\n\nIts kernel driver service 'nvtunedrv' is also not running (state: "+driver_state+").";
    }else{
        const auto path=data?utf8(data->helper_path):std::string();
        reason="nvtune.exe is present"+(path.empty()?std::string():" ("+path+")")+" but its kernel driver service 'nvtunedrv' is not running (state: "+
            driver_state+"). It maps the GPU's BAR0 FBPA aperture for the nvtune register editor. Start it from an elevated prompt:  sc start nvtunedrv";
    }
    reason+="\n\nThe nvtune register editor is unavailable until its helper and driver are ready.";
    frame_wrapped(kWarn,reason,page_wrap());
    ImGui::Dummy(ImVec2(0,classic_px(16)));
    // Released 1.9.1 binds tim_step1 to its 'sel' font (semibold 21).
    ImGui::PushFont(classic_fonts.heading,21.0f);text_line(kAccent,no_exe?"Point Druta at it:":"Let Windows load its driver:");ImGui::PopFont();
    ImGui::Dummy(ImVec2(0,classic_px(4)));
    const ImVec2 size(classic_px(260),classic_px(34));const float gap=ImGui::GetStyle().ItemSpacing.x*2+classic_px(14);
    ImGui::BeginDisabled(busy);if(ImGui::Button("Locate nvtune.exe...",size))out.choose_helper=true;ImGui::EndDisabled();
    ImGui::SameLine(0,gap);ImGui::BeginDisabled(worker_busy);if(ImGui::Button("Enable test signing...",size))out.test_signing_setup=true;ImGui::EndDisabled();
    // Native-only: drop a registration that no longer resolves.
    if(state.helper_registration_present){ImGui::SameLine(0,gap);ImGui::BeginDisabled(worker_busy);
        if(ImGui::Button("Forget nvtune location",size))out.forget_helper=true;ImGui::EndDisabled();}
    ImGui::Dummy(ImVec2(0,classic_px(16)));
    frame_wrapped(kDim,"nvtune is a separate program by Sebastian Marrufo, GPL-3.0-or-later, not shipped with Druta:",wrap);
    static char upstream[]="https://github.com/sebastianmarrufo/nvtune";
    push_classic_mono();ImGui::SetNextItemWidth(classic_px(560));
    ImGui::InputText("##tim_upstream",upstream,sizeof(upstream),ImGuiInputTextFlags_ReadOnly);ImGui::PopFont();
    ImGui::Dummy(ImVec2(0,classic_px(10)));
    frame_wrapped(kDim,"The nvtune register editor needs its BAR0 driver. Its driver is self-signed, so loading it costs "
        "Secure Boot, Memory Integrity and driver-signature enforcement, machine-wide. Read what the button says before you use it.",wrap);
}
// Released 1.9.1 read row: blue held read, cheap re-read, auto-capture.
void draw_read_row(TimingsViewState& state,const TimingViewData& data,std::uint64_t generation,bool busy,bool unlocked,TimingsViewOutput& out){
    const float gap=ImGui::GetStyle().ItemSpacing.x*2+classic_px(10);
    ImGui::BeginDisabled(!can_capture_timing(&data,busy));
    ImGui::PushStyleColor(ImGuiCol_Button,ImVec4(26/255.f,84/255.f,152/255.f,1));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,ImVec4(34/255.f,108/255.f,192/255.f,1));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,ImVec4(44/255.f,132/255.f,232/255.f,1));
    ImGui::PushStyleColor(ImGuiCol_Text,ImVec4(238/255.f,245/255.f,1,1));
    if(ImGui::Button("Read memory timings  (will hold P0)",ImVec2(classic_px(300),0))){Command c;c.operation=Command::Operation::timings_capture_p0;
        c.timing_generation=generation;c.curve_cap_mv=state.capture_cap_mv;c.timing_allow_hold=unlocked;out.commands.push_back(c);state.capture_hold_requested=unlocked;}
    ImGui::PopStyleColor(4);ImGui::EndDisabled();
    // Released 1.9.1 read-row tooltips are unwrapped texts with hard line
    // breaks; this build's sentences say what its blue read really does.
    tip(std::string("Puts the card in its top memory band and reads the\ntiming registers there - the only state whose timings\nmean anything.\n\n")+
        (unlocked?"With controls unlocked it uses current P0 first, then\na temporary P0 hold or the optional CUDA workload,\n"
            "and releases only this capture's own hold afterwards.":"With controls locked it uses current P0 or the\n"
            "optional CUDA workload; no tuning hold is taken.")+
        "\n\nThe hold's V/F cap is under 'nvtune helper and\ncapture options' below.",0);
    ImGui::SameLine(0,gap);
    ImGui::BeginDisabled(busy||data.helper_path.empty());
    if(ImGui::Button("Re-read timings",ImVec2(classic_px(150),0))){Command c{};c.operation=Command::Operation::timings_read;out.commands.push_back(c);state.read_requested=true;}
    ImGui::EndDisabled();
    tip("Reads the registers again, right now, in whatever\nstate the card happens to be in, and files the result\nunder that memory clock.\n\n"
        "A SANITY CHECK, not the way to get a reading: on an\nidle card this returns idle timings, which say nothing\n"
        "about performance. Use the blue button for a reading\nworth having, then this to confirm it is stable.\n\n"
        "It is also how you capture a SECOND state to prove the\ndecode: read at P0, let the card idle, re-read. The\n"
        "comparison below then shows each field's cycle ratio\nbeside the clock ratio.\n\nNothing here writes to the GPU.",0);
    ImGui::SameLine(0,gap);
    // Released 1.9.1 timings_auto_toggled logs every toggle, untagged.
    if(ImGui::Checkbox("Auto-capture at P0",&state.classic_auto_capture))
        out.classic_log.push_back({std::string("timings: auto-capture at P0 ")+(state.classic_auto_capture?"armed":"off"),std::nullopt});
    tip("Captures the first time the card reaches its top\nmemory state, and again on each re-entry. Start a\n"
        "game and come back: a valid capture will be waiting.\n\nIt reads. It never writes.",0);
}
// Native-only helper setup, kept reachable in Classic.
void draw_helper_options(TimingsViewState& state,const TimingViewData& data,bool worker_busy,bool busy,bool unlocked,TimingsViewOutput& out){
    if(!ImGui::CollapsingHeader("nvtune helper and capture options"))return;
    if(!data.helper_path.empty()){const auto path=utf8(data.helper_path);wrapped(kDim,"Helper: "+path);}
    if(!state.helper_registration_message.empty())wrapped(kDim,state.helper_registration_message);
    ImGui::BeginDisabled(busy);if(ImGui::Button("Locate nvtune..."))out.choose_helper=true;ImGui::EndDisabled();ImGui::SameLine();
    ImGui::BeginDisabled(worker_busy||!state.helper_registration_present);if(ImGui::Button("Forget nvtune location"))out.forget_helper=true;ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(worker_busy);if(ImGui::Button("Test signing setup..."))out.test_signing_setup=true;ImGui::EndDisabled();
    ImGui::BeginDisabled(!unlocked||!can_capture_timing(&data,busy));
    ImGui::SetNextItemWidth(classic_px(110));ImGui::InputDouble("P0 capture V/F cap (mV)",&state.capture_cap_mv,0,0,"%.2f");
    ImGui::EndDisabled();
    wrapped(kDim,unlocked?"The blue read uses current P0 first, then a temporary hold or optional CUDA workload. Releases only this capture's hold.":
        "The blue read uses current P0 or an optional CUDA workload. Tuning holds remain disabled.");
    // Native note, moved here from the Auto-capture tooltip (released 1.9.1 has no such sentence).
    wrapped(kDim,"Auto-capture at P0 uses ordinary telemetry only; Pause and minimized refresh settings still apply.");
}
void draw_classic_page(TimingsViewState& state,const TimingViewData* data,std::uint64_t generation,bool worker_busy,bool busy,bool unlocked,
                       const TimingLiveClock* live,TimingsViewOutput& out){
    // It is taller than the page, so the page scrolls as released 1.9.1 does.
    const float page_h=classic_timing_share_height();
    const bool no_exe=!data||(data->helper_path.empty()&&state.helper_missing);
    const bool driver_stopped=state.helper_driver_running&&!*state.helper_driver_running&&!(data&&data->snapshot.ok);
    const bool setup=no_exe||driver_stopped;
    // tim_busy: what the capture is doing, else the live clock. Released 1.9.1
    // stands it, tim_state and tim_induce_note above the setup screen too.
    // The busy text keeps the colour the live line last set (ACCENT before one).
    const ImVec4 held=!state.live_at_p0?kAccent:*state.live_at_p0?kGood:kDim;
    const bool capturing=data&&data->capture_running;
    if(capturing)text_line(held,state.capture_hold_requested?"  reading at P0…":"  inducing GPU load…");
    else if(state.read_in_flight)text_line(held,"  reading…");
    else if(!setup&&data->helper_path.empty())text_line(kAccent,"  locating nvtune…");
    else{const TimingLiveClock none;const auto& clock=live?*live:none;const bool at_p0=timing_live_at_p0(clock);
        if(clock.memory_mhz)state.live_at_p0=at_p0;
        text_line(at_p0?kGood:kDim,timing_live_status(clock,state.captures,state.classic_auto_capture));}
    if(capturing){ImGui::SameLine();if(ImGui::SmallButton("Cancel timing capture"))out.cancel_capture=true;}
    // Released 1.9.1 tim_state: the loud line for a capture outside the top
    // band (amber when the band is unknown, red when idle), hidden for an
    // in-band capture and an empty line before any capture. A failed read
    // empties it without showing it again.
    if(!data||!data->snapshot.ok){if(!state.state_line_hidden)text_line(kWarn,"");}
    else if(const auto band=timing_perf_band(data->snapshot);!band||!*band)
        frame_wrapped(band?kBad:kWarn,"⚠ "+timing_state_headline(data->snapshot),page_wrap());
    // Released 1.9.1 tim_induce_note: where the blue read landed, until the next capture.
    if(data&&!data->capture_note.empty())frame_wrapped(kWarn,data->capture_note,page_wrap());
    if(setup){draw_setup_screen(state,data,worker_busy,busy,no_exe,out);return;}
    ImGui::Dummy(ImVec2(0,classic_px(4)));
    frame_wrapped(kDim,kReadOnly,page_wrap());
    // tw_backup: state.message, unless it is the profile result shown above.
    const std::string backup=state.message==state.profile_status?std::string():state.message;
    // Released 1.9.1 tim_hdr_h / tw_block_h: estimated from the text, not the
    // drawn widgets, and that estimate is what relayout() splits by.
    // The width the texts below are drawn at (a floated page can be narrower).
    const float lh=ImGui::GetTextLineHeight(),wrapw=std::max(1.0f,std::min(page_wrap(),ImGui::GetContentRegionAvail().x));
    const auto text_h=[&](const std::string& t){return t.empty()?0.0f:ImGui::CalcTextSize(t.c_str(),nullptr,false,wrapw).y;};
    float hdr_h=lh*3+classic_px(28);
    {std::string warn;for(const auto& w:timing_header(data->snapshot).warnings)warn+=(warn.empty()?"":"\n")+("⚠ "+w);hdr_h=std::floor(hdr_h+text_h(warn));}
    float tw_h=lh+text_h(kWriteWarn)+text_h("Edit the 'new value' column in the table below. A cell is green while it matches the register and red "
        "once it does not; only red rows are written.")+text_h(classic_timing_plan(state,*data).text);
    tw_h+=text_h(classic_timing_result(state,*data))+text_h(backup);
    tw_h+=2*(lh+classic_px(12))+classic_px(20)+lh;
    draw_header_box(data->snapshot,hdr_h);
    draw_edit_block(state,*data,busy,unlocked,out);
    ImGui::Dummy(ImVec2(0,classic_px(6)));rule();ImGui::Dummy(ImVec2(0,classic_px(6)));
    draw_read_row(state,*data,generation,busy,unlocked,out);
    // tw_result / tw_backup: always two lines, empty at rest. A read's own
    // result is already the header's clock line (released 1.9.1 logs it).
    // tw_result is GOOD for an action that succeeded, BAD for one that did not.
    frame_wrapped(tone_colour(classic_timing_result_tone(*data)),classic_timing_result(state,*data),page_wrap());frame_wrapped(kDim,backup,page_wrap());
    if(data->capture_method==TimingCaptureMethod::cuda)ImGui::TextDisabled("Capture method: CUDA workload; actual P-state and band determine qualification.");
    if(data->capture_hold_pending||data->capture_workload_pending){
        if(data->capture_workload_pending)wrapped(kWarn,"CUDA workload exit is unconfirmed. Retry cleanup before another capture.");
        ImGui::BeginDisabled(worker_busy);
        if(ImGui::Button("Release timing capture resources")){Command c;c.operation=Command::Operation::timings_release_capture_hold;out.commands.push_back(c);}
        ImGui::EndDisabled();
    }
    ImGui::Dummy(ImVec2(0,classic_px(4)));rule();ImGui::Dummy(ImVec2(0,classic_px(4)));
    const auto diagnostics=timing_diagnostics(data->snapshot,data->snapshot.diagnostic_evidence,true);
    const auto divergence=data->snapshot.ok?timing_divergence(data->snapshot,diagnostics):std::vector<TimingDivergence>{};
    // Released 1.9.1 relayout(): rest, then the decoded / compared split.
    const float rest=std::max(classic_px(320),page_h-hdr_h-tw_h-classic_px(104));
    const float div_h=divergence.empty()?0.0f:std::max(classic_px(120),std::floor(rest*0.20f));
    const float tim_h=std::max(classic_px(180),std::floor((rest-div_h)*0.56f));
    const float cmp_h=std::max(classic_px(150),rest-div_h-tim_h-classic_px(30));
    draw_decoded_panel(state,*data,diagnostics,divergence.size(),tim_h);
    ImGui::Dummy(ImVec2(0,classic_px(4)));
    draw_comparison(state,data->snapshot,cmp_h);
    if(!divergence.empty()){ImGui::Dummy(ImVec2(0,classic_px(4)));draw_divergence(divergence,div_h);}
    // Native-only paths, under collapsed headers below the released page.
    ImGui::Dummy(ImVec2(0,classic_px(4)));
    if(data->capture_diagnostic)draw_raw_diagnostic(*data->capture_diagnostic);
    // The table has no row selection in the released layout, so a combo selects.
    if(ImGui::CollapsingHeader("Single-field preview and apply")){
        ImGui::SetNextItemWidth(classic_px(160));
        if(ImGui::BeginCombo("Field",state.field.empty()?"--":state.field.c_str())){
            for(const auto& f:data->snapshot.fields)if(ImGui::Selectable(f.name.c_str(),state.field==f.name))select_timing_field(state,*data,f.name);
            ImGui::EndCombo();
        }
        draw_single_field(state,*data,busy,unlocked,out);
    }
    draw_helper_options(state,*data,worker_busy,busy,unlocked,out);
}
}
TimingsViewOutput draw_timings(TimingsViewState& state,const TimingViewData* data,std::uint64_t generation,bool worker_busy,bool unlocked,bool connected,bool classic,
                               const TimingLiveClock* live) {
    const bool busy=worker_busy||!connected;
    sync_timings_view(state,data,generation); TimingsViewOutput out;
    if(classic){draw_classic_page(state,data,generation,worker_busy,busy,unlocked,live,out);return out;}
    ImGui::TextUnformatted("Memory timings");
    ImGui::TextWrapped("nvtune is a separate helper. Timing writes affect the current memory band; capture and a stable top band are required.");
    ImGui::BeginDisabled(busy); if(ImGui::Button("Locate nvtune..."))out.choose_helper=true;
    ImGui::SameLine(); ImGui::BeginDisabled(!data||data->helper_path.empty());
    if(ImGui::Button("Read timings")){Command c{};c.operation=Command::Operation::timings_read;out.commands.push_back(c);} ImGui::EndDisabled(); ImGui::EndDisabled();
    ImGui::BeginDisabled(worker_busy||!state.helper_registration_present);if(ImGui::Button("Forget nvtune location"))out.forget_helper=true;ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(worker_busy);if(ImGui::Button("Test signing setup..."))out.test_signing_setup=true;ImGui::EndDisabled();
    if(!state.helper_registration_message.empty())ImGui::TextWrapped("%s",state.helper_registration_message.c_str());
    if(!data){ImGui::TextDisabled("No helper configured");return out;}
    ImGui::BeginDisabled(!can_capture_timing(data,busy));
    ImGui::BeginDisabled(!unlocked);
    ImGui::SetNextItemWidth(110);ImGui::InputDouble("P0 capture V/F cap (mV)",&state.capture_cap_mv,0,0,"%.2f");
    ImGui::EndDisabled();
    if(ImGui::Button("Capture at performance band")){Command c;c.operation=Command::Operation::timings_capture_p0;c.timing_generation=generation;c.curve_cap_mv=state.capture_cap_mv;c.timing_allow_hold=unlocked;out.commands.push_back(c);}
    ImGui::EndDisabled();
    ImGui::TextWrapped(unlocked?"Uses current P0 first, then a temporary hold or optional CUDA workload. Releases only this capture's hold.":"Uses current P0 or an optional CUDA workload. Tuning holds remain disabled.");
    if(data->capture_running&&ImGui::Button("Cancel timing capture"))out.cancel_capture=true;
    if(data->capture_method==TimingCaptureMethod::cuda)ImGui::TextDisabled("Capture method: CUDA workload; actual P-state and band determine qualification.");
    if(data->capture_diagnostic)draw_raw_diagnostic(*data->capture_diagnostic);
    if(data->capture_hold_pending||data->capture_workload_pending){
        if(data->capture_workload_pending)ImGui::TextWrapped("CUDA workload exit is unconfirmed. Retry cleanup before another capture.");
        ImGui::BeginDisabled(worker_busy);
        if(ImGui::Button("Release timing capture resources")){Command c;c.operation=Command::Operation::timings_release_capture_hold;out.commands.push_back(c);}
        ImGui::EndDisabled();
    }
    ImGui::Checkbox("Auto-capture on stable P0 entry",&state.auto_capture);
    if(ImGui::IsItemHovered())ImGui::SetTooltip("Uses ordinary telemetry only; Pause and minimized refresh settings still apply. Does not acquire a hold.");
    if(!data->helper_path.empty()) {const auto path=utf8(data->helper_path); ImGui::TextWrapped("Helper: %s",path.c_str());}
    if(!data->message.empty()) ImGui::TextWrapped("%s",data->message.c_str());
    ImGui::BeginDisabled(busy||!unlocked||!data->undo_available);
    if(ImGui::Button("Undo timing write")){Command c{};c.operation=Command::Operation::timings_undo;out.commands.push_back(c);} ImGui::EndDisabled();
    if(!state.message.empty())ImGui::TextWrapped("%s",state.message.c_str());
    ImGui::BeginDisabled(busy||!unlocked||!data->initial_available);
    if(ImGui::Button("Restore captured initial")){Command c{};c.operation=Command::Operation::timings_restore_initial;out.commands.push_back(c);}ImGui::EndDisabled();
    ImGui::TextWrapped("Captured initial means this connection's first qualified capture, not factory defaults. Values originate from captured broadcast fields; units and partition agreement require independent evidence.");
    if(!data->snapshot.ok)ImGui::TextDisabled("No current timing capture. Read timings to retry; staged edits are retained.");
    ImGui::Text("Adapter %s | %s | memory %s | top band %s",data->snapshot.slot.c_str(),data->snapshot.codename.c_str(),
        data->snapshot.memory_clock_stable?"stable":"unconfirmed",data->snapshot.top_band?"yes":"not established");
    const auto diagnostics=timing_diagnostics(data->snapshot,data->snapshot.diagnostic_evidence);
    ImGui::TextWrapped("%s | %s",diagnostics.clock_status.c_str(),diagnostics.partition_status.c_str());
    if(data->snapshot.ok&&ImGui::BeginTable("timing_fields",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,220))) {
        ImGui::TableSetupColumn("Field");ImGui::TableSetupColumn("Captured value");ImGui::TableSetupColumn("Register");ImGui::TableSetupColumn("Details");ImGui::TableHeadersRow();
        for(const auto& f:data->snapshot.fields){
            ImGui::TableNextRow();ImGui::TableSetColumnIndex(0);
            if(ImGui::Selectable(f.name.c_str(),state.field==f.name,ImGuiSelectableFlags_SpanAllColumns)) select_timing_field(state,*data,f.name);
            ImGui::TableSetColumnIndex(1);
            const auto index=static_cast<size_t>(&f-data->snapshot.fields.data());
            const auto& d=diagnostics.rows[index];
            if(d.value) { if(d.nanoseconds) ImGui::Text("%u %s / %.3f ns",*d.value,d.unit.c_str(),*d.nanoseconds); else ImGui::Text("%u %s",*d.value,d.unit.c_str()); } else ImGui::TextDisabled("--");
            ImGui::TableSetColumnIndex(2);ImGui::TextUnformatted(f.reg.c_str());
            ImGui::TableSetColumnIndex(3);ImGui::TextUnformatted(f.description.c_str());
            if(!d.provenance.empty())ImGui::TextWrapped("Units: %s",d.provenance.c_str());
            for(const auto& p:d.partitions) { if(p.value) ImGui::Text("%s: %u%s",p.scope.c_str(),*p.value,p.differs?" (differs)":""); else ImGui::Text("%s: unavailable",p.scope.c_str()); }
        } ImGui::EndTable();
    }
    draw_single_field(state,*data,busy,unlocked,out);
    ImGui::SeparatorText("Staged timing fields");
    if(state.staged.empty())ImGui::TextDisabled("No staged timing fields.");
    for(size_t i=0;i<state.staged.size();++i){const auto& r=state.staged[i];ImGui::PushID(static_cast<int>(i));
        const auto* f=field(*data,r.field);const auto* w=f?word(*data,*f):nullptr;
        if(f&&w&&f->lo<=f->hi&&f->hi<32)ImGui::Text("%s: %u -> %u",r.field.c_str(),(w->value>>f->lo)&f->maximum,r.value);
        else ImGui::Text("%s: -- -> %u (refresh required)",r.field.c_str(),r.value);
        ImGui::SameLine();if(ImGui::SmallButton("Remove")){state.staged.erase(state.staged.begin()+static_cast<std::ptrdiff_t>(i));ImGui::PopID();break;}ImGui::PopID();
    }
    if(ImGui::Button("Revert staged edits"))state.staged.clear();ImGui::SameLine();
    ImGui::BeginDisabled(!can_preview_timing_batch(state,data,busy));
    if(ImGui::Button("Preview all staged")){auto request=std::make_shared<TimingEditorRequest>();request->fields=state.staged;
        Command c{};c.operation=Command::Operation::timings_preview_batch;c.timing_editor=std::move(request);out.commands.push_back(std::move(c));}ImGui::EndDisabled();ImGui::SameLine();
    ImGui::BeginDisabled(!can_apply_timing_batch(state,data,busy,unlocked));
    if(ImGui::Button("Apply all staged")){auto request=std::make_shared<TimingEditorRequest>();request->fields=state.staged;
        Command c{};c.operation=Command::Operation::timings_apply_batch;c.timing_editor=std::move(request);out.commands.push_back(std::move(c));}ImGui::EndDisabled();
    ImGui::BeginDisabled(busy||!data->snapshot.ok||!data->snapshot.top_band);if(ImGui::Button("Save timing profile..."))out.save_profile=true;ImGui::EndDisabled();ImGui::SameLine();
    ImGui::BeginDisabled(busy);if(ImGui::Button("Load timing profile..."))out.load_profile=true;ImGui::EndDisabled();
    if(data->batch_preview&&!data->batch_preview->message.empty())ImGui::TextWrapped("%s",data->batch_preview->message.c_str());
    return out;
}
}
