// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_startup.h"
#include "profile_catalog.h"
#include "session.h"
#include "policy_workspace.h"
#include <windows.h>
#include <sddl.h>
#include <taskschd.h>
#include <winevt.h>
#include <shlobj.h>
#include <filesystem>
#include <algorithm>
#include <memory>
#include <stdexcept>

namespace druta::ui {
namespace {
void require(bool success, const char* reason) {
    if (!success) throw std::runtime_error(reason);
}
void check(HRESULT result, const char* operation) {
    if (FAILED(result)) throw std::runtime_error(std::string(operation) + " (Windows error " +
        std::to_string(static_cast<unsigned long>(result)) + ")");
}
template<class T> class ComPtr {
    T* value_ = nullptr;
public:
    ComPtr() = default;
    ComPtr(const ComPtr&) = delete;
    ~ComPtr() { if (value_) value_->Release(); }
    T** put() { return &value_; }
    T* operator->() const { return value_; }
};
// Task Scheduler 2 and the modern event log are optional on legacy Windows.
// Resolve their system DLLs only when a startup operation needs them.
class SystemModule {
    HMODULE library_;
public:
    explicit SystemModule(const wchar_t* name) {
        wchar_t system[MAX_PATH]{};
        const auto size = GetSystemDirectoryW(system,MAX_PATH);
        require(size && size < MAX_PATH,"Cannot locate the Windows system directory");
        library_ = LoadLibraryW((std::wstring(system) + L"\\" + name).c_str());
        require(library_ != nullptr,"Windows sign-in profiles are unavailable on this system");
    }
    ~SystemModule() { FreeLibrary(library_); }
    template<class F> F function(const char* name) const {
        auto address = GetProcAddress(library_,name);
        require(address != nullptr,"A Windows sign-in profile API is unavailable");
        return reinterpret_cast<F>(address);
    }
};
struct Automation {
    SystemModule ole{L"ole32.dll"}, automation{L"oleaut32.dll"};
    decltype(&CoInitializeEx) init = ole.function<decltype(init)>("CoInitializeEx");
    decltype(&CoUninitialize) uninit = ole.function<decltype(uninit)>("CoUninitialize");
    decltype(&CoCreateInstance) create = ole.function<decltype(create)>("CoCreateInstance");
    decltype(&SysAllocStringLen) allocate = automation.function<decltype(allocate)>("SysAllocStringLen");
    decltype(&SysFreeString) free = automation.function<decltype(free)>("SysFreeString");
};
const Automation& automation() { static Automation api; return api; }
class Bstr {
    BSTR value_;
public:
    explicit Bstr(const std::wstring& text) : value_(automation().allocate(text.data(),static_cast<UINT>(text.size()))) {
        if (!value_) throw std::bad_alloc();
    }
    Bstr() : value_(nullptr) {}
    Bstr(const Bstr&) = delete;
    ~Bstr() { automation().free(value_); }
    operator BSTR() const { return value_; }
    BSTR* put() { return &value_; }
};
struct ComApartment {
    HRESULT init = automation().init(nullptr, COINIT_APARTMENTTHREADED);
    ComApartment() { if (init != RPC_E_CHANGED_MODE) check(init, "Initialize Task Scheduler"); }
    ~ComApartment() { if (SUCCEEDED(init)) automation().uninit(); }
};
std::wstring current_sid() {
    HANDLE raw = nullptr;
    require(OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw) != 0, "Cannot identify the current Windows account");
    std::unique_ptr<void,decltype(&CloseHandle)> token(raw,CloseHandle);
    DWORD size = 0;
    GetTokenInformation(token.get(), TokenUser, nullptr, 0, &size);
    std::vector<BYTE> data(size);
    require(size && GetTokenInformation(token.get(),TokenUser,data.data(),size,&size), "Cannot read account SID");
    LPWSTR sid = nullptr;
    require(ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(data.data())->User.Sid,&sid) != 0, "Cannot format account SID");
    std::unique_ptr<void,decltype(&LocalFree)> memory(sid,LocalFree);
    return sid;
}
std::wstring executable() {
    std::wstring path(32768,0);
    auto n = GetModuleFileNameW(nullptr,path.data(),static_cast<DWORD>(path.size()));
    require(n && n < path.size(), "Cannot locate this Druta installation");
    path.resize(n);
    return path;
}
struct Scheduler {
    ComApartment apartment;
    ComPtr<ITaskService> service;
    ComPtr<ITaskFolder> folder;
    std::wstring sid = current_sid();
    Bstr name{L"Druta Native profile startup " + sid};
    VARIANT empty{};
    Scheduler() {
        check(automation().create(CLSID_TaskScheduler,nullptr,CLSCTX_INPROC_SERVER,IID_ITaskService,
                    reinterpret_cast<void**>(service.put())), "Open Task Scheduler");
        check(service->Connect(empty,empty,empty,empty), "Connect to Task Scheduler");
        check(service->GetFolder(Bstr(L"\\"),folder.put()), "Open Task Scheduler folder");
    }
};
std::wstring xml_escape(const std::wstring& raw) {
    std::wstring result;
    for (wchar_t c : raw) {
        switch (c) {
        case L'&': result += L"&amp;"; break;
        case L'<': result += L"&lt;"; break;
        case L'>': result += L"&gt;"; break;
        default: result += c;
        }
    }
    return result;
}
struct EventApi {
    SystemModule module{L"wevtapi.dll"};
    decltype(&EvtQuery) query = module.function<decltype(query)>("EvtQuery");
    decltype(&EvtNext) next = module.function<decltype(next)>("EvtNext");
    decltype(&EvtCreateRenderContext) context = module.function<decltype(context)>("EvtCreateRenderContext");
    decltype(&EvtRender) render = module.function<decltype(render)>("EvtRender");
    decltype(&EvtClose) close = module.function<decltype(close)>("EvtClose");
};
const EventApi& events() { static EventApi api; return api; }
struct EventHandle {
    EVT_HANDLE handle;
    explicit EventHandle(EVT_HANDLE h) : handle(h) { require(h != nullptr,"Windows boot evidence is unavailable"); }
    ~EventHandle() { events().close(handle); }
};
struct EventStamp { ULONGLONG record, time; };
EventStamp latest_boot_event(const wchar_t* query) {
    EventHandle search(events().query(nullptr,L"System",query,EvtQueryChannelPath | EvtQueryReverseDirection));
    EVT_HANDLE raw = nullptr;
    DWORD found = 0;
    require(events().next(search.handle,1,&raw,3000,0,&found) && found == 1,"Windows boot evidence is missing");
    EventHandle event(raw), context(events().context(0,nullptr,EvtRenderContextSystem));
    DWORD bytes = 0, count = 0;
    events().render(context.handle,event.handle,EvtRenderEventValues,0,nullptr,&bytes,&count);
    require(bytes && bytes <= 65536,"Windows boot evidence has an invalid size");
    std::vector<EVT_VARIANT> values((bytes + sizeof(EVT_VARIANT) - 1) / sizeof(EVT_VARIANT));
    require(events().render(context.handle,event.handle,EvtRenderEventValues,bytes,values.data(),&bytes,&count) &&
        count > EvtSystemEventRecordId && values[EvtSystemEventRecordId].Type == EvtVarTypeUInt64 &&
        values[EvtSystemTimeCreated].Type == EvtVarTypeFileTime,"Windows boot evidence is unreadable");
    return {values[EvtSystemEventRecordId].UInt64Val, values[EvtSystemTimeCreated].FileTimeVal};
}
StartupBoot windows_boot() {
    auto start = latest_boot_event(L"*[System[Provider[@Name='Microsoft-Windows-Kernel-General'] and EventID=12]]");
    auto status = latest_boot_event(L"*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and EventID=20]]");
    if (status.record < start.record || status.time < start.time || status.time-start.time > 1200000000ULL)
        throw std::runtime_error("Windows shutdown evidence does not belong to the current boot");
    auto good = latest_boot_event(L"*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and EventID=20] and "
        L"EventData[Data[@Name='LastShutdownGood']='true' and Data[@Name='LastBootGood']='true']]");
    return {std::to_string(start.time),status.record == good.record};
}
std::filesystem::path state_dir() {
    wchar_t local[MAX_PATH]{};
    check(SHGetFolderPathW(nullptr,CSIDL_LOCAL_APPDATA,nullptr,SHGFP_TYPE_CURRENT,local),"Locate Druta's sign-in journal");
    auto dir = std::filesystem::path(local)/L"Druta";
    std::filesystem::create_directories(dir);
    return dir;
}
// Deny sharing while reading/updating the durable claim so two hosts cannot
// queue the same sign-in action, including when invoked outside Task Scheduler.
struct Journal {
    std::filesystem::path dir = state_dir();
    std::filesystem::path path = dir/L"native-startup-attempt.json";
    HANDLE lock = CreateFileW((dir/L"native-startup.lock").c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
    Journal() { require(lock != INVALID_HANDLE_VALUE, "Another window is configuring or applying the sign-in profile"); }
    ~Journal() { CloseHandle(lock); }
    ProfileJson read() const {
        return std::filesystem::exists(path) ? read_profile(path.wstring()) : ProfileJson::object();
    }
    void save(const ProfileJson& json) const { write_profile(path.wstring(),json); }
};
std::string active_claim;
} // namespace

std::string startup_profile_reason(const ProfileDescription& d, const std::string& uuid, const std::string& slot) {
    if (!d.valid) return d.error.empty() ? "Invalid profile" : d.error;
    if (uuid.empty() || slot.empty()) return "A GPU with a known UUID and PCI slot is required";
    if ((d.uuid && *d.uuid != uuid) || (d.slot && *d.slot != slot)) return "The selected profile belongs to another GPU";
    // Keep the released manual-only scopes manual, including their native
    // counterparts. Controller presets may require maintenance while hidden.
    bool manual = d.scope == ProfileScope::combined || d.scope == ProfileScope::fan ||
        d.scope == ProfileScope::python_rails || d.scope == ProfileScope::power_integral ||
        d.scope == ProfileScope::icx || std::any_of(d.targets.begin(),d.targets.end(),[](const auto& t){
            return t.compare(0,5,"rail:") == 0 || t == "clock-gating:3" || t == "i2c:controller";
        });
    return manual ? "This profile includes controls that require manual application" : std::string{};
}
void validate_startup_profile(const StartupProfile& profile) {
    require(!profile.id.empty() && !profile.name.empty(),"Select a named profile");
    auto reason = startup_profile_reason(inspect_profile(profile.document),profile.uuid,profile.slot);
    if (!reason.empty()) throw std::runtime_error(reason);
}
void StartupProfileLaunch::start(Session& session, std::optional<StartupProfile> approved, bool hidden) {
    profile_ = std::move(approved);
    generation_ = session.snapshot()->generation;
    actions_.reset();
    if (hidden && !profile_) session.set_paused(true);
}
void StartupProfileLaunch::resume(Session& session) {
    session.set_paused(false);
    session.request_poll();
}
std::optional<StartupResult> StartupProfileLaunch::tick(Session& session, bool hidden, const PolicyAnnotations& annotations,
                                                        std::uint64_t annotation_action_log, bool annotation_ok) {
    if (!profile_) return {};
    const auto current = session.snapshot();
    if (current->opening || current->busy) return {};
    StartupResult result{false,{}};
    if (current->generation != generation_ || !current->data.connected ||
        current->data.identity.uuid != profile_->uuid || current->selected_slot != profile_->slot) {
        result.message = "Sign-in profile skipped. The selected GPU is unavailable or changed.";
    } else if (!actions_) {
        Command command;
        command.operation = Command::Operation::apply_catalog_profile;
        command.catalog_profile = std::make_shared<const ProfileJson>(profile_->document);
        command.annotation_snapshot = std::make_shared<const PolicyAnnotations>(annotations);
        if (session.submit(std::move(command),current->generation,true)) {
            actions_ = current->completed_actions;
            return {};
        }
        result.message = "Sign-in profile could not be queued.";
    } else {
        if (current->completed_actions <= *actions_) return {};
        result.ok = current->last_action_operation == Command::Operation::apply_catalog_profile && current->last_action_ok &&
                    (annotation_action_log != current->last_action_log_id || annotation_ok);
        result.message = result.ok ? "Loaded " + profile_->name + " at Windows sign-in." :
                                     "Sign-in profile failed. See the operation log.";
    }
    profile_.reset();
    if (hidden) session.set_paused(true);
    return result;
}
std::optional<StartupProfile> registered_startup_profile() {
    Scheduler scheduler;
    ComPtr<IRegisteredTask> task;
    auto hr = scheduler.folder->GetTask(scheduler.name,task.put());
    if (hr == HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND) || hr == HRESULT_FROM_WIN32(ERROR_PATH_NOT_FOUND)) return {};
    check(hr,"Read the registered sign-in task");
    VARIANT_BOOL enabled = VARIANT_FALSE;
    check(task->get_Enabled(&enabled),"Read sign-in task status");
    if (!enabled) return {};
    ComPtr<ITaskDefinition> definition;
    ComPtr<IRegistrationInfo> info;
    Bstr data;
    check(task->get_Definition(definition.put()),"Read sign-in task definition");
    check(definition->get_RegistrationInfo(info.put()),"Read sign-in profile binding");
    check(info->get_Documentation(data.put()),"Read sign-in profile");
    const std::wstring wide(data ? static_cast<BSTR>(data) : L"");
    require(wide.size() <= profile_max_bytes && std::all_of(wide.begin(),wide.end(),[](wchar_t c){return c < 128;}),
            "Invalid sign-in profile encoding");
    std::string bytes(wide.size(),0);
    std::transform(wide.begin(),wide.end(),bytes.begin(),[](wchar_t c){ return static_cast<char>(c); });
    auto json = ProfileJson::parse(bytes);
    require(json.at("version") == 1,"Unsupported sign-in profile version");
    StartupProfile profile{json.at("id"),json.at("name"),json.at("uuid"),json.at("slot"),json.at("profile")};
    validate_startup_profile(profile);
    return profile;
}
std::wstring startup_task_xml(const StartupProfile& profile,const std::wstring& exe,const std::wstring& core,const std::wstring& sid) {
    validate_startup_profile(profile);
    const auto json = ProfileJson{{"version",1},{"id",profile.id},{"name",profile.name},{"uuid",profile.uuid},
        {"slot",profile.slot},{"profile",profile.document}}.dump(-1,' ',true);
    require(json.size() <= profile_max_bytes,"The selected profile exceeds the sign-in task size limit");
    return LR"(<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>Apply the selected Druta profile at sign-in</Description><Documentation>)" +
        xml_escape(std::wstring(json.begin(),json.end())) + L"</Documentation></RegistrationInfo><Triggers><LogonTrigger><UserId>" + sid +
        L"</UserId><Delay>PT30S</Delay></LogonTrigger></Triggers><Principals><Principal id=\"User\"><UserId>" + sid +
        L"</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>"
        L"<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        L"<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>"
        L"<Actions Context=\"User\"><Exec><Command>" + xml_escape(exe) +
        L"</Command><Arguments>--startup-profile --core &quot;" + xml_escape(core) +
        L"&quot;</Arguments><WorkingDirectory>" + xml_escape(std::filesystem::path(exe).parent_path().wstring()) +
        L"</WorkingDirectory></Exec></Actions></Task>";
}

void register_startup_profile(const StartupProfile& profile, const std::wstring& core) {
    require(!core.empty(),"Cannot locate the NVIDIA core used by this installation");
    const auto dll = std::filesystem::absolute(core);
    require(std::filesystem::is_regular_file(dll),"Cannot locate the NVIDIA core used by this installation");
    Scheduler scheduler;
    const auto xml = startup_task_xml(profile,executable(),dll.wstring(),scheduler.sid);
    ComPtr<IRegisteredTask> result;
    check(scheduler.folder->RegisterTask(scheduler.name,Bstr(xml),TASK_CREATE_OR_UPDATE,scheduler.empty,scheduler.empty,
        TASK_LOGON_INTERACTIVE_TOKEN,scheduler.empty,result.put()),"Register the selected profile at Windows sign-in");
}
void unregister_startup_profile() {
    Scheduler scheduler;
    auto hr = scheduler.folder->DeleteTask(scheduler.name,0);
    if (hr != HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND) && hr != HRESULT_FROM_WIN32(ERROR_PATH_NOT_FOUND))
        check(hr,"Remove the Windows sign-in task");
}
std::string claim_startup_boot(ProfileJson& journal, const StartupBoot& boot) {
    require(!boot.id.empty(),"Windows boot identity is unavailable");
    if (journal.value("attempted_boot",std::string{}) == boot.id) return "Sign-in profile already processed on this boot";
    const bool interrupted = journal.value("in_progress",false);
    journal = {{"attempted_boot",boot.id},{"in_progress",boot.clean && !interrupted}};
    if (!boot.clean) return "Windows reports an abnormal previous shutdown or boot";
    if (interrupted) return "The previous sign-in profile application was interrupted";
    return {};
}
void claim_profile_startup() {
    const auto boot = windows_boot();
    Journal file;
    auto journal = file.read();
    const auto reason = claim_startup_boot(journal,boot);
    file.save(journal);
    if (!reason.empty()) throw std::runtime_error(reason);
    active_claim = boot.id;
}
void finish_profile_startup() {
    if (active_claim.empty()) return;
    Journal file;
    auto journal = file.read();
    if (journal.value("attempted_boot",std::string{}) == active_claim) {
        journal["in_progress"] = false;
        file.save(journal);
    }
    active_claim.clear();
}
}
