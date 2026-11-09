// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/rails.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>

#include "backend/nvapi.h"
#include "backend/rail_limits.h"
#include "backend/telemetry.h"
#include "core/calling.h"
#include "core/mutex.h"
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

// round(x * 1000) of a millivolt figure that is an int32 / 1000.0: back into the int32 range.
std::int64_t microvolts(double mv) { return static_cast<std::int64_t>(std::nearbyint(mv * 1000)); }

const char* const kPreserve = "Could not preserve the current native rail record: ";

// str(values) of a list or tuple of ints (verify's `wanted`).
std::string values_text(const RailRecord& record) {
    const std::size_t n = record.count < 4 ? record.count : 4;
    std::string text = record.is_list ? "[" : "(";
    for (std::size_t k = 0; k < n; ++k) {
        if (k) text += ", ";
        text += std::to_string(record.values[k]);
    }
    if (!record.is_list && n == 1) text += ",";
    text += record.is_list ? "]" : ")";
    return text;
}

// Python's f"{x:.0f}": printf's "%.0f" (both round half to even on the exact binary value), with
// Python's spellings of the non-finite values (printf may write "-nan").
std::string fixed0(double x) {
    if (std::isnan(x)) return "nan";
    if (std::isinf(x)) return x < 0 ? "-inf" : "inf";
    char text[400];  // DBL_MAX is 309 digits
    std::snprintf(text, sizeof text, "%.0f", x);
    return text;
}

// Python's min(a, b) and max(a, b): the first argument unless the second compares strictly beyond it.
double py_min(double a, double b) { return b < a ? b : a; }
double py_max(double a, double b) { return b > a ? b : a; }

// A read_volt_rail_limits row as the rail_limits port's Row (the four fields, "_base_mv" and
// "_boost_mv"), for abs_limit_mv, rail_floor_mv and rail_ceiling_mv.
rail::Row port_row(const RailLimitRow& row) {
    rail::Row fields;
    const RailFields limit = fields_of(row.deltas);
    fields.bases = rail::Bases::mapping;  // "_base_mv" is always a dict here ({} without a reference)
    for (std::size_t k = 0; k < rail::kFieldCount; ++k) {
        fields.limit[k] = rail::Value::real(limit[k]);
        if (row.base) fields.base[k] = rail::Value::real((*row.base)[k]);
    }
    fields.boost_mv = rail::Value::real(row.boost_mv);
    return fields;
}

// The rows of read_volt_rail_limits hold floats with a dict base, so the port's error paths are
// unreachable: abs_limit_mv is fields[key] + base.get(key, nan).
double abs_mv(const RailLimitRow& row, std::size_t field) { return rail::abs_limit_mv(port_row(row), field).value.v; }

// {r: [int(round(row[k] * 1000)) for k in VOLT_LIMIT_FIELDS]}: one exact list record.
RailRecord record_of(std::size_t rail, const RailLimitRow& row) {
    RailRecord record;
    record.rail = static_cast<std::int32_t>(rail);
    record.key = RailKey::plain;
    record.count = rail::kFieldCount;
    record.is_list = true;
    const RailFields fields = fields_of(row.deltas);
    for (std::size_t k = 0; k < rail::kFieldCount; ++k) record.values[k] = microvolts(fields[k]);
    record.exact = true;
    return record;
}

bool fits_i32(std::int64_t v) { return v >= INT32_MIN && v <= INT32_MAX; }

// `expected = {r: [round(row[k] * 1000) ...] for r, row in cur.items() if row.get("_base_mv")}`,
// then `expected.update(recs)`: a rail of recs replaces its entry in place, or is appended.
RailRecords expected_records(const RailLimits& cur, const RailRecords& recs) {
    RailRecords expected;
    for (std::size_t r = 0; r < kRailCount; ++r) {
        if (cur[r] && cur[r]->base) expected.records[expected.count++] = record_of(r, *cur[r]);
    }
    for (std::size_t i = 0; i < recs.count; ++i) {
        bool replaced = false;
        for (std::size_t j = 0; j < expected.count; ++j) {
            if (expected.records[j].rail == recs.records[i].rail) {
                expected.records[j] = recs.records[i];
                replaced = true;
            }
        }
        if (!replaced) expected.records[expected.count++] = recs.records[i];
    }
    return expected;
}

std::string hex_upper(std::uint32_t value) {
    char text[16];
    std::snprintf(text, sizeof text, "%X", static_cast<unsigned>(value));
    return text;
}

}  // namespace

const char* rail_name(std::size_t rail) {
    switch (rail) {
        case 0: return "core rail (NVVDD)";
        case 1: return "MSVDD";
        default: return nullptr;
    }
}

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

std::optional<RailLimits> Rails::limits() {
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
    if (!needs.empty() && architecture_supported(telemetry_.arch())) {
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

std::optional<RailProfile> Rails::profile() {
    // Current-adapter references, not a generation's factory defaults. The absolute getter is
    // VID-quantized; its first stable paired sample anchors a displayed estimate, and only the
    // signed control deltas are exact.
    if (!architecture_supported(telemetry_.arch())) return std::nullopt;
    RailProfile p;
    p.bases = reference_;
    p.poweron = initial_;
    p.order = order_;
    p.control_version = caches_.versions[static_cast<std::size_t>(RailGetter::Control)].value_or(kRailControlVersion);
    // self.arch() a second time, as the original does (cached by now: it was recognized above).
    const std::optional<std::uint32_t> arch = telemetry_.arch();
    p.settle_vmin = arch && *arch == kArchPascal && p.control_version == kRailControlV1;
    return p;
}

RailDiagnosis Rails::diagnostics() {
    RailDiagnosis d;
    d.architecture = telemetry_.arch();
    const std::optional<RailLimits> limits = this->limits();
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
    d.architecture_source = telemetry_.arch_source();
    return d;
}

bool Rails::limits_supported(std::optional<std::int32_t> rail) {
    // Each understood rail stands on its own current-adapter getters.
    if (!profile()) return false;
    const std::optional<RailLimits> limits = this->limits();
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

std::size_t Rails::limit_fields(std::int32_t rail) {
    if (!has_reference()) limits();
    const std::optional<RailProfile> p = profile();
    if (!p || rail < 0 || static_cast<std::size_t>(rail) >= kRailCount) return 0;
    return p->bases[static_cast<std::size_t>(rail)] ? rail::kFieldCount : 0;
}

RailStockLimit Rails::stock_limit_mv(std::int32_t rail, std::int32_t field) {
    // Compatibility name: the estimated first-read value, never factory stock.
    RailStockLimit out;
    if (!has_reference()) limits();
    const std::optional<RailProfile> p = profile();
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

std::optional<RailLimitsMv> Rails::limits_mv() {
    const std::optional<RailLimits> raw = limits();
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

bool Rails::write_records_locked(const RailRecords& records, RailWriteOutcome& out) {
    // _write_rail_records_locked: recognize the observed RM packet independently of the NVAPI getter
    // version; the preconditions below return (False, None) without touching the attributes.
    if (!limits_supported(std::nullopt)) return false;
    const std::optional<RailProfile> p = profile();
    // Python dereferences the profile here; supported() has just found it, and the architecture it
    // depends on is cached once recognized, so a None here is not reached through the driver.
    if (!p) return false;
    const std::uint32_t control_version = p->control_version;
    // `not records or not set(records).issubset(profile["fields"])`: "fields" holds the referenced rails
    if (records.count == 0 || records.foreign) return false;
    for (std::size_t i = 0; i < records.count; ++i) {
        if (!p->bases[static_cast<std::size_t>(records.records[i].rail)]) return false;
    }
    // any(not self.volt_rail_limits_supported(r) for r in records): in order, stopping at the first
    for (std::size_t i = 0; i < records.count; ++i) {
        if (!limits_supported(records.records[i].rail)) return false;
    }
    for (std::size_t i = 0; i < records.count; ++i) {
        if (records.records[i].count != rail::kFieldCount) return false;
    }
    const std::optional<std::int32_t> boost = telemetry_.read_voltage_boost();
    if (!boost) return false;
    // rail_mask = sum(1 << rail for rail in records): TypeError for a key without an int shift
    for (std::size_t i = 0; i < records.count; ++i) {
        if (records.records[i].key == RailKey::no_shift) {
            out.type_error = true;
            return false;
        }
    }
    std::uint32_t rail_mask = 0;
    for (std::size_t i = 0; i < records.count; ++i) rail_mask += 1u << records.records[i].rail;
    out.attributes = true;
    ++out.writes;
    out.result = RailWriteResult{};
    // rail_transport._validated_records, which Python's prepare_write runs inside the hook after the
    // GET: the ABI records carry int32 values, so the same refusal is given here, before any escape.
    for (std::size_t i = 0; i < records.count; ++i) {
        if (records.records[i].key != RailKey::plain) {
            out.result.error = std::string(kPreserve) + "requested_mask must exactly match the record rails";
            return true;
        }
    }
    for (std::size_t i = 0; i < records.count; ++i) {
        if (!records.records[i].exact) {
            out.result.error = std::string(kPreserve) + "rail " + std::to_string(records.records[i].rail) +
                               " record values must be signed int32 integers";
            return true;
        }
    }
    ::druta::rail::Record raw[kRailCount] = {};
    for (std::size_t i = 0; i < records.count; ++i) {
        raw[i].rail = static_cast<std::uint32_t>(records.records[i].rail);
        for (std::size_t k = 0; k < ::druta::rail::kRecordValueCount; ++k)
            raw[i].values[k] = static_cast<std::int32_t>(records.records[i].values[k]);
    }
    out.result = rail_write_locked(gpu_, control_version, rail_mask, raw, records.count,
                                   static_cast<std::uint32_t>(*boost));
    return true;
}

RailWriteOutcome Rails::write_records(const RailRecords& records) {
    RailWriteOutcome out;
    // The code patch is process-wide even when GPU objects have different per-card locks. Only one
    // installer may own it at a time, for the whole sequence.
    LockGuard guard(rail_hook_mutex());
    // A fresh pre-write snapshot for the vmin settle decision; no driver string is consulted.
    const std::optional<RailLimits> previous = limits();
    const std::optional<RailProfile> p = profile();
    bool settle = false;
    std::optional<std::int32_t> boost;
    if (p && p->settle_vmin) {
        for (std::size_t i = 0; i < records.count && !settle; ++i) {
            const RailRecord& r = records.records[i];
            const auto& row = previous ? (*previous)[static_cast<std::size_t>(r.rail)] : std::optional<RailLimitRow>{};
            settle = r.count == rail::kFieldCount && row && microvolts(row->deltas.vmin) != r.values[3];
        }
        if (settle) boost = telemetry_.read_voltage_boost();
    }
    bool ran = write_records_locked(records, out);
    if (out.type_error) return out;  // Python raised: nothing after it runs
    out.ok = ran && out.result.done;
    out.status = ran ? out.result.status : std::nullopt;
    if (settle && out.ok && out.status && *out.status == 0) {
        // GP102/R470 recomputes an idle floor using the preceding stored value. A verified identity
        // re-send makes its live voltage catch up, including when restoring the stock floor. No extra
        // delta is added and every requested field is equal.
        const RailVerifyOutcome verified = verify_records(records, boost);
        if (!verified.error.empty()) {
            out.ok = false;
            return out;
        }
        ran = write_records_locked(records, out);
        out.ok = ran && out.result.done;
        out.status = ran ? out.result.status : std::nullopt;
    }
    return out;
}

RailVerifyOutcome Rails::verify_records(const RailRecords& records, std::optional<std::int32_t> boost) {
    // Check every preserved rail/field and the independent boost setting.
    RailVerifyOutcome out;
    const std::optional<RailLimits> back = limits();
    bool complete = back.has_value() && !records.foreign;
    for (std::size_t i = 0; complete && i < records.count; ++i)
        complete = (*back)[static_cast<std::size_t>(records.records[i].rail)].has_value();
    if (!complete) {
        out.error = "write issued but the rail read-back is incomplete";
        return out;
    }
    for (std::size_t i = 0; i < records.count; ++i) {
        const RailRecord& wanted = records.records[i];
        const RailFields fields = fields_of((*back)[static_cast<std::size_t>(wanted.rail)]->deltas);
        std::array<std::int64_t, 4> got{};
        for (std::size_t k = 0; k < got.size(); ++k) got[k] = microvolts(fields[k]);
        // `got != wanted`: a list never equals a tuple
        const bool equal = wanted.is_list && wanted.count == got.size() && got == wanted.values;
        if (!equal) {
            std::string text = "rail " + std::to_string(wanted.rail) + " read-back disagrees: wanted " +
                               values_text(wanted) + ", got [";
            for (std::size_t k = 0; k < got.size(); ++k) {
                if (k) text += ", ";
                text += std::to_string(got[k]);
            }
            out.error = text + "]";
            return out;
        }
    }
    if (telemetry_.read_voltage_boost() != boost) {
        out.error = "rail write changed the voltage boost setting";
        return out;
    }
    out.back = back;
    return out;
}

// ---- Increment R5: the verified setters ---------------------------------------------------------

RailWriteOutcome Rails::write_traced(const RailRecords& records, RailWriterTrace& trace) {
    RailWriteOutcome out = write_records(records);
    // _write_rail_records clears _volt_rail_write_error first; a locked write that ran sets all three.
    ++trace.calls;
    trace.last_attributes = out.attributes;
    if (out.attributes) {
        trace.attributes = true;
        trace.result = out.result;
    }
    return out;
}

void Rails::adopt_verified_hold_fields(std::int32_t rail, std::uint32_t fields, const RailLimits& back) {
    // Retain an explicit selected request equal to our temporary raise.
    if (!hold_.present || hold_.rail != rail) return;
    if (rail < 0 || static_cast<std::size_t>(rail) >= kRailCount) return;
    const auto& row = back[static_cast<std::size_t>(rail)];
    if (!row) return;  // the verified read-back always holds the written rail
    const RailFields values = fields_of(row->deltas);
    for (std::size_t k = 0; k < rail::kFieldCount; ++k) {
        if (!(fields >> k & 1u)) continue;
        if (hold_.written_uv[k] && hold_.prior_uv[k] && microvolts(values[k]) == *hold_.written_uv[k])
            hold_.prior_uv[k] = hold_.written_uv[k];
    }
}

Outcome Rails::set_limits(std::int32_t rail, const RailLimitRequest& limits, bool write_enabled, bool xoc,
                          RailWriterTrace& trace) {
    if (!write_enabled)
        return {false, "rail limit writes are disabled: nothing bounds this value but Druta, so it is off by default"};
    if (!limits_supported(rail)) return {false, "rail limit runtime getters are unavailable or structurally inconsistent"};
    // `if unknown:` tests the set, so the keyword "" alone refuses with an empty text
    if (limits.unknown) return {false, "not a rail limit: " + *limits.unknown};
    const std::optional<RailLimits> cur = this->limits();
    if (!cur) return {false, "cannot read the current limits"};
    const bool valid_rail = rail >= 0 && static_cast<std::size_t>(rail) < kRailCount;
    if (!valid_rail || !(*cur)[static_cast<std::size_t>(rail)])
        return {false, "rail " + std::to_string(rail) + " is not present on this GPU"};
    const std::size_t r = static_cast<std::size_t>(rail);
    // set(limits) - set(volt_rail_limit_fields(rail)): the fields are all four or none
    const std::size_t validated = limit_fields(rail);
    if (limits.count && validated == 0)
        return {false, "one or more fields are not validated for rail " + std::to_string(rail)};
    const std::optional<std::int32_t> boost = telemetry_.read_voltage_boost();
    if (!boost) return {false, "cannot preserve the current voltage boost"};
    const RailLimitRow& row = *(*cur)[r];
    RailRecords recs;
    recs.records[0] = record_of(r, row);
    recs.count = 1;
    RailRecord& rec = recs.records[0];
    for (std::size_t i = 0; i < limits.count; ++i) {
        const std::size_t key = limits.order[i];
        if (!limits.value_mv[key]) continue;
        const double mv = *limits.value_mv[key];
        double maximum = xoc ? kVoltLimitXocMaxMv : kVoltLimitMaxMv;
        // Leaving XOC keeps existing values and permits lowering them, but cannot raise an already
        // above-normal value any further.
        const double current = abs_mv(row, key);
        maximum = py_max(maximum, current);
        const RailStockLimit initial = stock_limit_mv(rail, static_cast<std::int32_t>(key));
        const double minimum = py_min(py_min(kVoltLimitMinMv, current),
                                      initial.kind == RailStockLimit::Kind::value ? initial.mv : kVoltLimitMinMv);
        if (!(minimum <= mv && mv <= maximum))
            return {false, std::string(rail::field_name(key)) + " " + fixed0(mv) + " mV is outside Druta's " +
                               fixed0(minimum) + "-" + fixed0(maximum) + " mV bound"};
        // int(round((mv - base) * 1000)): round half to even, as nearbyint is in the default mode
        const double delta = std::nearbyint((mv - (*row.base)[key]) * 1000);
        rec.values[key] = static_cast<std::int64_t>(delta);
    }
    for (std::size_t k = 0; k < rail::kFieldCount; ++k) rec.exact = rec.exact && fits_i32(rec.values[k]);
    const RailWriteOutcome w = write_traced(recs, trace);
    if (!w.ok) {
        if (trace.last_attributes && !trace.result.error.empty()) return {false, trace.result.error};
        return {false, "the rails request was not seen - nothing was written"};
    }
    if (w.status && *w.status) return {false, "driver refused the write (NV_STATUS 0x" + hex_upper(*w.status) + ")"};
    const RailVerifyOutcome v = verify_records(expected_records(*cur, recs), boost);
    if (!v.error.empty()) return {false, v.error};
    const RailLimits& back = *v.back;
    // A user can deliberately select the exact value currently supplied by our temporary hold raise.
    // Readback alone cannot distinguish that choice from the raise, so make it the release target
    // only after the selected write and all preserved records have been verified.
    std::uint32_t chosen = 0;
    for (std::size_t i = 0; i < limits.count; ++i) {
        if (limits.value_mv[limits.order[i]]) chosen |= 1u << limits.order[i];
    }
    adopt_verified_hold_fields(rail, chosen, back);
    const RailLimitRow& stored = *back[r];
    // sorted(limits): the field names in alphabetical order, including keys passed as None
    static constexpr std::size_t kSorted[] = {1, 2, 0, 3};  // alt_reliability, overvoltage, reliability, vmin
    std::string wrote;
    bool vmin_named = false;
    for (std::size_t key : kSorted) {
        bool named = false;
        for (std::size_t i = 0; i < limits.count; ++i) named = named || limits.order[i] == key;
        if (!named) continue;
        if (key == 3) vmin_named = true;
        if (!wrote.empty()) wrote += ", ";
        wrote += std::string(rail::field_name(key)) + " " + fixed0(abs_mv(stored, key));
    }
    std::string note;
    if (vmin_named) {
        const std::optional<std::uint32_t> arch = telemetry_.arch();
        if (arch && *arch == kArchPascal)
            note = "; Pascal vmin was verified at idle; a V/F point lock or P2 can hold the live voltage below this floor";
    }
    const rail::Row fields = port_row(stored);
    return {true, std::string(rail_name(r)) + ": " + wrote + " mV stored; floor " +
                      fixed0(rail::rail_floor_mv(fields).value.v) + ", estimated cap at current boost " +
                      fixed0(rail::rail_ceiling_mv(fields).value.v) +
                      " mV; absolute requests are relative to this session's quantized first read" + note};
}

Outcome Rails::set_limits_raw(std::int32_t rail, const RailRawRequest& limits, bool write_enabled, bool xoc,
                              RailWriterTrace& trace) {
    // Replay exact saved signed fields; request bounds still apply.
    const std::optional<RailLimits> current = this->limits();
    const bool valid_rail = rail >= 0 && static_cast<std::size_t>(rail) < kRailCount;
    const RailLimitRow* row = current && valid_rail && (*current)[static_cast<std::size_t>(rail)]
                                  ? &*(*current)[static_cast<std::size_t>(rail)]
                                  : nullptr;
    if (!row || !row->base) return {false, "selected rail has no readable current-card reference"};
    RailLimitRequest request;
    request.count = limits.count;
    request.order = limits.order;
    for (std::size_t i = 0; i < limits.count; ++i) {
        const std::size_t key = limits.order[i];
        request.value_mv[key] = (*row->base)[key] + limits.uv[key] / 1000.0;  // bases[k] + v / 1000.0
    }
    return set_limits(rail, request, write_enabled, xoc, trace);
}

Outcome Rails::reset_limits(std::optional<std::int32_t> rail, const RailResetRequest& fields, RailWriterTrace& trace) {
    // Restore selected fields to their exact first-read control values: this session's initial state,
    // which may already include another application's tuning. It is not a factory reset.
    if (!rail) {
        this->limits();
        const std::optional<RailProfile> p = profile();
        std::vector<std::int32_t> initial;
        if (p) {
            for (std::size_t r : p->order) {
                if (p->poweron[r]) initial.push_back(static_cast<std::int32_t>(r));
            }
        }
        if (initial.size() > 1) {
            bool ok = true;
            std::string message;
            for (std::size_t i = 0; i < initial.size(); ++i) {
                const Outcome one = reset_limits(initial[i], fields, trace);
                ok = ok && one.ok;
                if (i) message += "; ";
                message += one.message;
            }
            return {ok, message};
        }
    }
    if (!limits_supported(std::nullopt)) return {false, "no readable initial rail state is available for restoration"};
    const std::optional<RailProfile> p = profile();
    const std::optional<RailLimits> cur = this->limits();
    if (!cur) return {false, "cannot read the current limits"};
    // supported() has just found the profile; the architecture it depends on is cached once recognized.
    if (!p) return {false, "no readable initial rail state is available for restoration"};
    std::vector<std::size_t> rails;
    bool present = true;
    if (!rail) {
        for (std::size_t r = 0; r < kRailCount; ++r) {
            if ((*cur)[r] && p->bases[r]) rails.push_back(r);
        }
    } else if (*rail >= 0 && static_cast<std::size_t>(*rail) < kRailCount) {
        rails.push_back(static_cast<std::size_t>(*rail));
    } else {
        present = false;  // a key that names no rail is in neither dict
    }
    // any(r not in cur or r not in profile["fields"] or not supported(r)), stopping at the first
    for (std::size_t i = 0; present && i < rails.size(); ++i) {
        const std::size_t r = rails[i];
        present = (*cur)[r] && p->bases[r] && limits_supported(static_cast<std::int32_t>(r));
    }
    if (rails.empty() || !present) return {false, "requested rail is not present on this GPU"};
    const std::uint32_t keys = fields.has_fields ? fields.mask : (1u << rail::kFieldCount) - 1;
    // `if bad:` tests the set, so the key "" alone refuses with an empty text
    if (fields.bad) return {false, "not a rail limit: " + *fields.bad};
    const std::optional<std::int32_t> boost = telemetry_.read_voltage_boost();
    if (!boost) return {false, "cannot preserve the current voltage boost"};
    RailRecords recs;
    for (std::size_t r : rails) {
        RailRecord record = record_of(r, *(*cur)[r]);
        for (std::size_t k = 0; k < rail::kFieldCount; ++k) {
            // a rail in profile["fields"] has its first-read record: both are set together
            if (keys >> k & 1u) record.values[k] = (*p->poweron[r])[k];
        }
        recs.records[recs.count++] = record;
    }
    const RailWriteOutcome w = write_traced(recs, trace);
    if (!w.ok || (w.status && *w.status))
        return {false, "reset refused (NV_STATUS 0x" + hex_upper(w.status.value_or(0u)) + ")"};
    const RailVerifyOutcome v = verify_records(expected_records(*cur, recs), boost);
    if (!v.error.empty()) return {false, v.error};
    for (std::size_t r : rails) adopt_verified_hold_fields(static_cast<std::int32_t>(r), keys, *v.back);
    if (hold_.present) {
        bool in_rails = false;
        for (std::size_t r : rails) in_rails = in_rails || hold_.rail == static_cast<std::int32_t>(r);
        bool subset = true;
        for (std::size_t k = 0; k < rail::kFieldCount; ++k) subset = subset && (!hold_.written_uv[k] || (keys >> k & 1u));
        // The raise was overwritten by first-read values; restoring its "prior" values later would
        // write stale limits back over them.
        if (in_rails && subset) hold_ = HoldRecord{};
    }
    std::string what;
    if (!rail && !fields.has_fields) {
        what = "rail limits";
    } else {
        what = std::string(rail_name(rails[0])) + " " + (fields.has_fields ? fields.joined : std::string("limits"));
    }
    return {true, what + " restored to first-read values (not factory defaults)"};
}

}  // namespace druta::backend
