// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_policy_service.h"
#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <vector>
namespace druta::ui {
namespace {
constexpr uint32_t gate_get=0x2080A030,gate_set=0x2080A031,vf_get=0x2080901B,vf_set=0x2080D01C;
constexpr size_t vf_size=0x83C,vf_header=0x3C,guard_words=16;
using Header=std::array<uint8_t,vf_header>;
struct Failure:std::runtime_error {
    VoltagePolicyAvailability availability;
    Failure(VoltagePolicyAvailability a,const std::string& text):std::runtime_error(text),availability(a){}
};
[[noreturn]]void fail(const std::string& message,VoltagePolicyAvailability a=VoltagePolicyAvailability::invalid){throw Failure(a,message);}
void api_failure(const dc_error& e,const char* fallback){
    const auto a=e.kind==DC_ERR_UNSUPPORTED?VoltagePolicyAvailability::unsupported:e.kind==DC_ERR_IDENTITY?VoltagePolicyAvailability::invalid:VoltagePolicyAvailability::transient;
    const auto* end=static_cast<const char*>(std::memchr(e.message,0,sizeof(e.message)));
    fail(end&&e.message[0]?std::string(e.message,end):std::string(fallback),a);
}
bool terminated(const char* p,size_t n){return std::memchr(p,0,n)!=nullptr;}
bool same_id(const dc_gpu_identity& a,const dc_gpu_identity& b){
    return a.size==sizeof(a)&&b.size==sizeof(b)&&terminated(a.slot,sizeof(a.slot))&&terminated(b.slot,sizeof(b.slot))&&terminated(a.uuid,sizeof(a.uuid))&&terminated(b.uuid,sizeof(b.uuid))&&
        terminated(a.pairing_error,sizeof(a.pairing_error))&&terminated(b.pairing_error,sizeof(b.pairing_error))&&a.uuid[0]&&b.uuid[0]&&
        a.slot[0]&&a.nvapi_ok&&a.nvapi_selected&&!a.pairing_error[0]&&b.nvapi_ok&&b.nvapi_selected&&!b.pairing_error[0]&&std::strcmp(a.slot,b.slot)==0&&std::strcmp(a.uuid,b.uuid)==0&&
        a.nvapi_handle==b.nvapi_handle&&a.nvapi_devid==b.nvapi_devid&&a.nvapi_subsys==b.nvapi_subsys&&
        a.nvml_selected==b.nvml_selected&&a.nvml_has_devid==b.nvml_has_devid&&a.nvml_devid==b.nvml_devid&&a.nvml_subsys==b.nvml_subsys;
}
bool same_target(const dc_target& a,const dc_target& b){return a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id&&a.handle==b.handle&&std::strcmp(a.slot,b.slot)==0;}
struct Guard {
    VoltagePolicyApi& api;dc_gpu* gpu;bool held;
    Guard(VoltagePolicyApi& a,dc_gpu* g):api(a),gpu(g),held(a.lock&&a.unlock&&g&&a.lock(g)==DC_OK){}
    ~Guard(){if(held)api.unlock(gpu);}
};
struct Sample {uint32_t values=0;Header header{};dc_rm_transport transport{};};
struct Write {bool dispatched=false;std::optional<bool> previous;};
struct Record {uint32_t selection=0,before=0,attempted=0;dc_rm_transport transport{};bool prior_known=true;};
bool valid_selection(uint32_t mask){return mask&&!(mask&~voltage_all_policies)&&(!(mask&voltage_admission)||mask==voltage_admission);}
void header_valid(const Header& h){
    if(h[0]==0xA5||h[4]||h[5]||h[6]||h[7]||h[0x38]!=h[8])fail("Clock VF group, empty mask or duplicate flag is malformed");
    for(size_t i=8;i<14;++i)if(h[i]>1)fail("Clock VF flag is not canonical Boolean");
    for(size_t i:{size_t{1},size_t{2},size_t{3},size_t{14},size_t{15},size_t{0x39},size_t{0x3A},size_t{0x3B}})
        if(h[i]!=0xA5)fail("Clock VF reserved header geometry changed");
}
Header changed(Header h,uint32_t selection,uint32_t values){if(selection&voltage_monotonicity)h[0xA]=(values&voltage_monotonicity)?1:0;if(selection&voltage_smoothing)h[0xB]=(values&voltage_smoothing)?1:0;return h;}
}
struct VoltagePolicyService::Impl {
    VoltagePolicyApi api;dc_gpu* gpu;dc_gpu_identity target;std::optional<dc_target> binding;
    VoltagePolicyData data;std::optional<Record> undo_record,recovery_record,undo_before_recovery;
    Impl(VoltagePolicyApi a,dc_gpu* g,const dc_gpu_identity& id):api(a),gpu(g),target(id){}
    void bind(){
        if(!api.identity||!api.bind||!api.architecture_supported||!api.transport||!api.escape)fail("Voltage-policy interfaces unavailable",VoltagePolicyAvailability::unsupported);
        dc_gpu_identity id{};id.size=sizeof(id);if(api.identity(gpu,&id)!=DC_OK)fail("Paired GPU identity read unavailable",VoltagePolicyAvailability::transient);
        if(!same_id(target,id))fail("Voltage-policy adapter identity or handle changed");
        dc_target value{};value.size=sizeof(value);dc_error error{};error.size=sizeof(error);
        if(api.bind(gpu,1,&value,&error)!=DC_OK)api_failure(error,"Paired target binding unavailable");
        if(value.size!=sizeof(value)||!value.handle||!terminated(value.slot,sizeof(value.slot))||!value.slot[0]||std::strcmp(value.slot,target.slot)||value.handle!=target.nvapi_handle||
            value.device_id>0xFFFF||value.device_id!=target.nvapi_devid||value.subsystem_id!=target.nvapi_subsys)fail("Voltage-policy target does not match complete PCI identity");
        if(!api.architecture_supported(value.architecture))fail("Understood voltage-policy architecture unavailable",VoltagePolicyAvailability::unsupported);
        if(binding&&!same_target(*binding,value))fail("Voltage-policy sticky target changed");binding=value;
    }
    dc_rm_transport transport(){
        bind();dc_rm_transport t{};t.size=sizeof(t);dc_error error{};error.size=sizeof(error);
        if(api.transport(gpu,&t,&error)!=DC_OK)api_failure(error,"Paired transport unavailable");
        bind();if(!t.captured||t.header_count!=17||uint64_t(t.header[2])!=68+uint64_t(t.header[15])||
            t.header[0]!=0x4E564441||t.header[1]!=0x10002||t.header[3]!=0x4E562A2A||t.header[4]!=0x0500002B)fail("Unrecognized NVDA transport envelope");
        return t;
    }
    void reset_transport(){if(api.reset_transport)api.reset_transport(gpu,DC_TRANSPORT_POWER);}
    Sample call(uint32_t command,const dc_rm_transport& t,const Header* raw,uint32_t values,Write* write){
        const bool gate=command==gate_get||command==gate_set;const bool setting=command==gate_set||command==vf_set;
        const size_t payload=gate?(setting?8:4):vf_size,words=17+payload/4;
        std::vector<uint32_t> storage(words+2*guard_words,0xA5A5A5A5);auto* packet=storage.data()+guard_words;
        std::copy(t.header,t.header+17,packet);packet[2]=static_cast<uint32_t>(68+payload);packet[14]=command;packet[15]=static_cast<uint32_t>(payload);packet[16]=0;packet[5]=packet[11]=0;
        if(gate){packet[17]=setting?0:0xA5A55A5A;if(setting)packet[18]=(values&voltage_admission)?1:0;}
        else if(setting){if(!raw)fail("Missing fresh Clock VF header");header_valid(*raw);std::memcpy(packet+17,raw->data(),raw->size());}
        else packet[18]=0; // Explicit empty per-domain subset.
        std::array<uint32_t,16> expected{};std::copy(packet,packet+16,expected.begin());bind();
        int32_t ntstatus=0;dc_error error{};error.size=sizeof(error);if(write)write->dispatched=true;
        const auto status=api.escape(gpu,reinterpret_cast<uint8_t*>(packet),68+payload,&t.fields,&ntstatus,&error);
        if(status==DC_E_UNAVAILABLE&&write)write->dispatched=false; // ABI guarantees it was not issued.
        if(!std::all_of(storage.begin(),storage.begin()+guard_words,[](uint32_t v){return v==0xA5A5A5A5;})||
            !std::all_of(storage.end()-guard_words,storage.end(),[](uint32_t v){return v==0xA5A5A5A5;}))fail("Voltage-policy packet bounds changed");
        if(status!=DC_OK)api_failure(error,"Voltage-policy escape unavailable");
        if(ntstatus)fail("Voltage-policy transport failed (NTSTATUS "+std::to_string(ntstatus)+")",VoltagePolicyAvailability::transient);
        for(size_t i=0;i<16;++i)if(i!=5&&i!=11&&packet[i]!=expected[i])fail("Voltage-policy response changed transport geometry");
        if(packet[5]>1)fail("Invalid NVDA context output");
        if(packet[11]!=1)fail("NVDA escape completion failed",VoltagePolicyAvailability::transient);
        if(packet[16]){const uint32_t rm=packet[16];fail("Voltage policy rejected in current mode (RM "+std::to_string(rm)+")",
            rm==0x1F||rm==0x56||(command==vf_set&&rm==0x6C)?VoltagePolicyAvailability::unsupported:VoltagePolicyAvailability::transient);}
        Sample s;s.transport=t;
        if(gate){if(packet[17]>1||(setting&&packet[18]!=((values&1)?1u:0u)))fail("Noncanonical gate Boolean or changed request");
            s.values=packet[17]?voltage_admission:0;if(write)write->previous=packet[17]!=0;
        }else{
            auto* bytes=reinterpret_cast<const uint8_t*>(packet+17);std::copy(bytes,bytes+vf_header,s.header.begin());header_valid(s.header);
            if(!std::all_of(bytes+vf_header,bytes+vf_size,[](uint8_t b){return b==0xA5;}))fail("Clock VF empty-mask operation changed a per-domain entry");
            if(setting&&s.header!=*raw)fail("Clock VF SET changed its request header");
            s.values=(s.header[0xA]?voltage_monotonicity:0)|(s.header[0xB]?voltage_smoothing:0);
        }
        // Gate atomic previous is trustworthy before the post-call binding
        // check: retain it even if that check fails.
        bind();return s;
    }
    Sample fresh(uint32_t selection,const dc_rm_transport* fixed=nullptr){
        for(int attempt=0;attempt<2;++attempt){try{
            bind();const auto t=fixed?*fixed:transport();const auto command=selection==voltage_admission?gate_get:vf_get;
            const auto a=call(command,t,nullptr,0,nullptr),b=call(command,t,nullptr,0,nullptr);
            if(a.values!=b.values||(selection!=voltage_admission&&a.header!=b.header))fail("Voltage policy changed between fresh reads",VoltagePolicyAvailability::transient);
            return b;
        }catch(const Failure& error){if(error.availability!=VoltagePolicyAvailability::transient||attempt==1)throw;if(!fixed)reset_transport();}
        catch(const std::exception& error){if(attempt==1)fail(error.what(),VoltagePolicyAvailability::transient);if(!fixed)reset_transport();}}
        fail("Bounded voltage-policy read failed",VoltagePolicyAvailability::transient);
    }
    void publish(const Sample& sample,uint32_t mask){
        for(size_t i=0;i<3;++i)if(mask&(1u<<i)){auto& row=data.rows[i];row.current=(sample.values&(1u<<i))!=0;
            if(!row.initial)row.initial=row.current;row.availability=VoltagePolicyAvailability::available;row.writable=true;row.message.clear();}
    }
    void unavailable(uint32_t mask,VoltagePolicyAvailability availability,const std::string& message){
        for(size_t i=0;i<3;++i)if(mask&(1u<<i)){auto& row=data.rows[i];row.availability=availability;
            if(availability!=VoltagePolicyAvailability::transient)row.writable=false;row.message=message;}
    }
    void flags(){data.undo_available=undo_record&&undo_record->prior_known;data.undo_selection=undo_record?undo_record->selection:0;
        data.recovery_pending=recovery_record.has_value();data.recovery_prior_unknown=recovery_record&&!recovery_record->prior_known;
        data.recovery_selection=recovery_record?recovery_record->selection:0;++data.revision;}
    bool repair(Record& record,std::string& message){
        try{
            if(!record.prior_known)fail("Dispatched gate SET did not return a qualified atomic prior; prior-state recovery is unknown");
            const auto current=fresh(record.selection,record.selection==voltage_admission?nullptr:&record.transport);
            for(uint32_t bit:{1u,2u,4u})if(record.selection&bit){const auto now=current.values&bit;
                if(now!=(record.before&bit)&&now!=(record.attempted&bit))fail("Selected request changed outside this operation; recovery refused");}
            if((current.values&record.selection)==(record.before&record.selection)){publish(current,record.selection==1?1:6);return true;}
            const auto wanted=(current.values&~record.selection)|(record.before&record.selection);Write write;
            try{const auto header=changed(current.header,record.selection,wanted);
                call(record.selection==1?gate_set:vf_set,current.transport,record.selection==1?nullptr:&header,wanted,&write);
            }catch(...){ // A failed compensating SET can still have written.
                if(record.selection==1&&write.dispatched&&!write.previous){record.prior_known=false;throw;}
                if(write.previous&&record.selection==1&&((*write.previous?1u:0u)!=(current.values&1))){record.attempted=wanted;record.before=*write.previous?1u:0u;}
                if(!write.dispatched)throw;
                // Never dispatch compensation twice. A read may still establish
                // that its requested selected values reached the adapter.
            }
            if(record.selection==1&&write.previous&&((*write.previous?1u:0u)!=(current.values&1))){record.attempted=wanted;record.before=*write.previous?1u:0u;fail("Gate changed concurrently during recovery");}
            const auto after=fresh(record.selection,record.selection==1?nullptr:&record.transport);
            if((after.values&record.selection)!=(record.before&record.selection)||
                (record.selection!=1&&after.header!=changed(current.header,record.selection,wanted)))fail("Selected recovery readback differs");
            publish(after,record.selection==1?1:6);return true;
        }catch(const std::exception& error){message=error.what();return false;}
    }
    VoltagePolicyResult set(const VoltagePolicyRequest& request){
        VoltagePolicyResult result;Guard guard(api,gpu);if(!guard.held){result.message="GPU lock unavailable";return result;}
        if(!valid_selection(request.selection)||(request.values&~7u)||(request.expected&&(*request.expected&~7u))){result.message="Select only understood Boolean voltage policies";return result;}
        if(recovery_record){result.message="Recover the pending selected voltage-policy request first";return result;}
        Write write;std::optional<Record> record;
        try{
            const auto before=fresh(request.selection);publish(before,request.selection==1?1:6);
            if(request.expected&&((before.values^*request.expected)&request.selection)){
                result.message="Voltage policy changed since displayed state; review the fresh reading";
                data.message=result.message;flags();return result;
            }
            const auto wanted=(before.values&~request.selection)|(request.values&request.selection);
            record=Record{request.selection,before.values,wanted,before.transport};
            if(request.selection!=1&&wanted==before.values){result.ok=true;result.message="Selected VF policy already matches";data.message=result.message;flags();return result;}
            const auto header=changed(before.header,request.selection,wanted);
            call(request.selection==1?gate_set:vf_set,before.transport,request.selection==1?nullptr:&header,wanted,&write);
            if(write.previous){record->before=*write.previous?1u:0u;if(record->before!=(before.values&1))fail("Gate atomic previous state differs from pre-read");}
            const auto after=fresh(request.selection,request.selection==1?nullptr:&before.transport);
            if((after.values&request.selection)!=(wanted&request.selection)||
                (request.selection!=1&&after.header!=header))fail("Voltage policy selected or preserved header readback differs");
            publish(after,request.selection==1?1:6);record->selection&=record->before^wanted;
            if(record->selection)undo_record=*record;
            result.ok=true;result.may_have_changed=write.dispatched;result.message="Selected host policy request read back exactly; physical effect is not established";
        }catch(const std::exception& error){
            result.message=error.what();result.may_have_changed=write.dispatched;
            if(write.dispatched&&record){
                if(write.previous)record->before=*write.previous?1u:0u;
                else if(request.selection==1)record->prior_known=false;
                if(request.selection==1)reset_transport();std::string recovery_error;
                result.rollback_verified=repair(*record,recovery_error);
                if(result.rollback_verified)result.message+="; selected prior values restored and verified";
                else{undo_before_recovery=undo_record;recovery_record=*record;undo_record=*record;result.message+="; selected recovery pending: "+recovery_error;
                    unavailable(request.selection,VoltagePolicyAvailability::transient,result.message);}
            }else if(const auto* typed=dynamic_cast<const Failure*>(&error))unavailable(request.selection,typed->availability,error.what());
        }
        data.message=result.message;flags();return result;
    }
};
VoltagePolicyService::VoltagePolicyService(VoltagePolicyApi api,dc_gpu* gpu,const dc_gpu_identity& target):impl_(std::make_unique<Impl>(api,gpu,target)){}
VoltagePolicyService::~VoltagePolicyService()=default;
const VoltagePolicyData& VoltagePolicyService::state()const{return impl_->data;}
const VoltagePolicyData& VoltagePolicyService::read(uint32_t selection){auto& p=*impl_;Guard guard(p.api,p.gpu);
    if(!selection||(selection&~voltage_all_policies)){p.data.message="Select understood voltage policies to read";p.flags();return p.data;}
    if(!guard.held){p.data.message="GPU lock unavailable";p.unavailable(selection,VoltagePolicyAvailability::transient,p.data.message);p.flags();return p.data;}
    for(uint32_t mask:{1u,6u})if(selection&mask){try{p.publish(p.fresh(mask),mask);}catch(const Failure& error){p.unavailable(mask,error.availability,error.what());}
        catch(const std::exception& error){p.unavailable(mask,VoltagePolicyAvailability::transient,error.what());}}
    unsigned available=0,total=0;for(size_t i=0;i<3;++i)if(selection&(1u<<i)){++total;if(p.data.rows[i].availability==VoltagePolicyAvailability::available)++available;}
    p.data.message="Voltage policies read: "+std::to_string(available)+" / "+std::to_string(total)+" selected flags available";
    p.flags();return p.data;
}
VoltagePolicyResult VoltagePolicyService::apply(const VoltagePolicyRequest& request){return impl_->set(request);}
VoltagePolicyResult VoltagePolicyService::restore_initial(uint32_t selection){uint32_t values=0;for(size_t i=0;i<3;++i)if(selection&(1u<<i)){
    if(!impl_->data.rows[i].initial)return {false,false,false,"Read qualified initial policies first"};if(*impl_->data.rows[i].initial)values|=1u<<i;}
    if(impl_->recovery_record&&!impl_->recovery_record->prior_known&&selection==impl_->recovery_record->selection){
        // Explicit user choice of the captured initial request is a new
        // transaction, never a claim to recover an unknown atomic prior.
        const auto pending=impl_->recovery_record,undo=impl_->undo_record,older=impl_->undo_before_recovery;
        impl_->recovery_record.reset();impl_->undo_record.reset();
        auto result=impl_->set({selection,values,{}});
        if(!result.ok&&!impl_->recovery_record){impl_->recovery_record=pending;impl_->undo_record=undo;impl_->undo_before_recovery=older;impl_->flags();}
        if(result.ok)impl_->undo_before_recovery.reset();
        if(result.ok){result.message="Captured initial request explicitly applied and verified; unknown prior was not reconstructed";impl_->data.message=result.message;}
        return result;
    }
    return impl_->set({selection,values,{}});
}
VoltagePolicyResult VoltagePolicyService::recover(){auto& p=*impl_;VoltagePolicyResult out;Guard guard(p.api,p.gpu);
    if(!guard.held){out.message="GPU lock unavailable";return out;}if(!p.recovery_record){out.ok=true;out.message="No selected recovery pending";return out;}
    std::string error;out.may_have_changed=true;out.ok=p.repair(*p.recovery_record,error);out.rollback_verified=out.ok;
    if(out.ok){p.recovery_record.reset();p.undo_record=p.undo_before_recovery;p.undo_before_recovery.reset();out.message="Selected policy recovery verified; current unselected fields preserved";}else {out.message="Recovery pending: "+error;p.unavailable(p.recovery_record->selection,VoltagePolicyAvailability::transient,out.message);}
    p.data.message=out.message;p.flags();return out;
}
VoltagePolicyResult VoltagePolicyService::undo(){auto& p=*impl_;if(p.recovery_record)return recover();VoltagePolicyResult out;Guard guard(p.api,p.gpu);
    if(!guard.held){out.message="GPU lock unavailable";return out;}if(!p.undo_record){out.message="No voltage-policy Undo";return out;}
    std::string error;out.may_have_changed=true;out.ok=p.repair(*p.undo_record,error);
    if(out.ok){p.undo_record.reset();out.message="Selected prior policy values restored; unselected fields preserved";}
    else {p.recovery_record=p.undo_record;out.message="Selected Undo pending: "+error;p.unavailable(p.recovery_record->selection,VoltagePolicyAvailability::transient,out.message);}
    p.data.message=out.message;p.flags();return out;
}
}
