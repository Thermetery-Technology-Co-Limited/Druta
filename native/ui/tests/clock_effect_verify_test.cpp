// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_effect_verify.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>

using namespace druta::ui;
namespace {
void check(bool ok, const char* test, int line) {
    if (!ok) { std::fprintf(stderr, "line %d: %s\n", line, test); std::exit(1); }
}
#define CHECK(x) check(bool(x), #x, __LINE__)
struct Fake {
    int offset = 12000, sibling = 0, sets = 0, locks = 0;
    bool mirror = false, stuck = false, co_move = false, foreign = false, wrong_id = false;
    bool foreign_sibling = false, p0 = true, fail_apply = false, fail_restore = false, public_mem = false;
    bool accept = true, change_source = false, inconsistent = false;
    int ticks = 0, reads_since_set = 0;
    bool intermediate = false, lag_only = false, near_mirror = false;
} f;
dc_gpu* const gpu = reinterpret_cast<dc_gpu*>(&f);
dc_status DC_CALL lock(dc_gpu*) { ++f.locks; return DC_OK; }
dc_status DC_CALL unlock(dc_gpu*) { --f.locks; return DC_OK; }
dc_status DC_CALL id(const dc_gpu*, dc_gpu_identity* out) {
    out->nvapi_ok = out->nvml_ok = out->nvapi_selected = out->nvml_selected = 1;
    std::strcpy(out->slot,"0000:01:00.0");
    std::strcpy(out->nvapi_slot,out->slot); std::strcpy(out->nvml_slot,out->slot);
    std::strcpy(out->uuid,f.wrong_id?"other":"gpu");
    return DC_OK;
}
dc_status DC_CALL telemetry(dc_gpu*, dc_telemetry* t) {
    t->present = DC_T_PSTATE | DC_T_PRIV_CLOCKS; t->pstate = f.p0 ? 0 : 8; return DC_OK;
}
dc_status DC_CALL classify(const uint32_t*, size_t, int32_t, uint32_t, int32_t, uint32_t, int32_t, uint32_t,
                          dc_clock_domains* d) {
    d->count = 2; ++f.ticks; ++f.reads_since_set;
    for (unsigned i=0; i<2; ++i) {
        auto& row = d->rows[i]; row.domain = i+1; row.scale=1;
        row.srcid = f.change_source && f.offset!=12000 ? 3 : 1;
        std::strcpy(row.kind,"freq"); std::strcpy(row.grade,"confirmed");
        if (i == 0 && f.public_mem) std::strcpy(row.name,"MEM");
        const int delta = f.offset-12000;
        row.prog_khz = 1800000 + ((i==0 || (f.co_move && !(f.inconsistent && f.sets>=3))) ? delta : 0);
        row.meas_khz = f.stuck ? 1700000 : row.prog_khz - (f.mirror ? 0 : (f.ticks%2 ? 4000 : 2000));
        if (f.near_mirror) row.meas_khz=row.prog_khz-1;
        if (f.sets && f.reads_since_set==1 && i==0 && (f.intermediate || f.lag_only))
            row.meas_khz = f.intermediate ? 1785000u : (f.offset!=12000 ? 1800000u : 1755000u);
    }
    return DC_OK;
}
dc_status DC_CALL pub(dc_gpu*, dc_public_clock_readings* out) {
    out->present = (1u << 2) | 1; out->mhz[0] = 1740;
    out->mhz[2] = f.public_mem ? 1800 + (f.offset-12000)/1000 : 5000; return DC_OK;
}
dc_status DC_CALL arch(dc_gpu*, uint32_t* out) { *out = 7; return DC_OK; }
dc_status DC_CALL layout(dc_gpu*, dc_clkdom_layout* out) {
    out->version = 3; out->block_size = 4096; out->stride = 32; return DC_OK;
}
dc_status DC_CALL requests(dc_gpu*, dc_clkdom_requests* out) {
    out->count=2; out->rows[0]={1,9,f.offset,0,0}; out->rows[1]={2,9,f.sibling,0,0};
    return DC_OK;
}
dc_status DC_CALL cap(dc_gpu*, uint32_t domain, int32_t mode, dc_outcome* out) {
    out->ok = f.accept && domain == 1 && mode == 9; return DC_OK;
}
dc_status DC_CALL set(dc_gpu*, int64_t domain, double mhz, dc_outcome* out) {
    CHECK(f.locks == 1 && f.p0 && !f.wrong_id && domain == 1);
    ++f.sets;
    f.reads_since_set=0;
    if (f.sets == 2 && f.fail_restore) return DC_E_FAILED;
    f.offset = static_cast<int>(mhz * 1000);
    if (f.sets == 1) {
        if (f.foreign) f.offset += 7000;
        if (f.foreign_sibling) f.sibling = 3000;
        if (f.fail_apply) return DC_E_FAILED;
    }
    out->ok=1; return DC_OK;
}
ClockEffectApi api() {
    ClockEffectApi a{};
    a.lock=lock; a.unlock=unlock; a.identity=id; a.telemetry=telemetry; a.classify=classify;
    a.public_clocks=pub; a.architecture=arch; a.layout=layout; a.requests=requests; a.capability=cap; a.set=set;
    a.wait_ms=[](unsigned){}; return a;
}
ClockEffectResult run(const ClockEffectApi& a, ClockEffectPending& p) {
    const auto r=verify_clock_effect(a,gpu,"0000:01:00.0","gpu",1,p);
    CHECK(f.locks==0); return r;
}
}
int main() {
    auto a=api(); ClockEffectPending p;
    auto r=run(a,p);
    CHECK(r.completed && r.verified && r.restored && f.offset==12000 && f.sets==2 && !p.active);
    CHECK(r.phases[0].size()==5 && r.phases[1][2].requests.rows[0].freq_khz==-33000);
    f=Fake{}; f.mirror=true; r=run(a,p);
    CHECK(r.completed && !r.verified && r.restored && r.message.find("No independent")!=std::string::npos);
    f=Fake{}; f.stuck=true; CHECK(!run(a,p).verified);
    f=Fake{}; f.near_mirror=true; CHECK(!run(a,p).verified);
    f=Fake{}; f.mirror=true; f.lag_only=true; CHECK(!run(a,p).verified);
    f=Fake{}; f.near_mirror=true; f.intermediate=true; CHECK(run(a,p).verified);
    f=Fake{}; f.public_mem=true; CHECK(run(a,p).verified);
    f=Fake{}; f.public_mem=true; f.mirror=true; CHECK(run(a,p).verified);
    f=Fake{}; f.co_move=true; r=run(a,p);
    CHECK(r.verified && r.coupled && r.restored && r.confirmation_phases[2].size()==5 && f.sets==4);
    f=Fake{}; f.co_move=true; f.inconsistent=true; r=run(a,p);
    CHECK(r.completed && !r.verified && r.restored && !r.coupled && f.sets==4);
    f=Fake{}; f.change_source=true; CHECK(!run(a,p).verified);
    f=Fake{}; f.change_source=true; f.public_mem=true; CHECK(run(a,p).verified);
    f=Fake{}; f.accept=false; CHECK(!run(a,p).attempted && f.sets==0);
    f=Fake{}; f.p0=false; r=run(a,p); CHECK(r.p0_lost && !r.attempted && f.sets==0);
    f=Fake{}; f.fail_apply=true; r=run(a,p);
    CHECK(!r.completed && r.attempted && r.restored && !r.verified && f.sets==2 && f.offset==12000);
    f=Fake{}; f.foreign=true; r=run(a,p);
    CHECK(!r.restored && p.active && !r.verified && f.sets==1);
    CHECK(!run(a,p).attempted && f.sets==1);
    f.offset=p.test_khz; CHECK(restore_clock_effect(a,gpu,p).restored && !p.active);
    f=Fake{}; f.foreign_sibling=true; CHECK(!run(a,p).restored && p.active && f.sets==1);
    f.sibling=0; f.p0=false; auto recovery=restore_clock_effect(a,gpu,p);
    CHECK(recovery.p0_lost && !recovery.attempted && !recovery.restored && p.active && f.sets==1);
    f.p0=true; recovery=restore_clock_effect(a,gpu,p);
    CHECK(recovery.attempted && recovery.restored && !p.active && f.sets==2);
    f=Fake{}; a.wait_ms=[](unsigned){ if (f.sets==1) f.p0=false; };
    r=run(a,p);
    CHECK(r.p0_lost && !r.verified && !r.restored && p.active && f.sets==1);
    f.p0=true; CHECK(restore_clock_effect(a,gpu,p).restored && f.sets==2 && !p.active);
    a=api();
    f=Fake{}; f.fail_restore=true;
    // The bounded final recovery makes a second attempt after fresh ownership checks.
    r=run(a,p); CHECK(!r.verified && r.restored && !p.active && f.sets==3);
    f=Fake{}; a.wait_ms=[](unsigned){ if (f.sets==1) f.wrong_id=true; };
    r=run(a,p); CHECK(!r.verified && !r.restored && p.active && f.sets==1);
    f.wrong_id=false; CHECK(restore_clock_effect(a,gpu,p).restored && !p.active);
    return 0;
}
