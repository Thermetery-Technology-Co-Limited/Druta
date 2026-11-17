// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "clock_gating_service.h"
namespace druta::ui {
struct ClockGatingIntent {
    enum class Operation {read,apply,release,restore_initial,recover,undo,save_profile,load_profile};
    Operation operation=Operation::read;uint32_t families=15;
    ClockGatingFamily family=ClockGatingFamily::engine;
    std::vector<ClockGatingRequest> requests;std::vector<ClockGatingFamily> selection;
};
struct ClockGatingViewState {bool initialized=false;uint64_t generation=0;uint32_t pcie_generation=1;bool pcie_dirty=false;std::optional<uint32_t> pcie_baseline;std::string pcie_binding;std::array<bool,4> included{};std::vector<ClockGatingRequest> staged;std::string status;
    // Classic "Request previous speed" (pcie_speed_panel.py): the target that a verified
    // Request speed replaced. pcie_pending holds {data revision, from, to} until the
    // next published readback confirms or refutes that request.
    std::optional<uint32_t> pcie_previous;struct PciePending {uint64_t revision=0;uint32_t from=0,to=0;};std::optional<PciePending> pcie_pending;
    // Classic status line (released 1.9.1 panels): the last request each family's own
    // buttons sent and the data revision then. matched: the readback already showed the
    // requested PCIe generation on the target and both links when it was sent. prior: the
    // action before it, which a failure still describes until this one's result is
    // published. outcome: the service's account of this action, kept from the first
    // published revision after it was sent.
    struct LastAction {enum class Kind {read,hold_disabled,hold_enabled,release,request};Kind kind=Kind::read;uint64_t revision=0;uint32_t generation=0;bool matched=false;
        std::optional<Kind> prior;std::optional<std::string> outcome;};
    std::array<std::optional<LastAction>,4> last;};
void sync_clock_gating_view(ClockGatingViewState&,const ClockGatingData*,uint64_t generation);
// The classic framed readback, in the panels' _format lines; native detail follows them.
std::string clock_gating_readback(const ClockGatingState&,ClockGatingFamily);
// The box as drawn: PCIe adds pcie_speed_panel.py's "Historical readback (refresh
// required):" heading after a failed read or request, and "Historical or unverified
// readback (refresh required):" after a request the readback did not verify.
std::string clock_gating_readback_box(const ClockGatingViewState&,const ClockGatingData*,ClockGatingFamily);
// The classic status line, in the panels' wording (see ClockGatingViewState::last),
// including _failed's "<family> <operation> failed: <reason>. <action>".
std::string clock_gating_status(const ClockGatingViewState&,const ClockGatingData*,ClockGatingFamily);
bool stage_clock_gating_document(ClockGatingViewState&,const ProfileJson&,const ClockGatingData&);
// With a family, only that family, its save and its staged request are drawn, for
// classic's per-family subsections (released 1.9.1 Live power-saving & PCIe).
// Undo and Load act on every family; classic draws them once with the shared controls.
std::vector<ClockGatingIntent> draw_clock_gating_controls(ClockGatingViewState&,const ClockGatingData*,uint64_t generation,bool busy,bool unlocked,bool classic=false,std::optional<ClockGatingFamily> family={});
std::vector<ClockGatingIntent> draw_clock_gating_shared_controls(ClockGatingViewState&,const ClockGatingData*,uint64_t generation,bool busy,bool unlocked);
}
