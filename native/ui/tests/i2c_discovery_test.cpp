// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Fake read-only NCT3933 ABI; no GPU, DLL or actual bus access.
#include "i2c_session.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <windows.h>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool value,const char* label){if(!value){++failures;std::printf("FAIL: %s\n",label);}}
struct Fake {
    dc_gpu_identity id{};uint8_t port=2,address=0x12;int reads=0,writes=0,locks=0,fail_reads=0,cancel_at=-1;
    bool cancel=false,partial=false,present=true;std::vector<I2cDiscoveryProgress> progress;
    Fake(){id.size=sizeof(id);id.nvapi_ok=id.nvapi_selected=1;id.nvapi_handle=7654321;id.nvapi_devid=0x9876;id.nvapi_subsys=0x12345678;
        std::snprintf(id.slot,sizeof(id.slot),"0000:9e:00.0");std::snprintf(id.uuid,sizeof(id.uuid),"GPU-route-test-alternate");}
    dc_gpu* gpu(){return reinterpret_cast<dc_gpu*>(this);}
};
Fake& fake(dc_gpu* gpu){return *reinterpret_cast<Fake*>(gpu);}
dc_status DC_CALL identity(const dc_gpu* gpu,dc_gpu_identity* out){*out=reinterpret_cast<const Fake*>(gpu)->id;return DC_OK;}
dc_status DC_CALL lock(dc_gpu* gpu){++fake(gpu).locks;return DC_OK;}
dc_status DC_CALL unlock(dc_gpu* gpu){--fake(gpu).locks;return DC_OK;}
dc_status DC_CALL bytes(dc_gpu* gpu,uint32_t port,uint32_t address,uint32_t reg,uint8_t* out,uint32_t count,dc_error*) {
    auto& f=fake(gpu);++f.reads;check(f.locks>0,"read-only discovery serialized on selected GPU");
    if(f.cancel_at>0 && f.reads>=f.cancel_at)f.cancel=true;
    if(f.fail_reads-->0 || port!=f.port || address!=f.address || count!=1 || !out)return DC_E_UNAVAILABLE;
    if(reg==0x5d)*out=f.present?0x39:0;else if(reg==0x5e)*out=0x33;
    else if(reg==5)*out=0;else if(reg>=1 && reg<=3){if(f.partial)return DC_E_UNAVAILABLE;*out=static_cast<uint8_t>(reg*2);}
    else return DC_E_UNAVAILABLE;return DC_OK;
}
dc_status DC_CALL read(dc_gpu* gpu,uint32_t port,uint32_t address,uint32_t reg,uint32_t count,uint32_t* out,dc_error* error){
    uint8_t value=0;const auto status=bytes(gpu,port,address,reg,&value,count,error);if(status==DC_OK)*out=value;return status;
}
dc_status DC_CALL write(dc_gpu* gpu,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t,dc_error*){++fake(gpu).writes;return DC_E_FAILED;}
dc_status DC_CALL typed(dc_gpu* gpu,uint32_t,uint32_t,uint32_t,uint32_t,const uint8_t*,uint32_t,dc_error*){++fake(gpu).writes;return DC_E_FAILED;}
I2cApi api(Fake& f,const std::wstring& path={}) {
    I2cApi a;a.identity=identity;a.lock=lock;a.unlock=unlock;a.read=read;a.read_bytes=bytes;a.write=write;a.controller_write=typed;
    a.sleep=[](unsigned){};a.discovery_cancelled=[&f]{return f.cancel;};a.discovery_progress=[&f](const auto& p){f.progress.push_back(p);};a.route_cache_path=path;return a;
}
Command scan(bool full=false){Command c;c.operation=Command::Operation::i2c_scan;c.i2c_family=I2cFamily::nct3933;c.i2c_full_scan=full;return c;}
Command select(Fake& f){Command c;c.operation=Command::Operation::i2c_select;c.i2c_family=I2cFamily::nct3933;c.i2c_route=I2cRoute{f.port,f.address};return c;}
I2cRouteHint hint(Fake& f){I2cRouteHint h;h.uuid=f.id.uuid;h.family=I2cFamily::nct3933;h.route={f.port,f.address};h.recipe="controller";h.contract=i2c_route_contract(h.family,h.recipe);return h;}
void discovery(){
    Fake f;I2cSession s(api(f),f.gpu(),f.id);check(s.execute(scan()).ok && s.data().selected && s.data().candidates.size()==1,"scoped scan finds and freshly selects single readable controller");
    check(f.writes==0 && !s.data().gate && !s.owned(),"discovery never enables gate, verifies writes or owns output");
    check(!f.progress.empty() && f.progress.back().completed==48 && !f.progress.back().running,"scoped progress counts every candidate route");
    const auto revision=s.data().selection_revision;f.fail_reads=1000;const auto request=scan();check(!s.execute(request).ok,"transient selected reconnect fails boundedly");
    check(s.data().selection_revision==revision && s.data().selected->port==f.port,"transient reconnect retains controller and recipe");
    f.fail_reads=0;f.partial=true;check(!s.execute(select(f)).ok && s.data().selection_revision==revision,"partial selection does not replace established controller");
    f.partial=false;f.cancel=true;const int before=f.reads;check(!s.execute(scan()).ok && f.reads==before,"cancel before probe performs no bus operation");
    check(s.data().selected && s.data().selection_revision==revision,"cancel preserves established selection and drafts revision");
    f.cancel=false;f.cancel_at=f.reads+2;check(!s.execute(select(f)).ok && s.data().selection_revision==revision,"cancel during explicit selection rolls back transaction without replacing controller");
    check(f.writes==0 && f.locks==0,"cancel leaves no write or lock ownership");
    f.cancel=false;f.cancel_at=-1;std::snprintf(f.id.uuid,sizeof(f.id.uuid),"GPU-changed");const int old=f.reads;
    check(!s.execute(select(f)).ok && f.reads==old && f.writes==0,"changed UUID rejects route and writes before bus use");
    std::snprintf(f.id.uuid,sizeof(f.id.uuid),"GPU-route-test-alternate");f.present=false;
    Command gate;gate.operation=Command::Operation::i2c_gate;gate.enabled=true;s.execute(gate);
    Command apply;apply.operation=Command::Operation::i2c_apply;apply.i2c_field="out1";apply.value=0;
    check(!s.execute(apply).ok && f.writes==0,"explicit Apply revalidates current controller identity before any write");
}
void partial_cancel(){
    Fake f;f.port=0;f.address=0x10;auto a=api(f);a.discovery_progress=[&f](const auto& p){f.progress.push_back(p);if(p.running && p.completed>0 && !p.candidates.empty())f.cancel=true;};
    I2cSession s(a,f.gpu(),f.id);const auto r=s.execute(scan());
    check(!r.ok && !s.data().selected && s.data().candidates.size()==1,"cancel after candidate publication retains partial unverified hit without automatic selection");
    check(f.progress.back().cancelled && !f.progress.back().running && f.writes==0,"cancel completion publishes bounded partial progress, no writes");
    f.cancel=false;check(s.execute(select(f)).ok,"partial hit requires fresh explicit selection after cancellation");
    Fake late;late.cancel_at=19;I2cSession late_session(api(late),late.gpu(),late.id);
    const auto completed=late_session.execute(select(late));
    check(completed.ok && late_session.data().selected && late_session.data().selection_revision==1
        && !late_session.data().discovery.cancelled,"cancel on first post-commit refresh read reports completed selection, never false retained-selection cancellation");
}
void cached(const std::wstring& path){
    Fake f;remember_i2c_route(path,hint(f));I2cSession s(api(f,path),f.gpu(),f.id);
    check(s.execute(scan()).ok && s.data().selected && f.reads<80,"matching remembered route narrows a fresh read-only probe");
    check(!s.data().gate && f.writes==0 && !s.owned(),"cache never replays gate, request or write qualification");
    Fake changed;changed.port=4;changed.address=0x14;I2cSession stale(api(changed,path),changed.gpu(),changed.id);
    check(stale.execute(scan()).ok && stale.data().selected->port==4,"stale positive hint falls back to scoped scan");
    const auto retained=read_profile(path);Fake transient;transient.fail_reads=1000;I2cSession failed(api(transient,path),transient.gpu(),transient.id);
    failed.execute(scan());check(read_profile(path)==retained,"failed or partial probe does not negatively cache/delete route");
    Fake other;std::snprintf(other.id.uuid,sizeof(other.id.uuid),"GPU-new-UUID");I2cSession alternate(api(other,path),other.gpu(),other.id);
    check(alternate.execute(scan()).ok && other.progress.back().total==48,"UUID change ignores another adapter's route hint");
    Fake full;I2cSession fs(api(full,path),full.gpu(),full.id);full.cancel_at=1;fs.execute(scan(true));
    check(!full.progress.empty() && full.progress.back().total>48,"Full scan bypasses cache and retains all-family scope");
}
}
int main(){discovery();partial_cancel();wchar_t directory[MAX_PATH]{};GetTempPathW(MAX_PATH,directory);
    const auto path=(std::filesystem::path(directory)/(L"druta-i2c-discovery-test-"+std::to_wstring(GetCurrentProcessId())+L".json")).wstring();
    std::filesystem::remove(path);cached(path);std::filesystem::remove(path);
    std::printf("I2C discovery: %d failures\n",failures);return failures?1:0;
}
