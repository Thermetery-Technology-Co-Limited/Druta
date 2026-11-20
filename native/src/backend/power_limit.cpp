// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The board power limit (increment A8); see power_limit.h.
#include "backend/power_limit.h"

#include <algorithm>
#include <cstdio>
#include <exception>
#include <map>
#include <utility>

#include "core/calling.h"

namespace druta::backend {

namespace {

using SetLimitFn = int(DRUTA_CDECL*)(void*, std::uint32_t);

constexpr const char* kSetExport = "nvmlDeviceSetPowerManagementLimit";
constexpr std::int64_t kMissingClient = 0xFFFFFFFF;  // the request word's "no client" sentinel

bool present(const StaticKey& key) { return key.kind == StaticKey::Kind::value; }

std::optional<PowerBounds> client_bounds(const CpReading& reading) {
    const auto& metadata = reading.observation.metadata;
    if (metadata.maximum_mw == 0 || metadata.maximum_mw >= kMissingClient) return std::nullopt;
    return PowerBounds{std::max<std::int64_t>(1, metadata.minimum_mw), metadata.maximum_mw};
}

bool client_is_extended(const CpReading& reading) {
    const auto range = client_bounds(reading);
    return range && !range->contains(reading.observation.requested_mw);
}

PowerBounds xoc_bounds(PowerBounds nominal) {
    return {1, std::min(kMissingClient - 1, std::max(nominal.high, kPowerLimitXocMaxMw))};
}

bool temporary(RmErrorKind kind) {
    return kind == RmErrorKind::transient || kind == RmErrorKind::unstable;
}

void fill(StaticKey& key, const std::optional<std::uint32_t>& value) {
    if (!present(key) && value) {
        key.kind = StaticKey::Kind::value;
        key.value = *value;
    }
}

}  // namespace

PowerBounds normal_power_bounds(const StaticTriState& st) {
    // Retain the historical defaults for the pure compatibility helper.
    return {present(st.pl_min) ? st.pl_min.value : 50000, present(st.pl_max) ? st.pl_max.value : 400000};
}

PowerBounds power_request_bounds(const StaticTriState& st, bool xoc, bool live) {
    PowerBounds b = normal_power_bounds(st);
    b.low = std::max<std::int64_t>(1, b.low);
    return xoc && live ? xoc_bounds(b) : b;
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

bool PowerLimit::live_owns_request() const {
    if (owner_ == Owner::live) return true;
    const auto& last = cp_.live().last;
    return last && client_is_extended(*last);
}

std::optional<std::uint32_t> PowerLimit::read(StaticTriState* st, bool* from_live) {
    if (live_owns_request()) {
        owner_ = Owner::live;
    } else {
        if (auto current = read_nvml()) return current;
        if (owner_ == Owner::nvml) return std::nullopt;
    }
    if (gpu_.nvapi.ok) {
        CpRead<CpReading> reading = live_read(st);
        if (reading.value) {
            if (from_live) *from_live = true;
            if (client_is_extended(*reading.value))
                owner_ = Owner::live;
            return reading.value->observation.requested_mw;
        }
    }
    return std::nullopt;
}

PowerLimit::RequestState PowerLimit::inspect(StaticTriState& st, bool xoc, std::optional<std::int64_t> desired) {
    RequestState state;
    const bool live_owner = live_owns_request();
    if (live_owner) owner_ = Owner::live;
    bool nvml_transient = false;
    const bool nvml_set = gpu_.nvml.ok && gpu_.nvml.has(kSetExport);
    if (!live_owner && nvml_set) {
        state.nominal = telemetry_.read_power_bounds_nvml(nvml_transient);
        if (state.nominal) {
            auto& store = statics_();
            store.pl_min_mw = static_cast<std::uint32_t>(state.nominal->low);
            store.pl_max_mw = static_cast<std::uint32_t>(state.nominal->high);
            fill(st.pl_min, store.pl_min_mw);
            fill(st.pl_max, store.pl_max_mw);
        }
        state.nvml_current = read_nvml();
    }
    const bool nvml_outside = state.nominal && state.nvml_current && !state.nominal->contains(*state.nvml_current);
    const bool probe_extension = xoc && (!desired || !state.nominal || !state.nominal->contains(*desired));
    if (live_owner || probe_extension || !state.nominal || !state.nvml_current || nvml_outside) {
        if (gpu_.nvapi.ok) {
            state.live = live_read(&st);
            const auto& last = cp_.live().last;
            if (last) state.live_range = client_bounds(*last);
        }
        const bool live_outside = state.live.value && client_is_extended(*state.live.value);
        state.use_live = live_owner || live_outside ||
                         (nvml_outside && (state.live_range || temporary(state.live.error.kind))) ||
                         (state.live_range && (!state.nominal || (!state.nvml_current && owner_ != Owner::nvml)));
        if (state.use_live) {
            state.nominal = state.live_range;
            if (state.live.value) owner_ = Owner::live;
            if (state.live_range) {
                // A board default must describe the same route as the writable bounds.
                st.pl_def = {StaticKey::Kind::value, cp_.live().last->observation.metadata.rated_mw};
            }
        }
    }
    if (nvml_transient || (gpu_.nvapi.ok && temporary(state.live.error.kind)))
        state.bounds_error = RmErrorKind::transient;
    return state;
}

PowerBoundsResult PowerLimit::bounds(StaticTriState& st, bool xoc) {
    const RequestState state = inspect(st, xoc);
    if (!state.nominal) return {std::nullopt, state.bounds_error};
    if (!xoc || !state.live_range) return {state.nominal};
    PowerBounds extended = xoc_bounds(*state.live_range);
    extended.high = std::max(extended.high, state.nominal->high);
    return {extended};
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
    if (!(0 < mw && mw < kMissingClient))
        return done(false, "power limit must be a finite number in whole milliwatts");
    const RequestState state = inspect(st, xoc, mw);
    if (!state.nominal)
        return done(false, "hardware power request bounds unavailable; no write issued");
    const bool outside = !state.nominal->contains(mw);
    const bool use_live = state.use_live || outside;
    const bool allow_extension = xoc && state.live_range && xoc_bounds(*state.live_range).contains(mw);
    if (outside && !allow_extension) {
        const auto current = state.use_live
            ? (state.live.value ? std::optional<std::uint32_t>(state.live.value->observation.requested_mw) : std::nullopt)
            : state.nvml_current;
        if (current && *current == mw)
            return done(true, "power request already configured to " + watts_g(mw) + " W");
        if (xoc && !state.live_range)
            return done(false, "live power request not available; no write issued: " + state.live.error.message);
        const PowerBounds range = xoc ? xoc_bounds(*state.live_range) : *state.nominal;
        return done(false, "power request " + watts_g(mw) + " W out of [" + watts_g(range.low) + ".." +
                           watts_g(range.high) + "] W; extended requests require XOC");
    }
    if (use_live) {
        if (!state.live.value)
            return done(false, "live power request not available; no write issued: " + state.live.error.message);
        CpSetResult result = cp_.set_request(mw, peer);
        if (result.after || result.rollback) record_live_dispatch();
        if (!result.after) {
            // The controller already attempted its single compensation.
            if (result.rollback && result.rollback->verified) cp_.set_live_last(result.rollback->readback);
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
    if (!state.nvml_current) return done(false, "configured power-limit readback is unavailable; no write issued");
    owner_ = Owner::nvml;
    NvmlLimitSet written = set_nvml(mw);
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

NvmlLimitSet PowerLimit::set_nvml(std::int64_t mw) {
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
    auto got = read_nvml();
    if (!got) got = read_nvml();
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

// ---- increment B4: stock_power_policy, max_all_power_policies, read_native_power_limit_mw ------ //

namespace {

// struct.pack(f"<{len(words)}I", *words): the words' little-endian bytes.
std::vector<std::uint8_t> packed(const std::vector<std::uint32_t>& words) {
    std::vector<std::uint8_t> bytes;
    bytes.reserve(words.size() * 4);
    for (std::uint32_t word : words)
        for (int shift = 0; shift < 32; shift += 8) bytes.push_back(static_cast<std::uint8_t>(word >> shift));
    return bytes;
}

// next((r for r in rows if r["policy"] == policy), None)
const PolicyRow* row_of(const std::vector<PolicyRow>& rows, std::int64_t policy) {
    for (const PolicyRow& row : rows)
        if (static_cast<std::int64_t>(row.policy) == policy) return &row;
    return nullptr;
}

// `if error:` on read_power_policies' result (an empty text is falsy).
bool failed(const ReadPoliciesResult& r) { return r.error && !r.error->empty(); }

constexpr const char* kIndexError = "list index out of range";  // str() of Python's IndexError

}  // namespace

bool PowerLimit::confirm_clamp(void* context, std::uint32_t, std::uint32_t, std::uint32_t,
                               const std::vector<std::uint32_t>& info, const std::vector<std::uint32_t>& control) {
    // CurrentLimits::state has already checked the generation, the range and the effective value;
    // this is the fresh paired FE observation nvbackend reads there (read_live_power_request, with
    // its bookkeeping and the static back-fill into the caller's keys).
    const Session& s = *static_cast<const Session*>(context);
    const CpRead<CpReading> r = s.self->live_read(s.st);
    if (!r.value) return false;
    const CpObservation& o = r.value->observation;
    const CpMetadata& m = o.metadata;
    return m.role_info.size() > 4 && m.role_info[4] == 0x30 && o.requested_mw > m.maximum_mw &&
           m.native_info == packed(info) && o.native_control == packed(control);
}

PolicyInputs PowerLimit::policy_inputs(void* context) {
    const Session& s = *static_cast<const Session*>(context);
    PolicyInputs in;
    if (std::optional<std::uint32_t> board = s.self->read(s.st)) in.board_mw = *board;
    in.statics = *s.st;  // after the read's back-fill, as Python's dict is read after it
    in.board_policy = s.self->cp_.live().board_policy;
    return in;
}

ReadPoliciesResult PowerLimit::read_policies(StaticTriState& st) {
    Session session{this, &st};
    ReadPoliciesResult r = cl_.read_power_policies(confirm_clamp, &session, policy_inputs, &session);
    if (r.in_bounds && !r.error && r.remember) cp_.set_board_policy(*r.remember);
    return r;
}

StockResult PowerLimit::stock_policy(std::int64_t policy, bool whole, const std::string& name, StaticTriState& st) {
    StockResult out;
    auto done = [&](bool ok, std::string text) {
        out.ok = ok;
        out.message = std::move(text);
        message = out.message;
        return out;
    };
    auto escape = [&] {
        out.in_bounds = false;
        return out;
    };
    // Drop the user's value before anything is validated: a refused stock still unpins it.
    if (0 <= policy && policy <= 0xFFFFFFFF) cl_.pins.erase(static_cast<std::uint32_t>(policy));
    ReadPoliciesResult rows = read_policies(st);
    if (!rows.in_bounds) return escape();
    if (failed(rows)) return done(false, *rows.error);
    const PolicyRow* found = row_of(rows.rows, policy);
    if (!found) return done(false, "policy " + name + " is not in the driver's table");
    if (!found->named_current && !found->writable)
        return done(false, "policy " + name + ": " + CurrentLimits::unwritable_reason(*found));
    if (!(found->minimum <= found->default_value && found->default_value <= found->maximum))
        return done(false, "policy " + name + ": its default lies outside its own range; nothing written");
    const std::int64_t default_value = found->default_value;
    std::string label = "policy " + name;
    for (const CurrentRow& r : cl_.last_rows) {
        if (static_cast<std::int64_t>(r.policy) == policy) {
            label = r.label;
            break;
        }
    }
    std::optional<std::int64_t> board;
    if (std::optional<std::uint32_t> mw = read(&st)) board = *mw;
    if (!board && live_owns_request())
        return done(false, "policy default not written: live board request is unreadable");
    Nvml& nv = gpu_.nvml;
    bool transient = false;
    const auto normal = telemetry_.read_power_bounds_nvml(transient);
    const bool extended = normal && board && !normal->contains(*board);
    const bool live_stock = board && (live_owns_request() || extended || !(nv.ok && nv.has(kSetExport)) || !read_nvml());
    if (live_stock) {
        CpRead<CpReading> observed = live_read(&st);
        if (!observed.value)
            return done(false, "policy default not written: live board request is unreadable (" +
                                   observed.error.message + ")");
        board = observed.value->observation.requested_mw;
    }
    PolicyWriteResult written;
    if (whole) {
        written = cl_.write_policy_limit(policy, default_value);
        if (!written.in_bounds) return escape();
    } else {
        // _write_policy_limit with a policy that is not an int: the table read, `mask = info[1]`,
        // then its refusal.
        PolicyTableResult table = cl_.policy_table();
        if (!table.table)
            written.message = "policy " + name + ": the policy table could not be read (" + table.error.message +
                              "); nothing written";
        else if (table.table->info.size() < 2)
            return escape();
        else
            written.message = "policy " + name + " is not in the driver's table";
    }
    if (!written.ok) return done(false, written.message);
    std::vector<std::string> notes;
    bool ok = true;
    if (live_stock) {
        CpRead<CpReading> observed = live_read(&st);
        if (!observed.value || static_cast<std::int64_t>(observed.value->observation.requested_mw) != *board) {
            const std::string why =
                !observed.value && !observed.error.message.empty() ? observed.error.message : "request changed";
            return done(false, label + " default was sent, but the board request was not preserved exactly (" + why +
                                   ")");
        }
        notes.push_back("board request preserved at " + watts_g(*board) +
                        " W; host-derived policy limits were not recomputed");
    } else if (std::optional<std::uint32_t> now = read(&st)) {
        // Python's nv.dll.nvmlDeviceSetPowerManagementLimit raises AttributeError here.
        if (!(nv.ok && nv.has(kSetExport))) {
            out.setter_missing = true;
            return out;
        }
        NvmlLimitSet sent = set_nvml(*now);
        if (!sent.ok)
            return done(false, label + " is at its default, but re-sending the board limit failed: " + sent.message);
        const PolicyWriteResult pins = reapply(sent.got ? std::optional<std::int64_t>(*sent.got) : std::nullopt);
        if (!pins.in_bounds) return escape();
        ok = pins.ok;
        if (!pins.message.empty()) notes.push_back(pins.message);
    }
    ReadPoliciesResult after = read_policies(st);
    if (!after.in_bounds) return escape();
    const PolicyRow* now = failed(after) ? nullptr : row_of(after.rows, policy);
    std::string text = label + (live_stock ? " is at its rated default: " : " is back to the driver's value: ") +
                       (now ? CurrentLimits::policy_amount(now->requested, now->unit) : std::string("unreadable"));
    for (const std::string& note : notes) text += "; " + note;
    return done(ok, std::move(text));
}

MaxAllResult PowerLimit::max_all(bool xoc, StaticTriState& st, const CpPeerWriter& peer) {
    MaxAllResult out;
    steps.clear();
    auto escape = [&] {
        out.in_bounds = false;
        out.steps.clear();
        return out;
    };
    cl_.pins.clear();
    std::optional<std::int64_t> top;
    if (present(st.pl_max)) top = st.pl_max.value;
    if (xoc) {
        if (const auto range = bounds(st, true).range) {
            if (range->high > top.value_or(0)) top = range->high;
        }
    }
    if (top && *top != 0) {
        const PowerLimitSet r = set(*top, xoc, st, peer);
        if (!r.in_bounds) return escape();
        out.steps.push_back({"power limit", r.ok, r.message});
    }
    ReadPoliciesResult rows = read_policies(st);
    if (!rows.in_bounds) return escape();
    if (failed(rows)) {
        out.steps.push_back({"power policies", false, *rows.error});
        steps = out.steps;
        return out;
    }
    std::map<std::uint32_t, CurrentRow> currents;
    for (const CurrentRow& r : cl_.last_rows) currents[r.policy] = r;
    for (const PolicyRow& row : rows.rows) {
        if (row.board) continue;
        if (row.named_current) {
            const auto it = currents.find(row.policy);
            if (it == currents.end()) continue;
            const CurrentRow& current = it->second;
            std::int64_t ceiling = xoc ? current.maximum_ma : std::max(current.normal_maximum_ma, current.limit_ma);
            ceiling = std::min<std::int64_t>(ceiling, current.maximum_ma);
            if (current.requested_ma < ceiling) {
                Session session{this, &st};
                const SetResult r = cl_.set_current_limit_ma(row.policy, ceiling, false, confirm_clamp, &session);
                out.steps.push_back({current.label, r.ok, r.message});
            }
            continue;
        }
        if (!row.writable || row.requested >= row.maximum) continue;
        const PolicyWriteResult w = cl_.write_policy_limit(row.policy, row.maximum);
        if (!w.in_bounds) return escape();
        out.steps.push_back({"policy " + std::to_string(row.policy), w.ok, w.message});
    }
    steps = out.steps;
    return out;
}

NativeLimit PowerLimit::native_limit(StaticTriState& st) {
    NativeLimit out;
    auto fail = [&](std::string text) {
        out.value.reset();
        out.error = std::move(text);
        message = out.error;
        return out;
    };
    const CpRead<CpReading> observed = live_read(&st);
    if (!observed.value)
        return fail(observed.error.message.empty() ? "native board-policy binding unavailable"
                                                   : observed.error.message);
    const CpObservation& o = observed.value->observation;
    PolicyTableResult read = cl_.policy_table();
    if (!read.table) return fail(read.error.message);
    const PolicyTable& t = *read.table;
    if (!t.control || !t.dynamic) return fail("native power-policy status unavailable");
    const std::vector<std::uint32_t>& info = t.info;
    const std::vector<std::uint32_t>& control = *t.control;
    const std::vector<std::uint32_t>& status = *t.dynamic;
    if (packed(info) != o.metadata.native_info || packed(control) != o.native_control)
        return fail("native power definitions or configured requests changed during status read");
    const std::uint64_t index = o.metadata.native_index;
    const PowerLayout& layout = *t.layout;
    const std::uint64_t meta = (layout.info_base + index * layout.info_stride) / 4;
    const std::uint64_t record = (layout.control_base + index * layout.control_stride) / 4;
    const std::uint64_t state = (layout.status_base + index * layout.status_stride) / 4;
    // Python's short-circuit order: each operand indexes only when every earlier one passed.
    // A missing word is Python's IndexError (the offsets are 64-bit; size_t may be 32).
    auto word = [](const std::vector<std::uint32_t>& words, std::uint64_t at) -> std::optional<std::uint32_t> {
        if (at >= words.size()) return std::nullopt;
        return words[static_cast<std::size_t>(at)];
    };
    const char* mismatch = "native effective power status does not match the bound milliwatt policy";
    const std::optional<std::uint32_t> definition = word(info, meta + 1);
    if (!definition) return fail(kIndexError);
    const std::uint32_t type_id = *definition & 255, unit = *definition >> 16 & 255;
    if (index >= 32 || !(info[1] >> index & 1) || unit != 0) return fail(mismatch);  // info[1]: meta + 1 >= 1
    const std::optional<std::uint32_t> kind = word(control, record);
    if (!kind) return fail(kIndexError);
    if (*kind != type_id) return fail(mismatch);
    const std::optional<std::uint32_t> status_kind = word(status, state);
    if (!status_kind) return fail(kIndexError);
    if ((*status_kind & 255) != type_id) return fail(mismatch);
    const std::optional<std::uint32_t> requested = word(control, record + 1);
    if (!requested) return fail(kIndexError);
    if (*requested != o.requested_mw) return fail(mismatch);
    const std::optional<std::uint32_t> value = word(status, state + 1);
    if (!value) return fail(kIndexError);
    if (*value == 0xFFFFFFFF) return fail("native effective power status is missing or malformed");
    out.value = value;
    message.clear();
    return out;
}

}  // namespace druta::backend
