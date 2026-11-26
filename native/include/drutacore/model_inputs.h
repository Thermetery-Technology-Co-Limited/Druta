/* Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef DRUTACORE_MODEL_INPUTS_H
#define DRUTACORE_MODEL_INPUTS_H
#include "drutacore.h"
#ifdef __cplusplus
extern "C" {
#endif
enum { DC_MODEL_SPEEDO, DC_MODEL_SPEEDO1, DC_MODEL_SPEEDO2, DC_MODEL_IDDQ_NVVDD, DC_MODEL_IDDQ_MSVDD, DC_MODEL_INPUT_COUNT };
enum { DC_MODEL_ABSENT, DC_MODEL_READY, DC_MODEL_NOT_READY, DC_MODEL_INVALID, DC_MODEL_AMBIGUOUS };
typedef struct dc_model_input {
    uint32_t state, index; /* index is meaningful for a unique row only */
    int64_t active, raw_hardware, default_value;
    uint32_t override_present, override_value;
    float minimum, maximum;
    char reason[120];
} dc_model_input;
typedef struct dc_model_inputs {
    uint32_t size;
    dc_target target;
    dc_model_input rows[DC_MODEL_INPUT_COUNT];
} dc_model_inputs;
/* Explicit, read-only NVAPI capture of Speedo and IDDQ input definitions.
 * Provide sizeof(dc_model_inputs) in size. Only READY rows have numeric values.
 * IDDQ is in raw model units. A successful inventory may have zero ready rows.
 * On failure the result is untouched. error, if provided, requires its size.
 * No ROM capture, PCI register access, registry access, SET or driver refresh. */
DC_API dc_status DC_CALL dc_gpu_model_inputs_get(dc_gpu* gpu,dc_model_inputs* out,dc_error* error);
#ifdef __cplusplus
}
#endif
#endif
