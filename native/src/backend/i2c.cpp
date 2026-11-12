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
bool i2c_controller_payload(std::uint32_t family, std::uint32_t reg, const std::uint8_t* d, std::uint32_t n) {
    if (!d) return false;
    switch (family) {
    case 1: return reg >= 1 && reg <= 3 && n == 1;
    case 2: return (reg == 0x21 && n == 2) || ((reg == 0xd2 || reg == 0xd3) && n == 1);
    case 3: return (reg == 0x0a || reg == 0x0b || reg == 0x0c || reg == 0x2a) && n == 1;
    case 4:
        if (reg == 0xb2) return n == 5 && d[0] == 4 && d[1] == 0xc6 && d[2] == 0xeb && d[3] == 0xea && d[4] == 0x15;
        if (reg == 0x11) return n == 2 && d[0] == 1 && d[1] <= 15;
        if (reg == 0x12 || reg == 0x1e) return n == 2 && d[0] == 1 && d[1] <= 1;
        if (reg == 0x13 || reg == 0x1f) return n == 2 && d[0] == 1 && d[1] >= 3 && d[1] <= 10;
        if ((reg == 0x10 || reg == 0x18) && n == 4 && d[0] == 3) {
            if (reg == 0x10 && d[1] == 0 && !d[2] && !d[3]) return true;
            const unsigned mv = (unsigned(d[2]) | (unsigned(d[3]) << 8)) * 5;
            return d[1] == 1 && (reg == 0x10 ? mv >= 750 && mv <= 1600 : mv >= 1250 && mv <= 1450);
        }
        return false;
    case 5: return (reg == 0x23 || reg == 0x21 || reg == 0xe1) && n == 2;
    case 6: return reg == 0x22 && n == 2;
    default: return false;
    }
}
I2cResult i2c_exchange_bytes(I2cExFn fn, void* handle, std::uint32_t port, std::uint32_t address,
    std::uint32_t reg, std::uint8_t* data, std::uint32_t count, bool write, bool indexed, bool check_extra) {
    I2cResult out{};
    if (!fn || !handle || !data || port > 255 || address > 127 || reg > 255 || count < 1 || count > 32 || (indexed && (!write || count != 1))) return out;
    // A successful status is not proof that the driver filled the read buffer.
    // Challenge an untouched A5 response once with 5A; a genuine A5 payload
    // then differs from the second seed. Never repeat any write-direction or
    // indexed-prefix operation, even when its dispatch status is uncertain.
    for (unsigned attempt = 0; attempt < (write ? 1u : 2u); ++attempt) {
        std::array<std::uint8_t, 2> prefix{{static_cast<std::uint8_t>(reg), indexed ? data[0] : std::uint8_t(0)}};
        const auto expected_prefix = prefix;
        std::array<std::uint8_t, 32> bytes{};
        if (write) std::memcpy(bytes.data(), data, count);
        else std::memset(bytes.data(), attempt ? 0x5a : 0xa5, count);
        if (indexed) bytes[0] = data[0] ^ 0xff;
        const auto original = bytes;
        I2cInfoV3 packet{}; packet.version = static_cast<std::uint32_t>(sizeof(packet)) | (3u << 16);
        packet.address = static_cast<std::uint8_t>(address << 1); packet.reg = prefix.data(); packet.reg_size = indexed ? 2 : 1;
        packet.data = bytes.data(); packet.data_size = count; packet.speed = 0xffffu; packet.port = static_cast<std::uint8_t>(port); packet.port_set = 1;
        const auto expected = packet; std::uint32_t extra[2]{};
        out.status = fn(handle, &packet, extra);
        out.geometry_ok = packet.version == expected.version && packet.display_mask == expected.display_mask && packet.is_ddc_port == expected.is_ddc_port &&
            packet.address == expected.address && packet.reg == expected.reg && packet.reg_size == expected.reg_size && packet.data == expected.data &&
            packet.data_size == expected.data_size && packet.speed == expected.speed && packet.speed_khz == expected.speed_khz &&
            packet.port == expected.port && packet.port_set == expected.port_set && prefix == expected_prefix &&
            (!(indexed || check_extra) || (extra[0] == 0 && extra[1] == 0));
        if (write && !indexed && bytes != original) out.geometry_ok = false;
        if (indexed && bytes[0] != data[0]) out.geometry_ok = false;
        for (std::uint32_t i = count; i < bytes.size(); ++i) if (bytes[i] != original[i]) out.geometry_ok = false;
        if (!write && out.status == 0 && out.geometry_ok) {
            if (std::memcmp(bytes.data(), original.data(), count) == 0) {
                if (!attempt) continue;
                // Valid geometry but no returned data is a retryable read
                // failure, not an unsupported controller or packet layout.
                out.status = -1;
            } else std::memcpy(data, bytes.data(), count);
        }
        return out;
    }
    return out;
}

}
