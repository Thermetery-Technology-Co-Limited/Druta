// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_limits_service.h"
namespace druta::ui {
struct VoltageLimitsIntent {
    enum class Operation {read,apply,restore_initial,restore_rom,undo,recover,save_profile,load_profile};
    Operation operation=Operation::read;uint32_t families=3;
    std::vector<VoltageLimitRequest> requests;std::vector<VoltageLimitKey> selection;
};
struct VoltageLimitsDraft {VoltageLimitKey key;int64_t value=0,lower=0;bool dirty=false,include=true;std::string binding;std::optional<int64_t> expected_value,expected_lower;
    // Classic edits in mV (switch delays in us); an entry that does not parse to a
    // whole microvolt (or microsecond) blocks Apply and keeps value / lower as they were.
    std::array<char,32> value_text{},lower_text{};bool value_invalid=false,lower_invalid=false;};
// Classic: the last action a part's controls sent (a Read or a request) and, from the first
// revision published after it, that action's result. A single control's request also keeps
// the control and the value it asked for, so its status reads as registry_panel.py's.
struct VoltageLimitsLastAction {bool read=false;uint64_t revision=0;std::optional<std::string> outcome;
    std::optional<VoltageLimitKey> key;std::optional<int64_t> value,lower;};
struct VoltageLimitsViewState {bool initialized=false;uint64_t generation=0;std::vector<VoltageLimitsDraft> drafts;std::vector<VoltageLimitRequest> staged;std::string status;
    std::array<std::optional<VoltageLimitKey>,4> choice; // classic selector per part
    // Classic per part: the read failure last seen, and whether a row was picked since
    // (registry_panel.py then prefixes the row's status with "Previous read is stale. ").
    std::array<std::string,4> failure_seen;std::array<bool,4> picked_after_failure{};
    std::array<std::optional<VoltageLimitsLastAction>,4> last; // classic, per part
};
// Classic draws REL bounds, the OV/VMIN/ALT constants and the device switch
// delays as separate subsections, as released 1.9.1 does; all is one panel.
enum class VoltageLimitsPart {all,rel,constants,switch_delay};
bool voltage_limits_part_has(VoltageLimitsPart,VoltageLimitKind);
// Decimal(uV) / 1000 without trailing zeros, as released 1.9.1 shows it.
std::string voltage_limits_mv_text(int64_t microvolts);
void sync_voltage_limits_view(VoltageLimitsViewState&,const VoltageLimitsData*,uint64_t generation);
bool stage_voltage_limits_document(VoltageLimitsViewState&,const ProfileJson&,const VoltageLimitsData&);
std::vector<VoltageLimitsIntent> draw_voltage_limits_controls(VoltageLimitsViewState&,const VoltageLimitsData*,uint64_t generation,bool busy,bool unlocked,bool classic=false,VoltageLimitsPart part=VoltageLimitsPart::all);
// A part draws its rows, reads, save and staged rows only. Undo, Recover and Load
// act on every part; classic draws them once with the shared controls.
std::vector<VoltageLimitsIntent> draw_voltage_limits_shared_controls(VoltageLimitsViewState&,const VoltageLimitsData*,uint64_t generation,bool busy,bool unlocked);
// Classic inventory box and status line of one part, in registry_panel.py wording.
// After a failed Read the status names it ("Live bound read unavailable: ..."); once a
// row is picked after a transient failure that kept the rows, the row's status follows
// "Previous read is stale. " until a good read.
std::string voltage_limits_inventory(const VoltageLimitsData*,VoltageLimitsPart);
std::string voltage_limits_status(const VoltageLimitsData*,VoltageLimitsPart,const VoltageLimitRow* selected,bool picked_after_failure=false);
// The part's status line once its last request's result is published: "Applied live. " and the
// control's status when the readback holds the requested value, else "Live ... change failed: "
// and the result; a staged profile's result as the service words it. Empty otherwise.
std::string voltage_limits_request_status(const VoltageLimitsViewState&,const VoltageLimitsData*,VoltageLimitsPart);
}
