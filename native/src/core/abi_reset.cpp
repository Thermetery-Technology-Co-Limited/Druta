// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/reset.h (increment G3): dc_gpu_reset_all and dc_gpu_reset_message_get.
#include <cstring>
#include <string>
#include <vector>

#include "backend/reset_all.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::StaticKey;
using druta::backend::StaticTriState;

static_assert(DC_RESET_STEPS_MAX == druta::backend::kResetStepsMax, "the step bound");
static_assert(sizeof(dc_reset_step::message) == DC_RESET_MESSAGE_MAX, "message capacity");
static_assert(sizeof(dc_reset_step::name) == DC_RESET_NAME_MAX, "name capacity");

// The text of a failure no Python path has: an exception (only allocation throws) inside the core.
constexpr char kCoreFailed[] = "native core: allocation failed";
static_assert(sizeof(kCoreFailed) <= sizeof(dc_error::message), "message capacity");

struct ConfirmContext {
    dc_cl_confirm_fn fn;
    void* context;
};

bool confirm_adapter(void* ctx, uint32_t policy, uint32_t requested_ma, uint32_t effective_ma,
                     const std::vector<uint32_t>& info, const std::vector<uint32_t>& control) {
    const ConfirmContext* c = static_cast<const ConfirmContext*>(ctx);
    return c->fn(c->context, policy, requested_ma, effective_ma, info.data(), static_cast<uint32_t>(info.size()),
                 control.data(), static_cast<uint32_t>(control.size())) != 0;
}

struct InputsContext {
    dc_gpu* handle;
    dc_cl_policy_inputs_fn fn;
    void* context;
};

// read_power_policies' inputs as dc_cl_power_policies_read takes them: read_power_limit_mw through
// the caller, then the static keys, then GPU._board_policy as it is now. Nonzero: the caller raised.
druta::backend::PolicyInputs inputs_adapter(void* ctx) {
    const InputsContext* c = static_cast<const InputsContext*>(ctx);
    int32_t has_board_mw = 0;
    int64_t board_mw = 0;
    dc_cl_statics st{};
    st.size = sizeof(dc_cl_statics);
    st.pl_min_state = st.pl_def_state = st.pl_max_state = DC_CL_STATIC_NONE;
    druta::backend::PolicyInputs in;
    if (c->fn(c->context, &has_board_mw, &board_mw, &st) != 0) {
        in.failed = true;
        return in;
    }
    if (has_board_mw) in.board_mw = board_mw;
    if (!druta::abi::statics_ok(&st)) st.pl_min_state = st.pl_def_state = st.pl_max_state = DC_CL_STATIC_NONE;
    in.statics = druta::abi::get_statics(st);
    in.board_policy = c->handle->cp.live().board_policy;
    return in;
}

// The keys the live read filled, written over the caller's statics as dc_pl_set writes them back
// (a key the caller passed with a value is never changed).
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

void put_writes(const druta::backend::RailWriterTrace& trace, dc_rails_setter_writes* writes) {
    if (!writes) return;
    std::memset(writes, 0, sizeof *writes);
    writes->size = sizeof(dc_rails_setter_writes);
    writes->calls = trace.calls;
    writes->attributes = trace.attributes ? 1 : 0;
    writes->last_attributes = trace.last_attributes ? 1 : 0;
    writes->result.size = sizeof(dc_rail_write_result);
    if (trace.attributes) druta::abi::put_rail_write_result(&writes->result, trace.result);
}

void put_failure(dc_error* error) {
    if (!error) return;
    dc_error e{};
    e.size = sizeof(dc_error);
    e.kind = DC_ERR_TRANSIENT;
    std::memcpy(e.message, kCoreFailed, sizeof(kCoreFailed));
    *error = e;
}

void put_none(dc_error* error) {
    if (!error) return;
    dc_error e{};
    e.size = sizeof(dc_error);
    *error = e;
}

void put_step(const druta::backend::ResetStep& step, dc_reset_step* out) {
    std::memset(out, 0, sizeof *out);
    out->size = sizeof(dc_reset_step);
    DC_COPY(out->name, step.name);
    out->ok = step.ok ? 1 : 0;
    out->message_length = static_cast<uint32_t>(step.message.size());
    DC_COPY(out->message, step.message);
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_gpu_reset_all(dc_gpu* handle, dc_cl_confirm_fn confirm, void* confirm_context,
                                          dc_cl_policy_inputs_fn inputs, void* inputs_context,
                                          dc_reset_statics_fn statics, void* statics_context, int32_t xoc,
                                          dc_reset_step* steps, uint32_t capacity, uint32_t* count,
                                          dc_rails_setter_writes* rail_writes, dc_error* error) {
    if (!handle || !inputs || !statics || !steps || !count || (error && error->size != sizeof(dc_error)) ||
        (rail_writes && rail_writes->size != sizeof(dc_rails_setter_writes)))
        return DC_E_ARGUMENT;
    if (capacity < DC_RESET_STEPS_MAX) {  // before anything is read, written or changed
        *count = DC_RESET_STEPS_MAX;
        return DC_E_BUFFER;
    }
    for (uint32_t i = 0; i < DC_RESET_STEPS_MAX; ++i)
        if (steps[i].size != sizeof(dc_reset_step)) return DC_E_ARGUMENT;
    *count = 0;
    druta::backend::RailWriterTrace trace;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        ConfirmContext cc{confirm, confirm_context};
        InputsContext ic{handle, inputs, inputs_context};
        druta::backend::ResetHooks hooks;
        hooks.confirm = confirm ? confirm_adapter : nullptr;
        hooks.confirm_context = &cc;
        hooks.inputs = inputs_adapter;
        hooks.inputs_context = &ic;
        dc_cl_statics given{};  // what the caller passed for set_power_limit_mw
        hooks.read_statics = [&](StaticTriState& out) {
            given = dc_cl_statics{};
            given.size = sizeof(dc_cl_statics);
            given.pl_min_state = given.pl_def_state = given.pl_max_state = DC_CL_STATIC_ABSENT;
            if (statics(statics_context, nullptr, &given) != 0) return false;
            if (!druta::abi::statics_ok(&given)) {  // a broken shape reports every key as None
                given.size = sizeof(dc_cl_statics);
                given.pl_min_state = given.pl_def_state = given.pl_max_state = DC_CL_STATIC_NONE;
            }
            out = druta::abi::get_statics(given);
            return true;
        };
        hooks.backfill = [&](const StaticTriState&, const StaticTriState& after) {
            dc_cl_statics filled = given;
            put_statics(after, &filled);
            return statics(statics_context, &given, &filled) == 0;
        };
        hooks.peer = druta::backend::CpPeerWriter{druta::abi::write_peer, handle};
        hooks.xoc = xoc != 0;
        const druta::backend::ResetParts parts{
            handle->gpu,
            handle->telemetry,
            handle->clkdom,
            handle->rails,
            handle->cl,
            handle->rm,
            handle->cp,
            handle->pl,
            handle->legacy_p0,
            handle->vflock,
            [handle]() -> druta::backend::Clocks& { return handle->clocks(); },
            [handle]() { return handle->fan(); },
            [handle]() -> druta::backend::VfCurve& { return handle->curve(); },
        };
        druta::backend::ResetAllResult r = druta::backend::reset_all(parts, hooks, trace);
        put_writes(trace, rail_writes);
        if (r.stop == druta::backend::ResetStop::caller) {
            put_none(error);
            return DC_E_CALLER;
        }
        if (r.stop == druta::backend::ResetStop::index_error) {
            put_none(error);
            return DC_E_RANGE;
        }
        if (r.steps.size() > capacity) {  // unreachable: kResetStepsMax bounds every group
            put_failure(error);
            return DC_E_FAILED;
        }
        std::vector<std::string> messages;
        messages.reserve(r.steps.size());
        for (const auto& step : r.steps) messages.push_back(step.message);
        handle->reset_messages.swap(messages);
        for (size_t i = 0; i < r.steps.size(); ++i) put_step(r.steps[i], &steps[i]);
        *count = static_cast<uint32_t>(r.steps.size());
        put_none(error);
        return DC_OK;
    } catch (...) {  // no exception crosses the C boundary; the handle lock is released by its guard
    }
    put_writes(trace, rail_writes);
    put_failure(error);
    return DC_E_FAILED;
}

DC_API dc_status DC_CALL dc_gpu_reset_message_get(dc_gpu* handle, uint32_t index, char* out, size_t capacity,
                                                  size_t* needed) {
    if (!handle || !needed || (!out && capacity)) return DC_E_ARGUMENT;
    try {
        druta::LockGuard guard(handle->gpu.lock());
        if (index >= handle->reset_messages.size()) return DC_E_RANGE;
        const std::string& text = handle->reset_messages[index];
        *needed = text.size() + 1;
        if (capacity) {
            const size_t n = text.size() < capacity - 1 ? text.size() : capacity - 1;
            std::memcpy(out, text.data(), n);
            out[n] = '\0';
        }
        return DC_OK;
    } catch (...) {
    }
    return DC_E_FAILED;
}

}  // extern "C"
