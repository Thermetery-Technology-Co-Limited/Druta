// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// NVAPI: QueryInterface binding, enumeration and selection by PCI slot.
// Port of nvbackend.NvAPI. Every interface is "null if the id does not
// resolve on this driver"; nothing is decided from a driver version.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "backend/exports.h"

namespace druta::backend {

struct NvapiGpu {
    void* handle = nullptr;
    std::uint32_t enum_index = 0;
    std::string name;  // "" when unreadable
    std::uint32_t devid = 0;
    std::uint32_t subsys = 0;
    std::string slot;  // domain always 0000: NVAPI exposes no PCI domain; "" without bus ids
};

// NvAPI_ShortString is 64 bytes.
inline constexpr unsigned kNvapiShortString = 64;

// The public interface ids this slice binds (the rest come with their features).
namespace nvapi_id {
inline constexpr std::uint32_t Initialize = 0x0150E828;
inline constexpr std::uint32_t EnumPhysicalGPUs = 0xE5AC921F;
inline constexpr std::uint32_t GetPCIIdentifiers = 0x2DDFB66E;
inline constexpr std::uint32_t GetErrorMessage = 0x6C2D048C;
inline constexpr std::uint32_t GetBusId = 0x1BE0B8E5;
inline constexpr std::uint32_t GetBusSlotId = 0x2A0A350F;
inline constexpr std::uint32_t GetArchInfo = 0xD8265D24;
inline constexpr std::uint32_t GetFullName = 0xCEEE8E9F;
inline constexpr std::uint32_t GetVbiosVersionString = 0xA561FD7D;
inline constexpr std::uint32_t GetDriverAndBranchVersion = 0x2926AAAD;
// The getters the RM escape captures hook (DESIGN-TRANSPORT.md 2.4).
inline constexpr std::uint32_t PowerPolInfo = 0x34206D86;
inline constexpr std::uint32_t VoltRailsCtlGet = 0xA3070DB0;
inline constexpr std::uint32_t VoltRailsAbs = 0x5D0634EE;
inline constexpr std::uint32_t ForcePstate = 0x025BFB10;  // (handle, pstate index, fallback flag): the legacy P0 request
}  // namespace nvapi_id

class Nvapi {
public:
    explicit Nvapi(const std::string& slot = "");

    bool ok = false;
    void* gpu = nullptr;  // the selected handle
    std::vector<NvapiGpu> gpus;
    const NvapiGpu* selected = nullptr;
    std::string err_detail;

    // The binder: a function pointer for a QueryInterface id, or nullptr.
    void* interface(std::uint32_t id) const;
    template <typename Fn>
    Fn get(std::uint32_t id) const {
        return reinterpret_cast<Fn>(interface(id));
    }
    bool loaded() const { return library_ != nullptr; }

    // status(handle, NvAPI_ShortString) readers; true only on status 0 and text.
    bool short_string(std::uint32_t id, void* handle, std::string& out) const;

    // Drop the NVAPI half after a failed pairing (nvbackend.GPU._pair).
    void disable(const std::string& reason);

private:
    void select(const std::string& slot);
    std::unique_ptr<ExportSource> library_;
    void*(DRUTA_CDECL* query_)(unsigned) = nullptr;
};

}  // namespace druta::backend
