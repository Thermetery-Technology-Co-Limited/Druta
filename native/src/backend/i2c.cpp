// Druta - Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/i2c.h"
#include <array>
#include <cstring>
namespace druta::backend {
// https://docs.nvidia.com/nvapi/structNV__I2C__INFO__V3.html
// https://docs.nvidia.com/nvapi/group__i2capi.html
// The public NVIDIA NV_I2C_INFO_V3 definition uses MAKE_NVAPI_VERSION(type,3)
// and ordinary native packing. Public definition is evidence for layout, not
// evidence that the private Ex transport accepts 32-bit packets. Production
// Ex calls therefore refuse x86 until that separate protocol is established.
static_assert(offsetof(I2cInfoV3, reg) == (sizeof(void*) == 8 ? 16u : 12u), "NVAPI I2C reg pointer offset");
static_assert(offsetof(I2cInfoV3, data) == (sizeof(void*) == 8 ? 32u : 20u), "NVAPI I2C data pointer offset");
static_assert(offsetof(I2cInfoV3, port_set) == (sizeof(void*) == 8 ? 56u : 40u), "NVAPI I2C port flag offset");
static_assert(sizeof(I2cInfoV3) == (sizeof(void*) == 8 ? 64u : 44u), "NVAPI I2C V3 size");
bool i2c_forbidden_write(std::uint32_t reg) { return reg == 0x04 || (reg >= 0x11 && reg <= 0x18); }
I2cResult i2c_exchange(I2cExFn fn, void* handle, std::uint32_t port, std::uint32_t address,
                       std::uint32_t reg, std::uint32_t width, std::uint32_t value, bool write) {
    I2cResult out{};
    if (!fn || !handle || port > 255 || address > 127 || reg > 255 || width < 1 || width > 4 ||
        (write && (i2c_forbidden_write(reg) || (width < 4 && (value >> (width * 8)) != 0)))) return out;
    std::uint8_t command = static_cast<std::uint8_t>(reg);
    std::array<std::uint8_t, 4> bytes{};
    for (std::uint32_t i = 0; i < width; ++i) bytes[i] = static_cast<std::uint8_t>((value >> (8 * i)) & 0xffu);
    const auto original = bytes;
    I2cInfoV3 packet{}; packet.version = static_cast<std::uint32_t>(sizeof(packet)) | (3u << 16);
    packet.address = static_cast<std::uint8_t>(address << 1); packet.reg = &command; packet.reg_size = 1;
    packet.data = bytes.data(); packet.data_size = width; packet.speed = 0xffffu;
    packet.port = static_cast<std::uint8_t>(port); packet.port_set = 1;
    const I2cInfoV3 expected = packet;
    std::uint32_t extra[2]{};
    out.status = fn(handle, &packet, extra);
    out.geometry_ok = packet.version == expected.version && packet.display_mask == expected.display_mask &&
        packet.is_ddc_port == expected.is_ddc_port && packet.address == expected.address && packet.reg == expected.reg &&
        packet.reg_size == expected.reg_size && packet.data == expected.data && packet.data_size == expected.data_size &&
        packet.speed == expected.speed && packet.speed_khz == expected.speed_khz && packet.port == expected.port &&
        packet.port_set == expected.port_set && command == reg && (!write || bytes == original);
    if (!write) for (std::uint32_t i = width; i < 4; ++i) if (bytes[i] != 0) out.geometry_ok = false;
    if (out.status == 0 && out.geometry_ok) for (std::uint32_t i = 0; i < width; ++i) out.value |= std::uint32_t(bytes[i]) << (8 * i);
    return out;
}
}
