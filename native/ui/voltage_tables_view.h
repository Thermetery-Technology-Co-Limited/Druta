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
struct VoltageTablesViewState {
    bool initialized=false;uint64_t generation=0;
    std::vector<VoltageTablesDraft> drafts;std::vector<VoltageTableRequest> staged,offsets_staged;
    std::string status;
};
std::optional<int32_t> parse_voltage_table_mv(const std::string& text);
void sync_voltage_tables_view(VoltageTablesViewState&,const VoltageTablesData*,uint64_t generation);
bool stage_voltage_tables_document(VoltageTablesViewState&,const ProfileJson&,const VoltageTablesData&);
std::vector<VoltageTablesIntent> draw_voltage_tables_controls(VoltageTablesViewState&,const VoltageTablesData*,uint64_t generation,bool busy,bool unlocked,bool classic=false);
}
