// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/perf_diagnostics.h"

#include <cstdio>
#include <cstring>
#include <string>

#include "backend/gdi.h"
#include "core/mutex.h"
#include "core/sha256.h"

namespace druta::backend {

namespace {

RmError perf_error(RmErrorKind kind, std::string message) {
    RmError e;
    e.kind = kind;
    e.message = std::move(message);
    return e;
}

// f"0x{value:08X}" (padded) or f"0x{value:X}".
std::string hex(std::uint32_t value, bool padded) {
    char text[16];
    if (padded)
        std::snprintf(text, sizeof text, "0x%08X", static_cast<unsigned>(value));
    else
        std::snprintf(text, sizeof text, "0x%X", static_cast<unsigned>(value));
    return text;
}

constexpr std::uint32_t kHeaderWords = 17;
constexpr std::uint32_t kPrologueBytes = kHeaderWords * 4;  // 68: the payload starts after the header
constexpr std::uint32_t kMaskOffset = 72;                   // the request mask's byte offset in the packet

}  // namespace

// ---- perf_layouts -----------------------------------------------------------------------------

const std::array<PerfGroupLayout, 3>& perf_layouts() {
    static const std::array<PerfGroupLayout, 3> table{{
        {"limits", 0x4D2C0A9C, 0xA59BE705, 0x2080A022, 0x2080A024, 0x7A8C, 0x16850, 512, 64, 0x288, 0x3C, 0x50, 0xB4},
        {"cf_controller", 0x153DE751, 0x3BD390E8, 0x2080A0C8, 0x2080E0CA, 0x6A0C, 0x588C, 32, 4, 0xC, 0x350, 0xC,
         0x2C4},
        {"cf_policy", 0x1CAC3865, 0x69DE2C54, 0x2080A0CC, 0x2080A0CE, 0x230, 0x88, 32, 4, 0x30, 0x10, 8, 4},
    }};
    return table;
}

bool perf_get_allowed(std::uint32_t command, std::uint32_t size) {
    for (const PerfGroupLayout& layout : perf_layouts())
        if ((command == layout.info_command && size == layout.info_size) ||
            (command == layout.control_command && size == layout.control_size))
            return true;
    return false;
}

const PerfGroupLayout* perf_layout_for_command(std::uint32_t command) {
    for (const PerfGroupLayout& layout : perf_layouts())
        if (command == layout.info_command || command == layout.control_command) return &layout;
    return nullptr;
}

// ---- decode_group -----------------------------------------------------------------------------

const PerfGroupLayout* perf_layout_named(const std::string& name) {
    for (const PerfGroupLayout& layout : perf_layouts())
        if (name == layout.name) return &layout;
    return nullptr;
}

std::string perf_unknown_group_message(const std::string& name) {
    // repr(str): single quotes unless the text holds ' and no ".
    const bool double_quote = name.find('\'') != std::string::npos && name.find('"') == std::string::npos;
    const char quote = double_quote ? '"' : '\'';
    std::string out = "Unknown PERF diagnostic group ";
    out += quote;
    for (const char ch : name) {
        const auto c = static_cast<unsigned char>(ch);
        if (ch == '\\') {
            out += "\\\\";
        } else if (ch == quote) {
            out += '\\';
            out += ch;
        } else if (ch == '\t') {
            out += "\\t";
        } else if (ch == '\n') {
            out += "\\n";
        } else if (ch == '\r') {
            out += "\\r";
        } else if (c < 0x20 || c == 0x7F) {
            char text[8];
            std::snprintf(text, sizeof text, "\\x%02x", static_cast<unsigned>(c));
            out += text;
        } else {
            out += ch;
        }
    }
    out += quote;
    return out;
}

namespace {

std::uint16_t le16(const std::uint8_t* p) {
    return static_cast<std::uint16_t>(p[0] | (p[1] << 8));
}

std::uint32_t le32(const std::uint8_t* p) {
    return std::uint32_t{p[0]} | (std::uint32_t{p[1]} << 8) | (std::uint32_t{p[2]} << 16) |
           (std::uint32_t{p[3]} << 24);
}

constexpr const char* kTypesDisagree = "INFO and CONTROL row types disagree";
constexpr const char* kLabelEncoding = "Unrecognized driver label encoding";
constexpr const char* kLimitNoLabel = "Limit row type has no decoded label layout";
constexpr const char* kPolicyNotBoolean = "Policy flag is not Boolean";
constexpr const char* kPolicyNoFlag = "Policy row type has no decoded flag layout";
constexpr const char* kTargetOutside = "Target index is outside the valid policy mask";
constexpr const char* kTargetRole = "Target policy role does not match its INFO role table";
constexpr const char* kTargetFlag = "Target policy flag layout is not understood";

}  // namespace

PerfDecodeResult perf_decode_group(const std::string& name, const std::uint8_t* info, std::size_t info_size,
                                   const std::uint8_t* control, std::size_t control_size) {
    PerfDecodeResult out;
    const PerfGroupLayout* found = perf_layout_named(name);
    if (!found) {
        out.error = perf_error(RmErrorKind::invalid, perf_unknown_group_message(name));
        return out;
    }
    const PerfGroupLayout& layout = *found;
    const bool limits = name == "limits", controller = name == "cf_controller", policy = name == "cf_policy";
    // _mask: the INFO geometry first.
    if (info_size != layout.info_size) {
        out.error = perf_error(RmErrorKind::malformed, "Unexpected PERF INFO parameter geometry");
        return out;
    }
    const std::uint8_t* mask = info + 4;
    // bits >> capacity: any mask bit at or past the capacity.
    for (std::uint32_t bit = layout.capacity; bit < layout.mask_bytes * 8u; ++bit)
        if (mask[bit / 8] & (1u << (bit % 8))) {
            out.error = perf_error(RmErrorKind::malformed, "PERF INFO mask exceeds group capacity");
            return out;
        }
    bool any = false;
    for (std::uint32_t k = 0; k < layout.mask_bytes; ++k) any = any || mask[k];
    if (control) {
        if (control_size != layout.control_size) {
            out.error = perf_error(RmErrorKind::malformed, "Unexpected PERF CONTROL parameter geometry");
            return out;
        }
        if (std::memcmp(mask, control + 4, layout.mask_bytes) != 0 || info[0] != control[0]) {
            out.error = perf_error(RmErrorKind::malformed, "PERF INFO and CONTROL group identifiers or masks disagree");
            return out;
        }
    } else if (any) {
        out.error = perf_error(RmErrorKind::malformed, "A populated PERF group needs matching CONTROL readback");
        return out;
    }

    PerfSnapshot snap;
    snap.layout = &layout;
    snap.mask.assign(mask, mask + layout.mask_bytes);
    // Every geometry fits exactly (offset + capacity * stride <= size for each table), and a set bit
    // implies control != nullptr (checked above), so each row slice below is the full stride.
    for (std::uint32_t index = 0; index < layout.capacity; ++index) {
        if (!(mask[index / 8] & (1u << (index % 8)))) continue;
        const std::uint8_t* raw = info + layout.info_offset + std::size_t{index} * layout.info_stride;
        const std::uint8_t* current = control + layout.control_offset + std::size_t{index} * layout.control_stride;
        PerfEntry row;
        row.index = index;
        row.type = raw[0];
        row.control_type = current[0];
        row.info_raw.assign(raw, raw + layout.info_stride);
        row.control_raw.assign(current, current + layout.control_stride);
        if (raw[0] != current[0]) {
            row.issues.push_back(kTypesDisagree);
        } else if (limits) {
            if (raw[0] == 6) {
                // label_field = raw[2:32]; the label is the text before the first NUL.
                const std::uint8_t* field = raw + 2;
                std::size_t length = 0;
                while (length < 30 && field[length]) ++length;
                bool printable = length > 0;
                for (std::size_t k = 0; k < length; ++k) printable = printable && field[k] >= 32 && field[k] <= 126;
                if (length == 30 || !printable)
                    row.issues.push_back(kLabelEncoding);
                else
                    row.driver_label.emplace(reinterpret_cast<const char*>(field), length);
            } else {
                row.issues.push_back(kLimitNoLabel);
            }
        } else if (policy) {
            if (raw[0] == 0) {
                row.policy = true;
                row.role_bytes_raw[0] = raw[2];
                row.role_bytes_raw[1] = raw[3];
                row.role_id = raw[3];
                row.controller_mask = le32(raw + 0xC);
                if (current[2] == 0 || current[2] == 1)
                    row.registry_controlled_flag_raw = current[2];
                else
                    row.issues.push_back(kPolicyNotBoolean);
            } else {
                row.issues.push_back(kPolicyNoFlag);
            }
        }
        snap.entries.push_back(std::move(row));
    }

    if (any && control) {
        struct Source {
            const char* label;
            const std::uint8_t* packet;
            std::uint32_t offset;
        };
        Source offsets[3] = {};
        std::size_t count = 0;
        if (limits) {
            offsets[count++] = {"limits_control_0x4c", control, 0x4C};
            offsets[count++] = {"limits_control_0x4d", control, 0x4D};
            // Only the identified subclass adds this INFO field after the array.
            if (info[0x44] == 8 && control[0x44] == 8) offsets[count++] = {"limits_info_0x7a88", info, 0x7A88};
        } else if (controller) {
            offsets[count++] = {"controller_control_0x08", control, 8};
            offsets[count++] = {"controller_control_0x09", control, 9};
        }
        for (std::size_t k = 0; k < count; ++k) {
            const Source& source = offsets[k];
            const std::uint8_t value = source.packet[source.offset];
            PerfFlag flag{source.label, value, std::nullopt};
            if (value == 0 || value == 1) flag.boolean = value == 1;
            snap.flags.push_back(flag);
            if (value > 1) snap.issues.push_back(std::string(source.label) + " is not Boolean");
        }
        if (policy) {
            struct Field {
                const char* field;
                std::uint32_t offset;
                std::uint8_t role;
            };
            static const Field fields[] = {{"bit0", 0x18, 7}, {"bits2_1", 0x14, 5}, {"bit3", 0x20, 11}};
            for (const Field& f : fields) {
                const std::uint16_t index = le16(info + f.offset);
                const PerfEntry* row = nullptr;
                for (const PerfEntry& entry : snap.entries)
                    if (entry.index == index) row = &entry;
                PerfTarget target;
                target.field = f.field;
                if (index != 0xFFFF) target.index = index;
                target.present = row != nullptr;
                if (index != 0xFFFF && !row) {
                    target.issue = kTargetOutside;
                } else if (row) {
                    // row.get("role_id") is None unless the row decoded as a type-0 policy.
                    if (!row->policy || row->role_id != f.role)
                        target.issue = kTargetRole;
                    else
                        target.flag_raw = row->registry_controlled_flag_raw;
                    if (!target.flag_raw && !target.issue) target.issue = kTargetFlag;
                }
                snap.targets.push_back(target);
            }
        }
    }
    snap.info_sha256 = druta::sha256_hex(info, info_size);
    if (control) snap.control_sha256 = druta::sha256_hex(control, control_size);
    out.snapshot = std::move(snap);
    return out;
}

// ---- the reader -------------------------------------------------------------------------------

PerfTransportResult PerfDiagnostics::transport() {
    PerfTransportResult out;
    LayoutsResult layouts = rm_.power_layouts();
    if (layouts.layouts.empty()) {
        // CurrentLimitUnsupported is "unsupported"; every other ValueError a failed read.
        out.error = perf_error(
            layouts.error.kind == RmErrorKind::unsupported ? RmErrorKind::unsupported : RmErrorKind::transient,
            layouts.error.message);
        return out;
    }
    if (!rm_.power) {
        out.error = perf_error(RmErrorKind::transient, "No captured paired RM getter transport");
        return out;
    }
    // Python also checks that every header word is an int in 0..0xFFFFFFFF and that the fields are
    // exactly the five escape fields: both hold by construction here (std::uint32_t words, the
    // EscapeFields struct). header_count is a fixture's stored length.
    const CapturedTransport& t = *rm_.power;
    if (t.header_count != kHeaderWords || t.header[14] != kPerfTransportCommand ||
        static_cast<std::uint64_t>(t.header[2]) != std::uint64_t{kPrologueBytes} + t.header[15]) {
        // Explicit, as the plan asks: an RM26 (0x20802612) transport is a validated power transport
        // but not one the PERF GETs are understood to share.
        out.error = perf_error(RmErrorKind::unsupported, "PERF diagnostics need the validated modern RM transport");
        return out;
    }
    out.transport = t;
    return out;
}

PerfGetResult PerfDiagnostics::get(std::uint32_t command, std::uint32_t size, const PerfRequest* transport,
                                   PerfMask mask_kind, const std::uint8_t* mask, std::uint32_t mask_size) {
    PerfGetResult out;
    if (!perf_get_allowed(command, size)) {
        out.error = perf_error(RmErrorKind::invalid, "Only understood read-only PERF commands are allowed");
        return out;
    }
    const PerfGroupLayout& layout = *perf_layout_for_command(command);
    if (command == layout.control_command) {
        if (mask_kind != PerfMask::bytes || mask_size != layout.mask_bytes) {
            out.error = perf_error(RmErrorKind::invalid, "PERF CONTROL GET requires an exact INFO mask");
            return out;
        }
    } else if (mask_kind != PerfMask::none) {
        out.error = perf_error(RmErrorKind::invalid, "PERF INFO GET does not accept a request mask");
        return out;
    }
    TargetResult bound = binding_.bind();
    if (!bound.target) {
        out.error = bound.error;
        return out;
    }
    const std::uint32_t words = kHeaderWords + size / 4;
    const std::uint32_t packet_bytes = words * 4;
    // Python unpacks the transport and copies every header word into a zeroed packet of `words`
    // words: a shorter header leaves the rest 0, words past 16 land in the payload, and a header
    // longer than the packet raises IndexError before anything is sent.
    if (!transport || transport->header_count > words || (transport->header_count && !transport->header)) {
        out.bad_transport = true;
        return out;
    }
    std::vector<std::uint32_t> packet(words, 0);
    for (std::uint32_t i = 0; i < transport->header_count; ++i) packet[i] = transport->header[i];
    packet[2] = packet_bytes;
    packet[14] = command;
    packet[15] = size;
    packet[16] = 0;
    if (mask_kind == PerfMask::bytes && mask_size)
        std::memcpy(reinterpret_cast<std::uint8_t*>(packet.data()) + kMaskOffset, mask, mask_size);

    EscapeResult escape = escape_raw(gpu_, packet.data(), packet_bytes, transport->fields);
    bound = binding_.bind();
    if (!bound.target) {
        out.error = bound.error;
        return out;
    }
    // _legacy_clk_escape returns None when no escape was issued; None != 0 takes this branch too.
    if (escape.outcome != EscapeOutcome::issued || escape.ntstatus != 0) {
        rm_.power.reset();
        rm_.layout_choice.reset();
        const bool issued = escape.outcome == EscapeOutcome::issued;
        out.error = perf_error(RmErrorKind::transient, "PERF GET transport failed (NTSTATUS " +
                                                           (issued ? std::to_string(escape.ntstatus) : std::string("None")) +
                                                           ")");
        if (issued) out.error.ntstatus = escape.ntstatus;
        return out;
    }
    if (packet[16] != 0) {
        const std::uint32_t rm = packet[16];
        out.error = perf_error(rm == 0x1F || rm == 0x56 ? RmErrorKind::unsupported : RmErrorKind::transient,
                               "PERF GET " + hex(command, true) + " rejected (RM " + hex(rm, false) + ")");
        out.error.ntstatus = 0;
        out.error.rm_status = rm;
        return out;
    }
    if (packet[2] != packet_bytes || packet[14] != command || packet[15] != size) {
        out.error = perf_error(RmErrorKind::malformed, "PERF response changed its command or parameter geometry");
        out.error.ntstatus = 0;
        return out;
    }
    const std::uint8_t* bytes = reinterpret_cast<const std::uint8_t*>(packet.data());
    out.payload.emplace(bytes + kPrologueBytes, bytes + packet_bytes);
    return out;
}

PerfDiagnostics::GroupRead PerfDiagnostics::read_group(const PerfGroupLayout& layout, std::uint32_t attempts) {
    GroupRead out;
    const std::string name = layout.name;
    for (std::uint32_t attempt = 1; attempt <= attempts; ++attempt) {
        out.attempts = attempt;
        RmError error;
        {
            LockGuard guard(gpu_.lock());
            [&] {
                TargetResult bound = binding_.bind();
                if (!bound.target) {
                    error = bound.error;
                    return;
                }
                out.target = bound.target;
                if (!gpu_.nvapi.interface(layout.info_id) || !gpu_.nvapi.interface(layout.control_id)) {
                    error = perf_error(RmErrorKind::unsupported, "PERF getter exports are unavailable");
                    return;
                }
                PerfTransportResult t = transport();
                if (!t.transport) {
                    error = t.error;
                    return;
                }
                PerfRequest request;
                request.header = t.transport->header.data();
                request.header_count = t.transport->header_count;
                request.fields = t.transport->fields;
                // Python's `except Exception` branch (a non-PerfDiagnosticError from the binding, the
                // capture or the escape: reported as transient "PERF read failed (Type): msg" with the
                // transport dropped) has no native counterpart: the core reports every failure as a
                // kinded error. The two _get outcomes that are Python exceptions rather than
                // PerfDiagnosticErrors (an invalid-kind ValueError, an unusable transport) cannot occur
                // here, as the commands, sizes, mask and 17-word transport come from the layout and the
                // validated capture; they are still routed through that branch's handling.
                auto unexpected = [&](const char* type, const std::string& message) {
                    rm_.power.reset();
                    rm_.layout_choice.reset();
                    error = perf_error(RmErrorKind::transient,
                                       std::string("PERF read failed (") + type + "): " + message);
                };
                PerfGetResult info = get(layout.info_command, layout.info_size, &request, PerfMask::none, nullptr, 0);
                if (info.bad_transport) return unexpected("IndexError", "invalid index");
                if (!info.payload) {
                    if (info.error.kind == RmErrorKind::invalid) return unexpected("ValueError", info.error.message);
                    error = info.error;
                    return;
                }
                const std::vector<std::uint8_t>& info_bytes = *info.payload;
                // _mask: the payload is exactly info_size bytes (the echo was checked).
                const std::uint8_t* mask = info_bytes.data() + 4;
                bool any = false;
                for (std::uint32_t k = 0; k < layout.mask_bytes; ++k) any = any || mask[k];
                std::optional<std::vector<std::uint8_t>> control;
                if (any) {
                    PerfGetResult c = get(layout.control_command, layout.control_size, &request, PerfMask::bytes,
                                          mask, layout.mask_bytes);
                    if (c.bad_transport) return unexpected("IndexError", "invalid index");
                    if (!c.payload) {
                        if (c.error.kind == RmErrorKind::invalid) return unexpected("ValueError", c.error.message);
                        error = c.error;
                        return;
                    }
                    control = std::move(c.payload);
                }
                PerfDecodeResult decoded =
                    perf_decode_group(name, info_bytes.data(), info_bytes.size(), control ? control->data() : nullptr,
                                      control ? control->size() : 0);
                if (!decoded.snapshot) {
                    error = decoded.error;
                    return;
                }
                out.snapshot = std::move(decoded.snapshot);
            }();
        }
        if (out.snapshot) {
            out.error = RmError{};
            return out;
        }
        out.error = error;
        if (error.kind != RmErrorKind::transient) break;
    }
    return out;
}

}  // namespace druta::backend
