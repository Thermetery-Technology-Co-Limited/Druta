/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * dc_power_arbitration_*: the pure arbitration decoder (power_arbitration.decode, _geometry and
 * ROW_FORMATS). Read-only: it interprets the word arrays of one validated policy-table read and
 * touches no GPU. Included from drutacore.h after its macros and common types; not for direct
 * inclusion.
 *
 * Every string field is Python's text byte for byte; an empty reason is Python's None. */
#ifndef DRUTACORE_POWER_ARBITRATION_H
#define DRUTACORE_POWER_ARBITRATION_H

#ifdef __cplusplus
extern "C" {
#endif

#define DC_ARBITRATION_POLICIES_MAX 32     /* rows a mask can select */
#define DC_ARBITRATION_INPUTS_MAX 8        /* active input slots of one arbitration structure */
#define DC_ARBITRATION_OBSERVATIONS_MAX 6  /* four row observations plus one per vector */
#define DC_ARBITRATION_TEXT 128
#define DC_ARBITRATION_ROW_FORMATS_MAX 8

/* One packet. present = 0 is Python's None. valid = 0 says the caller's sequence held an element
 * that is not an unsigned 32-bit int (a check only a dynamically typed caller can make); words is
 * then not read. words may be NULL only when count is 0. */
typedef struct dc_arbitration_words {
    const uint32_t* words;
    uint32_t count;
    int32_t present;
    int32_t valid;
} dc_arbitration_words;

typedef struct dc_arbitration_input {
    uint32_t client;
    uint32_t value;
} dc_arbitration_input;

/* _arbitration's dict. When available = 0 only reason is set. */
typedef struct dc_arbitration_vector {
    int32_t available;
    char reason[DC_ARBITRATION_TEXT];
    char method[16];                  /* "minimum", "maximum", "sum" */
    uint32_t method_code;
    uint32_t count;                   /* number of inputs */
    uint32_t output;
    uint32_t initial_value;
    dc_arbitration_input inputs[DC_ARBITRATION_INPUTS_MAX];
    int32_t has_computed_output;      /* 0: None (no active inputs) */
    uint32_t computed_output;
    int32_t consistent;               /* -1: None, 0: False, 1: True */
} dc_arbitration_vector;

/* _row's dict. When available = 0, requested/effective/value/delta are absent and host/pmu carry
 * the row's reason. */
typedef struct dc_arbitration_row {
    uint32_t policy;
    uint32_t type;
    uint32_t channel;
    int32_t has_unit;                 /* 0: None (unit_code is neither 0 nor 1) */
    char unit[4];                     /* "mW", "mA" */
    uint32_t unit_code;
    int32_t available;
    char reason[DC_ARBITRATION_TEXT];
    uint32_t requested;
    uint32_t effective;
    uint32_t value;
    int32_t delta;
    dc_arbitration_vector host;
    dc_arbitration_vector pmu;
    uint32_t observation_count;
    char observations[DC_ARBITRATION_OBSERVATIONS_MAX][DC_ARBITRATION_TEXT];
} dc_arbitration_row;

/* decode's dict. has_mask = 0: no "mask" key (a refused decode). */
typedef struct dc_arbitration_report {
    uint32_t size;                    /* sizeof(dc_arbitration_report), set by the caller */
    uint32_t schema;
    int32_t available;
    char reason[DC_ARBITRATION_TEXT];
    int32_t has_mask;
    uint32_t mask;
    uint32_t policy_count;
    dc_arbitration_row policies[DC_ARBITRATION_POLICIES_MAX];
} dc_arbitration_report;

/* power_arbitration.decode(layout, info, control, status). layout = NULL is a layout (or its
 * "sizes") that is not a mapping; otherwise its size must be sizeof(dc_power_layout). Refusals of
 * the packets are reported in out (available = 0, reason), not as a status. DC_E_ARGUMENT for a
 * NULL info/control/status/out, a wrong size field, or a NULL words pointer with a nonzero count. */
DC_API dc_status DC_CALL dc_power_arbitration_decode(const dc_power_layout* layout, const dc_arbitration_words* info,
                                                     const dc_arbitration_words* control,
                                                     const dc_arbitration_words* status, dc_arbitration_report* out);

/* power_arbitration._geometry(layout): DC_OK when understood, else DC_E_UNRECOGNIZED with the
 * refusal in reason (reason may be NULL when reason_size is 0). layout = NULL as above. */
DC_API dc_status DC_CALL dc_power_arbitration_geometry(const dc_power_layout* layout, char* reason,
                                                       uint32_t reason_size);

/* power_arbitration._arbitration(row, offset) over row_bytes bytes of a row (row may be NULL when
 * row_bytes is 0). The decoder only ever passes a whole 0x130-byte status prefix, so this is the
 * only way to reach the truncation refusal. */
DC_API dc_status DC_CALL dc_power_arbitration_vector(const uint8_t* row, uint32_t row_bytes, uint32_t offset,
                                                     dc_arbitration_vector* out);

/* power_arbitration.ROW_FORMATS in ascending order: the INFO row-format bytes decoded. V3 rows are
 * accepted on one board's evidence; this is a diagnostic, never a control gate. */
DC_API dc_status DC_CALL dc_power_arbitration_row_formats(uint32_t* out, uint32_t capacity, uint32_t* count);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_POWER_ARBITRATION_H */
