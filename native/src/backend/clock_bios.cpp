// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/clock_bios.h"

#include <cstring>

#include "backend/client_power_controller.h"  // GuardedBuffer (_guarded / _guards)
#include "backend/nvapi.h"

namespace druta::backend {
namespace {

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);
constexpr std::uint32_t ClkDomCtlGet = 0xF58938F5;

ClockBiosFailure clock_error(std::string message) {
    ClockBiosFailure f;
    f.error.message = std::move(message);
    return f;
}

ClockBiosFailure perf_error(RmErrorKind kind, std::string message) {
    ClockBiosFailure f;
    f.perf_error = true;
    f.error.kind = kind;
    f.error.message = std::move(message);
    return f;
}

std::uint32_t le_word(const std::uint8_t* p) {
    return static_cast<std::uint32_t>(p[0]) | static_cast<std::uint32_t>(p[1]) << 8 |
           static_cast<std::uint32_t>(p[2]) << 16 | static_cast<std::uint32_t>(p[3]) << 24;
}

// Python's Py_ISSPACE: what bytes.fromhex skips between byte pairs.
bool py_space(char c) { return c == ' ' || (c >= '\t' && c <= '\r'); }

int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

}  // namespace

ClockShapeResult clock_control_shape(const std::uint8_t* payload, std::size_t size, std::uint64_t mask) {
    ClockShapeResult out;
    // _integer(value["mask"], 1, 0xFFFFFFFF, ...)
    if (mask < 1 || mask > 0xFFFFFFFFu) {
        out.error = "User-clock request inventory is invalid";
        return out;
    }
    // _hex's geometry: the payload is exactly the declared parameter bytes.
    if (!payload || size != kClockControlSize) {
        out.error = "Clock readback has invalid packet geometry";
        return out;
    }
    if (le_word(payload) != kClockControlVersion || le_word(payload + 8) != mask) {
        out.error = "User-clock requests changed version or inventory";
        return out;
    }
    ClockRequests requests;
    for (std::uint32_t index = 0; index < kClockControlCapacity; ++index) {
        if (!(mask & (std::uint64_t{1} << index))) continue;
        const std::uint8_t* row = payload + kClockControlHeader + index * kClockControlRow;
        // NVAPI preserves the native ordinal; type16 is mapped to public 15 and its native kHz
        // request copied to public row+0x114 without scaling.
        if (le_word(row) == kClockControlRequestType) {
            const std::uint32_t raw = le_word(row + kClockControlRequestOffset);
            std::int32_t value;
            std::memcpy(&value, &raw, sizeof value);
            requests.present |= 1u << index;
            requests.khz[index] = value;
        }
    }
    out.requests = requests;
    return out;
}

ClockShapeResult clock_control_shape_hex(const char* text, std::size_t length, std::uint64_t mask) {
    ClockShapeResult out;
    if (mask < 1 || mask > 0xFFFFFFFFu) {
        out.error = "User-clock request inventory is invalid";
        return out;
    }
    // _hex(value, length): a str of exactly 2 * length code points ...
    if (!text || length != 2u * kClockControlSize) {
        out.error = "Clock readback has invalid packet geometry";
        return out;
    }
    // ... that bytes.fromhex accepts (ASCII whitespace skipped between pairs) ...
    std::vector<std::uint8_t> raw;
    raw.reserve(kClockControlSize);
    std::size_t i = 0;
    while (i < length) {
        if (py_space(text[i])) {
            ++i;
            continue;
        }
        const int high = hex_digit(text[i]);
        const int low = i + 1 < length ? hex_digit(text[i + 1]) : -1;
        if (high < 0 || low < 0) {
            out.error = "Clock readback is not hexadecimal";
            return out;
        }
        raw.push_back(static_cast<std::uint8_t>(high << 4 | low));
        i += 2;
    }
    // ... and that is canonical: exactly `length` bytes whose lower-case hex is the text itself.
    bool canonical = raw.size() == kClockControlSize;
    for (std::size_t k = 0; canonical && k < length; ++k) {
        const char c = text[k];
        canonical = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    }
    if (!canonical) {
        out.error = "Clock readback needs canonical bounded hexadecimal";
        return out;
    }
    return clock_control_shape(raw.data(), raw.size(), mask);
}

std::optional<ClockIdentity> ClockBios::binding(ClockBiosFailure& failure) {
    TargetResult bound = binding_.bind();
    if (!bound.target) {
        failure.perf_error = true;
        failure.error = bound.error;
        return std::nullopt;
    }
    const Target& t = *bound.target;
    // _identity: the binding always returns a str slot and ints (the core's types), so only the
    // ranges remain. bind() already required a parsed slot in PCI domain 0 and a probe architecture.
    const std::optional<PciSlot> address = parse_slot(t.slot);
    if (!address || address->domain != 0 || address->bus > 255 || address->device > 31 || address->function > 7) {
        failure = clock_error("Clock range has an invalid PCI identity");
        return std::nullopt;
    }
    if (t.device_id > 0xFFFF) {
        failure = clock_error("Clock range has an invalid adapter identity");
        return std::nullopt;
    }
    if (!probe_architecture(t.architecture)) {
        failure = clock_error("Clock INFO needs a supported modern architecture");
        return std::nullopt;
    }
    return ClockIdentity{t.slot, t.architecture, t.device_id, t.subsystem_id};
}

ClockControlResult ClockBios::control_once(const std::optional<ClockIdentity>& identity) {
    ClockControlResult out;
    Nvapi& api = gpu_.nvapi;
    auto getter = api.get<HandlePtrFn>(ClkDomCtlGet);
    if (!getter) {
        out.failure = clock_error("User-clock CONTROL GET is unavailable");
        return out;
    }
    // gpu.clkdom_domains(): unique ints in 0..31 by construction, so only emptiness can fail here.
    const std::vector<std::uint32_t>& domains = clkdom_.domains();
    if (domains.empty()) {
        out.failure = clock_error("User-clock request layout or inventory is unavailable");
        return out;
    }
    std::uint32_t mask = 0;
    for (std::uint32_t d : domains) {
        if (d >= kClockControlCapacity) {
            out.failure = clock_error("User-clock inventory is invalid");
            return out;
        }
        mask |= 1u << d;
    }
    GuardedBuffer packet(kClockControlSize);
    packet.set_word(0, kClockControlVersion);
    packet.set_word(2, mask);
    const int status = getter(api.gpu, packet.data());
    if (!packet.guards_intact()) {
        out.failure = perf_error(RmErrorKind::malformed, GuardedBuffer::kOverrun);
        return out;
    }
    ClockBiosFailure failure;
    const std::optional<ClockIdentity> now = binding(failure);
    if (!now) {
        out.failure = failure;
        return out;
    }
    if (!identity || now->slot != identity->slot || now->architecture != identity->architecture ||
        now->device_id != identity->device_id || now->subsystem_id != identity->subsystem_id) {
        out.failure = perf_error(RmErrorKind::identity, "User-clock request target changed");
        return out;
    }
    if (status != 0) {
        out.failure = clock_error("User-clock CONTROL GET failed: " + std::to_string(status));
        return out;
    }
    const ClockShapeResult shape = clock_control_shape(packet.data(), packet.size(), mask);
    if (!shape.requests) {
        out.failure = clock_error(shape.error);
        return out;
    }
    out.mask = mask;
    out.payload.assign(packet.data(), packet.data() + packet.size());
    return out;
}

}  // namespace druta::backend
