/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * dc_rail_limits_*: the pure rail-limit and hold-headroom helpers. Included from drutacore.h after its macros and
 * common types; not for direct inclusion.
 *
 * Ports of nvbackend.GPU: VOLT_LIMIT_FIELDS, _VOLT_RAIL_ARCHITECTURES, HOLD_HEADROOM_FIELDS,
 * HOLD_HEADROOM_MAX_MARGIN_MV, HOLD_HEADROOM_FALLBACK_GRAIN_MV and the grain bounds, _valid_volt_rail_state,
 * volt_boost_headroom_mv, abs_limit_mv, rail_ceiling_mv, rail_floor_mv, hold_headroom_architecture,
 * hold_headroom_ceiling_mv, _headroom_enforced_mv, hold_headroom_targets, hold_headroom_bound_mv and
 * hold_headroom_allowance_mv. Nothing here touches a driver.
 *
 * Conventions:
 *   - A Python dict value travels as a dc_rl_value whose kind keeps "missing", None, int, float and "another
 *     object" apart, so the helpers keep Python's arithmetic (int + int stays int) and its exceptions. An int
 *     beyond +-2^53 travels as DC_RL_BIG and is refused (DC_E_RL_BIG_INT) only where it meets another int in an
 *     addition (float + big int is Python's own conversion to the nearest double, and is computed). A result that is a Python number comes back as a
 *     dc_rl_value of kind DC_RL_INT or DC_RL_FLOAT.
 *   - Rows and records are structs whose leading `size` the caller sets to sizeof the struct (else
 *     DC_E_ARGUMENT).
 *   - Where the Python raises, these return one of the DC_E_RL_* statuses below.
 */
#ifndef DRUTACORE_RAIL_LIMITS_H
#define DRUTACORE_RAIL_LIMITS_H

#ifdef __cplusplus
extern "C" {
#endif

enum {
    DC_E_RL_KEY = -140,        /* KeyError: fields[key] of a limit the row does not have */
    DC_E_RL_TYPE = -141,       /* TypeError: None or a non-number object in arithmetic or a comparison */
    DC_E_RL_ATTRIBUTE = -142,  /* AttributeError: _base_mv is present but has no .get (None, ...) */
    DC_E_RL_CONVERSION = -143, /* float() of the value raised and the Python lets it through */
    DC_E_RL_BIG_INT = -144     /* int + int beyond +-2^53 (Python's ints are unbounded, these are not), or
                                  Python's OverflowError of converting an int beyond the double range */
};

/* A static, English description of a rail-limit status; other statuses as dc_status_text. */
DC_API const char* DC_CALL dc_rl_status_text(dc_status status);

#define DC_RL_ABSENT 0 /* the key is not in the dict (what .get() returns as None) */
#define DC_RL_NONE 1
#define DC_RL_INT 2    /* int or bool, exact in f */
#define DC_RL_FLOAT 3
#define DC_RL_OTHER 4  /* any other object: truthy is its bool(), has_float/f its float() when that succeeded */
#define DC_RL_BIG 5    /* an int beyond +-2^53: f is its float(), or +-inf where float() overflows */

typedef struct dc_rl_value {
    int32_t kind;
    int32_t truthy;    /* DC_RL_OTHER only */
    int32_t has_float; /* DC_RL_OTHER only */
    int32_t reserved;
    double f;
} dc_rl_value;

/* VOLT_LIMIT_FIELDS, in the order they sit in an RM record; the first three are HOLD_HEADROOM_FIELDS. */
#define DC_RL_FIELDS 4
#define DC_RL_HEADROOM_FIELDS 3
#define DC_RL_RELIABILITY 0
#define DC_RL_ALT_RELIABILITY 1
#define DC_RL_OVERVOLTAGE 2
#define DC_RL_VMIN 3

#define DC_RL_BASES_ABSENT 0  /* no _base_mv key: VOLT_LIMIT_BASE_MV, which is empty */
#define DC_RL_BASES_MAPPING 1 /* base[] holds _base_mv's values (DC_RL_ABSENT where a key is missing) */
#define DC_RL_BASES_NO_GET 2  /* present without .get (None, ...): AttributeError */

/* One rail row as read_volt_rail_limits returns it, or one control state for _valid_volt_rail_state. */
typedef struct dc_rl_row {
    uint32_t size;
    int32_t bases_kind;
    dc_rl_value limit[DC_RL_FIELDS];
    dc_rl_value base[DC_RL_FIELDS];
    dc_rl_value boost_mv;     /* _boost_mv */
    dc_rl_value effective_mv; /* _effective_mv */
    dc_rl_value headroom_mv;  /* _headroom_mv */
    dc_rl_value type;         /* type */
} dc_rl_row;

/* The hold-headroom record (apply_hold_headroom's dict), only the keys the helpers read. */
typedef struct dc_rl_record {
    uint32_t size;
    int32_t truthy; /* bool(record): a non-empty mapping */
    dc_rl_value raised_ceiling_mv;
    dc_rl_value hold_mv;
    dc_rl_value margin_mv;
    dc_rl_value grain_mv;
    dc_rl_value live_offset_mv;
} dc_rl_record;

typedef struct dc_rl_constants {
    uint32_t size;
    uint32_t architecture_count;      /* entries of architectures */
    uint32_t architectures[8];        /* _VOLT_RAIL_ARCHITECTURES */
    double max_margin_mv;             /* HOLD_HEADROOM_MAX_MARGIN_MV */
    /* HOLD_HEADROOM_FALLBACK_GRAIN_MV and hold_headroom_grain_mv's [fallback / 2, 2 * fallback] bounds:
     * half a TU102 V/F point, one card's estimate used only when this card's curve cannot be read. */
    double fallback_grain_mv;
    double grain_min_mv;
    double grain_max_mv;
} dc_rl_constants;

DC_API dc_status DC_CALL dc_rl_constants_get(dc_rl_constants* out);

/* VOLT_LIMIT_FIELDS[field], or NULL beyond DC_RL_FIELDS. */
DC_API const char* DC_CALL dc_rl_field_name(uint32_t field);

/* hold_headroom_architecture with the instance's arch() as a parameter (has_arch = 0 is None):
 * *supported = 1, 0, or -1 for unknown. */
DC_API dc_status DC_CALL dc_rl_volt_rail_architecture(int32_t has_arch, int64_t arch, int32_t* supported);

/* _valid_volt_rail_state(rail, state): *valid = 1 or 0. */
DC_API dc_status DC_CALL dc_rl_valid_volt_rail_state(const dc_rl_value* rail, const dc_rl_row* state,
                                                     int32_t* valid);

/* volt_boost_headroom_mv(fields): fields NULL is None or an empty dict. */
DC_API dc_status DC_CALL dc_rl_volt_boost_headroom_mv(const dc_rl_row* fields, double* out);

/* abs_limit_mv(fields, VOLT_LIMIT_FIELDS[field]). */
DC_API dc_status DC_CALL dc_rl_abs_limit_mv(const dc_rl_row* fields, uint32_t field, dc_rl_value* out);
DC_API dc_status DC_CALL dc_rl_rail_ceiling_mv(const dc_rl_row* fields, dc_rl_value* out);
DC_API dc_status DC_CALL dc_rl_rail_floor_mv(const dc_rl_row* fields, dc_rl_value* out);
DC_API dc_status DC_CALL dc_rl_hold_headroom_ceiling_mv(const dc_rl_row* row, dc_rl_value* out);
/* _headroom_enforced_mv(row, grain): grain NULL is None. */
DC_API dc_status DC_CALL dc_rl_headroom_enforced_mv(const dc_rl_row* row, const dc_rl_value* grain,
                                                    dc_rl_value* out);

#define DC_RL_TARGET_PLANNED 0
#define DC_RL_TARGET_NOT_NUMBERS 1
#define DC_RL_TARGET_NOT_FINITE 2
#define DC_RL_TARGET_MARGIN_NOT_POSITIVE 3
#define DC_RL_TARGET_MARGIN_TOO_LARGE 4
#define DC_RL_TARGET_HOLD_TOO_HIGH 5
#define DC_RL_TARGET_BOOST_NOT_NUMBER 6
#define DC_RL_TARGET_BOOST_NOT_FINITE 7
#define DC_RL_TARGET_FIELD_MISSING 8
#define DC_RL_TARGET_FIELD_NO_REFERENCE 9

/* hold_headroom_targets: reason DC_RL_TARGET_PLANNED is (targets, None) with has_target/target_mv in
 * HOLD_HEADROOM_FIELDS order; any other reason is (None, message). */
typedef struct dc_rl_targets {
    uint32_t size;
    int32_t reason;
    uint32_t field; /* FIELD_MISSING / FIELD_NO_REFERENCE: the term */
    int32_t has_target[DC_RL_HEADROOM_FIELDS];
    double target_mv[DC_RL_HEADROOM_FIELDS];
    char message[768]; /* Python's text; long enough for two %.0f/%.2f renderings of DBL_MAX */
} dc_rl_targets;

DC_API dc_status DC_CALL dc_rl_hold_headroom_targets(const dc_rl_row* fields, const dc_rl_value* hold_mv,
                                                     const dc_rl_value* margin_mv, const dc_rl_value* maximum_mv,
                                                     dc_rl_targets* out);

/* hold_headroom_bound_mv(record) / hold_headroom_allowance_mv(record), with the instance's
 * hold_headroom_record() as `active`: `record or active or {}`; NULL is None. */
DC_API dc_status DC_CALL dc_rl_hold_headroom_bound_mv(const dc_rl_record* record, const dc_rl_record* active,
                                                      double* out);
DC_API dc_status DC_CALL dc_rl_hold_headroom_allowance_mv(const dc_rl_record* record, const dc_rl_record* active,
                                                          double* out);

#ifdef __cplusplus
}
#endif

#endif /* DRUTACORE_RAIL_LIMITS_H */
