// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/current_limits.h"

#include <algorithm>
#include <cstdio>
#include <map>
#include <string>

#include "backend/gdi.h"

namespace druta::backend {
namespace {

constexpr std::uint32_t kInfo = 0x2080A618, kStatus = 0x2080A619, kControl = 0x2080A61A, kControlSet = 0x2080E61B;

RmError error(RmErrorKind kind, std::string message, bool fixed = false) {
    RmError e;
    e.kind = kind;
    e.fixed = fixed;
    e.message = std::move(message);
    return e;
}
RmError invalid(std::string message) { return error(RmErrorKind::invalid, std::move(message)); }
RmError transient(std::string message, bool fixed = false) { return error(RmErrorKind::transient, std::move(message), fixed); }
RmError unsupported(std::string message) { return error(RmErrorKind::unsupported, std::move(message), true); }

std::string hex_upper(std::uint32_t value) {  // Python's f"{value:X}"
    char text[16];
    std::snprintf(text, sizeof text, "%X", static_cast<unsigned>(value));
    return text;
}

std::string hex08(std::uint32_t value) {  // Python's f"0x{value:08X}"
    char text[16];
    std::snprintf(text, sizeof text, "0x%08X", static_cast<unsigned>(value));
    return text;
}

// "policy request failed (NTSTATUS {status}, RM 0x{rm:X})": Python prints the signed NTSTATUS,
// or "None" when no escape was issued (an accident of None != 0, kept for parity).
std::string request_failed(const EscapeResult& r, std::uint32_t rm_status) {
    std::string status = r.outcome == EscapeOutcome::issued ? std::to_string(r.ntstatus) : "None";
    return "policy request failed (NTSTATUS " + status + ", RM 0x" + hex_upper(rm_status) + ")";
}

// NVML's architecture enum, as _current_limit_state names the two legacy FE generations.
constexpr std::uint32_t kArchTuring = 6, kArchAmpere = 7;

// Python's format(value_ma / 1000, ".3f").rstrip("0").rstrip("."): three decimals of mA are exact.
std::string amps(std::int64_t value_ma) {
    char text[64];
    std::snprintf(text, sizeof text, "%.3f", static_cast<double>(value_ma) / 1000.0);
    std::string s = text;
    while (!s.empty() && s.back() == '0') s.pop_back();
    if (!s.empty() && s.back() == '.') s.pop_back();
    return s;
}

std::string join(const std::vector<std::string>& parts, const char* separator) {
    std::string out;
    for (const std::string& part : parts) {
        if (!out.empty()) out += separator;
        out += part;
    }
    return out;
}

std::map<std::uint32_t, std::int64_t> limits_of(const std::vector<CurrentRow>& rows) {
    std::map<std::uint32_t, std::int64_t> out;
    for (const CurrentRow& row : rows) out[row.policy] = row.limit_ma;
    return out;
}

std::uint32_t mask_word_of(const PowerLayout& layout) { return layout.control_mask_word.value_or(4); }
std::uint32_t status_mask_word_of(const PowerLayout& layout) { return layout.status_mask_word.value_or(1); }

std::optional<std::uint32_t> size_of(const PowerLayout& layout, std::uint32_t command) {
    for (std::size_t i = 0; i < 4; ++i)
        if (kPowerGetCommands[i] == command) return layout.sizes[i];
    return std::nullopt;
}

// The packet: the captured 17-word header, then the parameters; words 2, 14, 15 and 16 set.
struct Issued {
    EscapeResult result;
    std::vector<std::uint32_t> packet;
};

Issued issue(Gpu& gpu, const CapturedTransport& transport, std::uint32_t wire_command, const std::uint32_t* params,
             std::uint32_t size) {
    Issued out;
    out.packet.assign(17 + size / 4, 0);
    for (std::size_t i = 0; i < 17; ++i) out.packet[i] = transport.header[i];
    if (params) std::copy(params, params + size / 4, out.packet.begin() + 17);
    out.packet[2] = static_cast<std::uint32_t>(out.packet.size() * 4);
    out.packet[14] = wire_command;
    out.packet[15] = size;
    out.packet[16] = 0;
    out.result = escape_raw(gpu, out.packet.data(), static_cast<std::uint32_t>(out.packet.size() * 4), transport.fields);
    return out;
}

bool mask_fits(std::uint32_t mask, std::uint32_t capacity) {
    const std::uint64_t allowed = (std::uint64_t{1} << capacity) - 1;
    return (static_cast<std::uint64_t>(mask) & ~allowed) == 0;
}

}  // namespace

std::uint32_t CurrentLimits::capacity(const PowerLayout& layout) {
    // Policy occupancy is firmware data. The 18 occupied slots on one board do not define the
    // capacity of any of these ABI layouts.
    std::uint32_t value = 32;
    const std::uint32_t blocks[3][3] = {{layout.sizes[0], layout.info_base, layout.info_stride},
                                        {layout.sizes[1], layout.status_base, layout.status_stride},
                                        {layout.sizes[2], layout.control_base, layout.control_stride}};
    for (const auto& block : blocks) {
        const std::uint32_t fit = block[2] ? (block[0] - block[1]) / block[2] : 0;
        if (fit < value) value = fit;
    }
    return value;
}

Words CurrentLimits::probe_info(const PowerLayout& layout) {
    // Read-only info GET used to choose between same-sized transports.
    Words out;
    if (!rm_.power) {  // Python unpacks None here: a TypeError
        out.error = invalid("the power transport is not captured");
        return out;
    }
    const std::uint32_t size = layout.sizes[0];
    Issued r = issue(gpu_, *rm_.power, kInfo, nullptr, size);
    if (r.result.outcome != EscapeOutcome::issued || r.result.ntstatus != 0) {
        rm_.power.reset();
        rm_.layout_choice.reset();
        out.error = transient(request_failed(r.result, r.packet[16]));
        return out;
    }
    // .fixed marks a rejection that is a property of this layout on this driver (its size, or a
    // mask it cannot hold), not of the moment
    if (r.packet[16] != 0) {
        out.error = transient("policy info layout rejected (RM 0x" + hex_upper(r.packet[16]) + ")",
                              r.packet[16] == kRmCurrentPolicySizeRejected);
        return out;
    }
    std::vector<std::uint32_t> info(r.packet.begin() + 17, r.packet.end());
    if (info.size() < 2 || !mask_fits(info[1], capacity(layout))) {
        out.error = transient("current-policy mask does not fit this info layout", true);
        return out;
    }
    out.words = std::move(info);
    return out;
}

AbiResult CurrentLimits::abi() {
    AbiResult out;
    if (rm_.layout_choice && rm_.power) {
        std::vector<const PowerLayout*> layouts = power_layouts(RmTransport::key(*rm_.power));
        if (*rm_.layout_choice < layouts.size()) {
            out.layout = layouts[*rm_.layout_choice];
            out.index = *rm_.layout_choice;
            return out;
        }
    }
    LayoutsResult candidates = rm_.power_layouts();
    if (candidates.layouts.empty()) {
        out.error = candidates.error;
        return out;
    }
    if (candidates.layouts.size() == 1) {
        rm_.layout_choice = 0;
        out.layout = candidates.layouts[0];
        out.index = 0;
        return out;
    }
    struct Rejected {
        std::uint32_t info_bytes;
        RmError why;
    };
    std::vector<Rejected> rejected;
    for (std::uint32_t i = 0; i < candidates.layouts.size(); ++i) {
        const PowerLayout* candidate = candidates.layouts[i];
        Words probe = probe_info(*candidate);
        if (!probe.words) {
            if (!rm_.power) {  // the transport was dropped: re-raised as is
                out.error = probe.error;
                return out;
            }
            rejected.push_back({candidate->sizes[0], probe.error});
            continue;
        }
        rm_.layout_choice = i;
        out.layout = candidate;
        out.index = i;
        return out;
    }
    // Every candidate is reported, not only the last one tried: after a one-off RM error on the
    // right layout, the fallback's size rejection would otherwise hide the real code.
    std::string detail;
    bool all_fixed = !rejected.empty();
    for (const Rejected& item : rejected) {
        if (!detail.empty()) detail += "; ";
        detail += std::to_string(item.info_bytes) + "-byte info: " + item.why.message;
        if (!item.why.fixed) all_fixed = false;
    }
    if (all_fixed) {
        out.error = unsupported("no known policy-info layout fits this driver (" + detail + ")");
        return out;
    }
    out.error = transient(detail.empty() ? "unvalidated current-policy transport geometry" : detail);
    return out;
}

Words CurrentLimits::rm(std::uint32_t command, const std::vector<std::uint32_t>* params,
                        std::optional<std::uint32_t> policy_mask, bool any_live_policy) {
    // Only the four measured policy operations, on this live GPU client. A SET selects exactly one
    // policy: one of the generation's named current policies, or - for the full policy list
    // (any_live_policy) - one the driver's table listed on its last validated read.
    Words out;
    if (!rm_.profile_supported()) {
        out.error = invalid("current limits are not validated for this GPU generation");
        return out;
    }
    if (command != kInfo && command != kStatus && command != kControl && command != kControlSet) {
        out.error = invalid("unvalidated current-policy command");
        return out;
    }
    std::vector<std::uint32_t> allowed_masks;
    if (command == kControlSet) {
        if (!params) {
            out.error = invalid("current-policy writes require a getter buffer");
            return out;
        }
        if (any_live_policy) {
            for (std::uint32_t policy = 0; policy < 32; ++policy)
                if (power_policy_mask >> policy & 1) allowed_masks.push_back(std::uint32_t{1} << policy);
        } else {
            GenerationPolicies policies = rm_.generation_policies();
            for (std::size_t i = 0; i < policies.count; ++i) allowed_masks.push_back(std::uint32_t{1} << policies.policies[i].policy);
        }
        // Reject invalid requests before capturing a transport. The short RM26 CONTROL has its mask
        // at word zero; all larger understood CONTROL packets retain the five-word header. Geometry,
        // rather than a driver label, determines which word can be a selector.
        std::vector<std::uint32_t> selectors;
        for (const PowerLayout* candidate : all_power_layouts())
            if (size_of(*candidate, command) == static_cast<std::uint32_t>(params->size() * 4))
                selectors.push_back(mask_word_of(*candidate));
        if (selectors.empty()) {
            out.error = invalid("unexpected current-policy buffer size");
            return out;
        }
        bool selects = false;
        for (std::uint32_t index : selectors)
            if (index < params->size() && std::find(allowed_masks.begin(), allowed_masks.end(), (*params)[index]) != allowed_masks.end())
                selects = true;
        if (!selects) {
            out.error = invalid(any_live_policy ? "current-policy write must select one policy the validated table lists"
                                                : "current-policy write must select one generation-supported rail");
            return out;
        }
    }
    if (params && !rm_.layout_choice) {
        LayoutsResult candidates = rm_.power_layouts();
        if (candidates.layouts.empty()) {
            out.error = candidates.error;
            return out;
        }
        bool fits = false;
        for (const PowerLayout* candidate : candidates.layouts)
            if (size_of(*candidate, command) == static_cast<std::uint32_t>(params->size() * 4)) fits = true;
        if (!fits) {
            out.error = invalid("unexpected current-policy buffer size");
            return out;
        }
    }
    AbiResult chosen = abi();
    if (!chosen.layout) {
        out.error = chosen.error;
        return out;
    }
    const PowerLayout& layout = *chosen.layout;
    const std::uint32_t size = *size_of(layout, command);
    const std::uint32_t mask_word = mask_word_of(layout);
    if (command == kControlSet &&
        (params->size() <= mask_word || std::find(allowed_masks.begin(), allowed_masks.end(), (*params)[mask_word]) == allowed_masks.end())) {
        out.error = invalid("current-policy write mask does not select one validated policy");
        return out;
    }
    std::uint32_t wire_command = command;
    if (layout.has_commands) {
        bool identified = false;
        for (std::size_t i = 0; i < 4; ++i)
            if (kPowerGetCommands[i] == command) {
                wire_command = layout.commands[i];
                identified = true;
            }
        if (!identified) {
            out.error = invalid("legacy current-policy SET has not been identified");
            return out;
        }
    }
    std::vector<std::uint32_t> buffer;
    if (!params) {
        buffer.assign(size / 4, 0);
        if (command == kStatus) {
            if (!policy_mask) {
                out.error = invalid("current-policy status requires a validated mask");
                return out;
            }
            buffer[status_mask_word_of(layout)] = *policy_mask;
        } else if (command == kControl) {
            if (!policy_mask) {
                out.error = invalid("current-policy control requires a validated mask");
                return out;
            }
            buffer[mask_word] = *policy_mask;
        }
        params = &buffer;
    }
    if (params->size() * 4 != size) {
        out.error = invalid("unexpected current-policy buffer size");
        return out;
    }
    if (!rm_.power) {  // Python unpacks None here: a TypeError
        out.error = invalid("the power transport is not captured");
        return out;
    }
    Issued r = issue(gpu_, *rm_.power, wire_command, params->data(), size);
    const bool issued_ok = r.result.outcome == EscapeOutcome::issued && r.result.ntstatus == 0;
    if (!issued_ok || r.packet[16] != 0) {
        if (!issued_ok) {
            rm_.power.reset();
            rm_.layout_choice.reset();
        } else if (command != kControlSet && r.packet[16] == kRmCurrentPolicySizeRejected) {
            // a GET refused for its size: this layout is not the driver's, which a retry will not
            // change (a SET refusal is about the request, and stays an ordinary failure)
            out.error = unsupported("policy GET " + hex08(command) + " size " + std::to_string(size) + " rejected (RM 0x" +
                                    hex_upper(r.packet[16]) + ")");
            return out;
        }
        out.error = transient(request_failed(r.result, r.packet[16]));
        return out;
    }
    out.words = std::vector<std::uint32_t>(r.packet.begin() + 17, r.packet.end());
    return out;
}

PolicyTableResult CurrentLimits::policy_table() {
    // (layout, info, control, status) from one validated read of the driver's whole policy
    // table - control and status are None when the mask is empty. The generation-specific current
    // rows and the full policy list are both parsed from this, so both see the same checks.
    PolicyTableResult out;
    AbiResult chosen = abi();
    if (!chosen.layout) {
        out.error = chosen.error;
        return out;
    }
    const PowerLayout& layout = *chosen.layout;
    Words info = rm(kInfo, nullptr, std::nullopt, false);
    if (!info.words) {
        out.error = info.error;
        return out;
    }
    if (info.words->size() * 4 != layout.sizes[0]) {
        out.error = unsupported("current-policy info size differs from the measured ABI");
        return out;
    }
    const std::uint32_t mask = (*info.words)[1];
    if (!mask_fits(mask, capacity(layout))) {
        out.error = unsupported("current-policy mask exceeds the validated ABI capacity");
        return out;
    }
    PolicyTable table;
    table.layout = &layout;
    table.info = std::move(*info.words);
    if (!mask) {
        power_policy_mask = 0;
        last_policy_table = table;
        out.table = std::move(table);
        return out;
    }
    Words control = rm(kControl, nullptr, mask, false);
    if (!control.words) {
        out.error = control.error;
        return out;
    }
    Words dynamic = rm(kStatus, nullptr, mask, false);
    if (!dynamic.words) {
        out.error = dynamic.error;
        return out;
    }
    std::vector<std::uint32_t> control_header =
        mask_word_of(layout) == 0 ? std::vector<std::uint32_t>{mask} : std::vector<std::uint32_t>{0, 0, 0, 255, mask};
    const std::uint32_t status_word = status_mask_word_of(layout);
    const bool agrees = control.words->size() * 4 == layout.sizes[2] && dynamic.words->size() * 4 == layout.sizes[1] &&
                        status_word < dynamic.words->size() && (*dynamic.words)[status_word] == mask &&
                        control.words->size() >= control_header.size() &&
                        std::equal(control_header.begin(), control_header.end(), control.words->begin());
    if (!agrees) {
        out.error = unsupported("current-policy layout or mask differs from the measured ABI");
        return out;
    }
    // the mask a policy SET may select from (see rm)
    power_policy_mask = mask;
    table.control = std::move(*control.words);
    table.dynamic = std::move(*dynamic.words);
    last_policy_table = table;
    out.table = std::move(table);
    return out;
}

StateResult CurrentLimits::state(ConfirmDerivedClamp confirm, void* context) {
    StateResult out;
    unavailable.clear();
    PolicyTableResult table = policy_table();
    if (!table.table) {
        out.error = table.error;
        return out;
    }
    const PolicyTable& t = *table.table;
    const PowerLayout& layout = *t.layout;
    const std::vector<std::uint32_t>& info = t.info;
    const std::uint32_t mask = info[1];  // policy_table read it already
    const GenerationPolicies specs = rm_.generation_policies();
    for (std::size_t i = 0; i < specs.count; ++i) {
        const GenerationPolicy& spec = specs.policies[i];
        if (!(mask >> spec.policy & 1))
            unavailable.push_back({spec.policy, spec.label, false, false, "not exposed in the driver's policy mask"});
    }
    if (!mask) return out;  // ([], None)
    const std::vector<std::uint32_t>& control = *t.control;
    const std::vector<std::uint32_t>& dynamic = *t.dynamic;
    for (std::size_t i = 0; i < specs.count; ++i) {
        const GenerationPolicy& spec = specs.policies[i];
        const std::uint32_t policy = spec.policy;
        if (!(mask >> policy & 1)) continue;
        const std::size_t meta = (layout.info_base + policy * layout.info_stride) / 4;
        const std::size_t state = (layout.status_base + policy * layout.status_stride) / 4;
        const std::size_t record = (layout.control_base + policy * layout.control_stride) / 4;
        if (meta + 4 >= info.size() || state + 2 >= dynamic.size() || record + 1 >= control.size()) {
            // Python's IndexError; get_current_limits reports its text as the read error
            out.error = transient("list index out of range");
            return out;
        }
        const std::uint32_t type_id = info[meta + 1] & 255;
        const std::uint32_t channel = info[meta + 1] >> 8 & 255;
        const std::uint32_t unit = info[meta + 1] >> 16 & 255;
        const std::uint32_t minimum = info[meta + 2], default_ma = info[meta + 3], maximum = info[meta + 4];
        // The highest request without XOC: the generation's normal cap, raised to the policy's own
        // default so a lowered limit can always be typed back to the driver's value (default sits
        // above 500 A on some boards). The setter, Max all and the slider all use this.
        const std::uint32_t normal_maximum = std::min(std::max({spec.normal_maximum_ma, minimum, default_ma}), maximum);
        // A record of another type or unit, or whose three blocks disagree about its type, is a
        // policy this build does not understand: no control is offered for it. Values outside the
        // record's own range on an understood record are a bad reading, not that.
        const bool understood = type_id == spec.type && unit == 1 && 1 <= minimum && minimum <= default_ma &&
                                default_ma <= maximum && (dynamic[state] & 255) == type_id && control[record] == type_id;
        bool in_range = minimum <= control[record + 1] && control[record + 1] <= maximum && minimum <= dynamic[state + 1] &&
                        dynamic[state + 1] <= maximum;
        if (understood && !in_range) {
            // A legacy FE board request above its ordinary maximum can make the driver derive a peer
            // current above that peer's advertised maximum. Accept this *observation* only when a
            // fresh paired FE snapshot confirms the same native table and the effective current
            // agrees or saturates at its reported maximum. The setter still bounds every new
            // request by the current policy's own maximum.
            const std::uint32_t requested = control[record + 1], effective = dynamic[state + 1];
            const std::optional<std::uint32_t> arch = telemetry_.arch();
            if (arch && (*arch == kArchTuring || *arch == kArchAmpere) && maximum < requested && requested < 0xFFFFFFFF &&
                (effective == requested || effective == maximum))
                in_range = confirm != nullptr && confirm(context, policy, requested, effective, info, control);
        }
        if (!(understood && in_range)) {
            unavailable.push_back({policy, spec.label, true, !understood,
                                   "policy " + std::to_string(policy) + ": invalid current record (type=0x" + hex_upper(type_id) +
                                       ", channel=" + std::to_string(channel) + ", unit=" + std::to_string(unit) +
                                       ", min/default/max=" + std::to_string(minimum) + "/" + std::to_string(default_ma) + "/" +
                                       std::to_string(maximum) + ")"});
            continue;
        }
        CurrentRow row;
        row.policy = policy;
        row.label = channel == spec.channel ? std::string(spec.label)
                                            : "Policy " + std::to_string(policy) + " current (channel " + std::to_string(channel) + ")";
        row.channel = channel;
        row.minimum_ma = minimum;
        row.maximum_ma = maximum;
        row.normal_maximum_ma = normal_maximum;
        row.default_ma = default_ma;
        row.limit_ma = dynamic[state + 1];
        row.requested_ma = control[record + 1];
        row.value_ma = dynamic[state + 2];
        out.rows.push_back(std::move(row));
    }
    out.control = control;
    return out;
}

std::vector<CurrentRow> CurrentLimits::get_current_limits(ConfirmDerivedClamp confirm, void* context) {
    // `error` is every reason a policy is unavailable (shown in the UI and diagnostics).
    // `read_error` is the part that is a failed READ - what profiles and Reset all must not hide.
    // The rest is an interface this build does not understand, for which no control exists; it
    // stops being "not understood" once a policy has been read on this adapter, and counts as a
    // read failure again.
    const std::uint32_t established_before = established;
    if (!rm_.profile_supported()) {
        error = read_error = "NVAPI unavailable or GPU generation unsupported";
        last_rows.clear();
        unavailable.clear();
        return {};
    }
    StateResult s = state(confirm, context);
    if (s.error.kind != RmErrorKind::none) {
        error = s.error.message;
        read_error = s.error.kind == RmErrorKind::unsupported && !established_before ? "" : s.error.message;
        last_rows.clear();
        return {};
    }
    std::vector<std::string> errors, read_errors, vanished;
    for (const UnavailablePolicy& item : unavailable) {
        if (item.present) {
            errors.push_back(item.error);
            if (!item.unsupported || (established_before >> item.policy & 1)) read_errors.push_back(item.error);
        } else if (established_before >> item.policy & 1) {
            // a policy read earlier this session that the mask no longer lists has not stopped
            // existing: that is a bad read too
            vanished.push_back("policy " + std::to_string(item.policy) + " was read earlier this session and is now " + item.error);
        }
    }
    errors.insert(errors.end(), vanished.begin(), vanished.end());
    read_errors.insert(read_errors.end(), vanished.begin(), vanished.end());
    error = join(errors, "; ");
    read_error = join(read_errors, "; ");
    for (const CurrentRow& row : s.rows) established |= std::uint32_t{1} << row.policy;
    last_rows = s.rows;
    return s.rows;
}

SetResult CurrentLimits::set_current_limit_ma(std::int64_t policy, std::int64_t ma, bool pin, ConfirmDerivedClamp confirm,
                                              void* context) {
    // pin records the value as one the user set, so it is re-applied after a board-limit write
    // recomputes it (_reapply_power_policy_pins); profile restores and Reset all pass pin=False.
    SetResult result = set_unpinned(policy, ma, confirm, context);
    if (result.ok && pin) pins[static_cast<std::uint32_t>(policy)] = ma;
    return result;
}

SetResult CurrentLimits::set_unpinned(std::int64_t policy, std::int64_t ma, ConfirmDerivedClamp confirm, void* context) {
    // Change one policy and verify stored AND effective limits. Preserve all other control bytes.
    // If verification fails, replay only this policy's original record and report whether
    // restoration verified.
    const GenerationPolicies specs = rm_.generation_policies();
    bool supported = false;
    for (std::size_t i = 0; i < specs.count; ++i)
        if (specs.policies[i].policy == policy) supported = true;
    if (!supported) return {false, "not a supported current policy"};
    if (!rm_.profile_supported()) return {false, "current limits are not validated for this GPU generation"};
    StateResult s = state(confirm, context);
    if (s.error.kind != RmErrorKind::none) return {false, s.error.message};
    const CurrentRow* row = nullptr;
    for (const CurrentRow& candidate : s.rows)
        if (candidate.policy == policy) row = &candidate;
    if (!row) {
        std::string reason = "policy unavailable";
        for (const UnavailablePolicy& item : unavailable)
            if (item.policy == policy) {
                reason = item.error;
                break;
            }
        return {false, "policy " + std::to_string(policy) + ": " + reason + "; nothing written"};
    }
    const bool derived_clamp = row->requested_ma > row->maximum_ma && row->maximum_ma == row->limit_ma;
    if (row->limit_ma != row->requested_ma && !derived_clamp)
        return {false, "selected policy's stored and effective current limits disagree; nothing written"};
    std::int64_t maximum = clkdom_.voltage_xoc_enabled ? row->maximum_ma
                                                       : std::max<std::int64_t>(row->normal_maximum_ma, row->limit_ma);
    maximum = std::min<std::int64_t>(maximum, row->maximum_ma);
    if (!(row->minimum_ma <= ma && ma <= maximum))
        return {false, row->label + " must be between " + amps(row->minimum_ma) + " and " + amps(maximum) + " A in the current mode"};
    if (ma == row->limit_ma && ma == row->requested_ma) return {true, row->label + " already at " + amps(ma) + " A"};
    AbiResult chosen = abi();
    if (!chosen.layout) return {false, chosen.error.message};
    const PowerLayout& layout = *chosen.layout;
    const std::size_t word = (layout.control_base + static_cast<std::uint32_t>(policy) * layout.control_stride) / 4 + 1;
    const std::uint32_t mask_word = mask_word_of(layout);
    const std::vector<std::uint32_t>& original = *s.control;  // state() read control[word] already
    if (word >= original.size() || mask_word >= original.size()) return {false, "list index out of range"};
    std::vector<std::uint32_t> expected = original;
    expected[word] = static_cast<std::uint32_t>(ma);
    std::vector<std::uint32_t> request = expected;
    request[mask_word] = std::uint32_t{1} << policy;
    const std::string label = row->label;
    std::map<std::uint32_t, std::int64_t> wanted = limits_of(s.rows);
    wanted[static_cast<std::uint32_t>(policy)] = ma;
    const std::map<std::uint32_t, std::int64_t> before = limits_of(s.rows);

    std::string failure;
    Words set = rm(kControlSet, &request, std::nullopt, false);
    if (!set.words) {
        failure = set.error.message;
    } else {
        StateResult back = state(confirm, context);
        if (back.error.kind != RmErrorKind::none)
            failure = back.error.message;
        else if (!back.control || *back.control != expected)
            failure = "current-policy control read-back disagrees";
        else if (limits_of(back.rows) != wanted)
            failure = "effective current-policy read-back disagrees";
        else
            return {true, label + " set to " + amps(ma) + " A; effective limit verified"};
    }
    // This also runs when the SET itself failed: an uncertain write must not be mistaken for proof
    // that nothing changed.
    std::vector<std::uint32_t> restore = original;
    restore[mask_word] = std::uint32_t{1} << policy;
    std::string note;
    Words replay = rm(kControlSet, &restore, std::nullopt, false);
    if (!replay.words) {
        note = "restoration could not be verified: " + replay.error.message;
    } else {
        StateResult restored = state(confirm, context);
        if (restored.error.kind != RmErrorKind::none)
            note = "restoration could not be verified: " + restored.error.message;
        else if (!restored.control || *restored.control != original || limits_of(restored.rows) != before)
            note = "restoration could not be verified: restoration read-back disagrees";
        else
            note = "original current limit restored and verified";
    }
    return {false, failure + "; " + note};
}

}  // namespace druta::backend
