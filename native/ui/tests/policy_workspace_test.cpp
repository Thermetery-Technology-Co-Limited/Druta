// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "policy_workspace.h"
#include <cstdio>
using namespace druta::ui;
int failures=0;void check(bool v,const char* s){if(!v){++failures;std::printf("FAIL: %s\n",s);}}
int main(){
 ScopedSnapshot s;s.target={"GPU-other","0000:09:00.0"};ScopedCurrent p;p.key=ScopedKey::policy_request(7);p.binding="policy-geometry-A";p.unit="mA";p.value=120000;p.low=1000;p.high=180000;p.fresh=p.writable=true;p.channel=4;p.policy_type=1;s.entries.push_back(p);
 auto b=p;b.key=ScopedKey::policy_request(9);b.binding="policy-B";b.policy_type=2;b.fresh=false;s.entries.push_back(b);
 auto u=p;u.key=ScopedKey::policy_request(10);u.binding="policy-C";u.writable=false;s.entries.push_back(u);
 PolicyAnnotations notes({{"future",{{"opaque",42}}},{"annotations",ProfileJson::array()}});notes.set(s.target,p,"connector note");
 check(notes.get(s.target,p)=="connector note" && notes.document()["future"]["opaque"]==42,"annotation preserves unknown JSON");
 auto other=s.target;other.uuid="GPU-new";check(notes.get(other,p).empty(),"alternate adapter cannot inherit note");
 auto changed=p;changed.binding="new geometry";check(notes.get(s.target,changed).empty(),"binding change cannot inherit note");
 auto stale=p;stale.fresh=false;check(notes.get(s.target,stale)=="connector note","transient freshness retains established annotation");
 auto paired=p;paired.key=ScopedKey::policy_request(12);paired.binding="paired";paired.policy_type=3;s.entries.push_back(paired);
 auto other_channel=p;other_channel.key=ScopedKey::policy_request(13);other_channel.binding="other-channel";other_channel.channel=5;s.entries.push_back(other_channel);
 notes.set_channel(s,p.key,"PCIE 8pin");check(notes.get(s.target,p)=="PCIE 8pin"&&notes.get(s.target,paired)=="PCIE 8pin"&&notes.get(s.target,b).empty()&&notes.get(s.target,other_channel).empty(),"propagate only fresh same-channel independently bound rows");
 auto wrong=p;wrong.policy_type=9;check(notes.get(s.target,wrong).empty(),"changed policy type refuses annotation");
 auto wire=policy_current_display("PCIE 8pin","mA",15.0);check(wire&&wire->even_split&&wire->amps==5&&wire->band==PolicyCurrentBand::white,"wire boundary inclusive with even-split assumption");
 check(policy_current_display("EPS 8pin","mA",40)->band==PolicyCurrentBand::yellow && policy_current_display("12VHPWR/12V-2x6","mA",78)->band==PolicyCurrentBand::red,"alternate connector wire counts and bands");
 check(policy_current_display("PCIE","mA",7.1)->band==PolicyCurrentBand::crimson && !policy_current_display("other","mA",12) && !policy_current_display("PCIE","mW",12),"slot thresholds and unknown topology");
 ProfileJson py={{"GPU-other",{{"7",{{"name","slot note"},{"channel",4},{"type",1}}},{"12",{{"name","wrong"},{"channel",9},{"type",3}}}}}};
 check(import_python_policy_names(py,s,notes)==1&&notes.get(s.target,p)=="slot note"&&notes.get(s.target,paired)=="PCIE 8pin","explicit Python import requires identity/channel/type");
 auto preview=preview_policy_max(s,{p.key,b.key,u.key,ScopedKey::policy_request(11),ScopedKey::board_power()});
 check(preview.document && preview.plan && preview.plan->steps.size()==1 && preview.plan->steps[0].key==p.key && preview.plan->steps[0].value==180000,"only qualified selected policy planned at current limit");
 check(preview.rows.size()==5 && !preview.rows[1].included && !preview.rows[2].included && !preview.rows[3].included && !preview.rows[4].included,"stale, gated, missing and board explicitly skipped");
 auto duplicate=preview_policy_max(s,{p.key,p.key});check(!duplicate.document,"duplicate selection refused");
 auto invalid=s;invalid.entries[0].high=-1;check(!preview_policy_max(invalid,{p.key}).document,"invalid bounds refused");
 auto foreign=s;foreign.target.uuid="GPU-replacement";bool rejected=false;try{plan_scoped_profile(*preview.document,foreign);}catch(...){rejected=true;}check(rejected,"worker preflight binds reviewed document to current target");
 std::printf("Policy workspace: %d failures\n",failures);return failures?1:0;
}
