// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core_client.h"
#include "device_restart.h"
#include <cstdio>
using namespace druta::ui;
namespace {
std::string ascii(const wchar_t* s){std::string out;for(;*s;++s){if(*s<0||*s>127)return {};out.push_back(static_cast<char>(*s));}return out;}
}
int wmain(int argc,wchar_t** argv){
    if(argc!=4)return 2;
    const auto slot=ascii(argv[2]),uuid=ascii(argv[3]);auto backend=create_core_backend(argv[1]);std::string error;
    if(slot.empty()||uuid.empty()||!backend->open(slot,error)){std::printf("Open failed: %s\n",error.c_str());return 2;}
    const auto initial=backend->read(false);
    if(!initial.connected||initial.identity.slot!=slot||initial.identity.uuid!=uuid)return 2;
    ProfileJson report={{"slot",slot},{"uuid",uuid},{"hardware_write_requested",false}};
    try{const auto target=resolve_restart_target(slot,enumerate_restart_targets());std::string why;
        report["pnp_target"]={{"matched_slot",target.slot},{"unique",true},{"started",restart_target_started(target,why)},{"message",why}};
    }catch(const std::exception& error){report["pnp_target"]={{"unique",false},{"message",error.what()}};}
    Command command;command.operation=Command::Operation::power_saving_read;
    const auto saving=backend->execute(command);const auto first=backend->read(false);
    report["power_saving"]={{"ok",saving.ok},{"may_have_changed",saving.may_have_changed},{"message",saving.message},{"rows",ProfileJson::array()}};
    if(first.power_saving)for(const auto& row:first.power_saving->rows)report["power_saving"]["rows"].push_back({{"family",static_cast<unsigned>(row.key.family)},{"index",row.key.index},{"label",row.label},{"available",row.availability==VoltagePolicyAvailability::available},{"stale",row.stale},{"writable",row.writable},{"owned",row.owned},{"uncertain",row.uncertain},{"message",row.message}});
    command.operation=Command::Operation::clock_gating_read;
    const auto clocks=backend->execute(command);const auto second=backend->read(false);
    report["clock_gating"]={{"ok",clocks.ok},{"may_have_changed",clocks.may_have_changed},{"message",clocks.message},{"rows",ProfileJson::array()}};
    if(second.clock_gating)for(const auto& row:second.clock_gating->families)report["clock_gating"]["rows"].push_back({{"label",row.label},{"available",row.availability==VoltagePolicyAvailability::available},{"stale",row.stale},{"support",row.support},{"can_disable",row.can_disable},{"can_enable",row.can_enable},{"owned",row.owned},{"uncertain",row.uncertain},{"message",row.message},{"layout",row.layout}});
    // Observation must never acquire a lifecycle request. If it does, do not
    // turn this check into a write-based cleanup experiment.
    if(backend->auxiliary_cleanup_needed()){report["unexpected_ownership"]=true;std::puts(report.dump(2).c_str());backend.release();return 1;}
    const auto closed=backend->prepare_close();backend->close();report["close_ok"]=closed.ok;
    std::puts(report.dump(2).c_str());return saving.may_have_changed||clocks.may_have_changed||!closed.ok?1:0;
}
