// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/rails.h"

#include <cmath>
#include <cstring>

#include "backend/nvapi.h"
#include "backend/rail_limits.h"
#include "backend/telemetry.h"
#include "core/calling.h"
#include "core/sleep.h"

namespace druta::backend {
namespace {

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);

std::uint32_t word_u32(const RailBlock& block, std::size_t index) {
    std::uint32_t v = 0;
    std::memcpy(&v, block.data() + index * 4, sizeof v);
    return v;
}

std::int32_t word_i32(const RailBlock& block, std::size_t index) {
    std::int32_t v = 0;
    std::memcpy(&v, block.data() + index * 4, sizeof v);
    return v;
}

void put_u32(RailBlock& block, std::size_t index, std::uint32_t value) {
    std::memcpy(block.data() + index * 4, &value, sizeof value);
}

std::size_t record_word(std::size_t rail) { return (kRailRecordBase + rail * kRailRecordStride) / 4; }

using StateRows = std::array<std::optional<RailState>, kRailCount>;

// `self.read_volt_rail_state() or {}`
StateRows state_or_empty(std::optional<StateRows> rows) { return rows ? *rows : StateRows{}; }

// _valid_volt_rail_state(rail, state.get(rail, {})) through the rail_limits port. An absent row is
// the empty dict (its type is None, never the rail's). Every value here is a plain int or float, so
// the port's error paths (big ints, non-numbers) cannot be reached.
bool valid_state(std::size_t rail, const std::optional<RailState>& state) {
    rail::Row row;
    if (state) {
        row.type = rail::Value::integer(static_cast<double>(state->type));
        row.limit[rail::reliability] = rail::Value::real(state->reliability);
        row.limit[rail::alt_reliability] = rail::Value::real(state->alt_reliability);
        row.limit[rail::overvoltage] = rail::Value::real(state->overvoltage);
        row.limit[rail::vmin] = rail::Value::real(state->vmin);
    }
    const auto result = rail::valid_volt_rail_state(rail::Value::integer(static_cast<double>(rail)), row);
    return result.error == rail::Error::none && result.value;
}

// `arch in self._VOLT_RAIL_ARCHITECTURES` (None is not in it).
bool architecture_supported(const std::optional<std::uint32_t>& arch) {
    return rail::volt_rail_architecture(arch.has_value(), arch.value_or(0)) == 1;
}

RailFields fields_of(const RailDeltas& d) { return {d.reliability, d.alt_reliability, d.overvoltage, d.vmin}; }
RailFields fields_of(const RailState& s) { return {s.reliability, s.alt_reliability, s.overvoltage, s.vmin}; }

// `again.get(rail) != row`: the whole deltas row (type and the four fields).
bool same_deltas(const RailDeltas& a, const RailDeltas& b) { return a.type == b.type && fields_of(a) == fields_of(b); }

// any(absolute.get(k) != after.get(k) for k in (*VOLT_LIMIT_FIELDS, "_boost_mv", "type")), negated.
bool same_state(const RailState& a, const RailState& b) {
    return fields_of(a) == fields_of(b) && a.boost_mv == b.boost_mv && a.type == b.type;
}

}  // namespace

const char* rail_reason_text(RailReason reason) {
    switch (reason) {
        case RailReason::none: return "";
        case RailReason::no_architecture: return "GPU architecture could not be detected. Retry detection.";
        case RailReason::unknown_architecture:
            return "Rail limit control is not yet understood for this GPU architecture.";
        case RailReason::no_control:
            return "The driver did not return this rail's control settings. Retry detection.";
        case RailReason::invalid_state: return "The driver did not return a valid rail status. Retry detection.";
        case RailReason::no_reference:
            return "Waiting for stable voltage readings to establish this rail's reference.";
    }
    return "";
}

RailBlocks Rails::blocks(RailGetter function, std::uint32_t version) {
    RailBlocks out;
    Nvapi& a = gpu_.nvapi;
    const std::size_t f = static_cast<std::size_t>(function);
    auto fn = a.get<HandlePtrFn>(function == RailGetter::Control ? nvapi_id::VoltRailsCtlGet : nvapi_id::VoltRailsAbs);
    if (!(a.ok && fn)) return out;
    const std::uint32_t known = caches_.known[f].value_or(0u);
    auto& retry = caches_.retry_after[f];
    auto& diagnostics = caches_.diagnostics[f];
    const double now = druta::monotonic_seconds();
    std::vector<std::size_t> candidates;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        if ((known >> r & 1u) || now >= retry[r].value_or(0.0)) candidates.push_back(r);
    }
    // R470 exposes the same NVAPI record fields at V1. Retry only after
    // INCOMPATIBLE_STRUCT_VERSION, not after an absent-rail error.
    std::vector<std::uint32_t> layouts;
    if (function == RailGetter::Control) {
        layouts = {kRailControlVersion, kRailControlV1};
    } else {
        layouts = {version};
    }
    const std::uint32_t preferred = caches_.versions[f].value_or(version);
    std::vector<std::uint32_t> requested{preferred};
    for (std::uint32_t v : layouts) {
        if (v != preferred) requested.push_back(v);
    }
    for (std::size_t rail : candidates) {
        const std::uint32_t mask = 1u << rail;
        for (std::uint32_t candidate_version : requested) {
            // One immediate read-only retry handles transient startup failures. Missing rails are
            // retried at a bounded cadence; a failed first read is never a permanent capability verdict.
            int status = 0;
            bool valid = false;
            RailBlock buf;
            for (std::int32_t attempt = 0; attempt < 2; ++attempt) {
                buf.assign(kRailBlockSize, 0);
                put_u32(buf, 0, candidate_version);
                put_u32(buf, 1, mask);
                status = fn(a.gpu, buf.data());
                valid = status == 0 && word_u32(buf, 0) == candidate_version && word_u32(buf, 1) == mask;
                diagnostics[rail] = RailDiagnostic{candidate_version, status, valid, attempt + 1};
                if (valid || status == kNvapiIncompatibleStructVersion) break;
            }
            if (valid) {
                out.rails[rail] = std::move(buf);
                caches_.versions[f] = candidate_version;
                retry[rail].reset();
                break;
            }
            if (status != kNvapiIncompatibleStructVersion) break;
        }
        if (!out.rails[rail]) retry[rail] = now + kRailRetrySeconds;
    }
    // Keep successful identities through a transient failure, but never fabricate fresh data from
    // the cache. Every write still needs reads.
    std::uint32_t read = known;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        if (out.rails[r]) read |= 1u << r;
    }
    caches_.known[f] = read;
    return out;
}

std::array<std::optional<RailDeltas>, kRailCount> Rails::deltas() {
    std::array<std::optional<RailDeltas>, kRailCount> out;
    RailBlocks b = blocks(RailGetter::Control, kRailControlVersion);
    for (std::size_t rail = 0; rail < kRailCount; ++rail) {
        if (!b.rails[rail]) continue;
        const RailBlock& block = *b.rails[rail];
        const std::size_t base = record_word(rail);
        RailDeltas d;
        d.type = word_i32(block, base);
        d.reliability = word_i32(block, base + 1) / 1000.0;
        d.alt_reliability = word_i32(block, base + 2) / 1000.0;
        d.overvoltage = word_i32(block, base + 3) / 1000.0;
        d.vmin = word_i32(block, base + 4) / 1000.0;
        out[rail] = d;
    }
    return out;
}

std::optional<std::array<std::optional<RailState>, kRailCount>> Rails::state() {
    std::array<std::optional<RailState>, kRailCount> out;
    bool any = false;
    RailBlocks b = blocks(RailGetter::Abs, kLiveRailVersion);
    for (std::size_t rail = 0; rail < kRailCount; ++rail) {
        if (!b.rails[rail]) continue;
        const RailBlock& block = *b.rails[rail];
        const std::size_t base = record_word(rail);
        RailState s;
        s.boost_mv = word_u32(block, 2) / 1000.0;
        s.type = word_u32(block, base);
        s.live = word_u32(block, base + 1) / 1000.0;
        s.reliability = word_u32(block, base + 2) / 1000.0;
        s.alt_reliability = word_u32(block, base + 3) / 1000.0;
        s.overvoltage = word_u32(block, base + 4) / 1000.0;
        s.effective = word_u32(block, base + 5) / 1000.0;
        s.vmin = word_u32(block, base + 6) / 1000.0;
        // THE INDEX IDENTIFIES THE RAIL, not the type field, which is only decoded for the caller to
        // check. An all-zero record means the mask selected a rail this card does not have:
        // reporting 0.0 mV as a live voltage would be worse than reporting nothing.
        if (s.live != 0.0 || s.reliability != 0.0) {
            out[rail] = s;
            any = true;
        }
    }
    if (!any) return std::nullopt;
    return out;
}

std::optional<RailLimits> Rails::limits(Telemetry& telemetry) {
    // Exact signed deltas with per-device, explicitly estimated references. Pair stable control
    // samples around absolute status before capturing a reference. Status includes the reported
    // boost contribution and may be quantized; neither it nor these estimates measure an I2C
    // voltage change. Raw first-read values are retained separately for exact restoration.
    std::array<std::optional<RailDeltas>, kRailCount> out = deltas();
    const StateRows state = state_or_empty(this->state());
    std::vector<std::size_t> needs;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        if (out[r] && !reference_[r] && valid_state(r, state[r])) needs.push_back(r);
    }
    // self.arch() is evaluated only when a rail needs a reference (`needs and self.arch() in ...`).
    if (!needs.empty() && architecture_supported(telemetry.arch())) {
        const std::array<std::optional<RailDeltas>, kRailCount> again = deltas();
        const StateRows after_rows = state_or_empty(this->state());
        for (std::size_t rail : needs) {
            const RailDeltas& row = *out[rail];
            const RailState& absolute = *state[rail];  // present: its state was valid
            const std::optional<RailState>& after = after_rows[rail];
            // A rail missing from either second sample fails the comparison, as Python's None and {} do.
            if (!again[rail] || !same_deltas(*again[rail], row)) continue;
            if (!after || !same_state(absolute, *after)) continue;
            // boost is a u32 / 1000.0 here, so it is always finite and non-negative; the check is
            // kept from the original, which also guards a state built some other way.
            const double boost = absolute.boost_mv;
            if (!std::isfinite(boost) || boost < 0) continue;
            // absolute[k] - row[k] - (boost if k == "reliability" else 0), left to right
            const RailFields a = fields_of(absolute), d = fields_of(row);
            RailFields bases{};
            for (std::size_t k = 0; k < bases.size(); ++k) bases[k] = a[k] - d[k] - (k == 0 ? boost : 0.0);
            if (!reference_[rail]) order_.push_back(rail);
            reference_[rail] = bases;
            // initial.setdefault(rail, tuple(round(row[k] * 1000) for k in VOLT_LIMIT_FIELDS)):
            // round() is round-half-even, as nearbyint is in the default rounding mode. row[k] is an
            // int32 / 1000.0, so the product rounds back into the int32 range.
            if (!initial_[rail]) {
                RailInitial values{};
                for (std::size_t k = 0; k < values.size(); ++k)
                    values[k] = static_cast<std::int32_t>(std::nearbyint(d[k] * 1000));
                initial_[rail] = values;
            }
        }
    }
    // An empty V1 control response is not evidence of an absent/present rail. The independent
    // discriminator establishes presence on both versions.
    const auto& control_version = caches_.versions[static_cast<std::size_t>(RailGetter::Control)];
    if (control_version && *control_version == kRailControlV1) {
        for (std::size_t r = 0; r < kRailCount; ++r) {
            if (!state[r]) out[r].reset();
        }
    }
    RailLimits rows;
    bool any = false;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        if (!out[r]) continue;
        RailLimitRow row;
        row.deltas = *out[r];
        row.base = reference_[r];
        // False: the absolute state was not read this time, so the boost and effective figures are
        // placeholders, not the card's.
        row.absolute_ok = state[r].has_value();
        row.boost_mv = state[r] ? state[r]->boost_mv : 0.0;
        if (state[r]) row.effective_mv = state[r]->effective;
        rows[r] = row;
        any = true;
    }
    if (!any) return std::nullopt;
    return rows;
}

std::optional<RailProfile> Rails::profile(Telemetry& telemetry) {
    // Current-adapter references, not a generation's factory defaults. The absolute getter is
    // VID-quantized; its first stable paired sample anchors a displayed estimate, and only the
    // signed control deltas are exact.
    if (!architecture_supported(telemetry.arch())) return std::nullopt;
    RailProfile p;
    p.bases = reference_;
    p.poweron = initial_;
    p.order = order_;
    p.control_version = caches_.versions[static_cast<std::size_t>(RailGetter::Control)].value_or(kRailControlVersion);
    // self.arch() a second time, as the original does (cached by now: it was recognized above).
    const std::optional<std::uint32_t> arch = telemetry.arch();
    p.settle_vmin = arch && *arch == kArchPascal && p.control_version == kRailControlV1;
    return p;
}

RailDiagnosis Rails::diagnostics(Telemetry& telemetry) {
    RailDiagnosis d;
    d.architecture = telemetry.arch();
    const std::optional<RailLimits> limits = this->limits(telemetry);
    const RailLimits current = limits ? *limits : RailLimits{};
    const StateRows state = state_or_empty(this->state());
    for (std::size_t r = 0; r < kRailCount; ++r) {
        // sorted(set(current) | set(state) | {0})
        if (!(r == 0 || current[r] || state[r])) continue;
        RailReason reason = RailReason::none;
        if (!d.architecture) {
            reason = RailReason::no_architecture;
        } else if (!architecture_supported(d.architecture)) {
            reason = RailReason::unknown_architecture;
        } else if (!current[r]) {
            reason = RailReason::no_control;
        } else if (!valid_state(r, state[r])) {
            reason = RailReason::invalid_state;
        } else if (!current[r]->base) {
            reason = RailReason::no_reference;
        }
        d.present[r] = true;
        d.reasons[r] = reason;
        if (reason == RailReason::none) d.available = true;
    }
    d.reason = d.available ? RailReason::none : d.reasons[0];
    d.architecture_source = telemetry.arch_source();
    return d;
}

bool Rails::limits_supported(Telemetry& telemetry, std::optional<std::int32_t> rail) {
    // Each understood rail stands on its own current-adapter getters.
    if (!profile(telemetry)) return false;
    const std::optional<RailLimits> limits = this->limits(telemetry);
    const StateRows state = state_or_empty(this->state());
    std::array<bool, kRailCount> supported{};
    bool any = false;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        // row.get("_base_mv") is a full dict or an empty one
        supported[r] = limits && (*limits)[r] && (*limits)[r]->base && valid_state(r, state[r]);
        any = any || supported[r];
    }
    if (!rail) return any;
    return *rail >= 0 && static_cast<std::size_t>(*rail) < kRailCount && supported[static_cast<std::size_t>(*rail)];
}

std::size_t Rails::limit_fields(Telemetry& telemetry, std::int32_t rail) {
    if (!has_reference()) limits(telemetry);
    const std::optional<RailProfile> p = profile(telemetry);
    if (!p || rail < 0 || static_cast<std::size_t>(rail) >= kRailCount) return 0;
    return p->bases[static_cast<std::size_t>(rail)] ? rail::kFieldCount : 0;
}

RailStockLimit Rails::stock_limit_mv(Telemetry& telemetry, std::int32_t rail, std::int32_t field) {
    // Compatibility name: the estimated first-read value, never factory stock.
    RailStockLimit out;
    if (!has_reference()) limits(telemetry);
    const std::optional<RailProfile> p = profile(telemetry);
    if (!p || rail < 0 || static_cast<std::size_t>(rail) >= kRailCount) return out;
    const std::size_t r = static_cast<std::size_t>(rail);
    if (!p->poweron[r]) return out;
    // profile["bases"][rail][key]: a key outside VOLT_LIMIT_FIELDS raises KeyError here. A rail with
    // a first-read record always has bases: both are set together and neither is ever removed.
    if (field < 0 || static_cast<std::size_t>(field) >= rail::kFieldCount || !p->bases[r]) {
        out.kind = RailStockLimit::Kind::bad_field;
        return out;
    }
    const std::size_t k = static_cast<std::size_t>(field);
    out.kind = RailStockLimit::Kind::value;
    out.mv = (*p->bases[r])[k] + (*p->poweron[r])[k] / 1000.0;
    return out;
}

std::optional<RailLimitsMv> Rails::limits_mv(Telemetry& telemetry) {
    const std::optional<RailLimits> raw = limits(telemetry);
    if (!raw) return std::nullopt;
    RailLimitsMv out;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        const auto& row = (*raw)[r];
        if (!row) continue;
        // abs_limit_mv(fields, k): fields[k] + fields["_base_mv"].get(k, nan), through the port
        rail::Row fields;
        const RailFields limit = fields_of(row->deltas);
        fields.bases = rail::Bases::mapping;
        for (std::size_t k = 0; k < rail::kFieldCount; ++k) {
            fields.limit[k] = rail::Value::real(limit[k]);
            if (row->base) fields.base[k] = rail::Value::real((*row->base)[k]);
        }
        RailFields values{};
        for (std::size_t k = 0; k < rail::kFieldCount; ++k) values[k] = rail::abs_limit_mv(fields, k).value.v;
        out[r] = values;
    }
    return out;
}

std::optional<double> Rails::live_mv(std::int32_t rail) {
    auto s = state();
    if (!s || rail < 0 || static_cast<std::size_t>(rail) >= kRailCount) return std::nullopt;
    const auto& row = (*s)[static_cast<std::size_t>(rail)];
    if (!row) return std::nullopt;
    return row->live;
}

}  // namespace druta::backend
