// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of the pure current-limit tables (drutacore/power_layouts.h).
#include <cstring>
#include <vector>

#include "backend/power_layouts.h"
#include "core/abi_internal.h"

namespace {

using druta::backend::GenerationPolicies;
using druta::backend::GenerationPolicy;
using druta::backend::PowerLayout;

void put_policy(const GenerationPolicy& p, dc_generation_policy* out) {
    std::memset(out, 0, sizeof(*out));
    out->policy = p.policy;
    DC_COPY(out->label, p.label);
    out->type = p.type;
    out->channel = p.channel;
    out->normal_maximum_ma = p.normal_maximum_ma;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_power_layouts_lookup(uint32_t command, uint32_t packet_bytes, uint32_t parameter_bytes,
                                                 dc_power_layout* out, uint32_t capacity, uint32_t* count) {
    if (!count || (!out && capacity)) return DC_E_ARGUMENT;
    const std::vector<const PowerLayout*> layouts =
        druta::backend::power_layouts({command, packet_bytes, parameter_bytes});
    const uint32_t total = static_cast<uint32_t>(layouts.size());
    const uint32_t written = total < capacity ? total : capacity;
    for (uint32_t i = 0; i < written; ++i)
        if (out[i].size != sizeof(dc_power_layout)) return DC_E_ARGUMENT;
    for (uint32_t i = 0; i < written; ++i) druta::abi::put_power_layout(*layouts[i], &out[i]);
    *count = total;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_generation_policies(int32_t has_arch, uint32_t arch, dc_generation_policy* out,
                                                uint32_t capacity, uint32_t* count) {
    if (!count || (!out && capacity)) return DC_E_ARGUMENT;
    const GenerationPolicies policies = druta::backend::generation_policies(druta::abi::optional_u32(has_arch, arch));
    const uint32_t total = static_cast<uint32_t>(policies.count);
    const uint32_t written = total < capacity ? total : capacity;
    for (uint32_t i = 0; i < written; ++i) put_policy(policies.policies[i], &out[i]);
    *count = total;
    return DC_OK;
}

}  // extern "C"
