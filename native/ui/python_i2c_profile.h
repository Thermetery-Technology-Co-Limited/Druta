// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include "i2c_controls.h"
#include <array>
namespace druta::ui {
struct PythonNct3933Profile {
 I2cRoute route{};std::string profile,rail,recipe_sha256;
 std::array<uint8_t,3> outputs{};uint8_t configuration=0,selection=7;
};
// Strict, pure parsing of the full snapshot's i2c object. Throws on unknown
// controller/format; configuration is comparison-only, never a write request.
PythonNct3933Profile parse_python_nct3933_profile(const ProfileJson& i2c);
struct PythonUp9512rProfile { I2cRoute route{};std::string profile,rail,recipe_sha256;std::array<uint8_t,5> offsets_mv{};bool enabled=false; };
struct PythonNcp4206Profile { I2cRoute route{};std::string profile,rail,recipe_sha256;uint8_t command=0;bool enabled=false; };
PythonUp9512rProfile parse_python_up9512r_profile(const ProfileJson& i2c);
PythonNcp4206Profile parse_python_ncp4206_profile(const ProfileJson& i2c);
}
