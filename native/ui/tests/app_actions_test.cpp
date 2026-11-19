// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless snapshots / production rendering / Windows argument parsing only.
// No process is launched and no GPU, helper or report file is opened.
#include "app_actions.h"
#include "ui_view.h"
#include "session.h"
#include "clock_monitor_model.h"
#include "imgui_internal.h"
#include "implot.h"
#include <windows.h>
#include <shellapi.h>
#include <cstdio>
#include <algorithm>
#include <limits>
#include <string>
#include <vector>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* message){if(!ok){++failures;std::printf("FAIL: %s\n",message);}}
bool has(const std::string& text,const char* value){return text.find(value)!=std::string::npos;}
SessionSnapshot fixture() {
    SessionSnapshot s;s.generation=9;s.selected_slot="0000:03:00.0";s.data.connected=true;
    std::snprintf(s.data.identity.slot,sizeof(s.data.identity.slot),"%s",s.selected_slot.c_str());
    std::snprintf(s.data.identity.uuid,sizeof(s.data.identity.uuid),"GPU-alternate-identity");
    std::snprintf(s.data.identity.name,sizeof(s.data.identity.name),"Alternate adapter");
    return s;
}
std::vector<std::wstring> arguments(const std::wstring& command) {
    int count=0;auto** values=CommandLineToArgvW(command.c_str(),&count);std::vector<std::wstring> out;
    if(values){for(int i=0;i<count;++i)out.emplace_back(values[i]);LocalFree(values);}return out;
}
void targets_and_reports() {
    auto s=fixture();std::string why;
    auto disconnected=s;disconnected.data={};
    check(bool(restart_device_target(disconnected,why,true)),"explicit selected slot permits exact PnP recovery without live GPU identity");
    check(!restart_device_target(disconnected,why,false),"PnP recovery requires process elevation evidence");
    disconnected.busy=true;check(!restart_device_target(disconnected,why,true),"PnP handoff waits for active hardware operation");
    auto target=second_window_target(s,why);
    check(bool(target) && why.empty(),"alternate adapter identity is eligible without a developer-device allowlist");
    auto report=selected_device_report(s);
    check(has(report.text,"CORE CLOCK: unavailable") && has(report.text,"Driver: unavailable")
        && has(report.text,"Power-policy diagnostics: unavailable"),"partial report identifies missing fields independently");
    check(has(report.text,"Administrator: unavailable") && has(report.text,"step=unavailable kHz; wire divisor=unavailable"),
        "uncaptured process metadata and curve geometry do not masquerade as false/zero values");
    s.data.clock_monitor=std::make_shared<ClockMonitorData>();
    s.data.i2c.family=I2cFamily::mp2888;s.data.i2c.selected=I2cRoute{4,0x2d};s.data.i2c.control_profile="qualified-recipe";
    s.data.i2c.status.controller_vout_mv=975;s.data.i2c.status.stale=true;
    report=selected_device_report(s);
    check(has(report.text,"clock reading 0: unavailable") && has(report.text,"Controller route: port 4, address 0x2d")
        && has(report.text,"recipe: qualified-recipe") && has(report.text,"975 mV [stale]"),
        "clock evidence absence and exact I2C route/recipe/stale telemetry are explicit");
    s.data.controls[0].availability=Availability::transient;s.data.controls[0].current=12.5;s.data.controls[0].unit="MHz";
    s.data.telemetry_valid=true;s.data.telemetry.present=DC_T_CORE|DC_T_TEMP_EDGE;s.data.telemetry.core=0;
    s.telemetry_stale_mask=DC_T_CORE;report=selected_device_report(s);
    check(has(report.text,"configured=12.5") && has(report.text,"transient / last confirmed request"),"captured configured request is labelled separately from physical readings");
    check(has(report.text,"CORE CLOCK: 0 MHz driver [stale") && has(report.text,"HOTSPOT: unavailable"),"present zero, stale reading and missing unrelated telemetry remain distinct");
    const auto captured=report.target;++s.generation;
    check(!app_target_matches(captured,s),"generation change invalidates a dialog-bound action target");
    --s.generation;s.selected_slot="0000:04:00.0";check(!app_target_matches(captured,s),"selected slot change invalidates target");
    check(!second_window_target(s,why),"identity/selection slot mismatch cannot open another GPU");
    s=fixture();s.data.identity.uuid[0]=0;check(!second_window_target(s,why),"missing UUID does not fall back to an arbitrary adapter");
    s=fixture();s.data.connected=false;check(!second_window_target(s,why),"unavailable adapter cannot launch a live target");
    s=fixture();s.opening=true;check(!second_window_target(s,why),"opening session is not a qualified launch target");
    s=fixture();std::snprintf(s.data.identity.pairing_error,sizeof(s.data.identity.pairing_error),"pairing unresolved");
    check(!second_window_target(s,why),"unpaired API handles cannot launch a claimed verified target");
    // 1.9.1 open_gpu_window on any enumerated card: another card goes with its
    // enumerated slot and UUID, checked again at launch against the same session.
    s=fixture();s.devices={{"0000:03:00.0","Alternate adapter","GPU-alternate-identity"},{"0000:0a:00.0","Other card","GPU-other-card"},{"0000:0b:00.0","No UUID card",""}};
    auto other=second_window_card_target(s,"0000:0a:00.0",why);
    check(other && other->slot=="0000:0a:00.0" && other->uuid=="GPU-other-card" && other->generation==s.generation && why.empty(),
        "another enumerated card is a target with its own slot and UUID");
    check(other && second_window_launchable(*other,s,why),"an unchanged enumeration launches that card");
    auto busy=s;busy.busy=true;busy.data.connected=false;
    check(second_window_card_target(busy,"0000:0a:00.0",why) && !second_window_card_target(busy,"0000:03:00.0",why),
        "the selected session's busy or disconnected state holds back only the selected card");
    check(!second_window_card_target(s,"0000:0b:00.0",why) && !why.empty(),"a card enumerated without a UUID is not a target");
    check(!second_window_card_target(s,"0000:0c:00.0",why) && !second_window_card_target(s,"",why),"an unlisted or empty slot is not a target");
    auto changed=s;changed.devices[1].uuid="GPU-replaced-card";
    check(other && !second_window_launchable(*other,changed,why),"a different UUID at the slot by launch time cancels the launch");
    changed=s;changed.devices.pop_back();changed.devices.pop_back();
    check(other && !second_window_launchable(*other,changed,why),"a card gone from the enumeration by launch time cancels the launch");
    changed=s;++changed.generation;
    check(other && !second_window_launchable(*other,changed,why),"a new session cancels a launch picked in the old one");
    const auto selected=second_window_card_target(s,"0000:03:00.0",why);
    check(selected && selected->uuid=="GPU-alternate-identity" && second_window_launchable(*selected,s,why),"the selected card keeps its verified target");
    changed=s;std::snprintf(changed.data.identity.pairing_error,sizeof(changed.data.identity.pairing_error),"pairing unresolved");
    check(selected && !second_window_launchable(*selected,changed,why),"the selected card still needs its verified identity at launch");
    AppActionsState pending;pending.second_window=captured;pending.save_report_requested=pending.exit_requested=true;
    pending.report=report;cancel_pending_app_actions(pending);
    check(!pending.second_window && !pending.save_report_requested && !pending.exit_requested && pending.report,
        "shutdown cancels launch/dialog intents while retaining captured report contents");
}
void quoting() {
    const std::wstring exe=L"C:\\Program Files\\Druta & tools\\druta-ui.exe";
    const std::vector<std::wstring> values{L"",L"plain",L"spaces here",L"quote\"here",L"C:\\trailing\\",L"a\\\\\"b",L"& %PATH% $(text)"};
    std::wstring command=quote_windows_argument(exe);for(const auto& value:values)command+=L" "+quote_windows_argument(value);
    auto parsed=arguments(command);check(parsed.size()==values.size()+1,"quoted arguments retain original count");
    if(parsed.size()==values.size()+1)for(size_t i=0;i<values.size();++i)check(parsed[i+1]==values[i],"Win32 parser round-trips spaces, quotes, backslashes and shell-looking text");
    const auto s=fixture();const auto target=app_target(s);
    parsed=arguments(second_window_command(exe,L"C:\\Program Files\\Druta\\drutacore.dll",target,true));
    check(parsed.size()==8 && parsed[0]==exe && parsed[1]==L"--gpu" && parsed[2]==L"0000:03:00.0"
        && parsed[3]==L"--expect-uuid" && parsed[4]==L"GPU-alternate-identity" && parsed[5]==L"--core" && parsed[7]==L"--classic",
        "second process receives explicit slot, expected UUID, exact core and presentation only");
    parsed=arguments(second_window_command(exe,L"core.dll",target,false));
    check(parsed.size()==8 && parsed[7]==L"--focused","a second Focused window keeps its skin");
    bool refused=false;try{second_window_command(exe,L"core",{9,"slot",""},false);}catch(...){refused=true;}
    check(refused,"launch builder refuses an absent target UUID");
}
std::string frame(ViewState& v) {
    ImGui::GetIO().DisplaySize={1700,1100};ImGui::NewFrame();ImGui::LogToBuffer(0);draw(v);
    std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::Render();return text;
}
void rendering() {
    auto live=std::make_shared<SessionSnapshot>(fixture());ViewState v;v.presentation=Presentation::focused;v.preview=false;v.live=live;v.page=Page::monitor;
    frame(v);v.drafts[0].value=37.5;v.drafts[0].dirty=true;
    auto* root=ImGui::FindWindowByName("Druta workspace");
    ImGui::ActivateItemByID(ImHashStr("Device",0,root->GetID("##MenuBar")));frame(v);
    auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
    check(!stack.empty() && stack.back().Window,"Device menu opens on the same page");
    if(!stack.empty() && stack.back().Window) {
        ImGui::ActivateItemByID(stack.back().Window->GetID("Refresh capabilities"));frame(v);
        check(v.refresh_requested && v.page==Page::monitor && v.drafts[0].dirty && v.drafts[0].value==37.5,
            "capability refresh preserves page, selection and unfinished draft");
    }
    v.app_actions.report=selected_device_report(*live);v.app_actions.report_open=true;
    v.app_actions.about_open=v.app_actions.shortcuts_open=v.app_actions.licences_open=true;
    v.app_actions.notices=load_bundled_notices(L"");auto text=frame(v);
    check(has(text,"Alternate adapter") && has(text,"GNU General Public License") && has(text,"Driver: unavailable"),
        "supporting windows render actual selected report, About and uncertainty labels");
    check(v.commands.empty() && v.drafts[0].value==37.5 && v.page==Page::monitor,"support windows never submit tuning or change existing page/draft");
    ++live->generation;text=frame(v);
    check(has(text,"selected adapter or session changed"),"captured report visibly distinguishes an earlier selected session");
    auto* report=ImGui::FindWindowByName("Selected-device report###druta_device_report");
    if(report){ImGui::ActivateItemByID(report->GetID("Save report..."));frame(v);}
    check(!v.app_actions.save_report_requested,"save action remains disabled after adapter/session changes");
}
ImGuiWindow* open_menu(ViewState& v,const char* label){
    auto* workspace=ImGui::FindWindowByName("Druta workspace");
    if(!workspace)return nullptr;
    ImGui::ActivateItemByID(ImHashStr(label,0,workspace->GetID("##MenuBar")));frame(v);
    const auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
    return stack.empty()?nullptr:stack.back().Window;
}
void close_menus(ViewState& v){ImGui::ClosePopupsExceptModals();frame(v);}
int menu_deflatten_calls=0;
dc_status DC_CALL menu_deflatten(const dc_vf_point* p,size_t count,const dc_vfp_num*,const dc_vfp_num*,
    const dc_vfp_num*,const dc_vfp_num*,dc_vfp_deflatten* out){
    if(count!=2)return DC_E_ARGUMENT;
    ++menu_deflatten_calls;out->change_count=1;out->changes[0].pos=1;out->changes[0].idx=p[1].idx;
    out->changes[0].delta_khz.kind=DC_VFP_INT;out->changes[0].delta_khz.i=p[1].delta_khz+15000;return DC_OK;
}
dc_status DC_CALL menu_predict(const dc_vf_point*,size_t count,const dc_vfp_num* values,const dc_vfp_num*,int64_t* predicted,uint32_t* order,size_t capacity){
    if(count!=capacity)return DC_E_ARGUMENT;
    for(size_t i=0;i<count;++i){predicted[i]=values[i].i;order[i]=static_cast<uint32_t>(i);}
    return DC_OK;
}
// Released 1.9.1 headroom_changed and restore_raised_limits_now beyond one click:
// the session's busy state, the other place the setting is turned off, Focused,
// and the button's note once the restore's result is in. Expects a Classic view
// whose Clocks menu shows the headroom setting.
void classic_headroom_restore(ViewState& v,SessionSnapshot& live){
    const char* setting="Keep headroom above a held point (strongly recommended)";
    const auto toggle=[&]{if(auto* clocks=open_menu(v,"Clocks")){ImGui::ActivateItemByID(clocks->GetID(setting));frame(v);}close_menus(v);};
    const auto restores=[&]{return std::count_if(v.commands.begin(),v.commands.end(),[](const Command& c){return c.operation==Command::Operation::restore_raised_limits;});};
    v.commands.clear();v.hold_headroom=true;live.data.tuning={};live.data.tuning.owned=live.data.tuning.confirmed=live.data.tuning.headroom=true;
    frame(v);
    // 1.9.1 restores at the untick; the session refuses a command while another
    // runs, so the restore waits for that command to end, then goes once.
    live.busy=true;toggle();
    check(!v.hold_headroom&&v.commands.empty(),"unticking while another command runs queues nothing the session would refuse");
    frame(v);check(v.commands.empty(),"the owed restore waits while the command runs");
    live.busy=false;frame(v);
    check(restores()==1,"the owed restore is queued once the running command has ended");
    v.commands.clear();frame(v);check(v.commands.empty(),"the owed restore is queued only once");
    v.hold_headroom=true;frame(v);
    live.busy=true;toggle();toggle();live.busy=false;frame(v);
    check(v.hold_headroom&&v.commands.empty(),"ticking the setting again before the restore went drops it");
    live.busy=true;toggle();live.data.tuning.headroom=false;live.busy=false;frame(v);
    check(v.commands.empty(),"a running command that left nothing raised leaves nothing to restore");
    v.hold_headroom=true;live.data.tuning.headroom=true;frame(v);
    live.busy=true;toggle();++live.generation;live.busy=false;frame(v);
    check(v.commands.empty(),"a new session drops the owed restore");
    // The Classic editor's Hold headroom is the same setting: turning it off
    // there restores too. Focused keeps its own behaviour and queues nothing.
    v.hold_headroom=true;frame(v);v.hold_headroom=false;frame(v);
    check(restores()==1,"the setting turned off outside the Clocks menu queues the restore");
    v.commands.clear();v.hold_headroom=true;frame(v);
    v.presentation=Presentation::focused;frame(v);v.hold_headroom=false;frame(v);frame(v);
    check(v.commands.empty(),"Focused queues no restore when its setting is turned off");
    v.hold_headroom=true;frame(v);v.presentation=Presentation::classic;frame(v);
    // The button's note: 1.9.1 logs it untagged after the restore's own line,
    // only when a raise came off and the hold is still in force.
    const auto result=[&](bool ok,bool raised_after,bool held_after){
        live.completed_actions++;live.last_action_operation=Command::Operation::restore_raised_limits;live.last_action_ok=ok;
        const auto id=live.log.empty()?1:live.log.back().id+1;
        live.log.push_back({id,ok?"headroom: NVVDD: reliability restored after the hold":"headroom: headroom restore not verified",ok});
        live.last_action_log_id=id;live.data.tuning.headroom=raised_after;live.data.tuning.owned=held_after;
        return id;
    };
    const auto click=[&]{if(auto* clocks=open_menu(v,"Clocks")){ImGui::ActivateItemByID(clocks->GetID("Restore raised limits now"));frame(v);}close_menus(v);v.commands.clear();};
    const auto notes=[&]{return std::count_if(v.classic_log_lines.begin(),v.classic_log_lines.end(),[](const auto& l){return l.text==classic_raised_restored_under_hold_note;});};
    v.classic_log_lines.clear();live.data.tuning.owned=live.data.tuning.headroom=true;
    click();frame(v);check(notes()==0,"the note waits for the restore's result");
    const auto restored=result(true,false,true);frame(v);
    const auto note=std::find_if(v.classic_log_lines.begin(),v.classic_log_lines.end(),[](const auto& l){return l.text==classic_raised_restored_under_hold_note;});
    check(notes()==1&&note!=v.classic_log_lines.end()&&note->after_log_id==restored&&!note->ok.has_value(),
        "a raise restored under a hold adds 1.9.1's note as its own untagged line after the restore's");
    frame(v);check(notes()==1,"the note is added once");
    click();result(true,false,true);frame(v);
    check(notes()==1,"a click with nothing raised adds no note");
    live.data.tuning.headroom=true;click();result(false,true,true);frame(v);
    check(notes()==1,"a failed restore adds no note");
    click();result(true,false,false);frame(v);
    check(notes()==1,"no note once the hold has ended");
    v.commands.clear();live.data.tuning={};v.hold_headroom=true;frame(v);
}
// Released 1.9.1 menu bar: File (Exit), Device, Profiles (Save, Load, Undo last
// write), Clocks (Limited de-flatten first), Help. Classic moves the native
// header row's profile, refresh, pause and presentation controls into it.
void classic_menus(){
    auto live=std::make_shared<SessionSnapshot>(fixture());
    live->data.curve.push_back({0,750,1400,0});live->data.curve.push_back({1,900,1890,0});
    live->curve_revision=1;live->data.curve_freq_div=1;live->data.curve_step_khz=15000;
    live->data.curve_max_abs_delta_khz=1000000;live->data.curve_model_max_rise_khz=45000;live->data.curve_writable=true;
    ViewState v;v.preview=false;v.live=live;v.controls_unlocked=false;
    check(v.presentation==Presentation::classic,"a new view starts in Verbose");
    v.planner_api.deflatten=menu_deflatten;v.planner_api.predict=menu_predict;
    auto text=frame(v);
    check(has(text,"Skin")&&has(text,"Profiles")&&!has(text,"Save profile...")&&!has(text,"Always refresh")&&!has(text,"Session-only tuning"),
        "Verbose has a top-level Skin menu beside its regular menus");
    v.drafts[0].value=37.5;v.drafts[0].dirty=true;
    auto* skin=open_menu(v,"Skin");
    check(skin!=nullptr,"the Skin menu opens directly from the Verbose bar");
    if(skin){ImGui::ActivateItemByID(skin->GetID("Focused"));frame(v);}
    check(v.presentation==Presentation::focused&&v.drafts[0].dirty&&v.drafts[0].value==37.5
        &&v.commands.empty()&&!v.select_slot,"the Skin menu changes layout and preserves the session and edits");
    close_menus(v);v.presentation=Presentation::classic;
    ViewState focused;focused.preview=false;focused.live=live;focused.presentation=Presentation::focused;
    const auto focused_text=frame(focused);
    check(has(focused_text,"Save profile...")&&has(focused_text,"Always refresh")&&has(focused_text,"Session-only tuning")&&!has(focused_text,"Profiles"),
        "Focused keeps its header controls, footer and menu bar unchanged");
    frame(v);
    auto* profiles=open_menu(v,"Profiles");
    check(profiles!=nullptr,"Classic Profiles menu opens");
    if(profiles){
        ImGui::ActivateItemByID(profiles->GetID("Load profile..."));frame(v);
        ImGui::ActivateItemByID(profiles->GetID("Undo last write"));frame(v);
        check(!v.load_profile_requested&&v.commands.empty(),"locked or empty-history Profiles entries stay inert");
    }
    close_menus(v);
    v.controls_unlocked=true;live->data.undo_available=true;
    profiles=open_menu(v,"Profiles");
    if(profiles){ImGui::ActivateItemByID(profiles->GetID("Save profile..."));frame(v);}
    check(v.save_profile_requested&&v.commands.empty(),"Profiles > Save profile requests the save dialog without writing");
    close_menus(v);profiles=open_menu(v,"Profiles");
    if(profiles){ImGui::ActivateItemByID(profiles->GetID("Load profile..."));frame(v);}
    check(v.load_profile_requested,"Profiles > Load profile is reachable once unlocked");
    close_menus(v);profiles=open_menu(v,"Profiles");
    if(profiles){ImGui::ActivateItemByID(profiles->GetID("Undo last write"));frame(v);}
    check(v.commands.size()==1&&v.commands[0].operation==Command::Operation::undo,"Profiles > Undo last write queues the existing explicit Undo");
    v.commands.clear();close_menus(v);
    auto* file=open_menu(v,"File");
    if(file){ImGui::ActivateItemByID(file->GetID("Always refresh"));frame(v);}
    close_menus(v);file=open_menu(v,"File");
    if(file){ImGui::ActivateItemByID(file->GetID("Pause"));frame(v);}
    check(v.always_refresh&&v.pause_requested&&v.presentation==Presentation::classic,"File keeps Always refresh and Pause reachable in Classic");
    close_menus(v);
    auto* clocks=open_menu(v,"Clocks");
    check(clocks!=nullptr,"Classic Clocks menu opens");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Limited de-flatten \xE2\x89\xA4 cap"));frame(v);}
    check(menu_deflatten_calls==1&&v.curve_draft.dirty()&&v.commands.empty()&&!v.limited_deflatten_requested,
        "Clocks > Limited de-flatten stages the limited plan locally and writes nothing");
    close_menus(v);v.revert_draft();v.curve_draft.revert();
    v.planner_api.predict=nullptr;clocks=open_menu(v,"Clocks");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Limited de-flatten \xE2\x89\xA4 cap"));frame(v);}
    check(menu_deflatten_calls==1&&!v.limited_deflatten_requested,"Limited de-flatten stays inert without a planner");
    close_menus(v);
    // 1.9.1 clamps the headroom margin to [MIN_MARGIN_MV, MAX_MARGIN_MV] on
    // entry (a step or a typed value), and a non-finite value gives the default.
    v.headroom_margin_mv=500;clocks=open_menu(v,"Clocks");frame(v);
    check(v.headroom_margin_mv==500,"opening the Clocks menu does not rewrite the margin");
    if(clocks){
        const auto margin=clocks->GetID("margin mV");
        ImGui::ActivateItemByID(ImHashStr("+",0,margin));frame(v);
        check(v.headroom_margin_mv==100,"stepping the margin up clamps it to 1.9.1's MAX_MARGIN_MV");
        v.headroom_margin_mv=10;frame(v);ImGui::ActivateItemByID(ImHashStr("-",0,margin));frame(v);
        check(v.headroom_margin_mv==6.25,"stepping the margin down clamps it to 1.9.1's MIN_MARGIN_MV");
        v.headroom_margin_mv=50;frame(v);
        auto& io=ImGui::GetIO();ImGui::ActivateItemByID(margin);frame(v);
        io.AddInputCharactersUTF8("3");frame(v);
        check(v.headroom_margin_mv==50,"typing stays free until the entry is made");
        io.AddKeyEvent(ImGuiKey_Enter,true);frame(v);io.AddKeyEvent(ImGuiKey_Enter,false);frame(v);
        check(v.headroom_margin_mv==6.25,"a typed margin below the band is clamped on entry");
        ImGui::ActivateItemByID(margin);frame(v);io.AddInputCharactersUTF8("37.5");frame(v);
        io.AddKeyEvent(ImGuiKey_Enter,true);frame(v);io.AddKeyEvent(ImGuiKey_Enter,false);frame(v);
        check(v.headroom_margin_mv==37.5,"a typed margin inside the band is kept");
    }
    close_menus(v);
    check(clamp_headroom_margin_mv(std::numeric_limits<double>::quiet_NaN())==25&&clamp_headroom_margin_mv(std::numeric_limits<double>::infinity())==25
        &&clamp_headroom_margin_mv(-3)==6.25&&clamp_headroom_margin_mv(500)==100,"1.9.1 clamp_margin: band and default");
    // VOLTAGE-LIMIT HEADROOM: the core's architecture answer picks 1.9.1's three states.
    text=frame(v);clocks=open_menu(v,"Clocks");text=frame(v);
    check(has(text,"GPU architecture not read yet")&&has(text,"Keep headroom above a held point")&&has(text,"Restore raised limits now"),
        "an unread architecture warns and keeps the setting");
    close_menus(v);live->data.hold_headroom_architecture=false;clocks=open_menu(v,"Clocks");text=frame(v);
    check(has(text,"Not available: Druta cannot change this GPU")&&!has(text,"Keep headroom above a held point")
        &&!has(text,"margin mV")&&!has(text,"Restore raised limits now")&&has(text,"VOLTAGE-LIMIT HEADROOM"),
        "a generation without rail-limit control gets the note instead of the setting");
    close_menus(v);live->data.hold_headroom_architecture=true;clocks=open_menu(v,"Clocks");text=frame(v);
    check(!has(text,"GPU architecture not read yet")&&!has(text,"Not available: Druta")&&has(text,"25 mV is a fixed safe margin for every card")
        &&has(text,"Restore raised limits now"),"a supported generation shows the setting and 1.9.1's full note");
    // Restore raised limits now: queued on click, locked or not (1.9.1 does not
    // gate it); the core reports when nothing is raised. Unticking restores only
    // while a raise is in force.
    v.commands.clear();v.controls_unlocked=false;
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Restore raised limits now"));frame(v);}
    check(v.commands.size()==1&&v.commands[0].operation==Command::Operation::restore_raised_limits,
        "Restore raised limits now queues the explicit restore");
    {
        const auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
        check(clocks&&!stack.empty()&&stack.back().Window==clocks,"the Clocks menu stays open after the restore button, as a Dear PyGui button leaves it");
    }
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Restore raised limits now"));frame(v);}
    check(v.commands.size()==1,"a second click before the queue drains adds no second restore");
    v.commands.clear();live->busy=true;
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Restore raised limits now"));frame(v);}
    check(v.commands.empty(),"the restore button is inert while another command runs");
    live->busy=false;close_menus(v);
    clocks=open_menu(v,"Clocks");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Keep headroom above a held point (strongly recommended)"));frame(v);}
    check(!v.hold_headroom&&v.commands.empty(),"unticking with nothing raised queues nothing");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Keep headroom above a held point (strongly recommended)"));frame(v);}
    check(v.hold_headroom&&v.commands.empty(),"ticking the setting writes nothing");
    live->data.tuning.owned=live->data.tuning.confirmed=live->data.tuning.headroom=true;
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Keep headroom above a held point (strongly recommended)"));frame(v);}
    check(!v.hold_headroom&&v.commands.size()==1&&v.commands[0].operation==Command::Operation::restore_raised_limits,
        "unticking while a hold's raise is in force queues the restore");
    v.commands.clear();frame(v);
    check(v.commands.empty(),"one untick queues one restore");
    close_menus(v);
    classic_headroom_restore(v,*live);
    v.commands.clear();live->data.tuning={};v.hold_headroom=true;frame(v);
    // GPU CLOCK LOCK inline: the current adapter's bounds, unlock gate, and the
    // same frequency-lock commands as the window.
    auto& clock=live->data.frequency;
    clock.architecture=7;clock.minimum_mhz=345;clock.maximum_mhz=2205;clock.nvml_capable=clock.tables_current=true;clock.current_known=true;
    v.controls_unlocked=false;v.commands.clear();
    text=frame(v);clocks=open_menu(v,"Clocks");text=frame(v);
    check(has(text,"GPU CLOCK LOCK")&&has(text,"345-2205 MHz lockable")&&has(text,"min MHz")&&has(text,"max MHz"),
        "Clocks shows the adapter's lockable range and the lock inputs inline");
    check(has(text,"the result is one line in the Control tab log.")&&!has(text,"810 MHz"),
        "the inline lock's note carries 1.9.1's log sentence without the one-card memory clock");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Lock"));frame(v);ImGui::ActivateItemByID(clocks->GetID("Lock max"));frame(v);}
    check(v.commands.empty(),"inline clock lock stays inert while controls are locked");
    close_menus(v);v.controls_unlocked=true;
    clocks=open_menu(v,"Clocks");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Release"));frame(v);}
    check(v.commands.empty(),"Release stays inert with no owned clock request");
    close_menus(v);clocks=open_menu(v,"Clocks");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Lock"));frame(v);}
    check(v.commands.size()==1&&v.commands[0].operation==Command::Operation::frequency_lock_apply
        &&v.commands[0].frequency_min_mhz==345&&v.commands[0].frequency_max_mhz==2205,"Clocks > Lock queues the reported range as a frequency-lock apply");
    v.commands.clear();close_menus(v);clocks=open_menu(v,"Clocks");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Lock max"));frame(v);}
    check(v.commands.size()==1&&v.commands[0].operation==Command::Operation::frequency_lock_max,"Clocks > Lock max queues the existing lock-max command");
    v.commands.clear();close_menus(v);
    clock.architecture=4;clock.minimum_mhz.reset();clock.maximum_mhz.reset();clock.nvml_capable=clock.tables_current=false;
    clocks=open_menu(v,"Clocks");text=frame(v);
    check(has(text,"unavailable on Pascal")&&!has(text,"MHz lockable"),"a pre-Volta adapter gets 1.9.1's architecture note instead of a range");
    if(clocks){ImGui::ActivateItemByID(clocks->GetID("Lock"));frame(v);}
    check(v.commands.empty(),"Lock stays inert without a read lockable range");
    close_menus(v);
    // Preview keeps the catalog reachable, as the former Classic header button did.
    ViewState preview;preview.presentation=Presentation::classic;frame(preview);
    auto* catalog=open_menu(preview,"Profiles");
    if(catalog){ImGui::ActivateItemByID(catalog->GetID("Profile catalog..."));frame(preview);}
    check(preview.profile_catalog_view.open,"Profiles > Profile catalog opens in preview");
    close_menus(preview);
}
size_t at(const std::string& text,const char* value){return text.find(value);}
std::string copied_report;
// Device > Copy device report with the clipboard captured; true when text was copied.
bool copy_device_report(ViewState& v){
    auto& platform=ImGui::GetPlatformIO();const auto previous=platform.Platform_SetClipboardTextFn;
    copied_report.clear();platform.Platform_SetClipboardTextFn=[](ImGuiContext*,const char* text){copied_report=text?text:"";};
    close_menus(v);auto* menu=open_menu(v,"Device");
    if(menu){ImGui::ActivateItemByID(menu->GetID("Copy device report"));frame(v);}
    platform.Platform_SetClipboardTextFn=previous;close_menus(v);
    return menu&&!copied_report.empty();
}
// Released 1.9.1 Device and Help menus: wording and order, the second-window
// submenu over every enumerated card, and Keyboard shortcuts only with a curve.
void classic_device_and_help_menus(){
    auto live=std::make_shared<SessionSnapshot>(fixture());
    live->devices={{"0000:03:00.0","Alternate adapter","GPU-alternate-identity"},{"0000:07:00.0","Second fixture card","GPU-second-fixture"}};
    ViewState v;v.preview=false;v.live=live;v.presentation=Presentation::classic;
    frame(v);
    auto* device=open_menu(v,"Device");auto text=frame(v);
    check(device!=nullptr,"Classic Device menu opens");
    const char* order[]{"Open a second window on","Device report...","Copy device report","Save device report...","Refresh capabilities",
        "Restart GPU device (PnP)...","Locate nvtune...","Forget nvtune location","Enable test signing...","Memory timings / helper setup",
        "Shunt mod (corrected power)..."};
    bool ordered=true;size_t last=0;
    for(const char* item:order){const auto found=at(text,item);ordered=ordered&&found!=std::string::npos&&found>=last;if(found!=std::string::npos)last=found;}
    check(ordered,"Device menu follows 1.9.1's order with the native report save and timings entries beside their neighbours");
    check(!has(text,"Open selected GPU in a second window")&&!has(text,"Test-signing setup...")&&!has(text,"Restart selected GPU (PnP)"),
        "Device menu carries 1.9.1's wording");
    // Opens Device > Open a second window on, then picks one card.
    const auto pick=[&](const char* card){
        close_menus(v);auto* menu=open_menu(v,"Device");
        if(!menu)return std::string();
        ImGui::ActivateItemByID(menu->GetID("Open a second window on"));frame(v);auto listed=frame(v);
        const auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
        if(auto* cards=stack.empty()?nullptr:stack.back().Window){ImGui::ActivateItemByID(cards->GetID(card));frame(v);}
        return listed;
    };
    text=pick("Second fixture card   0000:07:00.0");
    check(has(text,"Alternate adapter   0000:03:00.0")&&has(text,"Second fixture card   0000:07:00.0")&&has(text,"for watching both at once"),
        "the second-window submenu lists every enumerated card with 1.9.1's three-space label and note");
    check(v.app_actions.second_window&&v.app_actions.second_window->slot=="0000:07:00.0"&&v.app_actions.second_window->uuid=="GPU-second-fixture",
        "as in 1.9.1, another listed card launches a second window, with its enumerated slot and expected UUID");
    v.app_actions.second_window.reset();pick("Alternate adapter   0000:03:00.0");
    check(v.app_actions.second_window&&v.app_actions.second_window->slot=="0000:03:00.0"&&v.app_actions.second_window->uuid=="GPU-alternate-identity",
        "the selected card launches with its verified slot and UUID");
    v.app_actions.second_window.reset();
    // 1.9.1 copy_device_report confirms the copy with one [ok] log line, not a result window.
    check(copy_device_report(v)&&!v.classic_log_lines.empty()&&v.classic_log_lines.back().text=="device report copied to clipboard"
        &&v.classic_log_lines.back().ok==true&&v.app_actions.status.empty(),"Classic Copy device report logs 1.9.1's line and opens no result window");
    // A card enumerated without a UUID cannot be verified by the child: inert.
    live->devices[1].uuid.clear();pick("Second fixture card   0000:07:00.0");
    check(!v.app_actions.second_window,"a card enumerated without a GPU UUID launches nothing");
    // The selected card waits for its session; another card does not.
    live->devices[1].uuid="GPU-second-fixture";live->busy=true;pick("Alternate adapter   0000:03:00.0");
    check(!v.app_actions.second_window,"the selected card waits while its session is busy");
    pick("Second fixture card   0000:07:00.0");
    check(v.app_actions.second_window&&v.app_actions.second_window->slot=="0000:07:00.0","another card opens while the selected session is busy");
    live->busy=false;close_menus(v);v.app_actions.second_window.reset();
    v.app_actions.about_open=true;text=frame(v);
    check(has(text,"A second window opens one explicit PCI slot with that card's expected GPU UUID"),"Classic About describes the second window on any card");
    v.app_actions.about_open=false;frame(v);
    live->devices.clear();device=open_menu(v,"Device");
    if(device){ImGui::ActivateItemByID(device->GetID("Open a second window on"));text=frame(v);text=frame(v);}
    check(has(text,"no NVIDIA GPU enumerated"),"an empty enumeration says so");
    close_menus(v);
    auto* help=open_menu(v,"Help");text=frame(v);
    check(help!=nullptr&&has(text,"Licences")&&!has(text,"Licences / bundled sources")&&has(text,"About")&&!has(text,"About Druta")
        &&!has(text,"Keyboard shortcuts"),"Help carries 1.9.1's Licences and About, and no shortcuts without a V/F curve");
    if(help){ImGui::ActivateItemByID(help->GetID("Licences"));frame(v);}
    check(v.app_actions.licences_open,"Help > Licences opens the bundled sources");
    close_menus(v);
    live->data.curve.push_back({0,750,1400,0});help=open_menu(v,"Help");text=frame(v);
    check(has(text,"Keyboard shortcuts"),"Help lists Keyboard shortcuts once the adapter has a V/F curve");
    close_menus(v);
    ViewState focused;focused.preview=false;focused.live=live;focused.presentation=Presentation::focused;frame(focused);
    live->data.curve.clear();help=open_menu(focused,"Help");text=frame(focused);
    check(has(text,"Keyboard shortcuts")&&has(text,"Licences / bundled sources")&&has(text,"About Druta"),"Focused Help is unchanged");
    close_menus(focused);device=open_menu(focused,"Device");text=frame(focused);
    check(has(text,"Open selected GPU in a second window")&&has(text,"Restart selected GPU (PnP)"),"Focused Device menu is unchanged");
    close_menus(focused);
    check(copy_device_report(focused)&&focused.app_actions.status=="Selected-device report copied."&&focused.classic_log_lines.empty(),
        "Focused Copy device report keeps its result window");
    focused.app_actions.status.clear();frame(focused);
    focused.app_actions.about_open=true;text=frame(focused);
    check(has(text,"A second window opens only the selected PCI slot with an expected GPU UUID"),"Focused About is unchanged");
    focused.app_actions.about_open=false;frame(focused);
    check(v.commands.empty()&&focused.commands.empty(),"menus queue no hardware command");
}
// Released 1.9.1 header details line and card selector label, and the File /
// Profiles split: File keeps Exit, refresh and pause. The profile windows
// sit under Profiles, and the Undo
// note carries 1.9.1's wording without its "takes one too" line, which native
// Undo does not do. Fixture identity strings are arbitrary.
void classic_header_and_profile_wording(){
    auto live=std::make_shared<SessionSnapshot>(fixture());
    std::snprintf(live->data.identity.driver,sizeof(live->data.identity.driver),"fixture-driver");
    std::snprintf(live->data.identity.vbios,sizeof(live->data.identity.vbios),"fixture-vbios");
    live->data.identity.admin=1;
    ViewState v;v.preview=false;v.live=live;v.presentation=Presentation::classic;
    auto text=frame(v);
    check(has(text,"Alternate adapter  \xE2\x80\xA2  driver fixture-driver  \xE2\x80\xA2  vbios fixture-vbios  \xE2\x80\xA2  admin")
        &&!has(text,"NOT admin")&&!has(text,"  |  driver"),"the header details line uses 1.9.1's bullet separators");
    check(has(text,"Alternate adapter   0000:03:00.0"),"the card selector shows 1.9.1's three-space card label");
    live->data.identity.admin=0;text=frame(v);
    check(has(text,"vbios fixture-vbios  \xE2\x80\xA2  NOT admin"),"a non-administrator session says NOT admin");
    auto* file=open_menu(v,"File");text=frame(v);
    check(file!=nullptr&&has(text,"Exit")&&has(text,"Always refresh")&&!has(text,"Presentation")
        &&!has(text,"Profile catalog...")&&!has(text,"Undo from history...")&&!has(text,"Rail / power profiles..."),
        "Classic File keeps Exit and the native view controls; profile windows are not in it");
    close_menus(v);
    auto* profiles=open_menu(v,"Profiles");text=frame(v);
    check(profiles!=nullptr&&has(text,"Profile catalog...")&&has(text,"Undo from history...")&&has(text,"Rail / power profiles..."),
        "the native profile windows stay reachable from Profiles");
    check(has(text,"restores the snapshot taken automatically")&&has(text,"immediately before the most recent write.")&&!has(text,"It is a write itself"),
        "the Undo note carries 1.9.1's wording for what native Undo does");
    close_menus(v);
    check(v.commands.empty(),"header and menus queue no hardware command");
}
void history_menu() {
    for(const auto skin:{Presentation::focused,Presentation::classic}) {
        ViewState v;v.preview=false;v.live=std::make_shared<SessionSnapshot>(fixture());v.presentation=skin;
        frame(v);auto* workspace=ImGui::FindWindowByName("Druta workspace");
        // Classic keeps the native history beside 1.9.1's profile actions.
        ImGui::ActivateItemByID(ImHashStr(skin==Presentation::classic?"Profiles":"File",0,workspace->GetID("##MenuBar")));frame(v);
        const auto& stack=ImGui::GetCurrentContext()->OpenPopupStack;
        check(!stack.empty()&&stack.back().Window,"profile menu exposes history in both skins");
        if(!stack.empty()&&stack.back().Window) {
            ImGui::ActivateItemByID(stack.back().Window->GetID("Undo from history..."));frame(v);
            const auto history=std::count_if(v.profile_catalog_intents.begin(),v.profile_catalog_intents.end(),[](const auto& intent){return intent.operation==ProfileCatalogIntent::Operation::stage_newest_history;});
            const auto apply=std::any_of(v.profile_catalog_intents.begin(),v.profile_catalog_intents.end(),[](const auto& intent){return intent.operation==ProfileCatalogIntent::Operation::apply||intent.operation==ProfileCatalogIntent::Operation::preflight;});
            check(history==1&&v.profile_catalog_view.open&&!apply&&v.commands.empty(),"History menu requests a fresh staged snapshot without preflight or hardware Apply");
        }
        v.profile_catalog_view.open=false;frame(v);
    }
}
}
int main(int argc,char** argv) {
    targets_and_reports();quoting();
    const auto missing=load_bundled_notices(L"");check(missing.size()==5,"all bundled sources have explicit entries");
    for(const auto& notice:missing)check(!notice.available && has(notice.text,"missing or unreadable"),"missing bundle never silently substitutes current-working-directory files");
    if(argc>1) {
        std::wstring directory;for(const unsigned char c:std::string(argv[1]))directory.push_back(static_cast<wchar_t>(c));
        const auto notices=load_bundled_notices(directory);
        for(const auto& n:notices)check(n.available && !n.text.empty(),"staged licence/guide is loaded from its actual bundled source");
        check(has(notices[0].text,"GNU GENERAL PUBLIC LICENSE"),"GPL viewer reads full staged COPYING text");
    } else check(false,"test requires staged executable-directory argument");
    ImGui::CreateContext();ImPlot::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    ImFontConfig config;config.SizePixels=18;io.Fonts->AddFontDefault(&config);
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);apply_theme(1);
    rendering();history_menu();classic_menus();classic_device_and_help_menus();classic_header_and_profile_wording();ImPlot::DestroyContext();ImGui::DestroyContext();
    std::printf("App actions: %d failures\n",failures);return failures?1:0;
}
