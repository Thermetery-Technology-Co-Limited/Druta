// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/hook.h"

#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "backend/exports.h"
#include "backend/power_layouts.h"
#include "backend/structs.h"
#include "core/mutex.h"
#include "win/d3dkmt.h"

#if defined(_WIN32)
#include <windows.h>
#else
#include <pthread.h>
#endif

namespace druta::backend {
namespace {

using win::D3DKMT_ESCAPE;
using win::KmtFn;

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);

// STATUS_INVALID_PARAMETER, as a signed NTSTATUS.
const long kStatusInvalidParameter = static_cast<long>(static_cast<std::int32_t>(0xC000000Du));

std::uintptr_t current_thread_id() {
#if defined(_WIN32)
    return static_cast<std::uintptr_t>(::GetCurrentThreadId());
#else
    return static_cast<std::uintptr_t>(::pthread_self());
#endif
}

// ---- patchers --------------------------------------------------------------------------------------

// Tests and non-Windows builds: nothing is written anywhere; the state is the whole effect.
class BufferPatcher final : public CodePatcher {
public:
    bool begin(void*, const std::uint8_t*, std::size_t) override {
        if (fail_begin.load()) return false;
        patched_.store(false);
        active_.store(true);
        return true;
    }
    void write_patch() override {
        if (active_.load()) patched_.store(true);
    }
    void write_original() override {
        if (active_.load()) patched_.store(false);
    }
    void end() override {
        write_original();
        active_.store(false);
    }
    bool patched() const override { return patched_.load(); }

    std::atomic<bool> fail_begin{false};

private:
    std::atomic<bool> active_{false}, patched_{false};
};

#if defined(_WIN32)
// The real one: VirtualProtect(0x40) for the hooked window, memcpy, FlushInstructionCache.
class VirtualProtectPatcher final : public CodePatcher {
public:
    bool begin(void* address, const std::uint8_t* patch, std::size_t n) override {
        address_ = static_cast<std::uint8_t*>(address);
        n_ = n;
        std::memcpy(original_, address, n);
        std::memcpy(patch_, patch, n);
        DWORD old = 0;
        if (!::VirtualProtect(address, n, PAGE_EXECUTE_READWRITE, &old)) return false;
        old_ = old;
        patched_.store(false);
        active_.store(true);
        return true;
    }
    void write_patch() override {
        if (!active_.load()) return;
        std::memcpy(address_, patch_, n_);
        flush();
        patched_.store(true);
    }
    void write_original() override {
        if (!active_.load()) return;
        std::memcpy(address_, original_, n_);
        flush();
        patched_.store(false);
    }
    void end() override {
        write_original();
        active_.store(false);  // a late foreign re-arm is a no-op once the page is protected again
        DWORD previous = 0;
        ::VirtualProtect(address_, n_, old_, &previous);
    }
    bool patched() const override { return patched_.load(); }

private:
    void flush() { ::FlushInstructionCache(::GetCurrentProcess(), address_, n_); }
    std::uint8_t* address_ = nullptr;
    std::size_t n_ = 0;
    DWORD old_ = 0;
    std::uint8_t original_[14] = {}, patch_[14] = {};
    std::atomic<bool> active_{false}, patched_{false};
};
#endif

// ---- the session -----------------------------------------------------------------------------------

enum class HookPolicy : int { power_capture = 0, legacy_clk_capture = 1, rail_splice = 2 };

// _volt_rail_write_error, as a code: any thread may set it (last writer wins, as the Python
// attribute), and only the owner turns it into text after the getter returns.
enum RailError : int { rail_ok = 0, rail_foreign = 1, rail_validation = 2, rail_preserve = 3 };

// Namespace-scope statics with constant initialization: no guarded local statics on the MinGW
// win32 thread model, and the patch is process-wide anyway.
struct HookSession {
    std::atomic<bool> active{false};
    std::atomic<KmtFn> original{nullptr};
    std::atomic<std::uintptr_t> owner{0};
    std::atomic<int> policy{0};
    std::atomic<CodePatcher*> patcher{nullptr};
    std::atomic<std::uint32_t> calls{0};
    // Owner-thread state. found[] saturates at 2: only "exactly one" matters.
    std::atomic<unsigned> found_count{0};
    CapturedTransport found[2];
    Observed observed;

    // The rail splice (2.5). A foreign thread touches only the two atomics.
    std::atomic<bool> aborted{false};
    std::atomic<int> error_code{rail_ok};
    bool done = false;
    std::optional<std::uint32_t> status;
    std::string error_detail;  // the %s of the owner-thread errors
    std::optional<RailWriteResult::Protocol> protocol;
    std::string observed_packets[8];
    unsigned observed_count = 0;
    std::uint32_t rail_mask = 0, boost = 0, control_version = 0;
    rail::Record records[2] = {};
    std::size_t record_count = 0;
    std::vector<std::uint8_t> payload, returned, current, prepared;  // owner-thread scratch
};

HookSession g_session;
BufferPatcher g_buffer_patcher;
#if defined(_WIN32)
VirtualProtectPatcher g_protect_patcher;
#endif

Mutex& hook_mutex() {  // GPU._RAIL_HOOK_LOCK
    static Mutex mutex;
    return mutex;
}

CodePatcher& select_patcher() {
#if defined(_WIN32)
    if (!test_resolver_installed()) return g_protect_patcher;
#endif
    return g_buffer_patcher;
}

void append_found(HookSession& s, const std::uint32_t* words, const D3DKMT_ESCAPE& e) {
    unsigned n = s.found_count.load();
    if (n >= 2) return;
    CapturedTransport& t = s.found[n];
    std::memcpy(t.header.data(), words, 17 * sizeof(std::uint32_t));
    t.fields = EscapeFields{e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext};
    s.found_count.store(n + 1);
}

// 2.4, power capture: any self-consistent power GET is observed (the last one wins); one whose
// packet is exactly the 68-byte prologue plus its parameters is found.
void inspect_power(HookSession& s, const D3DKMT_ESCAPE& e) {
    if (!e.pPrivateDriverData || e.PrivateDriverDataSize < 68) return;
    std::uint32_t w[17];
    std::memcpy(w, e.pPrivateDriverData, sizeof w);
    if (w[2] != e.PrivateDriverDataSize || !(w[14] == 0x2080A612u || w[14] == 0x20802612u)) return;
    s.observed = Observed{true, w[14], w[2], w[15]};
    if (static_cast<std::uint64_t>(w[2]) == 68u + static_cast<std::uint64_t>(w[15])) append_found(s, w, e);
}

// 2.4, legacy capture: the 716-byte VoltRailsCtlGet escape with one record selected.
void inspect_legacy(HookSession& s, const D3DKMT_ESCAPE& e) {
    if (!e.pPrivateDriverData || e.PrivateDriverDataSize != 716) return;
    std::uint32_t w[18];
    std::memcpy(w, e.pPrivateDriverData, sizeof w);
    if (w[2] == 716 && w[14] == 0x20803213u && w[15] == 648 && w[17] == 1) append_found(s, w, e);
}

// ---- the rail splice (2.5) --------------------------------------------------------------------------

const char* rail_error_text(rail::Error error) {
    switch (error) {
        case rail::Error::mask: return "requested_mask must select rail 0, rail 1, or both";
        case rail::Error::records: return "requested_mask must exactly match the record rails";
        case rail::Error::boost: return "boost must be an integer from 0 through 100";
        case rail::Error::unrecognized: return "payload is not an exact known rail GET packet";
        case rail::Error::none: break;
    }
    return "";
}

struct Matched {
    void* data;
    std::uint32_t size;
    std::size_t layout;
};

// Steps 4.1-4.7: read the GET back, prepare the SET over it, issue it. "" on success, else the
// text Python's exception carries. rc is the GET's status in, the SET's status out.
std::string splice(HookSession& s, void* escape, const Matched& m, long& rc) {
    const D3DKMT_ESCAPE& e = *static_cast<const D3DKMT_ESCAPE*>(escape);
    if (e.pPrivateDriverData != m.data || e.PrivateDriverDataSize != m.size)
        return "native GET changed the packet buffer or size";
    try {
        const auto* bytes = static_cast<const std::uint8_t*>(m.data);
        s.returned.assign(bytes, bytes + m.size);
        const std::uint32_t rm_status = ByteView(s.returned).u32(rail::kStatusWord).value_or(0);
        if (rc != 0 || rm_status != 0) {
            char text[96];
            std::snprintf(text, sizeof text, "native GET failed (NTSTATUS 0x%08X, RM 0x%08X)",
                          static_cast<unsigned>(static_cast<std::uint64_t>(static_cast<std::int64_t>(rc)) & 0xFFFFFFFFu),
                          static_cast<unsigned>(rm_status));
            return text;
        }
        if (rail::recognize_packet(ByteView(s.returned), s.rail_mask) != std::optional<std::size_t>(m.layout))
            return "native GET response does not match its request layout and mask";
        // The input GET contains empty records. Read them first so a full-record SET preserves
        // fields whose meaning is unknown. Keep the original request header: GET output includes
        // a valid mask that the native SET constructor leaves zero.
        const rail::Layout& layout = rail::known_layouts()[m.layout];
        std::size_t boundary = static_cast<std::size_t>(layout.record0) * 4;
        if (boundary > s.payload.size()) boundary = s.payload.size();
        s.current.assign(s.payload.begin(), s.payload.begin() + static_cast<std::ptrdiff_t>(boundary));
        s.current.insert(s.current.end(), s.returned.begin() + static_cast<std::ptrdiff_t>(boundary), s.returned.end());
        rail::Error error =
            rail::prepare_write(ByteView(s.current), s.rail_mask, s.records, s.record_count, s.boost, s.prepared);
        if (error != rail::Error::none) return rail_error_text(error);
        std::memcpy(m.data, s.prepared.data(), s.prepared.size() < m.size ? s.prepared.size() : m.size);
        s.done = true;
        s.status = 0xFFFFFFFFu;  // not success until SET returns
        s.protocol = RailWriteResult::Protocol{layout.get_command, layout.set_command, layout.packet_size,
                                               s.control_version};
        rc = s.original.load()(escape);
        if (rc == 0)
            s.status = ByteView(bytes, m.size).u32(rail::kStatusWord).value_or(0);
        else
            s.status = static_cast<std::uint32_t>(static_cast<std::uint64_t>(static_cast<std::int64_t>(rc)) & 0xFFFFFFFFu);
        return "";
    } catch (...) {
        return "out of memory";
    }
}

long rail_splice_entry(HookSession& s, void* escape) {
    s.calls.fetch_add(1);
    CodePatcher* patcher = s.patcher.load();
    const bool owned = current_thread_id() == s.owner.load();
    if (!owned) {
        s.aborted.store(true);
        s.error_code.store(rail_foreign);
    }
    std::optional<Matched> matched;
    if (escape && owned && !s.done && !s.aborted.load()) {
        const D3DKMT_ESCAPE& e = *static_cast<const D3DKMT_ESCAPE*>(escape);
        if (e.pPrivateDriverData && 68 <= e.PrivateDriverDataSize && e.PrivateDriverDataSize <= 8192) {
            try {
                const auto* bytes = static_cast<const std::uint8_t*>(e.pPrivateDriverData);
                s.payload.assign(bytes, bytes + e.PrivateDriverDataSize);
                const ByteView view(s.payload);
                if (s.observed_count < 8) s.observed_packets[s.observed_count++] = rail::packet_summary(view);
                if (auto layout = rail::recognize_packet(view, s.rail_mask))
                    matched = Matched{e.pPrivateDriverData, e.PrivateDriverDataSize, *layout};
            } catch (...) {
                s.aborted.store(true);
                s.error_code.store(rail_validation);
                s.error_detail = "out of memory";
            }
        }
    }
    patcher->write_original();  // real bytes back before calling
    long rc = s.original.load()(escape);
    if (matched) {
        std::string why = splice(s, escape, *matched, rc);
        if (!why.empty()) {
            s.aborted.store(true);
            s.error_code.store(rail_preserve);
            s.error_detail = why;
        }
    } else if (owned && s.active.load() && !s.aborted.load() && !s.done && s.calls.load() < 16) {
        patcher->write_patch();
    }
    return rc;
}

// Install, run the getter, uninstall (2.3). The caller holds Gpu::lock(); this takes the hook
// mutex. Nothing here throws: the thunk is C-callable and the getter is a C function.
template <typename Getter>
CaptureResult run_capture(HookPolicy policy, Getter&& getter) {
    CaptureResult r;
    LockGuard guard(hook_mutex());
    void* address = escape_export();
    if (!address) {  // _legacy_clk_gdi() is None: no hook
        r.no_escape_path = true;
        return r;
    }
    CodePatcher& patcher = select_patcher();
    Thunk thunk = thunk_bytes(static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(address)),
                              static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(&hook_entry)),
                              static_cast<unsigned>(sizeof(void*)));
    if (!patcher.begin(address, thunk.bytes, thunk.size)) return r;  // VirtualProtect failed
    HookSession& s = g_session;
    s.original.store(reinterpret_cast<KmtFn>(address));
    s.owner.store(current_thread_id());
    s.policy.store(static_cast<int>(policy));
    s.patcher.store(&patcher);
    s.calls.store(0);
    s.found_count.store(0);
    s.observed = Observed{};
    s.active.store(true, std::memory_order_release);
    patcher.write_patch();
    const int status = getter();
    // Python's finally: restore, then the old protection. A foreign thread that arrives after
    // this point only forwards (hook_entry's !active branch).
    s.active.store(false, std::memory_order_release);
    patcher.end();
    r.hooked = true;
    r.observed = s.observed;
    if (status == 0 && s.found_count.load() == 1) r.transport = s.found[0];
    return r;
}

}  // namespace

Thunk thunk_bytes(std::uint64_t address, std::uint64_t target, unsigned pointer_bytes) {
    Thunk t;
    if (pointer_bytes == 8) {
        // FF 25 00 00 00 00  jmp qword ptr [rip+0]; then the absolute target (nvbackend 4859-4861)
        t.size = 14;
        t.bytes[0] = 0xFF;
        t.bytes[1] = 0x25;
        for (unsigned i = 0; i < 8; ++i) t.bytes[6 + i] = static_cast<std::uint8_t>(target >> (8 * i));
    } else {
        // E9 rel32: relative to the end of the 5-byte instruction, modulo 2^32
        t.size = 5;
        const std::uint32_t rel = static_cast<std::uint32_t>((target - (address + 5)) & 0xFFFFFFFFu);
        t.bytes[0] = 0xE9;
        for (unsigned i = 0; i < 4; ++i) t.bytes[1 + i] = static_cast<std::uint8_t>(rel >> (8 * i));
    }
    return t;
}

long DRUTA_STDCALL hook_entry(void* escape) {
    HookSession& s = g_session;
    if (!s.active.load(std::memory_order_acquire)) {
        // A foreign thread arriving after uninstall: forward only. Python would call a freed
        // closure here (3.4, deliberate deviation).
        KmtFn original = s.original.load();
        return original ? original(escape) : kStatusInvalidParameter;
    }
    const HookPolicy policy = static_cast<HookPolicy>(s.policy.load());
    if (policy == HookPolicy::rail_splice) return rail_splice_entry(s, escape);
    s.calls.fetch_add(1);
    CodePatcher* patcher = s.patcher.load();
    const bool owned = current_thread_id() == s.owner.load();
    patcher->write_original();  // restore first, on every thread
    if (escape && owned) {
        const D3DKMT_ESCAPE& e = *static_cast<const D3DKMT_ESCAPE*>(escape);
        if (policy == HookPolicy::power_capture) inspect_power(s, e);
        else inspect_legacy(s, e);
    }
    const long status = s.original.load()(escape);
    // PowerPolInfo can query the architecture before its power getter: stay armed, on any
    // thread, until the header is found. The legacy capture never re-arms.
    if (policy == HookPolicy::power_capture && s.found_count.load() == 0) patcher->write_patch();
    return status;
}

CaptureResult capture_power_transport(Gpu& gpu, Telemetry& telemetry) {
    // _current_limit_profile_supported(): nvapi.ok and a generation-policy table for arch(),
    // in that order (arch() is a driver read); then the PowerPolInfo interface.
    if (!gpu.nvapi.ok) return {};
    if (generation_policies(telemetry.arch()).empty()) return {};
    auto fn = gpu.nvapi.get<HandlePtrFn>(nvapi_id::PowerPolInfo);
    if (!fn) return {};
    PwrPolInfo block{};
    block.version = nvapi_ver<PwrPolInfo>(1);
    return run_capture(HookPolicy::power_capture, [&] { return fn(gpu.nvapi.gpu, &block); });
}

CaptureResult capture_legacy_clk_transport(Gpu& gpu) {
    if (!gpu.nvapi.ok) return {};
    auto fn = gpu.nvapi.get<HandlePtrFn>(nvapi_id::VoltRailsCtlGet);
    if (!fn) return {};
    std::uint32_t block[0xAC8 / 4] = {0x10AC8, 1};
    return run_capture(HookPolicy::legacy_clk_capture, [&] { return fn(gpu.nvapi.gpu, block); });
}

RailWriteResult rail_write_locked(Gpu& gpu, std::uint32_t control_version, std::uint32_t rail_mask,
                                  const rail::Record* records, std::size_t record_count, std::uint32_t boost) {
    RailWriteResult out;
    LockGuard guard(hook_mutex());
    // Python takes D3DKMTEscape directly, without _legacy_clk_gdi() (open question 3): only the
    // escape export is required. Python would raise AttributeError without it; the native
    // writer reports the capability detail instead.
    void* address = escape_export(false);
    if (!address) {
        out.error = escape_capability().detail;
        return out;
    }
    auto fn = gpu.nvapi.get<HandlePtrFn>(nvapi_id::VoltRailsCtlGet);
    if (!fn) {
        out.error = "VoltRailsCtlGet did not resolve on this driver. Nothing was written.";
        return out;
    }
    HookSession& s = g_session;
    s.aborted.store(false);
    s.error_code.store(rail_ok);
    s.done = false;
    s.status.reset();
    s.error_detail.clear();
    s.protocol.reset();
    s.observed_count = 0;
    s.rail_mask = rail_mask;
    s.boost = boost;
    s.control_version = control_version;
    s.record_count = record_count < 2 ? record_count : 2;
    for (std::size_t i = 0; i < s.record_count; ++i) s.records[i] = records[i];
    try {
        for (std::vector<std::uint8_t>* scratch : {&s.payload, &s.returned, &s.current, &s.prepared}) scratch->reserve(8192);
    } catch (...) {
        out.error = "Could not inspect the driver rail request. Nothing was written.";
        return out;
    }
    CodePatcher& patcher = select_patcher();
    Thunk thunk = thunk_bytes(static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(address)),
                              static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(&hook_entry)),
                              static_cast<unsigned>(sizeof(void*)));
    if (!patcher.begin(address, thunk.bytes, thunk.size)) {
        out.error = "Could not inspect the driver rail request. Nothing was written.";
        return out;
    }
    s.original.store(reinterpret_cast<KmtFn>(address));
    s.owner.store(current_thread_id());
    s.policy.store(static_cast<int>(HookPolicy::rail_splice));
    s.patcher.store(&patcher);
    s.calls.store(0);
    s.active.store(true, std::memory_order_release);
    patcher.write_patch();
    std::uint32_t block[8192 / 4] = {};
    block[0] = control_version;
    block[1] = rail_mask;
    const int getter_status = fn(gpu.nvapi.gpu, block);
    s.active.store(false, std::memory_order_release);
    patcher.end();
    out.done = s.done;
    out.status = s.status;
    out.protocol = s.protocol;
    for (unsigned i = 0; i < s.observed_count; ++i) out.observed.push_back(s.observed_packets[i]);
    switch (s.error_code.load()) {
        case rail_foreign: out.error = "Another driver thread interrupted rail detection. Retry the write."; break;
        case rail_validation: out.error = "Rail packet validation failed: " + s.error_detail; break;
        case rail_preserve: out.error = "Could not preserve the current native rail record: " + s.error_detail; break;
        default: break;
    }
    if (!s.done && out.error.empty())
        out.error = "The driver did not emit a recognized rail-control packet. Nothing was written. Getter status: " +
                    std::to_string(getter_status) + ". Copy rail diagnostics for this driver.";
    return out;
}

HookState hook_state() {
    HookState state;
    state.installed = g_session.active.load();
    CodePatcher* patcher = g_session.patcher.load();
    state.patched = patcher ? patcher->patched() : false;
    state.calls = g_session.calls.load();
    return state;
}

void hook_test_fail_protect(bool fail) { g_buffer_patcher.fail_begin.store(fail); }

}  // namespace druta::backend
