// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_io.h"
#include <windows.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <stdexcept>
#include <unordered_set>
#include <vector>

namespace druta::ui {
namespace {
std::string utf8(const std::wstring& text) {
    if (text.empty()) return {};
    const int length = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.c_str(), -1,
        nullptr, 0, nullptr, nullptr);
    if (length <= 0) return "<unprintable path>";
    std::string result(static_cast<size_t>(length), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.c_str(), -1,
        result.data(), length, nullptr, nullptr)) return "<unprintable path>";
    result.pop_back();
    return result;
}

[[noreturn]] void failure(const char* operation, const std::wstring& path, DWORD code) {
    wchar_t message[1024]{};
    FormatMessageW(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, code, 0, message, static_cast<DWORD>(std::size(message)), nullptr);
    std::wstring text(message);
    while (!text.empty() && (text.back() == L'\r' || text.back() == L'\n' || text.back() == L' ')) text.pop_back();
    throw std::runtime_error(std::string(operation) + " '" + utf8(path) + "': Windows error "
        + std::to_string(code) + (text.empty() ? "" : " (" + utf8(text) + ")"));
}

std::wstring absolute_path(const std::wstring& path) {
    if (path.empty() || path.find(L'\0') != std::wstring::npos)
        throw std::runtime_error("Profile path must be nonempty and contain no NUL characters");
    DWORD needed = GetFullPathNameW(path.c_str(), 0, nullptr, nullptr);
    if (!needed) failure("Resolve profile path", path, GetLastError());
    std::wstring result(needed, L'\0');
    const DWORD length = GetFullPathNameW(path.c_str(), needed, result.data(), nullptr);
    if (!length) failure("Resolve profile path", path, GetLastError());
    if (length >= needed) throw std::runtime_error("Profile path changed while resolving '" + utf8(path) + "'");
    result.resize(length);
    return result;
}

struct File {
    HANDLE handle = INVALID_HANDLE_VALUE;
    explicit File(HANDLE value) : handle(value) {}
    ~File() { close(); }
    File(const File&) = delete;
    File& operator=(const File&) = delete;
    void close() noexcept {
        if (handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
        handle = INVALID_HANDLE_VALUE;
    }
};

struct TemporaryFile {
    std::wstring path;
    File file{INVALID_HANDLE_VALUE};
    bool owned = false;
    ~TemporaryFile() {
        file.close();
        if (owned) DeleteFileW(path.c_str());
    }
};

void create_temporary(const std::wstring& destination, TemporaryFile& temporary) {
    static std::atomic<unsigned long> serial{0};
    for (unsigned int attempt = 0; attempt < 32; ++attempt) {
        temporary.path = destination + L".druta-tmp-" + std::to_wstring(GetCurrentProcessId())
            + L"-" + std::to_wstring(serial.fetch_add(1, std::memory_order_relaxed));
        temporary.file.handle = CreateFileW(temporary.path.c_str(), GENERIC_WRITE, 0, nullptr,
            CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (temporary.file.handle != INVALID_HANDLE_VALUE) {
            temporary.owned = true;
            return;
        }
        const DWORD code = GetLastError();
        if (code != ERROR_FILE_EXISTS && code != ERROR_ALREADY_EXISTS)
            failure("Create profile temporary file", temporary.path, code);
    }
    throw std::runtime_error("Could not create a unique profile temporary file beside '" + utf8(destination) + "'");
}

void require_object(const ProfileJson& document) {
    if (!document.is_object()) throw std::runtime_error("Profile JSON root must be an object");
}

void require_finite(const ProfileJson& document) {
    // JSON does not represent NaN/infinity; the upstream dumper otherwise turns them into null.
    std::vector<const ProfileJson*> pending{&document};
    while (!pending.empty()) {
        const auto* value = pending.back();
        pending.pop_back();
        if (value->is_number_float() && !std::isfinite(value->get<double>()))
            throw std::runtime_error("Profile JSON contains a non-finite number");
        if (value->is_structured()) for (const auto& child : *value) pending.push_back(&child);
    }
}
} // namespace

ProfileJson read_profile(const std::wstring& path) {
    const auto full_path = absolute_path(path);
    std::string bytes;
    {
        File file(CreateFileW(full_path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
        if (file.handle == INVALID_HANDLE_VALUE) failure("Open profile", full_path, GetLastError());
        LARGE_INTEGER size{};
        if (!GetFileSizeEx(file.handle, &size)) failure("Read profile size", full_path, GetLastError());
        if (size.QuadPart < 0 || size.QuadPart > static_cast<LONGLONG>(profile_max_bytes))
            throw std::runtime_error("Profile exceeds the 1 MiB limit: '" + utf8(full_path) + "'");
        bytes.resize(static_cast<size_t>(size.QuadPart));
        size_t offset = 0;
        while (offset < bytes.size()) {
            DWORD got = 0;
            const DWORD count = static_cast<DWORD>(bytes.size() - offset);
            if (!ReadFile(file.handle, bytes.data() + offset, count, &got, nullptr))
                failure("Read profile", full_path, GetLastError());
            if (!got) throw std::runtime_error("Profile was truncated while reading: '" + utf8(full_path) + "'");
            offset += got;
        }
        char extra = 0;
        DWORD got = 0;
        if (!ReadFile(file.handle, &extra, 1, &got, nullptr)) failure("Read profile", full_path, GetLastError());
        if (got) throw std::runtime_error("Profile changed size while reading: '" + utf8(full_path) + "'");
    }
    try {
        std::vector<std::unordered_set<std::string>> keys;
        const auto callback = [&keys](int, ProfileJson::parse_event_t event, ProfileJson& parsed) {
            if (event == ProfileJson::parse_event_t::object_start) keys.emplace_back();
            else if (event == ProfileJson::parse_event_t::object_end) keys.pop_back();
            else if (event == ProfileJson::parse_event_t::key && !keys.back().insert(parsed.get<std::string>()).second)
                throw std::runtime_error("Duplicate JSON key: " + parsed.get<std::string>().substr(0, 96));
            return true;
        };
        auto document = ProfileJson::parse(bytes, callback);
        require_object(document);
        return document;
    } catch (const std::exception& error) {
        throw std::runtime_error("Read profile '" + utf8(full_path) + "': " + error.what());
    }
}

void write_profile(const std::wstring& path, const ProfileJson& document) {
    const auto full_path = absolute_path(path);
    std::string bytes;
    try {
        require_object(document);
        require_finite(document);
        bytes = document.dump(2);
        bytes += '\n';
        if (bytes.size() > profile_max_bytes) throw std::runtime_error("Profile exceeds the 1 MiB limit");
    } catch (const std::exception& error) {
        throw std::runtime_error("Serialize profile '" + utf8(full_path) + "': " + error.what());
    }
    TemporaryFile temporary;
    create_temporary(full_path, temporary);
    size_t offset = 0;
    while (offset < bytes.size()) {
        DWORD written = 0;
        const DWORD count = static_cast<DWORD>(bytes.size() - offset);
        if (!WriteFile(temporary.file.handle, bytes.data() + offset, count, &written, nullptr))
            failure("Write profile temporary file", temporary.path, GetLastError());
        if (!written) throw std::runtime_error("Profile temporary write made no progress: '" + utf8(temporary.path) + "'");
        offset += written;
    }
    if (!FlushFileBuffers(temporary.file.handle)) failure("Flush profile temporary file", temporary.path, GetLastError());
    if (!CloseHandle(temporary.file.handle)) failure("Close profile temporary file", temporary.path, GetLastError());
    temporary.file.handle = INVALID_HANDLE_VALUE;
    if (!MoveFileExW(temporary.path.c_str(), full_path.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        failure("Replace profile", full_path, GetLastError());
    temporary.owned = false;
}
}
