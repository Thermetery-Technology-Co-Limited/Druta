// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Render thread owns windows and presentation; Session owns all backend calls.
#include <windows.h>
#include <shellapi.h>
#include <commdlg.h>
#include <d3d9.h>
#include "imgui.h"
#include "implot.h"
#include "imgui_impl_win32.h"
#include "imgui_impl_dx9.h"
#include "refresh_policy.h"
#include "ui_view.h"
#include "device_recovery.h"
#include <future>
#include "timing_capture.h"
#include "classic_layout.h"
#include "session.h"
#include "curve_profile.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <stdexcept>
#include <vector>

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND, UINT, WPARAM, LPARAM);

namespace {
constexpr int restore_hotkey = 1;
constexpr UINT session_update_message = WM_APP + 1;
// Only delivered by newer Windows; do not raise the Vista API import floor.
constexpr UINT dpi_changed_message = 0x02e0;
constexpr wchar_t window_class[] = L"DrutaNativePreview";
double now_seconds() {
    static const double frequency = [] { LARGE_INTEGER n{}; QueryPerformanceFrequency(&n); return double(n.QuadPart); }();
    LARGE_INTEGER n{}; QueryPerformanceCounter(&n); return double(n.QuadPart) / frequency;
}
double process_seconds() {
    FILETIME created{}, exited{}, kernel{}, user{};
    GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user);
    const auto seconds = [](FILETIME t) {
        return (double(t.dwHighDateTime) * 4294967296.0 + t.dwLowDateTime) / 10000000.0;
    };
    return seconds(kernel) + seconds(user);
}
bool write_bytes(const std::wstring& path, const void* data, DWORD size) {
    HANDLE f = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    const bool ok = WriteFile(f, data, size, &written, nullptr) && written == size;
    CloseHandle(f); return ok;
}
class MessageWait {
    HANDLE timer_ = nullptr;
public:
    MessageWait() {
        // Probe the high-resolution timer capability, without version gates or
        // timeBeginPeriod's process/system-wide timer-resolution side effects.
        using CreateTimer = HANDLE (WINAPI*)(LPSECURITY_ATTRIBUTES, LPCWSTR, DWORD, DWORD);
        const auto create = reinterpret_cast<CreateTimer>(GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "CreateWaitableTimerExW"));
        if (create) timer_ = create(nullptr, nullptr, 0x00000002, TIMER_MODIFY_STATE | SYNCHRONIZE);
        if (!timer_) timer_ = CreateWaitableTimerW(nullptr, FALSE, nullptr);
    }
    ~MessageWait() { if (timer_) { CancelWaitableTimer(timer_); CloseHandle(timer_); } }
    void wait(double seconds) {
        // Use the precise timer only for interactive frame deadlines. Quiet
        // windows retain ordinary coarse waits; Pause cancels recurring work.
        if (timer_ && std::isfinite(seconds) && seconds <= druta::ui::RefreshPolicy::active_interval) {
            LARGE_INTEGER due{};
            due.QuadPart = -LONGLONG(std::max(1.0, std::ceil(seconds * 10000000.0)));
            if (SetWaitableTimer(timer_, &due, 0, nullptr, nullptr, FALSE)) {
                MsgWaitForMultipleObjectsEx(1, &timer_, INFINITE, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
                CancelWaitableTimer(timer_); return;
            }
        }
        if (timer_) CancelWaitableTimer(timer_);
        const DWORD timeout = std::isfinite(seconds)
            ? DWORD(std::min(60000.0, std::max(1.0, std::ceil(seconds*1000)))) : INFINITE;
        MsgWaitForMultipleObjectsEx(0, nullptr, timeout, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
    }
};
struct Options {
    int width = 1280, height = 800;
    float scale = 0;
    std::wstring smoke_dir, capture, core_path,recovery_receipt,restart_instance;
    bool recovery_helper=false,restart_child=false;uint32_t parent_pid=0;
    std::string slot, expected_uuid;
    druta::ui::Page page = druta::ui::Page::daily;
    bool classic = false, preview = false;
};
bool parse_options(Options& o) {
    int count = 0;
    LPWSTR* args = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!args) return false;
    bool valid = true;
    for (int i = 1; i < count && valid; ++i) {
        const std::wstring key = args[i];
        if(key==L"--device-recovery-helper")o.recovery_helper=true;
        else if(key==L"--device-restart-child")o.restart_child=true;
        else if (key == L"--classic") o.classic = true;
        else if (key == L"--preview") o.preview = true;
        else if (i + 1 >= count) valid = false;
        else {
            const wchar_t* value = args[++i];
            if (key == L"--smoke-test") { o.smoke_dir = value; o.preview = true; }
            else if (key == L"--live-smoke-test") { o.smoke_dir = value; o.preview = false; }
            else if (key == L"--capture") o.capture = value;
            else if (key == L"--core") o.core_path = value;
            else if(key==L"--recovery-result")o.recovery_receipt=value;
            else if(key==L"--expected-instance")o.restart_instance=value;
            else if(key==L"--parent-pid"){wchar_t* end=nullptr;const auto pid=wcstoul(value,&end,10);valid=*value&&end&&!*end&&pid>0;o.parent_pid=pid;}
            else if (key == L"--gpu") {
                const std::wstring slot(value);
                valid = slot.size() < 24 && std::all_of(slot.begin(), slot.end(), [](wchar_t c) { return c > 0 && c < 128; });
                if (valid) for (wchar_t c : slot) o.slot.push_back(static_cast<char>(c));
            }
            else if (key == L"--expect-uuid") {
                const std::wstring uuid(value);
                valid = !uuid.empty() && uuid.size() < 96 && std::all_of(uuid.begin(),uuid.end(),[](wchar_t c){return c>=0x20 && c<0x7f;});
                if(valid)for(wchar_t c:uuid)o.expected_uuid.push_back(static_cast<char>(c));
            }
            else if (key == L"--width" || key == L"--height") {
                wchar_t* end = nullptr; const long n = wcstol(value, &end, 10);
                valid = *value && end && !*end && n >= 640 && n <= 7680;
                if (key == L"--width") o.width = int(n); else o.height = int(n);
            } else if (key == L"--scale") {
                wchar_t* end = nullptr; const double n = wcstod(value, &end);
                valid = *value && end && !*end && std::isfinite(n) && n >= 1 && n <= 3;
                o.scale = float(n);
            } else if (key == L"--page") {
                if (wcscmp(value, L"advanced") == 0) o.page = druta::ui::Page::advanced;
                else if (wcscmp(value, L"monitor") == 0) o.page = druta::ui::Page::monitor;
                else if (wcscmp(value, L"curve") == 0) o.page = druta::ui::Page::curve;
                else if (wcscmp(value, L"timings") == 0) o.page = druta::ui::Page::timings;
                else if (wcscmp(value, L"rails") == 0) o.page = druta::ui::Page::rails;
                else if (wcscmp(value, L"i2c") == 0) o.page = druta::ui::Page::i2c;
                else if (wcscmp(value, L"daily") != 0) valid = false;
            } else valid = false;
        }
    }
    LocalFree(args); return valid && (o.expected_uuid.empty() || (!o.slot.empty() && !o.preview));
}
struct App {
    HWND window = nullptr;
    IDirect3D9* d3d = nullptr;
    IDirect3DDevice9* device = nullptr;
    D3DPRESENT_PARAMETERS present{};
    druta::ui::RefreshPolicy policy{now_seconds()};
    druta::ui::ViewState view;
    std::unique_ptr<druta::ui::Session> session;
    std::wstring active_core_path, app_executable, app_directory;
    bool closing = false;
    std::future<druta::ui::DeviceRecoveryRequest> recovery_preparation;
    std::optional<druta::ui::AppTarget> recovery_target;
    UINT width = 1280, height = 800;
    bool resize_pending = false, device_lost = false, initialized = false, quit = false;
    float pending_scale = 0, scale = 1;
    double device_retry_at = 0;
    unsigned long long frames = 0, presents = 0, fixture_sample_ticks = 0, resets = 0;
    bool i2c_save_waiting = false;
    uint64_t i2c_save_generation = 0, i2c_save_selection = 0, i2c_save_action = 0;
    struct CatalogPending {
        uint64_t generation=0,actions=0;
        druta::ui::Command::Operation operation;
        druta::ui::ProfileJson document;
    };
    std::optional<CatalogPending> catalog_pending;
    druta::ui::TimingAutoCapture timing_auto;
    uint64_t timing_auto_read=0,timing_auto_generation=0;
    void consume_timing_auto() {
        using namespace druta::ui;
        if(!session||closing)return;
        const auto snapshot=session->snapshot();
        const bool enabled=view.timings_view.auto_capture && view.timings_view.generation==snapshot->generation;
        const bool paused=!policy.telemetry_enabled()||snapshot->paused;
        if(!enabled||paused){timing_auto.observe(enabled,paused,{});return;}
        if(snapshot->busy||snapshot->opening||!snapshot->data.connected)return;
        if(timing_auto_generation==snapshot->generation && timing_auto_read==snapshot->completed_reads)return;
        timing_auto_generation=snapshot->generation;timing_auto_read=snapshot->completed_reads;
        std::optional<TimingBandObservation> observation;
        const auto& data=snapshot->data;const auto& timings=data.timings;
        constexpr auto required=DC_T_MEM|DC_T_PSTATE|DC_T_MEM_OFF;
        if(timings&&!timings->helper_token.empty()&&data.telemetry_valid&&
           (data.telemetry.present&required)==required&&!(snapshot->telemetry_stale_mask&required)) {
            TimingBandObservation sample;sample.target={snapshot->generation,data.identity.uuid,data.identity.slot,timings->helper_token};
            sample.pstate=data.telemetry.pstate;sample.memory_mhz=data.telemetry.mem;
            sample.memory_offset_raw=data.telemetry.mem_off;sample.top_memory_mhz=data.timing_top_memory_mhz;observation=sample;
        }
        if(timing_auto.observe(enabled,false,observation)) {
            Command command;command.operation=Command::Operation::timings_read;
            if(!session->submit(command,snapshot->generation))timing_auto.reset();
        }
    }
    static std::string path_utf8(const std::wstring& path) {
        const int length=WideCharToMultiByte(CP_UTF8,0,path.data(),static_cast<int>(path.size()),nullptr,0,nullptr,nullptr);
        std::string text(static_cast<size_t>(std::max(0,length)),'\0');
        if(length) WideCharToMultiByte(CP_UTF8,0,path.data(),static_cast<int>(path.size()),text.data(),length,nullptr,nullptr);
        return text;
    }
    std::wstring policy_notes_path;
    bool policy_notes_writable=true;
    void initialize_policy_notes() {
        const auto& config=view.shunt.config.path;
        if(config.empty()) {policy_notes_writable=false;return;}
        policy_notes_path=config.substr(0,config.find_last_of(L"\\/"))+L"\\policy-annotations-native.json";
        if(GetFileAttributesW(policy_notes_path.c_str())==INVALID_FILE_ATTRIBUTES) {
            if(GetLastError()!=ERROR_FILE_NOT_FOUND && GetLastError()!=ERROR_PATH_NOT_FOUND) policy_notes_writable=false;
            return;
        }
        try {view.policy_annotations=druta::ui::PolicyAnnotations(druta::ui::read_profile(policy_notes_path));}
        catch(const std::exception& e){policy_notes_writable=false;view.policy_workspace.status=std::string("Existing annotations preserved; could not load: ")+e.what();}
    }
    void voltage_profile_or_command(druta::ui::Command command,bool tables,bool save,bool load) {
        using namespace druta::ui;
        auto& status=tables ? view.voltage_tables_view.status : view.voltage_limits_view.status;
        const auto before=session->snapshot();
        try {
            if(save||load) {
                wchar_t path[32768]{};OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);dialog.hwndOwner=window;
                dialog.lpstrFile=path;dialog.nMaxFile=static_cast<DWORD>(std::size(path));
                dialog.lpstrFilter=L"Druta voltage profile (*.json)\0*.json\0\0";dialog.lpstrDefExt=L"json";
                dialog.lpstrTitle=save ? L"Save selected voltage requests" : L"Load voltage requests into draft";
                dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|(save?OFN_OVERWRITEPROMPT:OFN_FILEMUSTEXIST);
                const BOOL chosen=save?GetSaveFileNameW(&dialog):GetOpenFileNameW(&dialog);
                policy.note_activity(now_seconds());
                if(!chosen){if(CommDlgExtendedError())throw std::runtime_error("Voltage profile file dialog failed");return;}
                auto current=session->snapshot();
                if(current->generation!=before->generation)throw std::runtime_error("Adapter changed while selecting the voltage profile");
                if(load) {
                    const auto document=read_profile(path);current=session->snapshot();
                    if(current->generation!=before->generation)throw std::runtime_error("Adapter changed while reading the voltage profile");
                    if(tables) {
                        if(!current->data.voltage_tables)throw std::runtime_error("Read voltage request tables first");
                        stage_voltage_tables_document(view.voltage_tables_view,document,*current->data.voltage_tables);
                    }else{
                        if(!current->data.voltage_limits)throw std::runtime_error("Read voltage definitions first");
                        stage_voltage_limits_document(view.voltage_limits_view,document,*current->data.voltage_limits);
                    }
                    return;
                }
                command.profile_path=path;
            }
            if(!session->submit(command,before->generation))throw std::runtime_error("Session is busy or changed; retry the voltage operation");
            status=save?"Capturing selected requests; save result appears in the operation log.":"Operation queued; see the operation log.";
        }catch(const std::exception& e){status=e.what();}
    }
    void saving_profile_or_command(druta::ui::Command command,bool power,bool save,bool load){
        using namespace druta::ui;auto& status=power?view.power_saving_view.status:view.clock_gating_view.status;const auto before=session->snapshot();
        try{
            if(save||load){
                wchar_t path[32768]{};OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);dialog.hwndOwner=window;dialog.lpstrFile=path;dialog.nMaxFile=static_cast<DWORD>(std::size(path));
                dialog.lpstrFilter=L"Druta request profile (*.json)\0*.json\0\0";dialog.lpstrDefExt=L"json";dialog.lpstrTitle=save?L"Save selected requests":L"Load requests into draft";
                dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|(save?OFN_OVERWRITEPROMPT:OFN_FILEMUSTEXIST);
                const bool chosen=(save?GetSaveFileNameW(&dialog):GetOpenFileNameW(&dialog))!=FALSE;policy.note_activity(now_seconds());
                if(!chosen){if(CommDlgExtendedError())throw std::runtime_error("Profile file dialog failed");return;}
                auto current=session->snapshot();if(current->generation!=before->generation)throw std::runtime_error("Adapter changed while choosing profile");
                if(load){
                    const auto document=read_profile(path);current=session->snapshot();if(current->generation!=before->generation)throw std::runtime_error("Adapter changed while reading profile");
                    if(power){if(!current->data.power_saving)throw std::runtime_error("Read power saving controls first");stage_power_saving_document(view.power_saving_view,document,*current->data.power_saving);}
                    else{if(!current->data.clock_gating)throw std::runtime_error("Read clock gating controls first");stage_clock_gating_document(view.clock_gating_view,document,*current->data.clock_gating);}
                    return;
                }command.profile_path=path;
            }
            if(!session->submit(command,before->generation))throw std::runtime_error("Session busy or changed; retry the operation");
            status="Operation queued; see operation log.";
        }catch(const std::exception& e){status=e.what();}
    }
    bool selected_profile_path(bool save,uint64_t generation,std::wstring& result){
        wchar_t path[32768]{};OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);dialog.hwndOwner=window;
        dialog.lpstrFile=path;dialog.nMaxFile=static_cast<DWORD>(std::size(path));dialog.lpstrFilter=L"Druta profile (*.json)\0*.json\0\0";dialog.lpstrDefExt=L"json";
        dialog.lpstrTitle=save?L"Save configured requests":L"Load requests into draft";
        dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|(save?OFN_OVERWRITEPROMPT:OFN_FILEMUSTEXIST);
        const bool chosen=(save?GetSaveFileNameW(&dialog):GetOpenFileNameW(&dialog))!=FALSE;policy.note_activity(now_seconds());
        if(!chosen){if(CommDlgExtendedError())throw std::runtime_error("Profile dialog failed");return false;}
        if(session->snapshot()->generation!=generation)throw std::runtime_error("Adapter changed while choosing a profile");result=path;return true;
    }
    void consume_policy_profiles(){
        using namespace druta::ui;
        auto policies=std::move(view.voltage_policy_profile_intents);view.voltage_policy_profile_intents.clear();
        for(const auto& intent:policies){auto& status=view.voltage_policy_view.status;const auto before=session->snapshot();try{
            if(view.voltage_policy_view.generation!=before->generation)throw std::runtime_error("Selected adapter changed");
            const bool save=intent.operation==VoltagePolicyIntent::Operation::save_profile;std::wstring path;
            if(!selected_profile_path(save,before->generation,path))continue;
            if(save){Command c;c.operation=Command::Operation::voltage_policy_save;c.profile_path=path;c.voltage_policy=std::make_shared<const VoltagePolicyRequest>(intent.request);
                if(!session->submit(c,before->generation))throw std::runtime_error("Session changed or busy");status="Capturing selected requests; see operation log.";
            }else{const auto document=read_profile(path);const auto current=session->snapshot();
                if(current->generation!=before->generation||!current->data.voltage_policies)throw std::runtime_error("Session changed or policy readings unavailable");
                stage_voltage_policy_document(view.voltage_policy_view,document,*current->data.voltage_policies);
            }
        }catch(const std::exception& e){status=e.what();}}
        auto integrals=std::move(view.power_integral_profile_intents);view.power_integral_profile_intents.clear();
        for(const auto& intent:integrals){auto& status=view.power_integral_view.status;const auto before=session->snapshot();try{
            if(intent.generation!=before->generation||intent.slot!=before->selected_slot||intent.uuid!=before->data.identity.uuid)throw std::runtime_error("Selected adapter changed");
            Command c;c.integral_requests=std::make_shared<const std::vector<PowerIntegralRequest>>(intent.requests);c.integral_selection=std::make_shared<const std::vector<PowerIntegralKey>>(intent.selection);
            switch(intent.action){
            case PowerIntegralAction::save_profile:case PowerIntegralAction::load_profile:{
                const bool save=intent.action==PowerIntegralAction::save_profile;std::wstring path;if(!selected_profile_path(save,before->generation,path))continue;
                if(save){c.operation=Command::Operation::power_integral_save;c.profile_path=path;}
                else{const auto document=read_profile(path);const auto current=session->snapshot();if(current->generation!=before->generation||!current->data.power_integral)throw std::runtime_error("Session changed or feedback readings unavailable");stage_power_integral_document(view.power_integral_view,document,*current->data.power_integral);continue;}break;
            }
            case PowerIntegralAction::preview_profile:c.operation=Command::Operation::power_integral_preview_profile;break;
            case PowerIntegralAction::apply_profile:c.operation=Command::Operation::power_integral_apply_profile;break;
            case PowerIntegralAction::undo_profile:c.operation=Command::Operation::power_integral_undo_profile;break;
            case PowerIntegralAction::recover_profile:c.operation=Command::Operation::power_integral_recover_profile;break;
            default:throw std::runtime_error("Unknown feedback profile action");
            }
            if(!session->submit(c,before->generation))throw std::runtime_error("Session changed or busy");status="Operation queued; see operation log.";
        }catch(const std::exception& e){status=e.what();}}
    }
    void consume_saving_controls(){
        using namespace druta::ui;
        const auto saving=std::move(view.power_saving_intents);view.power_saving_intents.clear();
        for(const auto& intent:saving){
            Command command;command.saving_families=intent.families;command.power_saving_requests=std::make_shared<const std::vector<PowerSavingRequest>>(intent.requests);command.power_saving_selection=std::make_shared<const std::vector<PowerSavingKey>>(intent.selection);bool save=false,load=false;
            switch(intent.operation){
            case PowerSavingIntent::Operation::read:command.operation=Command::Operation::power_saving_read;break;
            case PowerSavingIntent::Operation::apply:command.operation=Command::Operation::power_saving_apply;break;
            case PowerSavingIntent::Operation::restore_initial:command.operation=Command::Operation::power_saving_restore;break;
            case PowerSavingIntent::Operation::undo:command.operation=Command::Operation::power_saving_undo;break;
            case PowerSavingIntent::Operation::recover:command.operation=Command::Operation::power_saving_recover;break;
            case PowerSavingIntent::Operation::release_owned:command.operation=Command::Operation::power_saving_release;break;
            case PowerSavingIntent::Operation::save_profile:save=true;command.operation=Command::Operation::power_saving_save;break;
            case PowerSavingIntent::Operation::load_profile:load=true;break;
            }saving_profile_or_command(std::move(command),true,save,load);
        }
        const auto clocks=std::move(view.clock_gating_intents);view.clock_gating_intents.clear();
        for(const auto& intent:clocks){
            Command command;command.clock_families=intent.families;command.clock_gating_requests=std::make_shared<const std::vector<ClockGatingRequest>>(intent.requests);
            auto selection=intent.selection;if(selection.empty())selection.push_back(intent.family);command.clock_gating_selection=std::make_shared<const std::vector<ClockGatingFamily>>(selection);bool save=false,load=false;
            switch(intent.operation){
            case ClockGatingIntent::Operation::read:command.operation=Command::Operation::clock_gating_read;break;
            case ClockGatingIntent::Operation::apply:command.operation=Command::Operation::clock_gating_apply;break;
            case ClockGatingIntent::Operation::restore_initial:command.operation=Command::Operation::clock_gating_restore;break;
            case ClockGatingIntent::Operation::undo:command.operation=Command::Operation::clock_gating_undo;break;
            case ClockGatingIntent::Operation::recover:command.operation=Command::Operation::clock_gating_recover;break;
            case ClockGatingIntent::Operation::release:command.operation=Command::Operation::clock_gating_release;break;
            case ClockGatingIntent::Operation::save_profile:save=true;command.operation=Command::Operation::clock_gating_save;break;
            case ClockGatingIntent::Operation::load_profile:load=true;break;
            }saving_profile_or_command(std::move(command),false,save,load);
        }
    }
    void consume_voltage_controls() {
        using namespace druta::ui;
        const auto tables=std::move(view.voltage_tables_intents);view.voltage_tables_intents.clear();
        for(const auto& intent:tables) {
            Command command;command.voltage_families=intent.families;
            command.voltage_table_requests=std::make_shared<const std::vector<VoltageTableRequest>>(intent.requests);
            command.voltage_table_selection=std::make_shared<const std::vector<VoltageTableKey>>(intent.selection);
            bool save=false,load=false;
            switch(intent.operation) {
            case VoltageTablesIntent::Operation::read:command.operation=Command::Operation::voltage_tables_read;break;
            case VoltageTablesIntent::Operation::apply:command.operation=Command::Operation::voltage_tables_apply;break;
            case VoltageTablesIntent::Operation::restore_initial:command.operation=Command::Operation::voltage_tables_restore;break;
            case VoltageTablesIntent::Operation::undo:command.operation=Command::Operation::voltage_tables_undo;break;
            case VoltageTablesIntent::Operation::recover:command.operation=Command::Operation::voltage_tables_recover;break;
            case VoltageTablesIntent::Operation::save_profile:save=true;command.operation=Command::Operation::voltage_tables_save;break;
            case VoltageTablesIntent::Operation::load_profile:load=true;break;
            }
            voltage_profile_or_command(std::move(command),true,save,load);
        }
        const auto limits=std::move(view.voltage_limits_intents);view.voltage_limits_intents.clear();
        for(const auto& intent:limits) {
            Command command;command.voltage_families=intent.families;
            command.voltage_limit_requests=std::make_shared<const std::vector<VoltageLimitRequest>>(intent.requests);
            command.voltage_limit_selection=std::make_shared<const std::vector<VoltageLimitKey>>(intent.selection);
            bool save=false,load=false;
            switch(intent.operation) {
            case VoltageLimitsIntent::Operation::read:command.operation=Command::Operation::voltage_limits_read;break;
            case VoltageLimitsIntent::Operation::apply:command.operation=Command::Operation::voltage_limits_apply;break;
            case VoltageLimitsIntent::Operation::restore_initial:command.operation=Command::Operation::voltage_limits_restore;break;
            case VoltageLimitsIntent::Operation::undo:command.operation=Command::Operation::voltage_limits_undo;break;
            case VoltageLimitsIntent::Operation::recover:command.operation=Command::Operation::voltage_limits_recover;break;
            case VoltageLimitsIntent::Operation::restore_rom:command.operation=Command::Operation::voltage_limits_rom;break;
            case VoltageLimitsIntent::Operation::save_profile:save=true;command.operation=Command::Operation::voltage_limits_save;break;
            case VoltageLimitsIntent::Operation::load_profile:load=true;break;
            }
            voltage_profile_or_command(std::move(command),false,save,load);
        }
    }
    void consume_profile_catalog() {
        using namespace druta::ui;
        auto& workspace=view.profile_catalog_view;
        const auto snapshot=session->snapshot();
        sync_profile_catalog_generation(workspace,snapshot->generation);
        if(catalog_pending) {
            if(snapshot->generation!=catalog_pending->generation) catalog_pending.reset();
            else if(!snapshot->busy && snapshot->completed_actions>catalog_pending->actions) {
                const auto pending=*catalog_pending; catalog_pending.reset();
                std::string message="Profile result unavailable; run preflight again.";
                bool ok=false;
                if(snapshot->last_action_operation==pending.operation) {
                    ok=snapshot->last_action_ok;
                    const auto log=std::find_if(snapshot->log.begin(),snapshot->log.end(),[&](const auto& entry){return entry.id==snapshot->last_action_log_id;});
                    if(log!=snapshot->log.end()) message=log->message;
                }
                record_profile_catalog_preflight(workspace,pending.generation,pending.document,
                    pending.operation==Command::Operation::preflight_catalog_profile && ok,message);
            }
        }
        const auto intents=std::move(view.profile_catalog_intents); view.profile_catalog_intents.clear();
        for(const auto& intent:intents) try {
            if(intent.operation==ProfileCatalogIntent::Operation::save_capture) {
                switch(intent.scope) {
                case ProfileScope::daily:view.save_profile_requested=true;break;
                case ProfileScope::curve:view.save_curve_profile_requested=true;break;
                case ProfileScope::scoped:{
                    view.scoped_profile_view.open=true;
                    Command command;command.operation=Command::Operation::prepare_scoped_profile;
                    if(!session->submit(command,snapshot->generation)) throw std::runtime_error("Session is busy; retry capture.");
                    workspace.status="Choose fields in Rail/power profiles, then Save selected requests.";break;
                }
                case ProfileScope::timing:view.timing_save_profile_requested=true;break;
                case ProfileScope::icx:view.i2c_save_profile_requested=true;break;
                case ProfileScope::fan:{std::wstring path;if(!selected_profile_path(true,snapshot->generation,path))break;
                    Command c;c.operation=Command::Operation::save_fan_profile;c.profile_path=path;if(!session->submit(c,snapshot->generation))throw std::runtime_error("Session changed or busy");workspace.status="Capturing individual fan requests; see operation log.";break;}
                default:throw std::runtime_error("Choose a supported capture scope.");
                }
                continue;
            }
            if(intent.operation==ProfileCatalogIntent::Operation::preflight || intent.operation==ProfileCatalogIntent::Operation::apply) {
                if(catalog_pending) throw std::runtime_error("A profile operation is still running.");
                Command command; command.operation=intent.operation==ProfileCatalogIntent::Operation::preflight ? Command::Operation::preflight_catalog_profile : Command::Operation::apply_catalog_profile;
                command.catalog_profile=std::make_shared<const ProfileJson>(intent.document);
                const auto before=session->snapshot();
                if(before->generation!=workspace.generation || !session->submit(command,before->generation)) throw std::runtime_error("GPU session changed or is busy; run preflight again.");
                catalog_pending=CatalogPending{before->generation,before->completed_actions,command.operation,intent.document};
                workspace.preflight_ok=false;
                workspace.status=intent.operation==ProfileCatalogIntent::Operation::preflight ? "Checking all selected scopes; no writes." : "Applying selected scopes; see operation log.";
                continue;
            }
            const bool save=intent.operation==ProfileCatalogIntent::Operation::save_composed;
            std::vector<wchar_t> buffer(32768,0); OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);
            dialog.hwndOwner=window;dialog.lpstrFile=buffer.data();dialog.nMaxFile=static_cast<DWORD>(buffer.size());
            dialog.lpstrFilter=L"Druta profiles (*.json)\0*.json\0\0";dialog.lpstrDefExt=L"json";
            dialog.lpstrTitle=save ? L"Save composed profile" : L"Add profile files to catalog";
            dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|(save?OFN_OVERWRITEPROMPT:OFN_FILEMUSTEXIST|OFN_ALLOWMULTISELECT);
            const BOOL chosen=save?GetSaveFileNameW(&dialog):GetOpenFileNameW(&dialog);
            policy.note_activity(now_seconds());
            if(!chosen) { if(CommDlgExtendedError()) throw std::runtime_error("Profile file dialog failed.");continue; }
            if(session->snapshot()->generation!=snapshot->generation) throw std::runtime_error("Adapter changed while choosing profile files.");
            if(save) { write_profile(buffer.data(),intent.document);workspace.status="Composed profile saved. No settings applied.";continue; }
            const std::wstring first(buffer.data());const wchar_t* next=buffer.data()+first.size()+1;
            std::vector<std::wstring> paths;
            if(!*next) paths.push_back(first);
            else while(*next) { const std::wstring name(next);paths.push_back(first+L"\\"+name);next+=name.size()+1; }
            std::string errors;
            for(const auto& path:paths) try {
                const auto document=read_profile(path);const auto name=path_utf8(path.substr(path.find_last_of(L"\\/")+1));
                add_profile_catalog_item(workspace,path_utf8(path),name,document);
            } catch(const std::exception& error) {if(!errors.empty())errors+="; ";errors+=path_utf8(path)+": "+error.what();}
            workspace.status=errors.empty()?"Files loaded. Choose included controls and compose to stage.":errors;
        } catch(const std::exception& error) {workspace.preflight_ok=false;workspace.status=error.what();}
    }

    void i2c_profile_dialog(bool save, const druta::ui::SessionSnapshot& snapshot) {
        const auto& data=snapshot.data.i2c;
        wchar_t path[32768]{}; OPENFILENAMEW dialog{}; dialog.lStructSize=sizeof(dialog);
        dialog.hwndOwner=window;dialog.lpstrFile=path;dialog.nMaxFile=static_cast<DWORD>(std::size(path));
        dialog.lpstrFilter=L"Druta iCX preset (*.json)\0*.json\0All files\0*.*\0\0";dialog.lpstrDefExt=L"json";
        dialog.lpstrTitle=save?L"Save captured iCX settings":L"Stage iCX preset";
        dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|(save?OFN_OVERWRITEPROMPT:OFN_FILEMUSTEXIST);
        const BOOL selected=save?GetSaveFileNameW(&dialog):GetOpenFileNameW(&dialog);
        if(selected) {
            druta::ui::Command c;c.operation=save?druta::ui::Command::Operation::i2c_save_profile:druta::ui::Command::Operation::i2c_load_profile;
            c.profile_path=path;c.i2c_route=data.selected;c.i2c_family=data.family;
            c.i2c_selection_revision=data.selection_revision;c.i2c_preset_revision=data.prepared_preset_revision;
            session->submit(c,snapshot.generation);
        } else if(const auto error=CommDlgExtendedError()) {
            const auto message=L"The iCX preset dialog could not open (Windows error "+std::to_wstring(error)+L").";
            MessageBoxW(window,message.c_str(),L"Druta",MB_OK|MB_ICONERROR);
        }
        policy.note_activity(now_seconds());
    }

    void pause() {
        policy.set_explicit_pause(true, now_seconds());
        if (session) session->set_paused(true);
        ShowWindow(window, SW_MINIMIZE);
    }
    void restore() {
        if (IsIconic(window)) ShowWindow(window, SW_RESTORE);
        policy.set_minimized(false, now_seconds());
        policy.set_explicit_pause(false, now_seconds());
        policy.note_activity(now_seconds());
        if (session) { session->set_paused(false); session->request_poll(); }
        SetForegroundWindow(window);
    }
    void request_close(bool for_device_restart=false) {
        closing = true;
        if (session) {if(for_device_restart)session->begin_device_restart_shutdown();else session->begin_shutdown();}
        else quit = true;
        SetWindowTextW(window, L"Druta - finishing current operation and closing");
        policy.note_activity(now_seconds());
    }
    void consume_app_actions() {
        auto& actions=view.app_actions;
        if(closing) {druta::ui::cancel_pending_app_actions(actions);return;}
        if(actions.notices.empty()) {
            std::vector<wchar_t> path(32768);
            const DWORD length=GetModuleFileNameW(nullptr,path.data(),static_cast<DWORD>(path.size()));
            app_executable=length && length<path.size()?std::wstring(path.data(),length):std::wstring{};
            const auto separator=app_executable.find_last_of(L"\\/");
            app_directory=separator==std::wstring::npos?std::wstring{}:app_executable.substr(0,separator);
            actions.notices=druta::ui::load_bundled_notices(app_directory);
        }
        if(recovery_preparation.valid()&&recovery_preparation.wait_for(std::chrono::seconds(0))==std::future_status::ready){
            actions.recovery_preparing=false;
            try{
                auto request=recovery_preparation.get();const auto fresh=session?session->snapshot():view.live;std::string why;
                if(!recovery_target||!fresh||view.select_slot||!druta::ui::app_target_matches(*recovery_target,*fresh)||!druta::ui::restart_device_target(*fresh,why,actions.administrator))throw std::runtime_error("Recovery cancelled because the selected session changed or became busy");
                if(!druta::ui::launch_device_recovery_helper(request,why))throw std::runtime_error(why);
                recovery_target.reset();request_close(true);return;
            }catch(const std::exception& e){recovery_target.reset();actions.status=e.what();}
        }
        if(actions.restart_device){
            const auto target=*actions.restart_device;actions.restart_device.reset();
            try{
                if(recovery_preparation.valid())throw std::runtime_error("Recovery target verification is already in progress");
                const auto fresh=session?session->snapshot():view.live;std::string why;
                if(!fresh||view.select_slot||!druta::ui::app_target_matches(target,*fresh)||!druta::ui::restart_device_target(*fresh,why,actions.administrator))throw std::runtime_error("Selected GPU changed or is busy. "+why);
                recovery_target=target;actions.recovery_preparing=true;
                recovery_preparation=std::async(std::launch::async,[target,exe=app_executable,core=active_core_path,hwnd=window]{
                    struct Notify{HWND hwnd;~Notify(){PostMessageW(hwnd,session_update_message,0,0);}} notify{hwnd};
                    druta::ui::DeviceRecoveryRequest request;request.target=druta::ui::resolve_restart_target(target.slot,druta::ui::enumerate_restart_targets());
                    request.uuid=target.uuid;request.parent_pid=GetCurrentProcessId();request.executable=exe;request.core=core;
                    const auto directory=druta::ui::device_recovery_directory();request.receipt=druta::ui::create_device_recovery_receipt(directory);
                    std::string why;if(!druta::ui::valid_device_recovery_request(request,directory,why))throw std::runtime_error(why);return request;
                });
                actions.status="Verifying the selected PnP device for recovery...";
            }catch(const std::exception& e){actions.recovery_preparing=false;actions.status=e.what();}
        }
        if(actions.exit_requested) {actions.exit_requested=false;request_close();return;}
        if(actions.save_report_requested) {
            actions.save_report_requested=false;
            const auto report=actions.report;
            const auto before=session?session->snapshot():view.live;
            if(!report || !before || view.select_slot || !druta::ui::app_target_matches(report->target,*before))
                actions.status="Selected adapter changed; capture its report again before saving.";
            else {
                wchar_t filename[32768]=L"Druta-device-report.txt";OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);
                dialog.hwndOwner=window;dialog.lpstrFile=filename;dialog.nMaxFile=static_cast<DWORD>(std::size(filename));
                dialog.lpstrFilter=L"Device report (*.txt)\0*.txt\0All files\0*.*\0\0";dialog.lpstrDefExt=L"txt";
                dialog.lpstrTitle=L"Save selected-device report";
                dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|OFN_OVERWRITEPROMPT;
                if(GetSaveFileNameW(&dialog)) {
                    const auto after=session?session->snapshot():view.live;
                    if(!after || view.select_slot || !druta::ui::app_target_matches(report->target,*after))
                        actions.status="Adapter/session changed during the dialog; no report file was written.";
                    else if(report->text.size()<(UINT64_C(1)<<32) && write_bytes(filename,report->text.data(),static_cast<DWORD>(report->text.size())))
                        actions.status="Captured selected-device report saved.";
                    else actions.status="Could not write the selected report file.";
                } else if(CommDlgExtendedError())actions.status="The report save dialog could not open.";
                policy.note_activity(now_seconds());
            }
        }
        if(actions.second_window) {
            const auto target=*actions.second_window;actions.second_window.reset();
            const auto fresh=session?session->snapshot():view.live;std::string why;
            if(!fresh || view.select_slot || !druta::ui::app_target_matches(target,*fresh) || !druta::ui::second_window_target(*fresh,why))
                actions.status="Second window cancelled: selected adapter changed or its slot/UUID is unverified. "+why;
            else {
                try {
                    auto command=druta::ui::second_window_command(app_executable,active_core_path,target,
                        view.presentation==druta::ui::Presentation::classic);
                    std::vector<wchar_t> buffer(command.begin(),command.end());buffer.push_back(0);
                    STARTUPINFOW startup{};startup.cb=sizeof(startup);PROCESS_INFORMATION process{};
                    if(CreateProcessW(app_executable.c_str(),buffer.data(),nullptr,nullptr,FALSE,0,nullptr,app_directory.c_str(),&startup,&process)) {
                        CloseHandle(process.hThread);CloseHandle(process.hProcess);
                        actions.status="Second window launched for the explicit PCI slot and expected UUID; no tuning was passed.";
                    } else actions.status="Could not launch the selected GPU window (Windows error "+std::to_string(GetLastError())+").";
                } catch(const std::exception& error) {actions.status=error.what();}
            }
        }
    }
    void consume_actions() {
        consume_app_actions();
        if (!session || closing) { view.commands.clear(); return; }
        consume_profile_catalog();
        consume_voltage_controls();consume_saving_controls();consume_policy_profiles();
        if(view.import_policy_annotations){
            view.import_policy_annotations=false;
            try{
                const auto before=session->snapshot();wchar_t path[32768]{};OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);dialog.hwndOwner=window;
                dialog.lpstrFile=path;dialog.nMaxFile=static_cast<DWORD>(std::size(path));dialog.lpstrFilter=L"Python policy labels (*.json)\0*.json\0\0";
                dialog.lpstrTitle=L"Import Python display labels";dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|OFN_FILEMUSTEXIST;
                if(GetOpenFileNameW(&dialog)){
                    const auto document=druta::ui::read_profile(path);const auto current=session->snapshot();
                    if(before->generation!=current->generation||!current->data.scoped_profiles)throw std::runtime_error("Refresh policies for the selected GPU before importing labels");
                    const auto count=druta::ui::import_python_policy_names(document,*current->data.scoped_profiles,view.policy_annotations);
                    view.save_policy_annotations=count!=0;view.policy_workspace.status=std::to_string(count)+" matching policy labels imported";
                }else if(CommDlgExtendedError())throw std::runtime_error("Policy label file dialog failed");
                policy.note_activity(now_seconds());
            }catch(const std::exception& e){view.policy_workspace.status=e.what();}
        }
        if(view.save_policy_annotations) {
            view.save_policy_annotations=false;
            try {
                if(!policy_notes_writable||policy_notes_path.empty())throw std::runtime_error("Existing annotations could not be loaded; the file is preserved");
                const auto parent=policy_notes_path.substr(0,policy_notes_path.find_last_of(L"\\/"));
                if(!CreateDirectoryW(parent.c_str(),nullptr)&&GetLastError()!=ERROR_ALREADY_EXISTS)throw std::runtime_error("Could not create the annotation directory");
                druta::ui::write_profile(policy_notes_path,view.policy_annotations.document());
                view.policy_workspace.status="Policy labels saved; tuning requests are unchanged.";
            }catch(const std::exception& e){view.policy_workspace.status=e.what();}
        }
        if(druta::ui::take_shunt_save_request(view.shunt)) {
            try {
                view.shunt.config.save();view.shunt.dirty=false;
                view.shunt.status="Shunt display configuration saved; board-power requests are unchanged.";
            } catch(const std::exception& error) { view.shunt.status=error.what(); }
        }
        if (view.select_slot) { session->select_device(*view.select_slot); view.select_slot.reset(); }
        if (view.reconnect_requested) {
            view.reconnect_requested = false;
            session->reconnect();
        }
        if (view.refresh_requested) { session->request_poll(true); view.refresh_requested = false; }
        if (view.i2c_cancel_requested) {
            view.i2c_cancel_requested=false;
            if (view.live) session->cancel_i2c_discovery(view.live->generation);
        }
        if(i2c_save_waiting) {
            const auto snap=session->snapshot();
            if(snap->generation!=i2c_save_generation || snap->data.i2c.selection_revision!=i2c_save_selection) i2c_save_waiting=false;
            else if(!snap->busy && snap->completed_actions>i2c_save_action) {
                i2c_save_waiting=false;
                if(snap->last_action_operation==druta::ui::Command::Operation::i2c_prepare_profile && snap->last_action_ok)
                    i2c_profile_dialog(true,*snap);
            }
        }
        if(view.i2c_save_profile_requested || view.i2c_load_profile_requested) {
            const bool save=view.i2c_save_profile_requested;
            view.i2c_save_profile_requested=view.i2c_load_profile_requested=false;
            if(view.live && !i2c_save_waiting) {
                if(save) {
                    const auto& snap=*view.live;druta::ui::Command c;c.operation=druta::ui::Command::Operation::i2c_prepare_profile;
                    c.i2c_route=snap.data.i2c.selected;c.i2c_family=snap.data.i2c.family;c.i2c_selection_revision=snap.data.i2c.selection_revision;
                    i2c_save_generation=snap.generation;i2c_save_selection=c.i2c_selection_revision;i2c_save_action=snap.completed_actions;
                    i2c_save_waiting=session->submit(c,snap.generation);
                } else i2c_profile_dialog(false,*view.live);
            }
        }
        if (view.timing_helper_requested) {
            view.timing_helper_requested=false;
            wchar_t path[32768]{}; OPENFILENAMEW dialog{}; dialog.lStructSize=sizeof(dialog);
            dialog.hwndOwner=window; dialog.lpstrFile=path; dialog.nMaxFile=static_cast<DWORD>(std::size(path));
            dialog.lpstrFilter=L"nvtune helper (*.exe)\0*.exe\0\0"; dialog.lpstrTitle=L"Select nvtune helper";
            dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|OFN_FILEMUSTEXIST;
            if (GetOpenFileNameW(&dialog) && view.live) { druta::ui::Command c; c.operation=druta::ui::Command::Operation::timings_select_helper; c.timing_helper_path=path; session->submit(c,view.live->generation); }
            policy.note_activity(now_seconds());
        }
        if (view.timing_save_profile_requested || view.timing_load_profile_requested) {
            const bool save=view.timing_save_profile_requested;
            view.timing_save_profile_requested=view.timing_load_profile_requested=false;
            const auto before=session->snapshot(); const auto staged=view.timings_view.staged;
            wchar_t path[32768]{}; OPENFILENAMEW dialog{}; dialog.lStructSize=sizeof(dialog);
            dialog.hwndOwner=window; dialog.lpstrFile=path; dialog.nMaxFile=static_cast<DWORD>(std::size(path));
            dialog.lpstrFilter=L"Druta timing profile (*.json)\0*.json\0\0"; dialog.lpstrDefExt=L"json";
            dialog.lpstrTitle=save ? L"Save captured timings with staged fields" : L"Load timing fields into draft";
            dialog.Flags=OFN_EXPLORER|OFN_NOCHANGEDIR|OFN_PATHMUSTEXIST|(save?OFN_OVERWRITEPROMPT:OFN_FILEMUSTEXIST);
            const BOOL selected=save?GetSaveFileNameW(&dialog):GetOpenFileNameW(&dialog);
            if (selected) {
                try {
                    const auto current=session->snapshot();
                    if (current->generation!=before->generation || !before->data.timings || !current->data.timings ||
                        current->data.timings->helper_path!=before->data.timings->helper_path ||
                        current->data.timings->helper_token!=before->data.timings->helper_token)
                        throw std::runtime_error("Timing helper or adapter changed while choosing the profile");
                    if (save) {
                        auto request=std::make_shared<druta::ui::TimingEditorRequest>(); request->fields=staged;
                        druta::ui::Command command; command.operation=druta::ui::Command::Operation::timings_save_profile;
                        command.profile_path=path; command.timing_editor=std::move(request);
                        if (!session->submit(command,before->generation)) throw std::runtime_error("Session is busy; timings were not saved");
                        view.timings_view.message="Capturing timings; save result appears in the operation log.";
                    } else {
                        const auto document=druta::ui::read_profile(path);
                        const auto fresh=session->snapshot();
                        if (fresh->generation!=before->generation || !fresh->data.timings)
                            throw std::runtime_error("Selected session changed while loading timings");
                        druta::ui::stage_timing_profile(document,*fresh->data.timings,view.timings_view);
                    }
                } catch (const std::exception& error) { view.timings_view.message=error.what(); }
            } else if (const auto error=CommDlgExtendedError()) view.timings_view.message="Timing profile dialog failed (Windows error "+std::to_string(error)+").";
            policy.note_activity(now_seconds());
        }
        if (view.save_profile_requested || view.load_profile_requested) {
            const bool save = view.save_profile_requested;
            view.save_profile_requested = view.load_profile_requested = false;
            wchar_t path[32768]{};
            OPENFILENAMEW dialog{}; dialog.lStructSize = sizeof(dialog);
            dialog.hwndOwner = window; dialog.lpstrFile = path;
            dialog.nMaxFile = static_cast<DWORD>(std::size(path));
            dialog.lpstrFilter = L"Druta daily settings (*.json)\0*.json\0All files\0*.*\0\0";
            dialog.lpstrDefExt = L"json";
            dialog.lpstrTitle = save ? L"Save current daily settings" : L"Apply saved daily settings";
            dialog.Flags = OFN_EXPLORER | OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST |
                (save ? OFN_OVERWRITEPROMPT : OFN_FILEMUSTEXIST);
            const BOOL selected = save ? GetSaveFileNameW(&dialog) : GetOpenFileNameW(&dialog);
            if (selected && view.live) {
                druta::ui::Command command;
                command.operation = save ? druta::ui::Command::Operation::save_profile
                                         : druta::ui::Command::Operation::load_profile;
                command.profile_path = path;
                session->submit(command, view.live->generation);
            } else if (const auto error = CommDlgExtendedError()) {
                const std::wstring message = L"The profile dialog could not open (Windows error " + std::to_wstring(error) + L").";
                MessageBoxW(window, message.c_str(), L"Druta", MB_OK | MB_ICONERROR);
            }
            policy.note_activity(now_seconds());
        }
        if (view.scoped_profile_view.save_requested || view.scoped_profile_view.load_requested) {
            auto& workspace = view.scoped_profile_view;
            const bool save = workspace.save_requested;
            workspace.save_requested = workspace.load_requested = false;
            const auto before = session->snapshot();
            const auto selection = workspace.selection;
            wchar_t path[32768]{}; OPENFILENAMEW dialog{}; dialog.lStructSize = sizeof(dialog);
            dialog.hwndOwner = window; dialog.lpstrFile = path; dialog.nMaxFile = static_cast<DWORD>(std::size(path));
            dialog.lpstrFilter = L"Druta selected rail/power requests (*.json)\0*.json\0\0"; dialog.lpstrDefExt = L"json";
            dialog.lpstrTitle = save ? L"Save selected configured requests" : L"Preview selected rail/power requests";
            dialog.Flags = OFN_EXPLORER | OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST | (save ? OFN_OVERWRITEPROMPT : OFN_FILEMUSTEXIST);
            const BOOL selected = save ? GetSaveFileNameW(&dialog) : GetOpenFileNameW(&dialog);
            if (selected) {
                try {
                    if (session->snapshot()->generation != before->generation) throw std::runtime_error("Selected adapter changed while choosing the profile");
                    if (save) {
                        auto request = std::make_shared<druta::ui::ScopedProfileRequest>(); request->selection = selection;
                        druta::ui::Command command; command.operation = druta::ui::Command::Operation::save_scoped_profile;
                        command.profile_path = path; command.scoped_profile = std::move(request);
                        const auto action_baseline = session->snapshot();
                        if (!session->submit(command, before->generation)) throw std::runtime_error("Session is busy; profile was not saved");
                        druta::ui::mark_scoped_profile_pending(workspace, *action_baseline, command.operation, true);
                        workspace.status = "Capturing selected requests; save result appears in the operation log.";
                    } else {
                        const auto document = druta::ui::read_profile(path);
                        const auto current = session->snapshot();
                        if (current->generation != before->generation) throw std::runtime_error("Selected adapter changed while reading the profile");
                        druta::ui::sync_scoped_profile_view(workspace, current->generation);
                        if (current->data.scoped_profiles) druta::ui::stage_scoped_profile(document, *current->data.scoped_profiles, workspace);
                        else { workspace.loaded_document = document; workspace.plan.reset(); workspace.status = "Refresh requests to preview this profile."; }
                    }
                } catch (const std::exception& error) { workspace.status = error.what(); }
            } else if (const auto error = CommDlgExtendedError()) workspace.status = "Profile dialog failed (Windows error " + std::to_string(error) + ").";
            policy.note_activity(now_seconds());
        }
        if (view.save_curve_profile_requested || view.load_curve_profile_requested) {
            const bool save = view.save_curve_profile_requested;
            view.save_curve_profile_requested = view.load_curve_profile_requested = false;
            const auto generation = view.live ? view.live->generation : 0;
            wchar_t path[32768]{}; OPENFILENAMEW dialog{}; dialog.lStructSize = sizeof(dialog);
            dialog.hwndOwner = window; dialog.lpstrFile = path; dialog.nMaxFile = static_cast<DWORD>(std::size(path));
            dialog.lpstrFilter = L"Druta V/F curve (*.json)\0*.json\0\0"; dialog.lpstrDefExt = L"json";
            dialog.lpstrTitle = save ? L"Save configured V/F curve" : L"Load V/F curve into draft";
            dialog.Flags = OFN_EXPLORER | OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST | (save ? OFN_OVERWRITEPROMPT : OFN_FILEMUSTEXIST);
            const BOOL selected = save ? GetSaveFileNameW(&dialog) : GetOpenFileNameW(&dialog);
            if (selected) {
                try {
                    const auto current = session->snapshot();
                    if (current->generation != generation) throw std::runtime_error("Adapter changed while selecting the curve profile");
                    if (save) {
                        druta::ui::Command command; command.operation = druta::ui::Command::Operation::save_curve_profile;
                        command.profile_path = path;
                        if (!session->submit(command, generation)) throw std::runtime_error("Session is busy; curve profile was not saved");
                        view.curve_profile_status = "Capturing configured curve; outcome appears in the operation log.";
                    } else {
                        if (view.curve_entry_dirty) throw std::runtime_error("Finish or revert the current numeric curve edit before loading");
                        const auto document = druta::ui::read_profile(path);
                        const auto loaded_snapshot = session->snapshot();
                        if (loaded_snapshot->generation != generation) throw std::runtime_error("Adapter changed while reading the curve profile");
                        druta::ui::stage_curve_profile(document, *loaded_snapshot, view.curve_draft);
                        view.curve_profile_status = "Profile staged locally. Review the curve and consequences, then Apply; Undo edit restores the prior draft.";
                        view.curve_entry_dirty = false; view.curve_entry_editing = false; view.curve_fit_requested = true;
                        const auto& points = view.curve_draft.draft();
                        const auto point = std::find_if(points.begin(), points.end(), [&](const auto& p) { return p.idx == view.selected_curve_point; });
                        if (point != points.end()) {
                            view.curve_frequency_mhz = point->freq_mhz;
                            view.curve_delta_mhz = static_cast<double>(point->delta_khz) / (1000.0 * view.curve_draft.freq_div());
                        }
                    }
                } catch (const std::exception& error) { view.curve_profile_status = error.what(); }
            } else if (const auto error = CommDlgExtendedError()) {
                view.curve_profile_status = "Curve profile dialog failed (Windows error " + std::to_string(error) + ").";
            }
            policy.note_activity(now_seconds());
        }
        if (view.live) for (const auto& command : view.commands) {
            const auto baseline = session->snapshot();
            if (session->submit(command, view.live->generation) &&
                (command.operation == druta::ui::Command::Operation::prepare_scoped_profile || command.operation == druta::ui::Command::Operation::apply_scoped_profile))
                druta::ui::mark_scoped_profile_pending(view.scoped_profile_view, *baseline, command.operation, true);
        }
        view.commands.clear();
    }
    void client_size(int w, int h) {
        RECT r{0, 0, w, h};
        AdjustWindowRectEx(&r, WS_OVERLAPPEDWINDOW, FALSE, 0);
        SetWindowPos(window, nullptr, 0, 0, r.right-r.left, r.bottom-r.top, SWP_NOMOVE | SWP_NOZORDER);
    }
    bool create_device() {
        d3d = Direct3DCreate9(D3D_SDK_VERSION);
        if (!d3d) return false;
        present.Windowed = TRUE; present.SwapEffect = D3DSWAPEFFECT_DISCARD;
        present.BackBufferFormat = D3DFMT_UNKNOWN;
        present.BackBufferWidth = width; present.BackBufferHeight = height;
        // One pacing owner: the policy sleeps after completed frames.
        present.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;
        // Only vertex processing falls back; do not silently substitute a CPU renderer.
        HRESULT hr = d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window,
            D3DCREATE_HARDWARE_VERTEXPROCESSING, &present, &device);
        if (FAILED(hr)) hr = d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window,
            D3DCREATE_SOFTWARE_VERTEXPROCESSING, &present, &device);
        return SUCCEEDED(hr);
    }
    bool recover_device(double now) {
        if (now < device_retry_at) return false;
        const HRESULT status = device->TestCooperativeLevel();
        if (status == D3DERR_DEVICELOST) { device_retry_at = now + 0.25; return false; }
        if (FAILED(status) && status != D3DERR_DEVICENOTRESET) {
            device_retry_at = now + 0.25; return false;
        }
        ImGui_ImplDX9_InvalidateDeviceObjects();
        present.BackBufferWidth = width; present.BackBufferHeight = height;
        if (FAILED(device->Reset(&present))) { device_retry_at = now + 0.25; return false; }
        if (!ImGui_ImplDX9_CreateDeviceObjects()) { device_retry_at = now + 0.25; return false; }
        device_lost = resize_pending = false; ++resets; return true;
    }
    bool capture(const std::wstring& path) {
        IDirect3DSurface9* back = nullptr; IDirect3DSurface9* copy = nullptr;
        if (FAILED(device->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &back))) return false;
        D3DSURFACE_DESC desc{}; back->GetDesc(&desc);
        bool ok = (desc.Format == D3DFMT_X8R8G8B8 || desc.Format == D3DFMT_A8R8G8B8)
            && SUCCEEDED(device->CreateOffscreenPlainSurface(desc.Width, desc.Height, desc.Format,
                    D3DPOOL_SYSTEMMEM, &copy, nullptr));
        if (ok) ok = SUCCEEDED(device->GetRenderTargetData(back, copy));
        D3DLOCKED_RECT lock{};
        if (ok) ok = SUCCEEDED(copy->LockRect(&lock, nullptr, D3DLOCK_READONLY));
        if (ok) {
            const DWORD pixels = desc.Width * desc.Height * 4;
            BITMAPFILEHEADER file{}; BITMAPINFOHEADER info{};
            file.bfType = 0x4d42; file.bfOffBits = sizeof(file) + sizeof(info);
            file.bfSize = file.bfOffBits + pixels;
            info.biSize = sizeof(info); info.biWidth = LONG(desc.Width); info.biHeight = -LONG(desc.Height);
            info.biPlanes = 1; info.biBitCount = 32; info.biCompression = BI_RGB;
            std::vector<unsigned char> bytes(file.bfSize);
            memcpy(bytes.data(), &file, sizeof(file)); memcpy(bytes.data()+sizeof(file), &info, sizeof(info));
            for (UINT y = 0; y < desc.Height; ++y)
                memcpy(bytes.data()+file.bfOffBits+y*desc.Width*4,
                       static_cast<unsigned char*>(lock.pBits)+y*lock.Pitch, desc.Width*4);
            copy->UnlockRect();
            ok = write_bytes(path, bytes.data(), DWORD(bytes.size()));
        }
        if (copy) copy->Release(); back->Release(); return ok;
    }
    bool render(const std::wstring& capture_path = {}) {
        if ((device_lost || resize_pending) && !recover_device(now_seconds())) return false;
        if (pending_scale > 0) {
            scale = pending_scale; pending_scale = 0;
            druta::ui::apply_theme(scale); ImGui::GetStyle().FontScaleDpi = scale;
        }
        ImGui_ImplDX9_NewFrame(); ImGui_ImplWin32_NewFrame();
        // The Win32 backend reports zero client size when minimized. Always refresh
        // deliberately renders the last full viewport, preserving its draft/layout.
        ImGui::GetIO().DisplaySize = ImVec2(float(width), float(height));
        ImGui::NewFrame(); druta::ui::draw(view); ImGui::Render();
        policy.set_always_refresh(view.always_refresh, now_seconds());
        if (session) session->set_paused(!policy.telemetry_enabled());
        consume_actions();
        if (view.pause_requested) {
            view.pause_requested = false; pause(); return false;
        }
        device->SetRenderState(D3DRS_ZENABLE, FALSE);
        device->SetRenderState(D3DRS_ALPHABLENDENABLE, FALSE);
        device->SetRenderState(D3DRS_SCISSORTESTENABLE, FALSE);
        device->Clear(0, nullptr, D3DCLEAR_TARGET, D3DCOLOR_XRGB(12,16,22), 1, 0);
        if (FAILED(device->BeginScene())) { device_lost = true; device_retry_at = now_seconds()+0.25; return false; }
        ImGui_ImplDX9_RenderDrawData(ImGui::GetDrawData()); device->EndScene();
        ++frames;
        const bool captured = capture_path.empty() || capture(capture_path);
        const HRESULT result = device->Present(nullptr, nullptr, nullptr, nullptr);
        if (SUCCEEDED(result)) ++presents;
        else { device_lost = true; device_retry_at = now_seconds() + 0.25; }
        return captured;
    }
};
App* active = nullptr;

LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    App* app = active;
    if (app) {
        if ((message >= WM_MOUSEFIRST && message <= WM_MOUSELAST)
            || (message >= WM_KEYFIRST && message <= WM_KEYLAST)) app->policy.note_activity(now_seconds());
        switch (message) {
        case WM_SIZE:
            app->policy.set_minimized(wparam == SIZE_MINIMIZED, now_seconds());
            if (app->session) app->session->set_paused(!app->policy.telemetry_enabled());
            if (wparam != SIZE_MINIMIZED && LOWORD(lparam) && HIWORD(lparam)) {
                app->width = LOWORD(lparam); app->height = HIWORD(lparam);
                app->resize_pending = app->device != nullptr;
            }
            return 0;
        case dpi_changed_message: {
            app->pending_scale = float(HIWORD(wparam)) / 96.0f;
            const auto* rect = reinterpret_cast<const RECT*>(lparam);
            SetWindowPos(window, nullptr, rect->left, rect->top, rect->right-rect->left,
                rect->bottom-rect->top, SWP_NOZORDER | SWP_NOACTIVATE);
            return 0;
        }
        case WM_GETMINMAXINFO: {
            auto* bounds = reinterpret_cast<MINMAXINFO*>(lparam);
            bounds->ptMinTrackSize = POINT{640, 480}; return 0;
        }
        case WM_HOTKEY:
            if (wparam == restore_hotkey) { app->restore(); return 0; }
            break;
        case WM_CLOSE: app->request_close(); return 0;
        case session_update_message:
            // Worker completion only wakes the host. Ordinary telemetry must
            // not renew the interaction grace and force permanent 60 FPS.
            return 0;
        case WM_SYSCOMMAND:
            if ((wparam & 0xfff0) == SC_KEYMENU) return 0;
            break;
        case WM_DESTROY: app->quit = true; PostQuitMessage(0); return 0;
        }
    }
    if (app && app->initialized && ImGui_ImplWin32_WndProcHandler(window, message, wparam, lparam)) return 1;
    return DefWindowProcW(window, message, wparam, lparam);
}
void load_font() {
    wchar_t directory[MAX_PATH]{};
    const bool have_directory = GetWindowsDirectoryW(directory, MAX_PATH) != 0;
    const auto load = [&](const wchar_t* filename, float size) -> ImFont* {
        if (!have_directory) return nullptr;
        const std::wstring path = std::wstring(directory) + L"\\Fonts\\" + filename;
        HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        if (f != INVALID_HANDLE_VALUE) {
            const DWORD length = GetFileSize(f, nullptr);
            if (length > 0 && length < 32*1024*1024) {
                void* bytes = IM_ALLOC(length); DWORD read = 0;
                if (bytes && ReadFile(f, bytes, length, &read, nullptr) && read == length) {
                    auto* font = ImGui::GetIO().Fonts->AddFontFromMemoryTTF(bytes, int(length), size);
                    CloseHandle(f); return font;
                }
                if (bytes) IM_FREE(bytes);
            }
            CloseHandle(f);
        }
        return nullptr;
    };
    // Keep Focused's default face/size unchanged. Classic follows released
    // 1.9.1's separate body, semibold heading and monospaced reading faces.
    if (!load(L"segoeui.ttf", 18)) {
        ImFontConfig config; config.SizePixels = 18;
        ImGui::GetIO().Fonts->AddFontDefault(&config);
    }
    druta::ui::classic_fonts.body = load(L"segoeui.ttf", 16);
    druta::ui::classic_fonts.heading = load(L"seguisb.ttf", 26);
    druta::ui::classic_fonts.mono = load(L"consola.ttf", 14);
}

// An opt-in bounded renderer harness. Live mode only reads; fixture mode opens
// no GPU handle. The report keeps real completed reads and fixture ticks separate.
struct Smoke {
    const wchar_t* captures[20] = {L"01-focused.bmp", L"02-always.bmp", L"03-minimized.bmp", nullptr,
        L"05-restored.bmp", nullptr, L"07-default.bmp", L"08-classic.bmp", L"09-advanced.bmp",
        L"10-monitor.bmp", L"11-small.bmp", L"12-shunt.bmp", L"13-catalog-focused.bmp", L"14-catalog-classic.bmp", L"15-policy-workspace.bmp", L"16-timing-capture.bmp", L"17-power-saving.bmp", L"18-clock-gating.bmp", L"19-policy-profiles.bmp", L"20-integral-profiles.bmp"};
    const char* names[20] = {"visible_default", "always_visible", "always_minimized", "explicit_pause",
        "hotkey_restore", "default_minimized", "default_restored", "classic", "advanced", "monitor", "resize_1024x768", "shunt_editor", "catalog_focused", "catalog_classic", "policy_workspace", "timing_capture_controls", "power_saving_controls", "clock_gating_controls", "policy_profile_controls", "integral_profile_controls"};
    int phase = 0; bool captured = false, passed = true, finished = false, live = false;
    double start = 0, cpu = 0;
    unsigned long long frames = 0, presents = 0, samples = 0;
    std::string report;
    explicit Smoke(App& app) : live(!!app.session) {
        report = std::string("{\n  \"live_mode\": ") + (live ? "true" : "false") + ",\n  \"phases\": [\n";
        begin(app);
    }
    unsigned long long sample_count(App& app) const {
        return live ? app.session->snapshot()->completed_reads : app.fixture_sample_ticks;
    }
    void begin(App& app) {
        start = now_seconds(); cpu = process_seconds(); frames = app.frames;
        presents = app.presents; samples = sample_count(app); captured = false;
    }
    void advance(App& app, const std::wstring& directory) {
        if (finished || now_seconds()-start < 2.0) return;
        const auto f = app.frames-frames, p = app.presents-presents, t = sample_count(app)-samples;
        // Pause may finish one read which was already in flight.
        bool ok = (phase == 3 || phase == 5) ? (f == 0 && t <= (live ? 1u : 0u)) : (f > 0 && p > 0 && captured);
        if (live) ok = ok && app.session->snapshot()->data.connected && app.session->snapshot()->completed_actions == 0;
        if (live && phase == 2) ok = ok && t > 0;
        if (phase >= 7) {
            const auto& draft = app.view.drafts[static_cast<size_t>(druta::ui::ControlId::memory)];
            ok = ok && (live ? (draft.dirty && draft.value == 26.875) : app.view.memory_offset_mhz == 26.875f);
        }
        if (phase == 10) ok = ok && app.width == 1024 && app.height == 768 && app.resets > 0;
        if (phase == 11) ok = ok && app.view.shunt.open && !app.view.shunt.dirty && !app.view.shunt.save_requested;
        if (phase == 12 || phase == 13) ok = ok && app.view.profile_catalog_view.open && !app.view.profile_catalog_view.preflight_ok && app.view.profile_catalog_intents.empty();
        if (phase == 14) ok = ok && app.view.policy_workspace.open && !app.view.save_policy_annotations && !app.view.policy_workspace.preview;
        if (phase == 15) ok = ok && !app.view.timings_view.auto_capture && app.view.commands.empty();
        if(phase==16)ok=ok&&app.view.power_saving_intents.empty()&&!app.view.open_power_saving_requested;
        if(phase==17)ok=ok&&app.view.clock_gating_intents.empty()&&!app.view.open_clock_gating_requested;
        if(phase==18)ok=ok&&app.view.voltage_policy_profile_intents.empty()&&!app.view.open_voltage_policy_requested;
        if(phase==19)ok=ok&&app.view.power_integral_profile_intents.empty()&&!app.view.open_power_integral_requested;
        passed = passed && ok;
        char row[400];
        snprintf(row, sizeof(row), "    {\"phase\": \"%s\", \"frames\": %llu, \"presents\": %llu, \"%s\": %llu, \"cpu_seconds\": %.6f, \"passed\": %s}%s\n",
            names[phase], f, p, live ? "telemetry_reads" : "fixture_ticks", t, process_seconds()-cpu, ok ? "true" : "false", phase == 19 ? "" : ",");
        report += row;
        if (++phase == 20) {
            report += "  ],\n  \"device_resets\": " + std::to_string(app.resets)
                + ",\n  \"hotkey_registered\": " + (app.view.hotkey_available ? "true" : "false")
                + ",\n  \"passed\": " + (passed ? "true" : "false") + "\n}\n";
            passed = write_bytes(directory + L"\\smoke.json", report.data(), DWORD(report.size())) && passed;
            finished = true; return;
        }
        if (phase == 1) { app.view.always_refresh = true; app.policy.set_always_refresh(true, now_seconds()); }
        if (phase == 2 || phase == 5) ShowWindow(app.window, SW_MINIMIZE);
        if (phase == 3) app.pause();
        if (phase == 4) SendMessageW(app.window, WM_HOTKEY, restore_hotkey, 0); // delivery test, not physical keyboard
        if (phase == 5) { app.view.always_refresh = false; app.policy.set_always_refresh(false, now_seconds()); }
        if (phase == 6) app.restore();
        if (phase == 7) {
            if (live) {
                auto& draft = app.view.drafts[static_cast<size_t>(druta::ui::ControlId::memory)];
                draft.value = 26.875; draft.dirty = true;
            } else app.view.memory_offset_mhz = 26.875f;
            app.view.presentation = druta::ui::Presentation::classic;
        }
        if (phase == 8) app.view.page = druta::ui::Page::advanced;
        if (phase == 9) { app.view.presentation = druta::ui::Presentation::focused; app.view.page = druta::ui::Page::monitor; }
        if (phase == 10) { app.view.page = druta::ui::Page::daily; app.client_size(1024, 768); }
        if (phase == 11) app.view.shunt.open = true;
        if (phase == 12) {
            app.view.shunt.open=false;app.client_size(1800,1200);app.view.profile_catalog_view.open=true;
        }
        if (phase == 13) app.view.presentation=druta::ui::Presentation::classic;
        if (phase == 14) {app.view.profile_catalog_view.open=false;app.view.policy_workspace.open=true;app.view.presentation=druta::ui::Presentation::focused;}
        if (phase == 15) {app.view.policy_workspace.open=false;app.view.page=druta::ui::Page::timings;app.view.presentation=druta::ui::Presentation::classic;}
        if(phase==16){app.view.page=druta::ui::Page::advanced;app.view.presentation=druta::ui::Presentation::focused;app.view.open_power_saving_requested=true;}
        if(phase==17){app.view.page=druta::ui::Page::advanced;app.view.presentation=druta::ui::Presentation::classic;app.view.open_clock_gating_requested=true;}
        if(phase==18){app.view.presentation=druta::ui::Presentation::focused;app.view.open_voltage_policy_requested=true;}
        if(phase==19){app.view.presentation=druta::ui::Presentation::classic;app.view.open_power_integral_requested=true;}
        begin(app);
    }
};
} // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
    {int count=0;auto args=CommandLineToArgvW(GetCommandLineW(),&count);
     if(args&&count>1&&std::wstring(args[1])==L"--device-restart-child"){
        int result=2;if(count==4){const std::wstring slot=args[2];if(std::all_of(slot.begin(),slot.end(),[](wchar_t c){return c>0&&c<128;})){std::string ascii;for(wchar_t c:slot)ascii.push_back(static_cast<char>(c));result=druta::ui::device_restart_child(ascii,args[3]);}}
        LocalFree(args);return result;
     }if(args)LocalFree(args);}

    Options options;
    if (!parse_options(options)) {
        MessageBoxW(nullptr, L"Options: --width N --height N --scale 1..3 --classic --page daily|curve|advanced|monitor|timings\n"
            L"--preview --gpu PCI_SLOT --expect-uuid UUID --core ABSOLUTE_DLL\n--capture FILE.bmp (save and exit)\n--smoke-test DIR (fixtures) or --live-smoke-test DIR (reads only)", L"Druta", MB_ICONERROR); return 2;
    }
    if(options.recovery_helper){
        try{
            std::vector<wchar_t> path(32768);const auto length=GetModuleFileNameW(nullptr,path.data(),static_cast<DWORD>(path.size()));if(!length||length>=path.size())return 2;
            druta::ui::DeviceRecoveryRequest request;request.target={options.slot,options.restart_instance,L"selected GPU"};request.uuid=options.expected_uuid;
            request.parent_pid=options.parent_pid;request.executable=std::wstring(path.data(),length);request.core=options.core_path;request.receipt=options.recovery_receipt;
            return druta::ui::device_recovery_helper(request,druta::ui::device_recovery_directory());
        }catch(...){return 2;}
    }
    if(options.restart_child||options.parent_pid||!options.restart_instance.empty())return 2;
    ImGui_ImplWin32_EnableDpiAwareness();
    App app; active = &app;
    app.width = UINT(options.width); app.height = UINT(options.height);
    WNDCLASSEXW wc{sizeof(wc), CS_CLASSDC, window_proc, 0, 0, instance, nullptr,
        LoadCursorW(nullptr, IDC_ARROW), nullptr, nullptr, window_class, nullptr};
    if (!RegisterClassExW(&wc)) return 3;
    RECT rect{0, 0, options.width, options.height}; AdjustWindowRectEx(&rect, WS_OVERLAPPEDWINDOW, FALSE, 0);
    app.window = CreateWindowExW(0, window_class, options.preview ? L"Druta Native UI - Preview" : L"Druta - Native", WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, rect.right-rect.left, rect.bottom-rect.top, nullptr, nullptr, instance, nullptr);
    if (!app.window || !app.create_device()) {
        MessageBoxW(nullptr, L"The Direct3D 9 preview window could not be created.", L"Druta native preview", MB_ICONERROR);
        if (app.device) app.device->Release(); if (app.d3d) app.d3d->Release();
        if (app.window) DestroyWindow(app.window); UnregisterClassW(window_class, instance); active = nullptr; return 4;
    }
    IMGUI_CHECKVERSION(); ImGui::CreateContext(); ImPlot::CreateContext();
    ImGuiIO& io = ImGui::GetIO(); io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.IniFilename = nullptr; io.LogFilename = nullptr; // no automatic state/tuning persistence
    load_font();
    app.scale = options.scale > 0 ? options.scale : ImGui_ImplWin32_GetDpiScaleForHwnd(app.window);
    druta::ui::apply_theme(app.scale); ImGui::GetStyle().FontScaleDpi = app.scale;
    ImGui_ImplWin32_Init(app.window); ImGui_ImplDX9_Init(app.device); app.initialized = true;
    app.view.page = options.page;
    app.view.preview = options.preview;
    {SID_IDENTIFIER_AUTHORITY authority=SECURITY_NT_AUTHORITY;PSID group=nullptr;BOOL member=FALSE;
     if(AllocateAndInitializeSid(&authority,2,SECURITY_BUILTIN_DOMAIN_RID,DOMAIN_ALIAS_RID_ADMINS,0,0,0,0,0,0,&group)){
        if(CheckTokenMembership(nullptr,group,&member))app.view.app_actions.administrator=member!=FALSE;FreeSid(group);}}

    if(!options.preview){druta::ui::set_shunt_config(app.view.shunt,druta::ui::ShuntConfig::load());app.initialize_policy_notes();}
    if(!options.recovery_receipt.empty()){
        try{const auto receipt=druta::ui::read_device_recovery_receipt(options.recovery_receipt,druta::ui::device_recovery_directory(),options.slot,options.expected_uuid);
            app.view.app_actions.status=receipt.message+(receipt.reboot_required?" Windows reports that a reboot is required.":"")+" A fresh session was opened; no tuning was reapplied.";
        }catch(const std::exception& e){app.view.app_actions.status=std::string("Recovery receipt unavailable: ")+e.what();}
    }
    app.view.presentation = options.classic ? druta::ui::Presentation::classic : druta::ui::Presentation::focused;
    app.view.hotkey_available = RegisterHotKey(app.window, restore_hotkey, MOD_CONTROL | MOD_SHIFT, VK_F11) != FALSE;
    if (!options.preview) {
        if (options.core_path.empty()) {
            std::vector<wchar_t> path(32768);
            const DWORD length = GetModuleFileNameW(nullptr, path.data(), DWORD(path.size()));
            if (length && length < path.size()) {
                std::wstring executable(path.data(), length);
                options.core_path = executable.substr(0, executable.find_last_of(L"\\/")+1) + L"drutacore.dll";
            }
        }
        app.active_core_path=options.core_path;
        auto backend = druta::ui::create_core_backend(options.core_path);
        app.view.planner_api = backend->planner_api(); // pure calls, module pinned by Session for the UI lifetime
        app.session = std::make_unique<druta::ui::Session>(std::move(backend),
            [window = app.window] { PostMessageW(window, session_update_message, 0, 0); },
            app.view.shunt.config.path.empty() ? std::wstring{} : app.view.shunt.config.path.substr(0,app.view.shunt.config.path.find_last_of(L"\\/")) + L"\\i2c-routes-native.json");
        app.session->start(options.slot,options.expected_uuid);
        app.view.live = app.session->snapshot();
    }
    ShowWindow(app.window, SW_SHOW); UpdateWindow(app.window);
    Smoke smoke(app); MessageWait waiter; const bool testing = !options.smoke_dir.empty();
    bool success = true, captured_output = false; unsigned settled_frames = 0;
    const double started = now_seconds();
    while (!app.quit) {
        MSG message;
        for (unsigned drained = 0; drained < 256 && PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE); ++drained) {
            if (message.message == WM_QUIT) app.quit = true;
            TranslateMessage(&message); DispatchMessageW(&message);
        }
        if (app.quit) break;
        if (app.session) {
            app.view.live = app.session->snapshot();
            if (app.closing && app.session->finished()) break;
            if (app.closing && app.view.live->close_refused) {
                app.closing=false;
                SetWindowTextW(app.window,L"Druta - recovery required before closing");
            }
        }
        if (testing) {
            smoke.advance(app, options.smoke_dir);
            if (smoke.finished) { success = smoke.passed; break; }
        }
        const double now = now_seconds();
        app.consume_timing_auto();
        if(app.view.advanced_view.rail_gate_flash_until>ImGui::GetTime() || app.view.advanced_view.xoc_gate_flash_until>ImGui::GetTime() || app.view.i2c_gate_flash_until>ImGui::GetTime())
            app.policy.note_activity(now);
        if (app.policy.telemetry_due(now)) {
            if (app.session && !app.closing) app.session->request_poll();
            else if (options.preview) ++app.fixture_sample_ticks;
            app.policy.sampled(now);
        }
        if (app.policy.render_due(now) && now >= app.device_retry_at) {
            std::wstring capture;
            const bool view_ready = options.preview || (app.view.live && !app.view.live->opening && app.view.live->revision > 1);
            if (testing && !smoke.captured && smoke.captures[smoke.phase] && app.frames-smoke.frames >= 2 && settled_frames >= 2)
                capture = options.smoke_dir + L"\\" + smoke.captures[smoke.phase];
            // ImPlot applies auto-fit on the next frame after points arrive.
            if (!options.capture.empty() && view_ready && settled_frames >= 2) capture = options.capture;
            const bool rendered = app.render(capture);
            if (rendered && view_ready) ++settled_frames; else if (!view_ready) settled_frames = 0;
            app.policy.rendered(now_seconds());
            if (!capture.empty() && rendered) {
                if (testing) smoke.captured = true;
                if (!options.capture.empty()) { captured_output = true; break; }
            }
        }
        if ((!options.capture.empty() || testing) && now_seconds()-started > 45) { success = false; break; }
        if(app.view.app_actions.recovery_preparing&&!app.closing)app.consume_app_actions();
        double delay = app.policy.wait_seconds(now_seconds(), app.device_retry_at);
        if (testing) delay = std::min(delay, std::max(0.001, smoke.start+2.0-now_seconds()));
        if (!options.capture.empty()) delay = std::min(delay, 0.25);
        if(app.view.app_actions.recovery_preparing)delay=std::min(delay,0.1);
        waiter.wait(delay);
    }
    if (testing && !smoke.finished) success = false;
    if (!options.capture.empty() && !captured_output) success = false;
    if (app.session) { app.session->begin_shutdown(); app.session.reset(); }
    if (app.view.hotkey_available) UnregisterHotKey(app.window, restore_hotkey);
    ImGui_ImplDX9_Shutdown(); ImGui_ImplWin32_Shutdown(); app.initialized = false;
    ImPlot::DestroyContext(); ImGui::DestroyContext();
    app.device->Release(); app.d3d->Release();
    if (IsWindow(app.window)) DestroyWindow(app.window);
    UnregisterClassW(window_class, instance); active = nullptr;
    return success ? 0 : 5;
}
