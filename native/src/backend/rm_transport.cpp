// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/rm_transport.h"

#include <cstdio>

#include "backend/gdi.h"

namespace druta::backend {
namespace {

RmError unsupported(std::string message) {
    RmError e;
    e.kind = RmErrorKind::unsupported;
    e.fixed = true;
    e.message = std::move(message);
    return e;
}

RmError transient(std::string message) {
    RmError e;
    e.kind = RmErrorKind::transient;
    e.fixed = false;
    e.message = std::move(message);
    return e;
}

constexpr const char* kUnvalidatedGeometry = "unvalidated current-policy transport geometry";

bool envelope_ok(const CapturedTransport& t) {
    const auto& h = t.header;
    return t.header_count == 17 && (h[14] == 0x2080A612u || h[14] == 0x20802612u) &&
           static_cast<std::uint64_t>(h[2]) == 68u + static_cast<std::uint64_t>(h[15]);
}

}  // namespace

GenerationPolicies RmTransport::generation_policies() { return backend::generation_policies(telemetry_.arch()); }

bool RmTransport::profile_supported() { return gpu_.nvapi.ok && !generation_policies().empty(); }

std::optional<CapturedTransport> RmTransport::capture_power() {
    // What THIS attempt saw: a record left by an earlier attempt must not describe a capture
    // that saw nothing.
    observed = Observed{};
    CaptureResult r = capture_power_transport(gpu_, telemetry_);
    observed = r.observed;
    last_capture_no_escape_path_ = r.no_escape_path;
    return r.transport;
}

std::optional<CapturedTransport> RmTransport::capture_legacy_clk() {
    return capture_legacy_clk_transport(gpu_).transport;
}

TransportResult RmTransport::power_transport() {
    TransportResult out;
    if (!power) {
        layout_choice.reset();
        std::optional<CapturedTransport> captured = capture_power();
        if (!captured) {
            char seen[96];
            std::snprintf(seen, sizeof seen, "observed 0x%08X, %u/%u bytes", static_cast<unsigned>(observed.command_id),
                          static_cast<unsigned>(observed.packet_bytes), static_cast<unsigned>(observed.parameter_bytes));
            if (observed.seen &&
                !find_power_layout(PowerLayoutKey{observed.command_id, observed.packet_bytes, observed.parameter_bytes})) {
                // a power GET WAS seen, and its geometry is not one Druta knows
                out.error = unsupported(std::string("unsupported current-policy transport: ") + seen);
                return out;
            }
            // nothing seen, or a known power GET whose getter then failed: a failed read,
            // retried on the next one
            std::string message = "unsupported current-policy transport: ";
            if (observed.seen) {
                message += seen;
                message += ", but the capture did not complete";
            } else {
                message += "no recognized power GET captured";
                // D5: when the capture stopped because gdi32 lacks the escape path (and for no
                // earlier reason), the capability detail is attached so the user sees which
                // export is missing. The classification stays main's retryable one (D4).
                if (last_capture_no_escape_path_) message += " (" + escape_capability().detail + ")";
            }
            out.error = transient(message);
            return out;
        }
        power = captured;
    }
    if (!envelope_ok(*power)) {
        power.reset();
        layout_choice.reset();
        out.error = unsupported(kUnvalidatedGeometry);
        return out;
    }
    out.transport = *power;
    return out;
}

LayoutsResult RmTransport::power_layouts() {
    LayoutsResult out;
    TransportResult t = power_transport();
    if (!t.transport) {
        out.error = t.error;
        return out;
    }
    std::vector<const PowerLayout*> layouts = backend::power_layouts(key(*t.transport));
    if (layouts.empty()) {
        // The transport stays captured: it is understood and other callers legitimately reuse
        // it. Only this driver's current-policy record layout is unknown.
        layout_choice.reset();
        out.error = unsupported(kUnvalidatedGeometry);
        return out;
    }
    out.layouts = std::move(layouts);
    return out;
}

}  // namespace druta::backend
