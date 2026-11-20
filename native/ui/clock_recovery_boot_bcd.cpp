// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_recovery_boot_bcd.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <algorithm>
#include <cstring>
#include <optional>
#include <stdexcept>
#include <vector>

namespace druta::ui {
namespace {
using Strings = std::vector<std::wstring>;
constexpr wchar_t manager[] = L"{9dea862c-5cdd-4e70-acc1-f32b344d4795}";
constexpr DWORD loader_type = 0x10200003;
constexpr DWORD manager_type = 0x10100002;

void check(bool ok, const char* reason) {
    if (!ok) throw std::runtime_error(reason);
}
void wincheck(LONG status, const char* operation) {
    if (status != ERROR_SUCCESS)
        throw std::runtime_error(std::string(operation) + " (Windows error " + std::to_string(status) + ")");
}

struct Key {
    HKEY key = nullptr;
    Key() = default;
    Key(const Key&) = delete;
    Key(Key&& other) noexcept : key(other.key) { other.key = nullptr; }
    ~Key() { if (key) RegCloseKey(key); }
};

struct Handle {
    HANDLE h = nullptr;
    ~Handle() { if (h) CloseHandle(h); }
};

struct BootLock {
    Handle guard;
    BootLock() {
        guard.h = CreateMutexW(nullptr, FALSE, L"Global\\DrutaClockRecoveryBcd-v1");
        check(guard.h != nullptr, "Cannot acquire the boot recovery lock");
        const auto wait = WaitForSingleObject(guard.h, 30000);
        check(wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED, "Another clock boot recovery operation is in progress");
    }
    ~BootLock() { ReleaseMutex(guard.h); }
};

bool is_guid(const std::wstring& value) {
    if (value.size() != 38 || value.front() != L'{' || value.back() != L'}') return false;
    for (size_t i = 1; i < 37; ++i) {
        const auto ch = value[i];
        const bool hyphen = i == 9 || i == 14 || i == 19 || i == 24;
        if (hyphen ? ch != L'-' : !((ch >= L'0' && ch <= L'9') || (ch >= L'a' && ch <= L'f') ||
                                     (ch >= L'A' && ch <= L'F'))) return false;
    }
    return true;
}
std::wstring label(const std::string& id) {
    check(id.size() == 32 && id.find_first_not_of("0123456789abcdef") == std::string::npos,
          "Invalid clock recovery receipt identifier");
    return L"Druta clock recovery " + std::wstring(id.begin(), id.end());
}
bool same(const std::wstring& a, const std::wstring& b) { return !_wcsicmp(a.c_str(), b.c_str()); }
bool contains(const Strings& values, const std::wstring& value) {
    return std::any_of(values.begin(), values.end(), [&](const auto& item) { return same(item, value); });
}

struct Store {
    std::wstring file, source = L"{current}"; // Only tests supply a separate on-disk store.
    Key open() const {
        Key result;
        if (file.empty()) {
            wincheck(RegOpenKeyExW(HKEY_LOCAL_MACHINE, L"BCD00000000", 0, KEY_READ | KEY_WOW64_64KEY, &result.key),
                     "Cannot read the mounted system BCD hive");
        } else {
            using OpenHive = LSTATUS (WINAPI*)(LPCWSTR, PHKEY, REGSAM, DWORD, DWORD);
            auto api = reinterpret_cast<OpenHive>(GetProcAddress(GetModuleHandleW(L"advapi32.dll"), "RegLoadAppKeyW"));
            check(api != nullptr, "Private BCD fixture hive API unavailable");
            wincheck(api(file.c_str(), &result.key, KEY_READ, 0, 0), "Cannot open the isolated BCD fixture");
        }
        return result;
    }
    void command(const std::wstring& arguments) const {
        wchar_t directory[MAX_PATH]{};
        const auto n = GetSystemDirectoryW(directory, MAX_PATH);
        check(n && n < MAX_PATH, "Cannot resolve Windows system directory");
        const auto exe = std::wstring(directory) + L"\\bcdedit.exe";
        auto cmd = L"\"" + exe + L"\" ";
        if (!file.empty()) cmd += L"/store \"" + file + L"\" ";
        cmd += arguments;
        STARTUPINFOW start{};
        start.cb = sizeof(start);
        PROCESS_INFORMATION pi{};
        wincheck(CreateProcessW(exe.c_str(), cmd.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW,
                                nullptr, directory, &start, &pi) ? ERROR_SUCCESS : GetLastError(), "Cannot start BCDEdit");
        Handle process{pi.hProcess}, thread{pi.hThread};
        // The worker's caller must never stage a GPU change before BCDEdit exits.
        check(WaitForSingleObject(process.h, INFINITE) == WAIT_OBJECT_0, "Cannot wait for BCDEdit");
        DWORD code = 0;
        check(GetExitCodeProcess(process.h, &code) != 0, "Cannot obtain BCDEdit result");
        if (code) throw std::runtime_error("BCDEdit rejected the boot recovery change (exit " + std::to_string(code) + ")");
    }
    void flush() const {
        auto root = open();
        wincheck(RegFlushKey(root.key), "Cannot flush the BCD hive to disk");
    }
};

Key subkey(HKEY root, const std::wstring& path, bool optional = false) {
    Key result;
    const auto status = RegOpenKeyExW(root, path.c_str(), 0, KEY_READ, &result.key);
    if (optional && (status == ERROR_FILE_NOT_FOUND || status == ERROR_PATH_NOT_FOUND)) return result;
    wincheck(status, "Cannot inspect a BCD key");
    return result;
}

Strings children(HKEY root) {
    Strings result;
    for (DWORD i = 0;; ++i) {
        wchar_t key[128]{};
        DWORD chars = 128;
        const auto status = RegEnumKeyExW(root, i, key, &chars, nullptr, nullptr, nullptr, nullptr);
        if (status == ERROR_NO_MORE_ITEMS) return result;
        wincheck(status, "Cannot enumerate the BCD store");
        result.emplace_back(key, chars);
    }
}

struct Value { DWORD type; std::vector<BYTE> data; };
Value read(HKEY key, const wchar_t* name) {
    DWORD type = 0, bytes = 0;
    wincheck(RegQueryValueExW(key, name, nullptr, &type, nullptr, &bytes), "Cannot inspect BCD value type");
    check(bytes && bytes <= 1024 * 1024, "Invalid BCD value size");
    Value result{type, std::vector<BYTE>(bytes)};
    const DWORD initial_type = type;
    wincheck(RegQueryValueExW(key, name, nullptr, &type, result.data.data(), &bytes), "Cannot read BCD value");
    check(type == initial_type && bytes == result.data.size(), "BCD value changed during inspection");
    return result;
}
std::optional<Value> element(HKEY obj, const wchar_t* type) {
    auto key = subkey(obj, std::wstring(L"Elements\\") + type, true);
    if (!key.key) return {};
    return read(key.key, L"Element");
}
DWORD object_type(HKEY obj) {
    auto description = subkey(obj, L"Description");
    auto value = read(description.key, L"Type");
    DWORD result = 0;
    check(value.type == REG_DWORD && value.data.size() == sizeof(result), "Malformed BCD object type");
    std::memcpy(&result, value.data.data(), sizeof(result));
    return result;
}
Strings strings(const Value& value, DWORD want) {
    check(value.type == want && value.data.size() % sizeof(wchar_t) == 0, "Malformed BCD string type");
    std::wstring s(value.data.size() / sizeof(wchar_t), L'\0');
    std::memcpy(s.data(), value.data.data(), value.data.size());
    check(!s.empty() && s.back() == 0, "Unterminated BCD string");
    if (want == REG_SZ) {
        s.pop_back();
        check(s.find(L'\0') == std::wstring::npos, "Malformed BCD string");
        return {s};
    }
    Strings list;
    size_t at = 0;
    while (at < s.size() && s[at]) {
        const auto end = s.find(L'\0', at);
        list.emplace_back(s, at, end - at);
        at = end + 1;
    }
    check(at < s.size() && s.find_first_not_of(L'\0', at) == std::wstring::npos, "Malformed BCD identifier list");
    return list;
}
Strings references(HKEY obj, const wchar_t* field, DWORD type) {
    auto value = element(obj, field);
    if (!value) return {};
    auto ids = strings(*value, type);
    for (const auto& id : ids) check(is_guid(id), "Invalid BCD object reference");
    return ids;
}
bool flag(HKEY obj, const wchar_t* field) {
    auto value = element(obj, field);
    if (!value) return false;
    check(value->type == REG_BINARY && value->data.size() == 1, "Invalid BCD boolean");
    return value->data[0] != 0;
}
struct State {
    std::wstring owned;
    Strings display, sequence;
    bool minimal = false, persistent = false, external_ref = false;
    bool armed() const {
        return !owned.empty() && minimal && !persistent && !external_ref && !contains(display, owned) &&
               sequence.size() == 1 && same(sequence.front(), owned);
    }
};

State inspect(const Store& store, const std::wstring& title) {
    auto root = store.open();
    auto objects = subkey(root.key, L"Objects");
    auto boot = subkey(objects.key, manager);
    check(object_type(boot.key) == manager_type, "BCD boot manager type differs");
    State s;
    s.display = references(boot.key, L"24000001", REG_MULTI_SZ);
    s.sequence = references(boot.key, L"24000002", REG_MULTI_SZ);
    s.persistent = flag(boot.key, L"26000031");
    const auto ids = children(objects.key);
    for (const auto& id : ids) {
        check(is_guid(id), "Invalid BCD object key");
        auto obj = subkey(objects.key, id);
        if (object_type(obj.key) != loader_type) continue;
        const auto description = element(obj.key, L"12000004");
        if (!description || strings(*description, REG_SZ).front() != title) continue;
        check(s.owned.empty(), "Multiple boot entries match this recovery receipt");
        s.owned = id;
        auto safe = element(obj.key, L"25000080");
        s.minimal = safe && safe->type == REG_BINARY && safe->data == std::vector<BYTE>(8, 0) &&
                    !flag(obj.key, L"26000081");
    }
    if (s.owned.empty()) return s;
    // Never delete an entry someone selected as their default or linked from a different policy.
    for (const auto& id : ids) {
        if (same(id, s.owned)) continue;
        auto obj = subkey(objects.key, id);
        auto fields = subkey(obj.key, L"Elements", true);
        if (!fields.key) continue;
        for (const auto& field : children(fields.key)) {
            if (field.size() != 8 || (field[1] != L'3' && field[1] != L'4')) continue;
            if (same(id, manager) && (field == L"24000001" || field == L"24000002")) continue;
            if (contains(references(obj.key, field.c_str(), field[1] == L'3' ? REG_SZ : REG_MULTI_SZ), s.owned))
                s.external_ref = true;
        }
    }
    return s;
}

void disarm(const Store& store, const std::wstring& title) {
    auto state = inspect(store, title);
    if (state.owned.empty()) {
        // A prior disarm may have exited after deletion but before its final flush.
        store.flush();
        return;
    }
    check(!state.external_ref, "Recovery loader is referenced by another boot setting; leaving it in place");
    const std::wstring id = state.owned;
    if (contains(state.sequence, id)) store.command(L"/bootsequence " + id + L" /remove");
    if (contains(state.display, id)) store.command(L"/displayorder " + id + L" /remove");
    state = inspect(store, title);
    check(state.owned == id && !state.external_ref && !contains(state.sequence, id) && !contains(state.display, id),
          "Boot settings changed during recovery entry cleanup");
    store.command(L"/delete " + id + L" /nocleanup");
    check(inspect(store, title).owned.empty(), "Recovery loader still exists after cleanup");
    store.flush();
}
void check_available(const State& state) {
    check(!state.persistent, "Windows is set to retain the temporary boot sequence");
    check(!state.external_ref, "Another boot setting refers to this recovery entry");
    check(state.sequence.empty() || (state.sequence.size() == 1 && same(state.sequence.front(), state.owned)),
          "Another one-time boot is scheduled");
}
void arm(const Store& store, const std::wstring& title) {
    auto state = inspect(store, title);
    if (state.armed()) { store.flush(); return; }
    check_available(state);
    try {
        if (state.owned.empty()) {
            store.command(L"/copy " + store.source + L" /d \"" + title + L"\"");
            state = inspect(store, title);
        }
        const auto id = state.owned;
        check(!id.empty() && !state.external_ref, "Cannot identify the copied recovery loader");
        if (contains(state.display, id)) store.command(L"/displayorder " + id + L" /remove");
        store.command(L"/set " + id + L" safeboot minimal");
        store.command(L"/set " + id + L" safebootalternateshell No");
        state = inspect(store, title);
        check(state.owned == id && state.minimal && !state.persistent && !state.external_ref &&
              (state.sequence.empty() || (state.sequence.size() == 1 && same(state.sequence.front(), id))),
              "Cannot verify the Safe Mode recovery loader or another boot is pending");
        if (state.sequence.empty()) store.command(L"/bootsequence " + id + L" /addfirst");
        check(inspect(store, title).armed(), "One-time Safe Mode selection failed readback");
        store.flush();
    } catch (const std::exception& error) {
        try { disarm(store, title); }
        catch (const std::exception& rollback) {
            throw std::runtime_error(std::string(error.what()) + ". Recovery entry cleanup pending: " + rollback.what());
        }
        throw;
    }
}

Store isolated(const std::wstring& file, const std::wstring& source = L"") {
    check(file.size() > 3 && file[1] == L':' && file[2] == L'\\' && file.find(L'"') == std::wstring::npos &&
          (source.empty() || is_guid(source)), "Invalid isolated BCD fixture path or GUID");
    return {file, source};
}
} // namespace

bool clock_recovery_boot_supported() {
    using Version = LONG (WINAPI*)(OSVERSIONINFOW*);
    const auto query = reinterpret_cast<Version>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "RtlGetVersion"));
    OSVERSIONINFOW info{};
    info.dwOSVersionInfoSize = sizeof(info);
    return query && query(&info) == 0 && info.dwMajorVersion >= 6;
}
void check_clock_recovery_boot_available(const std::string& id) {
    check(clock_recovery_boot_supported(), "One-time Safe Mode boot requires Windows Vista or later");
    check(!GetSystemMetrics(SM_CLEANBOOT), "Return to normal Windows before arming a clock recovery boot");
    check_available(inspect(Store{}, label(id)));
}
void arm_clock_recovery_boot(const std::string& id) {
    const auto title = label(id);
    check(!GetSystemMetrics(SM_CLEANBOOT), "Return to normal Windows before arming a clock recovery boot");
    BootLock lock;
    arm(Store{}, title);
}
void disarm_clock_recovery_boot(const std::string& id) {
    const auto title = label(id);
    BootLock lock;
    disarm(Store{}, title);
}
bool clock_recovery_boot_is_armed(const std::string& id) { return inspect(Store{}, label(id)).armed(); }
namespace clock_boot_test {
void arm_file(const std::wstring& file, const std::wstring& source, const std::string& id) {
    arm(isolated(file, source), label(id));
}
void disarm_file(const std::wstring& file, const std::string& id) { disarm(isolated(file), label(id)); }
bool armed_file(const std::wstring& file, const std::string& id) { return inspect(isolated(file), label(id)).armed(); }
}
}
