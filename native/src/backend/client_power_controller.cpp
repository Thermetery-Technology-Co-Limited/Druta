// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/client_power_controller.h"

#include <cstdio>
#include <cstring>

#include "backend/gdi.h"

namespace druta::backend {

namespace {

constexpr std::uint32_t kA612 = 0x2080A612, kP2612 = 0x20802612;
constexpr CpTransportKey kRm26Key{kP2612, 2524, 2456}, kRm26ExtendedKey{kP2612, 3548, 3480};

RmError failure(RmErrorKind kind, std::string message, std::uint32_t rm_status = 0) {
    RmError e;
    e.kind = kind;
    e.message = std::move(message);
    e.rm_status = rm_status;
    return e;
}

// ClientPowerError(exc.kind, str(exc)) from a binding error: the kind and message only.
RmError rethrown(const RmError& why) { return failure(why.kind, why.message); }

// The identity dict _binding returns: Python compares slot, architecture, device_id and subsystem_id.
bool same_identity(const Target& a, const Target& b) {
    return a.slot == b.slot && a.architecture == b.architecture && a.device_id == b.device_id &&
           a.subsystem_id == b.subsystem_id;
}

bool contains(CpU32List list, std::uint32_t value) {
    for (std::size_t i = 0; i < list.count; ++i)
        if (list.values[i] == value) return true;
    return false;
}

std::string hex(std::uint32_t value) {  // Python's f"{value:#x}"
    char text[16];
    std::snprintf(text, sizeof text, "0x%x", static_cast<unsigned>(value));
    return text;
}

}  // namespace

// ---- GuardedBuffer ------------------------------------------------------ //

std::uint32_t GuardedBuffer::word(std::size_t i) const {
    std::uint32_t value;
    std::memcpy(&value, data() + 4 * i, 4);  // little-endian hosts only, as the packet is
    return value;
}

void GuardedBuffer::set_word(std::size_t i, std::uint32_t value) { std::memcpy(data() + 4 * i, &value, 4); }

bool GuardedBuffer::guards_intact() const {
    for (std::size_t i = 0; i < kGuard; ++i)
        if (storage_[i] != kSentinel || storage_[kGuard + size_ + i] != kSentinel) return false;
    return true;
}

// ---- the controller ----------------------------------------------------- //

void ClientPowerController::reset_transport() {
    rm_.power.reset();
    rm_.layout_choice.reset();
}

void ClientPowerController::reset() {
    state_ = CpControllerState{};
    binding_.reset();
}

std::optional<std::size_t> ClientPowerController::command_index(std::uint32_t command) {
    // Every size group holds the same seven commands, so `command in self.sizes` is membership here.
    for (std::size_t i = 0; i < kCpCommandCount; ++i)
        if (kCpCommands[i] == command) return i;
    return std::nullopt;
}

TargetResult ClientPowerController::binding() {
    TargetResult r = binding_.bind_client_power();
    if (!r.target) r.error = rethrown(r.error);
    return r;
}

CpTransportResult ClientPowerController::transport() {
    CpTransportResult out;
    TargetResult identity = binding();
    if (!identity.target) {
        out.error = identity.error;
        return out;
    }
    // The paired transport only, not a current-policy record layout (see the Python comment).
    TransportResult t = rm_.power_transport();
    if (!t.transport) {
        // Python's `except ValueError`: CurrentLimitUnsupported stays "unsupported", every other
        // ValueError becomes "transient" with the same text. Python's catch-all ("Paired power
        // transport could not be captured: ...") covers non-ValueError exceptions of the Python
        // getter; RmTransport::power_transport reports every failure as an RmError, so that path
        // has no native counterpart.
        out.error = failure(t.error.kind == RmErrorKind::unsupported ? RmErrorKind::unsupported : RmErrorKind::transient,
                            t.error.message);
        return out;
    }
    const CapturedTransport& transport = *t.transport;
    const auto& header = transport.header;
    // The 32-bit word, tuple/list and exact field-set checks of Python are satisfied by
    // construction here: the header is a uint32 array and EscapeFields has exactly the five fields.
    if (transport.header_count != 17 ||
        static_cast<std::uint64_t>(header[2]) != 68u + static_cast<std::uint64_t>(header[15])) {
        out.error = failure(RmErrorKind::malformed, "Client-power transport geometry is not understood");
        return out;
    }
    const CpTransportKey key{header[14], header[2], header[15]};
    if (state_.transport_key && key != *state_.transport_key) {
        out.error = failure(RmErrorKind::identity, "Client-power transport changed; create a fresh controller");
        return out;
    }
    if (key == kRm26Key || key == kRm26ExtendedKey) {
        if (!contains(cp_rm26_architectures(), identity.target->architecture)) {
            out.error = failure(RmErrorKind::unsupported,
                                "This client-power RM26 ABI is not qualified for this GPU generation");
            return out;
        }
        state_.rm26_extended = key == kRm26ExtendedKey;
        state_.sizes = state_.rm26_extended ? CpSizeGroup::rm26_extended : CpSizeGroup::rm26;
        state_.rm26 = state_.rm26_records = true;
    } else {
        if (state_.rm26) {
            out.error = failure(RmErrorKind::identity, "Client-power transport changed; create a fresh controller");
            return out;
        }
        if (header[14] != kA612) {
            out.error = failure(RmErrorKind::unsupported, "Client-power command family is not understood");
            return out;
        }
    }
    TargetResult again = binding();
    if (!again.target) {
        out.error = again.error;
        return out;
    }
    if (!same_identity(*again.target, *identity.target)) {
        out.error = failure(RmErrorKind::identity, "Client-power target changed during transport capture");
        return out;
    }
    for (const EnvelopeWord& w : kRmEnvelope) {
        if (header[w.index] != w.value) {
            out.error = failure(RmErrorKind::malformed, "Client-power transport envelope is not understood");
            return out;
        }
    }
    state_.transport_key = key;
    out.transport = transport;
    return out;
}

CpExchangeResult ClientPowerController::exchange(std::uint32_t command, const std::uint8_t* payload,
                                                 std::size_t payload_size, bool has_write) {
    CpExchangeResult out;
    std::optional<std::size_t> index = command_index(command);
    if (!index || payload_size != sizes()[*index] || (command == kCpSet) != has_write) {
        out.error = failure(RmErrorKind::malformed, "Client-power command or packet geometry is not understood");
        return out;
    }
    TargetResult identity = binding();
    if (!identity.target) {
        out.error = identity.error;
        return out;
    }
    CpTransportResult t = transport();
    if (!t.transport) {
        out.error = t.error;
        return out;
    }
    const CapturedTransport& transport = *t.transport;
    // _transport may have switched the size table (the RM26 keys): size comes from the new one.
    const std::uint32_t size = sizes()[*index];
    if (size != payload_size) {
        // Python's `buffer[68:] = payload` on a ctypes array of another length raises this bare
        // ValueError (not a ClientPowerError), before any dispatch. Reported as kind invalid.
        out.error = failure(RmErrorKind::invalid, "Can only assign sequence of same size");
        return out;
    }
    GuardedBuffer buffer(68u + std::size_t{size});
    for (std::size_t i = 0; i < 17; ++i) buffer.set_word(i, transport.header[i]);
    const std::uint32_t wire = state_.rm26 ? kCpRm26Commands[*index] : command;
    buffer.set_word(2, 68u + size);
    buffer.set_word(14, wire);
    buffer.set_word(15, size);
    buffer.set_word(16, 0);
    buffer.set_word(5, 0);
    buffer.set_word(11, 0);
    if (size) std::memcpy(buffer.data() + 68, payload, size);
    std::uint32_t expected[16];
    for (std::size_t i = 0; i < 16; ++i) expected[i] = buffer.word(i);
    // Python sets write.dispatched here whenever a write object was passed (`if write is not None`),
    // which the binding decides; the core reports that the packet reached this point.
    out.dispatched = true;
    // ctypes.sizeof(packet): the whole words of the guarded allocation.
    EscapeResult escape =
        escape_raw(gpu_, buffer.data(), static_cast<std::uint32_t>(buffer.words() * 4), transport.fields);
    // An unavailable escape is Python's `status = None` (nvbackend returns None, it does not raise).
    // Python's "Client-power transport failed: {exc}" covers an exception from the escape itself;
    // escape_raw reports every failure as unavailable, so that path has no native counterpart.
    std::optional<long> status;
    if (escape.outcome == EscapeOutcome::issued) status = escape.ntstatus;
    if (!buffer.guards_intact()) {
        out.error = failure(RmErrorKind::malformed, GuardedBuffer::kOverrun);
        return out;
    }
    TargetResult after = binding();
    if (!after.target) {
        out.error = after.error;
        return out;
    }
    if (!same_identity(*after.target, *identity.target)) {
        out.error = failure(RmErrorKind::identity, "Client-power target changed during the operation");
        return out;
    }
    bool envelope = buffer.word(5) == 0 || buffer.word(5) == 1;
    for (std::size_t i = 0; i < 16 && envelope; ++i)
        if (i != 5 && i != 11 && buffer.word(i) != expected[i]) envelope = false;
    if (!envelope) {
        out.error = failure(RmErrorKind::malformed, "Client-power response changed its transport envelope");
        return out;
    }
    const std::uint32_t completion = buffer.word(11);
    if (!status || *status != 0 || completion != 1) {
        out.error = failure(RmErrorKind::transient, "Client-power escape failed (NT " +
                                                        (status ? std::to_string(*status) : std::string("None")) +
                                                        ", completion " + std::to_string(completion) + ")");
        return out;
    }
    const std::uint32_t rm = buffer.word(16);
    if (rm != 0) {
        const bool definite = contains(cp_size_refusals(), rm) || rm == 0x56 || rm == 0x40;
        out.error = failure(definite ? RmErrorKind::unsupported : RmErrorKind::transient,
                            "Client-power command " + hex(wire) + " rejected (RM " + hex(rm) + ")", rm);
        return out;
    }
    std::vector<std::uint8_t> result(buffer.data() + 68, buffer.data() + 68 + size);
    if (command == kCpSet && (size && std::memcmp(result.data(), payload, size) != 0)) {
        out.error = failure(RmErrorKind::malformed, "Client-power SET changed the requested packet");
        return out;
    }
    out.response = std::move(result);
    return out;
}

}  // namespace druta::backend
