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
    bool refused=false;try{second_window_command(exe,L"core",{9,"slot",""},false);}catch(...){refused=true;}
    check(refused,"launch builder refuses an absent target UUID");
}
std::string frame(ViewState& v) {
    ImGui::GetIO().DisplaySize={1700,1100};ImGui::NewFrame();ImGui::LogToBuffer(0);draw(v);
    std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::Render();return text;
}
void rendering() {
    auto live=std::make_shared<SessionSnapshot>(fixture());ViewState v;v.preview=false;v.live=live;v.page=Page::monitor;
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
    rendering();ImPlot::DestroyContext();ImGui::DestroyContext();
    std::printf("App actions: %d failures\n",failures);return failures?1:0;
}
