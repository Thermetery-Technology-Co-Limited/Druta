// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The calling convention of every driver export and every callback that
// crosses the C ABI. Both NVAPI and NVML are cdecl on 32-bit Windows.
#pragma once

#if defined(_WIN32)
#define DRUTA_CDECL __cdecl
#else
#define DRUTA_CDECL
#endif
