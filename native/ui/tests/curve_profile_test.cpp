/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "curve_profile.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <stdexcept>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x); std::abort(); } } while(false)
static SessionSnapshot fixture() {
    SessionSnapshot s; s.generation=7; s.curve_revision=1; s.selected_slot="0000:09:00.0";
    auto& d=s.data; d.connected=true; std::strcpy(d.identity.uuid,"GPU-alternate"); std::strcpy(d.identity.slot,s.selected_slot.c_str());
    d.curve_freq_div=2; d.curve_max_abs_delta_khz=100000;
    auto& l=d.curve_layout; l.size=sizeof(l); l.n_entries=7; l.gpu_count=2; l.other_count=1; l.freq_div=2;
    l.gpu_idx[0]=0; l.gpu_idx[1]=2; l.other_idx[0]=5;
    d.curve={{0,750,1400,1000},{2,925,1900,-5000}}; return s;
}
static bool rejected(const ProfileJson& p,const SessionSnapshot& s,CurveDraft& d) {
    try { stage_curve_profile(p,s,d); } catch (const std::runtime_error&) { return true; } return false;
}
int main() {
    auto source=fixture(); auto profile=capture_curve_profile(source.data);
    auto current=fixture(); current.data.curve[0].delta_khz=3000; current.data.curve[0].freq_mhz=1401;
    current.data.curve[1].delta_khz=7000; current.data.curve[1].freq_mhz=1906;
    CurveDraft draft; draft.observe(current.generation,current.data.curve,true,current.data.curve_freq_div);
    stage_curve_profile(profile,current,draft);
    CHECK(draft.dirty() && draft.changes().size()==2 && draft.can_undo());
    CHECK(draft.draft()[0].delta_khz==1000 && draft.draft()[1].delta_khz==-5000);
    CHECK(draft.undo_edit() && !draft.dirty() && !draft.can_undo());
    CHECK(draft.redo_edit() && draft.dirty()); CHECK(draft.undo_edit());
    const auto no_op=capture_curve_profile(current.data);
    stage_curve_profile(no_op,current,draft);
    CHECK(!draft.dirty() && !draft.can_undo() && draft.can_redo());
    CHECK(draft.redo_edit() && draft.dirty()); CHECK(draft.undo_edit());
    CHECK(draft.stage_khz(0,4000)); auto before=draft.draft();
    CHECK(rejected(profile,current,draft) && draft.draft()[0].delta_khz==before[0].delta_khz && draft.can_undo());
    CHECK(draft.undo_edit() && !draft.dirty());
    auto invalid=profile; invalid["points"][1]["delta_khz"]=100001;
    CHECK(rejected(invalid,current,draft) && !draft.dirty() && draft.can_redo());
    CHECK(draft.redo_edit() && draft.draft()[0].delta_khz==4000); CHECK(draft.undo_edit());
    invalid=profile; invalid["points"][1]["idx"]=99; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["points"][1]["idx"]=0; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["points"][1]["volt_mv"]=std::numeric_limits<double>::quiet_NaN(); CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["points"][1]["volt_mv"]=926; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["points"][1]["delta_khz"]=1.5; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["points"][1]["extra"]=1; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["target"]["uuid"]="GPU-other"; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["target"]["slot"]="0000:10:00.0"; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["geometry"]["other_indices"][0]=2; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["geometry"]["freq_div"]=1; CHECK(rejected(invalid,current,draft));
    invalid=profile; invalid["version"]=2; CHECK(rejected(invalid,current,draft));
    auto changed=current; changed.busy=true; CHECK(rejected(profile,changed,draft));
    changed=current; changed.curve_stale=true; CHECK(rejected(profile,changed,draft));
    changed=current; changed.curve_revision=0; CHECK(rejected(profile,changed,draft));
    changed=current; changed.data.connected=false; CHECK(rejected(profile,changed,draft));
    changed=current; changed.data.curve_layout.other_idx[0]=0; CHECK(rejected(profile,changed,draft));
    changed=current; changed.data.curve_max_abs_delta_khz=0; CHECK(rejected(profile,changed,draft));
    changed=current; ++changed.generation; CHECK(rejected(profile,changed,draft));
    CurveDraft stale=draft; stale.observe(current.generation,current.data.curve,false,current.data.curve_freq_div);
    CHECK(rejected(profile,current,stale));
    CHECK(!draft.dirty());
    std::printf("curve_profile_test: %u checks passed\n",checks);
}
