// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI of drutacore/power_arbitration.h.
#include <cstring>
#include <iterator>
#include <optional>

#include "backend/power_arbitration.h"
#include "core/abi_arbitration.h"
#include "core/abi_internal.h"

namespace {

using druta::backend::ArbitrationReport;
using druta::backend::ArbitrationWords;
using druta::backend::PowerLayout;

bool get_words(const dc_arbitration_words* in, ArbitrationWords* out) {
    if (!in || (!in->words && in->count)) return false;
    out->present = in->present != 0;
    out->valid = in->valid != 0;
    out->data = in->words;
    out->count = in->count;
    return true;
}

}  // namespace

extern "C" {

DC_API dc_status DC_CALL dc_power_arbitration_decode(const dc_power_layout* layout, const dc_arbitration_words* info,
                                                     const dc_arbitration_words* control,
                                                     const dc_arbitration_words* status, dc_arbitration_report* out) {
    if (!out || out->size != sizeof(dc_arbitration_report)) return DC_E_ARGUMENT;
    if (layout && layout->size != sizeof(dc_power_layout)) return DC_E_ARGUMENT;
    ArbitrationWords i, c, s;
    if (!get_words(info, &i) || !get_words(control, &c) || !get_words(status, &s)) return DC_E_ARGUMENT;
    std::optional<PowerLayout> pure;
    if (layout) pure = druta::abi::get_power_layout(*layout);
    const ArbitrationReport report = druta::backend::decode_power_arbitration(pure ? &*pure : nullptr, i, c, s);
    std::memset(out, 0, sizeof(*out));
    druta::abi::arbitration::put_report(report, out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_power_arbitration_geometry(const dc_power_layout* layout, char* reason,
                                                       uint32_t reason_size) {
    if ((!reason && reason_size) || (layout && layout->size != sizeof(dc_power_layout))) return DC_E_ARGUMENT;
    std::optional<PowerLayout> pure;
    if (layout) pure = druta::abi::get_power_layout(*layout);
    const std::string refusal = druta::backend::arbitration_geometry(pure ? &*pure : nullptr);
    if (reason_size) druta::abi::copy_field(reason, reason_size, refusal);
    return refusal.empty() ? DC_OK : DC_E_UNRECOGNIZED;
}

DC_API dc_status DC_CALL dc_power_arbitration_vector(const uint8_t* row, uint32_t row_bytes, uint32_t offset,
                                                     dc_arbitration_vector* out) {
    if (!out || (!row && row_bytes)) return DC_E_ARGUMENT;
    std::memset(out, 0, sizeof(*out));
    druta::abi::arbitration::put_vector(druta::backend::decode_arbitration_vector(row, row_bytes, offset), out);
    return DC_OK;
}

DC_API dc_status DC_CALL dc_power_arbitration_row_formats(uint32_t* out, uint32_t capacity, uint32_t* count) {
    if (!count || (!out && capacity)) return DC_E_ARGUMENT;
    const uint32_t total = static_cast<uint32_t>(std::size(druta::backend::kArbitrationRowFormats));
    for (uint32_t i = 0; i < total && i < capacity; ++i) out[i] = druta::backend::kArbitrationRowFormats[i];
    *count = total;
    return DC_OK;
}

}  // extern "C"
