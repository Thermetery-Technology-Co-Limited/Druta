// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Console test: compile with profile_io.cpp and Win32 headers/libs, C++17.
// Run with one argument: an existing native/build directory. CTest should pass
// its native build directory explicitly. All files live in a newly owned child.
// Checks execute in Release too; no GPU, Session or private profile paths.
#include "../profile_io.h"
#include <windows.h>
#include <cstdio>
#include <functional>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
using namespace druta::ui;
int checks = 0, failures = 0;
void check(bool condition, const char* message) {
    ++checks;
    if (!condition) {
        ++failures;
        std::printf("FAIL: %s\n", message);
    }
}
void must(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

struct Handle {
    HANDLE value = INVALID_HANDLE_VALUE;
    explicit Handle(HANDLE handle) : value(handle) {}
    ~Handle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
};

struct TestDirectory {
    std::wstring path;
    explicit TestDirectory(const std::wstring& parent) {
        wchar_t absolute[32768]{};
        const DWORD length = GetFullPathNameW(parent.c_str(), 32768, absolute, nullptr);
        must(length > 0 && length < 32768, "cannot resolve test build directory");
        const DWORD attributes = GetFileAttributesW(absolute);
        must(attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_DIRECTORY),
            "test build directory must already exist");
        for (unsigned int i = 0; i < 32; ++i) {
            path = std::wstring(absolute) + L"\\profile-io-test-" + std::to_wstring(GetCurrentProcessId())
                + L"-" + std::to_wstring(GetTickCount()) + L"-" + std::to_wstring(i);
            if (CreateDirectoryW(path.c_str(), nullptr)) return;
            const DWORD code = GetLastError();
            must(code == ERROR_ALREADY_EXISTS, "cannot create owned profile test directory");
        }
        throw std::runtime_error("cannot allocate unique profile test directory");
    }
    ~TestDirectory() {
        // Only this constructor's newly created directory is enumerated. No
        // recursive deletion, shell expansion or caller-owned files are used.
        WIN32_FIND_DATAW entry{};
        const std::wstring extended = L"\\\\?\\" + path;
        const HANDLE search = FindFirstFileW((extended + L"\\*").c_str(), &entry);
        if (search != INVALID_HANDLE_VALUE) {
            do {
                if (!(entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
                    DeleteFileW((extended + L"\\" + entry.cFileName).c_str());
            } while (FindNextFileW(search, &entry));
            FindClose(search);
        }
        RemoveDirectoryW(extended.c_str());
    }
    std::wstring file(const std::wstring& name) const { return path + L"\\" + name; }
};

void raw_write(const std::wstring& path, const std::string& bytes) {
    Handle file(CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr));
    must(file.value != INVALID_HANDLE_VALUE, "cannot create owned test input");
    size_t offset = 0;
    while (offset < bytes.size()) {
        DWORD written = 0;
        must(WriteFile(file.value, bytes.data() + offset, static_cast<DWORD>(bytes.size() - offset), &written, nullptr)
            && written > 0, "cannot write owned test input");
        offset += written;
    }
}

void expect_failure(const std::function<void()>& action, const char* contains, const char* message) {
    try {
        action();
        check(false, message);
    } catch (const std::runtime_error& error) {
        check(std::string(error.what()).find(contains) != std::string::npos, message);
    } catch (const std::exception&) {
        check(false, "profile failure should be a useful std::runtime_error");
    }
}

unsigned int temporary_count(const TestDirectory& directory) {
    WIN32_FIND_DATAW entry{};
    const auto pattern = directory.file(L"*.druta-tmp-*");
    const HANDLE search = FindFirstFileW(pattern.c_str(), &entry);
    if (search == INVALID_HANDLE_VALUE) {
        const DWORD code = GetLastError();
        must(code == ERROR_FILE_NOT_FOUND, "cannot inspect owned temporary file cleanup");
        return 0;
    }
    unsigned int count = 0;
    do { ++count; } while (FindNextFileW(search, &entry));
    FindClose(search);
    return count;
}

void roundtrip_and_replacement(const TestDirectory& directory) {
    const auto path = directory.file(L"调校-配置-Δ.json");
    const ProfileJson original = {
        {"schema", 2}, {"scope", "tuning_action"}, {"value", -1.25},
        {"device", {{"name", u8"不同适配器"}, {"slot", "0000:02:00.0"}}},
        {"unknown_field", {{"keep", true}, {"nested", ProfileJson::array({1, "two", nullptr})}}},
        {"large_unsigned", std::numeric_limits<unsigned long long>::max()}
    };
    write_profile(path, original);
    check(read_profile(path) == original, "Unicode path and UTF-8 values roundtrip without losing unknown fields or exact integers");
    const ProfileJson replacement = {{"schema", 2}, {"replacement", true}, {"fractional", 7.125}};
    write_profile(path, replacement);
    check(read_profile(path) == replacement, "explicit Save replaces an existing file completely");
    check(temporary_count(directory) == 0, "successful saves leave no temporary files");
    const auto empty_path = directory.file(L"empty-object.json");
    write_profile(empty_path, ProfileJson::object());
    check(read_profile(empty_path).is_object() && read_profile(empty_path).empty(), "empty object is valid at the I/O boundary");
}

void invalid_inputs(const TestDirectory& directory) {
    const auto path = directory.file(L"invalid.json");
    for (const auto& bytes : {std::string("[]"), std::string("null"), std::string("true"),
            std::string("42"), std::string("\"text\"")}) {
        raw_write(path, bytes);
        expect_failure([&] { (void)read_profile(path); }, "root must be an object", "non-object JSON root is rejected");
    }
    for (const auto& bytes : {std::string(""), std::string("{\"a\": }"), std::string("{} trailing"),
            std::string("{\"n\":1e999}"), std::string("{\"text\":\"\xff\"}")}) {
        raw_write(path, bytes);
        expect_failure([&] { (void)read_profile(path); }, "Read profile", "malformed or non-UTF-8 input reports a contextual parse error");
    }
    for (const auto& bytes : {std::string("{\"a\":1,\"a\":2}"),
            std::string("{\"array\":[{\"nested\":1,\"nested\":2}]}"),
            std::string("{\"na\\u006de\":1,\"name\":2}")}) {
        raw_write(path, bytes);
        expect_failure([&] { (void)read_profile(path); }, "Duplicate JSON key", "duplicate decoded keys are rejected at any nesting level");
    }
    raw_write(path, "{\"array\":[{\"key\":1},{\"key\":2}],\"other\":{\"key\":3}}");
    check(read_profile(path)["array"].size() == 2, "identical key names in distinct objects remain valid");
    expect_failure([&] { (void)read_profile(L""); }, "Profile path", "empty path is rejected before file access");
    expect_failure([&] { write_profile(std::wstring(L"x\0y", 3), ProfileJson::object()); },
        "Profile path", "embedded NUL path cannot target a truncated filename");
    expect_failure([&] { (void)read_profile(directory.file(L"never-created.json")); },
        "Open profile", "missing explicit file reports an open error");
}

void size_bounds(const TestDirectory& directory) {
    const auto path = directory.file(L"bound.json");
    const std::string exact = "{\"x\":\"" + std::string(profile_max_bytes - 8, 'x') + "\"}";
    check(exact.size() == profile_max_bytes, "test constructs an exact 1 MiB document");
    raw_write(path, exact);
    check(read_profile(path)["x"].get_ref<const std::string&>().size() == profile_max_bytes - 8,
        "read accepts an object exactly at the 1 MiB bound");
    raw_write(path, exact + "\n");
    expect_failure([&] { (void)read_profile(path); }, "1 MiB", "read rejects oversized files before parsing");
    expect_failure([&] { (void)read_profile(path,64*1024); }, "65536 byte", "iCX read uses its stricter size limit");
    const ProfileJson original = {{"original", true}};
    write_profile(path, original);
    const ProfileJson large = {{"payload", std::string(profile_max_bytes, 'a')}};
    expect_failure([&] { write_profile(path, large); }, "1 MiB", "oversized Save is refused before destination replacement");
    check(read_profile(path) == original, "size refusal preserves existing profile");
}

void preserve_on_failure(const TestDirectory& directory) {
    const auto path = directory.file(L"preserve.json");
    const ProfileJson original = {{"original", true}, {"value", 1.25}};
    write_profile(path, original);
    expect_failure([&] { write_profile(path, ProfileJson::array({1, 2})); },
        "root must be an object", "non-object Save is rejected");
    const ProfileJson not_finite = {{"value", std::numeric_limits<double>::quiet_NaN()}};
    expect_failure([&] { write_profile(path, not_finite); }, "non-finite", "NaN cannot silently become null during Save");
    const ProfileJson bad_utf8 = {{"value", std::string(1, static_cast<char>(0xff))}};
    expect_failure([&] { write_profile(path, bad_utf8); }, "Serialize profile", "invalid UTF-8 cannot replace the original");
    check(read_profile(path) == original, "serialization failures preserve the original file");
    {
        // Denying delete sharing deterministically fails the final replacement,
        // after its temporary file was created, written and flushed.
        Handle lock(CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr));
        must(lock.value != INVALID_HANDLE_VALUE, "cannot hold owned replacement-failure test file");
        expect_failure([&] { write_profile(path, ProfileJson{{"replacement", 2}}); },
            "Replace profile", "failed replacement reports an error instead of claiming Save success");
    }
    check(read_profile(path) == original, "failure after temporary write preserves the original");
    check(temporary_count(directory) == 0, "failed replacement cleans its owned temporary file");

    // A valid 245-character component leaves no room for the temporary suffix.
    // Extended paths avoid conflating the component limit with MAX_PATH.
    const auto long_path = L"\\\\?\\" + directory.file(std::wstring(240, L'x') + L".json");
    raw_write(long_path, original.dump());
    expect_failure([&] { write_profile(long_path, ProfileJson{{"replacement", 3}}); },
        "Create profile temporary file", "temporary creation failure is surfaced before original replacement");
    check(read_profile(long_path) == original, "temporary creation failure preserves the original");
    check(temporary_count(directory) == 0, "temporary creation failure leaves no owned temporary file");
}
} // namespace

int wmain(int argc, wchar_t* argv[]) {
    if (argc != 2) {
        std::puts("Usage: druta-ui-profile-io-test <existing native/build directory>");
        return 2;
    }
    try {
        TestDirectory directory(argv[1]);
        roundtrip_and_replacement(directory);
        invalid_inputs(directory);
        size_bounds(directory);
        preserve_on_failure(directory);
    } catch (const std::exception& error) {
        check(false, error.what());
    }
    std::printf("%d profile I/O checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
