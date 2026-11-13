// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "app_actions.h"
#include "session.h"
#include "monitor_model.h"
#include "clock_monitor_model.h"
#include "i2c_catalog.h"
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
namespace druta::ui {
namespace {
std::string value(std::optional<double> n) {
    if (!n || !std::isfinite(*n)) return "unavailable";
    std::ostringstream out; out << std::setprecision(12) << *n; return out.str();
}
const char* known(const char* text) { return text[0] ? text : "unavailable"; }
const char* availability(Availability a) {
    switch(a) { case Availability::available:return "available"; case Availability::transient:return "transient / last confirmed request";
    case Availability::unsupported:return "unsupported"; default:return "unknown"; }
}
void control(std::ostream& out,const std::string& label,const ControlState& c) {
    out << label << ": " << availability(c.availability) << "; configured=" << value(c.current)
        << " " << c.unit << "; range=" << value(c.low) << ".." << value(c.high)
        << "; restore reference=" << value(c.stock) << "; step=" << value(c.step)
        << "; writable=" << (c.writable?"yes":"no") << '\n';
    if(!c.detail.empty())out << "  " << c.detail << '\n';
}
std::wstring ascii(const std::string& text) {
    std::wstring out;
    for(unsigned char c:text) {
        if(c<0x20 || c>=0x7f)throw std::invalid_argument("device target contains non-ASCII or control characters");
        out.push_back(static_cast<wchar_t>(c));
    }
    return out;
}
}
AppTarget app_target(const SessionSnapshot& s) { return {s.generation,s.selected_slot,s.data.identity.uuid}; }
void cancel_pending_app_actions(AppActionsState& state) {
    state.restart_device.reset();state.second_window.reset();state.save_report_requested=false;state.exit_requested=false;
}
bool app_target_matches(const AppTarget& t,const SessionSnapshot& s) {
    return t.generation==s.generation && t.slot==s.selected_slot && t.uuid==s.data.identity.uuid;
}
std::optional<AppTarget> restart_device_target(const SessionSnapshot& s,std::string& why,bool administrator){
    const auto& id=s.data.identity;
    if(s.opening||s.busy)why="Wait for the current hardware operation to finish.";
    else if(s.selected_slot.empty()||(id.slot[0]&&s.selected_slot!=id.slot)||id.pairing_error[0])why="A verified selected PCI slot is required.";
    else if(!administrator&&!id.admin)why="Run Druta as administrator to restart the selected GPU.";
    else{why.clear();return app_target(s);}return {};
}
std::optional<AppTarget> second_window_target(const SessionSnapshot& s,std::string& why) {
    const auto& id=s.data.identity;
    if(!s.data.connected || s.opening || s.busy)why="Wait for the selected adapter to finish opening or its current operation.";
    else if(s.selected_slot.empty() || s.selected_slot!=id.slot || !id.uuid[0] || id.pairing_error[0])
        why="A verified selected PCI slot and GPU UUID are required; no second window was launched.";
    else {why.clear();return app_target(s);}
    return {};
}
DeviceReport selected_device_report(const SessionSnapshot& s) {
    DeviceReport report; report.target=app_target(s); const auto& d=s.data;const auto& id=d.identity;
    std::ostringstream out;out << std::setprecision(12);
    out << "Druta 1.9.1 native frontend — selected-device report\n"
        << "Captured snapshot generation " << s.generation << ", revision " << s.revision << ", completed reads " << s.completed_reads << "\n"
        << "Selected slot: " << (s.selected_slot.empty()?"unavailable":s.selected_slot) << "\nIdentity slot: " << known(id.slot)
        << "\nUUID: " << known(id.uuid) << "\nDevice: " << known(id.name) << "\nDriver: " << known(id.driver)
        << "\nVBIOS: " << known(id.vbios) << "\nConnection: " << (d.connected?"connected":"disconnected / values may be captured earlier")
        << "; opening=" << s.opening << "; busy=" << s.busy << "; paused=" << s.paused << "\nStatus: " << s.status
        << "\nNvAPI: " << (id.nvapi_ok?"available":"unavailable") << "; NVML: " << (id.nvml_ok?"available":"unavailable")
        << "\nNVML source: " << known(id.nvml_path) << "\nPairing error: " << (id.pairing_error[0]?id.pairing_error:"none reported")
        << "\nNvAPI error: " << known(id.nvapi_error) << "\nNVML error: " << known(id.nvml_error)
        << "\nAdministrator: " << (id.size>=sizeof(id) && (id.nvapi_ok || id.nvml_ok)?(id.admin?"yes":"no"):"unavailable / identity not established")
        << "\n\nConfigured requests (not physical measurements)\n";
    const char* names[]{"Core offset","Memory offset","Board power","Fan policy duty","Voltage boost"};
    for(size_t i=0;i<d.controls.size();++i)control(out,names[i],d.controls[i]);
    out << "NVML enforced board power: " << value(d.enforced_power_w) << " W (separate from configured request/draw)\n";
    for(const auto& a:d.advanced_controls)control(out,a.label+" [domain "+std::to_string(a.id.domain)+"]",a.state);
    for(const auto& p:d.power_policies)control(out,p.label+" [policy "+std::to_string(p.id)+"]",p.state);
    if(d.power_policies.empty())out << "Power-policy diagnostics: unavailable in this snapshot\n";
    for(const auto& r:d.rail_controls) {
        control(out,"Rail "+std::to_string(r.id.rail)+" field "+std::to_string(r.id.field),r.state);
        out << "  Captured base estimate: " << value(r.estimated_base_mv) << " mV; not a factory default\n";
    }
    if(d.rail_controls.empty())out << "Rail controls: unavailable in this snapshot\n";
    out << "\nCurve: " << d.curve.size() << " points; stale=" << s.curve_stale << "; writable=" << d.curve_writable
        << "; step=" << (d.curve_step_khz?std::to_string(d.curve_step_khz):"unavailable")
        << " kHz; wire divisor=" << (d.curve_freq_div?std::to_string(d.curve_freq_div):"unavailable") << "\nCurve detail: " << d.curve_error
        << "\nMemory technology/divisor and per-memory lockable-clock list: not published by this native snapshot\n";
    const auto model=build_monitor_model(s);
    auto reading=[&](const MonitorReading& r) {
        out << r.label << ": ";
        if(r.freshness==MonitorFreshness::unavailable)out << "unavailable";
        else out << r.text << " " << r.unit << (r.freshness==MonitorFreshness::stale?" [stale / last confirmed read]":" [current snapshot]");
        out << '\n';
    };
    out << "\nTelemetry (independent availability/freshness)\n";for(const auto& r:model.tiles)reading(r);
    for(const auto& g:model.groups){out << g.label << '\n';for(const auto& r:g.readings)reading(r);}
    for(const auto* reasons:{&model.clock_reasons,&model.perf_reasons})for(const auto& r:*reasons)
        out << r.label << ": " << (!r.active?"unavailable":*r.active?"active":"inactive")
            << (r.freshness==MonitorFreshness::stale?" [stale]":"") << '\n';
    out << "\nClock-domain evidence\n";
    if(d.clock_monitor) {
        out << (d.clock_monitor->available?"available":"unavailable") << "; " << d.clock_monitor->message
            << (d.clock_monitor->stale || s.paused?" [stale]":"") << '\n';
        for(const auto& row:d.clock_monitor->rows)out << row.label << ": A=" << row.a << "; B=" << row.b
            << "; delta=" << row.delta << "; " << row.flags << "; source=" << row.source << '\n';
        for(size_t i=0;i<d.clock_monitor->readings.size();++i) {
            const auto& r=d.clock_monitor->readings[i];
            out << (r.key.empty()?"clock reading "+std::to_string(i):r.key) << ": ";
            if(!r.available)out << "unavailable";
            else out << r.trust << "; " << r.state << "; " << r.verdict;
            out << (r.stale || d.clock_monitor->stale || s.paused?" [stale]":"") << '\n';
        }
    } else out << "unavailable\n";
    out << "\nI2C: " << (d.i2c.available?"transport available":"unavailable") << "; selected=" << bool(d.i2c.selected)
        << "; selection revision=" << d.i2c.selection_revision << "; verified=" << d.i2c.verified
        << "; recovery=" << d.i2c.status.recovery_pending << "; " << d.i2c.message << '\n';
    out << "Controller family: " << i2c_family_name(d.i2c.family) << "; recipe: "
        << (d.i2c.control_profile.empty()?"unavailable":d.i2c.control_profile) << '\n';
    if(d.i2c.selected)out << "Controller route: port " << unsigned(d.i2c.selected->port) << ", address 0x"
        << std::hex << unsigned(d.i2c.selected->addr7) << std::dec << '\n';
    else out << "Controller route: unavailable\n";
    out << "Controller request: " << value(d.i2c.status.requested_mv) << " mV; controller VOUT: "
        << value(d.i2c.status.controller_vout_mv) << " mV" << (d.i2c.status.stale?" [stale]":"")
        << "; gate=" << d.i2c.gate << "; owned=" << d.i2c.status.owned << "; " << d.i2c.status.message << '\n';
    for(const auto& f:d.i2c.controller.fields)out << "  " << f.label << " [" << f.key << "]: " << value(f.value)
        << " " << f.unit << (f.stale?" [stale]":"") << "; writable=" << f.writable << "; " << f.detail << '\n';
    for(const auto& r:d.i2c.controller.readings)out << "  " << r.first << ": " << r.second << '\n';
    out << "\nTuning is session-only. Saving/copying this report writes nothing to the GPU. Missing values are not zero or unsupported capabilities.\n";
    report.text=out.str();return report;
}
std::wstring quote_windows_argument(const std::wstring& argument) {
    std::wstring out=L"\"";size_t slashes=0;
    for(wchar_t c:argument) {
        if(c==L'\\'){++slashes;continue;}
        out.append(slashes*(c==L'"'?2:1),L'\\');slashes=0;
        if(c==L'"')out.push_back(L'\\');out.push_back(c);
    }
    out.append(slashes*2,L'\\');out.push_back(L'"');return out;
}
std::wstring second_window_command(const std::wstring& executable,const std::wstring& core,const AppTarget& target,bool classic) {
    if(executable.empty() || core.empty() || target.slot.empty() || target.uuid.empty())throw std::invalid_argument("explicit executable, core, slot and UUID required");
    std::vector<std::wstring> args{executable,L"--gpu",ascii(target.slot),L"--expect-uuid",ascii(target.uuid),L"--core",core};
    if(classic)args.push_back(L"--classic");
    std::wstring out;for(const auto& arg:args){if(!out.empty())out.push_back(L' ');out+=quote_windows_argument(arg);}return out;
}
std::vector<BundledNotice> load_bundled_notices(const std::wstring& directory) {
    const std::pair<const wchar_t*,const char*> files[]{{L"COPYING","Druta — GPL-3.0-or-later"},
        {L"ImGui-LICENSE.txt","Dear ImGui — MIT"},{L"ImPlot-LICENSE.txt","ImPlot — MIT"},{L"JSON-LICENSE.txt","nlohmann JSON — MIT"},
        {L"Native-README.md","Native UI guide"}};
    std::vector<BundledNotice> out;
    for(const auto& file:files) {
        BundledNotice notice;notice.title=file.second;notice.source=std::filesystem::path(file.first).u8string();
        std::ifstream input;
        if(!directory.empty())input.open(std::filesystem::path(directory)/file.first,std::ios::binary|std::ios::ate);
        if(input && input.tellg()>0 && input.tellg()<=4*1024*1024) {
            notice.text.resize(static_cast<size_t>(input.tellg()));input.seekg(0);input.read(notice.text.data(),static_cast<std::streamsize>(notice.text.size()));
            notice.available=bool(input);
        }
        if(!notice.available)notice.text="Bundled source is missing or unreadable: "+notice.source+". No replacement licence text was synthesized.";
        out.push_back(std::move(notice));
    }
    return out;
}
}
