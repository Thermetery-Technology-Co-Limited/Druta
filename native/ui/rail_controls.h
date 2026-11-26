/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <drutacore/drutacore.h>
#include <array>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
namespace druta::ui {
struct RailApi {
    decltype(&dc_gpu_lock) lock = nullptr;
    decltype(&dc_gpu_unlock) unlock = nullptr;
    decltype(&dc_gpu_identity_get) identity = nullptr;
    decltype(&dc_rails_limits) limits = nullptr;
    decltype(&dc_rails_limit_fields) fields = nullptr;
    decltype(&dc_rails_profile) profile = nullptr;
    decltype(&dc_rails_initial_get) initial = nullptr;
    decltype(&dc_rails_hold_record_get) hold = nullptr;
    decltype(&dc_rails_set_limits_raw) set_raw = nullptr;
    decltype(&dc_rails_reset_limits) reset = nullptr;
    decltype(&dc_rails_state) state = nullptr; // optional: live/effective readout only
    decltype(&dc_rails_writer_evidence_get) writer_evidence = nullptr;
};
struct RailField {
    bool established = false, stale = false;
    std::optional<std::int32_t> requested_uv, initial_uv;
    std::optional<double> estimated_base_mv;
};
// Python build_volt_limits_rows / refresh_rail_live readout of one rail: the
// limits read's "_boost_mv" and the state read's absolute fields, live and
// effective. Display only; never a request. Stale after a failed read.
// Read by RailControls::readouts(), never by read() or a write preflight.
struct RailReadout {
    bool stale = false;
    bool listed = false; // the rail is a key of read_volt_rail_limits()
    std::optional<double> boost_mv;
    std::optional<dc_rails_state_row> state;
};
struct RailRow {
    std::optional<std::int32_t> type;
    std::array<RailField, DC_RAILS_LIMIT_FIELDS> fields{};
};
struct RailUndo {
    int rail = -1;
    std::uint32_t field = 0;
    std::int32_t type = 0, requested_uv = 0;
    // A paired write (Python's linked reliability + alt-reliability) also
    // records the partner field's prior request; restore() puts both back in
    // one setter call.
    std::optional<std::uint32_t> partner_field;
    std::int32_t partner_requested_uv = 0;
};
// Python VLIM_LINKED: Link ties only reliability (0) and alt_reliability (1),
// in VOLT_LIMIT_FIELDS order. Overvoltage and vmin are never linked.
constexpr bool rail_linked_pair(std::uint32_t a, std::uint32_t b) { return (a == 0 && b == 1) || (a == 1 && b == 0); }
struct RailResult {
    bool ok = false, may_have_changed = false;
    std::string message;
    std::optional<RailUndo> undo;
};
// Per-handle lifetime: caller owns the handle and explicit session mode bits.
// Never share a service between GPU handles or hold it across a driver reset.
class RailControls {
public:
    RailControls(RailApi api, dc_gpu* handle, const dc_gpu_identity& target);
    // When requested, return the effective NVVDD cap from this limits read.
    std::array<RailRow, DC_RAILS_COUNT> read(std::optional<double>* nvvdd_effective_mv = nullptr);
    // Display readout. with_limits: also re-read which rails the limits read
    // lists and their boost (Python build_volt_limits_rows); without it only the
    // state getter runs (Python refresh_rail_live, every tick) and the listing
    // and boost from the last full readout are kept.
    // The optional cap comes from the current state read even if the separate
    // edit-limit listing is unavailable.
    std::array<RailReadout, DC_RAILS_COUNT> readouts(bool with_limits = true,
        std::optional<double>* nvvdd_effective_mv = nullptr);
    RailResult apply(int rail, std::uint32_t field, std::int32_t raw_uv, bool rail_mode, bool xoc,
                     const std::function<void(const RailUndo&)>& before_write = {});
    // Python set_volt_rail_limits(rail, a=..., b=...): both fields in one setter
    // call, preflighted together and both read back before success.
    RailResult apply_pair(int rail, std::uint32_t field_a, std::int32_t raw_uv_a, std::uint32_t field_b, std::int32_t raw_uv_b,
                          bool rail_mode, bool xoc, const std::function<void(const RailUndo&)>& before_write = {});
    RailResult stock(int rail, std::uint32_t field, const std::function<void(const RailUndo&)>& before_write = {}); // captured initial, allowed with rail mode off
    RailResult restore(const RailUndo& undo, bool rail_mode, bool xoc);
private:
    RailApi api_;
    dc_gpu* gpu_;
    dc_gpu_identity target_{};
    std::array<RailRow, DC_RAILS_COUNT> last_{};
    std::array<RailReadout, DC_RAILS_COUNT> last_readouts_{};
    std::array<std::optional<std::int32_t>, DC_RAILS_COUNT> initial_type_{};
    struct Target { std::uint32_t field = 0; std::int32_t value = 0; };
    RailResult write(int rail, const Target* targets, std::size_t count, bool mode, bool xoc, bool stock,
                     const std::optional<std::int32_t>& expected_type,
                     const std::function<void(const RailUndo&)>& before_write);
};
}
