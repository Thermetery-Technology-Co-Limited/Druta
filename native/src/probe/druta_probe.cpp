// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// druta-probe: a read-only capability report for one Windows installation.
//
// It answers, for the machine it runs on, what the native core could do there:
// the Windows version and driver model, every Windows export Druta binds,
// every NVAPI interface id and NVML export Druta uses, and which GPUs the two
// driver libraries enumerate. It changes no setting, issues no driver
// escape and patches nothing: the D3DKMTEscape entry bytes are only read.
// It imports only kernel32 and the C runtime, so it starts on any Windows
// from XP up and names what is missing instead of failing to load.
//
// Output goes to the console and to druta-probe-report.txt beside the EXE
// (or in %TEMP%). Send that file back.
#include <windows.h>

#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "core/slot.h"
#include "win/dynlib.h"

namespace {

using druta::win::DynLib;

// ---- report -------------------------------------------------------------------------------------

class Report {
public:
    ~Report() {
        if (file_) std::fclose(file_);
    }
    void open(const std::wstring& path) {
        file_ = _wfopen(path.c_str(), L"w");
        path_ = path;
    }
    bool is_open() const { return file_ != nullptr; }
    const std::wstring& path() const { return path_; }

    void line(const char* format, ...) {
        char buffer[4096];
        va_list args;
        va_start(args, format);
        std::vsnprintf(buffer, sizeof buffer, format, args);
        va_end(args);
        std::printf("%s\n", buffer);
        if (file_) {
            std::fprintf(file_, "%s\n", buffer);
            std::fflush(file_);
        }
    }
    void section(const char* title) {
        line("");
        line("== %s", title);
    }

private:
    FILE* file_ = nullptr;
    std::wstring path_;
};

std::string narrow(const std::wstring& text) {
    if (text.empty()) return "";
    int size = ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(size > 0 ? size : 1), '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, &out[0], size, nullptr, nullptr);
    out.resize(out.empty() ? 0 : out.size() - 1);
    return out;
}

std::wstring exe_directory() {
    wchar_t buffer[1024] = {};
    DWORD length = ::GetModuleFileNameW(nullptr, buffer, 1024);
    std::wstring path(buffer, length);
    std::size_t slash = path.find_last_of(L"\\/");
    return slash == std::wstring::npos ? L"." : path.substr(0, slash);
}

std::wstring temp_directory() {
    wchar_t buffer[1024] = {};
    DWORD length = ::GetTempPathW(1024, buffer);
    return length ? std::wstring(buffer, length) : L".";
}

// ---- Windows version and process ---------------------------------------------------------------

void report_windows(Report& out) {
    out.section("Windows");
    // RtlGetVersion is not subject to manifest-based version lies and exists since XP.
    OSVERSIONINFOEXW info{};
    info.dwOSVersionInfoSize = sizeof info;
    bool have_version = false;
    if (HMODULE ntdll = ::GetModuleHandleW(L"ntdll.dll")) {
        using RtlGetVersionFn = LONG(NTAPI*)(OSVERSIONINFOEXW*);
        auto rtl_get_version = reinterpret_cast<RtlGetVersionFn>(
            reinterpret_cast<void*>(::GetProcAddress(ntdll, "RtlGetVersion")));
        if (rtl_get_version && rtl_get_version(&info) == 0) have_version = true;
    }
    if (!have_version) {
        // GetVersionExW is deprecated since Windows 8.1, but it is the only
        // fallback on an image whose ntdll lacks RtlGetVersion.
        using GetVersionExFn = BOOL(WINAPI*)(OSVERSIONINFOW*);
        if (HMODULE kernel = ::GetModuleHandleW(L"kernel32.dll")) {
            auto get_version = reinterpret_cast<GetVersionExFn>(
                reinterpret_cast<void*>(::GetProcAddress(kernel, "GetVersionExW")));
            have_version = get_version && get_version(reinterpret_cast<OSVERSIONINFOW*>(&info)) != 0;
        }
    }
    if (have_version) {
        out.line("version: %lu.%lu build %lu %s (product type %u)", info.dwMajorVersion, info.dwMinorVersion,
                 info.dwBuildNumber, narrow(info.szCSDVersion).c_str(), info.wProductType);
    } else {
        out.line("version: unavailable");
    }
    SYSTEM_INFO system{};
    ::GetNativeSystemInfo(&system);
    const char* machine = system.wProcessorArchitecture == 9 ? "x64" : system.wProcessorArchitecture == 0 ? "x86" : "other";
    // IsWow64Process does not exist on XP before SP2; its absence is reported, not assumed.
    const char* wow64 = "unknown (no IsWow64Process)";
    if (HMODULE kernel = ::GetModuleHandleW(L"kernel32.dll")) {
        using IsWow64Fn = BOOL(WINAPI*)(HANDLE, PBOOL);
        auto is_wow64 = reinterpret_cast<IsWow64Fn>(reinterpret_cast<void*>(::GetProcAddress(kernel, "IsWow64Process")));
        BOOL flag = FALSE;
        if (is_wow64)
            wow64 = is_wow64(::GetCurrentProcess(), &flag) ? (flag ? "yes" : "no") : "unknown (IsWow64Process failed)";
    }
    out.line("machine: %s; this probe: %u-bit process; WOW64: %s", machine, static_cast<unsigned>(sizeof(void*) * 8),
             wow64);
    DynLib shell(L"shell32.dll");
    using IsAdminFn = BOOL(WINAPI*)();
    auto is_admin = shell.get<IsAdminFn>("IsUserAnAdmin");
    out.line("administrator: %s", is_admin ? (is_admin() ? "yes" : "no") : "unknown (no IsUserAnAdmin)");
}

// ---- Windows exports Druta binds ----------------------------------------------------------------

struct Export {
    const wchar_t* dll;
    const char* name;
    const char* purpose;
};

// The graphics kernel exports decide the driver model and which escape path exists.
const Export kDriverModelExports[] = {
    {L"gdi32.dll", "D3DKMTOpenAdapterFromHdc", "WDDM (Vista+): adapter handle from a display DC"},
    {L"gdi32.dll", "D3DKMTOpenAdapterFromGdiDisplayName", "WDDM (Vista+): adapter handle from a display name"},
    {L"gdi32.dll", "D3DKMTEnumAdapters", "WDDM 1.1 (Windows 7+): adapter enumeration"},
    {L"gdi32.dll", "D3DKMTEnumAdapters2", "WDDM 1.2 (Windows 8+): what Druta's transport capture uses"},
    {L"gdi32.dll", "D3DKMTQueryAdapterInfo", "adapter identity for driver escapes"},
    {L"gdi32.dll", "D3DKMTEscape", "driver escapes"},
    {L"gdi32.dll", "D3DKMTCloseAdapter", "driver escapes"},
    {L"gdi32.dll", "D3DKMTOpenAdapterFromLuid", "adapter handle from a LUID (not used; listed as evidence)"},
};

const Export kWindowsExports[] = {
    {L"kernel32.dll", "GetTickCount64", "startup recovery (Vista+)"},
    {L"kernel32.dll", "GetSystemDirectoryW", "driver library lookup"},
    {L"kernel32.dll", "CreateMutexW", "single-instance checks"},
    {L"kernel32.dll", "GetProcessTimes", "startup recovery"},
    {L"kernel32.dll", "GetSystemTimeAsFileTime", "startup recovery"},
    {L"kernel32.dll", "AttachConsole", "command-line output"},
    {L"kernel32.dll", "VirtualProtect", "transport capture"},
    {L"kernel32.dll", "FlushInstructionCache", "transport capture"},
    {L"kernel32.dll", "AddDllDirectory", "LOAD_LIBRARY_SEARCH_* flags (KB2533623 on 7)"},
    {L"kernel32.dll", "IsWow64Process", "process architecture"},
    {L"user32.dll", "GetDpiForSystem", "Windows 10+ DPI; GetDeviceCaps fallback"},
    {L"user32.dll", "SetProcessDPIAware", "Vista+ DPI awareness"},
    {L"user32.dll", "GetDC", "DPI fallback"},
    {L"user32.dll", "EnumDisplayDevicesW", "display-name adapter fallback (D3DKMTOpenAdapterFromGdiDisplayName)"},
    {L"user32.dll", "SetWindowLongPtrW", "window handling"},
    {L"user32.dll", "SendMessageTimeoutW", "window handling"},
    {L"user32.dll", "CreateWindowExW", "shutdown notification window"},
    {L"user32.dll", "MessageBoxW", "error dialogs"},
    {L"shcore.dll", "SetProcessDpiAwareness", "Windows 8.1+ DPI awareness"},
    {L"gdi32.dll", "GetDeviceCaps", "DPI fallback"},
    {L"comdlg32.dll", "GetOpenFileNameW", "file dialogs"},
    {L"comdlg32.dll", "GetSaveFileNameW", "file dialogs"},
    {L"shell32.dll", "IsUserAnAdmin", "administrator detection"},
    {L"shell32.dll", "SHGetFolderPathW", "Program Files lookup"},
    {L"setupapi.dll", "SetupDiGetClassDevsW", "GPU device restart"},
    {L"setupapi.dll", "SetupDiGetDeviceRegistryPropertyW", "GPU device restart"},
    {L"setupapi.dll", "SetupDiOpenDeviceInfoW", "GPU device restart (SetupAPI path)"},
    {L"setupapi.dll", "SetupDiSetClassInstallParamsW", "GPU device restart (SetupAPI path)"},
    {L"setupapi.dll", "SetupDiCallClassInstaller", "GPU device restart (SetupAPI path)"},
    {L"cfgmgr32.dll", "CM_Locate_DevNodeW", "GPU device restart"},
    {L"cfgmgr32.dll", "CM_Get_DevNode_Status", "GPU device restart"},
    {L"advapi32.dll", "OpenSCManagerW", "nvtune service status"},
    {L"advapi32.dll", "QueryServiceStatus", "nvtune service status"},
    {L"advapi32.dll", "RegOpenKeyExW", "registry overrides"},
    {L"dwmapi.dll", "DwmIsCompositionEnabled", "Vista+ compositor"},
    {L"d3d9.dll", "Direct3DCreate9", "Direct3D 9 renderer (XP+)"},
    {L"d3d11.dll", "D3D11CreateDevice", "Direct3D 11 renderer (Vista SP2 platform update+)"},
    {L"dxgi.dll", "CreateDXGIFactory", "Direct3D 11 renderer"},
    {L"dxgi.dll", "CreateDXGIFactory1", "Direct3D 11 renderer"},
};

void report_exports(Report& out, const char* title, const Export* table, std::size_t count) {
    out.section(title);
    std::vector<std::pair<std::wstring, DynLib>> loaded;
    std::size_t missing = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const Export& entry = table[i];
        DynLib* lib = nullptr;
        for (auto& item : loaded)
            if (item.first == entry.dll) lib = &item.second;
        if (!lib) {
            loaded.emplace_back(entry.dll, DynLib(entry.dll));
            lib = &loaded.back().second;
        }
        const char* state;
        char detail[64] = "";
        if (!lib->ok()) {
            state = "DLL not loadable";
            std::snprintf(detail, sizeof detail, " (error %lu)", lib->error());
        } else {
            state = lib->has(entry.name) ? "present" : "missing";
        }
        if (std::strcmp(state, "present") != 0) ++missing;
        out.line("%s!%s: %s%s - %s", narrow(entry.dll).c_str(), entry.name, state, detail, entry.purpose);
    }
    out.line("%u of %u unavailable", static_cast<unsigned>(missing), static_cast<unsigned>(count));
}

void report_driver_model(Report& out) {
    report_exports(out, "Graphics kernel exports (driver model)", kDriverModelExports,
                   sizeof kDriverModelExports / sizeof kDriverModelExports[0]);
    DynLib gdi(L"gdi32.dll");
    const char* model;
    if (!gdi.ok() || !gdi.has("D3DKMTEscape"))
        model = "XPDM: no D3DKMT escape path; driver escapes are not possible on this image";
    else if (gdi.has("D3DKMTEnumAdapters2"))
        model = "WDDM 1.2 or later: the transport capture path Druta uses today exists";
    else if (gdi.has("D3DKMTEnumAdapters"))
        model = "WDDM 1.1 (Windows 7): escapes exist; adapters must be opened by display name or DC";
    else
        model = "WDDM 1.0 (Vista): escapes exist; adapters must be opened by display name or DC";
    out.line("-> %s", model);
}

// ---- D3DKMTEscape entry bytes -------------------------------------------------------------------
//
// The native core will patch the first bytes of whatever address GetProcAddress returns for
// D3DKMTEscape (x64: 14-byte FF 25 absolute jump; x86: 5-byte E9 rel32). This section records, read
// only, which module that address lives in and what the first 16 bytes are, so a report from an image
// can show whether such an overwrite stays inside one stub (DESIGN-TRANSPORT.md section 5, 5 and 6).

// The module containing an address, or "" when it cannot be named. GetModuleHandleExW does not exist
// on XP RTM, so it is resolved at run time; the refcount is not changed.
std::string module_of(const void* address, std::string& why) {
    why.clear();
    HMODULE kernel = ::GetModuleHandleW(L"kernel32.dll");
    using GetModuleHandleExFn = BOOL(WINAPI*)(DWORD, LPCWSTR, HMODULE*);
    auto get_module = kernel ? reinterpret_cast<GetModuleHandleExFn>(
                                   reinterpret_cast<void*>(::GetProcAddress(kernel, "GetModuleHandleExW")))
                             : nullptr;
    if (!get_module) {
        why = "unknown (no GetModuleHandleExW)";
        return "";
    }
    HMODULE module = nullptr;
    const DWORD flags = 0x4 /* FROM_ADDRESS */ | 0x2 /* UNCHANGED_REFCOUNT */;
    if (!get_module(flags, static_cast<LPCWSTR>(address), &module) || !module) {
        char text[64];
        std::snprintf(text, sizeof text, "unknown (GetModuleHandleExW error %lu)", ::GetLastError());
        why = text;
        return "";
    }
    wchar_t buffer[1024] = {};
    DWORD length = ::GetModuleFileNameW(module, buffer, 1024);
    if (!length) {
        char text[64];
        std::snprintf(text, sizeof text, "unknown (GetModuleFileNameW error %lu)", ::GetLastError());
        why = text;
        return "";
    }
    return narrow(std::wstring(buffer, length));
}

std::string describe_module(const void* address) {
    std::string why;
    std::string name = module_of(address, why);
    return name.empty() ? why : name;
}

// Read up to `size` bytes of this process's memory without touching page protection. Returns how many
// leading bytes were readable (a read that would cross into an unreadable page stops short).
std::size_t read_own_memory(const void* address, unsigned char* buffer, std::size_t size) {
    SIZE_T done = 0;
    if (::ReadProcessMemory(::GetCurrentProcess(), address, buffer, size, &done) && done == size) return size;
    std::size_t count = 0;
    for (; count < size; ++count) {
        done = 0;
        if (!::ReadProcessMemory(::GetCurrentProcess(), static_cast<const unsigned char*>(address) + count,
                                 buffer + count, 1, &done) ||
            done != 1)
            break;
    }
    return count;
}

// A plain observation of the first instruction; no decision is taken from it.
const char* classify_entry(const unsigned char* bytes, std::size_t count) {
    if (count >= 1 && bytes[0] == 0xE9) return "jmp rel32 (E9)";
    if (count >= 1 && bytes[0] == 0xEB) return "short jmp rel8 (EB)";
    if (count >= 2 && bytes[0] == 0xFF && bytes[1] == 0x25)
        return sizeof(void*) == 8 ? "jmp [rip+disp32] (FF 25)" : "jmp [abs32] (FF 25)";
    if (count >= 3 && bytes[0] == 0x48 && bytes[1] == 0xFF && bytes[2] == 0x25) return "jmp [rip+disp32] (48 FF 25)";
    if (count >= 2 && bytes[0] == 0x8B && bytes[1] == 0xFF) return "mov edi,edi hot-patch prologue (8B FF)";
    if (count >= 1 && bytes[0] == 0x55) return "push ebp prologue (55)";
    if (count >= 3 && bytes[0] == 0x4C && bytes[1] == 0x8B && bytes[2] == 0xD1) return "mov r10,rcx syscall stub (4C 8B D1)";
    if (count >= 1 && bytes[0] == 0xB8) return "mov eax,imm32 (B8), as in an x86 syscall stub";
    if (count >= 1 && bytes[0] == 0xCC) return "int3 (CC)";
    return "other";
}

void report_escape_entry(Report& out) {
    out.section("D3DKMTEscape entry (read-only)");
    DynLib gdi(L"gdi32.dll");
    if (!gdi.ok()) {
        out.line("gdi32.dll: not loadable (error %lu)", gdi.error());
        return;
    }
    const void* address = gdi.get<void*>("D3DKMTEscape");
    if (!address) {
        out.line("gdi32.dll!D3DKMTEscape: missing; nothing to inspect");
        return;
    }
    out.line("address: %p", address);
    out.line("module: %s", describe_module(address).c_str());
    unsigned char bytes[16] = {};
    std::size_t count = read_own_memory(address, bytes, sizeof bytes);
    std::string hex;
    for (std::size_t i = 0; i < count; ++i) {
        char item[4];
        std::snprintf(item, sizeof item, i ? " %02X" : "%02X", bytes[i]);
        hex += item;
    }
    if (count == 0) {
        out.line("first 16 bytes: unreadable (error %lu)", ::GetLastError());
        return;
    }
    out.line("first %u bytes: %s", static_cast<unsigned>(count), hex.c_str());
    out.line("first instruction looks like: %s", classify_entry(bytes, count));
    // Where a leading jump goes, by address arithmetic and one more read-only read; observation only.
    const unsigned char* base = static_cast<const unsigned char*>(address);
    const void* target = nullptr;
    if (count >= 5 && bytes[0] == 0xE9) {
        std::int32_t rel = 0;
        std::memcpy(&rel, bytes + 1, sizeof rel);
        target = base + 5 + rel;
    } else {
        std::size_t prefix = (count >= 3 && bytes[0] == 0x48 && bytes[1] == 0xFF && bytes[2] == 0x25) ? 1 : 0;
        if (count >= prefix + 6 && bytes[prefix] == 0xFF && bytes[prefix + 1] == 0x25) {
            std::int32_t disp = 0;
            std::memcpy(&disp, bytes + prefix + 2, sizeof disp);
            const void* slot = sizeof(void*) == 8
                                   ? static_cast<const void*>(base + prefix + 6 + disp)
                                   : reinterpret_cast<const void*>(static_cast<std::uintptr_t>(static_cast<std::uint32_t>(disp)));
            void* pointer = nullptr;
            if (read_own_memory(slot, reinterpret_cast<unsigned char*>(&pointer), sizeof pointer) == sizeof pointer)
                target = pointer;
            else
                out.line("jump slot %p: unreadable", slot);
        }
    }
    if (target) out.line("jump target: %p in %s", target, describe_module(target).c_str());
}

// ---- D3DKMT adapter enumeration -----------------------------------------------------------------
//
// Local layouts (no DDK headers). Every D3DKMT entry point is NTSTATUS NTAPI fn(void*).

struct KmtAdapterInfo {
    std::uint32_t hAdapter;
    std::uint32_t LuidLow;
    std::int32_t LuidHigh;
    std::uint32_t NumOfSources;
    std::int32_t bPresentMoveRegionsPreferred;
};
struct KmtEnumAdapters2 {
    std::uint32_t NumAdapters;
    KmtAdapterInfo* pAdapters;
};
struct KmtEnumAdapters {
    std::uint32_t NumAdapters;
    KmtAdapterInfo Adapters[16];
};
struct KmtQueryAdapterInfo {
    std::uint32_t hAdapter;
    std::uint32_t Type;
    void* pPrivateDriverData;
    std::uint32_t PrivateDriverDataSize;
};
struct KmtCloseAdapter {
    std::uint32_t hAdapter;
};
struct KmtAdapterAddress {
    std::uint32_t BusNumber;
    std::uint32_t DeviceNumber;
    std::uint32_t FunctionNumber;
};
static_assert(sizeof(KmtAdapterInfo) == 20, "D3DKMT_ADAPTERINFO");
static_assert(sizeof(KmtEnumAdapters2) == (sizeof(void*) == 8 ? 16 : 8), "D3DKMT_ENUMADAPTERS2");
static_assert(sizeof(KmtEnumAdapters) == 324, "D3DKMT_ENUMADAPTERS");
static_assert(sizeof(KmtQueryAdapterInfo) == (sizeof(void*) == 8 ? 24 : 16), "D3DKMT_QUERYADAPTERINFO");
static_assert(sizeof(KmtCloseAdapter) == 4, "D3DKMT_CLOSEADAPTER");
static_assert(sizeof(KmtAdapterAddress) == 12, "D3DKMT_ADAPTERADDRESS");

constexpr std::uint32_t kKmtAdapterAddress = 6;  // KMTQAITYPE_ADAPTERADDRESS

unsigned long ntstatus_bits(LONG status) { return static_cast<unsigned long>(status) & 0xFFFFFFFFul; }

void report_kmt_adapters(Report& out) {
    out.section("D3DKMT adapters (read-only)");
    DynLib gdi(L"gdi32.dll");
    if (!gdi.ok()) {
        out.line("gdi32.dll: not loadable (error %lu)", gdi.error());
        return;
    }
    using KmtFn = LONG(NTAPI*)(void*);
    auto enum2 = gdi.get<KmtFn>("D3DKMTEnumAdapters2");
    auto enum1 = gdi.get<KmtFn>("D3DKMTEnumAdapters");
    auto query = gdi.get<KmtFn>("D3DKMTQueryAdapterInfo");
    auto close = gdi.get<KmtFn>("D3DKMTCloseAdapter");
    if (!enum2 && !enum1) {
        out.line("no D3DKMTEnumAdapters2 or D3DKMTEnumAdapters; enumeration not possible on this image");
        return;
    }
    if (!close) {
        // Enumeration opens handles; without a way to close them the probe does not open any.
        out.line("D3DKMTCloseAdapter: missing; not enumerating, because the opened handles could not be closed");
        return;
    }

    std::vector<KmtAdapterInfo> adapters;
    LONG status = 0;
    if (enum2) {
        KmtEnumAdapters2 request{};
        request.NumAdapters = 0;
        request.pAdapters = nullptr;
        LONG count_status = enum2(&request);
        std::uint32_t capacity = 16;
        if (count_status == 0 && request.NumAdapters > 0 && request.NumAdapters <= 1024) {
            out.line("D3DKMTEnumAdapters2 count query: %u adapter(s)", request.NumAdapters);
            capacity = request.NumAdapters;
        } else if (count_status == 0) {
            out.line("D3DKMTEnumAdapters2 count query: status 0 with count %u; using a 16-entry array",
                     request.NumAdapters);
        } else {
            out.line("D3DKMTEnumAdapters2 count query: NTSTATUS 0x%08lX; using a 16-entry array",
                     ntstatus_bits(count_status));
        }
        adapters.assign(capacity, KmtAdapterInfo{});
        request.NumAdapters = capacity;
        request.pAdapters = adapters.data();
        status = enum2(&request);
        if (status != 0) {
            out.line("D3DKMTEnumAdapters2: NTSTATUS 0x%08lX (no handles returned)", ntstatus_bits(status));
            return;
        }
        adapters.resize(request.NumAdapters < capacity ? request.NumAdapters : capacity);
        out.line("D3DKMTEnumAdapters2: %u adapter(s)", request.NumAdapters);
    } else {
        KmtEnumAdapters request{};
        request.NumAdapters = 16;
        status = enum1(&request);
        if (status != 0) {
            out.line("D3DKMTEnumAdapters (16 entries): NTSTATUS 0x%08lX (no handles returned)", ntstatus_bits(status));
            return;
        }
        std::uint32_t count = request.NumAdapters < 16 ? request.NumAdapters : 16;
        adapters.assign(request.Adapters, request.Adapters + count);
        out.line("D3DKMTEnumAdapters (16 entries): %u adapter(s)", request.NumAdapters);
    }

    for (std::size_t i = 0; i < adapters.size(); ++i) {
        const KmtAdapterInfo& item = adapters[i];
        char address[96];
        if (!query) {
            std::snprintf(address, sizeof address, "? (no D3DKMTQueryAdapterInfo)");
        } else {
            KmtAdapterAddress pci{};
            KmtQueryAdapterInfo request{};
            request.hAdapter = item.hAdapter;
            request.Type = kKmtAdapterAddress;
            request.pPrivateDriverData = &pci;
            request.PrivateDriverDataSize = sizeof pci;
            LONG st = query(&request);
            if (st == 0)
                std::snprintf(address, sizeof address, "%02X:%02X.%X", pci.BusNumber, pci.DeviceNumber,
                              pci.FunctionNumber);
            else
                std::snprintf(address, sizeof address, "? (type 6 NTSTATUS 0x%08lX)", ntstatus_bits(st));
        }
        out.line("adapter %u: handle=0x%08X luid=%08lX:%08X sources=%u pci=%s", static_cast<unsigned>(i),
                 item.hAdapter, static_cast<unsigned long>(static_cast<std::uint32_t>(item.LuidHigh)), item.LuidLow,
                 item.NumOfSources, address);
    }
    // Every handle the successful enumeration returned is closed, whatever the queries did.
    std::size_t close_failures = 0;
    for (const KmtAdapterInfo& item : adapters) {
        KmtCloseAdapter request{item.hAdapter};
        LONG st = close(&request);
        if (st != 0) {
            ++close_failures;
            out.line("D3DKMTCloseAdapter(0x%08X): NTSTATUS 0x%08lX", item.hAdapter, ntstatus_bits(st));
        }
    }
    out.line("closed %u of %u handle(s)", static_cast<unsigned>(adapters.size() - close_failures),
             static_cast<unsigned>(adapters.size()));
}

// ---- NVAPI --------------------------------------------------------------------------------------

struct Interface {
    std::uint32_t id;
    const char* name;
};

// Every QueryInterface id Druta binds (src/druta/nvbackend.py and registry_overrides.py).
const Interface kNvapiInterfaces[] = {
    {0x0150E828, "Initialize"},
    {0xE5AC921F, "EnumPhysicalGPUs"},
    {0x2DDFB66E, "GPU_GetPCIIdentifiers"},
    {0x6C2D048C, "GetErrorMessage"},
    {0x1BE0B8E5, "GPU_GetBusId"},
    {0x2A0A350F, "GPU_GetBusSlotId"},
    {0xD8265D24, "GPU_GetArchInfo"},
    {0xCEEE8E9F, "GPU_GetFullName"},
    {0xA561FD7D, "GPU_GetVbiosVersionString"},
    {0x2926AAAD, "SYS_GetDriverAndBranchVersion"},
    {0xE3640A56, "GPU_GetThermalSettings"},
    {0x65FE3AAD, "GPU_ThermalSensors (private)"},
    {0xDA141340, "GPU_GetCoolerSettings"},
    {0x891FA0AE, "GPU_SetCoolerLevels"},
    {0x8F6ED0FB, "GPU_RestoreCoolerSettings"},
    {0x5F608315, "GPU_GetTachReading"},
    {0x814B209F, "GPU_ClientFanCoolersGetControl"},
    {0x35AED5E8, "GPU_ClientFanCoolersGetStatus"},
    {0xA58971A5, "GPU_ClientFanCoolersSetControl"},
    {0x465F9BCF, "GPU_ClientVoltRailsGetStatus"},
    {0x0EDCF624E, "GPU_ClientPowerTopologyGetStatus"},
    {0x34206D86, "GPU_ClientPowerPoliciesGetInfo"},
    {0x70916171, "GPU_ClientPowerPoliciesGetStatus"},
    {0x60DED2ED, "GPU_GetDynamicPstatesInfoEx"},
    {0x927DA4F6, "GPU_GetCurrentPstate"},
    {0x025BFB10, "GPU_SetForcePstate (private)"},
    {0x7F7F4600, "GPU_PerfDecrease (private)"},
    {0x6FF81213, "GPU_GetPstates20"},
    {0x0F4DAE6B, "GPU_SetPstates20"},
    {0xDCB616C3, "GPU_GetAllClockFrequencies"},
    {0x1BD69F49, "GPU_GetAllClocks (private)"},
    {0x527FC458, "GPU_ClkMeasureFreq (private)"},
    {0x57F7CAAC, "GPU_GetRamType"},
    {0x21537AD4, "GPU_ClientVfpCurve (private)"},
    {0x23F1B133, "GPU_ClientBoostTableGet (private)"},
    {0x0733E009, "GPU_ClientBoostTableSet (private)"},
    {0xE440B867, "GPU_ClientBoostLock (private)"},
    {0x39442CFB, "GPU_ClientVfLockSet (private)"},
    {0x9DF23CA1, "GPU_ClientVoltCtrlGet (private)"},
    {0xB9306D9B, "GPU_ClientVoltCtrlSet (private)"},
    {0xA3070DB0, "GPU_ClientVoltRailsGetControl (private)"},
    {0x5D0634EE, "GPU_ClientVoltRailsAbs (private)"},
    {0xF58938F5, "GPU_ClientClkDomCtlGet (private)"},
    {0xD14B69CF, "GPU_ClientClkDomCtlSet (private)"},
    {0xFC13EE11, "GPU_VbiosImageGet (private, registry overrides)"},
};

struct GpuArchInfo {
    std::uint32_t version;
    std::uint32_t architecture;
    std::uint32_t implementation;
    std::uint32_t revision;
};

void report_nvapi(Report& out) {
    out.section("NVAPI");
    const wchar_t* name = sizeof(void*) == 8 ? L"nvapi64.dll" : L"nvapi.dll";
    DynLib nvapi(name);
    if (!nvapi.ok()) {
        out.line("%s: not loadable (error %lu); no NVIDIA driver for this architecture, or none installed",
                 narrow(name).c_str(), nvapi.error());
        return;
    }
    out.line("library: %s", narrow(nvapi.path()).c_str());
    using QueryInterfaceFn = void*(__cdecl*)(unsigned int);
    auto query = nvapi.get<QueryInterfaceFn>("nvapi_QueryInterface");
    if (!query) {
        out.line("nvapi_QueryInterface: missing");
        return;
    }
    std::vector<void*> pointers;
    std::size_t missing = 0;
    for (const Interface& entry : kNvapiInterfaces) {
        void* pointer = query(entry.id);
        pointers.push_back(pointer);
        if (!pointer) {
            ++missing;
            out.line("0x%08X %s: missing", entry.id, entry.name);
        }
    }
    out.line("interfaces: %u of %u present", static_cast<unsigned>(pointers.size() - missing),
             static_cast<unsigned>(pointers.size()));

    using InitializeFn = int(__cdecl*)();
    using EnumGpusFn = int(__cdecl*)(void**, unsigned*);
    using PciIdsFn = int(__cdecl*)(void*, unsigned*, unsigned*, unsigned*, unsigned*);
    using BusFn = int(__cdecl*)(void*, unsigned*);
    using NameFn = int(__cdecl*)(void*, char*);
    using ArchFn = int(__cdecl*)(void*, GpuArchInfo*);
    using DriverFn = int(__cdecl*)(unsigned*, char*);
    using ErrorFn = int(__cdecl*)(int, char*);
    auto initialize = reinterpret_cast<InitializeFn>(query(0x0150E828));
    auto enum_gpus = reinterpret_cast<EnumGpusFn>(query(0xE5AC921F));
    auto pci_ids = reinterpret_cast<PciIdsFn>(query(0x2DDFB66E));
    auto bus_id = reinterpret_cast<BusFn>(query(0x1BE0B8E5));
    auto bus_slot = reinterpret_cast<BusFn>(query(0x2A0A350F));
    auto full_name = reinterpret_cast<NameFn>(query(0xCEEE8E9F));
    auto vbios = reinterpret_cast<NameFn>(query(0xA561FD7D));
    auto arch_info = reinterpret_cast<ArchFn>(query(0xD8265D24));
    auto driver = reinterpret_cast<DriverFn>(query(0x2926AAAD));
    auto error_message = reinterpret_cast<ErrorFn>(query(0x6C2D048C));
    auto describe = [&](int status) {
        char text[64] = "";
        if (error_message) error_message(status, text);
        return std::string(text);
    };
    if (!initialize) {
        out.line("NvAPI_Initialize: missing");
        return;
    }
    int status = initialize();
    out.line("NvAPI_Initialize: status %d %s", status, describe(status).c_str());
    if (status != 0) return;
    if (driver) {
        unsigned version = 0;
        char branch[64] = "";
        int st = driver(&version, branch);
        if (st == 0)
            out.line("driver: %u.%02u branch %s", version / 100, version % 100, branch);
        else
            out.line("driver: status %d %s", st, describe(st).c_str());
    }
    if (!enum_gpus) {
        out.line("NvAPI_EnumPhysicalGPUs: missing");
        return;
    }
    void* handles[64] = {};
    unsigned count = 0;
    status = enum_gpus(handles, &count);
    out.line("NvAPI_EnumPhysicalGPUs: status %d %s, %u GPU(s)", status, describe(status).c_str(), count);
    for (unsigned i = 0; i < count && i < 64; ++i) {
        char name_buffer[64] = "";
        if (full_name && full_name(handles[i], name_buffer) != 0) std::strcpy(name_buffer, "?");
        unsigned device_id = 0, subsystem = 0, revision = 0, external = 0;
        bool have_ids = pci_ids && pci_ids(handles[i], &device_id, &subsystem, &revision, &external) == 0;
        unsigned bus = 0, slot = 0;
        bool have_bus = bus_id && bus_slot && bus_id(handles[i], &bus) == 0 && bus_slot(handles[i], &slot) == 0;
        std::string slot_text = have_bus ? druta::format_slot(0, bus, slot) : "?";
        out.line("GPU %u: %s slot=%s device=%04X vendor=%04X subsystem=%08X revision=%08X", i, name_buffer,
                 slot_text.c_str(), have_ids ? device_id >> 16 : 0, have_ids ? device_id & 0xFFFF : 0,
                 have_ids ? subsystem : 0, have_ids ? revision : 0);
        if (arch_info) {
            GpuArchInfo arch{};
            arch.version = static_cast<std::uint32_t>(sizeof arch) | (2u << 16);
            int st = arch_info(handles[i], &arch);
            if (st != 0) {
                arch.version = static_cast<std::uint32_t>(sizeof arch) | (1u << 16);
                st = arch_info(handles[i], &arch);
            }
            if (st == 0)
                out.line("  architecture=0x%X implementation=0x%X revision=0x%X", arch.architecture,
                         arch.implementation, arch.revision);
            else
                out.line("  architecture: status %d %s", st, describe(st).c_str());
        }
        if (vbios) {
            char version[64] = "";
            if (vbios(handles[i], version) == 0) out.line("  vbios=%s", version);
        }
    }
}

// ---- NVML ---------------------------------------------------------------------------------------

const char* const kNvmlExports[] = {
    "nvmlInit_v2", "nvmlInit", "nvmlErrorString", "nvmlSystemGetDriverVersion",
    "nvmlDeviceGetCount_v2", "nvmlDeviceGetCount", "nvmlDeviceGetHandleByIndex_v2", "nvmlDeviceGetHandleByIndex",
    "nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetPciInfo_v2", "nvmlDeviceGetPciInfo", "nvmlDeviceGetName",
    "nvmlDeviceGetUUID", "nvmlDeviceGetVbiosVersion", "nvmlDeviceGetArchitecture",
    "nvmlDeviceGetClockInfo", "nvmlDeviceGetClockOffsets", "nvmlDeviceSetClockOffsets",
    "nvmlDeviceGetMinMaxClockOfPState", "nvmlDeviceGetSupportedGraphicsClocks",
    "nvmlDeviceGetSupportedMemoryClocks", "nvmlDeviceGetGpuLockedClocks", "nvmlDeviceSetGpuLockedClocks",
    "nvmlDeviceResetGpuLockedClocks", "nvmlDeviceSetMemoryLockedClocks",
    "nvmlDeviceGetCurrPcieLinkGeneration", "nvmlDeviceGetCurrPcieLinkWidth", "nvmlDeviceGetMaxPcieLinkGeneration",
    "nvmlDeviceGetCurrentClocksEventReasons", "nvmlDeviceGetCurrentClocksThrottleReasons",
    "nvmlDeviceGetPowerUsage", "nvmlDeviceGetEnforcedPowerLimit", "nvmlDeviceGetPowerManagementLimit",
    "nvmlDeviceGetPowerManagementDefaultLimit", "nvmlDeviceGetPowerManagementLimitConstraints",
    "nvmlDeviceSetPowerManagementLimit", "nvmlDeviceGetTotalEnergyConsumption",
    "nvmlDeviceGetTemperatureThreshold", "nvmlDeviceGetFieldValues",
    "nvmlDeviceGetFanSpeed", "nvmlDeviceGetFanSpeed_v2", "nvmlDeviceGetFanSpeedRPM", "nvmlDeviceGetNumFans",
    "nvmlDeviceGetMinMaxFanSpeed", "nvmlDeviceGetTargetFanSpeed", "nvmlDeviceGetFanControlPolicy_v2",
    "nvmlDeviceSetFanSpeed_v2", "nvmlDeviceSetDefaultFanSpeed_v2",
};

// Leading fields shared by nvmlPciInfo_t v2 and v3; the buffer covers the longer v3.
struct NvmlPciInfo {
    char bus_id_legacy[16];
    unsigned domain;
    unsigned bus;
    unsigned device;
    unsigned pci_device_id;
    unsigned pci_subsystem_id;
    char bus_id[32];
    unsigned reserved[8];
};

std::wstring system_directory() {
    wchar_t buffer[1024] = {};
    UINT length = ::GetSystemDirectoryW(buffer, 1024);
    return (length && length < 1024) ? std::wstring(buffer, length) : L"";
}

std::wstring program_files_directory() {
    DynLib shell(L"shell32.dll");
    using FolderFn = HRESULT(WINAPI*)(HWND, int, HANDLE, DWORD, LPWSTR);
    auto get_folder = shell.get<FolderFn>("SHGetFolderPathW");
    wchar_t buffer[MAX_PATH] = {};
    if (get_folder && get_folder(nullptr, 0x26 /* CSIDL_PROGRAM_FILES */, nullptr, 0, buffer) == 0) return buffer;
    DWORD length = ::GetEnvironmentVariableW(L"ProgramFiles", buffer, MAX_PATH);
    return (length && length < MAX_PATH) ? std::wstring(buffer, length) : L"";
}

void report_nvml(Report& out) {
    out.section("NVML");
    std::vector<std::wstring> candidates;
    std::wstring system = system_directory();
    if (!system.empty()) candidates.push_back(system + L"\\nvml.dll");
    std::wstring programs = program_files_directory();
    if (!programs.empty()) candidates.push_back(programs + L"\\NVIDIA Corporation\\NVSMI\\nvml.dll");
    DynLib nvml;
    for (const std::wstring& path : candidates) {
        DWORD attributes = ::GetFileAttributesW(path.c_str());
        if (attributes == INVALID_FILE_ATTRIBUTES) {
            out.line("%s: not present", narrow(path).c_str());
            continue;
        }
        if (nvml.open_path(path.c_str())) {
            out.line("%s: loaded", narrow(path).c_str());
            break;
        }
        out.line("%s: present but not loadable (error %lu)", narrow(path).c_str(), nvml.error());
    }
    if (!nvml.ok()) {
        out.line("NVML: unavailable");
        return;
    }
    std::size_t missing = 0;
    for (const char* name : kNvmlExports) {
        if (!nvml.has(name)) {
            ++missing;
            out.line("%s: missing", name);
        }
    }
    out.line("exports: %u of %u present", static_cast<unsigned>(sizeof kNvmlExports / sizeof kNvmlExports[0] - missing),
             static_cast<unsigned>(sizeof kNvmlExports / sizeof kNvmlExports[0]));

    using InitFn = int (*)();
    using CountFn = int (*)(unsigned*);
    using HandleFn = int (*)(unsigned, void**);
    using StringFn = int (*)(void*, char*, unsigned);
    using PciFn = int (*)(void*, NvmlPciInfo*);
    using DriverFn = int (*)(char*, unsigned);
    using ErrorFn = const char* (*)(int);
    auto error_string = nvml.get<ErrorFn>("nvmlErrorString");
    auto describe = [&](int status) { return error_string ? error_string(status) : ""; };
    auto init = nvml.get<InitFn>("nvmlInit_v2");
    const char* init_name = "nvmlInit_v2";
    if (!init) {
        init = nvml.get<InitFn>("nvmlInit");
        init_name = "nvmlInit";
    }
    if (!init) {
        out.line("no initialization export");
        return;
    }
    int status = init();
    out.line("%s: status %d %s", init_name, status, describe(status));
    if (status != 0) return;
    if (auto driver = nvml.get<DriverFn>("nvmlSystemGetDriverVersion")) {
        char version[80] = "";
        if (driver(version, sizeof version) == 0) out.line("driver: %s", version);
    }
    // Never mix a v1 count with a v2 index lookup; the index spaces differ.
    CountFn count_fn = nullptr;
    HandleFn handle_fn = nullptr;
    const char* pair = "";
    if (nvml.has("nvmlDeviceGetCount_v2") && nvml.has("nvmlDeviceGetHandleByIndex_v2")) {
        count_fn = nvml.get<CountFn>("nvmlDeviceGetCount_v2");
        handle_fn = nvml.get<HandleFn>("nvmlDeviceGetHandleByIndex_v2");
        pair = "v2";
    } else if (nvml.has("nvmlDeviceGetCount") && nvml.has("nvmlDeviceGetHandleByIndex")) {
        count_fn = nvml.get<CountFn>("nvmlDeviceGetCount");
        handle_fn = nvml.get<HandleFn>("nvmlDeviceGetHandleByIndex");
        pair = "v1";
    }
    if (!count_fn) {
        out.line("no matching device-count/index exports");
        return;
    }
    unsigned count = 0;
    status = count_fn(&count);
    out.line("device count (%s): status %d %s, %u device(s)", pair, status, describe(status), count);
    if (status != 0) return;
    auto name_fn = nvml.get<StringFn>("nvmlDeviceGetName");
    auto uuid_fn = nvml.get<StringFn>("nvmlDeviceGetUUID");
    auto pci_v3 = nvml.get<PciFn>("nvmlDeviceGetPciInfo_v3");
    auto pci_v2 = nvml.get<PciFn>("nvmlDeviceGetPciInfo_v2");
    for (unsigned i = 0; i < count; ++i) {
        void* device = nullptr;
        status = handle_fn(i, &device);
        if (status != 0) {
            out.line("device %u: handle status %d %s", i, status, describe(status));
            continue;
        }
        char name[96] = "?";
        if (name_fn) name_fn(device, name, sizeof name);
        char uuid[96] = "";
        if (uuid_fn) uuid_fn(device, uuid, sizeof uuid);
        NvmlPciInfo pci{};
        const char* reader = "none";
        int pci_status = -1;
        if (pci_v3) {
            pci_status = pci_v3(device, &pci);
            reader = "v3";
        }
        if (pci_status == 13 /* NVML_ERROR_FUNCTION_NOT_FOUND: exported stub */ && pci_v2) {
            pci_status = pci_v2(device, &pci);
            reader = "v2 (v3 is a stub)";
        } else if (pci_status == -1 && pci_v2) {
            pci_status = pci_v2(device, &pci);
            reader = "v2";
        }
        if (pci_status == 0)
            out.line("device %u: %s slot=%s device=%04X subsystem=%08X pci reader=%s", i, name,
                     druta::format_slot(pci.domain, pci.bus, pci.device).c_str(), pci.pci_device_id >> 16,
                     pci.pci_subsystem_id, reader);
        else
            out.line("device %u: %s slot=? (pci status %d %s, reader %s)", i, name, pci_status, describe(pci_status),
                     reader);
        if (uuid[0]) out.line("  uuid=%s", uuid);
    }
}

}  // namespace

int main() {
    Report out;
    std::wstring path = exe_directory() + L"\\druta-probe-report.txt";
    out.open(path);
    if (!out.is_open()) {
        path = temp_directory() + L"druta-probe-report.txt";
        out.open(path);
    }
    out.line("Druta native probe (read-only; changes no setting), built %s", __DATE__);
    report_windows(out);
    report_driver_model(out);
    report_escape_entry(out);
    report_kmt_adapters(out);
    report_exports(out, "Windows exports Druta binds", kWindowsExports, sizeof kWindowsExports / sizeof kWindowsExports[0]);
    report_nvapi(out);
    report_nvml(out);
    out.line("");
    out.line("report written to %s", out.is_open() ? narrow(path).c_str() : "(no writable location)");
    return 0;
}
