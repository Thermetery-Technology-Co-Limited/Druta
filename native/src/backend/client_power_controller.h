// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The client-power controller (port of client_power_runtime.ClientPowerController): increment A3 adds
// the paired transport and the guarded exchange; later increments the metadata read, the full read and
// the request SET. One sticky controller per handle, with its own target binding as the Python
// controller keeps its own PerfDiagnosticsReader.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
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

struct CpExchangeResult {
    std::optional<std::vector<std::uint8_t>> response;  // the `size` response bytes on success
    RmError error;
    bool dispatched = false;  // the packet reached the escape: Python sets write.dispatched there
};

// The controller state of ClientPowerController.__init__ (and nothing else yet).
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
    // isinstance(write, _Write); result.dispatched says the packet reached the escape.
    CpExchangeResult exchange(std::uint32_t command, const std::uint8_t* payload, std::size_t payload_size,
                              bool has_write);

    const CpControllerState& state() const { return state_; }
    const CpSizes& sizes() const { return *cp_size_group(static_cast<std::uint32_t>(state_.sizes)); }
    // The index of command in kCpCommands (the current table's keys), or nullopt: `command in self.sizes`.
    static std::optional<std::size_t> command_index(std::uint32_t command);

private:
    TargetResult binding();

    Gpu& gpu_;
    Telemetry& telemetry_;
    RmTransport& rm_;
    TargetBinding binding_;  // self._reader: the controller's own sticky identity
    CpControllerState state_;
};

}  // namespace druta::backend
