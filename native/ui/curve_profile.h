/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "curve_draft.h"
#include "profile_io.h"
#include "session.h"
namespace druta::ui {
// Configured curve only. No file I/O or hardware access.
ProfileJson capture_curve_profile(const LiveData& data);
// Validates the entire document before staging one undoable local gesture.
void stage_curve_profile(const ProfileJson& profile, const SessionSnapshot& snapshot, CurveDraft& draft);
}
