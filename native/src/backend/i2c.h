// Druta - Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstddef>
#include <cstdint>
#include "core/calling.h"
namespace druta::backend {
// NVIDIA's documented NV_I2C_INFO_V3 fields, native alignment. Python railctl
// uses this exact structure with the private Ex IDs and a two-word tail arg.
struct I2cInfoV3 {
    std::uint32_t version, display_mask;
    std::uint8_t is_ddc_port, address;
    std::uint8_t* reg;
    std::uint32_t reg_size;
    std::uint8_t* data;
    std::uint32_t data_size, speed, speed_khz;
    std::uint8_t port;
    std::uint32_t port_set;
};
using I2cExFn = int(DRUTA_CDECL*)(void*, I2cInfoV3*, std::uint32_t*);
struct I2cResult { int status = 0; bool geometry_ok = false; std::uint32_t value = 0; };
I2cResult i2c_exchange(I2cExFn fn, void* handle, std::uint32_t port, std::uint32_t address,
                       std::uint32_t reg, std::uint32_t width, std::uint32_t value, bool write);
I2cResult i2c_exchange_bytes(I2cExFn fn, void* handle, std::uint32_t port, std::uint32_t address,
    std::uint32_t reg, std::uint8_t* data, std::uint32_t count, bool write, bool indexed = false, bool check_extra = false);
bool i2c_controller_payload(std::uint32_t family, std::uint32_t reg, const std::uint8_t* data, std::uint32_t count);
bool i2c_forbidden_write(std::uint32_t reg);
}
