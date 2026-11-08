/* Druta - a monitor and tuner for NVIDIA GPUs.
 * Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The pure V/F curve planners (nvbackend.GPU.peak_info, resolve_vf_point,
 * compute_deflatten, evaluate_curve_law, predict_curve, cascade_meta,
 * compute_hard_deflatten, compute_ramp, compute_rephase) over the C ABI.
 * Included by drutacore.h; nothing here touches a driver.
 */
#ifndef DRUTACORE_VFPLAN_H
#define DRUTACORE_VFPLAN_H

#include "drutacore/drutacore.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Filled by the planner port. */

#ifdef __cplusplus
}
#endif
#endif /* DRUTACORE_VFPLAN_H */
