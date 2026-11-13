/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "profile_io.h"
#include "timings_service.h"
#include <vector>
namespace druta::ui {
using TimingProfileRequest=TimingRequest;
struct TimingEditorRequest {
    std::vector<TimingRequest> fields;
    ProfileJson document;
};
// Both calls are pure: they access no files, helper process, GPU or draft.
// Callers must provide the freshly paired identity for the captured snapshot.
ProfileJson capture_timing_profile(const dc_gpu_identity& identity, const TimingSnapshot& snapshot,
    const std::vector<TimingProfileRequest>& requests);
std::vector<TimingProfileRequest> validate_timing_profile(const ProfileJson& profile,
    const dc_gpu_identity& identity, const TimingSnapshot& snapshot);
// Structural recognition only; does not authorize staging or replace live validation.
bool is_legacy_timing_profile(const ProfileJson& profile);
// Returns current-adapter native schema after validating legacy fields and metadata.
ProfileJson normalize_timing_profile(const ProfileJson& profile,const dc_gpu_identity& identity,const TimingSnapshot& snapshot);
// Understood broadcast fields from the qualified capture with staged values
// overlaid. Raw words, structural/inferred/overlapping fields are never saved.
ProfileJson capture_timing_editor_profile(const dc_gpu_identity& identity,const TimingSnapshot& snapshot,
    const std::vector<TimingRequest>& staged);
}
