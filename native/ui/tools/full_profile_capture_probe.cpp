// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Explicit full-profile capture probe; no tuning command is issued.
#include "core_client.h"
#include "profile_catalog.h"
#include <algorithm>
#include <cstdio>
#include <cwchar>
#include <filesystem>
using namespace druta::ui;
namespace {
std::string ascii(const wchar_t* text){std::string out;for(;*text;++text){if(*text<32||*text>126)return {};out.push_back(static_cast<char>(*text));}return out;}
bool same(const LiveData& data,const std::string& slot,const std::string& uuid){return data.connected&&data.identity.nvapi_ok&&data.identity.nvapi_selected&&!data.identity.pairing_error[0]&&data.identity.slot==slot&&data.identity.uuid==uuid;}
}
int wmain(int argc,wchar_t** argv){
 if(argc!=9){std::puts("Usage: full-profile-capture-probe --core ABSOLUTE_DLL --gpu PCI_SLOT --expect-uuid UUID --output ABSOLUTE_FILE");return 2;}
 std::wstring core,output,slot_arg,uuid_arg;for(int i=1;i<argc;i+=2){const std::wstring key=argv[i];auto* dest=key==L"--core"?&core:key==L"--gpu"?&slot_arg:key==L"--expect-uuid"?&uuid_arg:key==L"--output"?&output:nullptr;if(!dest||!dest->empty()){std::puts("Invalid or duplicate argument");return 2;}*dest=argv[i+1];}
 const auto slot=ascii(slot_arg.c_str()),uuid=ascii(uuid_arg.c_str());if(core.empty()||output.empty()||slot.empty()||uuid.empty()||!std::filesystem::path(core).is_absolute()||!std::filesystem::path(output).is_absolute()){std::puts("Absolute paths and exact adapter identity are required");return 2;}
 ProfileJson report={{"slot",slot},{"uuid",uuid},{"hardware_write_requested",false},{"ok",false}};auto backend=create_core_backend(core);bool opened=false,changed=false,ok=false;
 try{const auto devices=backend->enumerate();const auto exact=std::count_if(devices.begin(),devices.end(),[&](const Device& d){return d.slot==slot&&d.uuid==uuid;});const auto same_slot=std::count_if(devices.begin(),devices.end(),[&](const Device& d){return d.slot==slot;});report["enumerated_matches"]=exact;if(exact!=1||same_slot!=1)throw std::runtime_error("Selected slot and UUID do not identify one enumerated adapter");std::string error;if(!backend->open(slot,error))throw std::runtime_error("Adapter open failed: "+error);opened=true;
  const auto before=backend->read(false);if(!same(before,slot,uuid))throw std::runtime_error("Fresh paired adapter identity mismatch before capture");
  Command command;command.operation=Command::Operation::save_full_profile;command.profile_path=output;const auto captured=backend->execute(command);changed|=captured.may_have_changed;report["capture"]={{"ok",captured.ok},{"may_have_changed",captured.may_have_changed},{"message",captured.message}};if(!captured.ok||captured.may_have_changed)throw std::runtime_error("Full profile capture did not complete read-only");
  const auto after=backend->read(false);if(!same(after,slot,uuid))throw std::runtime_error("Fresh paired adapter identity mismatch after capture");const auto document=read_profile(output);const auto description=inspect_profile(document);report["inspection"]={{"valid",description.valid},{"scope",profile_scope_name(description.scope)},{"summary",description.summary},{"error",description.error},{"targets",description.targets}};if(description.slot)report["inspection"]["slot"]=*description.slot;if(description.uuid)report["inspection"]["uuid"]=*description.uuid;report["incomplete"]=document.value("incomplete",ProfileJson::array());report["profile"]=document;ok=true;
 }catch(const std::exception& e){report["error"]=e.what();}
 if(opened){const auto closed=backend->prepare_close();changed|=closed.may_have_changed;report["close"]={{"ok",closed.ok},{"may_have_changed",closed.may_have_changed},{"message",closed.message}};backend->close();ok=ok&&closed.ok;}report["ok"]=ok&&!changed;std::puts(report.dump(2).c_str());return report["ok"].get<bool>()?0:1;
}
