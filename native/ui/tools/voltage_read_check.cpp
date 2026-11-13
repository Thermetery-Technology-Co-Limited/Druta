// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "core_client.h"
#include <cstdio>
#include <cwchar>
using namespace druta::ui;
namespace {
std::string ascii(const wchar_t* value) {std::string out;for(;*value;++value){if(*value<0||*value>127)return {};out.push_back(static_cast<char>(*value));}return out;}
}
int wmain(int argc,wchar_t** argv) {
    if(argc!=4){std::puts("Usage: voltage-read-check CORE_DLL PCI_SLOT EXPECTED_UUID");return 2;}
    const auto slot=ascii(argv[2]),uuid=ascii(argv[3]);
    auto backend=create_core_backend(argv[1]);std::string error;
    if(slot.empty()||uuid.empty()||!backend->open(slot,error)){std::printf("Open failed: %s\n",error.c_str());return 2;}
    const auto initial=backend->read(false);
    if(!initial.connected||initial.identity.slot!=slot||initial.identity.uuid!=uuid){std::puts("Expected adapter identity mismatch");return 2;}
    ProfileJson report={{"slot",slot},{"uuid",uuid},{"hardware_write_requested",false}};
    Command command;command.operation=Command::Operation::voltage_tables_read;command.voltage_families=1;
    const auto table_result=backend->execute(command);
    const auto tables=backend->read(false).voltage_tables;
    report["requests"]={{"ok",table_result.ok},{"may_have_changed",table_result.may_have_changed},{"message",table_result.message}};
    if(tables){report["requests"]["complete"]=tables->requests_complete;report["requests"]["rows"]=ProfileJson::array();
        for(const auto& row:tables->rows)report["requests"]["rows"].push_back({{"rail",row.key.rail},{"field",row.key.field},{"available",row.availability==VoltagePolicyAvailability::available},{"writable",row.writable},{"stale",row.stale},{"message",row.message}});}
    command.operation=Command::Operation::voltage_limits_read;command.voltage_families=3;
    const auto device_result=backend->execute(command);
    const auto limits=backend->read(false).voltage_limits;
    report["devices"]={{"ok",device_result.ok},{"may_have_changed",device_result.may_have_changed},{"message",device_result.message}};
    if(limits){report["devices"]["rows"]=ProfileJson::array();
        report["equations"]={{"count",limits->equation_count},{"definitions_stale",limits->definitions_stale},{"message",limits->equations_message},{"rows",ProfileJson::array()}};
        for(const auto& row:limits->rows){ProfileJson item={{"kind",static_cast<int>(row.key.kind)},{"rail",row.key.rail},{"index",row.key.index},{"available",row.availability==VoltagePolicyAvailability::available},{"availability",static_cast<int>(row.availability)},{"writable",row.writable},{"stale",row.stale},{"message",row.message}};
            if(row.value)item["value"]=*row.value;if(row.lower)item["lower"]=*row.lower;
            if(row.stored_value)item["stored_value"]=*row.stored_value;if(row.stored_lower)item["stored_lower"]=*row.stored_lower;
            if(row.reported)item["reported"]=*row.reported;
            report[row.key.kind==VoltageLimitKind::switch_delay?"devices":"equations"]["rows"].push_back(std::move(item));}}
    const auto closed=backend->prepare_close();backend->close();
    report["close_ok"]=closed.ok;std::puts(report.dump(2).c_str());
    return table_result.may_have_changed||device_result.may_have_changed||!closed.ok ? 1 : 0;
}
