// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/rails.h.
#include <cstring>

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
using druta::backend::RailReason;
static_assert(DC_RAILS_REASON_NONE == static_cast<int>(RailReason::none), "reasons");
static_assert(DC_RAILS_REASON_NO_ARCHITECTURE == static_cast<int>(RailReason::no_architecture), "reasons");
static_assert(DC_RAILS_REASON_UNKNOWN_ARCHITECTURE == static_cast<int>(RailReason::unknown_architecture), "reasons");
static_assert(DC_RAILS_REASON_NO_CONTROL == static_cast<int>(RailReason::no_control), "reasons");
static_assert(DC_RAILS_REASON_INVALID_STATE == static_cast<int>(RailReason::invalid_state), "reasons");
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
        auto rows = handle->rails.limits();
        clear(out);
        if (!rows) return DC_OK;
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

}  // extern "C"
