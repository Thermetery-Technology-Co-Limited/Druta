/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The pure V/F curve planners (nvbackend.GPU.peak_info, resolve_vf_point,
 * compute_deflatten, evaluate_curve_law, predict_curve, cascade_meta,
 * compute_hard_deflatten, compute_ramp, compute_rephase) over the C ABI.
 * Included by drutacore.h; nothing here touches a driver.
 *
 * Conventions beyond drutacore.h:
 *   - Points are dc_vf_point arrays in the caller's order (read_vf_curve shape). Every point the
 *     planners name comes back as `pos`, its position in that array, beside its idx, so a caller
 *     can echo its own values exactly. At most DC_VFP_POINTS points (else DC_E_ARGUMENT).
 *   - A scalar that Python accepts as None, int or float travels as a dc_vfp_num, so the planners
 *     keep Python's arithmetic: int stays int, a float operand makes the result float. A NULL
 *     dc_vfp_num pointer means None.
 *   - Where the Python raises, these functions return one of the DC_E_VFP_* statuses below; each
 *     names the Python exception it stands for.
 */
#ifndef DRUTACORE_VFPLAN_H
#define DRUTACORE_VFPLAN_H

#include "drutacore/drutacore.h"

#ifdef __cplusplus
extern "C" {
#endif

enum {
    DC_E_VFP_FLOORDIV_ZERO = -100, /* ZeroDivisionError of x // 0 (a grid step that truncates to 0) */
    DC_E_VFP_MOD_ZERO = -101,      /* ZeroDivisionError of x % 0 (compute_rephase) */
    DC_E_VFP_NAN_INT = -102,       /* ValueError of int() or round() of a NaN */
    DC_E_VFP_INF_INT = -103,       /* OverflowError of int() or round() of an infinity */
    DC_E_VFP_NONE_INT = -104,      /* TypeError of int(None) */
    DC_E_VFP_NONE_ADD = -105,      /* TypeError of None + 0.01: a None voltage cap reached below_cap */
    DC_E_VFP_NONE_SUB = -106,      /* TypeError of None - 0.01: a None voltage floor reached above_floor */
    DC_E_VFP_EMPTY_INDEX = -107,   /* IndexError of [][0]: peak_info when the first peak value is NaN */
    DC_E_VFP_EMPTY_MIN = -108,     /* ValueError of min([]): resolve_vf_point when the frequency is NaN */
    DC_E_VFP_FLOAT_SLICE = -109,   /* TypeError of a float slice index: compute_deflatten, float extra */
    DC_E_VFP_UNORDERED = -110,     /* a NaN voltage would be sorted; Python's order is then an artefact
                                      of its sort algorithm, so the core refuses rather than guess */
    DC_E_VFP_BIG_INT = -111,       /* an integer beyond +-2^53: Python's ints are unbounded, these are not */
    DC_E_VFP_MISSING = -112        /* cascade_meta: a lifted row carries no voltage (Python's lookup error) */
};

/* A static, English description of a planner status; other statuses as dc_status_text. */
DC_API const char* DC_CALL dc_vfp_status_text(dc_status status);

#define DC_VFP_NONE 0
#define DC_VFP_INT 1
#define DC_VFP_FLOAT 2

typedef struct dc_vfp_num {
    int32_t kind;      /* DC_VFP_NONE, DC_VFP_INT or DC_VFP_FLOAT */
    int32_t reserved;
    int64_t i;         /* DC_VFP_INT, within +-2^53 */
    double f;          /* DC_VFP_FLOAT, any value including NaN and infinities */
} dc_vfp_num;

/* One planned edit: (idx, volt_mv, old_mhz, new_mhz, delta_khz) of the Python tuple. */
typedef struct dc_vfp_change {
    uint32_t pos;          /* position in the caller's points */
    uint32_t idx;          /* points[pos].idx */
    double volt_mv;        /* points[pos].volt_mv */
    double old_mhz;
    double new_mhz;
    dc_vfp_num delta_khz;  /* the new delta: an int, or a float when a float ceiling clamped */
} dc_vfp_change;

/* ---- peak_info(points, cap_mv) -> (peak_mhz, park_idx, park_mv, n_at_peak) ------------------- */

typedef struct dc_vfp_peak {
    uint32_t size;          /* sizeof(dc_vfp_peak), set by the caller */
    int32_t found;          /* 0: Python's (0.0, None, 0.0, 0) */
    uint32_t peak_pos;      /* the point whose freq_mhz is returned as the peak */
    uint32_t park_pos;      /* the lowest-voltage point of the peak run */
    uint32_t park_idx;
    uint32_t n_at_peak;
    double peak_mhz;
    double park_mv;
} dc_vfp_peak;

/* cap_mv NULL or DC_VFP_NONE is cap_mv=None (the whole table). */
DC_API dc_status DC_CALL dc_vfp_peak_info(const dc_vf_point* points, size_t count, const dc_vfp_num* cap_mv,
                                          dc_vfp_peak* out);

/* ---- resolve_vf_point(points, volt_mv) -> point or None --------------------------------------- */

/* *pos receives the resolved point's position; DC_E_UNAVAILABLE is Python's None. */
DC_API dc_status DC_CALL dc_vfp_resolve_vf_point(const dc_vf_point* points, size_t count,
                                                 const dc_vfp_num* volt_mv, uint32_t* pos);

/* ---- compute_deflatten(points, vcap_mv, max_khz, extra_points_above, step_khz) ---------------- */

typedef struct dc_vfp_deflatten {
    uint32_t size;
    uint32_t change_count;
    dc_vfp_change changes[DC_VFP_POINTS];  /* in position order, as Python's sorted(new) */
    double ceil_before_mhz;
    double ceil_after_mhz;
    int32_t full_meta;      /* 0: meta is only {clamped: False, boundary_idx: None, unique: False} */
    int32_t clamped;
    uint32_t boundary_pos;
    uint32_t boundary_idx;
    int32_t unique;
    int32_t reserved;
    int64_t lowered;
    double lowered_by_mhz;
} dc_vfp_deflatten;

DC_API dc_status DC_CALL dc_vfp_compute_deflatten(const dc_vf_point* points, size_t count,
                                                  const dc_vfp_num* vcap_mv, const dc_vfp_num* max_khz,
                                                  const dc_vfp_num* extra_points_above, const dc_vfp_num* step_khz,
                                                  dc_vfp_deflatten* out);

/* ---- evaluate_curve_law(khz, max_rise_khz) -> [int] ------------------------------------------ */

/* khz in voltage order; out receives count values (capacity >= count, count <= DC_VFP_POINTS). */
DC_API dc_status DC_CALL dc_vfp_evaluate_curve_law(const dc_vfp_num* khz, size_t count,
                                                   const dc_vfp_num* max_rise_khz, int64_t* out, size_t capacity);

/* ---- predict_curve(points, khz, new, max_rise_khz) -> (real, pos) ----------------------------- */

/* values[i] is the planned kHz of position i (Python's new.get(i, khz[i])). real receives the
 * evaluated curve in voltage order and order[k] the position at voltage rank k, so Python's
 * pos is {order[k]: k}. */
DC_API dc_status DC_CALL dc_vfp_predict_curve(const dc_vf_point* points, size_t count, const dc_vfp_num* values,
                                              const dc_vfp_num* max_rise_khz, int64_t* real, uint32_t* order,
                                              size_t capacity);

/* ---- cascade_meta(points, khz, real, pos, idxs) ----------------------------------------------- */

/* One row per entry of idxs, in its order: real[pos[i]], khz[i] and points[i]["volt_mv"]. */
typedef struct dc_vfp_cascade_row {
    dc_vfp_num real;
    dc_vfp_num khz;
    int32_t has_volt;       /* 0: the caller could not look the voltage up */
    int32_t reserved;
    double volt_mv;
} dc_vfp_cascade_row;

typedef struct dc_vfp_cascade {
    uint32_t size;
    uint32_t lifted_below;
    double lift_max_mhz;
    int32_t has_lowest;     /* 0: lift_lowest_mv is None */
    uint32_t lowest_row;    /* the row whose volt_mv is lift_lowest_mv */
    double lift_lowest_mv;
    uint32_t missing_row;   /* DC_E_VFP_MISSING: the first lifted row without a voltage */
    uint32_t reserved;
} dc_vfp_cascade;

DC_API dc_status DC_CALL dc_vfp_cascade_meta(const dc_vfp_cascade_row* rows, size_t count, dc_vfp_cascade* out);

/* ---- compute_hard_deflatten(points, floor_mv, target_khz, step_khz) --------------------------- */

typedef struct dc_vfp_hard {
    uint32_t size;
    uint32_t change_count;
    dc_vfp_change changes[DC_VFP_POINTS];
    double floor_before_mhz;
    double target_after_mhz;
    double target_mhz;
    int32_t has_band;       /* 0: floor_idx, floor_mv, park_idx, park_mv are None */
    uint32_t n_flat;
    uint32_t floor_pos;
    uint32_t floor_idx;
    double floor_mv;
    uint32_t park_pos;
    uint32_t park_idx;
    double park_mv;
    double park_mhz;
    int32_t parks_at_floor;
    uint32_t lifted_below;
    double lift_max_mhz;
    int32_t has_lift_lowest;  /* 0: lift_lowest_mv is None */
    uint32_t lift_lowest_pos;
    double lift_lowest_mv;
} dc_vfp_hard;

DC_API dc_status DC_CALL dc_vfp_compute_hard_deflatten(const dc_vf_point* points, size_t count,
                                                       const dc_vfp_num* floor_mv, const dc_vfp_num* target_khz,
                                                       const dc_vfp_num* step_khz, dc_vfp_hard* out);

/* ---- compute_ramp(points, lo_mv, cap_mv, max_khz, step_khz) ----------------------------------- */

typedef struct dc_vfp_ramp {
    uint32_t size;
    uint32_t change_count;
    dc_vfp_change changes[DC_VFP_POINTS];
    double ceil_before_mhz;
    double ceil_after_mhz;
    int32_t has_band;       /* 0: boundary/lo/cap fields are None */
    int32_t clamped;
    int32_t unique;
    uint32_t boundary_pos;  /* also cap_idx's point */
    uint32_t boundary_idx;
    uint32_t lo_pos;
    uint32_t lo_idx;
    uint32_t rungs;
    double lo_mv;
    double cap_mv;
    double top_mhz;
    double floor_before_mhz;
    double floor_after_mhz;
    double floor_cost_mhz;
    uint32_t leveled_above;
    int32_t has_under_band;   /* 0: under_band_mhz is None */
    double under_band_mhz;
    uint32_t shadowed;
    uint32_t delivered;
    uint32_t lifted_below;
    int32_t has_lift_lowest;  /* 0: lift_lowest_mv is None */
    double lift_max_mhz;
    uint32_t lift_lowest_pos;
    uint32_t dropped_rungs;
    double lift_lowest_mv;
    char dropped_reason[64];
} dc_vfp_ramp;

DC_API dc_status DC_CALL dc_vfp_compute_ramp(const dc_vf_point* points, size_t count, const dc_vfp_num* lo_mv,
                                             const dc_vfp_num* cap_mv, const dc_vfp_num* max_khz,
                                             const dc_vfp_num* step_khz, dc_vfp_ramp* out);

/* ---- compute_rephase(deltas, step_khz) -> (changes, phase) ------------------------------------ */

typedef struct dc_vfp_rephase_change {
    uint32_t pos;           /* position in the caller's deltas (Python's dict order) */
    uint32_t reserved;
    int64_t delta_khz;
} dc_vfp_rephase_change;

typedef struct dc_vfp_rephase {
    uint32_t size;
    uint32_t change_count;
    int64_t phase;
    dc_vfp_rephase_change changes[DC_VFP_POINTS];  /* in the deltas' order */
} dc_vfp_rephase;

DC_API dc_status DC_CALL dc_vfp_compute_rephase(const dc_vfp_num* deltas, size_t count, const dc_vfp_num* step_khz,
                                                dc_vfp_rephase* out);

#ifdef __cplusplus
}
#endif
#endif /* DRUTACORE_VFPLAN_H */
