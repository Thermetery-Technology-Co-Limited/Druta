// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The C ABI: thin, exception-free wrappers over the C++ core.
#include "drutacore/drutacore.h"

#include <cstring>

#include "core/slot.h"

extern "C" {

DC_API uint32_t DC_CALL dc_abi_version(void) { return DC_ABI_VERSION; }

DC_API dc_status DC_CALL dc_parse_slot(const char* text, uint32_t* domain, uint32_t* bus, uint32_t* device,
                                       uint32_t* function) {
    if (!text || !domain || !bus || !device || !function) return DC_E_ARGUMENT;
    auto slot = druta::parse_slot(text);
    if (!slot) return DC_E_PARSE;
    *domain = slot->domain;
    *bus = slot->bus;
    *device = slot->device;
    *function = slot->function;
    return DC_OK;
}

DC_API dc_status DC_CALL dc_format_slot(uint32_t domain, uint32_t bus, uint32_t device, uint32_t function,
                                        char* buffer, size_t size, size_t* needed) {
    std::string text = druta::format_slot(domain, bus, device, function);
    if (needed) *needed = text.size() + 1;
    if (!buffer) return DC_E_ARGUMENT;
    if (size < text.size() + 1) return DC_E_BUFFER;
    std::memcpy(buffer, text.c_str(), text.size() + 1);
    return DC_OK;
}

DC_API int32_t DC_CALL dc_same_slot(const char* a, const char* b) {
    if (!a || !b) return 0;
    return druta::same_slot(a, b) ? 1 : 0;
}

}  // extern "C"
