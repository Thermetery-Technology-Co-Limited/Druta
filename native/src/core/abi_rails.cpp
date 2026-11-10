// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/rails.h.
#include <cstring>
#include <limits>
#include <optional>

#include "backend/rail_limits.h"
#include "backend/rails.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::RailGetter;

static_assert(DC_RAILS_COUNT == druta::backend::kRailCount, "rail count");
static_assert(DC_RAILS_BLOCK_SIZE == druta::backend::kRailBlockSize, "block size");
static_assert(DC_RAILS_CONTROL_ID == druta::backend::nvapi_id::VoltRailsCtlGet, "control getter");
static_assert(DC_RAILS_ABS_ID == druta::backend::nvapi_id::VoltRailsAbs, "live getter");
static_assert(DC_RAILS_CONTROL_VERSION == druta::backend::kRailControlVersion, "control version");
static_assert(DC_RAILS_LIVE_VERSION == druta::backend::kLiveRailVersion, "live version");
static_assert(DC_RAILS_CONTROL == static_cast<int>(RailGetter::Control), "control index");
static_assert(DC_RAILS_ABS == static_cast<int>(RailGetter::Abs), "live index");
static_assert(DC_RAILS_LIMIT_FIELDS == druta::backend::rail::kFieldCount, "field count");
static_assert(DC_RAILS_LIMIT_MIN_MV == druta::backend::kVoltLimitMinMv, "min");
static_assert(DC_RAILS_LIMIT_MAX_MV == druta::backend::kVoltLimitMaxMv, "max");
static_assert(DC_RAILS_LIMIT_XOC_MAX_MV == druta::backend::kVoltLimitXocMaxMv, "xoc max");
// The longest composed message (a two-rail reset: two "<rail name> <joined> restored ..." texts and
// "; ") must fit dc_outcome.message with its NUL.
static_assert(2 * (sizeof("core rail (NVVDD) ") - 1 + DC_RAILS_TEXT_MAX +
                   sizeof(" restored to first-read values (not factory defaults)") - 1) + 2 <
                  sizeof(((dc_outcome*)nullptr)->message),
              "message capacity");
using druta::backend::RailReason;
static_assert(DC_RAILS_REASON_NONE == static_cast<int>(RailReason::none), "reasons");
static_assert(DC_RAILS_REASON_NO_ARCHITECTURE == static_cast<int>(RailReason::no_architecture), "reasons");
static_assert(DC_RAILS_REASON_UNKNOWN_ARCHITECTURE == static_cast<int>(RailReason::unknown_architecture), "reasons");
static_assert(DC_RAILS_REASON_NO_CONTROL == static_cast<int>(RailReason::no_control), "reasons");
static_assert(DC_RAILS_REASON_INVALID_STATE == static_cast<int>(RailReason::invalid_state), "reasons");
static_assert(DC_RAILS_KEY_PLAIN == static_cast<int>(druta::backend::RailKey::plain), "keys");
static_assert(DC_RAILS_KEY_INT == static_cast<int>(druta::backend::RailKey::other_int), "keys");
static_assert(DC_RAILS_KEY_NO_SHIFT == static_cast<int>(druta::backend::RailKey::no_shift), "keys");
static_assert(DC_RAILS_REASON_NO_REFERENCE == static_cast<int>(RailReason::no_reference), "reasons");

// No exception crosses the C boundary; only allocation can throw here.
template <typename F>
dc_status guarded(F&& body) {
    try {
        return body();
    } catch (...) {
    }
    return DC_E_FAILED;
}

// Copy a NUL-terminated text into a fixed field, truncating.
template <std::size_t N>
void put_text(char (&field)[N], const char* text) {
    std::size_t n = text ? std::strlen(text) : 0;
    if (n >= N) n = N - 1;
    if (n) std::memcpy(field, text, n);
    field[n] = '\0';
}

template <typename A>
void put_fields(double (&out)[4], const A& values) {
    for (std::size_t k = 0; k < 4; ++k) out[k] = values[k];
}

// Zero the caller's struct, keeping its size.
template <typename T>
void clear(T* out) {
    std::memset(out, 0, sizeof *out);
    out->size = sizeof(T);
}

// A dc_rails_limit_set from read_volt_rail_limits' rows (present 0 for None).
void put_limits(dc_rails_limit_set* out, const std::optional<druta::backend::RailLimits>& rows) {
    clear(out);
    if (!rows) return;
    for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
        const auto& row = (*rows)[r];
        if (!row) continue;
        out->present |= 1u << r;
        dc_rails_limit_row& e = out->rails[r];
        e.type = row->deltas.type;
        put_fields(e.fields, std::array<double, 4>{row->deltas.reliability, row->deltas.alt_reliability,
                                                    row->deltas.overvoltage, row->deltas.vmin});
        e.has_base = row->base ? 1 : 0;
        if (row->base) put_fields(e.base, *row->base);
        e.absolute_ok = row->absolute_ok ? 1 : 0;
        e.boost_mv = row->boost_mv;
        e.has_effective = row->effective_mv ? 1 : 0;
        e.effective_mv = row->effective_mv.value_or(0.0);
    }
}

// The caller's records, or false for DC_E_ARGUMENT.
// verify: also refuse a record carrying more than the four values `wanted` is printed from.
bool get_records(const dc_rails_records* in, druta::backend::RailRecords& out, bool verify) {
    if (in->size != sizeof(dc_rails_records) || in->count > DC_RAILS_COUNT) return false;
    out.count = in->count;
    out.foreign = in->foreign != 0;
    unsigned seen = 0;
    for (std::size_t i = 0; i < in->count; ++i) {
        const dc_rails_record& r = in->records[i];
        if ((r.rail != 0 && r.rail != 1) || (seen >> r.rail & 1u)) return false;
        if (r.kind != DC_RAILS_VALUES_LIST && r.kind != DC_RAILS_VALUES_TUPLE) return false;
        if (r.key_kind != DC_RAILS_KEY_PLAIN && r.key_kind != DC_RAILS_KEY_INT && r.key_kind != DC_RAILS_KEY_NO_SHIFT)
            return false;
        if (verify && r.count > 4) return false;
        seen |= 1u << r.rail;
        druta::backend::RailRecord& e = out.records[i];
        e.rail = r.rail;
        e.key = static_cast<druta::backend::RailKey>(r.key_kind);
        e.count = r.count;
        e.is_list = r.kind == DC_RAILS_VALUES_LIST;
        e.exact = r.exact != 0;
        for (std::size_t k = 0; k < 4; ++k) e.values[k] = k < r.count ? r.values[k] : 0;
    }
    return true;
}

// A caller text as presence + pointer + byte length: exactly `length` bytes (NUL bytes included),
// nullopt when absent. Refused above DC_RAILS_TEXT_MAX bytes, or NULL with a nonzero length.
bool get_text(int32_t present, const char* text, uint32_t length, std::optional<std::string>& out) {
    out.reset();
    if (!present) return true;
    if (length > DC_RAILS_TEXT_MAX || (!text && length)) return false;
    out.emplace(text ? text : "", length);
    return true;
}

// Field indices in keyword order: distinct, below 4, exactly the passed fields.
template <typename Passed>
bool get_order(uint32_t count, const uint32_t (&order)[4], Passed passed, std::size_t& out_count,
               std::array<std::size_t, 4>& out) {
    if (count > DC_RAILS_LIMIT_FIELDS) return false;
    unsigned seen = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const uint32_t k = order[i];
        if (k >= DC_RAILS_LIMIT_FIELDS || (seen >> k & 1u) || !passed(k)) return false;
        seen |= 1u << k;
        out[i] = k;
    }
    for (uint32_t k = 0; k < DC_RAILS_LIMIT_FIELDS; ++k) {
        if (passed(k) && !(seen >> k & 1u)) return false;
    }
    out_count = count;
    return true;
}

// The writer trace into the caller's struct.
void put_writes(const druta::backend::RailWriterTrace& trace, dc_rails_setter_writes* writes) {
    clear(writes);
    writes->calls = trace.calls;
    writes->attributes = trace.attributes ? 1 : 0;
    writes->last_attributes = trace.last_attributes ? 1 : 0;
    writes->result.size = sizeof(dc_rail_write_result);
    if (trace.attributes) druta::abi::put_rail_write_result(&writes->result, trace.result);
}

// One setter call under the handle lock. The trace is the entry's own, so whatever the setter's
// _write_rail_records calls left in it reaches `writes` even when a later step throws (only
// allocation can): the status is then DC_E_FAILED, as guarded() gives, but the writer's attributes
// still describe the write that ran.
template <typename Setter>
dc_status run_setter(dc_gpu* handle, Setter&& setter, dc_outcome* out, dc_rails_setter_writes* writes) {
    druta::backend::RailWriterTrace trace;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        druta::abi::put_outcome(setter(trace), out);
    } catch (...) {
        put_writes(trace, writes);  // copies into fixed fields only: does not throw
        throw;                      // guarded() turns it into DC_E_FAILED
    }
    put_writes(trace, writes);
    return DC_OK;
}

// ---- Increment R6 ------------------------------------------------------------------------------

using druta::backend::HoldRecord;

static_assert(DC_RAILS_HOLD_OK == 1 && DC_RAILS_HOLD_NONE == 0 && DC_RAILS_HOLD_FAILED == -1, "hold ok");
static_assert(DC_RAILS_HOLD_HOOK_PROCEED == static_cast<int>(druta::backend::Tracked::proceed), "hook");
static_assert(DC_RAILS_HOLD_HOOK_FALSE == static_cast<int>(druta::backend::Tracked::refused), "hook");
static_assert(DC_RAILS_HOLD_HOOK_RAISED == static_cast<int>(druta::backend::Tracked::raised), "hook");
static_assert(DC_RAILS_HOLD_HOOK_ABORT == static_cast<int>(druta::backend::Tracked::abort), "hook");
static_assert(DC_RAILS_HOLD_BAD_HOLD == druta::backend::kHoldBadHold, "invalid bits");
static_assert(DC_RAILS_HOLD_BAD_MARGIN == druta::backend::kHoldBadMargin, "invalid bits");
static_assert(sizeof(((dc_rails_hold_outcome*)nullptr)->message) == DC_RAILS_HOLD_MESSAGE_MAX, "message");
static_assert(druta::backend::rail::kHeadroomFieldCount == 3, "headroom fields");
static_assert(DC_RAILS_HOLD_KEYS_ALL == druta::backend::kHoldKeysAll, "hold keys");
static_assert(DC_RAILS_HOLD_KEY_HOLD == druta::backend::kHoldKeyHold && DC_RAILS_HOLD_KEY_MARGIN == druta::backend::kHoldKeyMargin &&
                  DC_RAILS_HOLD_KEY_CEILING == druta::backend::kHoldKeyCeiling &&
                  DC_RAILS_HOLD_KEY_BOOST == druta::backend::kHoldKeyBoost &&
                  DC_RAILS_HOLD_KEY_LIVE_OFFSET == druta::backend::kHoldKeyLiveOffset &&
                  DC_RAILS_HOLD_KEY_GRAIN == druta::backend::kHoldKeyGrain &&
                  DC_RAILS_HOLD_KEY_INPUT == druta::backend::kHoldKeyInput &&
                  DC_RAILS_HOLD_KEY_PRIOR == druta::backend::kHoldKeyPrior &&
                  DC_RAILS_HOLD_KEY_WRITTEN == druta::backend::kHoldKeyWritten,
              "hold keys");
static_assert(DC_RAILS_HOLD_KEY_ERROR_WRITTEN == druta::backend::kHoldKeyErrorWritten + 1 &&
                  DC_RAILS_HOLD_KEY_ERROR_PRIOR == druta::backend::kHoldKeyErrorPrior + 1,
              "key errors");
// The longest apply text (the above-the-ceiling refusal, four %.2f of at most 309 digits each, or a
// two-rail failure text carrying the writer's error) fits the message with its NUL.
static_assert(DC_RAILS_HOLD_MESSAGE_MAX > 4 * 320 + 400 + 2 * 1024, "message capacity");

void put_hold(const HoldRecord& h, dc_rails_hold_record* out) {
    clear(out);
    if (!h.present) return;
    out->present = 1;
    out->rail = h.rail;
    out->keys = h.keys;
    for (std::size_t k = 0; k < DC_RAILS_LIMIT_FIELDS; ++k) {
        out->has_written[k] = h.written_uv[k] ? 1 : 0;
        out->written_uv[k] = h.written_uv[k].value_or(0);
        out->has_prior[k] = h.prior_uv[k] ? 1 : 0;
        out->prior_uv[k] = h.prior_uv[k].value_or(0);
    }
    for (std::size_t k = 0; k < 3; ++k) {
        out->has_input[k] = h.input_uv[k] ? 1 : 0;
        out->input_uv[k] = h.input_uv[k].value_or(0);
    }
    out->has_grain = h.grain_mv ? 1 : 0;
    out->has_raised_ceiling = h.raised_ceiling_mv ? 1 : 0;
    out->hold_mv = h.hold_mv;
    out->margin_mv = h.margin_mv;
    out->ceiling_mv = h.ceiling_mv;
    out->boost_mv = h.boost_mv;
    out->live_offset_mv = h.live_offset_mv;
    out->grain_mv = h.grain_mv.value_or(0.0);
    out->raised_ceiling_mv = h.raised_ceiling_mv.value_or(0.0);
}

HoldRecord get_hold(const dc_rails_hold_record& in) {
    HoldRecord h;
    if (!in.present) return h;
    h.present = true;
    h.rail = in.rail;
    h.keys = in.keys;
    for (std::size_t k = 0; k < DC_RAILS_LIMIT_FIELDS; ++k) {
        if (in.has_written[k]) h.written_uv[k] = in.written_uv[k];
        if (in.has_prior[k]) h.prior_uv[k] = in.prior_uv[k];
    }
    for (std::size_t k = 0; k < 3; ++k) {
        if (in.has_input[k]) h.input_uv[k] = in.input_uv[k];
    }
    // a float key the record does not carry is NaN (the header's contract), whatever the caller left
    // in the slot, so every reader treats it as Python treats the missing key
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const auto keyed = [&](std::uint32_t bit, double value) { return (in.keys & bit) ? value : nan; };
    h.hold_mv = keyed(DC_RAILS_HOLD_KEY_HOLD, in.hold_mv);
    h.margin_mv = keyed(DC_RAILS_HOLD_KEY_MARGIN, in.margin_mv);
    h.ceiling_mv = keyed(DC_RAILS_HOLD_KEY_CEILING, in.ceiling_mv);
    h.boost_mv = keyed(DC_RAILS_HOLD_KEY_BOOST, in.boost_mv);
    h.live_offset_mv = keyed(DC_RAILS_HOLD_KEY_LIVE_OFFSET, in.live_offset_mv);
    if (in.has_grain) h.grain_mv = in.grain_mv;
    if (in.has_raised_ceiling) h.raised_ceiling_mv = in.raised_ceiling_mv;
    return h;
}

void put_hold_outcome(const druta::backend::HoldOutcome& o, dc_rails_hold_outcome* out) {
    clear(out);
    out->ok = o.ok;
    out->key_error = o.key_error ? static_cast<int32_t>(*o.key_error) + 1 : 0;
    out->aborted = o.aborted ? 1 : 0;
    put_text(out->message, o.message.c_str());
}

// read_vf_curve()'s points' volt_mv over the handle's own curve reader (the handle lock is held).
druta::backend::CurveVolts curve_volts(dc_gpu* handle) {
    return [handle]() -> std::optional<std::vector<double>> {
        const druta::backend::CurveResult result = handle->curve().read_vf_curve();
        if (!result.points) return std::nullopt;
        std::vector<double> volts;
        volts.reserve(result.points->size());
        for (const druta::backend::VfPoint& p : *result.points) volts.push_back(p.volt_mv);
        return volts;
    };
}

// One hold call under the handle lock, its writer trace reaching `writes` as run_setter's does.
template <typename Call>
dc_status run_hold(dc_gpu* handle, Call&& call, dc_rails_hold_outcome* out, dc_rails_setter_writes* writes) {
    druta::backend::RailWriterTrace trace;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        put_hold_outcome(call(trace), out);
    } catch (...) {
        put_writes(trace, writes);
        throw;
    }
    put_writes(trace, writes);
    return DC_OK;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_rails_blocks(dc_gpu* handle, int32_t function, uint32_t version, dc_rails_block_set* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_block_set)) return DC_E_ARGUMENT;
    if (function != DC_RAILS_CONTROL && function != DC_RAILS_ABS) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::RailBlocks b = handle->rails.blocks(static_cast<RailGetter>(function), version);
        clear(out);
        for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
            if (!b.rails[r]) continue;
            out->present |= 1u << r;
            std::memcpy(out->blocks[r], b.rails[r]->data(), DC_RAILS_BLOCK_SIZE);
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_deltas(dc_gpu* handle, dc_rails_delta_set* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_delta_set)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        auto rows = handle->rails.deltas();
        clear(out);
        for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
            if (!rows[r]) continue;
            out->present |= 1u << r;
            dc_rails_delta& d = out->rails[r];
            d.type = rows[r]->type;
            d.reliability = rows[r]->reliability;
            d.alt_reliability = rows[r]->alt_reliability;
            d.overvoltage = rows[r]->overvoltage;
            d.vmin = rows[r]->vmin;
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_state(dc_gpu* handle, dc_rails_state_set* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_state_set)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        auto rows = handle->rails.state();
        clear(out);
        if (!rows) return DC_OK;
        for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
            const auto& row = (*rows)[r];
            if (!row) continue;
            out->present |= 1u << r;
            dc_rails_state_row& s = out->rails[r];
            s.type = row->type;
            s.boost_mv = row->boost_mv;
            s.live = row->live;
            s.reliability = row->reliability;
            s.alt_reliability = row->alt_reliability;
            s.overvoltage = row->overvoltage;
            s.effective = row->effective;
            s.vmin = row->vmin;
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_live_mv(dc_gpu* handle, int32_t rail, int32_t* has, double* mv) {
    if (!handle || !has || !mv) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        std::optional<double> live = handle->rails.live_mv(rail);
        *has = live ? 1 : 0;
        *mv = live.value_or(0.0);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_caches_get(dc_gpu* handle, dc_rails_caches* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_caches)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const druta::backend::RailCaches& c = handle->rails.caches();
        clear(out);
        for (std::size_t f = 0; f < druta::backend::kRailGetters; ++f) {
            out->has_known[f] = c.known[f] ? 1 : 0;
            out->known[f] = c.known[f].value_or(0u);
            out->has_version[f] = c.versions[f] ? 1 : 0;
            out->version[f] = c.versions[f].value_or(0u);
            for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
                out->has_retry[f][r] = c.retry_after[f][r] ? 1 : 0;
                out->retry_after[f][r] = c.retry_after[f][r].value_or(0.0);
                if (!c.diagnostics[f][r]) continue;
                const druta::backend::RailDiagnostic& d = *c.diagnostics[f][r];
                dc_rails_diagnostic& e = out->diagnostics[f][r];
                e.present = 1;
                e.version = d.version;
                e.status = d.status;
                e.valid = d.valid ? 1 : 0;
                e.attempts = d.attempts;
            }
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_refresh(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        handle->rails.refresh();
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_limits(dc_gpu* handle, dc_rails_limit_set* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_limit_set)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        put_limits(out, handle->rails.limits());
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_profile(dc_gpu* handle, dc_rails_profile_set* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_profile_set)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        auto p = handle->rails.profile();
        clear(out);
        if (!p) return DC_OK;
        out->has_profile = 1;
        for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
            out->has_reference[r] = p->bases[r] ? 1 : 0;
            if (p->bases[r]) put_fields(out->bases[r], *p->bases[r]);
            out->has_initial[r] = p->poweron[r] ? 1 : 0;
            if (p->poweron[r]) {
                for (std::size_t k = 0; k < 4; ++k) out->initial[r][k] = (*p->poweron[r])[k];
            }
        }
        out->order_count = static_cast<uint32_t>(p->order.size());
        for (std::size_t i = 0; i < p->order.size() && i < DC_RAILS_COUNT; ++i)
            out->order[i] = static_cast<uint32_t>(p->order[i]);
        out->control_version = p->control_version;
        out->settle_vmin = p->settle_vmin ? 1 : 0;
        out->min_mv = p->min_mv;
        out->max_mv = p->max_mv;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_diagnostics(dc_gpu* handle, dc_rails_diagnosis* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_diagnosis)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::RailDiagnosis d = handle->rails.diagnostics();
        clear(out);
        out->has_architecture = d.architecture ? 1 : 0;
        out->architecture = d.architecture.value_or(0u);
        for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
            if (!d.present[r]) continue;
            dc_rails_diagnostic_rail& e = out->rails[r];
            e.present = 1;
            e.available = d.reasons[r] == druta::backend::RailReason::none ? 1 : 0;
            e.reason_code = static_cast<int32_t>(d.reasons[r]);
            put_text(e.reason, druta::backend::rail_reason_text(d.reasons[r]));
        }
        out->available = d.available ? 1 : 0;
        out->reason_code = static_cast<int32_t>(d.reason);
        put_text(out->reason, druta::backend::rail_reason_text(d.reason));
        out->has_architecture_source = d.architecture_source ? 1 : 0;
        put_text(out->architecture_source, d.architecture_source);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_limits_supported(dc_gpu* handle, int32_t has_rail, int32_t rail, int32_t* out) {
    if (!handle || !out) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        std::optional<std::int32_t> which;
        if (has_rail) which = rail;
        *out = handle->rails.limits_supported(which) ? 1 : 0;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_limit_fields(dc_gpu* handle, int32_t rail, uint32_t* count) {
    if (!handle || !count) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        *count = static_cast<uint32_t>(handle->rails.limit_fields(rail));
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_stock_limit_mv(dc_gpu* handle, int32_t rail, int32_t field, int32_t* has, double* mv) {
    if (!handle || !has || !mv) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::RailStockLimit s = handle->rails.stock_limit_mv(rail, field);
        *has = static_cast<int32_t>(s.kind);
        *mv = s.kind == druta::backend::RailStockLimit::Kind::value ? s.mv : 0.0;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_limits_mv(dc_gpu* handle, dc_rails_limits_mv_set* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_limits_mv_set)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        auto rows = handle->rails.limits_mv();
        clear(out);
        if (!rows) return DC_OK;
        for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
            if (!(*rows)[r]) continue;
            out->present |= 1u << r;
            put_fields(out->mv[r], *(*rows)[r]);
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_write_records(dc_gpu* handle, const dc_rails_records* records,
                                                dc_rails_write_outcome* out) {
    if (!handle || !records || !out || out->size != sizeof(dc_rails_write_outcome)) return DC_E_ARGUMENT;
    druta::backend::RailRecords in;
    if (!get_records(records, in, false)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        // Lock order (DESIGN-TRANSPORT 3.8): the handle lock here, the hook mutex in write_records.
        druta::LockGuard guard(handle->gpu.lock());
        const druta::backend::RailWriteOutcome r = handle->rails.write_records(in);
        clear(out);
        out->ok = r.ok ? 1 : 0;
        out->has_status = r.status ? 1 : 0;
        out->status = r.status.value_or(0u);
        out->attributes = r.attributes ? 1 : 0;
        out->writes = r.writes;
        out->type_error = r.type_error ? 1 : 0;
        out->result.size = sizeof(dc_rail_write_result);
        if (r.attributes) druta::abi::put_rail_write_result(&out->result, r.result);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_verify_records(dc_gpu* handle, const dc_rails_records* records, int32_t has_boost,
                                                 int32_t boost, dc_rails_verify_result* out) {
    if (!handle || !records || !out || out->size != sizeof(dc_rails_verify_result)) return DC_E_ARGUMENT;
    druta::backend::RailRecords in;
    if (!get_records(records, in, true)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        std::optional<std::int32_t> wanted;
        if (has_boost) wanted = boost;
        const druta::backend::RailVerifyOutcome r = handle->rails.verify_records(in, wanted);
        clear(out);
        out->has_back = r.back ? 1 : 0;
        put_text(out->error, r.error.c_str());
        put_limits(&out->back, r.back);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_set_limits(dc_gpu* handle, int32_t rail, const dc_rails_limit_request* limits,
                                             int32_t write_enabled, int32_t xoc, dc_outcome* out,
                                             dc_rails_setter_writes* writes) {
    if (!handle || !limits || !out || !writes || limits->size != sizeof(dc_rails_limit_request) ||
        writes->size != sizeof(dc_rails_setter_writes))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::backend::RailLimitRequest request;
        const auto passed = [&](uint32_t k) { return limits->fields[k].passed != 0; };
        if (!get_order(limits->count, limits->order, passed, request.count, request.order)) return DC_E_ARGUMENT;
        if (!get_text(limits->has_unknown, limits->unknown, limits->unknown_length, request.unknown))
            return DC_E_ARGUMENT;
        for (std::size_t k = 0; k < DC_RAILS_LIMIT_FIELDS; ++k) {
            if (limits->fields[k].passed && limits->fields[k].has_value) request.value_mv[k] = limits->fields[k].value_mv;
        }
        return run_setter(
            handle,
            [&](druta::backend::RailWriterTrace& trace) {
                return handle->rails.set_limits(rail, request, write_enabled != 0, xoc != 0, trace);
            },
            out, writes);
    });
}

DC_API dc_status DC_CALL dc_rails_set_limits_raw(dc_gpu* handle, int32_t rail, const dc_rails_raw_request* limits,
                                                 int32_t write_enabled, int32_t xoc, dc_outcome* out,
                                                 dc_rails_setter_writes* writes) {
    if (!handle || !limits || !out || !writes || limits->size != sizeof(dc_rails_raw_request) ||
        writes->size != sizeof(dc_rails_setter_writes))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::backend::RailRawRequest request;
        const auto passed = [&](uint32_t k) { return limits->passed[k] != 0; };
        if (!get_order(limits->count, limits->order, passed, request.count, request.order)) return DC_E_ARGUMENT;
        for (std::size_t k = 0; k < DC_RAILS_LIMIT_FIELDS; ++k) request.uv[k] = limits->uv[k];
        return run_setter(
            handle,
            [&](druta::backend::RailWriterTrace& trace) {
                return handle->rails.set_limits_raw(rail, request, write_enabled != 0, xoc != 0, trace);
            },
            out, writes);
    });
}

DC_API dc_status DC_CALL dc_rails_reset_limits(dc_gpu* handle, int32_t has_rail, int32_t rail,
                                               const dc_rails_reset_request* fields, dc_outcome* out,
                                               dc_rails_setter_writes* writes) {
    if (!handle || !fields || !out || !writes || fields->size != sizeof(dc_rails_reset_request) ||
        writes->size != sizeof(dc_rails_setter_writes) || fields->field_mask >> DC_RAILS_LIMIT_FIELDS)
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::backend::RailResetRequest request;
        request.has_fields = fields->has_fields != 0;
        request.mask = fields->field_mask;
        std::optional<std::string> joined;
        if (!get_text(fields->has_bad, fields->bad, fields->bad_length, request.bad) ||
            !get_text(fields->has_joined, fields->joined, fields->joined_length, joined))
            return DC_E_ARGUMENT;
        request.joined = joined.value_or(std::string());
        std::optional<std::int32_t> which;
        if (has_rail) which = rail;
        return run_setter(
            handle,
            [&](druta::backend::RailWriterTrace& trace) { return handle->rails.reset_limits(which, request, trace); },
            out, writes);
    });
}

DC_API dc_status DC_CALL dc_rails_hold_record_get(dc_gpu* handle, dc_rails_hold_record* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_hold_record)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        put_hold(handle->rails.hold_record(), out);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_record_set(dc_gpu* handle, const dc_rails_hold_record* record) {
    if (!handle || !record || record->size != sizeof(dc_rails_hold_record) || (record->keys & ~DC_RAILS_HOLD_KEYS_ALL))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        const HoldRecord h = get_hold(*record);
        druta::LockGuard guard(handle->gpu.lock());
        handle->rails.set_hold_record(h);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_state_get(dc_gpu* handle, dc_rails_hold_state* out) {
    if (!handle || !out || out->size != sizeof(dc_rails_hold_state)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const druta::backend::HoldState& st = handle->rails.hold_state();
        clear(out);
        out->has_grain = st.grain ? 1 : 0;
        out->grain = st.grain.value_or(0.0);
        out->has_offset = st.offset_mv ? 1 : 0;
        out->offset_mv = st.offset_mv.value_or(0.0);
        out->unread = st.unread ? 1 : 0;
        out->live_trip = st.live_trip ? 1 : 0;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_state_set(dc_gpu* handle, uint32_t mask, const dc_rails_hold_state* state) {
    if (!handle || !state || state->size != sizeof(dc_rails_hold_state) || (mask & ~DC_RAILS_HOLD_STATE_ALL))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        // only the named fields change, under the handle lock: assigning one attribute never rewrites
        // another thread's update of the others (each is its own attribute in Python)
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::HoldState st = handle->rails.hold_state();
        if (mask & DC_RAILS_HOLD_STATE_GRAIN)
            st.grain = state->has_grain ? std::optional<double>(state->grain) : std::nullopt;
        if (mask & DC_RAILS_HOLD_STATE_OFFSET)
            st.offset_mv = state->has_offset ? std::optional<double>(state->offset_mv) : std::nullopt;
        if (mask & DC_RAILS_HOLD_STATE_UNREAD) st.unread = state->unread != 0;
        if (mask & DC_RAILS_HOLD_STATE_LIVE_TRIP) st.live_trip = state->live_trip != 0;
        handle->rails.set_hold_state(st);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_apply(dc_gpu* handle, double hold_mv, double margin_mv, uint32_t invalid,
                                             int32_t rail, int32_t xoc, dc_rails_before_write_fn before_write,
                                             void* context, dc_rails_hold_outcome* out,
                                             dc_rails_setter_writes* writes) {
    if (!handle || !out || !writes || out->size != sizeof(dc_rails_hold_outcome) ||
        writes->size != sizeof(dc_rails_setter_writes) ||
        (invalid & ~(DC_RAILS_HOLD_BAD_HOLD | DC_RAILS_HOLD_BAD_MARGIN)))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::backend::BeforeWrite hook;
        if (before_write) {
            hook = [before_write, context](const HoldRecord& planned, std::string& error) {
                dc_rails_hold_record record;
                record.size = sizeof record;
                put_hold(planned, &record);
                char text[DC_RAILS_HOLD_ERROR_MAX];
                text[0] = '\0';
                const int32_t answer = before_write(context, &record, text, sizeof text);
                text[sizeof text - 1] = '\0';
                switch (answer) {
                    case DC_RAILS_HOLD_HOOK_PROCEED:
                        return druta::backend::Tracked::proceed;
                    case DC_RAILS_HOLD_HOOK_RAISED:
                        error = text;
                        return druta::backend::Tracked::raised;
                    case DC_RAILS_HOLD_HOOK_ABORT:
                        return druta::backend::Tracked::abort;
                    default:  // FALSE, and 0 or any unknown answer: fail closed (no marker, no raise)
                        return druta::backend::Tracked::refused;
                }
            };
        }
        const druta::backend::CurveVolts volts = curve_volts(handle);
        return run_hold(
            handle,
            [&](druta::backend::RailWriterTrace& trace) {
                return handle->rails.apply_hold(hold_mv, margin_mv, invalid, rail, xoc != 0, volts, hook, trace);
            },
            out, writes);
    });
}

DC_API dc_status DC_CALL dc_rails_hold_restore(dc_gpu* handle, int32_t force, dc_rails_hold_outcome* out,
                                               dc_rails_setter_writes* writes) {
    if (!handle || !out || !writes || out->size != sizeof(dc_rails_hold_outcome) ||
        writes->size != sizeof(dc_rails_setter_writes))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        return run_hold(
            handle, [&](druta::backend::RailWriterTrace& trace) { return handle->rails.restore_hold(force != 0, trace); },
            out, writes);
    });
}

DC_API dc_status DC_CALL dc_rails_hold_live_ok(dc_gpu* handle, int32_t has_record, const dc_rails_hold_record* record,
                                               int32_t has_live, double live_mv, dc_outcome* out) {
    if (!handle || !out || (has_record && (!record || record->size != sizeof(dc_rails_hold_record))))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        std::optional<HoldRecord> given;
        if (has_record) given = get_hold(*record);
        std::optional<double> live;
        if (has_live) live = live_mv;
        druta::LockGuard guard(handle->gpu.lock());
        druta::abi::put_outcome(handle->rails.hold_live_ok(given ? &*given : nullptr, live), out);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_grain_mv(dc_gpu* handle, int32_t* has, double* grain_mv) {
    if (!handle || !has || !grain_mv) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const std::optional<double> grain = handle->rails.hold_grain_mv(curve_volts(handle));
        *has = grain ? 1 : 0;
        *grain_mv = grain.value_or(0.0);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_architecture(dc_gpu* handle, int32_t* supported) {
    if (!handle || !supported) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const auto arch = handle->telemetry.arch();
        *supported = druta::backend::rail::volt_rail_architecture(arch.has_value(), arch.value_or(0));
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_rail_unavailable(dc_gpu* handle, int32_t rail, int32_t* unavailable,
                                                        int32_t* kind, char* detail, size_t detail_cap,
                                                        size_t* needed) {
    if (!handle || !unavailable || !kind || !needed || (!detail && detail_cap)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const auto got = handle->rails.hold_rail_unavailable(rail);
        *unavailable = got ? 1 : 0;
        *kind = got && !got->transient ? DC_RAILS_HOLD_UNANSWERED : DC_RAILS_HOLD_TRANSIENT;
        const std::string text = got ? got->detail : std::string();
        *needed = text.size();
        if (detail_cap) {
            const std::size_t n = text.size() < detail_cap - 1 ? text.size() : detail_cap - 1;
            if (n) std::memcpy(detail, text.data(), n);
            detail[n] = '\0';
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_hold_overwritten(dc_gpu* handle, int32_t has_limits, const dc_rails_limit_set* limits,
                                                   int32_t* out) {
    if (!handle || !out || (has_limits && (!limits || limits->size != sizeof(dc_rails_limit_set) ||
                                           (limits->present >> DC_RAILS_COUNT))))
        return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        std::optional<druta::backend::RailLimits> given;
        if (has_limits && limits->present) {
            druta::backend::RailLimits rows;
            for (std::size_t r = 0; r < DC_RAILS_COUNT; ++r) {
                if (!(limits->present >> r & 1u)) continue;
                const dc_rails_limit_row& e = limits->rails[r];
                druta::backend::RailLimitRow row;
                row.deltas.type = e.type;
                row.deltas.reliability = e.fields[0];
                row.deltas.alt_reliability = e.fields[1];
                row.deltas.overvoltage = e.fields[2];
                row.deltas.vmin = e.fields[3];
                if (e.has_base) row.base = druta::backend::RailFields{e.base[0], e.base[1], e.base[2], e.base[3]};
                row.absolute_ok = e.absolute_ok != 0;
                row.boost_mv = e.boost_mv;
                if (e.has_effective) row.effective_mv = e.effective_mv;
                rows[r] = row;
            }
            given = rows;
        }
        druta::LockGuard guard(handle->gpu.lock());
        *out = static_cast<int32_t>(handle->rails.hold_overwritten(has_limits ? &given : nullptr));
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_initial_get(dc_gpu* handle, int32_t rail, int32_t* has, int32_t values[4]) {
    if (!handle || !has || !values || (rail != 0 && rail != 1)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const auto& initial = handle->rails.initial(static_cast<std::size_t>(rail));
        *has = initial ? 1 : 0;
        for (std::size_t k = 0; k < DC_RAILS_LIMIT_FIELDS; ++k) values[k] = initial ? (*initial)[k] : 0;
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_rails_initial_set(dc_gpu* handle, int32_t rail, const int32_t values[4], int32_t* replaced) {
    if (!handle || !values || !replaced || (rail != 0 && rail != 1)) return DC_E_ARGUMENT;
    return guarded([&]() -> dc_status {
        const druta::backend::RailInitial v{values[0], values[1], values[2], values[3]};
        druta::LockGuard guard(handle->gpu.lock());
        *replaced = handle->rails.replace_initial(static_cast<std::size_t>(rail), v) ? 1 : 0;
        return DC_OK;
    });
}

}  // extern "C"
