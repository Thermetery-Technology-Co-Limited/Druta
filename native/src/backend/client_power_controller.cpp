// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/client_power_controller.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <exception>
#include <new>
#include <string>
#include <vector>

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

std::uint32_t u32_at(const std::vector<std::uint8_t>& raw, std::size_t offset) {  // struct "<I"
    return static_cast<std::uint32_t>(raw[offset]) | static_cast<std::uint32_t>(raw[offset + 1]) << 8 |
           static_cast<std::uint32_t>(raw[offset + 2]) << 16 | static_cast<std::uint32_t>(raw[offset + 3]) << 24;
}

// _A612_FALLBACKS: the A612 client groups, in probe order after the modern one is refused for its
// size, each with the attribute that records the choice.
struct CpFallback {
    CpSizeGroup sizes;
    bool CpControllerState::*mark;
};
constexpr CpFallback kA612Fallbacks[] = {
    {CpSizeGroup::legacy, &CpControllerState::legacy},
    {CpSizeGroup::a612_compact, &CpControllerState::rm26_records},
};
constexpr std::size_t kA612FallbackCount = sizeof(kA612Fallbacks) / sizeof(kA612Fallbacks[0]);

}  // namespace

bool CpMetadata::operator==(const CpMetadata& o) const {
    return same_identity(identity, o.identity) && client_index == o.client_index && native_index == o.native_index &&
           minimum_mw == o.minimum_mw && rated_mw == o.rated_mw && maximum_mw == o.maximum_mw && layout == o.layout &&
           native_layout == o.native_layout && native_info_stride == o.native_info_stride && info == o.info &&
           role_info == o.role_info && native_info == o.native_info;
}

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
                                                 std::size_t payload_size, bool has_write, std::size_t capacity,
                                                 const CpCaller& caller, bool& dispatched) {
    CpExchangeResult out;
    dispatched = false;
    std::optional<std::size_t> index = command_index(command);
    if (!index || payload_size != sizes()[*index] || (command == kCpSet) != has_write) {
        out.error = failure(RmErrorKind::malformed, "Client-power command or packet geometry is not understood");
        return out;
    }
    // Not Python: the response must fit before anything runs. The payload already has this size, and
    // a size table switched by _transport below fails the payload's length check before the escape,
    // so a dispatched packet always has room for its response.
    if (capacity < sizes()[*index]) {
        out.too_small = true;
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
    GuardedBuffer buffer(68u + std::size_t{size});
    for (std::size_t i = 0; i < 17; ++i) buffer.set_word(i, transport.header[i]);
    // Python's `wire_command = _RM26_COMMANDS[command] if self._rm26 else command`, stored at packet[14]:
    // the caller's own value only when the RM26 table does not supply the wire command.
    if (caller.command_not_storable && !state_.rm26) {
        out.error = failure(RmErrorKind::invalid, kCpStopCommand);
        return out;
    }
    const std::uint32_t wire = state_.rm26 ? kCpRm26Commands[*index] : command;
    buffer.set_word(2, 68u + size);
    buffer.set_word(14, wire);
    buffer.set_word(15, size);
    buffer.set_word(16, 0);
    buffer.set_word(5, 0);
    buffer.set_word(11, 0);
    if (size != payload_size) {
        // _transport switched the size table (the RM26 keys) under the payload: Python's
        // `buffer[68:] = payload` on a ctypes array of another length raises this bare ValueError
        // (not a ClientPowerError), before any dispatch. Reported as kind invalid.
        out.error = failure(RmErrorKind::invalid, "Can only assign sequence of same size");
        return out;
    }
    if (caller.payload_not_storable) {
        out.error = failure(RmErrorKind::invalid, kCpStopPayload);
        return out;
    }
    if (size) std::memcpy(buffer.data() + 68, payload, size);
    std::uint32_t expected[16];
    for (std::size_t i = 0; i < 16; ++i) expected[i] = buffer.word(i);
    // Python's `if write is not None: write.dispatched = True`, before the escape: the caller's hook.
    if (caller.before_dispatch && caller.before_dispatch(caller.context) != 0) {
        out.error = failure(RmErrorKind::invalid, kCpStopDispatch);
        return out;
    }
    dispatched = true;
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
    if (!caller.caller_echo && command == kCpSet && (size && std::memcmp(result.data(), payload, size) != 0)) {
        out.error = failure(RmErrorKind::malformed, "Client-power SET changed the requested packet");
        return out;
    }
    out.response = std::move(result);
    return out;
}

// ---- increment A4: the client group, the metadata capture, the stable read -- //

CpExchangeResult ClientPowerController::get(std::uint32_t command, const std::vector<std::uint8_t>& payload) {
    bool dispatched = false;
    CpExchangeResult r = exchange(command, payload.data(), payload.size(), false, payload.size(), CpCaller{}, dispatched);
    if (r.too_small) {
        // Unreachable: the payload has the command's size in the current table, and so does the room.
        r.too_small = false;
        r.error = failure(RmErrorKind::malformed, "Client-power command or packet geometry is not understood");
    }
    return r;
}

CpExchangeResult ClientPowerController::client_group(const Target& identity) {
    CpExchangeResult first = get(kCpInfo, std::vector<std::uint8_t>(sizes()[kCpIdxInfo], 0));
    if (first.response) return first;
    // A bare ValueError (kind invalid, rm_status 0) is not a ClientPowerError: it escapes the
    // `except` uncaught, which is what the size-refusal test below does with it too.
    if (state_.legacy || state_.rm26_records || !contains(cp_size_refusals(), first.error.rm_status) ||
        !contains(cp_legacy_group_architectures(), identity.architecture))
        return first;
    const CpSizeGroup previous = state_.sizes;
    for (std::size_t position = 0; position < kA612FallbackCount; ++position) {
        const CpFallback& fallback = kA612Fallbacks[position];
        // _exchange checks the payload against self.sizes, so the table has to be in place to probe
        // with it. Put back on any failure: a transient error must leave no group half-selected.
        state_.sizes = fallback.sizes;
        CpExchangeResult info = get(kCpInfo, std::vector<std::uint8_t>(sizes()[kCpIdxInfo], 0));
        if (!info.response) {
            // Python's `except ClientPowerError` restores the table; a bare ValueError passes it by.
            if (info.error.kind == RmErrorKind::invalid) return info;
            state_.sizes = previous;
            if (!contains(cp_size_refusals(), info.error.rm_status) || position == kA612FallbackCount - 1) return info;
            continue;
        }
        state_.*fallback.mark = true;
        if (fallback.sizes == CpSizeGroup::a612_compact) state_.rm26_extended = true;
        return info;
    }
    CpExchangeResult none;  // unreachable: the last fallback either returns or raises
    none.error = failure(RmErrorKind::unsupported, "No understood client-power group was accepted");
    return none;
}

CpRead<CpMetadata> ClientPowerController::metadata_once() {
    CpRead<CpMetadata> out;
    TargetResult identity = binding();
    if (!identity.target) {
        out.error = identity.error;
        return out;
    }
    CpTransportResult transport_now = transport();  // select the command family before allocating INFO
    if (!transport_now.transport) {
        out.error = transport_now.error;
        return out;
    }
    CpExchangeResult group = client_group(*identity.target);
    if (!group.response) {
        out.error = group.error;
        return out;
    }
    const std::vector<std::uint8_t>& info = *group.response;

    std::size_t base, info_stride, capacity;
    if (state_.rm26_records)
        base = 4, info_stride = state_.rm26_extended ? 28 : 20, capacity = 32;
    else if (state_.legacy)
        base = 8, info_stride = kCpInfoStride, capacity = 32;
    else
        base = kCpInfoBase, info_stride = kCpInfoStride, capacity = kCpCapacity;
    std::size_t native_base, default_stride;
    if (state_.rm26_extended)
        native_base = 60, default_stride = 132;
    else if (state_.rm26_records)
        native_base = 52, default_stride = 116;
    else if (state_.legacy)
        native_base = 88, default_stride = 228;
    else
        native_base = kCpNativeInfoBase, default_stride = kCpNativeInfoStride;

    // int.from_bytes(info[:4] if _rm26_records else info[4:base], "little"): bit i is byte i / 8.
    const std::size_t mask_begin = state_.rm26_records ? 0 : 4;
    const std::size_t mask_end = std::min(state_.rm26_records ? std::size_t{4} : base, info.size());
    const std::size_t mask_bytes = mask_end > mask_begin ? mask_end - mask_begin : 0;
    auto mask_bit = [&](std::size_t i) {
        return i / 8 < mask_bytes && (static_cast<unsigned>(info[mask_begin + i / 8]) >> (i % 8) & 1u) != 0;
    };
    for (std::size_t j = capacity / 8; j < mask_bytes; ++j) {  // mask >> capacity (capacity is whole bytes)
        if (info[mask_begin + j] != 0) {
            out.error = failure(RmErrorKind::malformed, "Client-power inventory exceeds the understood row capacity");
            return out;
        }
    }
    std::vector<std::size_t> candidates;
    for (std::size_t i = 0; i < capacity; ++i) {
        const std::size_t at = base + i * info_stride + 2;
        // Every understood group's rows lie inside its INFO, so Python's IndexError cannot arise.
        if (mask_bit(i) && at < info.size() && info[at] == 1) candidates.push_back(i);
    }
    if (candidates.size() != 1) {
        out.error = failure(RmErrorKind::unsupported, "No unique role1 client-power policy is available");
        return out;
    }
    const std::size_t index = candidates[0];
    const std::size_t at = base + index * info_stride;
    const std::uint8_t* row = info.data() + at;
    if (row[0] != row[1]) {
        out.error = failure(RmErrorKind::malformed, "Selected client INFO type discriminants disagree");
        return out;
    }
    if (row[3] != 0) {
        out.error = failure(RmErrorKind::unsupported, "Selected client-power policy does not report milliwatts");
        return out;
    }
    const std::uint32_t limits[3] = {u32_at(info, at + 4), u32_at(info, at + 8), u32_at(info, at + 12)};
    if (!(limits[0] <= limits[1] && limits[1] <= limits[2])) {
        out.error = failure(RmErrorKind::malformed, "Client-power ordinary limits are inconsistent");
        return out;
    }

    auto role_request = [&] {
        std::vector<std::uint8_t> request(sizes()[kCpIdxRoleInfo], 0);
        request[0] = static_cast<std::uint8_t>(kCpRole);
        return request;
    };
    std::vector<std::uint8_t> request = role_request();
    CpExchangeResult role = get(kCpRoleInfo, request);
    if (!role.response) {
        // A role GET refused for its SIZE: try the one other understood small-group record size
        // (INFO216); a transient error selects nothing.
        if (!state_.legacy || state_.role_info216 || role.error.rm_status != 0x1F) {
            out.error = role.error;
            return out;
        }
        state_.sizes = CpSizeGroup::info216;
        state_.role_info216 = true;
        request = role_request();
        role = get(kCpRoleInfo, request);
        if (!role.response) {
            out.error = role.error;
            return out;
        }
    }
    const std::vector<std::uint8_t>& role_info = *role.response;
    if (std::memcmp(role_info.data(), request.data(), 4) != 0) {
        out.error = failure(RmErrorKind::malformed, "Power-role INFO changed its semantic selector");
        return out;
    }
    const std::optional<std::int64_t> derived = definition_stride(static_cast<std::int64_t>(role_info.size()));
    if (!derived || *derived <= 0 || *derived % 4 != 0 || *derived < 20) {
        out.error = failure(RmErrorKind::malformed, "Power-role INFO carries no usable policy definition");
        return out;
    }
    const std::size_t native_stride = static_cast<std::size_t>(*derived);
    const std::uint8_t* definition = role_info.data() + 4;  // role[4:], native_stride bytes

    CpExchangeResult native = get(kCpNativeInfo, std::vector<std::uint8_t>(sizes()[kCpIdxNativeInfo], 0));
    if (!native.response) {
        if (!state_.legacy || state_.native_policy124 || state_.role_info216 || native.error.rm_status != 0x1F) {
            out.error = native.error;
            return out;
        }
        state_.sizes = CpSizeGroup::turing;
        state_.native_policy124 = true;
        native = get(kCpNativeInfo, std::vector<std::uint8_t>(sizes()[kCpIdxNativeInfo], 0));
        if (!native.response) {
            out.error = native.error;
            return out;
        }
    }
    const std::vector<std::uint8_t>& native_info = *native.response;
    const std::uint32_t native_mask = u32_at(native_info, 4);
    std::vector<std::size_t> matches;
    for (std::size_t i = 0; i < kCpNativeCapacity; ++i) {
        if (!(native_mask >> i & 1u)) continue;
        const std::size_t begin = native_base + i * native_stride;
        // A slice running past the end is shorter than the definition, so it never equals it.
        if (begin + native_stride > native_info.size()) continue;
        if (std::memcmp(native_info.data() + begin, definition, native_stride) == 0) matches.push_back(i);
    }
    if (matches.size() != 1) {
        out.error = failure(RmErrorKind::malformed, "Role1 INFO does not uniquely bind a current native policy");
        return out;
    }
    const std::size_t native_index = matches[0];
    // This byte describes the firmware policy table, not the RM packet capacity.
    const bool v30_only = state_.legacy || state_.rm26_records;
    const std::uint8_t version = definition[0];
    const bool version_ok = version == 0x30 || (!v30_only && version == 0x40);
    const std::vector<std::uint8_t> source(definition, definition + native_stride);
    if (!version_ok || source[6] != row[3] || u32_at(source, 8) != limits[0] || u32_at(source, 12) != limits[1] ||
        u32_at(source, 16) != limits[2]) {
        out.error = failure(RmErrorKind::malformed, "Client INFO differs from the bound native policy definition");
        return out;
    }
    TargetResult again = binding();
    if (!again.target) {
        out.error = again.error;
        return out;
    }
    if (!same_identity(*again.target, *identity.target)) {
        out.error = failure(RmErrorKind::identity, "Client-power target changed during metadata capture");
        return out;
    }
    CpMetadata m;
    m.identity = *identity.target;
    m.client_index = static_cast<std::uint32_t>(index);
    m.native_index = static_cast<std::uint32_t>(native_index);
    m.minimum_mw = limits[0];
    m.rated_mw = limits[1];
    m.maximum_mw = limits[2];
    // The dict literal's later ** entries replace an earlier same-named key's value in place.
    if (state_.legacy) m.layout = CpLayout::legacy32;
    if (state_.rm26_records) m.layout = CpLayout::rm26;
    if (state_.rm26_extended) m.native_layout = CpNativeLayout::rm26_extended;
    if (state_.native_policy124 || state_.role_info216) m.native_layout = CpNativeLayout::policy124;
    if (native_stride != default_stride) m.native_info_stride = static_cast<std::uint32_t>(native_stride);
    m.info = info;
    m.role_info = role_info;
    m.native_info = native_info;
    out.value = std::move(m);
    return out;
}

CpRead<CpMetadata> ClientPowerController::read_metadata() {
    return stable_locked<CpMetadata>([this] { return metadata_once(); });
}

// ---- increment A5: the full read ------------------------------------------ //

namespace {

// _LAYOUT: the modern native INFO / CONTROL / STATUS geometry and SIZES' three packet sizes, in
// kPowerGetCommands order (A618 INFO, A619 STATUS, A61A CONTROL; the fourth GET is not in it).
PowerLayout cp_arbitration_layout() {
    const CpSizes& sizes = *cp_size_group(static_cast<std::uint32_t>(CpSizeGroup::sizes));
    PowerLayout layout{};
    layout.sizes[0] = sizes[kCpIdxNativeInfo];
    layout.sizes[1] = sizes[kCpIdxNativeStatus];
    layout.sizes[2] = sizes[kCpIdxNativeControl];
    layout.has_commands = false;  // no "commands" key: _geometry's default mapping
    layout.info_base = kCpNativeInfoBase;
    layout.info_stride = kCpNativeInfoStride;
    layout.status_base = kCpNativeStatusBase;
    layout.status_stride = kCpNativeStatusStride;
    layout.control_base = kCpNativeControlBase;
    layout.control_stride = kCpNativeControlStride;
    return layout;  // no "status_mask_word" key: _geometry's default
}

// _words(raw): the little-endian u32 words of a packet (every packet here is whole words).
std::vector<std::uint32_t> words_of(const std::vector<std::uint8_t>& raw) {
    std::vector<std::uint32_t> words(raw.size() / 4);
    for (std::size_t i = 0; i < words.size(); ++i) words[i] = u32_at(raw, 4 * i);
    return words;
}

ArbitrationWords arbitration_words(const std::vector<std::uint32_t>& words) {
    ArbitrationWords w;
    w.present = true;
    w.data = words.data();
    w.count = words.size();
    return w;
}

CpBinding cp_binding(const CpMetadata& m) {
    CpBinding b;
    b.layout = m.layout;
    b.native_layout = m.native_layout;
    if (m.native_info_stride) b.native_info_stride = std::int64_t{*m.native_info_stride};
    b.client_index = std::int64_t{m.client_index};
    b.native_index = std::int64_t{m.native_index};
    return b;
}

}  // namespace

CpRead<CpObservation> ClientPowerController::once() {
    CpRead<CpObservation> out;
    CpRead<CpMetadata> captured = metadata_once();
    if (!captured.value) {
        out.error = std::move(captured.error);
        return out;
    }
    CpObservation observation;
    observation.metadata = std::move(*captured.value);
    const CpMetadata& metadata = observation.metadata;
    const std::vector<std::uint8_t>& info = metadata.info;
    const std::vector<std::uint8_t>& native_info = metadata.native_info;
    const std::uint8_t* source = metadata.role_info.data() + 4;  // role INFO [4:], at least 20 bytes
    const std::size_t index = metadata.client_index, native_index = metadata.native_index;
    std::size_t base, info_stride, control_base;
    if (state_.rm26_records)
        base = 4, info_stride = state_.rm26_extended ? 28 : 20, control_base = 4;
    else if (state_.legacy)
        base = 8, info_stride = kCpInfoStride, control_base = 8;
    else
        base = kCpInfoBase, info_stride = kCpInfoStride, control_base = kCpControlBase;
    const CpGeometry geometry = native_geometry(cp_binding(metadata));
    const std::size_t native_base = static_cast<std::size_t>(geometry.control_base);
    const std::size_t native_stride = static_cast<std::size_t>(geometry.control_stride);
    const std::uint8_t* row = info.data() + base + index * info_stride;  // inside INFO: _metadata_once read it

    // The CONTROL request: the selected client's mask bit and its row type and client.
    const std::size_t cp = control_base + index * kCpControlStride;
    std::vector<std::uint8_t> request(sizes()[kCpIdxControl], 0);
    // Every group's mask, rows and CONTROL slots lie inside its packets (the row capacity bounds
    // index), so Python's slice growth, OverflowError and struct.error cannot arise; refused as a
    // geometry error should a table ever break that.
    if (cp + 12 > request.size() || base > request.size() || base > info.size() ||
        (!state_.rm26_records && index / 8 >= base - 4)) {
        out.error = failure(RmErrorKind::malformed, "Client-power command or packet geometry is not understood");
        return out;
    }
    if (state_.rm26_records) {
        const std::uint32_t mask = std::uint32_t{1} << index;  // index < 32 for the compact rows
        std::memcpy(request.data(), &mask, 4);
    } else {
        std::memcpy(request.data(), info.data(), 4);  // request[:base] = info[:base], then [4:base]
        request[4 + index / 8] = static_cast<std::uint8_t>(1u << (index % 8));
    }
    request[cp] = row[0];
    request[cp + 1] = row[1];
    request[cp + 8] = static_cast<std::uint8_t>(kCpClient);
    CpExchangeResult control = get(kCpControl, request);
    if (!control.response) {
        out.error = std::move(control.error);
        return out;
    }
    const std::vector<std::uint8_t>& c = *control.response;
    const bool group_differs = state_.rm26_records
                                   ? std::memcmp(c.data(), request.data(), 4) != 0
                                   : c[0] != info[0] || std::memcmp(c.data() + 4, request.data() + 4, base - 4) != 0;
    if (group_differs) {
        out.error = failure(RmErrorKind::malformed, "Client CONTROL group or selected mask differs");
        return out;
    }
    if (c[cp] != row[0] || c[cp + 1] != row[1] || c[cp + 8] != kCpClient) {
        out.error = failure(RmErrorKind::malformed, "Client CONTROL changed the selected row type or client");
        return out;
    }
    const std::uint32_t wanted = u32_at(c, cp + 4);
    if (wanted == kCpMissing) {
        out.error = failure(RmErrorKind::unsupported, "The selected FE client has no existing restorable request");
        return out;
    }
    if (wanted == 0) {
        out.error = failure(RmErrorKind::unsupported, "A zero FE input has uncharacterized removal semantics");
        return out;
    }

    // The NATIVE_CONTROL request: INFO's occupancy mask, at 0 for the short RM26 packet, else at 16.
    const std::uint32_t native_mask = u32_at(native_info, 4);
    const bool short_rm26 = state_.rm26_records && !state_.rm26_extended;
    std::vector<std::uint8_t> native_request(sizes()[kCpIdxNativeControl], 0);
    const std::size_t np = native_base + native_index * native_stride;
    if (native_request.size() < 20 || np + 8 > native_request.size()) {  // unreachable, as above
        out.error = failure(RmErrorKind::malformed, "Client-power command or packet geometry is not understood");
        return out;
    }
    std::memcpy(native_request.data() + (short_rm26 ? 0 : 16), &native_mask, 4);
    CpExchangeResult native = get(kCpNativeControl, native_request);
    if (!native.response) {
        out.error = std::move(native.error);
        return out;
    }
    const std::vector<std::uint8_t>& n = *native.response;
    const bool header_differs =
        short_rm26 ? u32_at(n, 0) != native_mask
                   : u32_at(n, 0) != 0 || u32_at(n, 4) != 0 || u32_at(n, 8) != 0 || u32_at(n, 12) != 255 ||
                         u32_at(n, 16) != native_mask;
    if (header_differs) {
        out.error = failure(RmErrorKind::malformed, "Native CONTROL header or inventory differs from INFO");
        return out;
    }
    if (u32_at(n, np) != source[4]) {
        out.error = failure(RmErrorKind::malformed, "Bound native policy CONTROL type differs from INFO");
        return out;
    }
    if (u32_at(n, np + 4) != wanted) {
        out.error = failure(RmErrorKind::unstable, "Client and native current requests disagree");
        return out;
    }
    TargetResult again = binding();
    if (!again.target) {
        out.error = again.error;
        return out;
    }
    if (!same_identity(*again.target, metadata.identity)) {
        out.error = failure(RmErrorKind::identity, "Client-power target changed during capture");
        return out;
    }
    observation.requested_mw = wanted;
    observation.control = std::move(*control.response);
    observation.native_control = std::move(*native.response);
    out.value = std::move(observation);
    return out;
}

CpArbitration ClientPowerController::arbitration(const CpObservation& observation) {
    CpArbitration out;
    // The early return that also makes SIZES' fixed STATUS allocation below the current table's.
    if (state_.legacy || state_.rm26_records) {
        out.error = kCpArbitrationUnavailable;
        return out;
    }
    const std::vector<std::uint8_t>& info = observation.metadata.native_info;
    const CpSizes& modern = *cp_size_group(static_cast<std::uint32_t>(CpSizeGroup::sizes));
    std::vector<std::uint8_t> request(modern[kCpIdxNativeStatus], 0);
    std::memcpy(request.data() + 4, info.data() + 4, 4);  // request[4:8] = info[4:8]
    static const PowerLayout layout = cp_arbitration_layout();
    std::string error;
    for (int attempt = 0; attempt < 2; ++attempt) {
        CpExchangeResult status = get(kCpNativeStatus, request);
        if (status.response) {
            const std::vector<std::uint32_t> info_words = words_of(info);
            const std::vector<std::uint32_t> control_words = words_of(observation.native_control);
            const std::vector<std::uint32_t> status_words = words_of(*status.response);
            ArbitrationReport decoded =
                decode_power_arbitration(&layout, arbitration_words(info_words), arbitration_words(control_words),
                                         arbitration_words(status_words));
            if (!decoded.available) {
                out.error = std::move(decoded.reason);
                return out;
            }
            out.report = std::move(decoded);
            return out;
        }
        // An identity change invalidates the binding, even when it was discovered during optional
        // telemetry. A bare ValueError (kind invalid) is not a ClientPowerError: it passes the
        // `except` uncaught. Other STATUS failures remain diagnostic.
        if (status.error.kind == RmErrorKind::identity || status.error.kind == RmErrorKind::invalid) {
            out.raised = std::move(status.error);
            return out;
        }
        error = status.error.message;
        if (status.error.kind != RmErrorKind::transient) break;
        reset_transport();
        if (attempt == 1) break;
    }
    out.error = std::move(error);
    return out;
}

CpRead<CpReading> ClientPowerController::read_locked() {
    CpRead<CpReading> out;
    CpRead<CpObservation> state = stable_locked<CpObservation>([this] { return once(); });
    if (!state.value) {
        out.error = std::move(state.error);
        return out;
    }
    CpArbitration diagnostics = arbitration(*state.value);
    if (diagnostics.raised) {
        out.error = std::move(*diagnostics.raised);
        return out;
    }
    CpReading reading;
    reading.observation = std::move(*state.value);
    if (diagnostics.report)
        reading.arbitration = std::move(diagnostics.report);
    else
        reading.arbitration_error = std::move(diagnostics.error);
    out.value = std::move(reading);
    return out;
}

CpRead<CpReading> ClientPowerController::read() { return read_locked(); }

CpRead<CpReading> ClientPowerController::live_read(StaticCapabilities& statics) {
    CpRead<CpReading> r;
    try {
        r = read();
    } catch (const std::exception& e) {
        // Python's `except Exception` around a non-ClientPowerError: the read failed, kind transient.
        r = CpRead<CpReading>{};
        r.error = failure(RmErrorKind::transient, std::string("Client-power controller failed: ") + e.what());
    } catch (...) {
        r = CpRead<CpReading>{};
        r.error = failure(RmErrorKind::transient, "Client-power controller failed: unknown exception");
    }
    if (!r.value) {
        // `exc.kind if isinstance(exc, ClientPowerError) else "transient"`: the bare ValueError (kind
        // invalid) is not a ClientPowerError.
        const RmErrorKind kind = (r.error.kind == RmErrorKind::invalid || r.error.kind == RmErrorKind::none)
                                     ? RmErrorKind::transient
                                     : r.error.kind;
        live_.error = r.error.message;
        live_.error_kind = kind;
        if (kind != RmErrorKind::transient) live_.last.reset();
        return r;
    }
    live_.last = *r.value;
    live_.error.reset();
    live_.error_kind.reset();
    const CpMetadata& m = r.value->observation.metadata;
    live_.board_policy = m.native_index;
    if (!statics.pl_min_mw) statics.pl_min_mw = m.minimum_mw;
    if (!statics.pl_def_mw) statics.pl_def_mw = m.rated_mw;
    if (!statics.pl_max_mw) statics.pl_max_mw = m.maximum_mw;
    return r;
}

void ClientPowerController::refresh_live() {
    reset();  // _live_power_controller = None: the next read builds a fresh controller
    live_.last.reset();
    live_.error.reset();
    live_.error_kind.reset();
}

// ---- increment A7: set_request and _rollback -------------------------------------------------- //

namespace {

// A Python exception as _rollback's `f"{type(exc).__name__}: {exc}"` and set_request's
// `getattr(exc, "kind", "transient")` see it: its type name, its kind (a ClientPowerError's own;
// RmErrorKind::invalid for every other type, which has no .kind), its text and rm_status.
struct Raised {
    std::string type;
    RmError error;
};

// A ClientPowerError, or the bare ValueError the core reports as kind invalid.
Raised raised(const RmError& e) {
    return Raised{e.kind == RmErrorKind::invalid ? "ValueError" : "ClientPowerError", e};
}

Raised raised(std::string type, std::string message) {
    return Raised{std::move(type), failure(RmErrorKind::invalid, std::move(message))};
}

// The exception the A2 helpers raise on a malformed input (unreachable here: every value comes from
// a reading this controller checked). str(KeyError(k)) is repr(k).
Raised raised(const CpError& e) {
    switch (e.kind) {
    case CpErrorKind::struct_error: return raised("error", e.message);  // struct.error.__name__
    case CpErrorKind::index_error: return raised("IndexError", e.message);
    case CpErrorKind::overflow: return raised("OverflowError", e.message);
    case CpErrorKind::key_error: return raised("KeyError", "'" + e.message + "'");
    case CpErrorKind::binding: return raised("AttributeError", e.message);
    default: return raised("ValueError", e.message);
    }
}

// Python's `except BaseException` around a C++ exception: only allocation can throw here, which
// Python reports as a MemoryError (whose text is empty).
Raised raised_exception() {
    try {
        throw;
    } catch (const std::bad_alloc&) {
        return raised("MemoryError", "");
    } catch (const std::exception& e) {
        return raised("RuntimeError", std::string("Client-power controller failed: ") + e.what());
    } catch (...) {
        return raised("RuntimeError", "Client-power controller failed: unknown exception");
    }
}

std::string entry(const Raised& r) { return r.type + ": " + r.error.message; }

// The binding dict's equality: every key _metadata_once writes.
bool same_binding(const CpMetadata& a, const CpMetadata& b) {
    return a.client_index == b.client_index && a.native_index == b.native_index && a.minimum_mw == b.minimum_mw &&
           a.rated_mw == b.rated_mw && a.maximum_mw == b.maximum_mw && a.layout == b.layout &&
           a.native_layout == b.native_layout && a.native_info_stride == b.native_info_stride;
}

// _matches(expected, observed): expected is a _STATE_KEYS dict (_state(before) or _project's), so
// it has no extra keys; observed is a reading, whose _state() the helper compares.
std::optional<bool> observation_matches(const CpObservation& expected, const CpObservation& observed, CpError& error) {
    const CpMetadata& e = expected.metadata;
    const CpMetadata& o = observed.metadata;
    CpMatchInput in;
    in.binding = cp_binding(e);
    const auto compare = [](bool equal) { return equal ? CpCompare::equal : CpCompare::different; };
    in.compare[kCpKeySchema] = CpCompare::equal;       // 1 in every reading
    in.compare[kCpKeyStableReads] = CpCompare::equal;  // 2 in every reading
    in.compare[kCpKeyIdentity] = compare(same_identity(e.identity, o.identity));
    in.compare[kCpKeyBinding] = compare(same_binding(e, o));
    in.compare[kCpKeyRequestedMw] = compare(expected.requested_mw == observed.requested_mw);
    for (std::size_t key = kCpFirstPayloadKey; key < kCpStateKeyCount; ++key) in.compare[key] = CpCompare::bytes;
    const std::vector<std::uint8_t>* payloads[2][kCpPayloadCount] = {
        {&e.info, &expected.control, &e.role_info, &e.native_info, &expected.native_control},
        {&o.info, &observed.control, &o.role_info, &o.native_info, &observed.native_control}};
    for (std::size_t i = 0; i < kCpPayloadCount; ++i) {
        in.expected[i] = *payloads[0][i];
        in.observed[i] = *payloads[1][i];
    }
    return matches(in, error);
}

// Python's `raw[at:at + 4]` of a bytes object: clipped to the payload.
std::vector<std::uint8_t> slice4(const std::vector<std::uint8_t>& raw, std::int64_t at) {
    const std::int64_t n = static_cast<std::int64_t>(raw.size());
    const std::int64_t begin = std::clamp<std::int64_t>(at, 0, n), end = std::clamp<std::int64_t>(at + 4, 0, n);
    using Diff = std::vector<std::uint8_t>::difference_type;  // clamped to raw.size(): fits on every target
    return std::vector<std::uint8_t>(raw.begin() + static_cast<Diff>(begin),
                                     raw.begin() + static_cast<Diff>(std::max(begin, end)));
}

constexpr const char* kRestoredVerified = "Original request restored and verified";
constexpr const char* kRestorationUnverified = "Original request restoration was not verified";

}  // namespace

CpRollback ClientPowerController::rollback(const CpReading& before, const CpPeerWriter& peer) {
    CpRollback result;
    std::vector<std::string> errors;
    const CpObservation& b = before.observation;
    bool dispatched = false;
    try {
        CpExchangeResult r =
            exchange(kCpSet, b.control.data(), b.control.size(), true, b.control.size(), CpCaller{}, dispatched);
        if (r.too_small)  // unreachable: the room is the payload's size, which the geometry check matched
            errors.push_back(entry(raised(failure(RmErrorKind::malformed,
                                                  "Client-power command or packet geometry is not understood"))));
        else if (!r.response)
            errors.push_back(entry(raised(r.error)));
    } catch (...) {
        errors.push_back(entry(raised_exception()));
    }
    result.attempted = dispatched;
    if (dispatched) {
        // Python's try: the first failure ends it as one entry; a readback assigned before it stays.
        std::optional<Raised> failed;
        try {
            [&] {
                CpRead<CpReading> first = read_locked();
                if (!first.value) return void(failed = raised(first.error));
                result.readback = std::move(*first.value);
                CpError helper;
                const bool coupled = coupled_requests(b.metadata.role_info, helper);
                if (helper) return void(failed = raised(helper));
                if (coupled) {
                    const std::optional<bool> same = observation_matches(b, result.readback->observation, helper);
                    if (!same) return void(failed = raised(helper));
                    if (*same) {
                        // Returning the board request can recalculate peer requests. Restore only
                        // peers that differ from the captured baseline.
                        const std::vector<std::uint8_t>& original = b.native_control;
                        const std::vector<std::uint8_t> current = result.readback->observation.native_control;
                        // _u32(native_info, 4): _matches above read the same word, so it is there.
                        const std::uint32_t mask = u32_at(b.metadata.native_info, 4);
                        const std::uint32_t selected = b.metadata.native_index;
                        for (std::uint32_t index = 0; index < 32; ++index) {
                            const CpGeometry g = native_geometry(cp_binding(b.metadata));
                            const std::int64_t at =
                                g.control_base + static_cast<std::int64_t>(index) * g.control_stride + 4;
                            if (index == selected || !(mask >> index & 1u) || slice4(original, at) == slice4(current, at))
                                continue;
                            // The identity is checked before EACH peer write, so a target change
                            // between two writes stops the second.
                            TargetResult identity = binding();
                            if (!identity.target) return void(failed = raised(identity.error));
                            if (!same_identity(*identity.target, b.metadata.identity))
                                return void(failed = raised(
                                                failure(RmErrorKind::identity, "GPU binding changed before peer restoration")));
                            // Two differing slices of equal-length payloads lie inside both, so the
                            // u32 Python unpacks is there.
                            const std::uint32_t value = u32_at(original, static_cast<std::size_t>(at));
                            if (!peer.write) return void(failed = raised("AttributeError", "no policy writer"));
                            const CpPeerWrite w = peer.write(peer.context, index, value);
                            if (!w.in_bounds) return void(failed = raised("IndexError", "list index out of range"));
                            if (!w.ok)
                                return void(failed = raised(failure(RmErrorKind::readback,
                                                                    "Policy " + std::to_string(index) +
                                                                        " restoration failed: " + w.message)));
                        }
                        CpRead<CpReading> again = read_locked();
                        if (!again.value) return void(failed = raised(again.error));
                        result.readback = std::move(*again.value);
                    }
                }
                if (result.readback->observation != b)
                    return void(failed = raised(failure(RmErrorKind::readback,
                                                        "Original client-power definitions and requests were not restored")));
            }();
        } catch (...) {
            failed = raised_exception();
        }
        if (failed)
            errors.push_back(entry(*failed));
        else
            result.verified = errors.empty();
    }
    if (!errors.empty()) {
        std::string text;
        for (std::size_t i = 0; i < errors.size(); ++i) text += (i ? "; " : "") + errors[i];
        result.error = std::move(text);
    }
    return result;
}

CpSetResult ClientPowerController::set_request(std::int64_t mw, const CpPeerWriter& peer) {
    CpSetResult out;
    last_rollback_error_.clear();
    // `type(mw) is not int or not 0 < mw < MISSING`, before the lock (the caller's) and any read.
    if (!(0 < mw && mw < std::int64_t{kCpMissing})) {
        out.error = failure(RmErrorKind::invalid, kCpInvalidRequest);
        return out;
    }
    CpRead<CpReading> before = read_locked();
    if (!before.value) {  // raised as it is: no SET was dispatched
        out.error = std::move(before.error);
        return out;
    }
    const CpObservation& b = before.value->observation;
    // expected = _project(before, mw): the state with requested_mw and both request words replaced.
    CpError helper;
    std::optional<CpProjection> projected =
        project(cp_binding(b.metadata), CpMwKind::integer, mw, b.control, b.native_control, helper);
    if (!projected) {
        // Unreachable (the reading's binding and payloads were checked by _once). Python raises the
        // helper's exception unchanged; it is no ClientPowerError, so kind invalid here.
        out.error = raised(helper).error;
        return out;
    }
    CpObservation expected = b;
    expected.requested_mw = static_cast<std::uint32_t>(mw);  // 0 < mw < 2**32 - 1
    expected.control = std::move(projected->control);
    expected.native_control = std::move(projected->native_control);

    bool dispatched = false;  // write.dispatched
    std::optional<Raised> failed;
    try {
        CpExchangeResult r = exchange(kCpSet, expected.control.data(), expected.control.size(), true,
                                      expected.control.size(), CpCaller{}, dispatched);
        if (r.too_small) {  // unreachable, as in rollback
            failed = raised(failure(RmErrorKind::malformed, "Client-power command or packet geometry is not understood"));
        } else if (!r.response) {
            failed = raised(r.error);
        } else {
            CpRead<CpReading> after = read_locked();
            if (!after.value) {
                failed = raised(after.error);
            } else {
                const std::optional<bool> same = observation_matches(expected, after.value->observation, helper);
                if (!same) {
                    failed = raised(helper);
                } else if (!*same) {
                    failed = raised(failure(RmErrorKind::readback,
                                            "Client-power request or preserved definitions did not read back exactly"));
                } else {
                    out.before = std::move(*before.value);
                    out.after = std::move(*after.value);
                    return out;
                }
            }
        }
    } catch (...) {
        if (!dispatched) throw;  // Python's `if not write.dispatched: raise`
        failed = raised_exception();
    }
    if (!dispatched) {  // `raise`: the exception unchanged, its rm_status included, no rollback
        out.error = std::move(failed->error);
        return out;
    }
    // The transport cache is forgotten before the compensation; its own exchange captures it again.
    reset_transport();
    CpRollback rollback_result = rollback(*before.value, peer);
    // getattr(exc, "kind", "transient"): only a ClientPowerError has a kind. ClientPowerError(kind,
    // message, rollback) passes no rm_status, whatever the inner exception carried.
    const RmErrorKind kind = failed->type == "ClientPowerError" ? failed->error.kind : RmErrorKind::transient;
    out.error = failure(kind, failed->error.message + ". " +
                                  (rollback_result.verified ? kRestoredVerified : kRestorationUnverified));
    if (rollback_result.error) last_rollback_error_ = *rollback_result.error;
    out.rollback = std::move(rollback_result);
    return out;
}

}  // namespace druta::backend
