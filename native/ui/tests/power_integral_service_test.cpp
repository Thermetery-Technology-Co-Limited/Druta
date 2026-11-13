// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Injectable ABI fixtures: no DLL, GPU, transport or hardware writes.
#include "power_integral_service.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* why){if(!ok){++failures;std::printf("FAIL: %s\n",why);}}
void put8(std::vector<uint32_t>& words,size_t at,uint32_t value){const auto shift=static_cast<uint32_t>((at%4)*8);words[at/4]=(words[at/4]&~(255u<<shift))|(value<<shift);}
void put16(std::vector<uint32_t>& words,size_t at,uint16_t value){put8(words,at,value&255);put8(words,at+1,value>>8);}
uint16_t get16(const std::vector<uint32_t>& words,size_t at){return static_cast<uint16_t>((words[at/4]>>((at%4)*8))&65535);}
constexpr size_t lower0=0x14+12,upper0=0x14+14;
struct Fake {
    dc_gpu_identity id{};dc_target target{};dc_power_layout layout{};
    std::vector<uint32_t> info=std::vector<uint32_t>(20512/4),control=std::vector<uint32_t>(13876/4);
    uint32_t mask=0;int sets=0,reads=0,resets=0,depth=0,fail_reads=0,abi_failures=0,post_failures=0;
    bool fail_set=false,skip_set=false,foreign_after_set=false,unrelated_after_set=false,change_identity_after_set=false;
    bool require_mask=true;int bind_calls=0;
    Fake(){id.size=sizeof(id);id.nvapi_ok=id.nvml_ok=id.nvapi_selected=id.nvml_selected=1;
        std::snprintf(id.slot,sizeof(id.slot),"0000:7e:02.0");std::snprintf(id.uuid,sizeof(id.uuid),"GPU-alternate-policy");
        id.nvapi_handle=0x778899;id.nvapi_devid=0x11223344;id.nvapi_subsys=0x55667788;
        target.size=sizeof(target);std::snprintf(target.slot,sizeof(target.slot),"%s",id.slot);
        target.architecture=7;target.handle=id.nvapi_handle;target.device_id=id.nvapi_devid;target.subsystem_id=id.nvapi_subsys;
        layout.size=sizeof(layout);layout.sizes[0]=20512;layout.sizes[1]=397048;layout.sizes[2]=layout.sizes[3]=13876;
        layout.info_base=0xCC;layout.info_stride=0x104;layout.status_base=0x9C;layout.status_stride=0x1730;
        layout.control_base=0x14;layout.control_stride=0xC4;layout.has_status_mask_word=layout.has_control_mask_word=1;
        layout.status_mask_word=1;layout.control_mask_word=4;
        info[1]=3;control[3]=255;control[4]=3;
        for(size_t policy=0;policy<2;++policy){const size_t a=0xCC+policy*0x104,b=0x14+policy*0xC4;
            put8(info,a,0x30);put8(info,a+4,static_cast<uint32_t>(policy+5));put8(info,a+5,static_cast<uint32_t>(policy+10));put8(info,a+6,static_cast<uint32_t>(policy));
            info[(a+8)/4]=10;info[(a+16)/4]=100000;put8(info,a+0x20,2);put8(info,a+0x22,4);put8(info,a+0x23,5);
            put16(info,a+0x24,1024);put16(info,a+0x26,4096);control[b/4]=static_cast<uint32_t>(policy+5);control[(b+4)/4]=1000;
            put8(control,b+8,2);put8(control,b+9,4);put8(control,b+10,5);put8(control,b+11,0xA5);
            put16(control,b+12,1024);put16(control,b+14,4096);control[(b+40)/4]=0xDEADBEEF;
        }
    }
    dc_gpu* gpu(){return reinterpret_cast<dc_gpu*>(this);}
};
Fake& fake(dc_gpu* gpu){return *reinterpret_cast<Fake*>(gpu);}
dc_status DC_CALL lock(dc_gpu* gpu){++fake(gpu).depth;return DC_OK;}
dc_status DC_CALL unlock(dc_gpu* gpu){--fake(gpu).depth;return DC_OK;}
dc_status DC_CALL identity(const dc_gpu* gpu,dc_gpu_identity* out){*out=reinterpret_cast<const Fake*>(gpu)->id;return DC_OK;}
dc_status DC_CALL bind(dc_gpu* gpu,int32_t,dc_target* out,dc_error*){++fake(gpu).bind_calls;*out=fake(gpu).target;return DC_OK;}
int32_t DC_CALL probe(uint32_t arch){return arch>=4 && arch<=10?1:0;}
dc_status DC_CALL abi(dc_gpu* gpu,dc_power_layout* out,uint32_t* index,dc_error* error){auto& f=fake(gpu);
    if(f.abi_failures-->0){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}*out=f.layout;*index=0;return DC_OK;}
dc_status DC_CALL reset(dc_gpu* gpu,int32_t kind){check(kind==DC_TRANSPORT_POWER,"only power transport recovery");++fake(gpu).resets;return DC_OK;}
dc_status DC_CALL mask(dc_gpu* gpu,uint32_t value){fake(gpu).mask=value;return DC_OK;}
dc_status DC_CALL rm(dc_gpu* gpu,uint32_t command,const uint32_t* params,uint32_t param_count,int32_t has_mask,uint32_t policy_mask,int32_t live,uint32_t* out,uint32_t capacity,uint32_t* count,dc_error* error){
    auto& f=fake(gpu);check(f.depth>0,"all policy operations serialized");
    if(command==0x2080E61B){++f.sets;check(param_count==f.control.size() && params && live==1,"SET uses full fresh CONTROL with any-live selector");
        if(f.require_mask && !f.mask){error->kind=DC_ERR_INVALID;return DC_E_FAILED;}
        check(params[4]==1 || params[4]==2,"SET selects exactly one policy");
        // The RM selector changes one row, not the getter's aggregate mask.
        const size_t policy=params[4]==2?1:0,b=(0x14+policy*0xC4)/4;
        if(!f.skip_set)std::copy(params+b,params+b+0xC4/4,f.control.begin()+b);
        if(f.foreign_after_set)put16(f.control,upper0,5000);
        if(f.unrelated_after_set){f.control[(0x14+40)/4]=0x12345678;f.unrelated_after_set=false;}
        if(f.change_identity_after_set)std::snprintf(f.id.uuid,sizeof(f.id.uuid),"GPU-replaced");
        f.fail_reads=f.post_failures;f.post_failures=0;
        *count=0;if(f.fail_set){error->kind=DC_ERR_TRANSIENT;std::snprintf(error->message,sizeof(error->message),"SET status uncertain");return DC_E_FAILED;}return DC_OK;
    }
    ++f.reads;if(f.fail_reads-->0){error->kind=DC_ERR_TRANSIENT;return DC_E_FAILED;}
    const auto& words=command==0x2080A618?f.info:f.control;
    if(command==0x2080A61A)check(has_mask && policy_mask==f.info[1],"CONTROL GET uses INFO mask");
    if(capacity<words.size())return DC_E_BUFFER;
    std::copy(words.begin(),words.end(),out);*count=static_cast<uint32_t>(words.size());return DC_OK;
}
PowerIntegralApi api(){PowerIntegralApi a;a.lock=lock;a.unlock=unlock;a.identity=identity;a.target_bind=bind;a.probe_architecture=probe;
    a.abi=abi;a.rm=rm;a.transport_reset=reset;a.policy_mask_set=mask;return a;}
void normal(){
    Fake f;PowerIntegralService s(api(),f.gpu(),f.id);check(f.bind_calls==0,"construction performs no privileged probe");
    auto d=s.read();check(d.current && d.rows.size()==2 && d.rows[0].available && d.rows[1].available,"alternate identity and independent mW/mA rows qualify");
    const auto before=f.control;auto r=s.apply(0,PowerIntegralField::upper,5000,4096);
    check(r.ok && f.sets==1 && f.mask==3,"fresh service publishes live mask before SET");
    auto expected=before;put16(expected,upper0,5000);check(f.control==expected,"unknown bits and unrelated policies preserved exactly");
    check(s.data().rows[0].undo[1].initial==4096 && s.data().rows[0].undo[1].confirmed,"captured initial and exact readback ownership");
    check(s.apply(0,PowerIntegralField::upper,6000,5000).ok,"second explicit edit");
    check(s.undo(0,PowerIntegralField::upper).ok && get16(f.control,upper0)==5000,"Undo second edit restores previous edit, not initial");
    check(s.restore(0,PowerIntegralField::upper).ok && get16(f.control,upper0)==4096 && !s.has_undo(),"Restore returns captured initial only");
    check(f.depth==0,"serialized lock released");
    check(s.apply(0,PowerIntegralField::lower,2048,1024).ok,"lower is independently writable");
    check(s.restore(0,PowerIntegralField::lower).ok && get16(f.control,upper0)==4096,"lower restoration preserves upper");
}
void qualification(){
    Fake f;put8(f.info,0xCC+6,7);PowerIntegralService s(api(),f.gpu(),f.id);auto d=s.read();
    check(!d.rows[0].available && d.rows[1].available,"unsupported unit isolates row");check(!s.apply(0,PowerIntegralField::upper,5000).ok && f.sets==0,"unknown unit cannot write");
    check(s.apply(1,PowerIntegralField::upper,5000).ok,"unrelated qualified row stays usable");
    Fake mode;put8(mode.control,0x14+8,1);PowerIntegralService ms(api(),mode.gpu(),mode.id);check(!ms.read().rows[0].available,"unsupported mode refuses row");
    Fake layout;layout.layout.control_stride+=4;PowerIntegralService ls(api(),layout.gpu(),layout.id);check(!ls.read().current && layout.reads==0,"alternate unknown layout rejected before GET");
    Fake mapped;mapped.layout.has_commands=1;PowerIntegralService mapped_s(api(),mapped.gpu(),mapped.id);check(!mapped_s.read().current,"remapped commands rejected");
    Fake arch;arch.target.architecture=3;PowerIntegralService as(api(),arch.gpu(),arch.id);check(!as.read().current,"architecture probe gate rejects pre-Pascal");
    Fake changed;PowerIntegralService cs(api(),changed.gpu(),changed.id);std::snprintf(changed.id.uuid,sizeof(changed.id.uuid),"GPU-other");
    check(!cs.read().current && changed.reads==0,"UUID replacement rejected without policy access");
    Fake partial;partial.id.uuid[0]=0;PowerIntegralService ps(api(),partial.gpu(),partial.id);check(!ps.read().current,"missing opened UUID is not silently accepted");
    Fake malformed;std::memset(malformed.target.slot,'x',sizeof(malformed.target.slot));PowerIntegralService malformed_s(api(),malformed.gpu(),malformed.id);
    check(!malformed_s.read().current && malformed.reads==0,"unterminated first bound target rejected before string comparison or policy access");
    Fake size;size.layout.size=0;PowerIntegralService size_s(api(),size.gpu(),size.id);check(!size_s.read().current,"layout structure size must match understood ABI");
    Fake transient;transient.abi_failures=1;PowerIntegralService ts(api(),transient.gpu(),transient.id);check(ts.read().current && transient.resets==1,"bounded transient ABI recovery");
    transient.fail_reads=10;check(!ts.read().current && ts.data().rows.size()==2,"transient read preserves established rows");transient.fail_reads=0;check(ts.read().current,"manual read recovers");
}
void bounds(){
    check(power_integral_percent_to_raw(100)==4096 && power_integral_percent_to_raw(0)==0,"U4.12 percentages");
    check(power_integral_percent_to_raw(100.0/8192)==1,"half-up ratio rounding");
    check(!power_integral_percent_to_raw(-1) && !power_integral_percent_to_raw(1600),"unsigned overflow and negative rejected");
    Fake f;PowerIntegralService s(api(),f.gpu(),f.id);
    check(!s.apply(0,PowerIntegralField::lower,4097).ok && !s.apply(0,PowerIntegralField::upper,0).ok
        && !s.apply(0,PowerIntegralField::upper,65536).ok && !s.apply(32,PowerIntegralField::upper,5000).ok && f.sets==0,"bounds and absent policy preconditions");
    check(!s.apply(0,PowerIntegralField::upper,5000,3000).ok && f.sets==0,"stale selected-field draft rejected");
}
void failures_and_recovery(){
    Fake f;PowerIntegralService s(api(),f.gpu(),f.id);f.fail_set=true;
    auto r=s.apply(0,PowerIntegralField::upper,5000);
    // A repeated failing return still may have restored on the device; next
    // explicit recovery must verify that state without blind replay.
    check(!r.ok && r.may_have_changed && s.has_undo(),"failed SET retains uncertain selected-field recovery");
    f.fail_set=false;check(s.restore(0,PowerIntegralField::upper).ok && get16(f.control,upper0)==4096,"recovery verifies an already-restored initial field");
    Fake uncertain;PowerIntegralService us(api(),uncertain.gpu(),uncertain.id);uncertain.post_failures=20;
    r=us.apply(0,PowerIntegralField::upper,5000);check(!r.ok && us.has_undo() && uncertain.sets==1,"post-SET read failure never retries write or loses Undo");
    check(!us.apply(1,PowerIntegralField::upper,6000).ok && uncertain.sets==1,"uncertain dispatch blocks fresh edits globally");
    uncertain.fail_reads=0;check(us.restore(0,PowerIntegralField::upper).ok,"later explicit selected-field recovery");
    Fake conflict;PowerIntegralService cs(api(),conflict.gpu(),conflict.id);check(cs.apply(0,PowerIntegralField::upper,5000).ok,"conflict fixture owned edit");
    put16(conflict.control,upper0,5500);const int sets=conflict.sets;check(!cs.restore(0,PowerIntegralField::upper).ok && conflict.sets==sets && get16(conflict.control,upper0)==5500,"foreign selected replacement is never overwritten");
    Fake unrelated;PowerIntegralService rs(api(),unrelated.gpu(),unrelated.id);unrelated.unrelated_after_set=true;
    r=rs.apply(0,PowerIntegralField::upper,5000);check(!r.ok && r.rollback_verified && get16(unrelated.control,upper0)==4096,"full packet mismatch triggers selected-field rollback");
    check(unrelated.control[(0x14+40)/4]==0x12345678,"rollback rebases and preserves concurrently changed unknown field");
    Fake target;PowerIntegralService is(api(),target.gpu(),target.id);target.change_identity_after_set=true;r=is.apply(0,PowerIntegralField::upper,5000);
    check(!r.ok && is.has_undo() && target.sets==1,"target replacement after SET blocks rollback and retains recovery");
    Fake chained;PowerIntegralService chained_s(api(),chained.gpu(),chained.id);
    check(chained_s.apply(0,PowerIntegralField::upper,5000).ok && chained_s.apply(0,PowerIntegralField::upper,6000).ok,"chained recovery fixture");
    chained.fail_set=true;check(!chained_s.undo(0,PowerIntegralField::upper).ok && get16(chained.control,upper0)==5000,"failed Undo retains its accepted previous-edit candidate");
    chained.skip_set=true;check(!chained_s.restore(0,PowerIntegralField::upper).ok && get16(chained.control,upper0)==5000,"failed Restore preserves immediate pre-dispatch candidate");
    chained.fail_set=chained.skip_set=false;check(chained_s.restore(0,PowerIntegralField::upper).ok && get16(chained.control,upper0)==4096,"chained uncertain recovery can still restore captured initial");
    Fake absent;PowerIntegralService absent_s(api(),absent.gpu(),absent.id);check(absent_s.apply(0,PowerIntegralField::upper,5000).ok,"absent row fixture");
    absent.info[1]=2;absent.control[4]=2;check(!absent_s.restore(0,PowerIntegralField::upper).ok,"disappeared policy refuses restore");
    const auto& absent_rows=absent_s.data().rows;
    check(absent_rows.size()==2 && absent_rows.back().policy==0 && absent_rows.back().undo[1].recovery && !absent_rows.back().undo[1].confirmed,"absent owned field remains visibly recoverable without old confirmed state");
}
}
int main(){normal();qualification();bounds();failures_and_recovery();std::printf("Power integral service: %d failures\n",failures);return failures?1:0;}
