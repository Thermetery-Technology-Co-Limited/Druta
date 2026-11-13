// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_annotation_profile.h"
#include "python_full_profile.h"
#include <cstdio>
#include <cstdlib>
#include <functional>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
template<class F>static void rejects(F fn){bool caught=false;try{fn();}catch(const std::exception&){caught=true;}CHECK(caught);}
static ScopedSnapshot snapshot(){ScopedSnapshot s;s.target={"GPU-label-other","0001:05:00.0"};for(uint32_t id:{3u,8u}){ScopedCurrent r;r.key=ScopedKey::policy_request(id);r.binding="initial tuning geometry";r.channel=id+1;r.policy_type=2;r.fresh=true;r.writable=false;r.unit="unknown";s.entries.push_back(r);}return s;}
static ProfileJson source(){return {{"schema",2},{"device",{{"uuid","GPU-label-other"},{"slot","0001:05:00.0"}}},{"power_policy_names",{{"3",{{"name","PCIE 8pin"},{"channel",4},{"type",2}}},{"8",{{"name","memory supply"},{"channel",9},{"type",2}}}}}};}
static ProfileJson selected(const ProfileJson& j){return select_python_full_profile(j,{"annotation:power"});}
static std::string repeat(const std::string& text,unsigned count){std::string out;for(unsigned i=0;i<count;++i)out+=text;return out;}
int main(){
 const auto s=snapshot();const auto a=s.entries[0],b=s.entries[1];
 {PolicyAnnotations notes({{"future",{{"opaque",42}}}});notes.set(s.target,a,"before A");notes.set(s.target,b,"before B");const auto original=notes.document();const auto rev=notes.revision(s.target,a);auto copy=notes;CHECK(copy.revision(s.target,a)==rev);
  auto plan=prepare_policy_annotation_profile(selected(source()),s,notes);CHECK(plan->rows.size()==2&&plan->skipped==0&&notes.document()==original&&notes.revision(s.target,a)==rev);
  auto fresh=s;fresh.entries[0].binding="changed limits/unit geometry";fresh.entries[0].unit="mA";PolicyAnnotationCommits commits;
  auto effect=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,fresh);auto result=commit_policy_annotation_effect(*effect,notes,commits);
  CHECK(result.ok&&result.changed&&result.complete&&result.applied==2&&notes.get(s.target,a)=="PCIE 8pin"&&notes.get(s.target,b)=="memory supply"&&notes.document()["future"]["opaque"]==42);
  const auto applied_rev=notes.revision(s.target,a);CHECK(commit_policy_annotation_effect(*effect,notes,commits).ok&&notes.revision(s.target,a)==applied_rev);
  auto undo=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::undo,fresh);CHECK(commit_policy_annotation_effect(*undo,notes,commits).ok&&notes.get(s.target,a)=="before A"&&notes.get(s.target,b)=="before B");
  const auto restored_rev=notes.revision(s.target,a);CHECK(commit_policy_annotation_effect(*undo,notes,commits).ok&&notes.revision(s.target,a)==restored_rev);
 }
 {PolicyAnnotations notes;notes.set(s.target,a,"same");auto one=source();one["power_policy_names"].erase("8");auto plan=prepare_policy_annotation_profile(selected(one),s,notes);notes.set(s.target,b,"unrelated later edit");PolicyAnnotationCommits commits;
  auto apply=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,s);CHECK(commit_policy_annotation_effect(*apply,notes,commits).ok&&notes.get(s.target,b)=="unrelated later edit");
  notes.set(s.target,a,"PCIE 8pin");const auto before=notes.document();auto undo=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::undo,s);auto failed=commit_policy_annotation_effect(*undo,notes,commits);CHECK(failed.ok&&failed.complete&&!failed.changed&&failed.skipped==1&&notes.document()==before); // Same-value edit is still a later edit.
 }
 {PolicyAnnotations notes;notes.set(s.target,a,"same");auto plan=prepare_policy_annotation_profile(selected(source()),s,notes);notes.set(s.target,a,"same");PolicyAnnotationCommits commits;
  auto apply=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,s);const auto before=notes.document();CHECK(!commit_policy_annotation_effect(*apply,notes,commits).ok&&notes.document()==before&&notes.get(s.target,b).empty());
  auto undo=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::undo,s);CHECK(!commit_policy_annotation_effect(*undo,notes,commits).ok&&notes.document()==before);
 }
 {PolicyAnnotations notes;notes.set(s.target,a,"before A");notes.set(s.target,b,"before B");auto plan=prepare_policy_annotation_profile(selected(source()),s,notes);PolicyAnnotationCommits commits;
  auto apply=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,s);CHECK(commit_policy_annotation_effect(*apply,notes,commits).ok);notes.set(s.target,a,"later edit");
  auto undo=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::undo,s);const auto restored=commit_policy_annotation_effect(*undo,notes,commits);
  CHECK(restored.ok&&restored.complete&&restored.changed&&restored.applied==1&&restored.skipped==1&&notes.get(s.target,a)=="later edit"&&notes.get(s.target,b)=="before B");
  notes.set(s.target,a,"PCIE 8pin");CHECK(commit_policy_annotation_effect(*undo,notes,commits).ok&&notes.get(s.target,a)=="PCIE 8pin"); // Completed disposition never authorizes a later retry.
 }
 {PolicyAnnotations notes;PolicyAnnotationCommits commits;auto plan=prepare_policy_annotation_profile(selected(source()),s,notes);auto undo=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::undo,s);CHECK(!commit_policy_annotation_effect(*undo,notes,commits).ok&&commits.transactions.empty());
  auto partial=s;partial.entries.erase(partial.entries.begin()+1);auto effect=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,partial);CHECK(effect->skipped==1&&effect->rows.size()==1);CHECK(commit_policy_annotation_effect(*effect,notes,commits).ok&&notes.get(s.target,a)=="PCIE 8pin"&&notes.get(s.target,b).empty());
  auto changed=s;changed.entries[0].channel=77;auto unavailable=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::undo,changed);auto preserved=commit_policy_annotation_effect(*unavailable,notes,commits);CHECK(preserved.ok&&preserved.complete&&preserved.applied==0&&preserved.skipped==1&&notes.get(s.target,a)=="PCIE 8pin");CHECK(commit_policy_annotation_effect(*undo,notes,commits).ok&&notes.get(s.target,a)=="PCIE 8pin");
 }
 {PolicyAnnotations notes({{"annotations","unknown future schema"},{"opaque",42}});const auto before=notes.document();rejects([&]{prepare_policy_annotation_profile(selected(source()),s,notes);});auto plan=prepare_policy_annotation_profile(selected(source()),s,PolicyAnnotations{});auto effect=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,s);PolicyAnnotationCommits commits;
  const auto failed=commit_policy_annotation_effect(*effect,notes,commits);CHECK(!failed.ok&&!failed.changed&&failed.applied==0&&notes.document()==before&&commits.transactions.empty());
 }
 {PolicyAnnotations notes;auto malformed=source();malformed["power_policy_names"]["3"]["name"]=std::string(81,'x');malformed["mem_off_units"]=0;rejects([&]{select_python_full_profile(malformed,{"clock:memory"});});
  for(const auto& bad:std::vector<std::string>{std::string("x\0y",3),std::string("\xc0\xaf",2),std::string("\xed\xa0\x80",3),std::string("\xf4\x90\x80\x80",4),std::string("\xe2\x82",2)}){rejects([&]{validate_policy_label(bad);});malformed=source();malformed["power_policy_names"]["3"]["name"]=bad;rejects([&]{describe_python_full_profile(malformed);});}
  auto stale=s;stale.entries[0].fresh=false;rejects([&]{prepare_policy_annotation_profile(selected(source()),stale,notes);});auto duplicate=s;duplicate.entries.push_back(a);rejects([&]{prepare_policy_annotation_profile(selected(source()),duplicate,notes);});
  auto foreign=s;foreign.target.uuid="GPU-another";rejects([&]{prepare_policy_annotation_profile(selected(source()),foreign,notes);});auto wrong=source();wrong["device"].erase("uuid");CHECK(prepare_policy_annotation_profile(selected(wrong),s,notes)->target.uuid==s.target.uuid);wrong["device"]["uuid"]=nullptr;CHECK(prepare_policy_annotation_profile(selected(wrong),s,notes)->target.uuid==s.target.uuid);wrong["device"]["uuid"]="";CHECK(prepare_policy_annotation_profile(selected(wrong),s,notes)->target.uuid==s.target.uuid);wrong["device"]["uuid"]="GPU-wrong";rejects([&]{prepare_policy_annotation_profile(selected(wrong),s,notes);});wrong=source();wrong["device"]["slot"]="0001:06:00.0";rejects([&]{prepare_policy_annotation_profile(selected(wrong),s,notes);});wrong["device"].erase("slot");rejects([&]{prepare_policy_annotation_profile(selected(wrong),s,notes);});
 }
 {const auto wide=repeat("\xf0\x9f\x94\x8c",80);CHECK(wide.size()==320&&policy_label_codepoints(wide)==80);validate_policy_label(wide);rejects([&]{validate_policy_label(wide+"x");});CHECK(normalize_policy_label("\xe3\x80\x80"+wide+"\xc2\xa0")==wide);CHECK(normalize_policy_label(wide+"x",true)==wide);
  PolicyAnnotations notes;auto doc=source();doc["power_policy_names"]["3"]["name"]=wide;auto plan=prepare_policy_annotation_profile(selected(doc),s,notes);PolicyAnnotationCommits commits;auto effect=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,s);CHECK(commit_policy_annotation_effect(*effect,notes,commits).ok&&notes.get(s.target,a)==wide);
  auto store=ProfileJson{{s.target.uuid,{{"3",{{"name","\xc2\xa0"+wide+"x\xe3\x80\x80"},{"channel",4},{"type",2}}}}}};CHECK(import_python_policy_names(store,s,notes)==1&&notes.get(s.target,a)==wide);
 }
 {PolicyAnnotations notes;notes.set(s.target,a,"old");const auto revision=notes.revision(s.target,a);notes.set_channel(s,a.key,"new");CHECK(notes.revision(s.target,a)>revision);const auto rev=notes.revision(s.target,a);auto copied=notes;copied.set(s.target,b,"other");CHECK(copied.revision(s.target,a)==rev);
  auto changed=a;changed.binding="new bounds";CHECK(notes.get(s.target,changed)=="new");changed.policy_type=77;CHECK(notes.get(s.target,changed).empty());
  auto legacy=PolicyAnnotations({{"annotations",ProfileJson::array({{{"uuid",s.target.uuid},{"slot",s.target.slot},{"policy",3},{"binding",a.binding},{"label","legacy"},{"future",true}}})}});CHECK(legacy.get(s.target,a)=="legacy");changed=a;changed.binding="different";CHECK(legacy.get(s.target,changed).empty());legacy.set(s.target,a,"typed");CHECK(legacy.get(s.target,changed)=="typed"&&legacy.document()["annotations"][0]["future"]==true);
 }
 {PolicyAnnotations notes;PolicyAnnotationCommits commits;auto plan=prepare_policy_annotation_profile(selected(source()),s,notes);auto retained=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,s);CHECK(commit_policy_annotation_effect(*retained,notes,commits).ok);const auto token=plan->transaction;plan.reset();
  for(unsigned i=0;i<100;++i){auto next=prepare_policy_annotation_profile(selected(source()),s,notes);auto event=qualify_policy_annotation_effect(next,PolicyAnnotationPhase::apply,s);CHECK(commit_policy_annotation_effect(*event,notes,commits).ok);CHECK(commits.transactions.size()<=2&&commits.transactions.count(token)==1);}
  const auto revision=notes.revision(s.target,a);CHECK(commit_policy_annotation_effect(*retained,notes,commits).ok&&notes.revision(s.target,a)==revision&&commits.transactions.size()==1);
 }
 {PolicyAnnotations notes({{"annotations",ProfileJson::array({{{"uuid",s.target.uuid},{"slot",s.target.slot},{"policy",3},{"binding",a.binding},{"label","PCIE 8pin"},{"future",9}}})}});auto one=source();one["power_policy_names"].erase("8");PolicyAnnotationCommits commits;
  auto plan=prepare_policy_annotation_profile(selected(one),s,notes);auto effect=qualify_policy_annotation_effect(plan,PolicyAnnotationPhase::apply,s);auto migrated=commit_policy_annotation_effect(*effect,notes,commits);CHECK(migrated.ok&&migrated.changed&&notes.document()["annotations"][0]["channel"]==4&&notes.document()["annotations"][0]["future"]==9);
  const auto revision=notes.revision(s.target,a);auto next=prepare_policy_annotation_profile(selected(one),s,notes);auto matching=qualify_policy_annotation_effect(next,PolicyAnnotationPhase::apply,s);const auto noop=commit_policy_annotation_effect(*matching,notes,commits);CHECK(noop.ok&&!noop.changed&&notes.revision(s.target,a)>revision);
 }
 for(bool same_label:{false,true}){PolicyAnnotations notes;PolicyAnnotationCommits commits;
  const auto edit=[&](const char* name){auto doc=source();doc["power_policy_names"].erase("8");doc["power_policy_names"]["3"]["name"]=name;auto p=prepare_policy_annotation_profile(selected(doc),s,notes);CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(p,PolicyAnnotationPhase::apply,s),notes,commits).ok);return p;};
  const auto undo=[&](const auto& p){return commit_policy_annotation_effect(*qualify_policy_annotation_effect(p,PolicyAnnotationPhase::undo,s),notes,commits);};
  auto first=edit("A"),second=edit(same_label?"A":"B"),third=edit(same_label?"A":"C");
  CHECK(undo(third).applied==1&&notes.get(s.target,a)==(same_label?"A":"B"));CHECK(undo(second).applied==1&&notes.get(s.target,a)=="A");CHECK(undo(first).applied==1&&notes.get(s.target,a).empty());
 }
 {PolicyAnnotations notes;PolicyAnnotationCommits commits;auto doc=source();doc["power_policy_names"].erase("8");doc["power_policy_names"]["3"]["name"]="A";auto first=prepare_policy_annotation_profile(selected(doc),s,notes);
  CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(first,PolicyAnnotationPhase::apply,s),notes,commits).ok);notes.set(s.target,a,"A"); // Standalone same-label edit must break the earlier ownership witness.
  doc["power_policy_names"]["3"]["name"]="B";auto second=prepare_policy_annotation_profile(selected(doc),s,notes);CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(second,PolicyAnnotationPhase::apply,s),notes,commits).ok);
  CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(second,PolicyAnnotationPhase::undo,s),notes,commits).applied==1);const auto old=commit_policy_annotation_effect(*qualify_policy_annotation_effect(first,PolicyAnnotationPhase::undo,s),notes,commits);CHECK(old.ok&&old.applied==0&&old.skipped==1&&notes.get(s.target,a)=="A");
 }
 {PolicyAnnotations notes,other_channel,other_card;PolicyAnnotationCommits commits;auto doc=source();doc["power_policy_names"].erase("8");doc["power_policy_names"]["3"]["name"]="A";
  auto first=prepare_policy_annotation_profile(selected(doc),s,notes);CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(first,PolicyAnnotationPhase::apply,s),notes,commits).ok);
  auto channel=s;channel.entries[0].channel=77;auto channel_doc=doc;channel_doc["power_policy_names"]["3"]["channel"]=77;auto channel_plan=prepare_policy_annotation_profile(selected(channel_doc),channel,other_channel);CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(channel_plan,PolicyAnnotationPhase::apply,channel),other_channel,commits).ok);
  auto card=s;card.target.uuid="GPU-distinct";auto card_doc=doc;card_doc["device"]["uuid"]=card.target.uuid;auto card_plan=prepare_policy_annotation_profile(selected(card_doc),card,other_card);CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(card_plan,PolicyAnnotationPhase::apply,card),other_card,commits).ok);
  doc["power_policy_names"]["3"]["name"]="B";auto second=prepare_policy_annotation_profile(selected(doc),s,notes);CHECK(commit_policy_annotation_effect(*qualify_policy_annotation_effect(second,PolicyAnnotationPhase::apply,s),notes,commits).ok);
  const auto undo_second=qualify_policy_annotation_effect(second,PolicyAnnotationPhase::undo,s);CHECK(commit_policy_annotation_effect(*undo_second,notes,commits).applied==1);
  CHECK(commits.transactions.at(channel_plan->transaction).rows[0].applied_revision==1&&commits.transactions.at(card_plan->transaction).rows[0].applied_revision==1);
  notes.set(s.target,a,"A");CHECK(commit_policy_annotation_effect(*undo_second,notes,commits).ok);const auto undo_first=commit_policy_annotation_effect(*qualify_policy_annotation_effect(first,PolicyAnnotationPhase::undo,s),notes,commits);CHECK(undo_first.applied==0&&undo_first.skipped==1&&notes.get(s.target,a)=="A");
 }
 std::printf("Policy annotation profile: %u checks\n",checks);return 0;
}
