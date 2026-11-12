/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "timing_transport.h"
#include <drutacore/drutacore.h>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct TimingsApi {
    decltype(&dc_gpu_identity_get) identity = nullptr;
    decltype(&dc_gpu_read_telemetry) telemetry = nullptr;
    decltype(&dc_gpu_capabilities_get) capabilities = nullptr;
    decltype(&dc_gpu_lock) lock = nullptr;
    decltype(&dc_gpu_unlock) unlock = nullptr;
};
struct TimingWord { std::string name, confidence; uint32_t offset = 0, value = 0; };
struct TimingField { std::string name, reg, description; uint32_t lo=0, hi=0, maximum=0; bool structural=false, inferred=false, overlapping=false; };
struct TimingSnapshot {
    bool ok = false, memory_clock_stable = false, top_band = false;
    std::string slot, codename, message;
    std::optional<uint32_t> memory_before_mhz, memory_after_mhz, pstate_before, pstate_after;
    std::optional<int32_t> offset_before, offset_after; // raw driver units
    std::vector<TimingWord> broadcast;
    std::vector<TimingField> fields;
};
struct TimingRequest { std::string field; uint32_t value=0; };
struct TimingPlan {
    bool ok=false, no_change=false;
    std::string message, slot, field, reg, helper_token;
    uint32_t value=0, prior=0, offset=0, mask=0, prior_word=0, target_word=0;
};
struct TimingResult { bool ok=false, may_have_changed=false; std::string message; std::optional<TimingPlan> undo; };
struct TimingBatchPlan {
    bool ok=false,no_change=false;
    std::string message,slot,codename,helper_token;
    TimingSnapshot baseline;
    std::vector<TimingPlan> fields;
};
struct TimingBatchResult {
    bool ok=false,may_have_changed=false;
    std::string message;
    std::optional<TimingBatchPlan> undo;
    // Successfully launched, or entered commit with launch outcome unknown
    // because transport threw. Never contains a later undispatched field.
    std::vector<std::string> launched_fields;
};
class TimingsService {
public:
    TimingsService(TimingsApi api, dc_gpu* gpu, const dc_gpu_identity& target, TimingTransport& transport);
    bool pending() const { return transport_.pending(); }
    TimingSnapshot capture();
    TimingPlan preview(const std::string& field, uint32_t value);
    TimingResult apply(const TimingPlan& plan, const std::function<void(const TimingPlan&)>& before_write = {});
    TimingResult restore(const TimingPlan& undo, const std::function<void(const TimingPlan&)>& before_write = {});
    TimingBatchPlan preview(const std::vector<TimingRequest>& requests);
    // Callback is cumulative before each commit attempt; returned Undo narrows
    // it to the issued/possibly-launched subset when launch status is known.
    TimingBatchResult apply(const TimingBatchPlan& plan,const std::function<void(const TimingBatchPlan&)>& before_write = {});
    TimingBatchResult restore(const TimingBatchPlan& undo,const std::function<void(const TimingBatchPlan&)>& before_write = {});
    TimingBatchPlan preview_initial();
    TimingBatchResult restore_initial(const std::function<void(const TimingBatchPlan&)>& before_write = {});
    const std::optional<TimingSnapshot>& initial_snapshot() const {return initial_;}
private:
    TimingsApi api_; dc_gpu* gpu_; dc_gpu_identity target_{}; TimingTransport& transport_;
    std::optional<TimingSnapshot> initial_;
    std::string initial_token_;
    void remember_initial(const TimingSnapshot& snapshot,const std::string& token);
    TimingBatchResult apply_batch(const TimingBatchPlan& plan,const std::function<void(const TimingBatchPlan&)>& before_write);
};
}
