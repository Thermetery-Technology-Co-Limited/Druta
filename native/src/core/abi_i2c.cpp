// Druta - Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/abi_internal.h"
#include "backend/i2c.h"
#include "core/slot.h"
#include <cstdio>
#include <cstring>

namespace {
using PciFn = int(DRUTA_CDECL*)(void*, unsigned*, unsigned*, unsigned*, unsigned*);
using BusFn = int(DRUTA_CDECL*)(void*, unsigned*);
using EnumFn = int(DRUTA_CDECL*)(void**, unsigned*);
constexpr std::uint32_t kReadEx = 0x4D7B0709u, kWriteEx = 0x283AC65Au;
void error_set(dc_error* error, int kind, const char* message) {
    if (!error) return;
    dc_error result{}; result.size = sizeof(result); result.kind = kind;
    std::snprintf(result.message, sizeof(result.message), "%s", message); *error = result;
}
bool target_ok(dc_gpu* handle) {
    auto& gpu = handle->gpu; auto& nv = gpu.nvapi; const auto* selected = nv.selected;
    if (!gpu.pairing_error.empty() || !nv.ok || !selected || !nv.gpu || selected->handle != nv.gpu) return false;
    auto enumerate = nv.get<EnumFn>(druta::backend::nvapi_id::EnumPhysicalGPUs);
    if (!enumerate) return false;
    void* current[64]{}; unsigned count = 0;
    if (enumerate(current, &count) != 0 || count > 64) return false;
    unsigned matches = 0; for (unsigned i = 0; i < count; ++i) if (current[i] == nv.gpu) ++matches;
    if (matches != 1) return false;
    auto bus_fn = nv.get<BusFn>(druta::backend::nvapi_id::GetBusId);
    auto slot_fn = nv.get<BusFn>(druta::backend::nvapi_id::GetBusSlotId);
    auto pci_fn = nv.get<PciFn>(druta::backend::nvapi_id::GetPCIIdentifiers);
    if (!bus_fn || !slot_fn || !pci_fn) return false;
    unsigned bus = 0, slot = 0, pci = 0, subsys = 0, rev = 0, ext = 0;
    if (bus_fn(nv.gpu, &bus) != 0 || slot_fn(nv.gpu, &slot) != 0 || pci_fn(nv.gpu, &pci, &subsys, &rev, &ext) != 0) return false;
    const auto selected_slot = druta::parse_slot(selected->slot);
    if (!selected_slot || selected_slot->bus != bus || selected_slot->device != slot || (pci >> 16) != selected->devid || subsys != selected->subsys || !druta::same_slot(gpu.slot(), selected->slot)) return false;
    const auto* other = gpu.nvml.selected;
    if (other && (!druta::same_slot(other->slot, selected->slot) || (other->devid && *other->devid != selected->devid) || (other->subsys && *other->subsys != subsys))) return false;
    return true;
}
dc_status transaction(dc_gpu* gpu, std::uint32_t port, std::uint32_t addr, std::uint32_t reg, std::uint32_t width,
                      std::uint32_t value, bool write, std::uint32_t* out, dc_error* error) {
    if (!gpu || (!write && !out) || (error && error->size != sizeof(dc_error))) return DC_E_ARGUMENT;
    if (port > 255 || addr > 127 || reg > 255 || width < 1 || width > 4 || (write && width < 4 && (value >> (width * 8)) != 0)) {
        error_set(error, DC_ERR_INVALID, "Invalid I2C port, address, register, width, or value"); return DC_E_ARGUMENT;
    }
    if (write && druta::backend::i2c_forbidden_write(reg)) { error_set(error, DC_ERR_INVALID, "Nonvolatile or password I2C command refused"); return DC_E_ARGUMENT; }
    if constexpr (sizeof(void*) != 8) { error_set(error, DC_ERR_UNSUPPORTED, "NVAPI Ex I2C packet geometry is not qualified on 32-bit processes"); return DC_E_UNAVAILABLE; } else {
    druta::LockGuard guard(gpu->gpu.lock());
    if (!target_ok(gpu)) { error_set(error, DC_ERR_IDENTITY, "Fresh NVAPI PCI target verification failed; no I2C transaction issued"); return DC_E_UNAVAILABLE; }
    auto fn = gpu->gpu.nvapi.get<druta::backend::I2cExFn>(write ? kWriteEx : kReadEx);
    if (!fn) { error_set(error, DC_ERR_UNSUPPORTED, "NVAPI Ex I2C interface unavailable"); return DC_E_UNAVAILABLE; }
    const auto result = druta::backend::i2c_exchange(fn, gpu->gpu.nvapi.gpu, port, addr, reg, width, value, write);
    if (!target_ok(gpu)) { error_set(error, DC_ERR_IDENTITY, write ? "GPU identity changed after I2C write; write may have occurred" : "GPU identity changed during I2C read"); return DC_E_FAILED; }
    if (!result.geometry_ok) { error_set(error, DC_ERR_MALFORMED, write ? "I2C packet or write payload changed; write may have occurred" : "I2C packet geometry changed during read"); return DC_E_FAILED; }
    if (result.status != 0) {
        char message[128]{}; std::snprintf(message, sizeof(message), "NVAPI I2C %s failed with status %d%s", write ? "write" : "read", result.status, write ? "; write may have occurred" : "");
        error_set(error, DC_ERR_TRANSIENT, message); return DC_E_FAILED;
    }
    if (!write) *out = result.value;
    error_set(error, DC_ERR_NONE, ""); return DC_OK;
    }
}
dc_status bytes_transaction(dc_gpu* gpu, std::uint32_t family, std::uint32_t port, std::uint32_t addr,
    std::uint32_t reg, std::uint8_t* data, std::uint32_t count, bool write, dc_error* error) {
    if (!gpu || !data || (error && error->size != sizeof(dc_error))) return DC_E_ARGUMENT;
    if (port > 255 || addr > 127 || reg > 255 || count < 1 || count > 32 ||
        (write && !druta::backend::i2c_controller_payload(family, reg, data, count))) {
        error_set(error, DC_ERR_INVALID, "Invalid I2C controller packet or route"); return DC_E_ARGUMENT;
    }
    if constexpr (sizeof(void*) != 8) { error_set(error, DC_ERR_UNSUPPORTED, "NVAPI Ex I2C packet geometry is not qualified on 32-bit processes"); return DC_E_UNAVAILABLE; } else {
        druta::LockGuard guard(gpu->gpu.lock());
        if (!target_ok(gpu)) { error_set(error, DC_ERR_IDENTITY, "Fresh NVAPI PCI target verification failed"); return DC_E_UNAVAILABLE; }
        bool indexed = false;
        if (write && family == 3) {
            struct Arch { std::uint32_t version, architecture, implementation, revision; };
            using ArchFn = int(DRUTA_CDECL*)(void*, Arch*);
            auto arch_fn = gpu->gpu.nvapi.get<ArchFn>(0xD8265D24u);
            if (arch_fn) {
                bool valid = false;
                for (unsigned revision : {2u, 1u}) {
                    Arch a{}; a.version = sizeof(a) | (revision << 16); const auto expected = a.version;
                    int status = arch_fn(gpu->gpu.nvapi.gpu, &a);
                    if (status == -9) continue;
                    if (status == 0 && a.version == expected && a.architecture && target_ok(gpu)) { indexed = a.architecture == 0x190; valid = true; }
                    break;
                }
                if (!valid) { error_set(error, DC_ERR_TRANSIENT, "uP9512R transport architecture unavailable"); return DC_E_UNAVAILABLE; }
            }
        }
        auto fn = gpu->gpu.nvapi.get<druta::backend::I2cExFn>((!write || indexed) ? kReadEx : kWriteEx);
        if (!fn) { error_set(error, DC_ERR_UNSUPPORTED, "NVAPI Ex I2C interface unavailable"); return DC_E_UNAVAILABLE; }
        std::uint8_t local[32]{}; if (write) std::memcpy(local, data, count);
        const auto result = druta::backend::i2c_exchange_bytes(fn, gpu->gpu.nvapi.gpu, port, addr, reg, local, count, write, indexed, family==3);
        if (!target_ok(gpu)) { error_set(error, DC_ERR_IDENTITY, write ? "GPU identity changed after write; write may have occurred" : "GPU identity changed during read"); return DC_E_FAILED; }
        if (!result.geometry_ok) { error_set(error, DC_ERR_MALFORMED, write ? "I2C packet or payload changed; write may have occurred" : "I2C packet geometry changed during read"); return DC_E_FAILED; }
        if (result.status != 0) { error_set(error, DC_ERR_TRANSIENT, write ? "I2C write failed; write may have occurred" : "I2C read failed"); return DC_E_FAILED; }
        if (!write) std::memcpy(data, local, count);
        error_set(error, DC_ERR_NONE, ""); return DC_OK;
    }
}

}
extern "C" {
DC_API dc_status DC_CALL dc_i2c_read_bytes(dc_gpu* gpu, uint32_t port, uint32_t addr, uint32_t reg, uint8_t* out, uint32_t count, dc_error* error) {
    try { return bytes_transaction(gpu, 0, port, addr, reg, out, count, false, error); }
    catch (...) { error_set(error, DC_ERR_TRANSIENT, "I2C read failed unexpectedly"); return DC_E_FAILED; }
}
DC_API dc_status DC_CALL dc_i2c_controller_write(dc_gpu* gpu, uint32_t family, uint32_t port, uint32_t addr, uint32_t reg, const uint8_t* data, uint32_t count, dc_error* error) {
    try { return bytes_transaction(gpu, family, port, addr, reg, const_cast<uint8_t*>(data), count, true, error); }
    catch (...) { error_set(error, DC_ERR_TRANSIENT, "I2C write failed unexpectedly; write may have occurred"); return DC_E_FAILED; }
}

DC_API dc_status DC_CALL dc_i2c_read(dc_gpu* gpu, uint32_t port, uint32_t addr7, uint32_t reg,
                                      uint32_t width, uint32_t* value, dc_error* error) {
    try { return transaction(gpu, port, addr7, reg, width, 0, false, value, error); }
    catch (...) { error_set(error, DC_ERR_TRANSIENT, "I2C read failed unexpectedly"); return DC_E_FAILED; }
}
DC_API dc_status DC_CALL dc_i2c_write(dc_gpu* gpu, uint32_t port, uint32_t addr7, uint32_t reg,
                                       uint32_t width, uint32_t value, dc_error* error) {
    try { return transaction(gpu, port, addr7, reg, width, value, true, nullptr, error); }
    catch (...) { error_set(error, DC_ERR_TRANSIENT, "I2C write failed unexpectedly; write may have occurred"); return DC_E_FAILED; }
}
}
