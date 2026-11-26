// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <drutacore/model_inputs.h>
#include <cmath>
#include <cstring>
#include <string>

namespace druta::ui {
struct ModelInputInventory {
    dc_model_inputs values{};
    std::string error;
};

inline const char* model_input_name(uint32_t row) {
    constexpr const char* names[] = {"Speedo", "Speedo 1", "Speedo 2", "NVVDD IDDQ", "MSVDD IDDQ"};
    static_assert(sizeof(names) / sizeof(names[0]) == DC_MODEL_INPUT_COUNT);
    return names[row];
}
inline const char* model_input_status(uint32_t state) {
    switch (state) {
    case DC_MODEL_ABSENT: return "Absent";
    case DC_MODEL_READY: return "Ready";
    case DC_MODEL_NOT_READY: return "Not ready";
    case DC_MODEL_AMBIGUOUS: return "Ambiguous";
    default: return "Invalid";
    }
}

inline ModelInputInventory read_model_inputs(decltype(&dc_gpu_model_inputs_get) get,
                                            dc_gpu* gpu, const dc_gpu_identity& identity) {
    ModelInputInventory inventory;
    if (!get) {
        inventory.error = "The loaded core does not expose the Speedo and IDDQ getter.";
        return inventory;
    }
    auto& values = inventory.values;
    values.size = sizeof(values);
    dc_error error{}; error.size = sizeof(error);
    if (get(gpu, &values, &error) != DC_OK) {
        inventory.error = std::memchr(error.message, 0, sizeof(error.message)) && error.message[0] ?
            error.message : "Speedo and IDDQ read failed.";
        return inventory;
    }
    const auto& target = values.target;
    if (values.size != sizeof(values) || target.size != sizeof(target) ||
        !identity.nvapi_selected || !target.handle || target.handle != identity.nvapi_handle ||
        target.device_id != identity.nvapi_devid || target.subsystem_id != identity.nvapi_subsys ||
        !std::memchr(target.slot, 0, sizeof(target.slot)) || std::strcmp(target.slot, identity.slot)) {
        inventory.error = "Speedo and IDDQ capture did not match the selected GPU.";
        return inventory;
    }
    for (auto& row : values.rows) {
        if (row.state > DC_MODEL_AMBIGUOUS || !std::memchr(row.reason, 0, sizeof(row.reason)) ||
            (row.state == DC_MODEL_READY && (row.index >= 255 || row.override_present > 1 ||
             !std::isfinite(row.minimum) || !std::isfinite(row.maximum) || row.minimum > row.maximum))) {
            row = {}; row.state = DC_MODEL_INVALID;
            constexpr char reason[] = "The core returned an inconsistent model input.";
            static_assert(sizeof(reason) <= sizeof(row.reason));
            std::memcpy(row.reason, reason, sizeof(reason));
        }
    }
    return inventory;
}
} // namespace druta::ui
