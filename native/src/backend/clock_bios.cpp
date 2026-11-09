// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/clock_bios.h"

#include <cstring>

#include "backend/client_power_controller.h"  // GuardedBuffer (_guarded / _guards)
#include "backend/gdi.h"
#include "backend/nvapi.h"
#include "core/mutex.h"

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

ClockDecodeResult clock_info_decode(const std::uint8_t* raw, std::size_t size) {
    ClockDecodeResult out;
    if (!raw || size != kClockInfoSize) {
        out.error = "Clock INFO geometry is not understood";
        return out;
    }
    ClockInfoRows rows;
    rows.present = le_word(raw + 4);
    for (std::uint32_t index = 0; index < kClockInfoCapacity; ++index) {
        if (!(rows.present & (1u << index))) continue;
        const std::uint8_t* row = raw + kClockInfoHeader + index * kClockInfoRow;
        ClockInfoRow& r = rows.rows[index];
        r.type = row[0];
        r.base_type = row[1];
        r.api_domain_mask = le_word(row + 4);
        r.typed = r.type == 16 && r.base_type == 16;
        if (r.typed) {
            const auto lo = static_cast<std::uint16_t>(row[0x26] | row[0x27] << 8);
            const auto hi = static_cast<std::uint16_t>(row[0x28] | row[0x29] << 8);
            std::memcpy(&r.minimum_mhz, &lo, sizeof lo);
            std::memcpy(&r.maximum_mhz, &hi, sizeof hi);
        }
    }
    out.rows = rows;
    return out;
}

std::optional<ClockIdentity> ClockBios::binding(Reader reader, ClockBiosFailure& failure) {
    TargetResult bound = reader.perf.bind();
    if (bound.target) reader.held = true;  // TargetBinding has recorded its sticky identity
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

ClockControlResult ClockBios::control_once(Reader reader, ClockIdentityDiffers differs, void* context) {
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
    const std::optional<ClockIdentity> now = binding(reader, failure);
    if (!now) {
        out.failure = failure;
        return out;
    }
    // `_binding(reader) != identity`: the caller compares with its own semantics.
    if (!differs || differs(context, *now) != 0) {
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

ClockInfoResult ClockBios::info_once(Reader reader) {
    ClockInfoResult out;
    ClockBiosFailure failure;
    const std::optional<ClockIdentity> identity = binding(reader, failure);
    if (!identity) {
        out.failure = failure;
        return out;
    }
    if (!gpu_.nvapi.interface(kClockInfoExport)) {
        out.failure = perf_error(RmErrorKind::unsupported, "Clock INFO GET export is unavailable");
        return out;
    }
    // reader._transport(): kinded failures only (transient or unsupported), as Python's.
    PerfTransportResult t = reader.perf.transport();
    if (!t.transport) {
        out.failure = perf_error(t.error.kind, t.error.message);
        return out;
    }
    const CapturedTransport& transport = *t.transport;
    // The envelope words. Python also requires every escape field to be an int in 0..0xFFFFFFFF:
    // EscapeFields holds std::uint32_t fields, so that holds by construction. transport() already
    // required exactly 17 header words.
    for (const ClockEnvelopeWord& w : kClockInfoEnvelope) {
        if (transport.header[w.index] != w.value) {
            out.failure = perf_error(RmErrorKind::malformed, "Clock INFO needs the understood paired envelope");
            return out;
        }
    }
    GuardedBuffer packet(kClockInfoPrologue + kClockInfoSize);
    for (std::uint32_t i = 0; i < transport.header_count && i < packet.words(); ++i)
        packet.set_word(i, transport.header[i]);
    packet.set_word(2, kClockInfoPrologue + kClockInfoSize);
    packet.set_word(14, kClockInfoCommand);
    packet.set_word(15, kClockInfoSize);
    packet.set_word(16, 0);
    packet.set_word(5, 0);
    packet.set_word(11, 0);
    std::uint32_t expected[16];
    for (std::uint32_t i = 0; i < 16; ++i) expected[i] = packet.word(i);

    const EscapeResult escape = escape_raw(gpu_, packet.data(), static_cast<std::uint32_t>(packet.size()),
                                           transport.fields);
    // `_binding(reader) != identity` comes before the guards here (after them in _control_once).
    const std::optional<ClockIdentity> now = binding(reader, failure);
    if (!now) {
        out.failure = failure;
        return out;
    }
    if (*now != *identity) {
        out.failure = perf_error(RmErrorKind::identity, "Clock INFO target changed during GET");
        return out;
    }
    if (!packet.guards_intact()) {
        out.failure = perf_error(RmErrorKind::malformed, GuardedBuffer::kOverrun);
        return out;
    }
    // _legacy_clk_escape returns None when no escape was issued: Python prints "None". Unlike the PERF
    // _get, the transport cache is not dropped here; read() drops it after its first attempt.
    if (escape.outcome != EscapeOutcome::issued || escape.ntstatus != 0) {
        const bool issued = escape.outcome == EscapeOutcome::issued;
        out.failure = perf_error(RmErrorKind::transient,
                                 "Clock INFO transport failed: " +
                                     (issued ? std::to_string(escape.ntstatus) : std::string("None")));
        return out;
    }
    bool changed = packet.word(5) > 1;
    for (std::uint32_t i = 0; i < 16; ++i)
        if (i != 5 && i != 11 && packet.word(i) != expected[i]) changed = true;
    if (changed) {
        out.failure = perf_error(RmErrorKind::malformed, "Clock INFO changed its envelope or geometry");
        return out;
    }
    if (packet.word(11) != 1) {
        out.failure = perf_error(RmErrorKind::transient, "Clock INFO completion is unavailable");
        return out;
    }
    const std::uint32_t rm = packet.word(16);
    if (rm != 0) {
        out.failure = perf_error(rm == 0x1F || rm == 0x56 ? RmErrorKind::unsupported : RmErrorKind::transient,
                                 "Clock INFO RM status " + std::to_string(rm));
        return out;
    }
    out.identity = identity;
    out.info.assign(packet.data() + kClockInfoPrologue, packet.data() + packet.size());
    return out;
}

namespace {

// `_binding(reader) != identity` inside read(): both are the core's own plain identity dicts.
std::int32_t identity_differs(void* context, const ClockIdentity& bound) {
    return bound != *static_cast<const ClockIdentity*>(context) ? 1 : 0;
}

}  // namespace

// read(gpu). Its _shape(observation) call re-validates what the core itself produced and cannot
// fail here, so it is not repeated:
//   - the _LAYOUT words and stable_reads == 2 are constants of this function;
//   - _canonical's key set is the fixed struct; _identity(identity) re-runs the checks binding()
//     already applied to this very identity (the slot parse and PCI ranges, the 16-bit device id,
//     the probe architecture; the 32-bit ids are std::uint32_t), so it returns the same identity;
//   - _decode(_hex(info_hex, SIZE)): info is exactly SIZE bytes, hex() is canonical;
//   - user_control is None (writable=False allows it) or the dict _control_once built after its own
//     _control_shape over the same bytes and mask, so _control_shape passes again;
//   - rows is _decode of the same bytes, so _same_state(rows, _decode(...)) holds.
// Python's `except Exception` branch is the ClockBiosError path here (a failure with perf_error
// clear): its message becomes `last` and the attempt is retried, as in Python.
ClockReadResult ClockBios::read() {
    ClockReadResult out;
    PerfDiagnostics fresh(gpu_, telemetry_, rm_);  // PerfDiagnosticsReader(gpu): one per read() call
    bool fresh_held = false;
    const Reader reader{fresh, fresh_held};
    std::string last;
    std::optional<ClockObservation> partial;
    for (int attempt = 0; attempt < 2; ++attempt) {
        ClockBiosFailure failure;
        bool failed = false;
        std::optional<ClockObservation> observation;
        bool stable = false;
        {
            LockGuard guard(gpu_.lock());
            struct Sample {
                ClockIdentity identity;
                std::vector<std::uint8_t> info;
                std::optional<ClockControlResult> control;
                std::string error;  // str(exc) of a ClockBiosError from _control_once
            };
            Sample samples[2];
            for (Sample& sample : samples) {
                ClockInfoResult info = info_once(reader);
                if (!info.ok()) {
                    failure = info.failure;
                    failed = true;
                    break;
                }
                sample.identity = *info.identity;
                sample.info = std::move(info.info);
                ClockControlResult control = control_once(reader, identity_differs, &sample.identity);
                if (control.ok()) {
                    sample.control = std::move(control);
                } else if (control.failure.perf_error) {
                    failure = control.failure;  // a PerfDiagnosticError propagates
                    failed = true;
                    break;
                } else {
                    sample.error = control.failure.error.message;
                }
            }
            if (!failed && (samples[0].identity != samples[1].identity || samples[0].info != samples[1].info)) {
                failure = perf_error(RmErrorKind::transient, "Clock INFO changed between fresh reads");
                failed = true;
            }
            if (!failed) {
                const Sample& first = samples[0];
                Sample& second = samples[1];
                // _same_state of the two control dicts: the layout is constant, so mask and payload.
                stable = first.control && second.control && first.control->mask == second.control->mask &&
                         first.control->payload == second.control->payload;
                ClockObservation o;
                o.identity = second.identity;
                o.info = std::move(second.info);
                if (stable) {
                    o.has_user_control = true;
                    o.user_control_mask = second.control->mask;
                    o.user_control = std::move(second.control->payload);
                } else {
                    o.user_control_error =
                        second.error.empty() ? std::string("User-clock requests were not stable") : second.error;
                }
                o.rows = *clock_info_decode(o.info.data(), o.info.size()).rows;
                observation = std::move(o);
            }
            // Python drops the transport cache after its first attempt when a second one follows
            // (gpu._current_limit_transport = None; gpu._current_limit_layout_choice = None), outside
            // its `with gpu._lock` as a GIL-atomic rebind. The shared RmTransport is only ever touched
            // under Gpu::lock() here, so the drop happens before this attempt's lock is released.
            const bool done = failed ? failure.perf_error && failure.error.kind != RmErrorKind::transient : stable;
            if (attempt == 0 && !done) {
                rm_.power.reset();
                rm_.layout_choice.reset();
            }
        }
        if (!failed) {
            if (stable) {
                out.observation = std::move(observation);
                return out;
            }
            partial = std::move(observation);
        } else {
            last = failure.error.message;
            if (failure.perf_error && failure.error.kind != RmErrorKind::transient) {
                out.error = last;
                return out;
            }
        }
    }
    if (partial) {
        out.observation = std::move(partial);
        return out;
    }
    out.error = last.empty() ? std::string("Clock INFO is unavailable") : last;
    return out;
}

}  // namespace druta::backend
