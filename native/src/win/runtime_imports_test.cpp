// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// druta-runtime-imports-test.exe (MinGW only): runs the shims of
// runtime_imports.c, compiled into this executable alone with the
// DRUTA_RUNTIME_IMPORTS_TEST seam, through the same import pointers libgcc
// uses. The fallbacks are the only code of that file an XP user runs, and
// Wine (like every Windows from Vista) has the real exports, so the seam hides
// them by name: a hidden name is looked up under a name no DLL exports, which
// fails exactly as it does on XP. native/build.sh runs this under Wine.
//
// Without arguments the program runs every mode in a child process of its
// own (the shims cache their lookups for the life of a process) and kills a
// child that has not finished within kChildTimeoutMs, so it cannot hang a
// build. With a mode argument it runs that mode:
//   real      nothing hidden: the real kernel32 functions behind the shims
//   fallback  the five kernel32 names hidden: every fallback
//   partial   only WakeConditionVariable hidden: all four condition-variable
//             functions must fall back together
//   nontdll   the five names and ntdll's NtQueryInformationThread hidden
// Exit status 0 = every check passed. The program imports nothing beyond
// tools/xp-imports.txt (CMake audits it like the shipped binaries), so it
// keeps to plain threads, polling and the C library functions listed there.

#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <cwchar>

namespace {

const char* g_mode = "";
constexpr DWORD kChildTimeoutMs = 60000;
constexpr DWORD kSentinelError = 1234;

bool hidden_kernel32(const char* name) {
    const char* const hidden[] = {"InitializeConditionVariable", "WakeConditionVariable", "WakeAllConditionVariable",
                                  "SleepConditionVariableCS", "GetThreadId"};
    for (const char* candidate : hidden) {
        if (std::strcmp(name, candidate) == 0) return true;
    }
    return false;
}

}  // namespace

extern "C" int druta_runtime_imports_test_hide(const char* name) {
    if (std::strcmp(g_mode, "fallback") == 0) return hidden_kernel32(name);
    if (std::strcmp(g_mode, "partial") == 0) return std::strcmp(name, "WakeConditionVariable") == 0;
    if (std::strcmp(g_mode, "nontdll") == 0) {
        return hidden_kernel32(name) || std::strcmp(name, "NtQueryInformationThread") == 0;
    }
    return 0;
}
extern "C" long druta_runtime_imports_test_fallback_sleeps;

// The build's _WIN32_WINNT (XP) hides these Vista declarations; these are the
// SDK's own, so calls go through the import pointers like libgcc's.
extern "C" {
WINBASEAPI VOID WINAPI InitializeConditionVariable(PCONDITION_VARIABLE ConditionVariable);
WINBASEAPI VOID WINAPI WakeConditionVariable(PCONDITION_VARIABLE ConditionVariable);
WINBASEAPI VOID WINAPI WakeAllConditionVariable(PCONDITION_VARIABLE ConditionVariable);
WINBASEAPI WINBOOL WINAPI SleepConditionVariableCS(PCONDITION_VARIABLE ConditionVariable,
                                                   PCRITICAL_SECTION CriticalSection, DWORD dwMilliseconds);
}

// The import pointers the program calls through (the libgcc objects use the same ones).
#if defined(__i386__)
extern "C" void* druta_test_imp_sleep_cs __asm__("__imp__SleepConditionVariableCS@12");
extern "C" void* druta_test_imp_initialize __asm__("__imp__InitializeConditionVariable@4");
extern "C" void* druta_test_imp_get_thread_id __asm__("__imp__GetThreadId@4");
#else
extern "C" void* druta_test_imp_sleep_cs __asm__("__imp_SleepConditionVariableCS");
extern "C" void* druta_test_imp_initialize __asm__("__imp_InitializeConditionVariable");
extern "C" void* druta_test_imp_get_thread_id __asm__("__imp_GetThreadId");
#endif

namespace {

int g_failures = 0;

// Every line is flushed at once, so a child's lines and the parent's stay in order.
__attribute__((format(printf, 1, 2))) void say(const char* format, ...) {
    va_list arguments;
    va_start(arguments, format);
    std::vfprintf(stdout, format, arguments);
    va_end(arguments);
    std::fflush(stdout);
}

void check(bool ok, const char* what) {
    say("  %s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++g_failures;
}

// Waits for every thread, within `timeout_ms` in all; true if all finished.
bool wait_all(const HANDLE* threads, int count, DWORD timeout_ms) {
    DWORD start = GetTickCount();
    for (int i = 0; i < count; ++i) {
        DWORD spent = GetTickCount() - start;
        if (WaitForSingleObject(threads[i], spent < timeout_ms ? timeout_ms - spent : 0) != WAIT_OBJECT_0) return false;
    }
    return true;
}

volatile LONG g_go = 0;  // released by the main thread; the racers poll it
void wait_for_go() {
    while (InterlockedCompareExchange(&g_go, 0, 0) == 0) Sleep(1);
}

long fallback_sleeps() { return __atomic_load_n(&druta_runtime_imports_test_fallback_sleeps, __ATOMIC_SEQ_CST); }

bool in_this_module(void* address) {
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery(address, &info, sizeof info)) return false;
    return info.AllocationBase == static_cast<void*>(GetModuleHandleW(nullptr));
}

void* kernel32_export(const char* name) {
    FARPROC address = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), name);
    void* result;
    std::memcpy(&result, &address, sizeof result);
    return result;
}

// ---- last error across the first function-local static (X1) -----------------------------------

struct First {
    int value;
    First() : value(7) {}
};
int first_static() {
    static First first;  // the process's first guarded initialization: its release reaches the shims
    return first.value;
}

// ---- races on one function-local static -------------------------------------------------------

volatile LONG g_constructions = 0;
struct Slow {
    int value;
    Slow() : value(0) {
        InterlockedIncrement(&g_constructions);
        Sleep(300);
        value = 42;
    }
};
Slow& slow() {
    static Slow instance;
    return instance;
}
Slow* g_seen[2];
DWORD WINAPI racer(LPVOID index) {
    g_seen[reinterpret_cast<INT_PTR>(index)] = &slow();
    return 0;
}

volatile LONG g_attempts = 0, g_constructed = 0, g_thrown = 0;
struct ThrowsOnce {
    ThrowsOnce() {
        LONG attempt = InterlockedIncrement(&g_attempts);
        Sleep(20);
        if (attempt == 1) throw 1;
    }
};
void throws_once() { static ThrowsOnce instance; }
DWORD WINAPI abort_racer(LPVOID) {
    wait_for_go();
    try {
        throws_once();
        InterlockedIncrement(&g_constructed);
    } catch (int) {
        InterlockedIncrement(&g_thrown);
    }
    return 0;
}

DWORD WINAPI idle(LPVOID) {
    Sleep(500);
    return 0;
}

// GetThreadId on the current thread: the thread queries its own handle, which
// the main thread publishes after CreateThread returns it.
HANDLE volatile g_own_handle = nullptr;
DWORD g_own_id = 0, g_own_result = 0, g_own_error = 0;
DWORD WINAPI query_self(LPVOID) {
    wait_for_go();
    g_own_id = GetCurrentThreadId();
    SetLastError(kSentinelError);
    g_own_result = GetThreadId(g_own_handle);
    g_own_error = GetLastError();
    return 0;
}

// Another thread that tries to take `section` without blocking.
struct TryArgs {
    CRITICAL_SECTION* section;
    BOOL entered;
};
DWORD WINAPI try_enter(LPVOID raw) {
    TryArgs* args = static_cast<TryArgs*>(raw);
    args->entered = TryEnterCriticalSection(args->section);
    if (args->entered) LeaveCriticalSection(args->section);
    return 0;
}
bool other_thread_can_enter(CRITICAL_SECTION* section) {
    TryArgs args = {section, FALSE};
    HANDLE thread = CreateThread(nullptr, 0, try_enter, &args, 0, nullptr);
    WaitForSingleObject(thread, INFINITE);
    CloseHandle(thread);
    return args.entered != FALSE;
}

void run_mode() {
    const bool real = std::strcmp(g_mode, "real") == 0;
    say("mode %s\n", g_mode);

    // Last error across the first static initialization: the real functions
    // leave it alone, so must every path of the shims.
    SetLastError(kSentinelError);
    int first = first_static();
    check(first == 7 && GetLastError() == kSentinelError, "first static initialization keeps the last error");

    // The program's import pointers are the shims, in this module, not kernel32's exports.
    check(druta_test_imp_sleep_cs != kernel32_export("SleepConditionVariableCS") && in_this_module(druta_test_imp_sleep_cs),
          "SleepConditionVariableCS import pointer is the shim");
    check(druta_test_imp_initialize != kernel32_export("InitializeConditionVariable") &&
              in_this_module(druta_test_imp_initialize),
          "InitializeConditionVariable import pointer is the shim");
    check(druta_test_imp_get_thread_id != kernel32_export("GetThreadId") && in_this_module(druta_test_imp_get_thread_id),
          "GetThreadId import pointer is the shim");

    // ---- condition variable ----
    CONDITION_VARIABLE condition;
    std::memset(&condition, 0xA5, sizeof condition);
    InitializeConditionVariable(&condition);
    check(condition.Ptr == nullptr, "InitializeConditionVariable zeroes the variable");
    WakeConditionVariable(&condition);
    WakeAllConditionVariable(&condition);
    check(true, "WakeConditionVariable and WakeAllConditionVariable return");

    CRITICAL_SECTION section;
    InitializeCriticalSection(&section);
    EnterCriticalSection(&section);
    long sleeps_before = fallback_sleeps();
    DWORD start = GetTickCount();
    SetLastError(0);
    BOOL woke = SleepConditionVariableCS(&condition, &section, 200);
    DWORD error = GetLastError();
    DWORD elapsed = GetTickCount() - start;
    say("  SleepConditionVariableCS(200 ms, no waker) -> %d, error %lu, %lu ms\n", woke, error, elapsed);
    if (real) {
        check(woke == FALSE && error == ERROR_TIMEOUT && elapsed >= 150, "real wait times out with ERROR_TIMEOUT");
        check(fallback_sleeps() == sleeps_before, "no fallback wait");
    } else {
        check(woke == TRUE && elapsed < 150, "fallback wait returns TRUE promptly (a spurious wake-up)");
        check(fallback_sleeps() == sleeps_before + 1, "the fallback wait ran");
    }
    check(!other_thread_can_enter(&section), "the critical section is held again after the wait");
    LeaveCriticalSection(&section);
    check(other_thread_can_enter(&section), "and usable by another thread once left");

    // Held recursively (depth 2). The fallback leaves the section once, as Wine's
    // real SleepConditionVariableCS does, so during its 1 ms sleep the section is
    // still held at depth 1 and no other thread can enter it; it returns at depth 2.
    // That is correct for callers that wait holding the section once (guard.cc
    // does); a caller that waits while holding it recursively, for a waker that
    // needs the section, would poll without progress, as the real function would
    // block without progress.
    if (!real) {
        EnterCriticalSection(&section);
        EnterCriticalSection(&section);
        woke = SleepConditionVariableCS(&condition, &section, INFINITE);
        check(woke == TRUE, "recursive: the fallback wait returns");
        LeaveCriticalSection(&section);
        check(!other_thread_can_enter(&section), "recursive: still held after one leave (depth restored to 2)");
        LeaveCriticalSection(&section);
        check(other_thread_can_enter(&section), "recursive: free after the second leave");
    }
    DeleteCriticalSection(&section);

    // ---- GetThreadId ----
    InterlockedExchange(&g_go, 0);
    HANDLE self = CreateThread(nullptr, 0, query_self, nullptr, 0, nullptr);
    g_own_handle = self;
    InterlockedExchange(&g_go, 1);
    WaitForSingleObject(self, INFINITE);
    DWORD mine = g_own_result, error_after_mine = g_own_error;
    DWORD other_id = 0;
    HANDLE other = CreateThread(nullptr, 0, idle, nullptr, 0, &other_id);
    SetLastError(kSentinelError);
    DWORD theirs = GetThreadId(other);
    error = GetLastError();
    say("  GetThreadId -> %lu (self %lu), %lu (other %lu), last error %lu then %lu\n", mine, g_own_id, theirs,
        other_id, error_after_mine, error);
    if (std::strcmp(g_mode, "nontdll") == 0) {
        check(mine == 0 && theirs == 0 && error == ERROR_CALL_NOT_IMPLEMENTED,
              "no kernel32 or ntdll entry: 0 with ERROR_CALL_NOT_IMPLEMENTED");
    } else {
        check(mine == g_own_id && mine != 0 && theirs == other_id, "ids of the current and another thread");
        check(error_after_mine == kSentinelError && error == kSentinelError, "a successful GetThreadId keeps the last error");
        SetLastError(0);
        DWORD bad = GetThreadId(reinterpret_cast<HANDLE>(static_cast<INT_PTR>(0x12340)));
        error = GetLastError();
        say("  GetThreadId(bad handle) -> %lu, error %lu\n", bad, error);
        check(bad == 0 && error != 0, "a bad handle gives 0 and the system's error");
    }
    WaitForSingleObject(other, INFINITE);
    CloseHandle(other);
    CloseHandle(self);

    // ---- two threads race for one function-local static ----
    sleeps_before = fallback_sleeps();
    HANDLE threads[4];
    for (INT_PTR i = 0; i < 2; ++i) threads[i] = CreateThread(nullptr, 0, racer, reinterpret_cast<LPVOID>(i), 0, nullptr);
    bool done = wait_all(threads, 2, 10000);
    long race_sleeps = fallback_sleeps() - sleeps_before;
    say("  race: finished %d, constructions %ld, fallback waits %ld\n", done, g_constructions, race_sleeps);
    check(done, "the race completes within 10 s");
    check(g_constructions == 1 && g_seen[0] && g_seen[0] == g_seen[1] && g_seen[0]->value == 42,
          "one construction, the same object for both threads");
    check(real ? race_sleeps == 0 : race_sleeps > 10, real ? "the loser blocked in the real wait"
                                                           : "the loser polled through the fallback wait");
    for (int i = 0; i < 2; ++i) CloseHandle(threads[i]);

    // ---- a constructor that throws once (guard abort), then a successful retry ----
    InterlockedExchange(&g_go, 0);
    for (int i = 0; i < 4; ++i) threads[i] = CreateThread(nullptr, 0, abort_racer, nullptr, 0, nullptr);
    InterlockedExchange(&g_go, 1);
    done = wait_all(threads, 4, 10000);
    say("  guard abort: finished %d, attempts %ld, constructed %ld, thrown %ld\n", done, g_attempts, g_constructed,
        g_thrown);
    check(done && g_attempts == 2 && g_constructed == 3 && g_thrown == 1,
          "one throw aborts the guard, one retry constructs, the others see it");
    for (int i = 0; i < 4; ++i) CloseHandle(threads[i]);
}

int run_children() {
    wchar_t self[MAX_PATH];
    if (!GetModuleFileNameW(nullptr, self, MAX_PATH)) return 2;
    const wchar_t* modes[] = {L"real", L"fallback", L"partial", L"nontdll"};
    int failed = 0;
    for (const wchar_t* mode : modes) {
        wchar_t command[MAX_PATH + 32];  // "<self>" <mode>
        std::size_t self_length = std::wcslen(self), mode_length = std::wcslen(mode);
        command[0] = L'"';
        std::memcpy(command + 1, self, self_length * sizeof(wchar_t));
        command[self_length + 1] = L'"';
        command[self_length + 2] = L' ';
        std::memcpy(command + self_length + 3, mode, (mode_length + 1) * sizeof(wchar_t));
        STARTUPINFOW startup;
        std::memset(&startup, 0, sizeof startup);
        startup.cb = sizeof startup;
        PROCESS_INFORMATION process;
        if (!CreateProcessW(self, command, nullptr, nullptr, FALSE, 0, nullptr, nullptr, &startup, &process)) {
            say("mode %ls: cannot start (error %lu)\n", mode, GetLastError());
            ++failed;
            continue;
        }
        DWORD code = 1;
        if (WaitForSingleObject(process.hProcess, kChildTimeoutMs) != WAIT_OBJECT_0) {
            say("mode %ls: no result within %lu ms, killed\n", mode, kChildTimeoutMs);
            TerminateProcess(process.hProcess, 3);
            WaitForSingleObject(process.hProcess, INFINITE);
        } else {
            GetExitCodeProcess(process.hProcess, &code);
        }
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        if (code != 0) ++failed;
    }
    say("runtime imports test: %d of 4 modes failed\n", failed);
    return failed ? 1 : 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) return run_children();
    g_mode = argv[1];
    if (std::strcmp(g_mode, "real") && std::strcmp(g_mode, "fallback") && std::strcmp(g_mode, "partial") &&
        std::strcmp(g_mode, "nontdll")) {
        say("unknown mode %s\n", g_mode);
        return 2;
    }
    run_mode();
    say("mode %s: %d failure(s)\n", g_mode, g_failures);
    return g_failures ? 1 : 0;
}
