// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Run-time binding of Windows DLL exports. Nothing in the native core links
// against a Windows DLL other than kernel32: every other function is a
// capability looked up here, so one binary runs on XP through 11 and reports
// exactly which capabilities the current image lacks.
#pragma once
#ifdef _WIN32

#include <windows.h>

#include <string>

namespace druta::win {

class DynLib {
public:
    DynLib() = default;
    explicit DynLib(const wchar_t* name) { open(name); }
    DynLib(const DynLib&) = delete;
    DynLib& operator=(const DynLib&) = delete;
    DynLib(DynLib&& other) noexcept : module_(other.module_), error_(other.error_) { other.module_ = nullptr; }
    DynLib& operator=(DynLib&& other) noexcept {
        if (this != &other) {
            close();
            module_ = other.module_;
            error_ = other.error_;
            other.module_ = nullptr;
        }
        return *this;
    }
    ~DynLib() { close(); }

    // Load by name through the normal search order (system DLLs), or by full path.
    bool open(const wchar_t* name) {
        close();
        module_ = ::LoadLibraryW(name);
        error_ = module_ ? 0 : ::GetLastError();
        return module_ != nullptr;
    }

    // Load an absolute path so its own directory is searched for dependencies.
    // LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|SYSTEM32 needs KB2533623 on Windows 7 and
    // does not exist before it; AddDllDirectory's presence is the documented
    // capability check. Otherwise LOAD_WITH_ALTERED_SEARCH_PATH (XP and later).
    bool open_path(const wchar_t* path) {
        close();
        DWORD flags = 0x8;  // LOAD_WITH_ALTERED_SEARCH_PATH
        if (HMODULE kernel = ::GetModuleHandleW(L"kernel32.dll")) {
            if (::GetProcAddress(kernel, "AddDllDirectory")) flags = 0x100 | 0x800;
        }
        module_ = ::LoadLibraryExW(path, nullptr, flags);
        error_ = module_ ? 0 : ::GetLastError();
        return module_ != nullptr;
    }

    void close() {
        if (module_) ::FreeLibrary(module_);
        module_ = nullptr;
    }

    bool ok() const { return module_ != nullptr; }
    DWORD error() const { return error_; }
    HMODULE handle() const { return module_; }

    template <typename Fn>
    Fn get(const char* name) const {
        if (!module_) return nullptr;
        return reinterpret_cast<Fn>(reinterpret_cast<void*>(::GetProcAddress(module_, name)));
    }

    bool has(const char* name) const { return module_ && ::GetProcAddress(module_, name) != nullptr; }

    std::wstring path() const {
        wchar_t buffer[1024] = {};
        if (!module_ || !::GetModuleFileNameW(module_, buffer, 1024)) return L"";
        return buffer;
    }

private:
    HMODULE module_ = nullptr;
    DWORD error_ = 0;
};

}  // namespace druta::win

#endif  // _WIN32
