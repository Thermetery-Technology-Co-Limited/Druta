/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * drutacore C ABI (draft; the surface grows one feature at a time).
 *
 * Rules:
 *   - Plain C: no C++ types, no exceptions, no callbacks that unwind.
 *   - Every function returns a dc_status; outputs go through caller-owned buffers.
 *   - Strings are UTF-8, NUL-terminated; buffer sizes are in bytes and include the NUL.
 *   - A function that needs more buffer returns DC_E_BUFFER and writes the needed size.
 *   - The ABI version changes only when an existing function or struct changes meaning.
 */
#ifndef DRUTACORE_H
#define DRUTACORE_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  if defined(DRUTACORE_BUILDING)
#    define DC_API __declspec(dllexport)
#  else
#    define DC_API __declspec(dllimport)
#  endif
#  define DC_CALL __cdecl
#else
#  define DC_API __attribute__((visibility("default")))
#  define DC_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define DC_ABI_VERSION 1u

typedef int32_t dc_status;
enum {
    DC_OK = 0,
    DC_E_ARGUMENT = -1,  /* a NULL or malformed argument */
    DC_E_BUFFER = -2,    /* caller buffer too small; *needed holds the size */
    DC_E_PARSE = -3,     /* input text does not have the expected shape */
};

/* The ABI version this library implements (DC_ABI_VERSION of its header). */
DC_API uint32_t DC_CALL dc_abi_version(void);

/* "0000:01:00.0" -> domain, bus, device, function. NVML's 8-digit domain is accepted. */
DC_API dc_status DC_CALL dc_parse_slot(const char* text, uint32_t* domain, uint32_t* bus,
                                       uint32_t* device, uint32_t* function);

/* domain, bus, device, function -> "0000:01:00.0" in a caller buffer of at least 13 bytes. */
DC_API dc_status DC_CALL dc_format_slot(uint32_t domain, uint32_t bus, uint32_t device, uint32_t function,
                                        char* buffer, size_t size, size_t* needed);

/* 1 when both parse and share bus and device, else 0 (never an error). */
DC_API int32_t DC_CALL dc_same_slot(const char* a, const char* b);

#ifdef __cplusplus
}
#endif
#endif /* DRUTACORE_H */
