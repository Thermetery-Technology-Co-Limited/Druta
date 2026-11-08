/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The pure current-limit tables (nvbackend.GPU.CURRENT_LIMIT_GENERATION_POLICIES,
 * _CURRENT_LIMIT_LAYOUTS, _CURRENT_LIMIT_LAYOUT_FALLBACKS), exposed so the parity tests can
 * compare them row by row. Included by drutacore.h; do not include directly. */
#ifndef DRUTACORE_POWER_LAYOUTS_H
#define DRUTACORE_POWER_LAYOUTS_H

#ifdef __cplusplus
extern "C" {
#endif

/* The four GETs a layout sizes: 0x2080A618 info, 0x2080A619 status, 0x2080A61A control,
 * 0x2080E61B control (E). sizes[i] and commands[i] follow this order. */
#define DC_POWER_GET_COUNT 4
/* A layout plus its fallbacks never exceeds this. */
#define DC_POWER_LAYOUTS_MAX 4
#define DC_GENERATION_POLICIES_MAX 8

typedef struct dc_power_layout {
    uint32_t size;                        /* sizeof(dc_power_layout), set by the caller */
    uint32_t sizes[DC_POWER_GET_COUNT];
    int32_t has_commands;                 /* the 2612 family maps each GET to its own command */
    uint32_t commands[DC_POWER_GET_COUNT];
    uint32_t info_base, info_stride;
    uint32_t status_base, status_stride;
    uint32_t control_base, control_stride;
    int32_t has_status_mask_word;
    uint32_t status_mask_word;
    int32_t has_control_mask_word;
    uint32_t control_mask_word;
} dc_power_layout;

typedef struct dc_generation_policy {
    uint32_t policy;
    char label[32];
    uint32_t type;
    uint32_t channel;
    uint32_t normal_maximum_ma;
} dc_generation_policy;

/* _CURRENT_LIMIT_LAYOUTS[key] followed by _CURRENT_LIMIT_LAYOUT_FALLBACKS[key]. *count is the
 * number of layouts (0 for an unknown key); at most capacity rows are written. Every row's size
 * must be sizeof(dc_power_layout). */
DC_API dc_status DC_CALL dc_power_layouts_lookup(uint32_t command, uint32_t packet_bytes, uint32_t parameter_bytes,
                                                 dc_power_layout* out, uint32_t capacity, uint32_t* count);

/* CURRENT_LIMIT_GENERATION_POLICIES.get(arch, {}) in policy order; *count = 0 for an unknown or
 * absent (has_arch = 0) architecture. */
DC_API dc_status DC_CALL dc_generation_policies(int32_t has_arch, uint32_t arch, dc_generation_policy* out,
                                                uint32_t capacity, uint32_t* count);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_POWER_LAYOUTS_H */
