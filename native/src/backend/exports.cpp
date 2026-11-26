// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/exports.h"

#include <cstring>
#include <array>
#include <optional>

#include "core/mutex.h"

#if defined(_WIN32)
#include <windows.h>

#include "win/dynlib.h"
#endif

namespace druta::backend {
namespace {

// ---- test resolver --------------------------------------------------------------------------------

class HookedSource final : public ExportSource {
public:
    HookedSource(ResolveFn fn, void* context, std::string library)
        : resolve_(fn), context_(context), library_(std::move(library)) {}
    void* resolve(const char* name) const override { return resolve_(context_, library_.c_str(), name); }
    std::string path() const override { return "<resolver:" + library_ + ">"; }

private:
    ResolveFn resolve_;
    void* context_;
    std::string library_;
};

class HookedLoader final : public Loader {
public:
    HookedLoader(ResolveFn fn, void* context) : resolve_(fn), context_(context) {}
    std::unique_ptr<ExportSource> open_nvapi(std::string& error) override { return open("nvapi", error); }
    std::unique_ptr<ExportSource> open_nvml(bool standard, std::string& error) override {
        return open(standard ? "nvml_standard" : "nvml", error);
    }
    std::unique_ptr<ExportSource> open_gdi32(std::string& error) override { return open("gdi32", error); }
    std::unique_ptr<ExportSource> open_user32(std::string& error) override { return open("user32", error); }
    std::unique_ptr<ExportSource> open_cfgmgr32(std::string& error) override { return open("cfgmgr32", error); }

private:
    std::unique_ptr<ExportSource> open(const char* library, std::string& error) {
        if (!resolve_(context_, library, "")) {
            std::string which(library);
            if (which == "nvapi")
                error = (kProcessBits == 64 ? "nvapi64.dll" : "nvapi.dll") + std::string(" not loadable: not provided by the test resolver");
            else if (which == "gdi32" || which == "user32" || which == "cfgmgr32")
                error = which + ".dll not loadable: not provided by the test resolver";
            else
                error = std::to_string(kProcessBits) + "-bit process; not provided by the test resolver";
            return nullptr;
        }
        return std::make_unique<HookedSource>(resolve_, context_, library);
    }
    ResolveFn resolve_;
    void* context_;
};

// ---- platform default ------------------------------------------------------------------------------

#if defined(_WIN32)

std::string narrow(const std::wstring& text) {
    if (text.empty()) return "";
    int size = ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(size > 0 ? size : 1), '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, &out[0], size, nullptr, nullptr);
    out.resize(out.empty() ? 0 : out.size() - 1);
    return out;
}

class DllSource final : public ExportSource {
public:
    explicit DllSource(win::DynLib lib) : lib_(std::move(lib)) {}
    void* resolve(const char* name) const override {
        return reinterpret_cast<void*>(::GetProcAddress(lib_.handle(), name));
    }
    std::string path() const override { return narrow(lib_.path()); }

private:
    win::DynLib lib_;
};

// Resolve the running process's system directory without assuming C:.
bool system_directory(std::wstring& out, std::string& error) {
    wchar_t buffer[32768];
    UINT length = ::GetSystemDirectoryW(buffer, 32768);
    if (!length) {
        error = "Windows system directory: GetSystemDirectoryW error " + std::to_string(::GetLastError());
        return false;
    }
    if (length >= 32768) {
        error = "Windows system directory: GetSystemDirectoryW returned an oversized path";
        return false;
    }
    out.assign(buffer, length);
    return true;
}

// The Program Files of THIS process architecture (x86 folder for a 32-bit
// process): a 32-bit process must not try to load a 64-bit driver DLL.
bool program_files_directory(std::wstring& out, std::string& error) {
    win::DynLib shell(L"shell32.dll");
    using FolderFn = HRESULT(WINAPI*)(HWND, int, HANDLE, DWORD, LPWSTR);
    auto get_folder = shell.get<FolderFn>("SHGetFolderPathW");
    if (!get_folder) {
        error = "Program Files directory: shell32 has no SHGetFolderPathW";
        return false;
    }
    wchar_t buffer[MAX_PATH] = {};
    HRESULT status = get_folder(nullptr, 0x26 /* CSIDL_PROGRAM_FILES */, nullptr, 0 /* SHGFP_TYPE_CURRENT */, buffer);
    if (status != 0) {
        char text[64];
        std::snprintf(text, sizeof text, "SHGetFolderPathW status 0x%08lX", static_cast<unsigned long>(status));
        error = std::string("Program Files directory: ") + text;
        return false;
    }
    out = buffer;
    return true;
}

bool absolute_drive_path(const std::wstring& folder) {
    // ntpath.splitdrive: "X:" followed by a path starting with a backslash
    return folder.size() >= 3 && folder[1] == L':' && (folder[2] == L'\\' || folder[2] == L'/');
}

class DefaultLoader final : public Loader {
public:
    std::unique_ptr<ExportSource> open_nvapi(std::string& error) override {
        const wchar_t* name = kProcessBits == 64 ? L"nvapi64.dll" : L"nvapi.dll";
        win::DynLib lib(name);
        if (!lib.ok()) {
            error = narrow(name) + " not loadable: Windows error " + std::to_string(lib.error());
            return nullptr;
        }
        return std::make_unique<DllSource>(std::move(lib));
    }

    // Only driver installation directories, never PATH or the working folder:
    // DCH drivers put nvml.dll in System32, Standard drivers under
    // Program Files\NVIDIA Corporation\NVSMI (NVIDIA R470 NVML reference).
    std::unique_ptr<ExportSource> open_nvml(bool standard, std::string& error) override {
        std::wstring folder;
        if (!(standard ? program_files_directory(folder, error) : system_directory(folder, error))) return nullptr;
        if (!absolute_drive_path(folder)) { error = "Windows returned a non-absolute driver directory"; return nullptr; }
        const auto path = folder + (standard ? L"\\NVIDIA Corporation\\NVSMI\\nvml.dll" : L"\\nvml.dll");
        win::DynLib lib;
        if (lib.open_path(path.c_str())) return std::make_unique<DllSource>(std::move(lib));
        error = narrow(path) + ": Windows error " + std::to_string(lib.error());
        return nullptr;
    }

    // gdi32 is a system DLL present on every Windows; which D3DKMT* exports it
    // has is the capability (XPDM images have none).
    std::unique_ptr<ExportSource> open_gdi32(std::string& error) override {
        return open_system_dll(L"gdi32.dll", error);
    }
    std::unique_ptr<ExportSource> open_user32(std::string& error) override {
        return open_system_dll(L"user32.dll", error);
    }
    std::unique_ptr<ExportSource> open_cfgmgr32(std::string& error) override {
        return open_system_dll(L"cfgmgr32.dll", error);
    }

private:
    static std::unique_ptr<ExportSource> open_system_dll(const wchar_t* name, std::string& error) {
        win::DynLib lib(name);
        if (!lib.ok()) {
            error = narrow(name) + " not loadable: Windows error " + std::to_string(lib.error());
            return nullptr;
        }
        return std::make_unique<DllSource>(std::move(lib));
    }
};

#else

class DefaultLoader final : public Loader {
public:
    std::unique_ptr<ExportSource> open_nvapi(std::string& error) override {
        error = "nvapi64.dll not loadable: NVIDIA driver libraries exist on Windows only";
        return nullptr;
    }
    std::unique_ptr<ExportSource> open_nvml(bool, std::string& error) override {
        error = std::to_string(kProcessBits) + "-bit process; NVIDIA driver libraries exist on Windows only";
        return nullptr;
    }
    std::unique_ptr<ExportSource> open_gdi32(std::string& error) override {
        error = "gdi32.dll not loadable: the graphics kernel interface exists on Windows only";
        return nullptr;
    }
    std::unique_ptr<ExportSource> open_user32(std::string& error) override {
        error = "user32.dll not loadable: display enumeration exists on Windows only";
        return nullptr;
    }
    std::unique_ptr<ExportSource> open_cfgmgr32(std::string& error) override {
        error = "cfgmgr32.dll not loadable: Windows Configuration Manager exists on Windows only";
        return nullptr;
    }
};

#endif

Mutex& loader_mutex() {
    static Mutex mutex;
    return mutex;
}

std::unique_ptr<Loader>& hooked_loader() {
    static std::unique_ptr<Loader> hooked;
    return hooked;
}

unsigned& generation_counter() {
    static unsigned generation = 1;
    return generation;
}

// Keep raw export addresses valid for the resolver generation, including an
// address requested before initialization picked a different installation.
struct NvmlSources {
    std::array<std::shared_ptr<ExportSource>, 2> modules;
    std::optional<bool> selected;

    std::shared_ptr<ExportSource>& load(bool standard, std::string& error) {
        auto& module = modules[standard];
        if (!module) module = loader().open_nvml(standard, error);
        return module;
    }
};

NvmlSources& nvml_sources() {
    static NvmlSources sources;
    return sources;
}

}  // namespace

Loader& loader() {
    static DefaultLoader platform;
    LockGuard guard(loader_mutex());
    if (hooked_loader()) return *hooked_loader();
    return platform;
}

unsigned loader_generation() {
    LockGuard guard(loader_mutex());
    return generation_counter();
}

void set_export_resolver(ResolveFn resolve, void* context) {
    LockGuard guard(loader_mutex());
    ++generation_counter();
    nvml_sources() = {};
    if (resolve)
        hooked_loader() = std::make_unique<HookedLoader>(resolve, context);
    else
        hooked_loader().reset();
}


bool test_resolver_installed() {
    LockGuard guard(loader_mutex());
    return hooked_loader() != nullptr;
}

std::shared_ptr<ExportSource> initialized_nvml(std::string& error, std::string& path) {
    LockGuard guard(loader_mutex());
    auto& sources = nvml_sources();
    const bool preferred = sources.selected.value_or(false);
    // Outstanding handles can only be used with exports from their own DLL.
    const bool active = sources.selected && sources.modules[preferred].use_count() > 1;
    std::string load_errors;
    for (bool standard : {preferred, !preferred}) {
        if (active && standard != preferred) break;
        std::string why;
        const auto& module = sources.load(standard, why);
        if (!module) {
            if (load_errors.empty()) load_errors = why;
            else if (load_errors != why) load_errors += "; " + why;
            continue;
        }
        using Init = int(DRUTA_CDECL*)();
        const char* name = module->has("nvmlInit_v2") ? "nvmlInit_v2" : "nvmlInit";
        if (auto initialize = module->get<Init>(name)) {
            const int status = initialize();
            if (!status) {
                sources.selected = standard;
                path = module->path();
                error.clear();
                return module;
            }
            why = std::string(name) + " status " + std::to_string(status);
        } else why = "NVML has no initialization export";
        if (error.empty()) { path = module->path(); error = why; }
        else error += "; " + module->path() + ": " + why;
    }
    if (error.empty()) error = "nvml.dll not loadable: " + load_errors;
    return nullptr;
}

void* export_address(const char* library, const char* name) {
    // Serialize the source cache and resolver replacement together. Loader calls take this same
    // recursive mutex; no driver entry is invoked here, only export lookup.
    LockGuard guard(loader_mutex());
    static unsigned generation = 0;
    static std::unique_ptr<ExportSource> nvapi;
    if (generation != generation_counter()) {
        nvapi.reset();
        generation = generation_counter();
    }
    std::string error;
    if (std::strcmp(library, "nvml") == 0) {
        auto& sources = nvml_sources();
        if (auto& module = sources.load(sources.selected.value_or(false), error)) return module->resolve(name);
        if (!sources.selected) {
            if (auto& module = sources.load(true, error)) return module->resolve(name);
        }
        return nullptr;
    }
    if (!nvapi) nvapi = loader().open_nvapi(error);
    return nvapi ? nvapi->resolve(name) : nullptr;
}

}  // namespace druta::backend
