// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "../refresh_policy.h"

#include <cmath>
#include <cstdio>

namespace {
int failures = 0;
int checks = 0;
void check(bool condition, const char* message) {
    ++checks;
    if (!condition) {
        ++failures;
        std::printf("FAIL: %s\n", message);
    }
}
bool near(double actual, double expected) {
    return std::abs(actual - expected) < 1e-8;
}
using druta::ui::RefreshPolicy;

void default_and_input() {
    RefreshPolicy policy(100.0);
    check(!policy.always_refresh(), "Always refresh defaults off");
    check(policy.rendering_enabled() && policy.telemetry_enabled(), "visible default enables ordinary work");
    check(policy.render_due(100.0) && policy.telemetry_due(100.0), "initial frame and sample are due immediately");
    check(!policy.maintenance_due(100.0), "no maintenance unless an active mode needs it");
    policy.rendered(100.0);
    policy.sampled(100.0);
    check(near(policy.wait_seconds(100.0), 1.0 / 60.0), "initial interaction grace is 60 Hz");
    policy.note_activity(100.005);
    check(!policy.render_due(100.005), "input during active rendering does not bypass the 60 Hz ceiling");
    check(policy.render_due(100.017), "active frame becomes due on schedule");
    policy.rendered(100.017);
    check(!policy.telemetry_due(100.9) && policy.telemetry_due(101.0), "ordinary sampling stays at 1 Hz");
    policy.sampled(101.0);
    policy.rendered(101.0);
    check(near(policy.wait_seconds(101.0), 0.1), "quiet visible window settles to 10 Hz");
    check(!policy.render_due(101.05), "idle frame waits rather than spinning");
    policy.note_activity(101.05);
    check(policy.render_due(101.05), "input wakes an idle frame immediately");
    check(!policy.telemetry_due(101.05), "input does not create another telemetry read");
    policy.rendered(101.05);
    check(near(policy.wait_seconds(101.05), 1.0 / 60.0), "input renews the active grace period");
}

void minimize_pause_and_restore() {
    RefreshPolicy policy;
    policy.rendered(0.0);
    policy.sampled(0.0);
    policy.set_minimized(true, 0.1);
    check(!policy.rendering_enabled() && !policy.telemetry_enabled(), "ordinary minimization disables rendering and sampling");
    check(!policy.render_due(50.0) && !policy.telemetry_due(50.0), "time passing cannot restart paused work");
    check(std::isinf(policy.wait_seconds(50.0)), "quiet minimized app can block on messages");
    policy.set_always_refresh(true, 50.0);
    check(policy.render_due(50.0) && policy.telemetry_due(50.0), "Always refresh restarts both while minimized");
    policy.rendered(50.0);
    policy.sampled(50.0);
    check(near(policy.wait_seconds(60.0), 0.0), "overdue continuous work is immediately due once");
    policy.rendered(60.0);
    policy.sampled(60.0);
    check(near(policy.wait_seconds(60.0), 1.0 / 60.0), "continuous minimized rendering stays at 60 Hz after long inactivity");
    policy.set_explicit_pause(true, 60.1);
    check(!policy.rendering_enabled() && !policy.telemetry_enabled(), "explicit Pause overrides Always refresh");
    policy.set_always_refresh(false, 60.2);
    policy.set_always_refresh(true, 60.3);
    check(!policy.render_due(60.3) && !policy.telemetry_due(60.3), "changing preference cannot cancel explicit Pause");
    policy.set_minimized(false, 100.0);
    check(!policy.explicitly_paused() && policy.always_refresh(), "restore clears manual Pause and preserves preference");
    check(policy.render_due(100.0) && policy.telemetry_due(100.0), "restore requests one immediate frame and fresh sample");
    policy.rendered(100.0);
    policy.sampled(100.0);
    check(!policy.render_due(100.001) && !policy.telemetry_due(100.001), "restoration does not create a catch-up burst");
    policy.set_always_refresh(false, 100.1);
    policy.set_minimized(true, 100.2);
    policy.set_explicit_pause(false, 100.3);
    check(!policy.rendering_enabled(), "clearing explicit pause does not override ordinary minimization");
    policy.set_minimized(false, 101.0);
    policy.rendered(101.0);
    policy.sampled(101.0);
    check(near(policy.wait_seconds(101.0), 1.0 / 60.0), "ordinary restore also renews responsive rendering");
}

void independent_maintenance() {
    RefreshPolicy policy;
    policy.set_explicit_pause(true, 0.0);
    policy.set_maintenance_required(true, 0.0);
    check(policy.maintenance_due(0.0), "newly required maintenance starts immediately while paused");
    check(!policy.render_due(0.0) && !policy.telemetry_due(0.0), "maintenance does not enable ordinary work");
    policy.maintained(0.0);
    check(near(policy.wait_seconds(0.0), 0.25), "paused maintenance owns its independent 250 ms deadline");
    check(!policy.maintenance_due(0.24) && policy.maintenance_due(0.25), "maintenance is neither early nor delayed by rendering");
    policy.maintained(4.0);
    check(!policy.maintenance_due(4.0) && near(policy.wait_seconds(4.0), 0.25), "slow maintenance has no catch-up burst");
    policy.set_maintenance_required(false, 4.1);
    check(!policy.maintenance_due(5.0) && std::isinf(policy.wait_seconds(5.0)), "releasing the last maintenance owner removes recurring wakeups");
    policy.set_maintenance_required(true, 5.1);
    check(policy.maintenance_due(5.1), "a new maintenance owner gets an immediate check");
    policy.maintained(5.1);
    policy.set_explicit_pause(false, 5.2);
    policy.rendered(5.2);
    policy.sampled(5.2);
    policy.rendered(6.0);
    policy.sampled(6.0);
    check(policy.wait_seconds(6.0) == 0.0 && policy.maintenance_due(6.0), "overdue maintenance wins over a later idle-frame deadline");
}

void slow_completion() {
    RefreshPolicy policy;
    policy.rendered(20.0);
    policy.sampled(20.0);
    check(near(policy.wait_seconds(20.0), 0.1), "slow idle render starts a fresh interval");
    check(!policy.render_due(20.05) && !policy.telemetry_due(20.5), "old render/read deadlines are not replayed");
    policy.set_always_refresh(true, 20.6);
    check(policy.render_due(20.6), "continuous preference wakes an overdue frame");
    policy.rendered(20.6);
    check(near(policy.wait_seconds(20.6), 1.0 / 60.0), "continuous rendering still uses completion-based pacing");
}

void renderer_backoff_preserves_services() {
    RefreshPolicy policy(9.0);
    policy.sampled(9.2);
    policy.rendered(10.0);
    check(near(policy.wait_seconds(10.0, 10.5), 0.2), "telemetry deadline wins over renderer retry backoff");
    check(!policy.render_due(10.2, 10.5) && policy.telemetry_due(10.2), "device backoff defers only rendering");
    policy.sampled(10.2);
    check(near(policy.wait_seconds(10.2, 10.5), 0.3), "renderer retry remains scheduled after telemetry finishes");
    policy.set_maintenance_required(true, 10.2);
    check(policy.wait_seconds(10.2, 10.5) == 0.0, "new maintenance starts immediately during renderer backoff");
    policy.maintained(10.2);
    check(near(policy.wait_seconds(10.2, 10.5), 0.25), "maintenance deadline wins over renderer retry backoff");
    check(policy.maintenance_due(10.45) && !policy.render_due(10.45, 10.5), "required maintenance runs before renderer recovery");
    check(policy.render_due(10.5, 10.5), "rendering becomes due at the retry deadline");
    policy.maintained(10.5);
    policy.rendered(10.5);
    check(near(policy.wait_seconds(10.5, 10.5), 0.1), "recovered rendering returns to completion-based idle cadence");
    policy.set_explicit_pause(true, 10.6);
    check(near(policy.wait_seconds(10.6, 99.0), 0.15), "paused maintenance is independent of renderer retry time");
    policy.set_maintenance_required(false, 10.6);
    check(std::isinf(policy.wait_seconds(10.6, 99.0)), "renderer retry does not wake a fully paused app");
}
} // namespace

int main() {
    default_and_input();
    minimize_pause_and_restore();
    independent_maintenance();
    slow_completion();
    renderer_backoff_preserves_services();
    std::printf("Refresh policy: %d checks, %d failures\n", checks, failures);
    return failures == 0 ? 0 : 1;
}
