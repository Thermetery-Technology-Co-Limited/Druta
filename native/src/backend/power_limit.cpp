// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The board power limit (increment A8); see power_limit.h.
#include "backend/power_limit.h"

#include <algorithm>
#include <cstdio>
#include <utility>

#include "core/calling.h"

namespace druta::backend {

namespace {

using SetLimitFn = int(DRUTA_CDECL*)(void*, std::uint32_t);

constexpr const char* kSetExport = "nvmlDeviceSetPowerManagementLimit";
constexpr std::int64_t kMissingClient = 0xFFFFFFFF;  // the request word's "no client" sentinel

bool present(const StaticKey& key) { return key.kind == StaticKey::Kind::value; }

// any(self.static.get(key) is None for key in ("pl_min_mw", "pl_max_mw", "pl_def_mw"))
bool missing(const StaticTriState& st) { return !present(st.pl_min) || !present(st.pl_max) || !present(st.pl_def); }

bool within(std::int64_t low, std::int64_t value, std::int64_t high) { return low <= value && value <= high; }

void fill(StaticKey& key, const std::optional<std::uint32_t>& value) {
    if (!present(key) && value) {
        key.kind = StaticKey::Kind::value;
        key.value = *value;
    }
}

}  // namespace

PowerBounds normal_power_bounds(const StaticTriState& st) {
    // 50000 and 400000 are Python's fallbacks (nvbackend._normal_power_bounds_mw), kept for parity:
    // they are not device facts, and whether they should exist is an open question for the user.
    return {present(st.pl_min) ? st.pl_min.value : 50000, present(st.pl_max) ? st.pl_max.value : 400000};
}

PowerBounds power_request_bounds(const StaticTriState& st, std::optional<std::int64_t> current, bool xoc, bool live) {
    PowerBounds b = normal_power_bounds(st);
    b.low = std::max<std::int64_t>(1, b.low);
    if (xoc && live) b = {1, std::min<std::int64_t>(0xFFFFFFFE, std::max(b.high, kPowerLimitXocMaxMw))};
    // Unticking XOC retains the actual endpoint; only movement toward the normal range, or
    // keeping the current request, remains available.
    if (current && 0 < *current && *current < kMissingClient)
        b = {std::min(b.low, *current), std::max(b.high, *current)};
    return b;
}

std::string watts_g(std::int64_t mw) {
    // Python's :g is C's %g (six significant digits, trailing zeros dropped, the same exponent
    // spelling) for every finite double; mw / 1000 is exact here as Python's true division is.
    char text[64];
    std::snprintf(text, sizeof text, "%g", static_cast<double>(mw) / 1000.0);
    return text;
}

std::optional<std::uint32_t> PowerLimit::read_nvml() { return telemetry_.read_power_limit_nvml(); }

CpRead<CpReading> PowerLimit::live_read(StaticTriState* st) {
    StaticCapabilities& store = statics_();
    CpRead<CpReading> r = cp_.live_read(store);
    if (r.value && st) {
        // read_live_power_request's back-fill as the binding sees it afterwards: a key that is None
        // (or absent) in its dict takes the handle store's value (native.Gpu.read_live_power_request).
        fill(st->pl_min, store.pl_min_mw);
        fill(st->pl_def, store.pl_def_mw);
        fill(st->pl_max, store.pl_max_mw);
    }
    return r;
}

std::optional<std::uint32_t> PowerLimit::read(StaticTriState* st, bool* from_live) {
    // Configured power request, or nullopt when its getters are unreadable. The enforced limit is
    // a different, potentially delayed constraint and never stands in for it.
    if (std::optional<std::uint32_t> current = read_nvml()) return current;
    if (gpu_.nvapi.ok) {
        CpRead<CpReading> observation = live_read(st);
        if (observation.value) {
            if (from_live) *from_live = true;
            return observation.value->observation.requested_mw;
        }
    }
    return std::nullopt;
}

PowerBounds PowerLimit::bounds(StaticTriState& st, bool xoc) {
    const PowerBounds normal = normal_power_bounds(st);
    std::optional<std::int64_t> current;
    if (std::optional<std::uint32_t> nvml = read_nvml()) current = *nvml;
    bool live = false;
    if (xoc || !current || missing(st) || !within(normal.low, *current, normal.high)) {
        CpRead<CpReading> observation = live_read(&st);
        if (observation.value) {
            live = true;
            current = observation.value->observation.requested_mw;
        } else if (cp_.live().error_kind == RmErrorKind::transient) {
            // The last observation counts as live for the bounds although the read failed.
            const std::optional<CpReading>& last = cp_.live().last;
            if (last) {
                live = true;
                if (!current) current = last->observation.requested_mw;
            }
        }
    }
    return power_request_bounds(st, current, xoc, live);
}

PolicyWriteResult PowerLimit::reapply(std::optional<std::int64_t> board_mw) {
    // _reapply_power_policy_pins stores the board candidate as GPU._board_policy.
    auto store = [](void* context, std::uint32_t policy) {
        static_cast<ClientPowerController*>(context)->set_board_policy(policy);
    };
    return cl_.reapply_power_policy_pins(board_mw, store, &cp_);
}

PowerLimitSet PowerLimit::set(std::int64_t mw, bool xoc, StaticTriState& st, const CpPeerWriter& peer) {
    PowerLimitSet out;
    auto done = [&](bool ok, std::string text) {
        out.ok = ok;
        out.message = std::move(text);
        message = out.message;
        return out;
    };
    if (!(0 < mw && mw < (std::int64_t{1} << 32)))
        return done(false, "power limit must be a finite number in whole milliwatts");
    Nvml& nv = gpu_.nvml;
    const bool nvml_set = nv.ok && nv.has(kSetExport);
    std::optional<std::int64_t> current;
    if (std::optional<std::uint32_t> nvml = read_nvml()) current = *nvml;
    const bool absent = missing(st);
    std::optional<CpReading> observed;
    std::string error;
    const bool probed = !current || !nvml_set || absent;
    auto probe = [&] {
        CpRead<CpReading> r = live_read(&st);
        observed = std::move(r.value);
        error = observed ? std::string() : r.error.message;
    };
    if (probed) probe();
    const PowerBounds normal = normal_power_bounds(st);  // after the probe's back-fill, as Python
    const bool outside = !within(normal.low, mw, normal.high);
    bool returning = current && !within(normal.low, *current, normal.high);
    if (!current) returning = observed && !within(normal.low, observed->observation.requested_mw, normal.high);
    if (outside || returning || !nvml_set || (!current && observed)) {
        if (!probed) probe();
        if (!observed) return done(false, "live power request not available; no write issued: " + error);
        const PowerBounds b = power_request_bounds(st, observed->observation.requested_mw, xoc, true);
        if (!within(b.low, mw, b.high) || mw >= kMissingClient)
            return done(false, "power request " + watts_g(mw) + " W out of [" + watts_g(b.low) + ".." +
                                   watts_g(b.high) + "] W; extended requests require XOC");
        CpSetResult result = cp_.set_request(mw, peer);
        if (!(result.before && result.after)) {
            // The controller compensates exactly once after dispatch; a second transport's SET
            // would obscure that verified result. The verified readback is the last observation.
            if (result.rollback && result.rollback->readback) cp_.set_live_last(result.rollback->readback);
            return done(false, "live power request failed: " + result.error.message);
        }
        const CpReading& after = *result.after;
        cp_.set_live_last(after);
        cp_.set_board_policy(after.observation.metadata.native_index);
        const std::vector<std::uint8_t>& role = after.observation.metadata.role_info;
        if (role.size() > 4 && role[4] == 0x30) {
            const PolicyWriteResult pins = reapply(mw);
            if (!pins.in_bounds) {
                out.in_bounds = false;
                return out;
            }
            return done(pins.ok, "power request configured to " + watts_g(mw) +
                                     " W; the driver may recalculate other policies; downstream power/current "
                                     "limits can be lower" +
                                     (pins.message.empty() ? std::string() : "; " + pins.message));
        }
        // E633 verifies every peer CONTROL byte unchanged. Reapplying session pins here would add
        // unrelated writes from old bookkeeping.
        return done(true, "power request configured to " + watts_g(mw) +
                              " W; downstream power/current limits can be lower");
    }
    if (!nvml_set) return done(false, "SetPowerManagementLimit not available");
    if (!current) return done(false, "configured power-limit readback is unavailable; no write issued");
    NvmlLimitSet written = set_nvml(mw, st);
    if (!written.ok) return done(false, written.message);
    // The driver has just recomputed the limits it couples to the board limit; put back the ones
    // the user set (see read_power_policies).
    const PolicyWriteResult pins = reapply(written.got ? std::optional<std::int64_t>(*written.got) : std::nullopt);
    if (!pins.in_bounds) {
        out.in_bounds = false;
        return out;
    }
    return done(pins.ok, written.message + (pins.message.empty() ? std::string() : "; " + pins.message));
}

NvmlLimitSet PowerLimit::set_nvml(std::int64_t mw, StaticTriState& st) {
    NvmlLimitSet out;
    Nvml& nv = gpu_.nvml;
    auto fn = nv.get<SetLimitFn>(kSetExport);
    if (!fn) {  // the caller checks; Python's attribute access would raise here
        out.message = "SetPowerManagementLimit not available";
        return out;
    }
    const int status = fn(nv.dev, static_cast<std::uint32_t>(static_cast<std::uint64_t>(mw)));  // ctypes' u32(mw)
    if (status != 0) {
        out.message = "power limit failed: " + nv.errstr(status);
        return out;
    }
    const std::optional<std::uint32_t> got = read(&st);
    if (!got) {
        out.message = "power limit was sent, but configured-limit readback failed";
        return out;
    }
    out.got = got;
    if (static_cast<std::int64_t>(*got) != mw) {
        out.message = "power limit requested " + watts_g(mw) + " W, but the driver reports a configured limit of " +
                      watts_g(*got) + " W";
        return out;
    }
    out.ok = true;
    out.message = "power limit configured to " + watts_g(*got) + " W";
    return out;
}

}  // namespace druta::backend
