// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/reset_all.h"

#include <cmath>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>

#include "backend/voltage.h"

namespace druta::backend {
namespace {

constexpr std::uint32_t kArchKepler = 2;  // GPU.ARCH_KEPLER (NVML's enum)
// The NVAPI interfaces reset_all checks for presence before the voltage-boost and curve steps
// (Python: self.nvapi.VoltCtrlGet / VoltCtrlSet / BoostTableSet). The same ids voltage.cpp and
// vfcurve.cpp bind; they live in those files' anonymous namespaces, so they are repeated here.
constexpr std::uint32_t kVoltCtrlGet = 0x9DF23CA1;
constexpr std::uint32_t kVoltCtrlSet = 0xB9306D9B;
constexpr std::uint32_t kBoostTableSet = 0x0733E009;

// str.lower() for the current-policy labels (ASCII: the generation tables' labels are).
std::string lower(std::string text) {
    for (char& c : text)
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    return text;
}

// RailDeltas in VOLT_LIMIT_FIELDS order (reliability, alt_reliability, overvoltage, vmin).
RailFields fields_of(const RailDeltas& row) { return {row.reliability, row.alt_reliability, row.overvoltage, row.vmin}; }

}  // namespace

ResetAllResult reset_all(const ResetParts& p, const ResetHooks& hooks, RailWriterTrace& rail_trace) {
    ResetAllResult out;
    std::vector<ResetStep>& steps = out.steps;
    auto add = [&steps](std::string name, const Outcome& o) { steps.push_back({std::move(name), o.ok, o.message}); };
    auto fail = [&steps](std::string name, std::string message) {
        steps.push_back({std::move(name), false, std::move(message)});
    };

    // Stock means the driver's own values: nothing set by hand is kept or re-applied past this
    // point (self._power_policy_pins = {}).
    p.cl.pins.clear();
    // preserve_curve=False: stock must not recreate a shaped table on the way to zero.
    add("core offset", p.clocks().set_clock_offset(0, 0.0, false));
    add("mem offset", p.clocks().set_clock_offset(2, 0.0, false));

    // The per-domain offsets and the core rail offset: only where the control block answers.
    const ClkDomLayout* layout = p.clkdom.ok() ? p.clkdom.layout() : nullptr;
    if (layout) {
        auto [rows, err] = p.clkdom.read_requests();
        if (!err.empty() || !rows || rows->empty()) {
            fail("clock-domain offsets",
                 "clock-domain offsets were not reset: " + (err.empty() ? std::string("current values unreadable") : err));
        } else {
            // CLKDOM_NAMES: ClkDom's architecture-independent name table (its understood() with no
            // architecture), so the names are the ones the control table itself uses.
            const std::map<std::uint32_t, std::string>& names = p.clkdom.understood(std::nullopt);
            for (const auto& [domain, request] : *rows) {  // sorted(rows): std::map order
                if (!request.freq_khz) continue;
                auto named = names.find(domain);
                std::string name = named != names.end() ? named->second : "domain " + std::to_string(domain);
                add(name + " offset", p.clkdom.set_clk_domain_offset(domain, 0.0));
            }
        }
        if (layout->nvvdd_uv) {
            std::optional<double> rail = p.clkdom.read_rail_offset_mv(0, 0);
            if (!rail) {
                fail("core rail offset", "core rail offset was not reset: current value unreadable");
            } else if (*rail != 0.0) {  // Python's truthiness (NaN is truthy, as here)
                add("core rail offset", p.clkdom.set_rail_offset_mv(0.0, 0, 0));
            }
        }
    }

    // The MSVDD requests: every domain this session wrote or saw, plus the readable candidate.
    std::set<std::uint32_t> domains = p.clkdom.msvdd_domains_written();
    domains.insert(p.clkdom.msvdd_domains_seen().begin(), p.clkdom.msvdd_domains_seen().end());
    const RailOffsetCapability candidate = p.clkdom.rail_offset_capability(1, std::nullopt);
    if (candidate.available && candidate.domain) domains.insert(static_cast<std::uint32_t>(*candidate.domain));
    for (std::uint32_t domain : domains) {  // sorted
        std::optional<double> value = p.clkdom.read_rail_offset_mv(domain, 1);
        const std::string name = "MSVDD request " + std::to_string(domain);
        if (!value) {
            fail(name, "stored request could not be read");
        } else if (*value != 0) {
            add(name, p.clkdom.reset_msvdd_offset_mv(domain));
        }
    }

    // The rail limits: exact first-read values of this session, never claimed as factory stock.
    // _volt_rail_profile() runs twice, as in Python: the second sees the first-read record the
    // read in between may have captured (and reads the architecture again).
    std::optional<RailProfile> profile = p.rails.profile();
    std::optional<RailLimits> cur;
    if (profile) cur = p.rails.limits();
    profile = p.rails.profile();
    if (profile) {
        bool subset = cur.has_value();
        for (std::size_t r = 0; subset && r < kRailCount; ++r)
            if (profile->poweron[r] && !(*cur)[r]) subset = false;
        if (!subset) {
            fail("rail limits", "rail limits were not reset: current values unreadable or incomplete");
            cur.reset();
        }
    }
    bool off_stock = false;
    if (cur && profile) {
        for (std::size_t r = 0; r < kRailCount; ++r) {
            const std::optional<RailLimitRow>& row = (*cur)[r];
            const std::optional<RailInitial>& want = profile->poweron[r];
            if (!row || !want) continue;
            const RailFields fields = fields_of(row->deltas);
            for (std::size_t n = 0; n < fields.size(); ++n) {
                if (std::fabs(fields[n] - (*want)[n] / 1000.0) > 1e-6) {
                    off_stock = true;
                    break;
                }
            }
        }
    }
    if (off_stock) add("rail limits", p.rails.reset_limits(std::nullopt, RailResetRequest{}, rail_trace));

    // The current limits: only a failed read is a failed step.
    const std::vector<CurrentRow> current_limits = p.cl.get_current_limits(hooks.confirm, hooks.confirm_context);
    const std::string read_error = p.cl.read_error;
    if (p.rm.profile_supported() && !read_error.empty())
        fail("current limits", "cannot read current limits: " + read_error);
    for (const CurrentRow& current : current_limits) {
        if (current.limit_ma != current.default_ma || current.requested_ma != current.default_ma) {
            const SetResult r = p.cl.set_current_limit_ma(current.policy, current.default_ma, false, hooks.confirm,
                                                          hooks.confirm_context);
            fail(lower(current.label), r.message);
            steps.back().ok = r.ok;
        }
    }

    // The board power limit, from the caller's static pl_def_mw as it stands now.
    StaticTriState statics;
    if (!hooks.read_statics || !hooks.read_statics(statics)) {
        out.stop = ResetStop::caller;
        return out;
    }
    if (statics.pl_def.kind == StaticKey::Kind::value && statics.pl_def.value != 0) {
        const StaticTriState before = statics;
        const PowerLimitSet r = p.pl.set(statics.pl_def.value, hooks.xoc, statics, hooks.peer);
        if (!hooks.backfill || !hooks.backfill(before, statics)) {  // the live read's back-fill happened
            out.stop = ResetStop::caller;
            return out;
        }
        if (!r.in_bounds) {
            out.stop = ResetStop::index_error;
            return out;
        }
        fail("power limit", r.message);
        steps.back().ok = r.ok;
    } else {
        fail("power limit", "power limit: default unknown, left unchanged");
    }

    // The policies the board limit does not couple. Python's `_power_policy_mask is not None`: a mask
    // the table assigned this session, 0 included, or a nonzero mask set through dc_cl_policy_mask_set
    // (Python's attribute assignment).
    const bool mask_known = p.cl.power_policy_mask_read || p.cl.power_policy_mask != 0;
    if (mask_known && p.rm.profile_supported()) {
        const ReadPoliciesResult read =
            p.cl.read_power_policies(hooks.confirm, hooks.confirm_context, hooks.inputs, hooks.inputs_context);
        if (read.caller_failed) {
            out.stop = ResetStop::caller;
            return out;
        }
        if (!read.in_bounds) {
            out.stop = ResetStop::index_error;
            return out;
        }
        if (read.error && !read.error->empty()) {
            if (!p.cl.read_error.empty()) fail("power policies", "power policies were not reset: " + *read.error);
        } else {
            if (read.remember) p.cp.set_board_policy(read.remember);  // as dc_cl_power_policies_read stores it
            for (const PolicyRow& row : read.rows) {
                if (row.writable && row.requested != row.default_value && row.minimum <= row.default_value &&
                    row.default_value <= row.maximum) {
                    const PolicyWriteResult w = p.cl.write_policy_limit(row.policy, row.default_value);
                    if (!w.in_bounds) {
                        out.stop = ResetStop::index_error;
                        return out;
                    }
                    fail("power policy " + std::to_string(row.policy), w.message);
                    steps.back().ok = w.ok;
                }
            }
        }
    }

    add(kResetLockStep, p.clocks().reset_gpu_clocks(true));
    if (p.legacy_p0.owned()) add(kResetP0LockStep, p.legacy_p0.release());
    // The V/F point lock is a different mechanism from the NVML clock lock above.
    if (p.vflock.available()) add(kResetVfLockStep, p.vflock.clear_vf_lock(std::nullopt, std::nullopt));
    add("fan", p.fan().reset_fan());
    Nvapi& nvapi = p.gpu.nvapi;
    if (nvapi.ok && nvapi.interface(kVoltCtrlGet) && nvapi.interface(kVoltCtrlSet))
        add("voltage boost", set_voltage_boost(p.gpu, 0));
    const std::optional<std::uint32_t> arch = p.telemetry.arch();
    const bool curve_applicable = !(arch && *arch == kArchKepler);  // vf_curve_applicable
    if (curve_applicable && nvapi.ok && nvapi.interface(kBoostTableSet)) add("vf curve", p.curve().reset_vf_curve());
    return out;
}

}  // namespace druta::backend
