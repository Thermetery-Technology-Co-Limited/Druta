// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The clock-BIOS reader (port of clock_bios_runtime's read path): increment C1 adds the raw guarded
// user-clock CONTROL GET (_control_once and the pure _control_shape); C2 the INFO GET through the paired
// A612 transport (_info_once), the pure _decode and the two-sample read(). The reader is a
// PerfDiagnostics (Python's PerfDiagnosticsReader): its bind() is _binding's sticky identity and its
// transport() is reader._transport(). The handle keeps one such reader for the standalone GETs
// (control_once, info_once; reset() is a fresh one); read() constructs its own, as each Python read()
// constructs a new PerfDiagnosticsReader shared by both of its attempts.
//
// The INFO layout (command 0x20809019 through NvAPI export 0x57B5A5DF's paired escape, 0x3030
// parameter bytes, a 0x30-byte header and 32 rows of 0x180) is the traced v50 BIOS builder's.
//
// Only GET calls occur here. The CONTROL layout (version 0x261A4, 24996 parameter bytes, a 0x124-byte
// header and 32 rows of 0x304) is the understood NVAPI wrapper; the request inventory is the one the
// current adapter accepted bit by bit (ClkDom::domains), and the driver must echo both the version and
// that mask. No board, VBIOS or driver identity is consulted.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "backend/clkdom.h"
#include "backend/gpu.h"
#include "backend/perf_diagnostics.h"
#include "backend/rm_error.h"
#include "backend/rm_transport.h"
#include "backend/target.h"
#include "backend/telemetry.h"

namespace druta::backend {

// _CONTROL_LAYOUT.
inline constexpr std::uint32_t kClockControlVersion = 0x261A4;
inline constexpr std::uint32_t kClockControlSize = 0x61A4;  // 24996 parameter bytes
inline constexpr std::uint32_t kClockControlHeader = 0x124;
inline constexpr std::uint32_t kClockControlRow = 0x304;
inline constexpr std::uint32_t kClockControlCapacity = 32;
inline constexpr std::uint32_t kClockControlRequestType = 15;     // the public type of a native type16 row
inline constexpr std::uint32_t kClockControlRequestOffset = 0x114;  // its signed kHz request in the row

// COMMAND, EXPORT, SIZE, HEADER, STRIDE, CAPACITY and _ENVELOPE.
inline constexpr std::uint32_t kClockInfoCommand = 0x20809019;
inline constexpr std::uint32_t kClockInfoExport = 0x57B5A5DF;
inline constexpr std::uint32_t kClockInfoSize = 0x3030;  // 12336 parameter bytes
inline constexpr std::uint32_t kClockInfoHeader = 0x30;
inline constexpr std::uint32_t kClockInfoRow = 0x180;
inline constexpr std::uint32_t kClockInfoCapacity = 32;
inline constexpr std::uint32_t kClockInfoPrologue = 68;  // the 17 escape header words before the parameters
struct ClockEnvelopeWord {
    std::uint32_t index, value;
};
inline constexpr ClockEnvelopeWord kClockInfoEnvelope[4] = {
    {0, 0x4E564441}, {1, 0x10002}, {3, 0x4E562A2A}, {4, 0x0500002B}};

// A failure of the clock-BIOS reader: Python raises PerfDiagnosticError(kind, message) when
// perf_error is set (kind != none), otherwise ClockBiosError(message).
struct ClockBiosFailure {
    bool perf_error = false;  // PerfDiagnosticError (error.kind) rather than ClockBiosError
    RmError error;            // the message, and the PerfDiagnosticError kind
};

// _control_shape's request decode: {row index: signed kHz} for rows of public type 15.
struct ClockRequests {
    std::uint32_t present = 0;  // bit i: requests[i] is set (Python's dict key i)
    std::int32_t khz[kClockControlCapacity] = {};
};

struct ClockShapeResult {
    std::optional<ClockRequests> requests;
    std::string error;  // the ClockBiosError message otherwise
};

// The pure part of _control_shape over the raw payload: the mask (an int in 1..0xFFFFFFFF), the
// geometry, the version and mask words, then the requests. Reused by C2 and the registry path.
ClockShapeResult clock_control_shape(const std::uint8_t* payload, std::size_t size, std::uint64_t mask);
// The same with _hex(payload_hex, 24996) in front of the geometry: `text` holds `length` code points
// of the Python str, each non-ASCII code point stood for by one byte >= 0x80 (or '?'); `text` null is a
// value that is not a str. The mask check comes first, as in Python.
ClockShapeResult clock_control_shape_hex(const char* text, std::size_t length, std::uint64_t mask);

struct ClockControlResult {
    std::uint32_t mask = 0;
    std::vector<std::uint8_t> payload;  // exactly kClockControlSize bytes on success
    ClockBiosFailure failure;           // otherwise
    bool ok() const { return payload.size() == kClockControlSize; }
};

// One row of _decode: type and base_type are the row's first two bytes, api_domain_mask the u32 at +4;
// minimum_mhz / maximum_mhz the two int16 at +0x26 only when type == base_type == 16 (typed), else
// Python's None.
struct ClockInfoRow {
    std::uint8_t type = 0, base_type = 0;
    std::uint32_t api_domain_mask = 0;
    bool typed = false;
    std::int16_t minimum_mhz = 0, maximum_mhz = 0;
};

// _decode(raw): the rows of every bit of the u32 mask at offset 4 (Python's dict keys str(index), in
// index order).
struct ClockInfoRows {
    std::uint32_t present = 0;  // the mask: bit i, rows[i] is decoded
    ClockInfoRow rows[kClockInfoCapacity] = {};
};

struct ClockDecodeResult {
    std::optional<ClockInfoRows> rows;
    std::string error;  // the ClockBiosError message otherwise
};

// _decode(raw). Pure: a size other than kClockInfoSize is "Clock INFO geometry is not understood".
ClockDecodeResult clock_info_decode(const std::uint8_t* raw, std::size_t size);

// The identity _control_once binds (_binding(reader)): Python's plain dict of the slot str and three
// ints, in that key order.
struct ClockIdentity {
    std::string slot;
    std::uint32_t architecture = 0, device_id = 0, subsystem_id = 0;
};

// Python's `_binding(reader) != identity`, evaluated by the caller on its own identity object: nonzero
// when the caller's identity differs from `bound`. Called once per control_once, after the guards and
// the binding and before the status check, under Gpu::lock(); it must not call back into the GPU.
// A null function stands for an identity that equals no binding (Python's None).
using ClockIdentityDiffers = std::int32_t (*)(void* context, const ClockIdentity& bound);

inline bool operator==(const ClockIdentity& a, const ClockIdentity& b) {
    return a.slot == b.slot && a.architecture == b.architecture && a.device_id == b.device_id &&
           a.subsystem_id == b.subsystem_id;
}
inline bool operator!=(const ClockIdentity& a, const ClockIdentity& b) { return !(a == b); }

// _info_once's return value: (identity, bytes(buffer)[68:]), or the failure.
struct ClockInfoResult {
    std::optional<ClockIdentity> identity;
    std::vector<std::uint8_t> info;  // exactly kClockInfoSize bytes on success
    ClockBiosFailure failure;        // otherwise
    bool ok() const { return identity.has_value(); }
};

// read()'s observation dict: {**_LAYOUT, identity, info_hex, user_control, rows, stable_reads (always
// 2), user_control_error}. _LAYOUT and stable_reads are constants the caller adds.
struct ClockObservation {
    ClockIdentity identity;
    std::vector<std::uint8_t> info;  // info_hex
    // user_control: {**_CONTROL_LAYOUT, "mask": mask, "payload_hex": payload.hex()}, or None.
    bool has_user_control = false;
    std::uint32_t user_control_mask = 0;
    std::vector<std::uint8_t> user_control;
    ClockInfoRows rows;                             // _decode(info)
    std::optional<std::string> user_control_error;  // None when the user control was stable
};

// read(gpu): (observation, None) or (None, message).
struct ClockReadResult {
    std::optional<ClockObservation> observation;
    std::string error;  // the message when there is no observation
};

class ClockBios {
public:
    ClockBios(Gpu& gpu, Telemetry& telemetry, RmTransport& rm, ClkDom& clkdom)
        : gpu_(gpu), telemetry_(telemetry), rm_(rm), clkdom_(clkdom), reader_(gpu, telemetry, rm) {}

    // _control_once(gpu, reader, identity) on the handle's reader, with the identity comparison
    // handed to `differs`. The caller holds Gpu::lock().
    ClockControlResult control_once(ClockIdentityDiffers differs, void* context) {
        return control_once(handle_reader(), differs, context);
    }
    // _info_once(gpu, reader) on the handle's reader. The caller holds Gpu::lock().
    ClockInfoResult info_once() { return info_once(handle_reader()); }
    // _binding(reader) on the handle's reader: the identity dict, or the failure (the binding's
    // PerfDiagnosticError, or _identity's ClockBiosError).
    std::optional<ClockIdentity> binding(ClockBiosFailure& failure) { return binding(handle_reader(), failure); }
    // read(gpu): a fresh reader for this call (the handle's reader is untouched), two attempts, each
    // under Gpu::lock() taken and released here. A caller that holds Gpu::lock() itself keeps it
    // across both attempts (the lock is recursive; read() gives back only its own level).
    ClockReadResult read();
    // A fresh reader: forget the sticky identity.
    void reset() {
        reader_.reset();
        held_ = false;
    }
    // Whether the handle's reader holds a sticky identity (Python's `reader._identity is not None`):
    // set by every bind() that returned a target, exactly when TargetBinding records its identity.
    bool held() const { return held_; }

private:
    // One PerfDiagnosticsReader: its binding and transport, and whether it holds an identity.
    struct Reader {
        PerfDiagnostics& perf;
        bool& held;
    };
    Reader handle_reader() { return Reader{reader_, held_}; }
    std::optional<ClockIdentity> binding(Reader reader, ClockBiosFailure& failure);
    ClockControlResult control_once(Reader reader, ClockIdentityDiffers differs, void* context);
    ClockInfoResult info_once(Reader reader);

    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport& rm_;
    ClkDom& clkdom_;
    PerfDiagnostics reader_;  // the handle's PerfDiagnosticsReader (dc_clock_bios_control / _info)
    bool held_ = false;
};

}  // namespace druta::backend
