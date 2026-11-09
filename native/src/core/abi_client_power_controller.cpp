// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/client_power_controller.h.
#include <cstring>
#include <exception>
#include <string>

#include "backend/client_power_controller.h"
#include "core/abi_internal.h"
#include "core/mutex.h"

namespace {

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

// The C++ exception that reached an entry point (only allocation can throw: the NATIVE_STATUS
// exchange alone needs a 397 KB packet). A transient failure: nothing about the target is known.
void put_exception(const char* what, dc_error* out) {
    druta::backend::RmError e;
    e.kind = druta::backend::RmErrorKind::transient;
    e.message = std::string("Client-power controller failed: ") + what;
    put_error(e, out);
}

constexpr bool same_text(const char* a, const char* b) {
    for (; *a && *a == *b; ++a, ++b) {
    }
    return *a == *b;
}
static_assert(same_text(druta::backend::kCpStopCommand, DC_CPC_STOP_COMMAND) &&
                  same_text(druta::backend::kCpStopPayload, DC_CPC_STOP_PAYLOAD) &&
                  same_text(druta::backend::kCpStopDispatch, DC_CPC_STOP_DISPATCH),
              "the caller-stop messages of drutacore/client_power_controller.h");

// No exception crosses the C boundary (as abi_vfplan.cpp's guarded): with an error out, the failure
// is reported there as transient.
template <typename F>
dc_status guarded(dc_error* error, F&& body) {
    try {
        return body();
    } catch (const std::exception& e) {
        try {
            put_exception(e.what(), error);
        } catch (...) {
        }
    } catch (...) {
        try {
            put_exception("unknown exception", error);
        } catch (...) {
        }
    }
    return DC_E_FAILED;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_cpc_transport(dc_gpu* handle, struct dc_rm_transport* out, dc_error* error) {
    if (!handle || !out || out->size != sizeof(dc_rm_transport) || (error && error->size != sizeof(dc_error)))
        return DC_E_ARGUMENT;
    return guarded(error, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::CpTransportResult r = handle->cp.transport();
        if (!r.transport) {
            put_error(r.error, error);
            return DC_E_FAILED;
        }
        dc_rm_transport t{};
        t.size = sizeof(dc_rm_transport);
        t.captured = 1;
        t.header_count = r.transport->header_count;
        for (std::size_t i = 0; i < 17; ++i) t.header[i] = r.transport->header[i];
        const druta::backend::EscapeFields& f = r.transport->fields;
        t.fields = dc_escape_fields{f.hAdapter, f.hDevice, f.Type, f.Flags, f.hContext};
        *out = t;
        put_error(druta::backend::RmError{}, error);
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_cpc_exchange(dc_gpu* handle, uint32_t command, const uint8_t* payload, uint32_t size,
                                         int32_t write, uint32_t flags, dc_cpc_dispatch_fn before_dispatch,
                                         void* context, uint8_t* out, uint32_t capacity, int32_t* dispatched,
                                         dc_error* error) {
    if (dispatched) *dispatched = 0;
    if (!handle || (!payload && size) || (!out && capacity) || !dispatched ||
        (flags & ~uint32_t{DC_CPC_COMMAND_NOT_STORABLE | DC_CPC_PAYLOAD_NOT_STORABLE | DC_CPC_CALLER_ECHO}) ||
        (error && error->size != sizeof(dc_error)))
        return DC_E_ARGUMENT;
    bool sent = false;
    const dc_status status = guarded(error, [&]() -> dc_status {
        druta::backend::CpCaller caller;
        caller.command_not_storable = (flags & DC_CPC_COMMAND_NOT_STORABLE) != 0;
        caller.payload_not_storable = (flags & DC_CPC_PAYLOAD_NOT_STORABLE) != 0;
        caller.caller_echo = (flags & DC_CPC_CALLER_ECHO) != 0;
        caller.before_dispatch = before_dispatch;
        caller.context = context;
        druta::LockGuard guard(handle->gpu.lock());
        druta::backend::CpExchangeResult r =
            handle->cp.exchange(command, payload, size, write != 0, capacity, caller, sent);
        if (r.too_small) return DC_E_BUFFER;  // before any binding or dispatch
        if (!r.response) {
            put_error(r.error, error);
            return DC_E_FAILED;
        }
        // Never reached with a response larger than capacity: exchange refuses that before it binds.
        if (!r.response->empty()) std::memcpy(out, r.response->data(), r.response->size());
        put_error(druta::backend::RmError{}, error);
        return DC_OK;
    });
    *dispatched = sent ? 1 : 0;
    return status;
}

DC_API dc_status DC_CALL dc_cpc_reset_transport(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        handle->cp.reset_transport();
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_cpc_reset(dc_gpu* handle) {
    if (!handle) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        handle->cp.reset();
        return DC_OK;
    });
}

DC_API dc_status DC_CALL dc_cpc_state_get(dc_gpu* handle, dc_cpc_state* out) {
    if (!handle || !out || out->size != sizeof(dc_cpc_state)) return DC_E_ARGUMENT;
    return guarded(nullptr, [&]() -> dc_status {
        druta::LockGuard guard(handle->gpu.lock());
        const druta::backend::CpControllerState& s = handle->cp.state();
        dc_cpc_state o{};
        o.size = sizeof(dc_cpc_state);
        o.sizes = static_cast<int32_t>(s.sizes);
        o.legacy = s.legacy ? 1 : 0;
        o.native_policy124 = s.native_policy124 ? 1 : 0;
        o.role_info216 = s.role_info216 ? 1 : 0;
        o.rm26_records = s.rm26_records ? 1 : 0;
        o.rm26 = s.rm26 ? 1 : 0;
        o.rm26_extended = s.rm26_extended ? 1 : 0;
        if (s.transport_key) {
            o.has_transport_key = 1;
            o.transport_key[0] = s.transport_key->command;
            o.transport_key[1] = s.transport_key->packet_bytes;
            o.transport_key[2] = s.transport_key->parameter_bytes;
        }
        *out = o;
        return DC_OK;
    });
}

}  // extern "C"
