/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "i2c_driver.h"
#include <array>
namespace druta::ui {
inline constexpr std::array<const char*, 7> i2c_family_names{{
    "Unknown -- Full Scan", "Nuvoton NCT3933U", "NCP4206", "uPI uP9512R",
    "EVGA iCX", "MPS MP2888A", "MPS MP29816"}};
inline const char* i2c_family_name(I2cFamily family) {
    const auto index = static_cast<size_t>(family);
    return index < i2c_family_names.size() ? i2c_family_names[index] : "Unknown controller";
}
}
