// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_tables_service.h"
namespace druta::ui {
struct VoltageTablesIntent {
    enum class Operation {read,apply,restore_initial,undo,recover,save_profile,load_profile};
    Operation operation=Operation::read;uint32_t families=1;
    std::vector<VoltageTableRequest> requests;std::vector<VoltageTableKey> selection;
};
struct VoltageTablesDraft {
    VoltageTableKey key;std::array<char,64> text{};
    bool dirty=false,include=false;
    std::string binding;std::optional<int32_t> expected;
};
// Classic: the last action a family's controls sent (a Read or a request) and, from the
// first revision published after it, that action's result. A single field's Apply also keeps
// the field, the value it asked for and whether the field already held it.
struct VoltageTablesLastAction {bool read=false;uint64_t revision=0;std::optional<std::string> outcome;
    std::optional<VoltageTableKey> key;int64_t value=0;bool unchanged=false;};
struct VoltageTablesViewState {
    bool initialized=false;uint64_t generation=0;
    std::vector<VoltageTablesDraft> drafts;std::vector<VoltageTableRequest> staged,offsets_staged;
    std::string status;
    // Classic selectors: the offset and the rail whose request fields are shown.
    std::optional<VoltageTableKey> offsets_choice;std::optional<uint32_t> requests_rail;
    // Indexed by family: 0 requests, 1 offsets.
    std::array<std::optional<VoltageTablesLastAction>,2> last;
};
// Classic draws the installed policy offsets and the legacy rail requests as
// separate subsections (released 1.9.1 order: offsets, then requests).
enum class VoltageTablesPart {all,requests,offsets};
std::optional<int32_t> parse_voltage_table_mv(const std::string& text);
void sync_voltage_tables_view(VoltageTablesViewState&,const VoltageTablesData*,uint64_t generation);
bool stage_voltage_tables_document(VoltageTablesViewState&,const ProfileJson&,const VoltageTablesData&);
std::vector<VoltageTablesIntent> draw_voltage_tables_controls(VoltageTablesViewState&,const VoltageTablesData*,uint64_t generation,bool busy,bool unlocked,bool classic=false,VoltageTablesPart part=VoltageTablesPart::all);
// Classic "Undo, recovery and profiles": the native actions the released panels do not
// have (Restore initial, Undo, Recover, loading requests and the staged requests), and a
// chosen subset of offsets or of requests to save (the Focused table's Include).
std::vector<VoltageTablesIntent> draw_voltage_tables_shared_controls(VoltageTablesViewState&,const VoltageTablesData*,uint64_t generation,bool busy,bool unlocked);
// Classic inventory boxes, in voltage_offsets_panel.py / voltage_requests_panel.py wording.
// Empty after a failed Read that kept nothing, as Python clears its box.
std::string voltage_offsets_inventory(const VoltageTablesData*);
std::string voltage_requests_inventory(const VoltageTablesData*);
// The Classic status lines: the released read-state line, or the published result of the
// family's last request (apply, restore, Undo, Recover) in its place.
std::string voltage_requests_status(const VoltageTablesViewState&,const VoltageTablesData*);
std::string voltage_offsets_status(const VoltageTablesViewState&,const VoltageTablesData*);
}
