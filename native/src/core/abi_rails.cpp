// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/rails.h.
#include <cstring>

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

// No exception crosses the C boundary; only allocation can throw here.
template <typename F>
dc_status guarded(F&& body) {
    try {
        return body();
    } catch (...) {
    }
    return DC_E_FAILED;
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

}  // extern "C"
