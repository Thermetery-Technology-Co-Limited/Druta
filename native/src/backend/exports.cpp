// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/exports.h"

#include <vector>

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
    std::unique_ptr<ExportSource> open_nvml(std::string& error) override { return open("nvml", error); }
    std::unique_ptr<ExportSource> open_gdi32(std::string& error) override { return open("gdi32", error); }

private:
    std::unique_ptr<ExportSource> open(const char* library, std::string& error) {
        if (!resolve_(context_, library, "")) {
            std::string which(library);
            if (which == "nvapi")
                error = (kProcessBits == 64 ? "nvapi64.dll" : "nvapi.dll") + std::string(" not loadable: not provided by the test resolver");
            else if (which == "gdi32")
                error = "gdi32.dll not loadable: not provided by the test resolver";
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
    std::unique_ptr<ExportSource> open_nvml(std::string& error) override {
        std::vector<std::wstring> candidates;
        std::vector<std::string> errors;
        std::wstring folder;
        std::string why;
        if (system_directory(folder, why) && absolute_drive_path(folder))
            candidates.push_back(folder + L"\\nvml.dll");
        else
            errors.push_back(why.empty() ? "Windows system directory: Windows returned a non-absolute path" : why);
        if (program_files_directory(folder, why) && absolute_drive_path(folder))
            candidates.push_back(folder + L"\\NVIDIA Corporation\\NVSMI\\nvml.dll");
        else
            errors.push_back(why.empty() ? "Program Files directory: Windows returned a non-absolute path" : why);
        for (const std::wstring& path : candidates) {
            DWORD attributes = ::GetFileAttributesW(path.c_str());
            if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY)) {
                errors.push_back(narrow(path) + ": file not found");
                continue;
            }
            win::DynLib lib;
            if (lib.open_path(path.c_str())) return std::make_unique<DllSource>(std::move(lib));
            errors.push_back(narrow(path) + ": Windows error " + std::to_string(lib.error()));
        }
        error = std::to_string(kProcessBits) + "-bit process";
        for (const std::string& item : errors) error += "; " + item;
        return nullptr;
    }

    // gdi32 is a system DLL present on every Windows; which D3DKMT* exports it
    // has is the capability (XPDM images have none).
    std::unique_ptr<ExportSource> open_gdi32(std::string& error) override {
        win::DynLib lib(L"gdi32.dll");
        if (!lib.ok()) {
            error = "gdi32.dll not loadable: Windows error " + std::to_string(lib.error());
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
    std::unique_ptr<ExportSource> open_nvml(std::string& error) override {
        error = std::to_string(kProcessBits) + "-bit process; NVIDIA driver libraries exist on Windows only";
        return nullptr;
    }
    std::unique_ptr<ExportSource> open_gdi32(std::string& error) override {
        error = "gdi32.dll not loadable: the graphics kernel interface exists on Windows only";
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
    if (resolve)
        hooked_loader() = std::make_unique<HookedLoader>(resolve, context);
    else
        hooked_loader().reset();
}

}  // namespace druta::backend
