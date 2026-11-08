// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of the current-policy operations on the transport (increment 6a).
#include <cstring>

#include "backend/current_limits.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

using druta::backend::PowerLayout;

void put_error(const druta::backend::RmError& why, dc_error* out) {
    if (!out) return;
    dc_error e{};
    e.size = sizeof(dc_error);
    e.kind = static_cast<int32_t>(why.kind);
    e.fixed = why.fixed ? 1 : 0;
    e.has_ntstatus = why.ntstatus ? 1 : 0;
    e.ntstatus = why.ntstatus ? static_cast<int32_t>(*why.ntstatus) : 0;
    e.rm_status = why.rm_status;
    DC_COPY(e.message, why.message);
    *out = e;
}

// Copies words into a caller's buffer: at most capacity, *count the total.
void put_words(const std::vector<uint32_t>& words, uint32_t* out, uint32_t capacity, uint32_t* count) {
    const uint32_t total = static_cast<uint32_t>(words.size());
    const uint32_t written = total < capacity ? total : capacity;
    if (written) std::memcpy(out, words.data(), written * sizeof(uint32_t));
    *count = total;
}

bool layout_row_ok(const dc_power_layout* layout) { return layout && layout->size == sizeof(dc_power_layout); }
bool error_ok(const dc_error* error) { return !error || error->size == sizeof(dc_error); }
bool buffer_ok(const uint32_t* out, uint32_t capacity, const uint32_t* count) { return count && (out || !capacity); }

}  // namespace

extern "C" {

DC_API uint32_t DC_CALL dc_cl_max_words(void) { return druta::backend::max_power_layout_bytes() / 4; }

DC_API dc_status DC_CALL dc_cl_capacity(const dc_power_layout* layout, uint32_t* capacity) {
    if (!layout_row_ok(layout) || !capacity) return DC_E_ARGUMENT;
    *capacity = druta::backend::CurrentLimits::capacity(druta::abi::get_power_layout(*layout));
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_abi(dc_gpu* handle, dc_power_layout* layout, uint32_t* index, dc_error* error) {
    if (!handle || !layout_row_ok(layout) || !index || !error_ok(error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::AbiResult r = handle->cl.abi();
    if (!r.layout) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    druta::abi::put_power_layout(*r.layout, layout);
    *index = r.index;
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_probe_info(dc_gpu* handle, const dc_power_layout* layout, uint32_t* out, uint32_t capacity,
                                          uint32_t* count, dc_error* error) {
    if (!handle || !layout_row_ok(layout) || !buffer_ok(out, capacity, count) || !error_ok(error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const PowerLayout candidate = druta::abi::get_power_layout(*layout);
    druta::backend::Words r = handle->cl.probe_info(candidate);
    if (!r.words) {
        put_error(r.error, error);
        *count = 0;
        return DC_E_FAILED;
    }
    put_words(*r.words, out, capacity, count);
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_rm(dc_gpu* handle, uint32_t command, const uint32_t* params, uint32_t param_count,
                                  int32_t has_mask, uint32_t policy_mask, int32_t any_live_policy, uint32_t* out,
                                  uint32_t capacity, uint32_t* count, dc_error* error) {
    if (!handle || !buffer_ok(out, capacity, count) || !error_ok(error) || (!params && param_count)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    std::vector<uint32_t> words;
    if (params) words.assign(params, params + param_count);
    druta::backend::Words r =
        handle->cl.rm(command, params ? &words : nullptr, druta::abi::optional_u32(has_mask, policy_mask), any_live_policy != 0);
    if (!r.words) {
        put_error(r.error, error);
        *count = 0;
        return DC_E_FAILED;
    }
    put_words(*r.words, out, capacity, count);
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_policy_table(dc_gpu* handle, dc_power_layout* layout, dc_error* error) {
    if (!handle || !layout_row_ok(layout) || !error_ok(error)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    druta::backend::PolicyTableResult r = handle->cl.policy_table();
    if (!r.table) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    druta::abi::put_power_layout(*r.table->layout, layout);
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_table_layout(dc_gpu* handle, dc_power_layout* layout, int32_t* present) {
    if (!handle || !layout_row_ok(layout) || !present) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const auto& table = handle->cl.last_policy_table;
    *present = table ? 1 : 0;
    if (table) druta::abi::put_power_layout(*table->layout, layout);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_table_get(dc_gpu* handle, int32_t block, uint32_t* out, uint32_t capacity, uint32_t* count,
                                         int32_t* present) {
    if (!handle || !buffer_ok(out, capacity, count) || !present) return DC_E_ARGUMENT;
    if (block != DC_CL_TABLE_INFO && block != DC_CL_TABLE_CONTROL && block != DC_CL_TABLE_STATUS) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *present = 0;
    *count = 0;
    const auto& table = handle->cl.last_policy_table;
    if (!table) return DC_OK;
    const std::vector<uint32_t>* words = nullptr;
    if (block == DC_CL_TABLE_INFO) words = &table->info;
    else if (block == DC_CL_TABLE_CONTROL) words = table->control ? &*table->control : nullptr;
    else words = table->dynamic ? &*table->dynamic : nullptr;
    if (!words) return DC_OK;
    *present = 1;
    put_words(*words, out, capacity, count);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_table_reset(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->cl.last_policy_table.reset();
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_policy_mask_get(dc_gpu* handle, uint32_t* mask) {
    if (!handle || !mask) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *mask = handle->cl.power_policy_mask;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_policy_mask_set(dc_gpu* handle, uint32_t mask) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->cl.power_policy_mask = mask;
    return DC_OK;
}

}  // extern "C"

namespace {

// ---- increment 6b ------------------------------------------------------------------------------ //

using druta::backend::CurrentRow;
using druta::backend::UnavailablePolicy;

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

void put_row(const CurrentRow& row, dc_current_row* out) {
    std::memset(out, 0, sizeof(*out));
    out->size = sizeof(dc_current_row);
    out->policy = row.policy;
    DC_COPY(out->label, row.label);
    out->channel = row.channel;
    out->minimum_ma = row.minimum_ma;
    out->maximum_ma = row.maximum_ma;
    out->normal_maximum_ma = row.normal_maximum_ma;
    out->default_ma = row.default_ma;
    out->limit_ma = row.limit_ma;
    out->requested_ma = row.requested_ma;
    out->value_ma = row.value_ma;
}

// Rows into a caller's buffer whose entries carry their size: at most capacity, *count the total.
dc_status put_rows(const std::vector<CurrentRow>& rows, dc_current_row* out, uint32_t capacity, uint32_t* count) {
    const uint32_t total = static_cast<uint32_t>(rows.size());
    const uint32_t written = total < capacity ? total : capacity;
    for (uint32_t i = 0; i < written; ++i)
        if (out[i].size != sizeof(dc_current_row)) return DC_E_ARGUMENT;
    for (uint32_t i = 0; i < written; ++i) put_row(rows[i], &out[i]);
    *count = total;
    return DC_OK;
}

bool rows_ok(const dc_current_row* out, uint32_t capacity, const uint32_t* count) { return count && (out || !capacity); }

// A string into a caller's buffer: NUL-terminated within capacity, *needed the full length + 1.
void put_text(const std::string& text, char* out, size_t capacity, size_t* needed) {
    *needed = text.size() + 1;
    if (!capacity) return;
    const size_t n = text.size() < capacity - 1 ? text.size() : capacity - 1;
    std::memcpy(out, text.data(), n);
    out[n] = '\0';
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_cl_state(dc_gpu* handle, dc_cl_confirm_fn confirm, void* context, dc_current_row* rows,
                                     uint32_t capacity, uint32_t* count, uint32_t* control, uint32_t control_capacity,
                                     uint32_t* control_count, int32_t* has_control, dc_error* error) {
    if (!handle || !rows_ok(rows, capacity, count) || !buffer_ok(control, control_capacity, control_count) || !has_control ||
        !error_ok(error))
        return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    ConfirmContext cc{confirm, context};
    druta::backend::StateResult r = handle->cl.state(confirm ? confirm_adapter : nullptr, &cc);
    if (r.error.kind != druta::backend::RmErrorKind::none) {
        put_error(r.error, error);
        return DC_E_FAILED;
    }
    const dc_status status = put_rows(r.rows, rows, capacity, count);
    if (status != DC_OK) return status;
    *has_control = r.control ? 1 : 0;
    if (r.control)
        put_words(*r.control, control, control_capacity, control_count);
    else
        *control_count = 0;
    if (error) put_error(druta::backend::RmError{}, error);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_unavailable(dc_gpu* handle, dc_current_unavailable* out, uint32_t capacity, uint32_t* count) {
    if (!handle || !count || (!out && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const std::vector<UnavailablePolicy>& items = handle->cl.unavailable;
    const uint32_t total = static_cast<uint32_t>(items.size());
    const uint32_t written = total < capacity ? total : capacity;
    for (uint32_t i = 0; i < written; ++i)
        if (out[i].size != sizeof(dc_current_unavailable)) return DC_E_ARGUMENT;
    for (uint32_t i = 0; i < written; ++i) {
        dc_current_unavailable* o = &out[i];
        std::memset(o, 0, sizeof(*o));
        o->size = sizeof(dc_current_unavailable);
        o->policy = items[i].policy;
        DC_COPY(o->label, items[i].label);
        o->present = items[i].present ? 1 : 0;
        o->unsupported = items[i].unsupported ? 1 : 0;
        DC_COPY(o->error, items[i].error);
    }
    *count = total;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_get_current_limits(dc_gpu* handle, dc_cl_confirm_fn confirm, void* context,
                                                  dc_current_row* out, uint32_t capacity, uint32_t* count) {
    if (!handle || !rows_ok(out, capacity, count)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    ConfirmContext cc{confirm, context};
    const std::vector<CurrentRow> rows = handle->cl.get_current_limits(confirm ? confirm_adapter : nullptr, &cc);
    return put_rows(rows, out, capacity, count);
}

DC_API dc_status DC_CALL dc_cl_last_rows(dc_gpu* handle, dc_current_row* out, uint32_t capacity, uint32_t* count) {
    if (!handle || !rows_ok(out, capacity, count)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    return put_rows(handle->cl.last_rows, out, capacity, count);
}

DC_API dc_status DC_CALL dc_cl_error_get(dc_gpu* handle, int32_t which, char* out, size_t capacity, size_t* needed) {
    if (!handle || !needed || (!out && capacity) || (which != DC_CL_ERROR && which != DC_CL_READ_ERROR)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    put_text(which == DC_CL_ERROR ? handle->cl.error : handle->cl.read_error, out, capacity, needed);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_established_get(dc_gpu* handle, uint32_t* mask) {
    if (!handle || !mask) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    *mask = handle->cl.established;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_established_set(dc_gpu* handle, uint32_t mask) {
    if (!handle) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->cl.established = mask;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_set_current_limit_ma(dc_gpu* handle, dc_cl_confirm_fn confirm, void* context, int64_t policy,
                                                    int64_t ma, int32_t pin, int32_t* ok, char* message, size_t capacity,
                                                    size_t* needed) {
    if (!handle || !ok || !needed || (!message && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    ConfirmContext cc{confirm, context};
    const druta::backend::SetResult r =
        handle->cl.set_current_limit_ma(policy, ma, pin != 0, confirm ? confirm_adapter : nullptr, &cc);
    *ok = r.ok ? 1 : 0;
    put_text(r.message, message, capacity, needed);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_pins_get(dc_gpu* handle, uint32_t* policies, int64_t* values, uint32_t capacity,
                                        uint32_t* count) {
    if (!handle || !count || ((!policies || !values) && capacity)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    const uint32_t total = static_cast<uint32_t>(handle->cl.pins.size());
    uint32_t i = 0;
    for (const auto& [policy, value] : handle->cl.pins) {
        if (i >= capacity) break;
        policies[i] = policy;
        values[i] = value;
        ++i;
    }
    *count = total;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cl_pins_set(dc_gpu* handle, const uint32_t* policies, const int64_t* values, uint32_t count) {
    if (!handle || ((!policies || !values) && count)) return DC_E_ARGUMENT;
    druta::LockGuard guard(handle->gpu.lock());
    handle->cl.pins.clear();
    for (uint32_t i = 0; i < count; ++i) handle->cl.pins[policies[i]] = values[i];
    return DC_OK;
}

}  // extern "C"
