// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <limits>

namespace druta::ui {

// UI-thread scheduling only. Times are finite, monotonic seconds supplied by
// the host. Completion times, rather than old deadlines, schedule the next job:
// a slow render/read or a long pause cannot accumulate a catch-up queue.
// The host owns operation lifetimes and rejects stale in-flight results.
class RefreshPolicy {
public:
    explicit RefreshPolicy(double now = 0.0) : last_activity_(now) {}

    void set_always_refresh(bool enabled, double now) {
        if (always_refresh_ == enabled)
            return;
        const bool was_enabled = rendering_enabled();
        always_refresh_ = enabled;
        note_activity(now);
        availability_changed(was_enabled, now);
    }

    void set_minimized(bool minimized, double now) {
        const bool was_enabled = rendering_enabled();
        const bool restored = minimized_ && !minimized;
        minimized_ = minimized;
        if (!minimized)
            explicit_pause_ = false;
        if (restored) {
            last_activity_ = now;
            frame_pending_ = true;
        }
        availability_changed(was_enabled, now);
    }

    void set_explicit_pause(bool paused, double now) {
        const bool was_enabled = rendering_enabled();
        explicit_pause_ = paused;
        availability_changed(was_enabled, now);
    }

    void set_maintenance_required(bool required, double /*now*/) {
        if (required && !maintenance_required_)
            maintenance_pending_ = true;
        maintenance_required_ = required;
    }

    void note_activity(double now) {
        // Wake an idle frame immediately, but a stream of input must not defeat
        // the active 60 Hz ceiling. This does not request extra telemetry.
        if (!always_refresh_ && now - last_activity_ >= active_grace)
            frame_pending_ = true;
        last_activity_ = now;
    }

    bool rendering_enabled() const {
        return !explicit_pause_ && (!minimized_ || always_refresh_);
    }
    bool telemetry_enabled() const { return rendering_enabled(); }
    bool always_refresh() const { return always_refresh_; }
    bool minimized() const { return minimized_; }
    bool explicitly_paused() const { return explicit_pause_; }

    bool render_due(double now, double render_not_before = 0.0) const {
        return rendering_enabled() && now >= render_not_before && frame_wait(now) == 0.0;
    }
    bool telemetry_due(double now) const {
        return telemetry_enabled()
            && remaining(now, last_sample_, sample_pending_, sample_interval) == 0.0;
    }
    bool maintenance_due(double now) const {
        return maintenance_required_
            && remaining(now, last_maintenance_, maintenance_pending_, maintenance_interval) == 0.0;
    }

    void rendered(double now) {
        last_frame_ = now;
        frame_pending_ = false;
    }
    void sampled(double now) {
        last_sample_ = now;
        sample_pending_ = false;
    }
    void maintained(double now) {
        last_maintenance_ = now;
        maintenance_pending_ = false;
    }

    // Infinity means no recurring work: the host can block until a message or
    // a worker completion wakes it. Do not convert infinity to an integer timeout.
    // Device recovery can defer rendering without delaying an earlier sample or
    // maintenance deadline. Pass the same render_not_before to render_due().
    double wait_seconds(double now, double render_not_before = 0.0) const {
        double wait = std::numeric_limits<double>::infinity();
        if (rendering_enabled())
            wait = std::max(frame_wait(now), render_not_before - now);
        if (telemetry_enabled())
            wait = std::min(wait, remaining(now, last_sample_, sample_pending_, sample_interval));
        if (maintenance_required_)
            wait = std::min(wait, remaining(now, last_maintenance_, maintenance_pending_, maintenance_interval));
        return wait;
    }

    static constexpr double active_interval = 1.0 / 60.0;
    static constexpr double idle_interval = 1.0 / 10.0;
    static constexpr double active_grace = 0.75;
    static constexpr double sample_interval = 1.0;
    static constexpr double maintenance_interval = 0.25;

private:
    static double remaining(double now, double completed, bool pending, double interval) {
        return pending ? 0.0 : std::max(0.0, completed + interval - now);
    }
    double frame_wait(double now) const {
        const double interval = always_refresh_ || now - last_activity_ < active_grace
            ? active_interval : idle_interval;
        return remaining(now, last_frame_, frame_pending_, interval);
    }
    void availability_changed(bool was_enabled, double now) {
        if (!was_enabled && rendering_enabled()) {
            last_activity_ = now;
            frame_pending_ = sample_pending_ = true;
        }
    }

    double last_activity_ = 0.0;
    double last_frame_ = 0.0;
    double last_sample_ = 0.0;
    double last_maintenance_ = 0.0;
    bool always_refresh_ = false;
    bool minimized_ = false;
    bool explicit_pause_ = false;
    bool maintenance_required_ = false;
    bool frame_pending_ = true;
    bool sample_pending_ = true;
    bool maintenance_pending_ = true;
};

} // namespace druta::ui
