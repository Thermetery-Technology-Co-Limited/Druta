// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_saving_service.h"
#include <algorithm>
#include <cstring>
#include <exception>
#include <map>
#include <set>
#include <random>
#include <stdexcept>
#include <limits>
namespace druta::ui {
namespace {
using A=VoltagePolicyAvailability;
struct Error:std::runtime_error {A kind;Error(A k,const std::string& s):std::runtime_error(s),kind(k){}};
[[noreturn]] void fail(const std::string& s,A k=A::invalid){throw Error(k,s);}
void check(bool b,const char* s){if(!b)fail(s);}
void api_error(const dc_error& e){const bool valid=std::memchr(e.message,0,sizeof(e.message))!=nullptr;
    fail(valid&&e.message[0]?e.message:"Power-saving transport unavailable",e.kind==DC_ERR_TRANSIENT||e.kind==DC_ERR_UNSTABLE?A::transient:e.kind==DC_ERR_UNSUPPORTED?A::unsupported:A::invalid);}
template<size_t N>bool text(const char(&s)[N]){return std::memchr(s,0,N)!=nullptr;}
bool identity(const dc_gpu_identity& a,const dc_gpu_identity& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&text(a.slot)&&text(b.slot)&&text(a.uuid)&&text(b.uuid)&&text(a.pairing_error)&&text(b.pairing_error)&&a.slot[0]&&a.nvapi_ok&&b.nvapi_ok&&a.nvapi_selected&&b.nvapi_selected&&!a.pairing_error[0]&&!b.pairing_error[0]&&std::strcmp(a.slot,b.slot)==0&&std::strcmp(a.uuid,b.uuid)==0&&a.nvapi_handle==b.nvapi_handle&&a.nvapi_devid==b.nvapi_devid&&a.nvapi_subsys==b.nvapi_subsys&&a.nvml_selected==b.nvml_selected&&a.nvml_has_devid==b.nvml_has_devid&&a.nvml_devid==b.nvml_devid&&a.nvml_subsys==b.nvml_subsys;}
bool target_equal(const dc_target& a,const dc_target& b){return a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id&&a.handle==b.handle&&std::strcmp(a.slot,b.slot)==0;}
bool transport_equal(const dc_rm_transport& a,const dc_rm_transport& b){if(std::memcmp(&a.fields,&b.fields,sizeof(a.fields)))return false;for(size_t i:{0u,1u,3u,4u,6u,7u,8u,9u,10u,12u,13u})if(a.header[i]!=b.header[i])return false;return true;}
struct Lock {VoltagePolicyApi& a;dc_gpu* g;bool held;Lock(VoltagePolicyApi& api,dc_gpu* gpu):a(api),g(gpu),held(g&&a.lock&&a.unlock&&a.lock(g)==DC_OK){}~Lock(){if(held)a.unlock(g);}};
using Words=std::vector<uint32_t>;
uint16_t u16(const Words& w,size_t offset){uint16_t v;std::memcpy(&v,reinterpret_cast<const uint8_t*>(w.data())+offset,2);return v;}
void p16(Words& w,size_t offset,uint16_t v){std::memcpy(reinterpret_cast<uint8_t*>(w.data())+offset,&v,2);}
uint32_t id(PowerSavingKey k){return static_cast<uint32_t>(k.family)*32+k.index;}
const std::vector<uint32_t> individual_indices{0,4,10,11,12,13,14,15},adaptive_indices{0,2,4,5,6,7};
bool valid(PowerSavingKey k){switch(k.family){case PowerSavingFamily::gr_elpg:case PowerSavingFamily::difr:return k.index==0;case PowerSavingFamily::individual:case PowerSavingFamily::idle_threshold:return std::find(individual_indices.begin(),individual_indices.end(),k.index)!=individual_indices.end();case PowerSavingFamily::adaptive:return std::find(adaptive_indices.begin(),adaptive_indices.end(),k.index)!=adaptive_indices.end();}return false;}
std::string label(PowerSavingKey k){
    if(k.family==PowerSavingFamily::gr_elpg)return "Graphics (GR) ELPG";
    if(k.family==PowerSavingFamily::difr)return "DIFR";
    const std::map<uint32_t,const char*> individual{{0,"Graphics (GR)"},{4,"Memory subsystem (MS)"},{10,"MS DIFR SW-ASR"},{11,"MS DIFR CG"},{12,"NVDEC"},{13,"NVENC"},{14,"OFA"},{15,"NVJPG"}};
    const std::map<uint32_t,const char*> adaptive{{0,"Graphics (GR)"},{2,"Memory subsystem (MS)"},{4,"NVDEC"},{5,"NVENC"},{6,"NVJPG"},{7,"OFA"}};
    const auto& names=k.family==PowerSavingFamily::adaptive?adaptive:individual;const auto it=names.find(k.index);
    const char* prefix=k.family==PowerSavingFamily::adaptive?"Adaptive power: ":k.family==PowerSavingFamily::idle_threshold?"Idle threshold: ":"Individual power gating: ";
    return std::string(prefix)+(it==names.end()?"Unknown controller":it->second);
}
bool impl_supported(uint32_t impl){return impl==2||impl==3||impl==5||impl==6||impl==7;}
struct Controller {bool available=false,supported=false,enabled=false;uint32_t a=0,b=0;};
bool operator==(const Controller& a,const Controller& b){return a.available==b.available&&a.supported==b.supported&&a.a==b.a&&a.b==b.b;}
struct Sample {PowerSavingRow row;std::array<uint32_t,4> arch{};std::vector<Controller> controllers;dc_rm_transport transport{};};
bool same(const Sample& a,const Sample& b){return a.arch==b.arch&&a.row.binding==b.row.binding&&a.controllers==b.controllers&&a.row.supported==b.row.supported&&a.row.reason_a==b.row.reason_a&&a.row.reason_b==b.row.reason_b&&a.row.raw_first==b.row.raw_first&&a.row.raw_second==b.row.raw_second&&a.row.scaled_first==b.row.scaled_first&&a.row.scaled_second==b.row.scaled_second&&a.row.scale==b.row.scale&&(a.row.key.family==PowerSavingFamily::gr_elpg||a.row.aggregate_enabled==b.row.aggregate_enabled);}
struct Receipt {PowerSavingKey key;std::optional<uint32_t> before;uint32_t wanted=0;Sample sample;bool dispatched=false,release=false;};
struct Ownership {std::string binding;bool owned=false,uncertain=false,attempted=false,release_attempted=false;int bank=-1;std::vector<uint32_t> witnesses;};
struct MutationScope {std::optional<PowerSavingKey>& key;std::optional<PowerSavingKey> prior;MutationScope(std::optional<PowerSavingKey>& k,PowerSavingKey value):key(k),prior(k){key=value;}~MutationScope(){key=prior;}};
}
bool operator==(const PowerSavingKey& a,const PowerSavingKey& b){return a.family==b.family&&a.index==b.index;}
struct PowerSavingPlan::State {
    std::shared_ptr<int> session;dc_target target{};
    std::vector<PowerSavingRequest> requests;std::vector<Sample> before;
    std::map<uint32_t,uint64_t> epochs;
    mutable std::map<uint32_t,uint64_t> verification_epochs;
};
struct PowerSavingReceipt::State {
    std::shared_ptr<int> session;dc_target target{};
    std::vector<Receipt> issued;
    std::optional<Receipt> previous_undo;
    std::shared_ptr<State> previous_external;
    std::map<uint32_t,uint64_t> epochs;
    std::map<uint32_t,uint64_t> original_epochs;
    // Selected first-threshold requests can be affected by an adaptive SET.
    // These are typed raw observations, never replayable transport packets.
    std::map<uint32_t,Sample> coupled_idle,effects;
    std::set<uint32_t> unobserved_effects;
    bool incomplete=false;
};
struct PowerSavingService::Impl {
    PowerSavingApi api;dc_gpu* gpu;dc_gpu_identity opened;std::optional<dc_target> target;
    PowerSavingData data;std::map<uint32_t,Ownership> owners;std::map<uint32_t,std::pair<std::string,std::optional<uint32_t>>> initials;
    std::optional<Receipt> undo_record,pending;std::map<uint32_t,std::pair<std::string,uint32_t>> adaptive_requests;
    std::optional<dc_rm_transport> profiler_transport;uint32_t profiler=0;bool profiler_requested=false,allocation_uncertain=false;
    std::shared_ptr<int> session=std::make_shared<int>(0);
    std::shared_ptr<PowerSavingReceipt::State> external;
    std::function<void()> history_callback;bool suppress_history=false;
    std::function<void()> before_mutation;uint64_t mutation_serial=0;
    std::optional<PowerSavingKey> mutation_key;std::map<uint32_t,uint64_t> epochs;
    Impl(PowerSavingApi a,dc_gpu* g,const dc_gpu_identity& i):api(std::move(a)),gpu(g),opened(i){data.identity=i;
        for(int f=0;f<5;++f){const auto family=static_cast<PowerSavingFamily>(f);auto indices=f==1||f==2?individual_indices:f==3?adaptive_indices:std::vector<uint32_t>{0};for(auto n:indices){PowerSavingRow r;r.key={family,n};r.label=label(r.key);data.rows.push_back(r);}}}
    void bind(){auto& a=api.transport;if(!a.identity||!a.bind||!a.transport||!a.escape||!a.architecture_supported)fail("Power-saving interfaces unavailable",A::unsupported);
        dc_gpu_identity i{};i.size=sizeof(i);if(a.identity(gpu,&i)!=DC_OK)fail("Adapter identity unavailable",A::transient);check(identity(opened,i),"Adapter identity or pairing changed");
        dc_target t{};t.size=sizeof(t);dc_error e{};e.size=sizeof(e);if(a.bind(gpu,1,&t,&e)!=DC_OK)api_error(e);
        check(t.size==sizeof(t)&&text(t.slot)&&t.slot[0]&&t.handle&&std::strcmp(t.slot,opened.slot)==0&&t.handle==opened.nvapi_handle&&t.device_id==opened.nvapi_devid&&t.subsystem_id==opened.nvapi_subsys,"Incomplete or changed target binding");
        if(!a.architecture_supported(t.architecture))fail("Power-saving architecture unavailable",A::unsupported);if(target)check(target_equal(*target,t),"Sticky adapter target changed");target=t;data.target=t;}
    dc_rm_transport transport(){bind();dc_rm_transport t{};t.size=sizeof(t);dc_error e{};e.size=sizeof(e);if(api.transport.transport(gpu,&t,&e)!=DC_OK)api_error(e);bind();
        check(t.size==sizeof(t)&&t.captured&&t.header_count==17&&uint64_t(t.header[2])==68+uint64_t(t.header[15])&&t.header[0]==0x4E564441&&t.header[1]==0x10002&&t.header[3]==0x4E562A2A&&t.header[4]==0x0500002B,"Unrecognized paired NVDA transport geometry");return t;}
    Words send(Words words,const dc_rm_transport& t,size_t status_index,const std::vector<size_t>& mutable_words,bool* dispatched=nullptr,bool allocation=false){
        const auto current=transport();check(transport_equal(t,current),"Power-saving routing changed; requests cannot be replayed");
        std::vector<uint32_t> storage(words.size()+32,0xA5A5A5A5);auto* packet=storage.data()+16;std::copy(words.begin(),words.end(),packet);
        int32_t nt=0;dc_error e{};e.size=sizeof(e);if(dispatched){if(before_mutation)before_mutation();++mutation_serial;if(mutation_key)++epochs[id(*mutation_key)];*dispatched=true;}
        const auto status=api.transport.escape(gpu,reinterpret_cast<uint8_t*>(packet),words.size()*4,&t.fields,&nt,&e);
        if(status==DC_E_UNAVAILABLE&&dispatched){*dispatched=false;--mutation_serial;if(mutation_key)--epochs[id(*mutation_key)];}
        check(std::all_of(storage.begin(),storage.begin()+16,[](uint32_t v){return v==0xA5A5A5A5;})&&std::all_of(storage.end()-16,storage.end(),[](uint32_t v){return v==0xA5A5A5A5;}),"Power-saving packet bounds changed");
        if(status!=DC_OK)api_error(e);
        for(size_t n=0;n<words.size();++n)if(n!=5&&n!=11&&n!=status_index&&std::find(mutable_words.begin(),mutable_words.end(),n)==mutable_words.end())check(packet[n]==words[n],"Power-saving response changed preserved routing or fields");
        check(packet[5]<=1,"Noncanonical NVDA context output");if(nt||packet[11]!=1)fail("Power-saving escape completion failed",A::transient);
        if(packet[status_index])fail("Power-saving RM request rejected: "+std::to_string(packet[status_index]),packet[status_index]==0x1F||packet[status_index]==0x56?A::unsupported:A::transient);
        const auto after=transport();check(transport_equal(t,after),"Power-saving target routing changed during operation");
        // Only a qualified response proves this exact candidate is our object.
        // A possible allocation with malformed acknowledgement remains unknown.
        if(allocation){profiler=words[14];allocation_uncertain=false;profiler_transport=t;}
        return {packet,packet+words.size()};}
    Words call(uint32_t command,const Words& payload,const dc_rm_transport& t,bool* dispatched=nullptr){Words words(t.header,t.header+17);words.insert(words.end(),payload.begin(),payload.end());words[2]=static_cast<uint32_t>(words.size()*4);words[5]=words[11]=words[16]=0;words[14]=command;words[15]=static_cast<uint32_t>(payload.size()*4);
        std::vector<size_t> outputs;for(size_t n=17;n<words.size();++n)outputs.push_back(n);const auto result=send(words,t,16,outputs,dispatched);return {result.begin()+17,result.end()};}
    std::optional<uint32_t> attribute(const dc_rm_transport& t,uint16_t group,uint16_t index,uint16_t attr){Words request(0x404/4);request[0]=1;p16(request,4,group);p16(request,6,index);p16(request,8,attr);auto response=call(0x2080A801,request,t);
        const auto flags=u16(response,10);const auto value=response[3];check(flags<=1,"Noncanonical LPWR attribute flags");p16(response,10,0);response[3]=0;check(response==request,"LPWR attribute changed unknown fields");if(group==4)check(flags||!value,"Unavailable adaptive attribute has a value");return flags?std::optional<uint32_t>(value):std::nullopt;}
    uint32_t required(const dc_rm_transport& t,uint16_t group,uint16_t index,uint16_t attr,bool boolean=false){const auto value=attribute(t,group,index,attr);check(value.has_value(),"Required LPWR attribute unavailable");if(boolean)check(*value<=1,"Noncanonical LPWR Boolean");return *value;}
    void set_attribute(const Sample& s,uint16_t group,uint16_t attr,uint32_t value,bool& dispatched){Words request(0x404/4);request[0]=1;p16(request,4,group);p16(request,6,static_cast<uint16_t>(s.row.key.index));p16(request,8,attr);request[3]=value;auto response=call(0x2080E802,request,s.transport,&dispatched);p16(request,10,1);check(response==request,"LPWR SET did not acknowledge only the selected field");}
    std::array<uint32_t,2> raw_pair(const dc_rm_transport& t,uint32_t index){std::array<uint32_t,2> output{};
        for(uint32_t sentinel:{0xA5A5A5A5u,0x5A5A5A5Au}){std::array<uint32_t,38> storage{};storage.fill(0xC3C3C3C3);auto* entries=storage.data()+16;entries[0]=1;entries[1]=index;entries[2]=sentinel;entries[3]=2;entries[4]=index;entries[5]=sentinel;Words payload(4);payload[0]=2;const auto pointer=reinterpret_cast<uintptr_t>(entries);static_assert(sizeof(pointer)<=8);std::memcpy(payload.data()+2,&pointer,sizeof(pointer));
            const auto response=call(0x20801706,payload,t);check(response==payload,"Idle raw GET changed pointer or count");check(std::all_of(storage.begin(),storage.begin()+16,[](uint32_t n){return n==0xC3C3C3C3;})&&std::all_of(storage.end()-16,storage.end(),[](uint32_t n){return n==0xC3C3C3C3;}),"Idle raw entry bounds changed");check(entries[0]==1&&entries[1]==index&&entries[3]==2&&entries[4]==index,"Idle raw selectors changed");std::array<uint32_t,2> values{entries[2],entries[5]};if(sentinel==0xA5A5A5A5)output=values;else check(output==values,"Idle raw entries were skipped or unstable");}return output;}
    Sample once(PowerSavingKey key){Sample s;s.row.key=key;s.row.label=label(key);s.transport=transport();if(key.family!=PowerSavingFamily::gr_elpg&&key.family!=PowerSavingFamily::difr){auto arch=call(0x20801701,Words(4),s.transport);std::copy(arch.begin(),arch.end(),s.arch.begin());}
        const auto architecture=s.arch[0],implementation=s.arch[1];const auto f=key.family;
        if(f==PowerSavingFamily::individual){if(architecture!=0x160&&architecture!=0x170&&architecture!=0x190&&architecture!=0x1B0)fail("Individual gating native layout unavailable",A::unsupported);
            if(key.index==4){const bool qualified=(architecture==0x1B0&&impl_supported(implementation))||((architecture==0x170||architecture==0x190)&&(implementation==2||implementation==3||implementation==4||implementation==6||implementation==7));if(!qualified)fail("MS gating dispatch class is not qualified",A::unsupported);}}
        if(f==PowerSavingFamily::idle_threshold||f==PowerSavingFamily::adaptive)if(architecture!=0x1B0||!impl_supported(implementation))fail("Native idle/adaptive dispatch layout unavailable",A::unsupported);
        s.row.binding=std::to_string(target->architecture)+":"+std::to_string(s.arch[0])+":"+std::to_string(s.arch[1])+":"+std::to_string(s.arch[2])+":"+std::to_string(s.arch[3]);
        s.row.capability=s.row.binding;
        // Include exact opaque routing in runtime draft binding, never serialize routing for replay.
        for(size_t n:{0u,1u,3u,4u,6u,7u,8u,9u,10u,12u,13u})s.row.binding+=":"+std::to_string(s.transport.header[n]);
        for(auto n:{s.transport.fields.hAdapter,s.transport.fields.hDevice,s.transport.fields.Type,s.transport.fields.Flags,s.transport.fields.hContext})s.row.binding+=":"+std::to_string(n);
        if(f==PowerSavingFamily::gr_elpg){for(uint32_t n=0;n<22;++n){Controller c;auto support=attribute(s.transport,3,static_cast<uint16_t>(n),0x39);c.available=support.has_value();if(support)check(*support<=1,"Noncanonical GR support");c.supported=support&&*support==1;if(c.supported){c.enabled=required(s.transport,3,static_cast<uint16_t>(n),0x37,true)!=0;c.a=required(s.transport,3,static_cast<uint16_t>(n),0x48);c.b=required(s.transport,3,static_cast<uint16_t>(n),0x49);s.row.supported=true;}s.controllers.push_back(c);}}
        else if(f==PowerSavingFamily::adaptive){s.row.supported=required(s.transport,4,static_cast<uint16_t>(key.index),9,true)!=0;s.row.aggregate_enabled=required(s.transport,4,static_cast<uint16_t>(key.index),10,true)!=0;}
        else {const auto n=static_cast<uint16_t>(f==PowerSavingFamily::difr?9:key.index);auto support=attribute(s.transport,3,n,0x39);if(support)check(*support<=1,"Noncanonical power-gating support");s.row.supported=support&&*support==1;
            if(f==PowerSavingFamily::difr){Words query{3};const auto r=call(0x20802801,query,s.transport);check(r[0]==1||r[0]==2,"Noncanonical DIFR support reply");s.row.supported=s.row.supported&&r[0]==1;}
            if(s.row.supported){if(f!=PowerSavingFamily::idle_threshold){s.row.aggregate_enabled=required(s.transport,3,n,0x37,true)!=0;s.row.reason_a=required(s.transport,3,n,0x48);s.row.reason_b=required(s.transport,3,n,0x49);}
                if(f==PowerSavingFamily::idle_threshold){s.row.scaled_first=required(s.transport,3,n,0x3A);s.row.scaled_second=required(s.transport,3,n,0x3B);const auto independent=required(s.transport,3,n,0x4C);const auto raw=raw_pair(s.transport,n);check(raw[0]==independent,"Independent first idle raw value disagrees");s.row.raw_first=raw[0];s.row.raw_second=raw[1];uint64_t low=1,high=UINT32_MAX;for(size_t i=0;i<2;++i){const uint64_t r=raw[i],scaled=i?s.row.scaled_second:s.row.scaled_first;low=std::max(low,r*1000/(scaled+1)+1);if(scaled)high=std::min(high,r*1000/scaled);}check(low<=high,"Idle raw/scaled relationship inconsistent");if(low==high){s.row.scale=static_cast<uint32_t>(low);s.row.maximum_us=static_cast<uint32_t>(std::min<uint64_t>(UINT32_MAX,((uint64_t{1}<<32)*1000-1)/low));const uint64_t us=(uint64_t(s.row.raw_first)*1000+low-1)/low;if(us&&us<=s.row.maximum_us&&std::max<uint64_t>(1,us*low/1000)==s.row.raw_first)s.row.value=static_cast<uint32_t>(us);}else s.row.message="Idle scale is ambiguous; inspection only";}}}
        s.row.availability=A::available;s.row.writable=s.row.supported&&(f!=PowerSavingFamily::idle_threshold||s.row.scale);return s;}
    Sample fresh(PowerSavingKey key){for(int attempt=0;attempt<2;++attempt)try{const auto a=once(key),b=once(key);if(!same(a,b))fail("Power-saving state changed between paired reads",A::transient);return b;}catch(const Error& e){if(e.kind!=A::transient||attempt)throw;if(api.transport.reset_transport)api.transport.reset_transport(gpu,DC_TRANSPORT_POWER);}fail("Bounded read exhausted",A::transient);}
    PowerSavingRow& row(PowerSavingKey key){auto it=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.key==key;});check(it!=data.rows.end(),"Unknown power-saving row");return *it;}
    void publish(Sample s){auto& o=owners[id(s.row.key)];auto& prior=row(s.row.key);if(o.owned&&!o.binding.empty()&&o.binding!=s.row.binding)o.uncertain=true;s.row.owned=o.owned;s.row.uncertain=o.uncertain;
        if(s.row.key.family==PowerSavingFamily::adaptive){auto it=adaptive_requests.find(id(s.row.key));if(it!=adaptive_requests.end()){if(it->second.first==s.row.binding)s.row.value=it->second.second;else adaptive_requests.erase(it);}}
        else if(s.row.key.family!=PowerSavingFamily::idle_threshold)s.row.value=o.owned?1u:0u;
        auto it=initials.find(id(s.row.key));if(it!=initials.end()&&it->second.first!=s.row.binding){initials.erase(it);it=initials.end();}
        if(it==initials.end()){initials[id(s.row.key)]={s.row.binding,s.row.key.family==PowerSavingFamily::adaptive||o.uncertain?std::optional<uint32_t>{}:s.row.value};it=initials.find(id(s.row.key));}s.row.initial=it->second.second;
        if(o.uncertain){s.row.writable=false;s.row.message="Uncertain request; explicit recovery only";}prior=std::move(s.row);}
    void unavailable(PowerSavingKey key,const Error& e){auto& r=row(key);r.stale=!r.binding.empty();r.availability=e.kind;r.message=e.what();r.writable=false;r.owned=owners[id(key)].owned;r.uncertain=owners[id(key)].uncertain;}
    Sample sample(PowerSavingKey key){try{auto value=fresh(key);publish(value);return value;}
        catch(const Error& error){unavailable(key,error);throw;}
        catch(const std::exception& error){unavailable(key,Error(A::transient,error.what()));throw;}}
    void witness(const Sample& before){const auto& o=owners[id(before.row.key)];if(!o.owned)return;
        check(!o.uncertain&&o.binding==before.row.binding,"Owned request binding or certainty changed");
        const auto f=before.row.key.family;
        if(f==PowerSavingFamily::individual)check((before.row.reason_a&1)!=0,"Owned individual witness disappeared");
        else if(f==PowerSavingFamily::difr)check(o.bank>=0&&(o.bank==0?((before.row.reason_a&1)&&!(before.row.reason_b&1)):((before.row.reason_b&1)&&!(before.row.reason_a&1))),"Owned DIFR witness disappeared");
        else if(f==PowerSavingFamily::gr_elpg){check(profiler&&profiler_requested&&!allocation_uncertain&&!o.witnesses.empty(),"Owned GR profiler witness unavailable");for(auto n:o.witnesses)check(n<before.controllers.size()&&before.controllers[n].supported&&(before.controllers[n].a&1),"Owned GR reason witness disappeared");}
    }
    void qualify_request(const PowerSavingRequest& request,const Sample& before){
        check(valid(request.key)&&!request.binding.empty()&&request.binding==before.row.binding,"Staged power-saving binding changed");
        const auto& current=row(request.key);const auto& o=owners[id(request.key)];
        check(before.row.writable&&!o.uncertain,"Selected family is unavailable or ownership uncertain");
        check(current.value.has_value(),"Catalog Undo requires a known prior explicit request; aggregate state is not a prior request");
        check(!request.expected||request.expected==current.value,"Selected request changed since staging");
        if(request.key.family==PowerSavingFamily::idle_threshold)check(request.value&&request.value<=before.row.maximum_us,"Idle request exceeds current-adapter bounds");
        else check(request.value<=1,"Power-saving request is not Boolean");
        witness(before);
        if(request.key.family==PowerSavingFamily::individual&&request.value&&!o.owned)check(!(before.row.reason_a&1),"Reason zero already has a foreign individual owner");
        if(request.key.family==PowerSavingFamily::difr&&request.value&&!o.owned)check(!((before.row.reason_a|before.row.reason_b)&1),"DIFR reason zero is occupied");
        if(request.key.family==PowerSavingFamily::gr_elpg&&!o.owned){check(!profiler&&!allocation_uncertain,"Resolve the existing profiler object before catalog Apply");
            if(request.value){check(before.transport.header[12]&&before.transport.header[13],"Profiler requires captured root and parent");check(std::any_of(before.controllers.begin(),before.controllers.end(),[](const auto& c){return c.supported&&!(c.a&1);}),"GR acquisition has no independent reason transition available");}}
    }
    std::shared_ptr<PowerSavingPlan::State> prepare(const std::vector<PowerSavingRequest>& requests){
        check(!pending&&!allocation_uncertain&&(!external||(!external->incomplete&&external->unobserved_effects.empty())),"Resolve uncertain request, coupled effect or profiler object before profile preflight");check(!requests.empty()&&requests.size()<=24,"Select between one and twenty-four power-saving requests");
        auto plan=std::make_shared<PowerSavingPlan::State>();plan->session=session;
        const auto selected_gr=std::find_if(requests.begin(),requests.end(),[](const auto& r){return r.key.family==PowerSavingFamily::gr_elpg;});
        const auto& gr_owner=owners[id({PowerSavingFamily::gr_elpg,0})];
        const bool release_gr=selected_gr!=requests.end()&&selected_gr->value==0&&gr_owner.owned;
        if(release_gr)witness(sample({PowerSavingFamily::gr_elpg,0}));
        bool desired_count=false,original_count=false;
        for(const auto& r:requests){check(valid(r.key),"Invalid power-saving selection");check(std::none_of(plan->requests.begin(),plan->requests.end(),[&](const auto& old){return old.key==r.key;}),"Duplicate power-saving target");
            auto current=sample(r.key);auto projected=current;const bool counted=r.key.family==PowerSavingFamily::individual||r.key.family==PowerSavingFamily::difr;
            if(counted){desired_count|=r.value!=0;original_count|=owners[id(r.key)].owned;
                if(gr_owner.owned&&r.value!=row(r.key).value.value_or(0))check(release_gr,"Select owned GR Release to qualify the shared-count handover");
                const uint32_t controller=r.key.family==PowerSavingFamily::difr?9:r.key.index;
                if(release_gr&&!owners[id(r.key)].owned&&std::find(gr_owner.witnesses.begin(),gr_owner.witnesses.end(),controller)!=gr_owner.witnesses.end())projected.row.reason_a&=~1u;
            }
            if(r.key.family==PowerSavingFamily::gr_elpg&&!gr_owner.owned&&r.value)for(const auto& count:requests){const auto& owner=owners[id(count.key)];if(count.value||!owner.owned)continue;
                const uint32_t controller=count.key.family==PowerSavingFamily::difr?9:count.key.index;
                if(count.key.family!=PowerSavingFamily::individual&&count.key.family!=PowerSavingFamily::difr)continue;
                if(controller<projected.controllers.size()){if(count.key.family==PowerSavingFamily::difr&&owner.bank==1)projected.controllers[controller].b&=~1u;else projected.controllers[controller].a&=~1u;}}
            qualify_request(r,projected);auto frozen=r;frozen.expected=row(r.key).value;current.row.value=frozen.expected;plan->requests.push_back(std::move(frozen));plan->before.push_back(std::move(current));plan->epochs[id(r.key)]=epochs[id(r.key)];
        }
        if(selected_gr!=requests.end()){
            check(!(selected_gr->value&&desired_count),"Simultaneous GR/count requests lack an independent shared-count ownership getter; select an exclusive handover");
            check(!(gr_owner.owned&&original_count),"Original simultaneous GR/count ownership cannot be independently restored; release it explicitly first");}
        // Adaptive can reevaluate thresholds; selected explicit idle requests
        // are applied only after all selected adaptive requests are settled.
        std::vector<size_t> order;for(size_t i=0;i<plan->requests.size();++i)order.push_back(i);
        std::stable_sort(order.begin(),order.end(),[&](size_t a,size_t b){const auto rank=[&](size_t n){const auto& r=plan->requests[n];if(r.key.family==PowerSavingFamily::adaptive)return -1;if(selected_gr==requests.end())return 0;if(r.key.family==PowerSavingFamily::idle_threshold)return 3;if(!r.value)return 0;return r.key.family==PowerSavingFamily::gr_elpg?2:1;};return rank(a)<rank(b);});
        auto old_requests=std::move(plan->requests);auto old_before=std::move(plan->before);for(auto i:order){plan->requests.push_back(std::move(old_requests[i]));plan->before.push_back(std::move(old_before[i]));}
        bind();plan->target=*target;plan->verification_epochs=plan->epochs;return plan;
    }
    void paired(const std::shared_ptr<int>& token,const dc_target& expected){check(token==session,"Power-saving transaction belongs to another service session");
        try{bind();check(target_equal(*target,expected),"Power-saving transaction target changed");}
        catch(const Error& error){for(const auto& r:data.rows)unavailable(r.key,error);throw;}
        catch(const std::exception& error){for(const auto& r:data.rows)unavailable(r.key,Error(A::transient,error.what()));throw;}}
    void selected(const PowerSavingRequest& request,const Sample& baseline,uint32_t wanted,bool allow_uncertain=false){
        const auto current=sample(request.key);check(current.row.binding==baseline.row.binding,"Selected power-saving binding changed");
        if(request.key.family==PowerSavingFamily::idle_threshold){check(current.row.scale==baseline.row.scale&&current.row.scale,"Idle scale changed");
            check(current.row.raw_first==std::max<uint64_t>(1,uint64_t(wanted)*baseline.row.scale/1000),"Selected idle request differs");}
        else check(row(request.key).value==wanted,"Selected explicit power-saving request differs");
        if(!allow_uncertain)witness(current);
        check(current.row.supported&&current.row.writable,"Selected power-saving capability unavailable");
    }
    void selected_raw(PowerSavingKey key,const Sample& baseline,uint32_t raw){const auto current=sample(key);
        check(current.row.binding==baseline.row.binding&&current.row.scale==baseline.row.scale&&current.row.supported&&current.row.writable,"Selected idle capability, scale or binding changed");
        check(current.row.raw_first==raw,"Selected idle request changed outside the coupled transaction");}
    void observe_effects(PowerSavingReceipt::State& state){std::string failure;
        for(const auto& [key,baseline]:state.coupled_idle)try{const auto current=sample(baseline.row.key);
            check(current.row.binding==baseline.row.binding&&current.row.scale==baseline.row.scale&&current.row.supported&&current.row.writable,"Adaptive effect changed selected idle layout or scale");
            state.effects[key]=current;state.unobserved_effects.erase(key);
        }catch(const std::exception& error){state.unobserved_effects.insert(key);if(failure.empty())failure=error.what();}
        if(!failure.empty())fail("Selected adaptive idle effect could not be qualified: "+failure,A::transient);
    }
    void issued_effects(PowerSavingReceipt::State& state){for(const auto& [key,baseline]:state.coupled_idle){(void)baseline;++epochs[key];state.epochs[key]=epochs[key];state.unobserved_effects.insert(key);}}
    bool idle_candidate(const PowerSavingReceipt::State& state,const Receipt& record,const Sample& current){
        if(current.row.raw_first==record.sample.row.raw_first||current.row.raw_first==std::max<uint64_t>(1,uint64_t(record.wanted)*current.row.scale/1000))return true;
        const auto observed=state.effects.find(id(record.key));return observed!=state.effects.end()&&current.row.raw_first==observed->second.row.raw_first;
    }
    void flags(){data.undo_available=((undo_record&&undo_record->before.has_value())||(external&&!external->issued.empty()))&&!pending&&!allocation_uncertain;data.recovery_pending=pending.has_value()||allocation_uncertain||(external&&(external->incomplete||!external->unobserved_effects.empty()));data.owned_requests=false;for(const auto& [key,o]:owners){const auto family=static_cast<PowerSavingFamily>(key/32);if(family==PowerSavingFamily::gr_elpg||family==PowerSavingFamily::individual||family==PowerSavingFamily::difr)data.owned_requests|=o.owned||o.uncertain;}data.owned_requests|=profiler!=0||allocation_uncertain;++data.revision;}
    void allocate(const Sample& s){check(!profiler&&!allocation_uncertain,"Profiler allocation is already owned or uncertain");check(s.transport.header[12]&&s.transport.header[13],"Profiler requires captured root and parent");uint32_t candidate=0;if(api.profiler_handle)candidate=api.profiler_handle();else{std::random_device random;candidate=0x60000000u|(random()&0xFFFFFFu);}check((candidate&0xFF000000u)==0x60000000u&&candidate!=s.transport.header[12]&&candidate!=s.transport.header[13],"Invalid fresh profiler candidate");Words words(s.transport.header,s.transport.header+12);words.insert(words.end(),{s.transport.header[12],s.transport.header[13],candidate,0xB2CC,8,0x77FEF81E,0,UINT32_MAX,0,0});words[2]=88;words[4]=0x0500002A;words[5]=words[11]=0;bool dispatched=false;try{send(words,s.transport,19,{},&dispatched,true);}catch(...){if(dispatched&&!profiler)allocation_uncertain=true;throw;}}
    void profiler_control(uint32_t command,const Words& payload,bool& dispatched){check(profiler&&profiler_transport,"No confirmed owned profiler");Words words(profiler_transport->header,profiler_transport->header+17);words.insert(words.end(),payload.begin(),payload.end());words[2]=static_cast<uint32_t>(words.size()*4);words[5]=words[11]=0;words[13]=profiler;words[14]=command;words[15]=static_cast<uint32_t>(payload.size()*4);words[16]=UINT32_MAX;const auto result=send(words,*profiler_transport,16,command==0xB0CC0301?std::vector<size_t>{17,19}:std::vector<size_t>{},&dispatched);if(command==0xB0CC0301)check(result[17]==0&&((result[19]>>6)&3)==0,"GR ELPG disable request rejected");}
    void free_profiler(){if(!profiler)return;MutationScope scope(mutation_key,{PowerSavingFamily::gr_elpg,0});check(!allocation_uncertain,"Profiler object outcome uncertain; driver reset required");const auto& t=*profiler_transport;Words words(t.header,t.header+12);words.insert(words.end(),{t.header[12],t.header[13],profiler,UINT32_MAX});words[2]=64;words[4]=0x0500000C;words[5]=words[11]=0;bool issued=false;try{send(words,t,15,{},&issued);}catch(...){if(issued)allocation_uncertain=true;throw;}profiler=0;profiler_requested=false;profiler_transport.reset();}
    void transition(const Sample& before,const Sample& after,uint32_t wanted,Ownership& owner){check(before.arch==after.arch&&before.row.binding==after.row.binding&&before.row.supported==after.row.supported,"Power-saving capability or binding changed during request");const auto f=before.row.key.family;
        if(f==PowerSavingFamily::gr_elpg){check(before.controllers.size()==after.controllers.size(),"GR inventory changed");std::vector<uint32_t> witnesses;for(size_t n=0;n<before.controllers.size();++n){const auto& a=before.controllers[n];const auto& b=after.controllers[n];check(a.available==b.available&&a.supported==b.supported&&a.b==b.b,"GR inventory or unrelated reason bank changed");if(!a.supported){check(a==b,"Unsupported GR controller changed");continue;}if(wanted){check(b.a==a.a||b.a==(a.a|1u),"GR disable changed unrelated reasons");if(a.a!=b.a)witnesses.push_back(static_cast<uint32_t>(n));}else {const bool witnessed=std::find(owner.witnesses.begin(),owner.witnesses.end(),n)!=owner.witnesses.end();check(b.a==(witnessed?(a.a&~1u):a.a),"GR release did not clear exact witnessed reasons");}}if(wanted){check(!witnesses.empty(),"GR disable had no independent reason transition");owner.witnesses=std::move(witnesses);}return;}
        if(f==PowerSavingFamily::adaptive)return; // aggregate is not a unique request getter
        if(f==PowerSavingFamily::idle_threshold){const uint32_t expected=static_cast<uint32_t>(std::max<uint64_t>(1,uint64_t(wanted)*before.row.scale/1000));check(after.row.scale==before.row.scale&&after.row.raw_first==expected&&after.row.raw_second==before.row.raw_second&&after.row.scaled_second==before.row.scaled_second&&after.row.scaled_first==uint64_t(expected)*1000/before.row.scale&&after.row.reason_a==before.row.reason_a&&after.row.reason_b==before.row.reason_b,"Idle selected or preserved threshold readback mismatch");return;}
        const auto a=before.row.reason_a,b=before.row.reason_b,c=after.row.reason_a,d=after.row.reason_b;
        if(f==PowerSavingFamily::individual){check(d==b&&(wanted?c==(a|1u):(c==(a&~1u)||c==a)),"Individual gating reason readback changed unrelated fields");return;}
        if(wanted){const bool first=c==(a|1u)&&c!=a&&d==b,second=d==(b|1u)&&d!=b&&c==a;check(first!=second,"DIFR acquisition has no unique reason-bank witness");owner.bank=first?0:1;}else check(owner.bank==0?(c==(a&~1u)&&d==b):(d==(b&~1u)&&c==a),"DIFR release did not verify owned bank");}
    uint32_t restore_us(const PowerSavingRow& r,uint32_t raw){check(r.scale&&raw,"Idle original raw value is not representable");const uint64_t us=(uint64_t(raw)*1000+r.scale-1)/r.scale;check(us&&us<=r.maximum_us&&std::max<uint64_t>(1,us*r.scale/1000)==raw,"Original idle raw threshold has no exact integer-us request");return static_cast<uint32_t>(us);}
    PowerSavingResult execute(const PowerSavingRequest& request,bool record=true,std::optional<uint32_t> expected_raw={},bool history=false){PowerSavingResult result;if(!valid(request.key)){result.message="Invalid power-saving selection";return result;}MutationScope scope(mutation_key,request.key);const bool replace_adaptive=pending&&pending->key==request.key&&request.key.family==PowerSavingFamily::adaptive;if(pending&&!replace_adaptive){result.message="Resolve uncertain request before another edit";return result;}auto& owner=owners[id(request.key)];Receipt receipt;receipt.key=request.key;receipt.wanted=request.value;
        try{const auto before=fresh(request.key);publish(before);check(request.binding.empty()||request.binding==before.row.binding,"Staged power-saving binding changed");const auto current=row(request.key).value;check(!request.expected||current==request.expected,"Selected request changed since staging");check(!expected_raw||before.row.raw_first==*expected_raw,"Selected raw idle request changed since coupled qualification");check(before.row.writable,"Selected family is unavailable or not qualified");check(!owner.uncertain||replace_adaptive,"Request ownership is uncertain");const auto f=request.key.family;check(f==PowerSavingFamily::idle_threshold||request.value<=1,"Power-saving request is not Boolean");
            receipt.before=current;receipt.sample=before;if(f==PowerSavingFamily::individual&&!request.value&&!(before.row.reason_a&1)){owner={};publish(before);result.ok=true;result.message="Owned reason is already clear; no decrement issued";flags();return result;}if(f==PowerSavingFamily::idle_threshold){check(request.value&&request.value<=before.row.maximum_us,"Idle request exceeds current-adapter bounds");receipt.before=before.row.value;if(std::max<uint64_t>(1,uint64_t(request.value)*before.row.scale/1000)==before.row.raw_first){result.ok=true;result.message="Selected raw threshold already matches";return result;}}
            else if(f!=PowerSavingFamily::adaptive&&owner.owned==(request.value!=0)){if(owner.owned){check(owner.binding==before.row.binding,"Owned request binding changed");if(f==PowerSavingFamily::individual)check((before.row.reason_a&1)!=0,"Owned individual witness disappeared");else if(f==PowerSavingFamily::difr)check(owner.bank==0?((before.row.reason_a&1)&&!(before.row.reason_b&1)):((before.row.reason_b&1)&&!(before.row.reason_a&1)),"Owned DIFR bank witness disappeared");else for(auto n:owner.witnesses)check(n<before.controllers.size()&&before.controllers[n].supported&&(before.controllers[n].a&1),"Owned GR witness disappeared");}result.ok=true;result.message="This application's request already matches";return result;}
            if(f==PowerSavingFamily::individual&&request.value)check(!(before.row.reason_a&1),"Reason zero already has a foreign individual owner");
            if(f==PowerSavingFamily::difr){if(request.value)check(!((before.row.reason_a|before.row.reason_b)&1),"DIFR reason zero is already occupied");else check(owner.owned&&owner.bank>=0&&(owner.bank==0?((before.row.reason_a&1)&&!(before.row.reason_b&1)):((before.row.reason_b&1)&&!(before.row.reason_a&1))),"DIFR owned witness was replaced");}
            if(!request.value&&(f==PowerSavingFamily::individual||f==PowerSavingFamily::difr))check(!owner.release_attempted,"A release was already attempted; never decrement twice");
            if(history&&history_callback)try{history_callback();}catch(const std::exception& error){result.message=std::string("History capture refused: ")+error.what();return result;}catch(...){result.message="History capture refused";return result;}
            if(f==PowerSavingFamily::gr_elpg&&request.value)allocate(before);
            owner.uncertain=true;pending=receipt;
            try{bool& issued=pending->dispatched;if(f==PowerSavingFamily::gr_elpg){if(request.value){
                    // A confirmed allocation is not a feature request. Keep
                    // its orphan eligible for FREE on guaranteed nondispatch.
                    try{profiler_control(0xB0CC0301,{UINT32_MAX,0x40,UINT32_MAX},issued);profiler_requested=true;}
                    catch(...){profiler_requested=issued;throw;}
                }else{profiler_control(0xB0CC0302,{0xC0},issued);profiler_requested=false;}}
                else if(f==PowerSavingFamily::individual){owner.attempted=true;if(!request.value){check(!owner.release_attempted,"A release was already attempted; never decrement twice");owner.release_attempted=true;}set_attribute(before,3,0x37,request.value?0u:1u,issued);}
                else if(f==PowerSavingFamily::adaptive)set_attribute(before,4,10,request.value,issued);
                else if(f==PowerSavingFamily::idle_threshold)set_attribute(before,3,0x3A,request.value,issued);
                else{if(!request.value){check(!owner.release_attempted,"A DIFR release was already attempted");owner.release_attempted=true;}const Words payload{request.value?1u:2u};const auto response=call(0x20802801,payload,before.transport,&issued);check(response==payload,"DIFR acknowledgement changed request");}
                pending->dispatched=issued;
            }catch(...){if(!pending->dispatched){pending.reset();owner.uncertain=false;if(!request.value)owner.release_attempted=false;}throw;}
            const auto after=fresh(request.key);transition(before,after,request.value,owner);
            if(f==PowerSavingFamily::gr_elpg&&!request.value)free_profiler();
            if(f==PowerSavingFamily::adaptive)adaptive_requests[id(request.key)]={before.row.binding,request.value};else if(f!=PowerSavingFamily::idle_threshold){owner.owned=request.value!=0;owner.binding=before.row.binding;owner.attempted=owner.owned;if(request.value)owner.release_attempted=false;}
            owner.uncertain=false;pending.reset();if(record)undo_record=receipt;publish(after);if(f==PowerSavingFamily::adaptive)for(auto& r:data.rows)if(r.key.family==PowerSavingFamily::idle_threshold){r.stale=true;r.writable=false;r.message="Adaptive request may change idle thresholds; Read again";}
            result.ok=true;result.may_have_changed=true;result.message=f==PowerSavingFamily::adaptive?"Request acknowledged; aggregate state and idle thresholds are independent":"Selected request and preserved fields verified";
        }catch(const Error& e){result.message=e.what();if(profiler||allocation_uncertain)result.may_have_changed=true;if(pending){result.may_have_changed=true;owner.uncertain=true;try{const auto after=fresh(request.key);if(request.key.family==PowerSavingFamily::individual&&request.value&&(after.row.reason_a&1)){owner.owned=true;owner.binding=receipt.sample.row.binding;}publish(after);}catch(const Error& read_error){unavailable(request.key,read_error);}}else unavailable(request.key,e);}
        if(request.key.family==PowerSavingFamily::adaptive&&result.may_have_changed)for(auto& r:data.rows)if(r.key.family==PowerSavingFamily::idle_threshold){r.stale=true;r.writable=false;r.message="Adaptive request may change idle thresholds; Read again";}flags();data.message=result.message;return result;}
    PowerSavingResult recover_pending(){if(!pending)return {false,false,false,allocation_uncertain?"Profiler object outcome unknown; driver reset required":"No pending request"};const auto receipt=*pending;auto& owner=owners[id(receipt.key)];MutationScope scope(mutation_key,receipt.key);
        try{const auto current=sample(receipt.key);check(current.row.binding==receipt.sample.row.binding,"Recovery binding changed");const auto f=receipt.key.family;
            if(f==PowerSavingFamily::difr)fail("DIFR counted outcome is uncertain; no blind decrement or retry is safe");
            if(f==PowerSavingFamily::gr_elpg){check(profiler!=0&&!allocation_uncertain,"No confirmed profiler object; driver reset required");if(profiler_requested){bool issued=false;profiler_control(0xB0CC0302,{0xC0},issued);profiler_requested=false;}free_profiler();owner={};pending.reset();publish(sample(receipt.key));}
            else if(f==PowerSavingFamily::individual){if(!(current.row.reason_a&1)){owner={};pending.reset();publish(current);}else if(owner.release_attempted)fail("Release already attempted; shared count cannot be decremented twice");else{check(owner.owned,"No independent acquisition witness; release refused");pending.reset();owner.uncertain=false;return execute({receipt.key,0,current.row.binding,{}},false);}}
            else if(f==PowerSavingFamily::idle_threshold){const auto desired=std::max<uint64_t>(1,uint64_t(receipt.wanted)*receipt.sample.row.scale/1000);check(current.row.raw_first==desired||current.row.raw_first==receipt.sample.row.raw_first,"Selected idle request replaced; recovery refused");if(current.row.raw_first==receipt.sample.row.raw_first){pending.reset();owner.uncertain=false;publish(current);}else{check(receipt.before.has_value(),"Previous threshold unknown");pending.reset();owner.uncertain=false;return execute({receipt.key,*receipt.before,current.row.binding,{}},false);}}
            else{check(receipt.before.has_value(),"Adaptive previous explicit request is unknown; driver reset or a separately explicit request is required");pending.reset();owner.uncertain=false;return execute({receipt.key,*receipt.before,current.row.binding,{}},false);}
            flags();return {true,false,true,"Pending request resolved without replaying opaque state"};
        }catch(const Error& e){unavailable(receipt.key,e);flags();return {false,false,false,e.what()};}
    }
};
PowerSavingService::PowerSavingService(PowerSavingApi a,dc_gpu* g,const dc_gpu_identity& i):impl_(std::make_unique<Impl>(std::move(a),g,i)){}
PowerSavingService::~PowerSavingService()=default;
void PowerSavingService::set_history_callback(std::function<void()> callback){impl_->history_callback=std::move(callback);}
const PowerSavingData& PowerSavingService::state()const{return impl_->data;}
const PowerSavingData& PowerSavingService::read(uint32_t families){auto& p=*impl_;Lock lock(p.api.transport,p.gpu);if(!lock.held){p.data.message="GPU lock unavailable";return p.data;}for(auto& r:p.data.rows)if(families&(1u<<static_cast<uint32_t>(r.key.family)))try{p.publish(p.fresh(r.key));}catch(const Error& e){p.unavailable(r.key,e);}p.flags();return p.data;}
PowerSavingResult PowerSavingService::apply(const PowerSavingRequest& r){auto& p=*impl_;Lock lock(p.api.transport,p.gpu);if(!lock.held)return {false,false,false,"GPU lock unavailable"};const auto serial=p.mutation_serial;PowerSavingResult result;try{result=p.execute(r,true,{},!p.suppress_history);}catch(const std::exception& e){result.message=e.what();if(valid(r.key))p.unavailable(r.key,Error(A::transient,result.message));}if(p.mutation_serial!=serial){p.external.reset();result.may_have_changed=true;}p.flags();return result;}
PowerSavingPlan PowerSavingService::preview(const std::vector<PowerSavingRequest>& requests){auto& p=*impl_;PowerSavingPlan result;Lock lock(p.api.transport,p.gpu);
    try{check(lock.held,"GPU lock unavailable");result.state=p.prepare(requests);result.ok=true;result.message="All selected power-saving requests qualified without writes";}
    catch(const std::exception& error){result.message=error.what();}p.data.message=result.message;p.flags();return result;
}
PowerSavingResult PowerSavingService::apply_profile(const PowerSavingPlan& plan,const std::function<void(const PowerSavingReceipt&)>& journal){auto& p=*impl_;PowerSavingResult result;Lock lock(p.api.transport,p.gpu);PowerSavingReceipt receipt;
    const auto serial=p.mutation_serial;
    try{check(lock.held,"GPU lock unavailable");check(plan.ok&&plan.state,"Invalid power-saving plan");p.paired(plan.state->session,plan.state->target);
        for(const auto& epoch:plan.state->epochs)check(p.epochs[epoch.first]==epoch.second,"Selected request lifecycle changed since preflight");
        (void)p.prepare(plan.state->requests); // Whole selected freshness before allocation or SET.
        receipt.state=std::make_shared<PowerSavingReceipt::State>();receipt.state->session=p.session;receipt.state->target=plan.state->target;
        receipt.state->previous_undo=p.undo_record;receipt.state->previous_external=p.external;receipt.state->original_epochs=plan.state->epochs;
        const bool adaptive_change=std::any_of(plan.state->requests.begin(),plan.state->requests.end(),[](const auto& r){return r.key.family==PowerSavingFamily::adaptive&&r.expected!=r.value;});
        if(adaptive_change)for(const auto& before:plan.state->before)if(before.row.key.family==PowerSavingFamily::idle_threshold)receipt.state->coupled_idle[id(before.row.key)]=before;
        std::vector<uint32_t> expected;for(const auto& before:plan.state->before)expected.push_back(before.row.key.family==PowerSavingFamily::idle_threshold?before.row.raw_first:*before.row.value);
        for(size_t i=0;i<plan.state->requests.size();++i){
            for(size_t n=0;n<expected.size();++n){const auto& selected=plan.state->requests[n];if(selected.key.family==PowerSavingFamily::idle_threshold)p.selected_raw(selected.key,plan.state->before[n],expected[n]);else p.selected(selected,plan.state->before[n],expected[n]);}
            auto request=plan.state->requests[i];const bool idle=request.key.family==PowerSavingFamily::idle_threshold;
            const auto desired=idle?static_cast<uint32_t>(std::max<uint64_t>(1,uint64_t(request.value)*plan.state->before[i].row.scale/1000)):request.value;
            if(expected[i]==desired)continue;
            if(idle)request.expected=p.row(request.key).value; // authorized by exact raw guard, not a new initial capture
            const auto field_serial=p.mutation_serial;const auto journal_start=receipt.state->issued.size();
            const auto append=[&](size_t index){const auto& r=plan.state->requests[index];if(std::none_of(receipt.state->issued.begin(),receipt.state->issued.end(),[&](const auto& old){return old.key==r.key;})){
                Receipt record;record.key=r.key;record.before=plan.state->before[index].row.value;record.wanted=r.value;record.sample=plan.state->before[index];receipt.state->issued.push_back(std::move(record));}};
            p.before_mutation=[&]{append(i);if(request.key.family==PowerSavingFamily::adaptive)for(size_t n=0;n<plan.state->requests.size();++n)if(receipt.state->coupled_idle.count(id(plan.state->requests[n].key)))append(n);if(journal)journal(receipt);};
            PowerSavingResult action;std::exception_ptr raised;
            try{action=p.execute(request,false,idle?std::optional<uint32_t>(expected[i]):std::nullopt);}catch(...){raised=std::current_exception();}
            p.before_mutation={};
            if(p.mutation_serial==field_serial)receipt.state->issued.resize(journal_start);
            else{p.external=receipt.state;receipt.state->epochs[id(request.key)]=p.epochs[id(request.key)];plan.state->verification_epochs[id(request.key)]=p.epochs[id(request.key)];
                if(request.key.family==PowerSavingFamily::adaptive&&!receipt.state->coupled_idle.empty()){
                    p.issued_effects(*receipt.state);for(const auto& entry:receipt.state->coupled_idle)plan.state->verification_epochs[entry.first]=p.epochs[entry.first];
                    try{p.observe_effects(*receipt.state);for(size_t n=0;n<plan.state->requests.size();++n){const auto effect=receipt.state->effects.find(id(plan.state->requests[n].key));if(effect!=receipt.state->effects.end())expected[n]=effect->second.row.raw_first;}}
                    catch(...){if(!raised)raised=std::current_exception();}
                }
            }
            if(raised)std::rethrow_exception(raised);check(action.ok,action.message.c_str());expected[i]=desired;
        }
        for(size_t i=0;i<plan.state->requests.size();++i){check(p.epochs[id(plan.state->requests[i].key)]==plan.state->verification_epochs.at(id(plan.state->requests[i].key)),"Selected request lifecycle has been replaced");p.selected(plan.state->requests[i],plan.state->before[i],plan.state->requests[i].value);}
        result.ok=true;result.message="Selected request intent and owned witnesses verified; adaptive aggregate remains independent";
    }catch(const std::exception& error){p.before_mutation={};result.message=error.what();if(receipt.state&&!receipt.state->issued.empty()&&p.mutation_serial!=serial){receipt.state->incomplete=true;p.external=receipt.state;}
        if(p.pending)p.unavailable(p.pending->key,Error(A::transient,result.message));}
    result.may_have_changed=p.mutation_serial!=serial;p.data.message=result.message;p.flags();return result;
}
PowerSavingResult PowerSavingService::verify(const PowerSavingPlan& plan){auto& p=*impl_;PowerSavingResult result;Lock lock(p.api.transport,p.gpu);
    try{check(lock.held,"GPU lock unavailable");check(plan.ok&&plan.state,"Invalid power-saving plan");p.paired(plan.state->session,plan.state->target);
        check(!p.pending&&!p.allocation_uncertain,"Power-saving outcome is uncertain; explicit recovery required");
        for(size_t i=0;i<plan.state->requests.size();++i){check(p.epochs[id(plan.state->requests[i].key)]==plan.state->verification_epochs.at(id(plan.state->requests[i].key)),"Selected request lifecycle has been replaced");p.selected(plan.state->requests[i],plan.state->before[i],plan.state->requests[i].value);}
        result.ok=true;result.message="Entire selected power-saving intent verified, including initial no-ops";
    }catch(const std::exception& error){result.message=error.what();}p.data.message=result.message;p.flags();return result;
}
PowerSavingResult PowerSavingService::verify(const PowerSavingReceipt& receipt,bool original){auto& p=*impl_;PowerSavingResult result;Lock lock(p.api.transport,p.gpu);
    try{check(lock.held,"GPU lock unavailable");check(receipt.state&&!receipt.state->issued.empty(),"Power-saving receipt has no issued changes");p.paired(receipt.state->session,receipt.state->target);
        check(!p.pending&&!p.allocation_uncertain,"Power-saving outcome is uncertain; explicit recovery required");
        for(const auto& record:receipt.state->issued){check(p.epochs[id(record.key)]==receipt.state->epochs.at(id(record.key)),"Issued request lifecycle has been replaced");p.selected({record.key,record.wanted,record.sample.row.binding,{}},record.sample,original?*record.before:record.wanted);}
        result.ok=true;result.message="Issued power-saving request intent and witnesses verified";
    }catch(const std::exception& error){result.message=error.what();}p.data.message=result.message;p.flags();return result;
}
PowerSavingResult PowerSavingService::restore(const PowerSavingReceipt& receipt){auto& p=*impl_;PowerSavingResult result;Lock lock(p.api.transport,p.gpu);const auto serial=p.mutation_serial;
    try{check(lock.held,"GPU lock unavailable");check(receipt.state&&!receipt.state->issued.empty(),"Power-saving receipt has no issued changes");auto& state=*receipt.state;p.paired(state.session,state.target);
        check(!p.allocation_uncertain,"Profiler object outcome unknown; driver reset required");
        check(!p.pending||std::any_of(state.issued.begin(),state.issued.end(),[&](const auto& r){return r.key==p.pending->key;}),"Another selected recovery is pending");
        // Whole-candidate preflight prevents partial Undo on an existing conflict.
        for(const auto& record:state.issued){check(p.epochs[id(record.key)]==state.epochs.at(id(record.key)),"Issued request lifecycle has been replaced");const auto current=p.sample(record.key);
            check(current.row.binding==record.sample.row.binding&&current.row.supported&&current.row.writable,"Selected capability or binding changed");
            const bool pending=p.pending&&p.pending->key==record.key;const auto family=record.key.family;
            if(family==PowerSavingFamily::idle_threshold){check(current.row.scale==record.sample.row.scale,"Selected idle scale changed");const auto original=std::max<uint64_t>(1,uint64_t(*record.before)*current.row.scale/1000),wanted=std::max<uint64_t>(1,uint64_t(record.wanted)*current.row.scale/1000);
                check(current.row.raw_first==original||current.row.raw_first==wanted||p.idle_candidate(state,record,current),"Selected idle request replaced or adaptive effect unobserved; no restoration issued");}
            else{const auto value=p.row(record.key).value;check(value==record.before||value==record.wanted,"Selected explicit request replaced; no restoration issued");}
            if(pending&&family==PowerSavingFamily::difr)fail("DIFR counted outcome is uncertain; no blind decrement or retry is safe");
            if(pending&&family==PowerSavingFamily::individual){auto& o=p.owners[id(record.key)];if(o.release_attempted)check(!(current.row.reason_a&1),"Release already attempted; shared count cannot be decremented twice");
                else if(current.row.reason_a&1){p.transition(p.pending->sample,current,1,o);o.owned=true;o.binding=current.row.binding;}}
            if(!pending)p.witness(current);
        }
        const bool active=p.external==receipt.state;
        std::vector<size_t> order;
        if(p.pending&&p.pending->key.family!=PowerSavingFamily::adaptive)for(size_t i=0;i<state.issued.size();++i)if(state.issued[i].key==p.pending->key)order.push_back(i);
        for(size_t i=state.issued.size();i>0;--i)if(state.issued[i-1].key.family==PowerSavingFamily::adaptive)order.push_back(i-1);
        for(size_t i=state.issued.size();i>0;--i)if(state.issued[i-1].key.family!=PowerSavingFamily::adaptive&&state.issued[i-1].key.family!=PowerSavingFamily::idle_threshold)order.push_back(i-1);
        for(size_t i=state.issued.size();i>0;--i)if(state.issued[i-1].key.family==PowerSavingFamily::idle_threshold)order.push_back(i-1);
        for(auto index:order){const auto& record=state.issued[index];bool force_adaptive=false,effect_recorded=false;std::optional<Receipt> deferred_pending;const auto before_serial=p.mutation_serial;
            try{if(p.pending&&p.pending->key==record.key){
                    if(record.key.family==PowerSavingFamily::idle_threshold||record.key.family==PowerSavingFamily::adaptive){force_adaptive=record.key.family==PowerSavingFamily::adaptive;deferred_pending=p.pending;}
                    else{const auto recovered=p.recover_pending();check(recovered.ok,recovered.message.c_str());}}
                // A successful allocation can precede a non-dispatched control.
                if(record.key.family==PowerSavingFamily::gr_elpg&&p.profiler&&!p.profiler_requested&&!p.owners[id(record.key)].owned)p.free_profiler();
                if(p.mutation_serial!=before_serial)state.epochs[id(record.key)]=p.epochs[id(record.key)];
                check(p.epochs[id(record.key)]==state.epochs.at(id(record.key)),"Issued request lifecycle has been replaced during restoration");
                const auto current=p.sample(record.key);check(current.row.binding==record.sample.row.binding&&current.row.supported&&current.row.writable,"Restoration capability or binding changed");
                if(record.key.family==PowerSavingFamily::idle_threshold){check(current.row.scale==record.sample.row.scale,"Selected idle scale changed during restoration");
                    const auto desired=std::max<uint64_t>(1,uint64_t(record.wanted)*current.row.scale/1000);
                    check(current.row.raw_first==record.sample.row.raw_first||current.row.raw_first==desired||p.idle_candidate(state,record,current),"Selected idle request replaced during restoration");}
                else check(p.row(record.key).value==record.before||p.row(record.key).value==record.wanted,"Selected request replaced during restoration");
                p.witness(current);
                bool original=p.row(record.key).value==record.before;
                if(record.key.family==PowerSavingFamily::idle_threshold)original=current.row.scale==record.sample.row.scale&&current.row.raw_first==record.sample.row.raw_first;
                if(!original||force_adaptive){if(deferred_pending){p.pending.reset();p.owners[id(record.key)].uncertain=false;}const auto restored=p.execute({record.key,*record.before,current.row.binding,p.row(record.key).value},false,record.key.family==PowerSavingFamily::idle_threshold?std::optional<uint32_t>(current.row.raw_first):std::nullopt);
                    if(record.key.family==PowerSavingFamily::adaptive&&p.mutation_serial!=before_serial&&!state.coupled_idle.empty()){p.issued_effects(state);effect_recorded=true;p.observe_effects(state);}check(restored.ok,restored.message.c_str());}
                else if(deferred_pending){p.pending.reset();p.owners[id(record.key)].uncertain=false;p.publish(current);}
            }catch(...){if(deferred_pending&&p.mutation_serial==before_serial){p.pending=deferred_pending;p.owners[id(record.key)].uncertain=true;}if(p.mutation_serial!=before_serial){state.epochs[id(record.key)]=p.epochs[id(record.key)];if(record.key.family==PowerSavingFamily::adaptive&&!effect_recorded&&!state.coupled_idle.empty()){p.issued_effects(state);try{p.observe_effects(state);}catch(...){}}}throw;}
            state.epochs[id(record.key)]=p.epochs[id(record.key)];
        }
        for(const auto& record:state.issued)p.selected({record.key,record.wanted,record.sample.row.binding,{}},record.sample,*record.before);
        if(active){p.undo_record=state.previous_undo;p.external=state.previous_external;
            // Known reverse handover transfers the current owned lifecycle back
            // to the earlier matching typed receipt, never an observed Boolean.
            for(const auto& record:state.issued)for(auto prior=state.previous_external;prior;prior=prior->previous_external){const auto stamp=prior->epochs.find(id(record.key));const auto match=std::find_if(prior->issued.begin(),prior->issued.end(),[&](const auto& r){return r.key==record.key&&r.wanted==*record.before;});if(match!=prior->issued.end()&&stamp!=prior->epochs.end()&&stamp->second==state.original_epochs.at(id(record.key))){stamp->second=p.epochs[id(record.key)];break;}}}
        state.unobserved_effects.clear();state.incomplete=false;result.ok=result.rollback_verified=true;result.message="Issued requests restored with fresh verification; prior standalone Undo preserved";
    }catch(const std::exception& error){if(receipt.state&&receipt.state->session==p.session&&!receipt.state->issued.empty())receipt.state->incomplete=true;result.message=std::string(error.what())+"; typed catalog receipt retained";if(p.pending)p.unavailable(p.pending->key,Error(A::transient,result.message));}
    result.may_have_changed=p.mutation_serial!=serial;p.data.message=result.message;p.flags();return result;
}
PowerSavingResult PowerSavingService::restore_initial(const PowerSavingKey& key){auto& p=*impl_;Lock lock(p.api.transport,p.gpu);if(!lock.held)return {false,false,false,"GPU lock unavailable"};if(!valid(key))return {false,false,false,"Invalid selection"};const auto initial=p.row(key).initial;if(!initial)return {false,false,false,"Captured initial request is unknown"};PowerSavingRequest request{key,*initial,p.row(key).binding,{}};const auto serial=p.mutation_serial;auto result=p.execute(request,true,{},true);if(p.mutation_serial!=serial)p.external.reset();p.flags();return result;}
PowerSavingResult PowerSavingService::undo(){auto& p=*impl_;if(p.external)return restore({p.external});Lock lock(p.api.transport,p.gpu);if(!lock.held)return {false,false,false,"GPU lock unavailable"};if(!p.undo_record||!p.undo_record->before||p.pending)return {false,false,false,"No qualified previous request"};auto receipt=*p.undo_record;
    try{const auto current=p.fresh(receipt.key);p.publish(current);check(current.row.binding==receipt.sample.row.binding,"Undo binding changed");if(receipt.key.family==PowerSavingFamily::idle_threshold){const auto wanted=std::max<uint64_t>(1,uint64_t(receipt.wanted)*receipt.sample.row.scale/1000);check(current.row.raw_first==wanted,"Selected idle request was replaced; Undo refused");}}
    catch(const Error& e){return {false,false,false,e.what()};}const auto result=p.execute({receipt.key,*receipt.before,receipt.sample.row.binding,{}},false);if(result.ok)p.undo_record.reset();p.flags();return result;}
PowerSavingResult PowerSavingService::recover(){auto& p=*impl_;if(p.external&&(p.pending||p.allocation_uncertain||p.external->incomplete||!p.external->unobserved_effects.empty()))return restore({p.external});Lock lock(p.api.transport,p.gpu);if(!lock.held)return {false,false,false,"GPU lock unavailable"};return p.recover_pending();}
bool PowerSavingService::has_owned_requests()const{return impl_->data.owned_requests;}
PowerSavingResult PowerSavingService::release_owned(){auto& p=*impl_;struct Suppress {bool& value;bool prior;~Suppress(){value=prior;}} suppress{p.suppress_history,p.suppress_history};p.suppress_history=true;if(p.pending){const auto family=p.pending->key.family;if(family==PowerSavingFamily::idle_threshold||family==PowerSavingFamily::adaptive){if(!has_owned_requests())return {true,false,false,"Ordinary request recovery retained; no close-time Undo issued"};return {false,false,false,"Resolve ordinary request uncertainty before releasing counted ownership"};}PowerSavingResult resolved;{Lock lock(p.api.transport,p.gpu);if(!lock.held)return {false,false,false,"GPU lock unavailable"};resolved=p.recover_pending();}if(!resolved.ok)return resolved;}if(p.profiler&&!p.owners[id({PowerSavingFamily::gr_elpg,0})].owned){Lock lock(p.api.transport,p.gpu);if(!lock.held)return {false,false,false,"GPU lock unavailable"};try{p.free_profiler();p.owners[id({PowerSavingFamily::gr_elpg,0})]={};p.flags();}catch(const Error& e){return {false,true,false,e.what()};}}if(p.allocation_uncertain)return {false,false,false,"Profiler allocation ownership is unknown; driver reset required"};for(const auto& r:p.data.rows)if(r.owned){const auto result=apply({r.key,0,r.binding,{}});if(!result.ok)return result;}return {true,false,false,"Owned requests released"};}
ProfileJson capture_power_saving_profile(const PowerSavingData& data,const std::vector<PowerSavingKey>& keys){if(!text(data.identity.uuid)||!data.identity.uuid[0])throw std::runtime_error("Saved profiles require a GPU UUID");ProfileJson rows=ProfileJson::array();for(const auto& key:keys){check(valid(key),"Invalid saved selection");const auto it=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.key==key;});check(it!=data.rows.end()&&!it->stale&&it->availability==A::available&&it->value&&!it->uncertain,"Selected request is unknown, stale or uncertain");rows.push_back({{"family",static_cast<uint32_t>(key.family)},{"index",key.index},{"value",*it->value},{"binding",it->capability}});}check(!rows.empty(),"No selected requests");return {{"schema",1},{"scope","power_saving_requests"},{"uuid",data.identity.uuid},{"slot",data.identity.slot},{"requests",rows}};}
std::vector<PowerSavingRequest> stage_power_saving_profile(const ProfileJson& document,const PowerSavingData& data){(void)describe_power_saving_profile(document);if(document.at("scope")!="power_saving_requests"){const auto split=split_saving_profile(document);check(split.clock_gating.empty(),"Clock-gating requests need explicit cross-service splitting");std::vector<PowerSavingRequest> out;for(const auto& child:split.power_saving){const auto saved=parse_python_saving_profile(child);match_python_saving_target(saved,data.identity,data.target);PowerSavingKey key{PowerSavingFamily(saved.family),saved.index};const auto it=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& row){return row.key==key;});check(it!=data.rows.end()&&!it->stale&&it->availability==A::available&&it->value&&!it->uncertain&&!it->binding.empty(),"Read the selected current-owned request before staging");out.push_back({key,saved.value,it->binding,it->value});}return out;}check(document.is_object()&&document.size()==5&&document.at("schema")==1&&document.at("scope")=="power_saving_requests"&&text(data.identity.uuid)&&data.identity.uuid[0]&&document.at("uuid")==data.identity.uuid&&document.at("slot")==data.identity.slot,"Profile schema or selected adapter mismatch");const auto& rows=document.at("requests");check(rows.is_array()&&!rows.empty()&&rows.size()<=24,"Invalid saved request count");std::vector<PowerSavingRequest> result;std::vector<uint32_t> seen;for(const auto& saved:rows){check(saved.is_object()&&saved.size()==4&&saved.at("family").is_number_integer()&&saved.at("index").is_number_integer()&&saved.at("value").is_number_integer()&&saved.at("binding").is_string(),"Invalid typed saved request");const auto f=saved.at("family").get<uint64_t>(),n=saved.at("index").get<uint64_t>(),v=saved.at("value").get<uint64_t>();check(f<5&&n<32&&v<=UINT32_MAX,"Saved request out of range");PowerSavingKey key{static_cast<PowerSavingFamily>(f),static_cast<uint32_t>(n)};check(valid(key)&&std::find(seen.begin(),seen.end(),id(key))==seen.end(),"Invalid or duplicate selection");seen.push_back(id(key));const auto it=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.key==key;});check(it!=data.rows.end()&&!it->stale&&it->availability==A::available&&saved.at("binding")==it->capability&&!it->uncertain,"Saved request requires a current matching binding");check(key.family==PowerSavingFamily::idle_threshold?(v>0&&v<=it->maximum_us):v<=1,"Saved request outside current bounds");result.push_back({key,static_cast<uint32_t>(v),it->binding,it->value});}return result;}
}
