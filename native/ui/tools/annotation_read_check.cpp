// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Read-only hardware qualification; annotation commits remain local memory.
#include "core_client.h"
#include "policy_annotation_profile.h"
#include "python_full_profile.h"
#include <algorithm>
#include <cstdio>
#include <cwchar>
using namespace druta::ui;
namespace {
std::string ascii(const wchar_t* value){std::string out;for(;*value;++value){if(*value<0||*value>127)return {};out.push_back(static_cast<char>(*value));}return out;}
}
int wmain(int argc,wchar_t** argv){
 if(argc!=4){std::puts("Usage: annotation-read-check CORE_DLL PCI_SLOT EXPECTED_UUID");return 2;}
 const auto slot=ascii(argv[2]),uuid=ascii(argv[3]);auto backend=create_core_backend(argv[1]);std::string error;
 if(slot.empty()||uuid.empty()||!backend->open(slot,error)){std::printf("Open failed: %s\n",error.c_str());return 2;}
 ProfileJson report={{"slot",slot},{"uuid",uuid},{"hardware_write_requested",false},{"ok",false}};bool ok=false;
 try{
  const auto initial=backend->read(false);if(!initial.connected||!initial.identity.nvapi_ok||!initial.identity.nvapi_selected||initial.identity.pairing_error[0]||initial.identity.slot!=slot||initial.identity.uuid!=uuid)throw std::runtime_error("Expected adapter identity mismatch");
  // prepare_scoped_profile returns immediately after the read-only capture;
  // it neither saves a file nor enters the selected writer path.
  Command prepare;prepare.operation=Command::Operation::prepare_scoped_profile;
  const auto prepared=backend->execute(prepare);report["prepare"]={{"ok",prepared.ok},{"may_have_changed",prepared.may_have_changed},{"effect",bool(prepared.annotation_effect)},{"message",prepared.message}};
  if(!prepared.ok||prepared.may_have_changed||prepared.annotation_effect)throw std::runtime_error("Read-only policy selection preparation failed");
  const auto data=backend->read(true);if(!data.connected||!data.identity.nvapi_ok||!data.identity.nvapi_selected||data.identity.pairing_error[0]||data.identity.slot!=slot||data.identity.uuid!=uuid)throw std::runtime_error("Fresh adapter identity mismatch");
  if(!data.scoped_profiles||data.scoped_profiles->target.slot!=slot||data.scoped_profiles->target.uuid!=uuid)throw std::runtime_error("No fresh prepared scoped policy snapshot available");
  const auto& snapshot=*data.scoped_profiles;const auto found=std::find_if(snapshot.entries.begin(),snapshot.entries.end(),[](const auto& row){return row.key.kind==ScopedKey::Kind::policy&&row.fresh&&row.channel&&row.policy_type&&!row.binding.empty();});
  if(found==snapshot.entries.end())throw std::runtime_error("No fresh policy row with channel and type");
  const ScopedCurrent selected=*found;report["selected_policy"]=selected.key.policy;
  const std::string wanted="Probe \xE6\xB5\x8B\xE8\xAF\x95";ProfileJson doc={{"schema",2},{"device",{{"uuid",uuid},{"slot",slot}}},{"incomplete",ProfileJson::array()},{"power_policy_names",{{std::to_string(selected.key.policy),{{"name",wanted},{"channel",*selected.channel},{"type",*selected.policy_type}}}}}};
  PolicyAnnotations labels(ProfileJson{{"opaque_sentinel",{{"keep",true}}}});const auto original=labels.document();PolicyAnnotationCommits commits;
  Command c;c.operation=Command::Operation::preflight_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(select_python_full_profile(doc,{"annotation:power"}));c.annotation_snapshot=std::make_shared<const PolicyAnnotations>(labels);
  const auto preflight=backend->execute(c);report["preflight"]={{"ok",preflight.ok},{"may_have_changed",preflight.may_have_changed},{"effect",bool(preflight.annotation_effect)},{"message",preflight.message}};
  if(!preflight.ok||preflight.may_have_changed||preflight.annotation_effect)throw std::runtime_error("Annotation preflight did not remain read-only");
  c.operation=Command::Operation::apply_catalog_profile;const auto applied=backend->execute(c);report["apply"]={{"ok",applied.ok},{"may_have_changed",applied.may_have_changed},{"message",applied.message}};
  if(!applied.ok||applied.may_have_changed||!applied.annotation_effect)throw std::runtime_error("Annotation Apply did not return a read-only effect");
  report["matched"]=applied.annotation_effect->rows.size();report["skipped"]=applied.annotation_effect->skipped;
  const auto effect_row=std::find_if(applied.annotation_effect->rows.begin(),applied.annotation_effect->rows.end(),[&](const auto& r){return r.key==selected.key;});if(effect_row==applied.annotation_effect->rows.end())throw std::runtime_error("Selected policy identity did not survive fresh qualification");
  const auto applied_commit=commit_policy_annotation_effect(*applied.annotation_effect,labels,commits);report["commit_apply"]={{"ok",applied_commit.ok},{"changed",applied_commit.changed},{"message",applied_commit.message}};
  if(!applied_commit.ok||!applied_commit.changed||labels.get(applied.annotation_effect->plan->target,*effect_row)!=wanted)throw std::runtime_error("In-memory Apply commit failed");
  Command undo;undo.operation=Command::Operation::undo;const auto undone=backend->execute(undo);report["undo"]={{"ok",undone.ok},{"may_have_changed",undone.may_have_changed},{"message",undone.message}};
  if(!undone.ok||undone.may_have_changed||!undone.annotation_effect)throw std::runtime_error("Annotation Undo did not return a read-only effect");
  const auto undo_commit=commit_policy_annotation_effect(*undone.annotation_effect,labels,commits);report["commit_undo"]={{"ok",undo_commit.ok},{"changed",undo_commit.changed},{"message",undo_commit.message}};
  if(!undo_commit.ok||labels.get(applied.annotation_effect->plan->target,*effect_row)!=""||labels.document()["opaque_sentinel"]!=original["opaque_sentinel"])throw std::runtime_error("In-memory Undo or opaque sentinel check failed");
  ok=true;
 }catch(const std::exception& e){report["error"]=e.what();}
 const auto closed=backend->prepare_close();report["close"]={{"ok",closed.ok},{"may_have_changed",closed.may_have_changed},{"message",closed.message}};backend->close();report["ok"]=ok&&closed.ok&&!closed.may_have_changed;std::puts(report.dump(2).c_str());return report["ok"].get<bool>()?0:1;
}
