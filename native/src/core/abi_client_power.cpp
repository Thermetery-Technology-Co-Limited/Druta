// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/client_power.h.
#include <cstring>

#include "backend/client_power.h"
#include "core/abi_internal.h"

namespace {

namespace be = druta::backend;

bool get_binding(const dc_cp_binding* in, be::CpBinding& out) {
    if (!in) return false;
    if (in->layout < DC_CP_ABSENT || in->layout > DC_CP_LAYOUT_LEGACY32) return false;
    if (in->native_layout < DC_CP_ABSENT || in->native_layout > DC_CP_NATIVE_RM26_EXTENDED) return false;
    out.layout = static_cast<be::CpLayout>(in->layout);
    out.native_layout = static_cast<be::CpNativeLayout>(in->native_layout);
    out.native_info_stride = in->has_native_info_stride ? std::optional<int64_t>(in->native_info_stride) : std::nullopt;
    out.native_index = in->has_native_index ? std::optional<int64_t>(in->native_index) : std::nullopt;
    out.client_index = in->has_client_index ? std::optional<int64_t>(in->client_index) : std::nullopt;
    return true;
}

bool get_payload(const dc_cp_payload* in, be::CpPayload& out) {
    if (!in) return false;
    if (!in->present) {
        out.reset();
        return true;
    }
    if (!in->data && in->length) return false;
    out.emplace(in->data, in->data + in->length);
    return true;
}

void clear_error(dc_cp_error* error) { std::memset(error, 0, sizeof(*error)); }

dc_status put_error(const be::CpError& e, dc_cp_error* out) {
    out->kind = static_cast<int32_t>(e.kind);
    out->key = static_cast<uint32_t>(e.key);
    out->side = e.side;
    DC_COPY(out->message, e.message);
    return DC_E_FAILED;
}

void put_list(be::CpU32List list, uint32_t* count, uint32_t* out) {
    *count = static_cast<uint32_t>(list.count);
    for (std::size_t i = 0; i < list.count && i < DC_CP_LIST_MAX; ++i) out[i] = list.values[i];
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_cp_constants_get(dc_cp_constants* out) {
    if (!out || out->size != sizeof(dc_cp_constants)) return DC_E_ARGUMENT;
    std::memset(out, 0, sizeof(*out));
    out->size = sizeof(dc_cp_constants);
    for (std::size_t i = 0; i < DC_CP_COMMAND_COUNT; ++i) {
        out->commands[i] = be::kCpCommands[i];
        out->rm26_commands[i] = be::kCpRm26Commands[i];
    }
    for (uint32_t g = 0; g < DC_CP_SIZE_GROUP_COUNT; ++g) {
        const be::CpSizes* sizes = be::cp_size_group(g);
        for (std::size_t i = 0; i < DC_CP_COMMAND_COUNT; ++i) out->group_sizes[g][i] = (*sizes)[i];
    }
    put_list(be::cp_size_refusals(), &out->size_refusal_count, out->size_refusals);
    out->info_base = be::kCpInfoBase;
    out->info_stride = be::kCpInfoStride;
    out->control_base = be::kCpControlBase;
    out->control_stride = be::kCpControlStride;
    out->capacity = be::kCpCapacity;
    out->native_info_base = be::kCpNativeInfoBase;
    out->native_info_stride = be::kCpNativeInfoStride;
    out->native_control_base = be::kCpNativeControlBase;
    out->native_control_stride = be::kCpNativeControlStride;
    out->native_status_base = be::kCpNativeStatusBase;
    out->native_status_stride = be::kCpNativeStatusStride;
    out->native_capacity = be::kCpNativeCapacity;
    out->role = be::kCpRole;
    out->client = be::kCpClient;
    out->missing = be::kCpMissing;
    put_list(be::cp_legacy_group_architectures(), &out->legacy_group_architecture_count,
             out->legacy_group_architectures);
    put_list(be::cp_rm26_architectures(), &out->rm26_architecture_count, out->rm26_architectures);
    for (std::size_t i = 0; i < DC_CP_STATE_KEY_COUNT; ++i) DC_COPY(out->state_keys[i], be::kCpStateKeys[i]);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cp_small_native_geometry(const dc_cp_binding* binding, uint32_t* control_size,
                                                     uint32_t* control_stride) {
    be::CpBinding b;
    if (!control_size || !control_stride || !get_binding(binding, b)) return DC_E_ARGUMENT;
    const be::CpSmallGeometry g = be::small_native_geometry(b);
    *control_size = g.control_size;
    *control_stride = g.control_stride;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cp_definition_stride(int64_t role_info_size, int64_t* stride) {
    if (!stride) return DC_E_ARGUMENT;
    const std::optional<int64_t> value = be::definition_stride(role_info_size);
    if (!value) return DC_E_RANGE;
    *stride = *value;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cp_native_geometry(const dc_cp_binding* binding, dc_cp_geometry* out) {
    be::CpBinding b;
    if (!out || !get_binding(binding, b)) return DC_E_ARGUMENT;
    const be::CpGeometry g = be::native_geometry(b);
    out->info_base = g.info_base;
    out->info_stride = g.info_stride;
    out->control_base = g.control_base;
    out->control_stride = g.control_stride;
    out->control_size = g.control_size;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cp_coupled_requests(const uint8_t* role_info, uint32_t length, int32_t* coupled,
                                                dc_cp_error* error) {
    if (!coupled || !error || (!role_info && length)) return DC_E_ARGUMENT;
    clear_error(error);
    const std::vector<uint8_t> role = role_info ? std::vector<uint8_t>(role_info, role_info + length)
                                                : std::vector<uint8_t>();
    be::CpError e;
    const bool result = be::coupled_requests(role, e);
    if (e) return put_error(e, error);
    *coupled = result ? 1 : 0;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cp_project(const dc_cp_binding* binding, int32_t mw_kind, int64_t mw,
                                       const dc_cp_payload* control, const dc_cp_payload* native_control,
                                       uint8_t* control_out, uint8_t* native_control_out, dc_cp_error* error) {
    be::CpBinding b;
    be::CpPayload c, n;
    if (!error || !get_binding(binding, b) || !get_payload(control, c) || !get_payload(native_control, n))
        return DC_E_ARGUMENT;
    if (mw_kind < DC_CP_MW_INTEGER || mw_kind > DC_CP_MW_OUT_OF_RANGE) return DC_E_ARGUMENT;
    if ((c && !c->empty() && !control_out) || (n && !n->empty() && !native_control_out)) return DC_E_ARGUMENT;
    clear_error(error);
    be::CpError e;
    const std::optional<be::CpProjection> result =
        be::project(b, static_cast<be::CpMwKind>(mw_kind), mw, c, n, e);
    if (!result) return put_error(e, error);
    if (!result->control.empty()) std::memcpy(control_out, result->control.data(), result->control.size());
    if (!result->native_control.empty())
        std::memcpy(native_control_out, result->native_control.data(), result->native_control.size());
    return DC_OK;
}

DC_API dc_status DC_CALL dc_cp_matches(const dc_cp_match_input* input, int32_t* matches, dc_cp_error* error) {
    if (!input || !matches || !error || input->size != sizeof(dc_cp_match_input)) return DC_E_ARGUMENT;
    be::CpMatchInput in;
    if (!get_binding(&input->binding, in.binding)) return DC_E_ARGUMENT;
    in.binding_usable = input->binding_usable != 0;
    in.extra_keys = input->extra_keys != 0;
    for (std::size_t k = 0; k < DC_CP_STATE_KEY_COUNT; ++k) {
        const int32_t c = input->compare[k];
        if (c < DC_CP_CMP_BYTES || c > DC_CP_CMP_MISSING) return DC_E_ARGUMENT;
        // A byte comparison is defined for the payload keys only.
        if (c == DC_CP_CMP_BYTES && k < DC_CP_FIRST_PAYLOAD_KEY) return DC_E_ARGUMENT;
        in.compare[k] = static_cast<be::CpCompare>(c);
    }
    for (std::size_t i = 0; i < DC_CP_PAYLOAD_COUNT; ++i)
        if (!get_payload(&input->expected[i], in.expected[i]) || !get_payload(&input->observed[i], in.observed[i]))
            return DC_E_ARGUMENT;
    clear_error(error);
    be::CpError e;
    const std::optional<bool> result = be::matches(in, e);
    if (!result) return put_error(e, error);
    *matches = *result ? 1 : 0;
    return DC_OK;
}

}  // extern "C"
