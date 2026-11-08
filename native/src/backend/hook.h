// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Layer 1 of the RM escape transport: the process-wide D3DKMTEscape hook and
// the two captures that borrow a getter's live escape header. Port of
// nvbackend.GPU._capture_current_limit_transport and
// _capture_legacy_clk_transport (DESIGN-TRANSPORT.md 2.3, 2.4, 3.4).
//
// The hook rewrites the first bytes of gdi32's D3DKMTEscape with a jump to
// hook_entry for the duration of ONE getter call, under a process-wide mutex.
// There is no trampoline: the thunk restores the original bytes, inspects the
// escape on the installing thread, forwards to the restored function and,
// for the power capture, re-arms while nothing has been found. A served fake
// D3DKMTEscape (test resolver) is a libffi trampoline that must never be
// overwritten, so a buffer patcher stands in for VirtualProtect there and on
// non-Windows builds; the thunk body is then reached through hook_entry
// directly (dc_hook_test_entry).
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "backend/gdi.h"
#include "backend/gpu.h"
#include "backend/telemetry.h"
#include "core/calling.h"
#include "core/rail_transport.h"

namespace druta::backend {

// Thunk size: 14 on x64 (jmp qword ptr [rip+0]; imm64), 5 on x86 (jmp rel32).
inline constexpr std::size_t kPatchBytes = sizeof(void*) == 8 ? 14 : 5;

// The thunk bytes for a pointer width (pure; both widths are checked from any host).
struct Thunk {
    std::uint8_t bytes[14] = {};
    std::size_t size = 0;
};
Thunk thunk_bytes(std::uint64_t address, std::uint64_t target, unsigned pointer_bytes);

// One address, n bytes: the VirtualProtect dance of the Python closures.
class CodePatcher {
public:
    virtual ~CodePatcher() = default;
    // Save n bytes at address and make them writable. false is Python's VirtualProtect failure.
    virtual bool begin(void* address, const std::uint8_t* patch, std::size_t n) = 0;
    virtual void write_patch() = 0;     // the thunk, then FlushInstructionCache
    virtual void write_original() = 0;  // the saved bytes, then FlushInstructionCache
    virtual void end() = 0;             // write_original(); the old protection is restored
    virtual bool patched() const = 0;
};

// What a capture borrows: the 17-word packet header and the escape fields.
struct CapturedTransport {
    std::array<std::uint32_t, 17> header{};
    std::uint32_t header_count = 17;  // a fixture may store another length; validation rejects it
    EscapeFields fields;
};

// _current_limit_observed_transport: the last self-consistent power GET the thunk saw.
struct Observed {
    bool seen = false;
    std::uint32_t command_id = 0, packet_bytes = 0, parameter_bytes = 0;
};

struct CaptureResult {
    std::optional<CapturedTransport> transport;  // Python's return value (None = nullopt)
    Observed observed;                           // what this attempt saw (power capture only)
    bool hooked = false;                         // a hook was installed and the getter ran
    bool no_escape_path = false;                 // stopped because gdi32 lacks the escape path (D5)
};

// The captures. Each takes the process-wide hook mutex; the caller holds Gpu::lock().
// Preconditions (checked before any hook): nvapi.ok, the getter interface, and for the
// power capture a non-empty generation-policy table for telemetry.arch().
CaptureResult capture_power_transport(Gpu& gpu, Telemetry& telemetry);
CaptureResult capture_legacy_clk_transport(Gpu& gpu);

// The rail splice: nvbackend.GPU._write_rail_records_locked after its Python preconditions
// (2.5). One VoltRailsCtlGet under the hook; the recognized GET is read back and exactly one
// SET with the prepared records is issued in its place. Never more than one SET.
struct RailWriteResult {
    bool done = false;
    std::optional<std::uint32_t> status;  // RM status of the SET (NTSTATUS & 0xFFFFFFFF when it failed)
    std::string error;                    // _volt_rail_write_error; "" is Python's None
    struct Protocol {
        std::uint32_t get_command = 0, set_command = 0, packet_size = 0, getter_version = 0;
    };
    std::optional<Protocol> protocol;      // _volt_rail_write_protocol
    std::vector<std::string> observed;     // _volt_rail_observed_packets (at most 8 summaries)
};
RailWriteResult rail_write_locked(Gpu& gpu, std::uint32_t control_version, std::uint32_t rail_mask,
                                  const rail::Record* records, std::size_t record_count, std::uint32_t boost);

// The thunk body: what the patched D3DKMTEscape jumps to. Public for the test seam.
long DRUTA_STDCALL hook_entry(void* escape);

// Test seams (DESIGN-TRANSPORT.md 3.7).
struct HookState {
    bool installed = false;  // a capture is between install and uninstall
    bool patched = false;    // the thunk is currently written
    std::uint32_t calls = 0;  // hook_entry entries since the install
};
HookState hook_state();
void hook_test_fail_protect(bool fail);  // the buffer patcher's begin() fails

}  // namespace druta::backend
