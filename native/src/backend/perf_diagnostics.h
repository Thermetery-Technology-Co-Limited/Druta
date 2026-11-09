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
#include <string>
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

// ---- decode_group: the pure decoder (perf_diagnostics.decode_group) ----------------------------

// The LAYOUTS entry named `name` (exact bytes), or nullptr.
const PerfGroupLayout* perf_layout_named(const std::string& name);
// _layout's ValueError text: "Unknown PERF diagnostic group " + repr(name), with Python's str repr for
// an ASCII name (quote choice, \\ \' \t \n \r and \xNN escapes). Bytes past 0x7F are copied as
// they are; Python keeps a printable non-ASCII character too but escapes a non-printable one, so the
// binding formats non-ASCII names itself.
std::string perf_unknown_group_message(const std::string& name);

inline constexpr std::uint32_t kPerfMaxEntries = 512;         // the largest capacity (limits)
inline constexpr std::uint32_t kPerfMaxInfoStride = 0x350;    // the largest INFO stride (cf_controller)
inline constexpr std::uint32_t kPerfMaxControlStride = 0x2C4; // the largest CONTROL stride (cf_controller)
inline constexpr std::uint32_t kPerfMaxMaskBytes = 64;

struct PerfEntry {  // one row of "entries"
    std::uint32_t index = 0;
    std::uint8_t type = 0, control_type = 0;
    std::optional<std::string> driver_label;  // None unless a limits type-6 label decoded
    std::vector<std::uint8_t> info_raw, control_raw;  // the row's bytes (info_hex / control_hex)
    std::vector<const char*> issues;  // static texts, in Python's order (at most one is ever appended)
    // cf_policy type-0 extras: role_bytes_raw, role_id and controller_mask are set together.
    bool policy = false;
    std::uint8_t role_bytes_raw[2] = {0, 0};
    std::uint8_t role_id = 0;
    std::uint32_t controller_mask = 0;
    std::optional<std::uint8_t> registry_controlled_flag_raw;  // only when Boolean
};

struct PerfFlag {  // one "flags" item: label -> {"raw", "boolean"}
    const char* label;
    std::uint8_t raw;
    std::optional<bool> boolean;  // None when raw is not 0 or 1
};

struct PerfTarget {  // one "registry_targets" item
    const char* field;  // bit0, bits2_1, bit3
    std::optional<std::uint32_t> index;  // None for 0xFFFF
    bool present = false;
    std::optional<std::uint8_t> flag_raw;
    const char* issue = nullptr;  // None
};

struct PerfSnapshot {  // decode_group's dict without slot, architecture and captured_at
    const PerfGroupLayout* layout = nullptr;  // group, capacity, commands and sizes
    std::vector<std::uint8_t> mask;           // mask_hex
    std::vector<PerfEntry> entries;           // present_count = entries.size()
    std::vector<PerfFlag> flags;              // in Python's dict order
    std::vector<PerfTarget> targets;
    std::vector<std::string> issues;
    std::string info_sha256;
    std::optional<std::string> control_sha256;
};

struct PerfDecodeResult {
    std::optional<PerfSnapshot> snapshot;
    // Otherwise the exception: kind invalid is _layout's ValueError, malformed a PerfDiagnosticError.
    RmError error;
};

// decode_group(name, info, control): control == nullptr is Python's None. Pure: no driver call.
PerfDecodeResult perf_decode_group(const std::string& name, const std::uint8_t* info, std::size_t info_size,
                                   const std::uint8_t* control, std::size_t control_size);

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

    // One group of PerfDiagnosticsReader.read: the bounded attempts loop for `layout`, attempts 1..3
    // (the caller validates). Each attempt holds Gpu::lock() itself, as Python holds gpu._lock per
    // attempt, so the lock is released between attempts.
    struct GroupRead {
        std::optional<PerfSnapshot> snapshot;  // ready / empty; otherwise `error` (a kinded failure)
        RmError error;
        std::uint32_t attempts = 0;            // Python's _attempt after the loop
        std::optional<Target> target;          // the last successful binding: report slot / architecture
    };
    GroupRead read_group(const PerfGroupLayout& layout, std::uint32_t attempts);

private:
    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport& rm_;
    TargetBinding binding_;  // the reader's own sticky identity
};

}  // namespace druta::backend
