/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Windows XP import shims for the MinGW-w64 GCC runtime (MinGW builds only).
 *
 * WHY THIS FILE EXISTS
 * The Windows binaries must load on XP SP3 (x86) and XP x64 SP2. The loader
 * refuses an image before any of its code runs when one imported function is
 * missing from the DLL that is named. GCC 13's win32 thread model (the
 * toolchain's `*-w64-mingw32-g++-win32` flavour) links two libgcc.a members
 * that import kernel32 functions XP does not export:
 *
 *   libgcc.a(gthr-win32-cond.o)  InitializeConditionVariable,
 *                                SleepConditionVariableCS,
 *                                WakeAllConditionVariable,
 *                                WakeConditionVariable      (Vista and later)
 *   libgcc.a(gthr-win32.o)       GetThreadId                (Vista, Server
 *                                                            2003; not XP x86)
 *
 * libstdc++.a(guard.o) pulls both in: __cxa_guard_acquire/release (the
 * thread-safe initialization of every function-local static) use the gthr
 * once/mutex/condition-variable functions, so in drutacore.dll the condition
 * variable functions are not only imported but CALLED on the first
 * initialization of a function-local static (InitializeConditionVariable
 * through the once-initialized static condition variable, WakeAllCondition-
 * Variable on every guard release, SleepConditionVariableCS when two threads
 * race for the same static). In gthr-win32.o only __gthr_win32_equal calls
 * GetThreadId and nothing in these binaries calls that, but the import alone
 * stops the load. The executables link gthr-win32.o too (libstdc++'s
 * eh_alloc.o uses its mutex) and so import GetThreadId as well.
 * The posix thread model is no way out: its winpthreads imports
 * GetTickCount64, also Vista and later.
 *
 * WHAT IT DOES
 * The libgcc objects call these functions through import pointers
 * (__imp_Name on x86_64, __imp__Name@N on x86, N = stdcall argument bytes).
 * This file defines those pointers itself. An object file on the link line
 * is loaded before any archive is searched, so the references from libgcc
 * resolve here and the linker never pulls the libkernel32.a members that
 * would have created the imports. (Were anything to reference the plain
 * kernel32 thunk as well, the libkernel32.a member would come in with its
 * own __imp_ definition and the link would fail on the duplicate: it cannot
 * silently pick the other definition.) Each pointer leads to a shim that
 * looks the real export up once, at run time, with GetProcAddress, the same
 * capability probe the core uses for every other Windows export:
 *
 *   * present (Vista and later, Server 2003 / XP x64 for GetThreadId): the
 *     shim calls the real function with the same arguments, so behaviour
 *     there is what it was with the direct import;
 *   * absent (XP): a correct fallback, below.
 *
 * The four condition-variable functions are resolved as one group: either
 * all four real functions are used or all four fallbacks. Mixing would be a
 * deadlock (a real infinite SleepConditionVariableCS woken by a no-op Wake).
 *
 * Fallbacks. A condition-variable wait may return spuriously, and every
 * correct caller re-checks its predicate in a loop (guard.cc does), so:
 * InitializeConditionVariable zeroes the one-pointer structure (the same as
 * CONDITION_VARIABLE_INIT), Wake/WakeAll do nothing, and
 * SleepConditionVariableCS leaves the critical section, sleeps 1 ms, enters
 * it again and returns TRUE: a spurious wake-up. The waiter polls instead of
 * blocking; that happens only while another thread is initializing the same
 * function-local static. GetThreadId falls back to ntdll's
 * NtQueryInformationThread(ThreadBasicInformation), itself resolved at run
 * time, and otherwise returns 0 with ERROR_CALL_NOT_IMPLEMENTED.
 *
 * Constraints. These shims run underneath the C++ runtime's own locking, so
 * they must not need it: C, no function-local static with a run-time
 * initializer, no C++ runtime lock. Each lookup result is cached in a static
 * pointer with atomic loads and stores; two threads that race simply both
 * look the export up and store the same value. Everything here is zero-
 * initialized data, so the shims work before the C runtime is initialized.
 * Nothing here decides by Windows version, and nothing in a shipped binary
 * can be switched from outside: DRUTA_RUNTIME_IMPORTS_TEST, a compile-time
 * seam defined only for druta-runtime-imports-test.exe (runtime_imports_test.cpp,
 * run under Wine by native/build.sh), is the only way to force the fallbacks.
 * A lookup that fails restores the caller's last-error code (see lookup()).
 *
 * WHEN IT CAN BE DELETED
 * When no Windows binary built without this file imports any of the five
 * names: delete the file and its line in CMakeLists.txt, rebuild, and run
 * native/tools/check_imports.py (build.sh runs it after every MinGW build).
 * That is the case if the toolchain's libgcc no longer uses these functions
 * or the XP requirement is dropped. If check_imports.py reports another
 * name, add it here the same way, only with a fallback that is correct.
 */

#if defined(_WIN32) && defined(__GNUC__)

#include <windows.h>

/* ---- run-time lookup --------------------------------------------------------------------------- */

/* NULL: not looked up yet; ABSENT: looked up and missing; otherwise the export. */
#define ABSENT ((void*)(ULONG_PTR)1)

#ifdef DRUTA_RUNTIME_IMPORTS_TEST
/* Test seam (druta-runtime-imports-test.exe only): the test defines this to
   hide exports by name. A hidden name is looked up under a name no DLL
   exports, so the lookup fails exactly as it does on a system without the
   export, last-error side effect included. */
int druta_runtime_imports_test_hide(const char* name);
#endif

/* The export, or NULL. A failed lookup leaves the thread's last-error code as
   it was: GetProcAddress sets ERROR_PROC_NOT_FOUND when it fails, and the real
   functions these shims stand in for never touch the last error on success,
   so without this the first function-local static initialization of a module
   (or the first GetThreadId) on XP would leave 127 behind for its caller. */
static void* lookup(const wchar_t* module, const char* name)
{
#ifdef DRUTA_RUNTIME_IMPORTS_TEST
    if (druta_runtime_imports_test_hide(name)) name = "DrutaRuntimeImportsTestNoSuchExport";
#endif
    DWORD saved = GetLastError();
    HMODULE handle = GetModuleHandleW(module); /* kernel32 and ntdll are mapped in every process */
    FARPROC address = handle ? GetProcAddress(handle, name) : NULL;
    void* result;
    /* FARPROC to object pointer: same size on Windows; memcpy keeps -Wpedantic quiet. */
    CopyMemory(&result, &address, sizeof result);
    if (!result) SetLastError(saved);
    return result;
}

static void* cached(void** slot, const wchar_t* module, const char* name)
{
    void* value = __atomic_load_n(slot, __ATOMIC_ACQUIRE);
    if (!value) {
        value = lookup(module, name);
        if (!value) value = ABSENT;
        __atomic_store_n(slot, value, __ATOMIC_RELEASE);
    }
    return value == ABSENT ? NULL : value;
}

/* ---- condition variables (resolved as one group) ---------------------------------------------- */

typedef VOID(WINAPI* CondFn)(PVOID);
typedef BOOL(WINAPI* CondSleepCsFn)(PVOID, PCRITICAL_SECTION, DWORD);

enum { COND_INITIALIZE, COND_WAKE, COND_WAKE_ALL, COND_SLEEP_CS, COND_COUNT };
static const char* const cond_names[COND_COUNT] = {
    "InitializeConditionVariable", "WakeConditionVariable", "WakeAllConditionVariable", "SleepConditionVariableCS"};
static void* cond_slots[COND_COUNT];
static LONG cond_group; /* 0: not decided; 1: all four present; 2: fallbacks */

/* The real export for `which` when all four are present, NULL when the
   fallbacks are in use. */
static void* cond_real(int which)
{
    LONG group = __atomic_load_n(&cond_group, __ATOMIC_ACQUIRE);
    if (!group) {
        group = 1;
        for (int i = 0; i < COND_COUNT; ++i) {
            if (!cached(&cond_slots[i], L"kernel32.dll", cond_names[i])) group = 2;
        }
        __atomic_store_n(&cond_group, group, __ATOMIC_RELEASE);
    }
    return group == 1 ? __atomic_load_n(&cond_slots[which], __ATOMIC_ACQUIRE) : NULL;
}

static CondFn cond_fn(int which)
{
    void* real = cond_real(which);
    CondFn fn = NULL;
    if (real) CopyMemory(&fn, &real, sizeof fn);
    return fn;
}

#ifdef DRUTA_RUNTIME_IMPORTS_TEST
long druta_runtime_imports_test_fallback_sleeps;
#endif

static VOID WINAPI shim_InitializeConditionVariable(PVOID condition)
{
    CondFn real = cond_fn(COND_INITIALIZE);
    if (real) {
        real(condition);
        return;
    }
    *(PVOID*)condition = NULL; /* CONDITION_VARIABLE is one pointer; zero is CONDITION_VARIABLE_INIT */
}

static VOID WINAPI shim_WakeConditionVariable(PVOID condition)
{
    CondFn real = cond_fn(COND_WAKE);
    if (real) real(condition);
    /* Fallback: nothing to wake, waiters poll (see SleepConditionVariableCS). */
}

static VOID WINAPI shim_WakeAllConditionVariable(PVOID condition)
{
    CondFn real = cond_fn(COND_WAKE_ALL);
    if (real) real(condition);
}

static BOOL WINAPI shim_SleepConditionVariableCS(PVOID condition, PCRITICAL_SECTION section, DWORD milliseconds)
{
    void* address = cond_real(COND_SLEEP_CS);
    if (address) {
        CondSleepCsFn real;
        CopyMemory(&real, &address, sizeof real);
        return real(condition, section, milliseconds);
    }
    /* A spurious wake-up: legal for any condition variable, and every correct
       waiter re-checks its predicate. Leaving the section lets the thread that
       will change the predicate (and would have signalled) take it. */
#ifdef DRUTA_RUNTIME_IMPORTS_TEST
    __atomic_add_fetch(&druta_runtime_imports_test_fallback_sleeps, 1, __ATOMIC_RELAXED);
#endif
    LeaveCriticalSection(section);
    Sleep(1);
    EnterCriticalSection(section);
    return TRUE;
}

/* ---- GetThreadId ------------------------------------------------------------------------------ */

typedef DWORD(WINAPI* GetThreadIdFn)(HANDLE);
typedef LONG(NTAPI* NtQueryInformationThreadFn)(HANDLE, int, PVOID, ULONG, PULONG);
typedef ULONG(NTAPI* RtlNtStatusToDosErrorFn)(LONG);

/* THREAD_BASIC_INFORMATION (ThreadBasicInformation = 0), declared here
   because the SDK headers do not: layout from the NT native API. */
struct DrutaThreadBasicInformation {
    LONG exit_status;
    PVOID teb_base_address;
    HANDLE unique_process;
    HANDLE unique_thread;
    ULONG_PTR affinity_mask;
    LONG priority;
    LONG base_priority;
};

static void* get_thread_id_slot;
static void* nt_query_information_thread_slot;
static void* rtl_nt_status_to_dos_error_slot;

static DWORD WINAPI shim_GetThreadId(HANDLE thread)
{
    void* real = cached(&get_thread_id_slot, L"kernel32.dll", "GetThreadId");
    if (real) {
        GetThreadIdFn fn;
        CopyMemory(&fn, &real, sizeof fn);
        return fn(thread);
    }
    /* From here on this is the fallback: like the real GetThreadId it leaves
       the last error alone on success and sets it on failure. */
    DWORD saved = GetLastError();
    void* query = cached(&nt_query_information_thread_slot, L"ntdll.dll", "NtQueryInformationThread");
    if (!query) {
        SetLastError(ERROR_CALL_NOT_IMPLEMENTED);
        return 0;
    }
    NtQueryInformationThreadFn query_fn;
    CopyMemory(&query_fn, &query, sizeof query_fn);
    struct DrutaThreadBasicInformation info;
    ZeroMemory(&info, sizeof info);
    LONG status = query_fn(thread, 0 /* ThreadBasicInformation */, &info, (ULONG)sizeof info, NULL);
    if (status < 0) { /* !NT_SUCCESS */
        void* convert = cached(&rtl_nt_status_to_dos_error_slot, L"ntdll.dll", "RtlNtStatusToDosError");
        DWORD error = ERROR_INVALID_HANDLE;
        if (convert) {
            RtlNtStatusToDosErrorFn convert_fn;
            CopyMemory(&convert_fn, &convert, sizeof convert_fn);
            error = convert_fn(status);
        }
        SetLastError(error);
        return 0;
    }
    SetLastError(saved); /* a native call does not set it, but say so rather than rely on it */
    return (DWORD)(ULONG_PTR)info.unique_thread;
}

/* ---- the import pointers the libgcc objects reference ---------------------------------------- */

/* The names are spelled out with asm labels: on x86 a C name gets one leading
   underscore and stdcall functions carry @<argument bytes>, so the import
   pointer for GetThreadId is "__imp__GetThreadId@4"; on x86_64 there is
   neither. They are writable, like the import address table they replace. */
#if defined(__i386__)
#define DRUTA_IMP(name, bytes) __asm__("__imp__" #name "@" #bytes)
#elif defined(__x86_64__)
#define DRUTA_IMP(name, bytes) __asm__("__imp_" #name)
#else
#error "runtime_imports.c: only x86 and x86_64 MinGW builds are understood"
#endif

extern VOID(WINAPI* druta_imp_InitializeConditionVariable)(PVOID) DRUTA_IMP(InitializeConditionVariable, 4);
extern VOID(WINAPI* druta_imp_WakeConditionVariable)(PVOID) DRUTA_IMP(WakeConditionVariable, 4);
extern VOID(WINAPI* druta_imp_WakeAllConditionVariable)(PVOID) DRUTA_IMP(WakeAllConditionVariable, 4);
extern BOOL(WINAPI* druta_imp_SleepConditionVariableCS)(PVOID, PCRITICAL_SECTION, DWORD)
    DRUTA_IMP(SleepConditionVariableCS, 12);
extern DWORD(WINAPI* druta_imp_GetThreadId)(HANDLE) DRUTA_IMP(GetThreadId, 4);

VOID(WINAPI* druta_imp_InitializeConditionVariable)(PVOID) = shim_InitializeConditionVariable;
VOID(WINAPI* druta_imp_WakeConditionVariable)(PVOID) = shim_WakeConditionVariable;
VOID(WINAPI* druta_imp_WakeAllConditionVariable)(PVOID) = shim_WakeAllConditionVariable;
BOOL(WINAPI* druta_imp_SleepConditionVariableCS)(PVOID, PCRITICAL_SECTION, DWORD) = shim_SleepConditionVariableCS;
DWORD(WINAPI* druta_imp_GetThreadId)(HANDLE) = shim_GetThreadId;

#endif /* _WIN32 && __GNUC__ */
