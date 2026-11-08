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

/* ---- GPU identity (nvbackend.Nvml / NvAPI / enumerate_gpus / GPU identity) ------------------- */

/* Test seam: serve driver exports from the caller instead of nvapi64.dll / nvml.dll.
 * resolve(context, library, name) with library "nvapi" or "nvml"; name "" asks whether the
 * library loads at all (non-NULL means yes). NULL restores the platform loader. */
typedef void*(DC_CALL* dc_resolve_fn)(void* context, const char* library, const char* name);
DC_API void DC_CALL dc_set_export_resolver(dc_resolve_fn resolve, void* context);

typedef struct dc_gpu_entry {
    uint32_t size;       /* sizeof(dc_gpu_entry), set by the caller */
    char slot[24];       /* "0000:01:00.0", "" when unknown */
    char name[96];
    char uuid[96];
    int32_t has_devid;   /* 0: NVML had no versioned PCI identity (devid/subsys unset) */
    uint32_t devid;
    uint32_t subsys;
    int32_t nvml_index;  /* -1: NVML did not enumerate it */
    int32_t has_nvapi;
} dc_gpu_entry;

/* Every NVIDIA GPU on the host, ordered by PCI slot. *count receives the total even when the
 * array is too small (then DC_E_BUFFER). A fresh enumeration through both libraries each call. */
DC_API dc_status DC_CALL dc_enumerate_gpus(dc_gpu_entry* entries, size_t capacity, size_t* count);

typedef struct dc_gpu dc_gpu;

/* Open the card at slot ("" or NULL: the lowest slot). Never fails for a missing driver: the
 * handle then reports both halves unavailable. DC_E_ARGUMENT only for a NULL out pointer. */
DC_API dc_status DC_CALL dc_gpu_open(const char* slot, dc_gpu** gpu);
DC_API void DC_CALL dc_gpu_close(dc_gpu* gpu);

typedef struct dc_gpu_identity {
    uint32_t size;             /* sizeof(dc_gpu_identity), set by the caller */
    int32_t nvapi_ok;
    int32_t nvml_ok;
    char nvapi_error[512];     /* NvAPI.err_detail */
    char nvml_error[1024];     /* Nvml.err_detail (lists every nvml.dll location tried) */
    char nvml_path[512];
    char pairing_error[512];   /* "" when the halves agree or could not be compared */
    char requested_slot[24];
    char slot[24];             /* GPU.slot(): NVML's, else NVAPI's, else requested */
    int32_t nvapi_selected;    /* the NVAPI half selected a card (even if later disabled) */
    char nvapi_slot[24];
    uint32_t nvapi_devid;
    uint32_t nvapi_subsys;
    int32_t nvml_selected;
    char nvml_slot[24];
    int32_t nvml_has_devid;
    uint32_t nvml_devid;
    uint32_t nvml_subsys;
    int32_t nvml_index;
    /* GPU.static identity keys */
    char name[96];
    char driver[96];
    char vbios[96];
    char uuid[96];
    int32_t admin;
} dc_gpu_identity;

DC_API dc_status DC_CALL dc_gpu_identity_get(const dc_gpu* gpu, dc_gpu_identity* identity);

/* "NVAPI: ok   |   NVML: FAIL (...)   |   slot 0000:01:00.0" */
DC_API dc_status DC_CALL dc_gpu_status_line(const dc_gpu* gpu, char* buffer, size_t size, size_t* needed);

#ifdef __cplusplus
}
#endif
#endif /* DRUTACORE_H */
