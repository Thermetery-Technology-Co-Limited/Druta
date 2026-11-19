// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_annotation_profile.h"
#include "python_full_profile.h"
#include <algorithm>
#include <atomic>
#include <set>
#include <stdexcept>
namespace druta::ui {
namespace {
void require(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
bool identity(const ScopedCurrent& a,const ScopedCurrent& b){return a.key==b.key&&a.channel&&a.policy_type&&a.channel==b.channel&&a.policy_type==b.policy_type;}
void qualify(const ScopedSnapshot& current){require(!current.target.uuid.empty()&&!current.target.slot.empty()&&current.target.uuid.find('\0')==std::string::npos&&current.target.slot.find('\0')==std::string::npos,"Exact annotation card identity unavailable");std::set<uint32_t> seen;
 for(const auto& r:current.entries){require(r.key.kind==ScopedKey::Kind::policy&&r.key.policy<32&&r.fresh&&r.channel&&r.policy_type&&seen.insert(r.key.policy).second,"Fresh unique complete policy identities required for annotations");}}
const ScopedCurrent* find(const std::vector<ScopedCurrent>& rows,const ScopedCurrent& wanted){for(const auto& row:rows)if(identity(row,wanted))return &row;return nullptr;}
void plan_valid(const PolicyAnnotationPlan& plan){require(plan.transaction&&!plan.target.uuid.empty()&&!plan.target.slot.empty(),"Invalid annotation transaction");std::set<uint32_t> seen;
 for(const auto& row:plan.rows){require(row.identity.key.kind==ScopedKey::Kind::policy&&row.identity.key.policy<32&&row.identity.channel&&row.identity.policy_type&&seen.insert(row.identity.key.policy).second,"Invalid selected annotation identity");validate_policy_label(row.before);validate_policy_label(row.desired);}}
std::atomic<uint64_t> next_transaction{1};
}
std::shared_ptr<const PolicyAnnotationPlan> prepare_policy_annotation_profile(const ProfileJson& selected,const ScopedSnapshot& current,const PolicyAnnotations& snapshot){
 const auto description=describe_python_full_profile(selected);require(description.valid&&description.targets==std::vector<std::string>{"annotation:power"},"Select only the full profile power annotations for this component");qualify(current);
 require(!snapshot.document().contains("annotations")||snapshot.document().at("annotations").is_array(),"Unknown annotation schema; profile preflight refused without effects");
 require(description.slot&&*description.slot==current.target.slot&&(!description.uuid||*description.uuid==current.target.uuid),"Saved annotation card UUID/slot differs or slot is absent");
 const auto& source=python_full_source(selected);const auto& names=source.at("power_policy_names");auto plan=std::make_shared<PolicyAnnotationPlan>();plan->transaction=next_transaction.fetch_add(1);plan->target=current.target;
 for(auto entry=names.begin();entry!=names.end();++entry){const auto policy=static_cast<uint32_t>(std::stoul(entry.key()));const auto& v=entry.value();const auto desired=normalize_policy_label(v.at("name").get<std::string>());const ScopedCurrent* matched=nullptr;
  for(const auto& row:current.entries)if(row.key.policy==policy&&*row.channel==v.at("channel").get<uint32_t>()&&*row.policy_type==v.at("type").get<uint32_t>())matched=&row;
  if(!matched||desired.empty()){++plan->skipped;continue;}PolicyAnnotationRow row;row.identity=*matched;row.before=snapshot.get(current.target,*matched);validate_policy_label(row.before);row.before_revision=snapshot.revision(current.target,*matched);row.desired=desired;plan->rows.push_back(std::move(row));}
 plan_valid(*plan);auto writable=snapshot;for(const auto& row:plan->rows)writable.set(plan->target,row.identity,row.desired);return plan;
}
std::shared_ptr<const PolicyAnnotationEffect> qualify_policy_annotation_effect(std::shared_ptr<const PolicyAnnotationPlan> plan,PolicyAnnotationPhase phase,const ScopedSnapshot& current){
 require(bool(plan),"Missing annotation plan");plan_valid(*plan);qualify(current);require(plan->target.uuid==current.target.uuid&&plan->target.slot==current.target.slot,"Annotation effect belongs to another card");
 auto effect=std::make_shared<PolicyAnnotationEffect>();effect->plan=std::move(plan);effect->phase=phase;effect->skipped=effect->plan->skipped;
 for(const auto& row:effect->plan->rows){if(const auto* matched=find(current.entries,row.identity))effect->rows.push_back(*matched);else ++effect->skipped;}return effect;
}
PolicyAnnotationCommitResult commit_policy_annotation_effect(const PolicyAnnotationEffect& effect,PolicyAnnotations& current,PolicyAnnotationCommits& commits){
 PolicyAnnotationCommitResult result;result.skipped=effect.skipped;
 for(auto it=commits.transactions.begin();it!=commits.transactions.end();)if(it->second.plan.expired())it=commits.transactions.erase(it);else ++it;
 try{require(bool(effect.plan),"Missing annotation effect plan");require(effect.phase==PolicyAnnotationPhase::apply||effect.phase==PolicyAnnotationPhase::undo,"Invalid annotation effect phase");const auto& plan=*effect.plan;plan_valid(plan);ScopedSnapshot qualified{plan.target,effect.rows};qualify(qualified);
  for(const auto& row:effect.rows)require(std::any_of(plan.rows.begin(),plan.rows.end(),[&](const auto& selected){return identity(row,selected.identity);}),"Effect includes an unselected annotation");
  const auto found=commits.transactions.find(plan.transaction);
  if(found!=commits.transactions.end()){require(found->second.plan.lock()==effect.plan,"Annotation transaction token collision");if(effect.phase==PolicyAnnotationPhase::apply){result.ok=found->second.accepted;result.complete=true;result.message=found->second.message;return result;}if(found->second.undone){result.ok=result.complete=true;result.message="Annotation Undo already committed";return result;}}
  if(effect.phase==PolicyAnnotationPhase::undo){require(found!=commits.transactions.end()&&found->second.accepted,"No successful annotation Apply commit exists for Undo");
   auto updated=current;auto receipts=commits;result.skipped=0;
   for(const auto& committed:found->second.rows){const auto* row=find(effect.rows,committed.row.identity);
    if(!row||current.get(plan.target,*row)!=committed.row.desired||current.revision(plan.target,*row)!=committed.applied_revision){++result.skipped;continue;}
    result.changed=result.changed||current.get(plan.target,*row)!=committed.row.before;updated.set(plan.target,*row,committed.row.before);++result.applied;
    // A typed Undo can restore the value owned by an earlier transaction.
    // Rebase only its exact prior revision witness, never a coincident label
    // following a standalone edit or a different card/channel/type.
    const auto restored_revision=updated.revision(plan.target,*row);
    for(auto& [token,prior]:receipts.transactions){if(token==plan.transaction||!prior.accepted||prior.undone)continue;const auto previous=prior.plan.lock();
     if(!previous||previous->target.uuid!=plan.target.uuid||previous->target.slot!=plan.target.slot)continue;
     for(auto& earlier:prior.rows)if(identity(earlier.row.identity,committed.row.identity)&&earlier.row.desired==committed.row.before&&earlier.applied_revision==committed.row.before_revision)earlier.applied_revision=restored_revision;}
   }
   receipts.transactions.at(plan.transaction).undone=true;result.changed=updated.document()!=current.document();current=std::move(updated);commits=std::move(receipts);result.ok=result.complete=true;result.message="Annotation Undo: "+std::to_string(result.applied)+" restored, "+std::to_string(result.skipped)+" later-edited or unmatched labels preserved";return result;
  }
  // Conflict checks precede every copied-store edit, including selected no-ops.
  for(const auto& selected:plan.rows)if(const auto* row=find(effect.rows,selected.identity))if(current.get(plan.target,*row)!=selected.before||current.revision(plan.target,*row)!=selected.before_revision){
   auto receipts=commits;PolicyAnnotationCommitReceipt rejected;rejected.plan=effect.plan;rejected.message="Selected annotation edited after submission; profile labels not committed";receipts.transactions[plan.transaction]=std::move(rejected);commits=std::move(receipts);result.complete=true;result.message=commits.transactions.at(plan.transaction).message;return result;}
  auto updated=current;auto receipts=commits;PolicyAnnotationCommitReceipt receipt;receipt.plan=effect.plan;receipt.accepted=true;
  for(const auto& selected:plan.rows)if(const auto* row=find(effect.rows,selected.identity)){result.changed=result.changed||current.get(plan.target,*row)!=selected.desired;updated.set(plan.target,*row,selected.desired);receipt.rows.push_back({selected,updated.revision(plan.target,*row)});++result.applied;}
  receipt.message="Selected annotations committed; "+std::to_string(result.applied)+" matched, "+std::to_string(result.skipped)+" skipped";result.message=receipt.message;receipts.transactions[plan.transaction]=std::move(receipt);result.changed=updated.document()!=current.document();current=std::move(updated);commits=std::move(receipts);result.ok=result.complete=true;return result;
 }catch(const std::exception& error){result.ok=false;result.changed=false;result.complete=false;result.applied=0;result.message=error.what();return result;}
}
}
