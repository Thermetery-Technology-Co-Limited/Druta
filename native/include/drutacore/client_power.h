/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * dc_cp_*: the pure client-power tables and geometry helpers (client_power_runtime's module
 * constants, _small_native_geometry, _definition_stride, _native_geometry, _coupled_requests,
 * _project and _matches). Included from drutacore.h after its macros and common types; not for
 * direct inclusion.
 *
 * A failure Python reports by raising returns DC_E_FAILED and fills a dc_cp_error with the
 * exception type and Python's message, in Python's evaluation order. DC_E_ARGUMENT is only for a
 * NULL or malformed argument. */
#ifndef DRUTACORE_CLIENT_POWER_H
#define DRUTACORE_CLIENT_POWER_H

#ifdef __cplusplus
extern "C" {
#endif

/* INFO, CONTROL, SET, ROLE_INFO, NATIVE_INFO, NATIVE_CONTROL, NATIVE_STATUS: the order of
 * commands[], rm26_commands[] and every group_sizes[] row. */
#define DC_CP_COMMAND_COUNT 7
#define DC_CP_SIZE_GROUP_COUNT 7
#define DC_CP_LIST_MAX 8
#define DC_CP_STATE_KEY_COUNT 10
#define DC_CP_STATE_KEY_CHARS 24
#define DC_CP_PAYLOAD_COUNT 5   /* info, control, role_info, native_info, native_control */
#define DC_CP_FIRST_PAYLOAD_KEY 5  /* state_keys[5 + i] is payload i */

/* group_sizes[] rows: SIZES, LEGACY_SIZES, TURING_SIZES, INFO216_SIZES, RM26_SIZES,
 * RM26_EXTENDED_SIZES, A612_COMPACT_SIZES. */
enum {
    DC_CP_SIZES = 0,
    DC_CP_LEGACY_SIZES = 1,
    DC_CP_TURING_SIZES = 2,
    DC_CP_INFO216_SIZES = 3,
    DC_CP_RM26_SIZES = 4,
    DC_CP_RM26_EXTENDED_SIZES = 5,
    DC_CP_A612_COMPACT_SIZES = 6
};

typedef struct dc_cp_constants {
    uint32_t size; /* sizeof(dc_cp_constants), set by the caller */
    uint32_t commands[DC_CP_COMMAND_COUNT];
    uint32_t rm26_commands[DC_CP_COMMAND_COUNT];                  /* _RM26_COMMANDS[commands[i]] */
    uint32_t group_sizes[DC_CP_SIZE_GROUP_COUNT][DC_CP_COMMAND_COUNT];
    uint32_t size_refusal_count;
    uint32_t size_refusals[DC_CP_LIST_MAX];
    uint32_t info_base, info_stride, control_base, control_stride, capacity;
    uint32_t native_info_base, native_info_stride, native_control_base, native_control_stride;
    uint32_t native_status_base, native_status_stride; /* _LAYOUT["status"] */
    uint32_t native_capacity;
    uint32_t role, client, missing;
    uint32_t legacy_group_architecture_count;
    uint32_t legacy_group_architectures[DC_CP_LIST_MAX];
    uint32_t rm26_architecture_count;
    uint32_t rm26_architectures[DC_CP_LIST_MAX];
    char state_keys[DC_CP_STATE_KEY_COUNT][DC_CP_STATE_KEY_CHARS]; /* _STATE_KEYS */
} dc_cp_constants;

DC_API dc_status DC_CALL dc_cp_constants_get(dc_cp_constants* out);

/* binding.get("layout") / binding.get("native_layout") as Python compares them. DC_CP_OTHER is a
 * present value that matches no understood name. */
enum { DC_CP_ABSENT = 0, DC_CP_OTHER = 1 };
enum { DC_CP_LAYOUT_RM26 = 2, DC_CP_LAYOUT_LEGACY32 = 3 };
enum { DC_CP_NATIVE_POLICY124 = 2, DC_CP_NATIVE_RM26_EXTENDED = 3 };

typedef struct dc_cp_binding {
    int32_t layout;        /* DC_CP_ABSENT, DC_CP_OTHER or DC_CP_LAYOUT_* */
    int32_t native_layout; /* DC_CP_ABSENT, DC_CP_OTHER or DC_CP_NATIVE_* */
    int32_t has_native_info_stride; /* the key is present; a value of 0 is Python's falsy stride */
    int64_t native_info_stride;
    int32_t has_native_index; /* absent: binding["native_index"] raises KeyError where Python reads it */
    int64_t native_index;
    int32_t has_client_index;
    int64_t client_index;
} dc_cp_binding;

/* The Python exception a helper raised. */
enum {
    DC_CP_ERROR_NONE = 0,
    DC_CP_ERROR_STRUCT = 1,   /* struct.error(message) */
    DC_CP_ERROR_INDEX = 2,    /* IndexError(message) */
    DC_CP_ERROR_OVERFLOW = 3, /* OverflowError(message) */
    DC_CP_ERROR_KEY = 4,      /* KeyError(message): message is the key */
    DC_CP_ERROR_PAYLOAD = 5,  /* payload `key` of `side` was given absent: Python's fromhex raised there */
    DC_CP_ERROR_BINDING = 6,  /* binding_usable was 0: Python's expected["binding"].get raised there */
    DC_CP_ERROR_RANGE = 7     /* a result outside int64 */
};

typedef struct dc_cp_error {
    int32_t kind;
    uint32_t key; /* DC_CP_ERROR_PAYLOAD: the state_keys index */
    int32_t side; /* DC_CP_ERROR_PAYLOAD: 0 expected / before, 1 observed */
    char message[256];
} dc_cp_error;

/* A decoded hex payload; present = 0 when Python's bytes.fromhex raises on its text. */
typedef struct dc_cp_payload {
    const uint8_t* data;
    uint32_t length;
    int32_t present;
} dc_cp_payload;

typedef struct dc_cp_geometry {
    int64_t info_base, info_stride, control_base, control_stride, control_size;
} dc_cp_geometry;

/* _small_native_geometry(binding) = (control_size, control_stride). */
DC_API dc_status DC_CALL dc_cp_small_native_geometry(const dc_cp_binding* binding, uint32_t* control_size,
                                                     uint32_t* control_stride);

/* _definition_stride(role_info_size); DC_E_RANGE when the result leaves int64. */
DC_API dc_status DC_CALL dc_cp_definition_stride(int64_t role_info_size, int64_t* stride);

/* _native_geometry(binding). */
DC_API dc_status DC_CALL dc_cp_native_geometry(const dc_cp_binding* binding, dc_cp_geometry* out);

/* _coupled_requests: *coupled = role_info[4] == 0x30; IndexError for a shorter payload. */
DC_API dc_status DC_CALL dc_cp_coupled_requests(const uint8_t* role_info, uint32_t length, int32_t* coupled,
                                                dc_cp_error* error);

/* The value _project packs: an int carried in mw, a value without __index__, or an int outside
 * int64 (which struct refuses with the same message as any out-of-range int). */
enum { DC_CP_MW_INTEGER = 0, DC_CP_MW_NOT_INTEGER = 1, DC_CP_MW_OUT_OF_RANGE = 2 };

/* The payload part of _project(before, mw): control_out and native_control_out (each the length
 * of its input) receive the copies with mw packed at the selected rows. A DC_CP_ERROR_PAYLOAD
 * names state key 6 (control_hex) or 9 (native_control_hex). The rest of the result is the deep
 * copy of the state with requested_mw = mw, which the caller builds. */
DC_API dc_status DC_CALL dc_cp_project(const dc_cp_binding* binding, int32_t mw_kind, int64_t mw,
                                       const dc_cp_payload* control, const dc_cp_payload* native_control,
                                       uint8_t* control_out, uint8_t* native_control_out, dc_cp_error* error);

/* How expected[state_keys[k]] compares with the observation's. */
enum {
    DC_CP_CMP_BYTES = 0,     /* payload keys only: compare the two payloads' bytes (both texts canonical) */
    DC_CP_CMP_EQUAL = 1,     /* the caller compared the values */
    DC_CP_CMP_DIFFERENT = 2, /* the caller compared the values */
    DC_CP_CMP_MISSING = 3    /* expected has no such key */
};

typedef struct dc_cp_match_input {
    uint32_t size; /* sizeof(dc_cp_match_input), set by the caller */
    dc_cp_binding binding;  /* expected["binding"] */
    int32_t binding_usable; /* 0: expected["binding"].get raises */
    int32_t compare[DC_CP_STATE_KEY_COUNT];
    int32_t extra_keys;     /* expected has keys beyond _STATE_KEYS */
    dc_cp_payload expected[DC_CP_PAYLOAD_COUNT];
    dc_cp_payload observed[DC_CP_PAYLOAD_COUNT];
} dc_cp_match_input;

/* _matches(expected, observed) once _state(observed) has been taken: *matches is 1 or 0. */
DC_API dc_status DC_CALL dc_cp_matches(const dc_cp_match_input* input, int32_t* matches, dc_cp_error* error);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_CLIENT_POWER_H */
