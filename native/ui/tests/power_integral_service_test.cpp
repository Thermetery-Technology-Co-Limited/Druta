// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Injectable ABI fixtures: no DLL, GPU, transport or hardware writes.
#include "power_integral_service.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <stdexcept>
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
    int fail_set_at=0,throw_set_at=0,interfere_at=0;
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
        check(get16(f.control,(b*4)+12)<=get16(f.control,(b*4)+14),"every intermediate SET preserves ordered bounds");
        if(f.interfere_at==f.sets)put16(f.control,upper0,5500);
        if(f.throw_set_at==f.sets)throw std::runtime_error("injected SET transport exception");
        if(f.foreign_after_set)put16(f.control,upper0,5000);
        if(f.unrelated_after_set){f.control[(0x14+40)/4]=0x12345678;f.unrelated_after_set=false;}
        if(f.change_identity_after_set)std::snprintf(f.id.uuid,sizeof(f.id.uuid),"GPU-replaced");
        f.fail_reads=f.post_failures;f.post_failures=0;
        *count=0;if(f.fail_set||f.fail_set_at==f.sets){error->kind=DC_ERR_TRANSIENT;std::snprintf(error->message,sizeof(error->message),"SET status uncertain");return DC_E_FAILED;}return DC_OK;
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
    Fake partial;partial.id.uuid[0]=0;PowerIntegralService ps(api(),partial.gpu(),partial.id);check(ps.read().current,"matching opened/current UUID absence retains paired runtime support");
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
PowerIntegralRequest staged(const PowerIntegralData& data,uint32_t policy,PowerIntegralField field,uint32_t raw){const auto& row=*std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.policy==policy;});return {policy,field,raw,row.type,row.channel,row.unit,field==PowerIntegralField::lower?row.lower:row.upper,row.binding};}
template<class F>void rejects(F f,const char* message){bool rejected=false;try{f();}catch(const std::exception&){rejected=true;}check(rejected,message);}
ProfileJson python_profile(const Fake& f,const ProfileJson& policies){return {{"schema",2},{"scope","power_integral"},{"saved_at","2026-11-13"},{"saved_ts",1.5},{"device",{{"name","alternate"},{"vbios",nullptr},{"driver",nullptr},{"uuid",f.id.uuid},{"slot",f.id.slot}}},{"incomplete",ProfileJson::array()},{"power_integral",{{"identity",{{"slot",f.id.slot},{"architecture",f.target.architecture},{"device_id",f.target.device_id},{"subsystem_id",f.target.subsystem_id}}},{"policies",policies}}}};}
void profiles(){
    Fake f;f.id.nvapi_devid=f.target.device_id=0x1234;PowerIntegralService s(api(),f.gpu(),f.id);auto data=s.read();const auto document=capture_power_integral_profile(data,{{0,PowerIntegralField::lower},{1,PowerIntegralField::upper}});auto loaded=stage_power_integral_profile(document,data);check(loaded.size()==2&&f.sets==0,"native selected capture/stage does not write and permits unrelated omitted fields");
    auto bad=document;bad["fields"][0]["type"]=7;rejects([&]{stage_power_integral_profile(bad,data);},"native changed policy metadata rejected");bad=document;bad["target"]["uuid"]="GPU-foreign";rejects([&]{stage_power_integral_profile(bad,data);},"native changed identity rejected");data.current=false;rejects([&]{stage_power_integral_profile(document,data);},"stale profile staging refused");data=s.read();
    const auto lower=python_profile(f,ProfileJson::array({{{"policy",0},{"type",5},{"channel",10},{"unit","mW"},{"values",{{"lower_raw",5000}}}}}));const auto upper=python_profile(f,ProfileJson::array({{{"policy",0},{"type",5},{"channel",10},{"unit","mW"},{"values",{{"upper_raw",6000}}}}}));
    rejects([&]{stage_power_integral_profile(lower,data);},"single lower child cannot exceed current upper");auto joined=stage_power_integral_profile(lower,data,false);const auto high=stage_power_integral_profile(upper,data,false);joined.insert(joined.end(),high.begin(),high.end());check(s.preview(joined).ok&&f.sets==0,"disjoint Python children coalesce before projected-range preflight");auto duplicate=joined;duplicate.push_back(joined[0]);check(!s.preview(duplicate).ok&&f.sets==0,"duplicate selected bounds rejected before writes");auto inverted=joined;inverted[1].raw=4000;check(!s.preview(inverted).ok&&f.sets==0,"inverted complete range refused before writes");
    std::vector<size_t> launched;auto result=s.apply(joined,[&](const auto& receipt){launched.push_back(receipt.requests.size());});check(result.ok&&f.sets==2&&get16(f.control,lower0)==5000&&get16(f.control,upper0)==6000,"upward range moves upper first through valid intermediate states");check(launched==std::vector<size_t>({1,2}),"callback exposes cumulative launched subset");check(s.undo_profile().ok&&get16(f.control,lower0)==1024&&get16(f.control,upper0)==4096,"grouped Undo safely lowers lower before upper and verifies originals");
    bad=lower;bad["power_integral"]["identity"]["architecture"]=8;rejects([&]{stage_power_integral_profile(bad,s.read(),false);},"Python architecture identity exact");bad=lower;bad["power_integral"]["policies"][0]["values"]["upper_raw"]=100;rejects([&]{stage_power_integral_profile(bad,s.read(),false);},"explicit inverted Python bounds malformed even when coalescing");
    f.fail_reads=2;s.read();rejects([&]{capture_power_integral_profile(s.data(),{{0,PowerIntegralField::lower}});},"failed fresh capture retains rows but cannot save stale values");
    Fake partial;put8(partial.info,0xCC+0x104+6,9);PowerIntegralService partial_s(api(),partial.gpu(),partial.id);const auto partial_data=partial_s.read();check(stage_power_integral_profile(capture_power_integral_profile(partial_data,{{0,PowerIntegralField::upper}}),partial_data).size()==1,"unrelated unsupported policy does not remove a selected supported profile field");
    Fake anonymous;anonymous.id.uuid[0]=0;PowerIntegralService anonymous_s(api(),anonymous.gpu(),anonymous.id);const auto anonymous_data=anonymous_s.read();check(anonymous_data.current&&anonymous_s.apply(0,PowerIntegralField::upper,5000).ok,"matching absent UUID retains complete paired runtime read/write support");rejects([&]{capture_power_integral_profile(anonymous_s.data(),{{0,PowerIntegralField::upper}});},"persistent native profile still requires UUID");std::strcpy(anonymous.id.uuid,"GPU-newly-visible");const auto anonymous_sets=anonymous.sets;check(!anonymous_s.apply(0,PowerIntegralField::upper,6000).ok&&anonymous.sets==anonymous_sets,"UUID appearance after opening invalidates runtime identity before SET");
}
void verification(){
    Fake f;PowerIntegralService s(api(),f.gpu(),f.id);auto data=s.read();
    auto plan=s.preview({staged(data,0,PowerIntegralField::upper,6000)});
    check(plan.ok&&s.verify(plan,true).ok&&!s.verify(plan).ok,"fresh verification distinguishes original and desired values");
    auto invalid=plan;invalid.requests.push_back(invalid.requests[0]);invalid.before.push_back(invalid.before[0]);invalid.bindings.push_back(invalid.bindings[0]);invalid.previous_owners.push_back(invalid.previous_owners[0]);
    auto calls=f.bind_calls;auto reads=f.reads;check(!s.verify(invalid).ok&&f.bind_calls==calls&&f.reads==reads,"duplicate receipt rejected before access");
    invalid=plan;invalid.bindings.clear();check(!s.verify(invalid).ok&&f.bind_calls==calls,"malformed receipt rejected before access");
    invalid=plan;std::strcpy(invalid.identity.uuid,"GPU-other");check(!s.verify(invalid).ok&&f.bind_calls==calls,"foreign receipt identity rejected before access");
    invalid=plan;invalid.target.architecture++;check(!s.verify(invalid,true).ok,"foreign receipt target rejected");
    invalid=plan;invalid.bindings[0]+="changed";invalid.requests[0].binding.clear();check(!s.verify(invalid,true).ok,"changed selected binding rejected");
    put16(f.control,0x14+0xC4+14,4500);check(s.verify(plan,true).ok,"unselected policy drift does not invalidate selected verification");
    put16(f.control,upper0,4500);check(!s.verify(plan,true).ok,"selected value drift rejected");
    put16(f.control,upper0,4096);f.fail_set_at=1;PowerIntegralPlan issued;
    check(!s.apply(std::vector<PowerIntegralRequest>{staged(s.read(),0,PowerIntegralField::upper,6000)},[&](const auto& p){issued=p;}).ok&&s.data().profile_recovery_pending,"uncertain receipt fixture");
    const auto writes=f.sets;check(s.verify(issued).ok&&!s.verify(issued,true).ok&&s.data().profile_recovery_pending&&s.data().profile_undo_available&&f.sets==writes,"verification never writes or discards uncertain grouped recovery");
    std::strcpy(f.id.uuid,"GPU-replaced");check(!s.verify(issued).ok&&s.data().profile_recovery_pending&&f.sets==writes,"fresh identity mismatch preserves recovery");
    std::strcpy(f.id.uuid,"GPU-alternate-policy");f.fail_set_at=0;check(s.recover_profile().ok,"verification retained usable recovery receipt");
}
void preview_recovery_gates(){
    Fake f;PowerIntegralService s(api(),f.gpu(),f.id);auto data=s.read();
    auto selected=staged(data,1,PowerIntegralField::upper,6000);f.fail_set_at=1;
    auto failed=s.apply(std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,5000)});
    check(!failed.ok&&s.data().profile_recovery_pending&&s.data().profile_undo_available,"uncertain group fixture retains receipt");
    const auto writes=f.sets,reads=f.reads;check(!s.preview({selected}).ok&&f.sets==writes&&f.reads==reads&&s.data().profile_recovery_pending&&s.data().profile_undo_available,"group recovery blocks preview before access and retains receipt");
    f.fail_set_at=0;check(s.recover_profile().ok,"group receipt remains recoverable after refused preview");
    Fake conflict;PowerIntegralService cs(api(),conflict.gpu(),conflict.id);data=cs.read();
    check(cs.apply(0,PowerIntegralField::upper,5000).ok,"owned field fixture");
    selected=staged(cs.data(),1,PowerIntegralField::upper,6000);put16(conflict.control,upper0,5500);const auto prior=conflict.sets;
    check(!cs.preview({selected}).ok&&conflict.sets==prior&&cs.has_undo()&&cs.data().rows[0].undo[1].recovery,"freshly discovered owned-field conflict blocks disjoint preview without losing Undo");
    put16(conflict.control,upper0,5000);check(cs.restore(0,PowerIntegralField::upper).ok,"owned receipt remains recoverable after refused preview");
}
void batch_failure(){
    Fake f;PowerIntegralService s(api(),f.gpu(),f.id);auto data=s.read();std::vector<PowerIntegralRequest> requests={staged(data,0,PowerIntegralField::upper,6000),staged(data,1,PowerIntegralField::upper,7000)};f.fail_set_at=1;PowerIntegralPlan receipt;auto result=s.apply(requests,[&](const auto& issued){receipt=issued;});check(!result.ok&&result.may_have_changed&&f.sets==1&&receipt.requests.size()==1&&s.data().profile_recovery_pending,"accepted failed first SET retains only launched field");check(get16(f.control,0x14+0xC4+14)==4096,"never-launched sibling unchanged");f.fail_set_at=0;const auto unknown=f.control[(0x14+40)/4]=0x99887766;check(s.recover_profile().ok&&get16(f.control,upper0)==4096&&f.control[(0x14+40)/4]==unknown,"recovery rebases typed selected field while preserving unrelated unknown bytes");
    Fake thrown;PowerIntegralService ts(api(),thrown.gpu(),thrown.id);data=ts.read();thrown.throw_set_at=2;requests={staged(data,0,PowerIntegralField::upper,6000),staged(data,1,PowerIntegralField::upper,7000)};result=ts.apply(requests,[&](const auto& issued){receipt=issued;});check(!result.ok&&receipt.requests.size()==2&&ts.data().profile_recovery_pending,"throwing second transport keeps both possibly launched requests");thrown.throw_set_at=0;check(ts.undo_profile().ok,"explicit grouped recovery after transport exception");
    Fake conflict;PowerIntegralService cs(api(),conflict.gpu(),conflict.id);data=cs.read();check(cs.apply(std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,6000)}).ok,"profile foreign replacement fixture");put16(conflict.control,upper0,6500);const auto writes=conflict.sets;check(!cs.undo_profile().ok&&conflict.sets==writes&&get16(conflict.control,upper0)==6500,"grouped Undo refuses external selected replacement before writes");
    Fake stale;PowerIntegralService ss(api(),stale.gpu(),stale.id);data=ss.read();requests={staged(data,0,PowerIntegralField::upper,6000),staged(data,1,PowerIntegralField::upper,7000)};put16(stale.control,0x14+0xC4+14,4500);check(!ss.apply(requests).ok&&stale.sets==0,"later stale sibling blocks every earlier write");
    Fake interference;PowerIntegralService is(api(),interference.gpu(),interference.id);data=is.read();requests={staged(data,0,PowerIntegralField::upper,6000),staged(data,1,PowerIntegralField::upper,7000)};interference.interfere_at=2;check(!is.apply(requests).ok&&is.data().profile_undo_available,"later component interference fails exact/final selected verification with recovery retained");
    Fake retry;PowerIntegralService rs(api(),retry.gpu(),retry.id);data=rs.read();requests={staged(data,0,PowerIntegralField::upper,6000),staged(data,1,PowerIntegralField::upper,7000)};check(rs.apply(requests).ok,"partial recovery fixture");retry.fail_set_at=4;check(!rs.undo_profile().ok&&rs.data().profile_recovery_pending,"failed second restore retains complete comparison receipt");const auto restored_writes=retry.sets;retry.fail_set_at=0;check(rs.recover_profile().ok&&retry.sets==restored_writes,"recovery verifies completed siblings without replaying either accepted restore");
    Fake previous;PowerIntegralService ps(api(),previous.gpu(),previous.id);check(ps.apply(0,PowerIntegralField::upper,5000).ok,"older per-field Undo fixture");data=ps.read();check(ps.apply(std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,6000)}).ok&&ps.undo_profile().ok,"profile Undo returns to prior owned request");check(ps.undo(0,PowerIntegralField::upper).ok&&get16(previous.control,upper0)==4096,"profile Undo preserves older per-field Undo state");
    Fake sequence;PowerIntegralService seq(api(),sequence.gpu(),sequence.id);data=seq.read();check(seq.apply(std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,5000)}).ok,"first confirmed profile");data=seq.read();check(!seq.restore(PowerIntegralPlan{}).ok&&seq.data().profile_undo_available&&!seq.data().profile_recovery_pending,"malformed external receipt cannot poison existing valid group Undo");auto wrong_receipt=seq.preview(std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,6000)});std::strcpy(wrong_receipt.identity.uuid,"GPU-wrong-receipt");check(!seq.restore(wrong_receipt).ok&&seq.data().profile_undo_available&&!seq.data().profile_recovery_pending,"wrong-target receipt preserves prior meaningful Undo");auto second=std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,6000)};auto noop=second;noop[0].raw=5000;const auto first_writes=sequence.sets;check(seq.apply(noop).ok&&sequence.sets==first_writes&&seq.data().profile_undo_available,"no-op next profile preserves previous group Undo");auto stale_second=second;stale_second[0].expected=4500;check(!seq.apply(stale_second).ok&&sequence.sets==first_writes&&seq.data().profile_undo_available,"stale next profile preflight preserves previous Undo");check(seq.apply(second).ok&&get16(sequence.control,upper0)==6000&&seq.undo_profile().ok&&get16(sequence.control,upper0)==5000,"consecutive confirmed profiles Undo to immediately preceding profile");data=seq.read();check(seq.apply(std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,7000)}).ok&&seq.apply(0,PowerIntegralField::upper,8000,7000).ok&&!seq.data().profile_undo_available&&seq.undo(0,PowerIntegralField::upper).ok&&get16(sequence.control,upper0)==7000,"individual continuation replaces grouped Undo only after launching and retains last individual Undo");
    Fake pending;PowerIntegralService pending_s(api(),pending.gpu(),pending.id);data=pending_s.read();pending.fail_set_at=1;check(!pending_s.apply(std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,5000)}).ok,"pending new profile fixture");data=pending_s.read();const auto pending_writes=pending.sets;check(!pending_s.apply(std::vector<PowerIntegralRequest>{staged(data,1,PowerIntegralField::upper,6000)}).ok&&!pending_s.apply(1,PowerIntegralField::upper,6000).ok&&pending.sets==pending_writes,"uncertain profile blocks both next batch and individual edit");
}
}
void history_boundary(){
    Fake f;PowerIntegralService service(api(),f.gpu(),f.id);auto data=service.read();int histories=0,expected_sets=0;
    service.set_history_callback([&]{check(f.sets==expected_sets,"history precedes first feedback SET");++histories;(void)capture_power_integral_profile(service.read(),{{0,PowerIntegralField::upper}});});
    check(service.apply(0,PowerIntegralField::upper,4096).ok&&histories==0&&f.sets==0,"feedback no-op has no history");
    check(!service.apply(0,PowerIntegralField::upper,5000,4500).ok&&histories==0&&f.sets==0,"stale feedback draft creates no history");
    check(service.apply(0,PowerIntegralField::upper,5000).ok&&histories==1&&f.sets==1,"changed feedback saves once before write");
    service.set_history_callback([]{throw std::runtime_error("history storage failed");});
    auto refused=service.apply(0,PowerIntegralField::upper,6000);check(!refused.ok&&!refused.may_have_changed&&f.sets==1&&service.has_undo(),"history failure preserves existing feedback request and Undo");
    refused=service.restore(0,PowerIntegralField::upper);check(!refused.ok&&!refused.may_have_changed&&f.sets==1&&service.has_undo(),"explicit feedback Restore refuses before effects if history fails");
    expected_sets=1;service.set_history_callback([&]{check(f.sets==expected_sets,"restore history precedes SET");++histories;});
    check(service.restore(0,PowerIntegralField::upper).ok&&histories==2&&f.sets==2,"captured initial Restore saves reverse snapshot");
    expected_sets=2;check(service.apply(0,PowerIntegralField::upper,6000).ok&&histories==3&&f.sets==3,"new individual feedback edit saves snapshot");
    check(service.undo(0,PowerIntegralField::upper).ok&&histories==3&&f.sets==4,"process-local Undo does not produce history");
    data=service.read();const std::vector<PowerIntegralRequest> requests{staged(data,0,PowerIntegralField::upper,7000),staged(data,1,PowerIntegralField::upper,8000)};
    expected_sets=4;check(service.apply(requests).ok&&histories==4&&f.sets==6,"multi-field standalone edit captures once");
    check(service.undo_profile().ok&&histories==4&&f.sets==8,"grouped feedback Undo does not create snapshots");
    service.set_history_callback([]{throw std::runtime_error("required history unavailable");});
    data=service.read();const auto one=std::vector<PowerIntegralRequest>{staged(data,0,PowerIntegralField::upper,7000)};
    refused=service.apply(one);check(!refused.ok&&!refused.may_have_changed&&f.sets==8&&!service.has_undo(),"batch history failure precedes ownership and SET");
    int journals=0;check(service.apply(one,[&](const auto&){++journals;}).ok&&journals==1&&f.sets==9,"external profile journal retains its own capture boundary");
}
int main(){normal();qualification();bounds();failures_and_recovery();profiles();verification();preview_recovery_gates();batch_failure();history_boundary();std::printf("Power integral service: %d failures\n",failures);return failures?1:0;}
