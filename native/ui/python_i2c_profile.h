// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include "i2c_driver.h"
#include <array>
namespace druta::ui {
// Identity of the understood released canonical/discovered recipe only.
// No device compatibility claim; caller must freshly qualify its controller.
ProfileJson python_i2c_profile_identity(I2cFamily,I2cRoute,int page=-1,int scale=-1);
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
struct PythonOffsetProfile {I2cFamily family=I2cFamily::unknown;I2cRoute route{};std::string profile,rail,recipe_sha256;double offset_mv=0;int page=-1,scale=-1;};
PythonOffsetProfile parse_python_offset_profile(const ProfileJson& i2c);
}
