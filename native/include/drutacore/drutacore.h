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
    DC_E_ARGUMENT = -1,      /* a NULL or malformed argument */
    DC_E_BUFFER = -2,        /* caller buffer too small; *needed holds the size */
    DC_E_PARSE = -3,         /* input text does not have the expected shape */
    DC_E_UNRECOGNIZED = -4,  /* packet is not an exact understood layout */
    DC_E_MASK = -5,          /* rail mask selects no rail or an unsupported one */
    DC_E_RECORDS = -6,       /* records do not match the rail mask exactly */
    DC_E_RANGE = -7,         /* a value is outside its allowed range */
};

/* The ABI version this library implements (DC_ABI_VERSION of its header). */
DC_API uint32_t DC_CALL dc_abi_version(void);

/* A static, English description of a status code. */
DC_API const char* DC_CALL dc_status_text(dc_status status);

/* "0000:01:00.0" -> domain, bus, device, function. NVML's 8-digit domain is accepted. */
DC_API dc_status DC_CALL dc_parse_slot(const char* text, uint32_t* domain, uint32_t* bus,
                                       uint32_t* device, uint32_t* function);

/* domain, bus, device, function -> "0000:01:00.0" in a caller buffer of at least 13 bytes. */
DC_API dc_status DC_CALL dc_format_slot(uint32_t domain, uint32_t bus, uint32_t device, uint32_t function,
                                        char* buffer, size_t size, size_t* needed);

/* 1 when both parse and share bus and device, else 0 (never an error). */
DC_API int32_t DC_CALL dc_same_slot(const char* a, const char* b);

/* ---- voltage-rail packets (rail_transport) ---------------------------------------------------- */

typedef struct dc_rail_layout {
    uint32_t packet_size;
    uint32_t params_size;
    uint32_t get_command;
    uint32_t set_command;
    uint32_t mask_word;
    uint32_t boost_word;
    uint32_t record0;
    uint32_t record_stride;
    uint32_t record_type;
    int32_t valid_word; /* -1 when the layout has no valid word */
} dc_rail_layout;

typedef struct dc_rail_record {
    uint32_t rail; /* 0 or 1 */
    int32_t values[4];
} dc_rail_record;

/* The understood GET layouts, in the same order as rail_transport.KNOWN_LAYOUTS. */
DC_API uint32_t DC_CALL dc_rail_layout_count(void);
DC_API dc_status DC_CALL dc_rail_layout_at(uint32_t index, dc_rail_layout* layout);

/* Index of the exact understood GET layout of payload, or DC_E_UNRECOGNIZED. */
DC_API dc_status DC_CALL dc_rail_recognize(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                           uint32_t* layout_index);

/* Build the SET packet (same size as payload) that changes only the owned words.
 * records holds exactly one record per rail selected in requested_mask. */
DC_API dc_status DC_CALL dc_rail_prepare_write(const uint8_t* payload, size_t size, uint32_t requested_mask,
                                               const dc_rail_record* records, size_t record_count, uint32_t boost,
                                               uint8_t* out, size_t out_size, size_t* needed);

/* A compact description for diagnostics ("rail packet size=... layout=..."). */
DC_API dc_status DC_CALL dc_rail_packet_summary(const uint8_t* payload, size_t size, char* buffer, size_t buffer_size,
                                                size_t* needed);

#ifdef __cplusplus
}
#endif
#endif /* DRUTACORE_H */
