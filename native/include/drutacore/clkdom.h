/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The private clock getter's per-domain rows with their earned names
 * (nvbackend.GPU.read_clock_domains and nvbackend.classify_domain_names).
 * Included by drutacore.h.
 */
#ifndef DRUTACORE_CLKDOM_H
#define DRUTACORE_CLKDOM_H

#include "drutacore/drutacore.h"

#ifdef __cplusplus
extern "C" {
#endif

#define DC_PRIV_DOMAINS 32
#define DC_PRIV_CLK_WORDS 288

/* One row of read_clock_domains. has_* == 0 is Python's None. */
typedef struct dc_clock_domain_row {
    uint32_t domain;
    char name[24];      /* "" when no name has been earned */
    char grade[16];     /* "confirmed" | "likely" | "unnamed" | "unpopulated" */
    char kind[12];      /* "freq" | "pcie_gen" */
    uint32_t prog_khz;
    uint32_t meas_khz;
    uint32_t flags;
    uint32_t srcid;
    uint32_t scale;
    int32_t has_prog_mhz;
    int32_t has_meas_mhz;
    int32_t has_delta_mhz;
    double prog_mhz;
    double meas_mhz;
    double delta_mhz;
} dc_clock_domain_row;

typedef struct dc_clock_domains {
    uint32_t size;      /* sizeof(dc_clock_domains), set by the caller */
    uint32_t count;
    dc_clock_domain_row rows[DC_PRIV_DOMAINS];
} dc_clock_domains;

/* read_clock_domains(pc, core_mhz, mem_nvml): `words` is a payload the caller already holds
 * (DC_PRIV_CLK_WORDS words, e.g. dc_telemetry.priv_clocks) or NULL to read the private getter now.
 * core/mem are the PUBLIC readings in MHz (has_* == 0: None). DC_E_UNAVAILABLE when the getter did
 * not answer (Python's (None, PRIV_UNAVAIL)); the architecture and the Blackwell flag come from the
 * handle, as GPU.arch() / clkdom_is_blackwell(). */
DC_API dc_status DC_CALL dc_gpu_clock_domains(dc_gpu* gpu, const uint32_t* words, int32_t has_core,
                                              uint32_t core_mhz, int32_t has_mem, uint32_t mem_mhz,
                                              dc_clock_domains* out);

/* The pure classifier over a caller-supplied payload: read_clock_domains' row loop followed by
 * classify_domain_names(rows, core_mhz, mem_nvml, blackwell=(architecture == 10), architecture).
 * has_arch == 0 is an unknown architecture (Python None). */
DC_API dc_status DC_CALL dc_clock_domains_classify(const uint32_t* words, size_t word_count, int32_t has_core,
                                                   uint32_t core_mhz, int32_t has_mem, uint32_t mem_mhz,
                                                   int32_t has_arch, uint32_t architecture,
                                                   dc_clock_domains* out);

#ifdef __cplusplus
}
#endif
#endif /* DRUTACORE_CLKDOM_H */
