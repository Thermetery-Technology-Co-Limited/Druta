// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The clock-BIOS reader (port of clock_bios_runtime's read path): increment C1 adds the raw guarded
// user-clock CONTROL GET (_control_once and the pure _control_shape); C2 the INFO GET through the paired
// A612 transport and the two-sample read. One reader per handle with its own target binding, as each
// Python read() constructs its own PerfDiagnosticsReader.
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

class ClockBios {
public:
    ClockBios(Gpu& gpu, Telemetry& telemetry, RmTransport& rm, ClkDom& clkdom)
        : gpu_(gpu), telemetry_(telemetry), rm_(rm), clkdom_(clkdom), binding_(gpu, telemetry) {}

    // _control_once(gpu, reader, identity) with the identity comparison handed to `differs`. The
    // caller holds Gpu::lock().
    ClockControlResult control_once(ClockIdentityDiffers differs, void* context);
    // _binding(reader): the identity dict, or the failure (the binding's PerfDiagnosticError, or
    // _identity's ClockBiosError).
    std::optional<ClockIdentity> binding(ClockBiosFailure& failure);
    // A fresh reader: forget the sticky identity.
    void reset() {
        binding_.reset();
        held_ = false;
    }
    // Whether the reader holds a sticky identity (Python's `reader._identity is not None`): set by
    // every bind() that returned a target, exactly when TargetBinding records its identity.
    bool held() const { return held_; }

private:
    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport& rm_;
    ClkDom& clkdom_;
    TargetBinding binding_;  // the read's PerfDiagnosticsReader
    bool held_ = false;
};

}  // namespace druta::backend
