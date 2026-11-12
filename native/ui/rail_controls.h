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
};
struct RailField {
    bool established = false, stale = false;
    std::optional<std::int32_t> requested_uv, initial_uv;
    std::optional<double> estimated_base_mv;
};
struct RailRow {
    std::optional<std::int32_t> type;
    std::array<RailField, DC_RAILS_LIMIT_FIELDS> fields{};
};
struct RailUndo {
    int rail = -1;
    std::uint32_t field = 0;
    std::int32_t type = 0, requested_uv = 0;
};
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
    std::array<RailRow, DC_RAILS_COUNT> read();
    RailResult apply(int rail, std::uint32_t field, std::int32_t raw_uv, bool rail_mode, bool xoc,
                     const std::function<void(const RailUndo&)>& before_write = {});
    RailResult stock(int rail, std::uint32_t field, const std::function<void(const RailUndo&)>& before_write = {}); // captured initial, allowed with rail mode off
    RailResult restore(const RailUndo& undo, bool rail_mode, bool xoc);
private:
    RailApi api_;
    dc_gpu* gpu_;
    dc_gpu_identity target_{};
    std::array<RailRow, DC_RAILS_COUNT> last_{};
    std::array<std::optional<std::int32_t>, DC_RAILS_COUNT> initial_type_{};
    RailResult write(int rail, std::uint32_t field, std::int32_t value, bool mode, bool xoc, bool stock,
                     const std::optional<std::int32_t>& expected_type,
                     const std::function<void(const RailUndo&)>& before_write);
};
}
