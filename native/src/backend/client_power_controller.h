// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The client-power controller (port of client_power_runtime.ClientPowerController): increment A3 adds
// the paired transport and the guarded exchange; A4 the client group choice, the metadata capture and
// the bounded stable read; later increments the full read and the request SET. One sticky controller per handle, with its own target binding as the Python
// controller keeps its own PerfDiagnosticsReader.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "backend/client_power.h"
#include "backend/gpu.h"
#include "backend/hook.h"
#include "backend/rm_error.h"
#include "backend/rm_transport.h"
#include "backend/target.h"
#include "backend/telemetry.h"

namespace druta::backend {

// clock_bios_runtime._ENVELOPE: the packet words every understood RM escape carries.
struct EnvelopeWord {
    std::size_t index;
    std::uint32_t value;
};
inline constexpr EnvelopeWord kRmEnvelope[4] = {
    {0, 0x4E564441}, {1, 0x10002}, {3, 0x4E562A2A}, {4, 0x0500002B}};

// clock_bios_runtime._guarded / _guards: `size` zeroed bytes between two 64-byte 0xA5 guards. The
// guards catch a driver that writes past the parameter block it was given. The word view is the
// ctypes c_uint32 array Python lays over the same bytes: size / 4 words, the escape's packet.
class GuardedBuffer {
public:
    static constexpr std::size_t kGuard = 64;
    static constexpr std::uint8_t kSentinel = 0xA5;

    explicit GuardedBuffer(std::size_t size) : size_(size), storage_(size + 2 * kGuard, kSentinel) {
        for (std::size_t i = 0; i < size; ++i) storage_[kGuard + i] = 0;
    }
    std::uint8_t* data() { return storage_.data() + kGuard; }
    const std::uint8_t* data() const { return storage_.data() + kGuard; }
    std::size_t size() const { return size_; }
    std::size_t words() const { return size_ / 4; }
    std::uint32_t word(std::size_t i) const;
    void set_word(std::size_t i, std::uint32_t value);
    // _guards: false when either guard lost a sentinel byte.
    bool guards_intact() const;
    // The message _guards raises (a PerfDiagnosticError "malformed").
    static constexpr const char* kOverrun = "Clock GET exceeded the guarded allocation";

private:
    std::size_t size_;
    std::vector<std::uint8_t> storage_;
};

// _transport's key: (header[14], header[2], header[15]).
struct CpTransportKey {
    std::uint32_t command = 0, packet_bytes = 0, parameter_bytes = 0;
    bool operator==(const CpTransportKey& o) const {
        return command == o.command && packet_bytes == o.packet_bytes && parameter_bytes == o.parameter_bytes;
    }
    bool operator!=(const CpTransportKey& o) const { return !(*this == o); }
};

struct CpTransportResult {
    std::optional<CapturedTransport> transport;  // (header, fields) on success
    RmError error;                               // a ClientPowerError(kind, message, rm_status=...) otherwise
};

// What the caller of _exchange holds that the core cannot see: Python's own failures inside the
// exchange, each at its own point. The Python binding sets them from its arguments; a C caller with
// plain bytes leaves them clear.
struct CpCaller {
    // `packet[14] = wire_command` raises (a command that compares equal to a known one without
    // being an integer ctypes accepts): refused there, unless the RM26 table supplies the wire command.
    bool command_not_storable = false;
    // `buffer[68:] = payload` raises after its length check (an element ctypes cannot store).
    bool payload_not_storable = false;
    // The caller compares the SET echo itself (Python's `result != payload` on its own object).
    bool caller_echo = false;
    // `write.dispatched = True`, right before the escape: nonzero refuses the dispatch.
    std::int32_t (*before_dispatch)(void* context) = nullptr;
    void* context = nullptr;
};

// The stops a CpCaller can cause: an RmError of kind invalid carrying one of these messages.
inline constexpr const char* kCpStopCommand = "caller stop: the command word";
inline constexpr const char* kCpStopPayload = "caller stop: the payload bytes";
inline constexpr const char* kCpStopDispatch = "caller stop: the dispatch";

struct CpExchangeResult {
    std::optional<std::vector<std::uint8_t>> response;  // the `size` response bytes on success
    RmError error;
    // The response capacity is below the command's size in the current table: refused before any
    // binding or dispatch (no error is set).
    bool too_small = false;
};

// _metadata_once's result dict. identity is _binding()'s dict (Target::handle is not part of it and
// is ignored by ==); semantic_role, client and units are the constants kCpRole, kCpClient and "mW".
// The optional binding keys keep Python's insertion order: layout, native_layout, native_info_stride.
struct CpMetadata {
    Target identity;
    std::uint32_t client_index = 0, native_index = 0;
    std::uint32_t minimum_mw = 0, rated_mw = 0, maximum_mw = 0;
    CpLayout layout = CpLayout::absent;                     // absent, rm26 or legacy32
    CpNativeLayout native_layout = CpNativeLayout::absent;  // absent, policy124 or rm26_extended
    std::optional<std::uint32_t> native_info_stride;        // only when it differs from the layout's
    std::vector<std::uint8_t> info, role_info, native_info;
    // Python's whole-dict `first != second`.
    bool operator==(const CpMetadata& o) const;
    bool operator!=(const CpMetadata& o) const { return !(*this == o); }
};

// A capture's value, or the ClientPowerError (or the bare ValueError, kind invalid) it raised.
template <typename T>
struct CpRead {
    std::optional<T> value;
    RmError error;
};

// The message _stable_locked raises (kind unstable) when two fresh captures differ.
inline constexpr const char* kCpUnstable = "Client-power definitions or requests changed between fresh reads";

// The controller state of ClientPowerController.__init__.
struct CpControllerState {
    CpSizeGroup sizes = CpSizeGroup::sizes;  // self.sizes: which module table it is
    bool legacy = false, native_policy124 = false, role_info216 = false;
    bool rm26_records = false, rm26 = false, rm26_extended = false;
    std::optional<CpTransportKey> transport_key;  // _transport_key
};

class ClientPowerController {
public:
    ClientPowerController(Gpu& gpu, Telemetry& telemetry, RmTransport& rm)
        : gpu_(gpu), telemetry_(telemetry), rm_(rm), binding_(gpu, telemetry) {}

    // _reset_transport: forget the GPU's cached power transport and layout choice.
    void reset_transport();
    // A fresh controller: every attribute back to __init__, the binding's sticky identity included.
    void reset();
    // _transport: the paired power transport, checked and keyed.
    CpTransportResult transport();
    // _exchange(command, payload, write=...): one exact command, one escape. has_write is
    // isinstance(write, _Write); capacity is the room for the response. dispatched becomes true the
    // moment the packet is handed to the escape (Python's write.dispatched), so it survives a later
    // failure, an exception included.
    CpExchangeResult exchange(std::uint32_t command, const std::uint8_t* payload, std::size_t payload_size,
                              bool has_write, std::size_t capacity, const CpCaller& caller, bool& dispatched);

    // _client_group(identity): the client INFO this driver accepts, chosen by acceptance alone.
    CpExchangeResult client_group(const Target& identity);
    // _metadata_once: one INFO / ROLE_INFO / NATIVE_INFO capture bound to a unique native policy.
    CpRead<CpMetadata> metadata_once();
    // _stable_locked(capture): two equal captures; one fresh pair after a transient failure, with the
    // transport cache reset in between. capture() returns a CpRead<T>; T needs operator==.
    template <typename T, typename Capture>
    CpRead<T> stable_locked(Capture&& capture);
    // read_metadata (the caller holds the GPU lock): _stable_locked(_metadata_once).
    CpRead<CpMetadata> read_metadata();

    const CpControllerState& state() const { return state_; }
    const CpSizes& sizes() const { return *cp_size_group(static_cast<std::uint32_t>(state_.sizes)); }
    // The index of command in kCpCommands (the current table's keys), or nullopt: `command in self.sizes`.
    static std::optional<std::size_t> command_index(std::uint32_t command);

private:
    TargetResult binding();
    // _exchange(command, payload) for a GET: no write, no caller stops, room for the response.
    CpExchangeResult get(std::uint32_t command, const std::vector<std::uint8_t>& payload);

    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport& rm_;
    TargetBinding binding_;  // self._reader: the controller's own sticky identity
    CpControllerState state_;
};

template <typename T, typename Capture>
CpRead<T> ClientPowerController::stable_locked(Capture&& capture) {
    CpRead<T> out;
    for (int attempt = 0; attempt < 2; ++attempt) {
        RmError error;
        CpRead<T> first = capture();
        if (first.value) {
            CpRead<T> second = capture();
            if (!second.value) {
                error = std::move(second.error);
            } else if (*first.value != *second.value) {
                error.kind = RmErrorKind::unstable;
                error.message = kCpUnstable;
            } else {
                return second;
            }
        } else {
            error = std::move(first.error);
        }
        // Only a ClientPowerError of kind transient is retried; anything else (a bare ValueError
        // included, which Python's `except ClientPowerError` does not catch) is raised as it is.
        if (error.kind != RmErrorKind::transient || attempt == 1) {
            if (error.kind == RmErrorKind::transient) reset_transport();
            out.error = std::move(error);
            return out;
        }
        reset_transport();
    }
    // Python's AssertionError("Bounded client-power read did not return or fail"): unreachable.
    out.error.kind = RmErrorKind::transient;
    out.error.message = "Bounded client-power read did not return or fail";
    return out;
}

}  // namespace druta::backend
