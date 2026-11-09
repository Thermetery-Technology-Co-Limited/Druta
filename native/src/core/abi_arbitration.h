// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The dc_arbitration_* conversion shared by the C ABI of drutacore/power_arbitration.h
// (dc_power_arbitration_decode, dc_power_arbitration_vector) and of
// drutacore/client_power_controller.h (dc_cpc_read), so one decode reads the same through both.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

#include "backend/power_arbitration.h"
#include "core/abi_internal.h"

namespace druta::abi::arbitration {

inline void put_vector(const druta::backend::ArbitrationVector& v, dc_arbitration_vector* out) {
    out->available = v.available ? 1 : 0;
    DC_COPY(out->reason, v.reason);
    DC_COPY(out->method, v.method);
    out->method_code = v.method_code;
    out->count = v.count;
    out->output = v.output;
    out->initial_value = v.initial_value;
    for (std::size_t i = 0; i < v.inputs.size() && i < DC_ARBITRATION_INPUTS_MAX; ++i) {
        out->inputs[i].client = v.inputs[i].client;
        out->inputs[i].value = v.inputs[i].value;
    }
    out->has_computed_output = v.computed_output ? 1 : 0;
    out->computed_output = v.computed_output.value_or(0u);
    out->consistent = v.consistent ? (*v.consistent ? 1 : 0) : -1;
}

inline void put_row(const druta::backend::ArbitrationRow& r, dc_arbitration_row* out) {
    out->policy = r.policy;
    out->type = r.type;
    out->channel = r.channel;
    out->has_unit = r.unit ? 1 : 0;
    DC_COPY(out->unit, r.unit.value_or(std::string()));
    out->unit_code = r.unit_code;
    out->available = r.available ? 1 : 0;
    DC_COPY(out->reason, r.reason);
    out->requested = r.requested;
    out->effective = r.effective;
    out->value = r.value;
    out->delta = r.delta;
    put_vector(r.host, &out->host);
    put_vector(r.pmu, &out->pmu);
    std::size_t n = 0;
    for (; n < r.observations.size() && n < DC_ARBITRATION_OBSERVATIONS_MAX; ++n)
        DC_COPY(out->observations[n], r.observations[n]);
    out->observation_count = static_cast<uint32_t>(n);
}

// *out zeroed by the caller (absent fields and padding stay zero).
inline void put_report(const druta::backend::ArbitrationReport& report, dc_arbitration_report* out) {
    out->size = sizeof(dc_arbitration_report);
    out->schema = report.schema;
    out->available = report.available ? 1 : 0;
    DC_COPY(out->reason, report.reason);
    out->has_mask = report.mask ? 1 : 0;
    out->mask = report.mask.value_or(0u);
    std::size_t n = 0;
    for (; n < report.policies.size() && n < DC_ARBITRATION_POLICIES_MAX; ++n)
        put_row(report.policies[n], &out->policies[n]);
    out->policy_count = static_cast<uint32_t>(n);
}

}  // namespace druta::abi::arbitration
