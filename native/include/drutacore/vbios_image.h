/* Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef DRUTACORE_VBIOS_IMAGE_H
#define DRUTACORE_VBIOS_IMAGE_H
#include "drutacore.h"
#ifdef __cplusplus
extern "C" {
#endif
#define DC_VBIOS_IMAGE_EXPORT 0xFC13EE11u
#define DC_VBIOS_IMAGE_VERSION 0x20010u
#define DC_VBIOS_IMAGE_CAPACITY 0xFA000u
/* One read-only, paired-target V2 capture. This entry does not establish
 * matching-read stability, interpret the ROM, or permit any image write.
 * Allocate DC_VBIOS_IMAGE_CAPACITY bytes to hold any understood response.
 * On DC_OK only, copies 32..CAPACITY bytes and sets *image_size. Every failure
 * leaves image and image_size untouched; error, when present, is diagnostic.
 * No size-query mode. DC_E_BUFFER means capacity cannot hold the response.
 * Missing export / unsupported pointer geometry: UNAVAILABLE + UNSUPPORTED.
 * Present API error: FAILED + TRANSIENT. Invalid descriptor/guards/length:
 * FAILED + MALFORMED. Changed/unpaired target: FAILED + IDENTITY.
 * No internal retries and no driver/device/version allowlists. */
DC_API dc_status DC_CALL dc_gpu_vbios_image_get(dc_gpu* gpu,uint8_t* image,uint32_t capacity,uint32_t* image_size,dc_error* error);
#ifdef __cplusplus
}
#endif
#endif
