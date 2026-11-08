// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The D3DKMT structures the transport uses, for both bitnesses, without
// windows.h so the Linux build can assert their sizes. Every entry point is
// `long fn(void*)` (NTAPI), exactly as the Python binds them.
// Python's _Escape (nvbackend 6915-6918) is the 32-byte x64 form; the x86
// form is 28 bytes and has never run.
#pragma once

#include <cstdint>

#include "core/calling.h"

namespace druta::win {

using KmtFn = long(DRUTA_STDCALL*)(void*);

struct D3DKMT_ESCAPE {
    std::uint32_t hAdapter, hDevice, Type, Flags;
    void* pPrivateDriverData;
    std::uint32_t PrivateDriverDataSize, hContext;
};

struct D3DKMT_ADAPTERINFO {  // nvbackend's Adapter: handle, luid[2], sources, preferred
    std::uint32_t hAdapter, LuidLow;
    std::int32_t LuidHigh;
    std::uint32_t NumOfSources;
    std::int32_t bPresentMoveRegionsPreferred;
};

struct D3DKMT_ENUMADAPTERS2 {
    std::uint32_t NumAdapters;
    D3DKMT_ADAPTERINFO* pAdapters;
};

struct D3DKMT_ENUMADAPTERS {  // the Windows 7 form: a fixed array (fallback increment)
    std::uint32_t NumAdapters;
    D3DKMT_ADAPTERINFO Adapters[16];
};

struct D3DKMT_QUERYADAPTERINFO {
    std::uint32_t hAdapter, Type;
    void* pPrivateDriverData;
    std::uint32_t PrivateDriverDataSize;
};

struct D3DKMT_ADAPTERADDRESS {
    std::uint32_t BusNumber, DeviceNumber, FunctionNumber;
};

struct D3DKMT_CLOSEADAPTER {
    std::uint32_t hAdapter;
};

inline constexpr std::uint32_t KMTQAITYPE_ADAPTERADDRESS = 6;
inline constexpr std::uint32_t kMaxAdapters = 16;  // Python enumerates into a 16-entry array

static_assert(sizeof(D3DKMT_ADAPTERINFO) == 20);
static_assert(sizeof(D3DKMT_ADAPTERADDRESS) == 12);
static_assert(sizeof(D3DKMT_CLOSEADAPTER) == 4);
static_assert(sizeof(D3DKMT_ENUMADAPTERS) == 324);
static_assert(sizeof(D3DKMT_ESCAPE) == (sizeof(void*) == 8 ? 32 : 28));
static_assert(sizeof(D3DKMT_ENUMADAPTERS2) == (sizeof(void*) == 8 ? 16 : 8));
static_assert(sizeof(D3DKMT_QUERYADAPTERINFO) == (sizeof(void*) == 8 ? 24 : 16));

}  // namespace druta::win
