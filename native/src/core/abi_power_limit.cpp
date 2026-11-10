// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/power_limit.h (increment A8).
#include <cstdint>
#include <cstring>
#include <exception>
#include <optional>
#include <string>
#include <vector>

#include "backend/power_limit.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::StaticKey;
using druta::backend::StaticTriState;

static_assert(DC_PL_XOC_MAX_MW == druta::backend::kPowerLimitXocMaxMw, "DC_PL_XOC_MAX_MW");

bool error_ok(const dc_error* error) { return !error || error->size == sizeof(dc_error); }

void put_error(const char* message, int32_t kind, dc_error* out) {
    if (!out) return;
    dc_error e{};
    e.size = sizeof(dc_error);
    e.kind = kind;
    DC_COPY(e.message, std::string(message));
    *out = e;
}

// No exception crosses the C boundary: only allocation can throw here.
template <typename F>
dc_status guarded(dc_error* error, F&& body) {
    try {
        return body();
    } catch (const std::exception& e) {
        try {
            put_error((std::string("Power limit failed: ") + e.what()).c_str(), DC_ERR_TRANSIENT, error);
        } catch (...) {
        }
    } catch (...) {
        try {
            put_error("Power limit failed: unknown exception", DC_ERR_TRANSIENT, error);
        } catch (...) {
        }
    }
    return DC_E_FAILED;
}

using druta::abi::get_statics;
using druta::abi::statics_ok;

// A string into a caller's buffer: NUL-terminated within capacity, *needed the full length + 1.
void put_text(const std::string& text, char* out, size_t capacity, size_t* needed) {
    *needed = text.size() + 1;
    if (!capacity) return;
    const size_t n = text.size() < capacity - 1 ? text.size() : capacity - 1;
    std::memcpy(out, text.data(), n);
    out[n] = '\0';
}

// The back-fill of a live read inside the call: a key the caller passed without a value that the
// call filled is written back (the binding then sets it in its dict).
void put_statics(const StaticTriState& st, dc_cl_statics* out) {
    auto put = [](const StaticKey& key, int32_t& state, int64_t& value) {
        if (key.kind == StaticKey::Kind::value && state != DC_CL_STATIC_VALUE) {
            state = DC_CL_STATIC_VALUE;
            value = key.value;
        }
    };
    put(st.pl_min, out->pl_min_state, out->pl_min_mw);
    put(st.pl_def, out->pl_def_state, out->pl_def_mw);
    put(st.pl_max, out->pl_max_state, out->pl_max_mw);
}

void put_reading(const std::optional<std::uint32_t>& value, int32_t* has, uint32_t* mw) {
    *has = value ? 1 : 0;
    *mw = value.value_or(0u);
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_pl_normal_bounds(const dc_cl_statics* statics, int64_t* low, int64_t* high) {
    if (!statics_ok(statics) || !low || !high) return DC_E_ARGUMENT;
    const druta::backend::PowerBounds b = druta::backend::normal_power_bounds(get_statics(*statics));
    *low = b.low;
    *high = b.high;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_pl_request_bounds(const dc_cl_statics* statics, int32_t has_current, int64_t current,
                                              int32_t xoc, int32_t live, int64_t* low, int64_t* high) {
    if (!statics_ok(statics) || !low || !high) return DC_E_ARGUMENT;
    const druta::backend::PowerBounds b = druta::backend::power_request_bounds(
        get_statics(*statics), druta::abi::optional_arg(has_current, current), xoc != 0, live != 0);
    *low = b.low;
    *high = b.high;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_pl_bounds(dc_gpu* handle, dc_cl_statics* statics, int32_t xoc, int64_t* low,
                                      int64_t* high, dc_error* error) {
    if (!handle || !statics_ok(statics) || !low || !high || !error_ok(error)) return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        StaticTriState st = get_statics(*statics);
        const druta::backend::PowerBounds b = handle->pl.bounds(st, xoc != 0);
        put_statics(st, statics);
        *low = b.low;
        *high = b.high;
        put_error("", DC_ERR_NONE, error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_read_nvml(dc_gpu* handle, int32_t* has, uint32_t* mw) {
    if (!handle || !has || !mw) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        put_reading(handle->pl.read_nvml(), has, mw);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_read(dc_gpu* handle, dc_cl_statics* statics, int32_t* has, uint32_t* mw,
                                    dc_error* error) {
    if (!handle || !statics_ok(statics) || !has || !mw || !error_ok(error)) return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        StaticTriState st = get_statics(*statics);
        put_reading(handle->pl.read(&st), has, mw);
        put_statics(st, statics);
        put_error("", DC_ERR_NONE, error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_set(dc_gpu* handle, dc_cl_statics* statics, int64_t mw, int32_t xoc, int32_t* ok,
                                   char* message, size_t capacity, size_t* needed) {
    if (!handle || !statics_ok(statics) || !ok || !needed || (!message && capacity)) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        // set_power_limit_mw: every callee locks in Python (the live read, set_request, the policy
        // writer); here the handle lock covers the whole call, so a concurrent telemetry sweep sees
        // the live bookkeeping before or after it, never in between.
        druta::LockGuard guard(handle->gpu.lock());
        StaticTriState st = get_statics(*statics);
        const druta::backend::CpPeerWriter peer{druta::abi::write_peer, handle};
        const druta::backend::PowerLimitSet r = handle->pl.set(mw, xoc != 0, st, peer);
        put_statics(st, statics);  // the live read's back-fill happened whatever the outcome
        if (!r.in_bounds) return DC_E_RANGE;  // the kept message is not replaced (PowerLimit::set)
        *ok = r.ok ? 1 : 0;
        put_text(r.message, message, capacity, needed);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_message_get(dc_gpu* handle, char* out, size_t capacity, size_t* needed) {
    if (!handle || !needed || (!out && capacity)) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        put_text(handle->pl.message, out, capacity, needed);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_set_nvml(dc_gpu* handle, dc_cl_statics* statics, int64_t mw, dc_outcome* out,
                                        int32_t* has_got, uint32_t* got) {
    if (!handle || !statics_ok(statics) || !out || !has_got || !got) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::Nvml& nv = handle->gpu.nvml;
        if (!nv.ok || !nv.has("nvmlDeviceSetPowerManagementLimit")) return DC_E_UNAVAILABLE;
        StaticTriState st = get_statics(*statics);
        const druta::backend::NvmlLimitSet r = handle->pl.set_nvml(mw, st);
        put_statics(st, statics);
        druta::abi::put_outcome({r.ok, r.message}, out);
        put_reading(r.got, has_got, got);
        return DC_OK;
    });
}

// ---- increment B4 ------------------------------------------------------------------------------ //

DC_API dc_status DC_CALL dc_pl_stock_power_policy(dc_gpu* handle, dc_cl_statics* statics, int64_t policy,
                                                  int32_t whole, const char* name, size_t name_size, int32_t* ok,
                                                  char* message, size_t capacity, size_t* needed) {
    if (!handle || !statics_ok(statics) || (!name && name_size) || !ok || !needed || (!message && capacity))
        return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        StaticTriState st = get_statics(*statics);
        const druta::backend::StockResult r =
            handle->pl.stock_policy(policy, whole != 0, std::string(name ? name : "", name_size), st);
        put_statics(st, statics);
        if (!r.in_bounds) return DC_E_RANGE;
        if (r.setter_missing) return DC_E_UNAVAILABLE;
        *ok = r.ok ? 1 : 0;
        put_text(r.message, message, capacity, needed);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_max_all(dc_gpu* handle, dc_cl_statics* statics, int32_t xoc, dc_policy_step* steps,
                                       uint32_t capacity, uint32_t* count) {
    if (!handle || !statics_ok(statics) || !count || (!steps && capacity)) return DC_E_ARGUMENT;
    for (uint32_t i = 0; i < capacity; ++i)
        if (steps[i].size != sizeof(dc_policy_step)) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        StaticTriState st = get_statics(*statics);
        const druta::backend::CpPeerWriter peer{druta::abi::write_peer, handle};
        const druta::backend::MaxAllResult r = handle->pl.max_all(xoc != 0, st, peer);
        put_statics(st, statics);
        *count = 0;
        if (!r.in_bounds) return DC_E_RANGE;
        *count = static_cast<uint32_t>(r.steps.size());
        for (uint32_t i = 0; i < capacity && i < r.steps.size(); ++i) {
            dc_policy_step& out = steps[i];
            const druta::backend::PolicyStep& step = r.steps[i];
            size_t label_needed = 0, message_needed = 0;
            out.ok = step.ok ? 1 : 0;
            put_text(step.label, out.label, sizeof out.label, &label_needed);
            put_text(step.message, out.message, sizeof out.message, &message_needed);
            out.label_needed = static_cast<uint32_t>(label_needed);  // texts are far below 4 GiB
            out.message_needed = static_cast<uint32_t>(message_needed);
        }
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_step_text_get(dc_gpu* handle, uint32_t index, int32_t field, char* out,
                                             size_t capacity, size_t* needed) {
    if (!handle || !needed || (!out && capacity) || (field != DC_PL_STEP_LABEL && field != DC_PL_STEP_MESSAGE))
        return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const std::vector<druta::backend::PolicyStep>& kept = handle->pl.steps;
        if (index >= kept.size()) return DC_E_RANGE;
        put_text(field == DC_PL_STEP_LABEL ? kept[index].label : kept[index].message, out, capacity, needed);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_pl_native_power_limit(dc_gpu* handle, dc_cl_statics* statics, int32_t* has_value,
                                                  uint32_t* value, char* error, size_t capacity, size_t* needed) {
    if (!handle || !statics_ok(statics) || !has_value || !value || !needed || (!error && capacity))
        return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        StaticTriState st = get_statics(*statics);
        const druta::backend::NativeLimit r = handle->pl.native_limit(st);
        put_statics(st, statics);
        put_reading(r.value, has_value, value);
        put_text(r.error, error, capacity, needed);
        return DC_OK;
    });
}

}  // extern "C"
