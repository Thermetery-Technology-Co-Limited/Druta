// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

#include "core/calling.h"

namespace druta::win {

struct InterfaceGuid {
    std::uint32_t a;
    std::uint16_t b, c;
    std::uint8_t d[8];
};
// GUID_DISPLAY_DEVICE_ARRIVAL identifies adapters, including those without an attached display.
inline constexpr InterfaceGuid kDisplayDeviceArrival{0x1ca05180, 0xa699, 0x450a, {0x9a,0x0c,0xde,0x4f,0xbe,0x3d,0xdd,0x89}};
static_assert(sizeof(InterfaceGuid) == 16);
using InterfaceSizeFn = std::uint32_t(DRUTA_STDCALL*)(std::uint32_t*, const InterfaceGuid*, const char16_t*, std::uint32_t);
using InterfaceListFn = std::uint32_t(DRUTA_STDCALL*)(const InterfaceGuid*, const char16_t*, char16_t*, std::uint32_t, std::uint32_t);
inline constexpr const char* kInterfaceSize = "CM_Get_Device_Interface_List_SizeW";
inline constexpr const char* kInterfaceList = "CM_Get_Device_Interface_ListW";

// Both the core and the standalone read-only probe consume this MULTI_SZ.
// A topology change between the size and list calls can require a new size.
inline std::string display_interface_paths(InterfaceSizeFn size, InterfaceListFn list, std::vector<std::u16string>& paths) {
    constexpr std::uint32_t buffer_small = 0x1a;  // CR_BUFFER_SMALL
    constexpr std::uint32_t max_chars = 1024 * 1024;
    for (int attempt = 0; attempt < 3; ++attempt) {
        std::uint32_t count = 0;
        auto status = size(&count, &kDisplayDeviceArrival, nullptr, 0 /* PRESENT */);
        if (status == buffer_small) continue;
        if (status != 0) return std::string(kInterfaceSize) + " CONFIGRET " + std::to_string(status);
        if (!count || count > max_chars) return "invalid display interface list size";
        std::vector<char16_t> data(count, u'\uffff');
        status = list(&kDisplayDeviceArrival, nullptr, data.data(), count, 0);
        if (status == buffer_small) continue;
        if (status != 0) return std::string(kInterfaceList) + " CONFIGRET " + std::to_string(status);
        std::vector<std::u16string> result;
        auto entry = data.begin();
        while (entry != data.end() && *entry) {
            auto end = std::find(entry, data.end(), u'\0');
            if (end == data.end()) return "unterminated display interface path";
            result.emplace_back(entry, end);
            entry = end + 1;
        }
        if (entry == data.end()) return "unterminated display interface list";
        paths.swap(result);
        return {};
    }
    return "display interface list changed size repeatedly (CR_BUFFER_SMALL)";
}

}  // namespace druta::win
