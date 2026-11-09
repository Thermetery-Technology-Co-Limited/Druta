// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The PERF diagnostics reader (port of perf_diagnostics.PerfDiagnosticsReader): increment P1 adds the
// pure group layouts, the A612-only transport check and the read-only GET; P2 the group decoder and the
// bounded read loop. One sticky reader per handle, with its own target binding as the Python reader.
//
// Contracts are traced in experiments/registry-overrides/driver-controls. The architecture gate is
// probe_architecture (target.h, Python's PROBE_ARCHITECTURES); packet acceptance, the exact echo of
// the request geometry and RM's status remain independent results for each group on the current
// adapter. No board, VBIOS or driver identity is consulted.
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <vector>

#include "backend/gpu.h"
#include "backend/hook.h"
#include "backend/rm_error.h"
#include "backend/rm_transport.h"
#include "backend/target.h"
#include "backend/telemetry.h"

namespace druta::backend {

// ---- perf_layouts: the pure tables (perf_diagnostics.GroupLayout / LAYOUTS / _GET_COMMANDS) ----

struct PerfGroupLayout {
    const char* name;  // the LAYOUTS key
    std::uint32_t info_id, control_id;            // NvAPI getter interface ids
    std::uint32_t info_command, control_command;  // RM GET commands
    std::uint32_t info_size, control_size;        // parameter bytes of each GET
    std::uint32_t capacity, mask_bytes;
    std::uint32_t info_offset, info_stride, control_offset, control_stride;
};

// LAYOUTS in Python's dict order: limits, cf_controller, cf_policy.
const std::array<PerfGroupLayout, 3>& perf_layouts();
// (command, size) in _GET_COMMANDS: the INFO or CONTROL GET of one group with its exact size.
bool perf_get_allowed(std::uint32_t command, std::uint32_t size);
// The group whose INFO or CONTROL command this is (Python's next(...) over LAYOUTS), or nullptr.
const PerfGroupLayout* perf_layout_for_command(std::uint32_t command);

inline constexpr std::uint32_t kPerfTransportCommand = 0x2080A612;  // the modern power GET family

// ---- the reader -------------------------------------------------------------------------------

struct PerfTransportResult {
    std::optional<CapturedTransport> transport;  // (header, fields) on success
    RmError error;  // PerfDiagnosticError(kind, message): transient or unsupported
};

// _get's mask argument as Python sees it.
enum class PerfMask { none, bytes, other };  // None, a bytes object, any other value

// _get's transport argument once the binding has marshalled it as ctypes does (`header, fields =
// transport`, each header word stored modulo 2^32 into the packet in order): `header_count` words,
// copied from packet word 0 (a word past 16 lands in the payload), and the escape fields.
struct PerfRequest {
    const std::uint32_t* header = nullptr;
    std::uint32_t header_count = 0;
    EscapeFields fields;
};

struct PerfGetResult {
    std::optional<std::vector<std::uint8_t>> payload;  // the `size` bytes after offset 68
    // Otherwise the exception. kind invalid is a plain ValueError raised before the driver was touched
    // (the allow-set and mask rules); the others are PerfDiagnosticError kinds. Python's one `status`
    // attribute is ntstatus for the transport failure (unset when no escape was issued, Python's None)
    // and rm_status for an RM rejection.
    RmError error;
    // The transport could not be used where Python builds the packet, after the rules and the first
    // binding: None (Python's unpacking TypeError) or a header longer than the packet (ctypes'
    // IndexError). No escape was issued.
    bool bad_transport = false;
};

class PerfDiagnostics {
public:
    PerfDiagnostics(Gpu& gpu, Telemetry& telemetry, RmTransport& rm)
        : gpu_(gpu), telemetry_(telemetry), rm_(rm), binding_(gpu, telemetry) {}

    // PerfDiagnosticsReader._transport. The caller holds Gpu::lock().
    PerfTransportResult transport();
    // PerfDiagnosticsReader._binding alone, on the reader's sticky identity. The caller holds Gpu::lock().
    TargetResult bind() { return binding_.bind(); }
    // PerfDiagnosticsReader._get(command, size, transport, mask). The caller holds Gpu::lock().
    // `transport` is the caller's marshalled (header, fields); nullptr is Python's None. Either is
    // refused (bad_transport) where Python builds the packet, after the rules and the first binding.
    // A second binding of an unchanged handle is idempotent: the sticky identity matches and the
    // recognised architecture is cached, so it makes no driver call.
    PerfGetResult get(std::uint32_t command, std::uint32_t size, const PerfRequest* transport, PerfMask mask_kind,
                      const std::uint8_t* mask, std::uint32_t mask_size);
    // A fresh reader: forget the sticky identity.
    void reset() { binding_.reset(); }

private:
    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport& rm_;
    TargetBinding binding_;  // the reader's own sticky identity
};

}  // namespace druta::backend
