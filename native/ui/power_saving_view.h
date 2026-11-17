// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_saving_service.h"
#include <array>
namespace druta::ui {
struct PowerSavingIntent {
    enum class Operation {read,apply,restore_initial,undo,recover,save_profile,load_profile,release_owned};
    Operation operation=Operation::read;uint32_t families=31;
    std::vector<PowerSavingRequest> requests;std::vector<PowerSavingKey> selection;
    bool lifecycle_release=false;
};
struct PowerSavingDraft {PowerSavingKey key;std::array<char,32> text{};bool dirty=false,include=false;std::string binding;std::optional<uint32_t> expected;};
// Classic status line (released 1.9.1 panels): the last request a row's own buttons
// sent and the data revision then, so the line can name what a later readback shows.
// prior: the action before it, which a failure still describes until this one's result
// is published. outcome: the service's account of this action, kept from the first
// published revision after it was sent (a failed request that left an uncertain row).
struct PowerSavingLastAction {enum class Kind {read,hold,release,set};PowerSavingKey key;Kind kind=Kind::read;uint32_t value=0;std::optional<bool> aggregate_before;uint64_t revision=0;
    std::optional<Kind> prior;std::optional<std::string> outcome;};
struct PowerSavingViewState {bool initialized=false;uint64_t generation=0;std::vector<PowerSavingDraft> drafts;std::vector<PowerSavingRequest> staged;std::string status;
    std::array<std::optional<uint32_t>,5> choice; // classic controller selector per family (idle threshold, adaptive)
    std::vector<PowerSavingLastAction> last;
};
// The classic status line under a family's controls, in the panel's wording:
// the prompt before a read, the panel's "<family> <operation> failed: <reason>. <action>"
// after a failure, otherwise what the latest readback confirms for the last request
// this view sent.
std::string power_saving_status(const PowerSavingViewState&,const PowerSavingData*,const PowerSavingRow*,PowerSavingFamily);
// The classic framed readback, in the panel's _format lines.
std::string power_saving_readback(const PowerSavingRow&);
std::optional<uint32_t> parse_power_saving_us(const std::string&);
void sync_power_saving_view(PowerSavingViewState&,const PowerSavingData*,uint64_t generation);
bool stage_power_saving_document(PowerSavingViewState&,const ProfileJson&,const PowerSavingData&);
// With a family, only that family's rows, read, save and staged requests are
// drawn, for classic's per-family subsections (released 1.9.1 Live power-saving
// panels). Release owned, Undo, Recover, Load and Discard act on every family;
// classic draws them once with draw_power_saving_shared_controls.
std::vector<PowerSavingIntent> draw_power_saving_controls(PowerSavingViewState&,const PowerSavingData*,uint64_t generation,bool busy,bool unlocked,bool classic=false,std::optional<PowerSavingFamily> family={});
std::vector<PowerSavingIntent> draw_power_saving_shared_controls(PowerSavingViewState&,const PowerSavingData*,uint64_t generation,bool busy,bool unlocked);
}
