/* Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <stdint.h>
#include "drutacore/drutacore.h"
#ifdef __cplusplus
extern "C" {
#endif
#define DC_VOLTAGE_PEER_ABSENT 0u
#define DC_VOLTAGE_PEER_AVAILABLE 1u
#define DC_VOLTAGE_PEER_FAILED 2u
#define DC_VOLTAGE_PEER_UNSUPPORTED 3u
#define DC_VOLTAGE_PEER_PSTATES_MAX 7416u
#define DC_VOLTAGE_PEER_CLOCK_MAX 65536u
#define DC_VOLTAGE_PEER_BOOST_SIZE 9248u
#define DC_VOLTAGE_PEER_CURVE_SIZE 7208u
/* Full returned bytes are comparison-only. State 0 means export absent; state
 * 2 means present but not qualified, never an empty request. Curve is evaluated
 * diagnostic data, not a retained request. Caller initializes size. */
typedef struct dc_voltage_peer_packet {uint32_t state,length;int32_t status;uint32_t reserved;} dc_voltage_peer_packet;
typedef struct dc_voltage_peers {
 uint32_t size;dc_target target;
 dc_voltage_peer_packet pstates,clock_control,boost,curve;
 /* Independent voltage-boost percentage getter; unsupported is optional. */
 dc_voltage_peer_packet voltage_boost; int32_t voltage_boost_percent;
 uint32_t clock_mask,clock_version,clock_header,clock_stride,clock_mask_dword,clock_mode,clock_freq_khz;
 uint32_t has_nvvdd_uv,nvvdd_uv,has_msvdd_uv,msvdd_uv,vf_entries;
 uint8_t pstates_bytes[DC_VOLTAGE_PEER_PSTATES_MAX];
 uint8_t clock_bytes[DC_VOLTAGE_PEER_CLOCK_MAX];
 uint8_t boost_bytes[DC_VOLTAGE_PEER_BOOST_SIZE];
 uint8_t curve_bytes[DC_VOLTAGE_PEER_CURVE_SIZE];
} dc_voltage_peers;
/* Locked, paired-target, bounded two-round read-only snapshot. Returns DC_OK
 * only when all present families qualify and retained requests are stable.
 * Per-family states retain absent vs failed; output has no writer authority. */
DC_API dc_status DC_CALL dc_voltage_peers_read(dc_gpu* gpu,dc_voltage_peers* out,dc_error* error);
#ifdef __cplusplus
}
#endif
