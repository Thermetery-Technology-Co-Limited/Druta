// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_tables_service.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <map>
#include <stdexcept>
namespace druta::ui {
namespace {
constexpr size_t info_size=0x98C,native_size=0x40C,public_size=0xAC8;
constexpr uint32_t info_get=0x2080B201,native_get=0x2080B213,public_set_id=0x87C55C8A;
constexpr const char* fields[]={"REL","ALT/OP","OV","VMIN","VCRIT low","VCRIT high"};
struct Failure:std::runtime_error {
    VoltagePolicyAvailability kind;
    Failure(const std::string& message,VoltagePolicyAvailability k=VoltagePolicyAvailability::invalid):std::runtime_error(message),kind(k){}
};
[[noreturn]]void fail(const std::string& message,VoltagePolicyAvailability kind=VoltagePolicyAvailability::invalid){throw Failure(message,kind);}
template<size_t N>bool text(const char(&s)[N]){return std::memchr(s,0,N)!=nullptr;}
void api_failure(const dc_error& error,const char* fallback){
    fail(text(error.message)&&error.message[0]?error.message:fallback,error.kind==DC_ERR_UNSUPPORTED?VoltagePolicyAvailability::unsupported:
        error.kind==DC_ERR_TRANSIENT||error.kind==DC_ERR_UNSTABLE?VoltagePolicyAvailability::transient:VoltagePolicyAvailability::invalid);
}
uint32_t word(const std::vector<uint8_t>& bytes,size_t at){uint32_t out=0;for(size_t i=0;i<4;++i)out|=uint32_t(bytes.at(at+i))<<(8*i);return out;}
int32_t signed_word(const std::vector<uint8_t>& bytes,size_t at){const uint32_t value=word(bytes,at);int32_t out;std::memcpy(&out,&value,4);return out;}
void put(std::vector<uint8_t>& bytes,size_t at,uint32_t value){for(size_t i=0;i<4;++i)bytes.at(at+i)=static_cast<uint8_t>(value>>(8*i));}
size_t public_at(uint32_t rail,uint32_t field){return 0x48+static_cast<size_t>(rail)*0x54+4+static_cast<size_t>(field)*4;}
size_t native_at(uint32_t rail,uint32_t field){return 12+static_cast<size_t>(rail)*32+4+static_cast<size_t>(field)*4;}
bool valid_key(const VoltageTableKey& key){return key.family==VoltageTableFamily::requests && key.rail<2 && key.field<6;}
std::string hex(const std::vector<uint8_t>& bytes){constexpr char digits[]="0123456789abcdef";std::string out;out.reserve(bytes.size()*2);
    for(uint8_t value:bytes){out.push_back(digits[value>>4]);out.push_back(digits[value&15]);}return out;}
bool same_identity(const dc_gpu_identity& a,const dc_gpu_identity& b){return a.size==sizeof(a)&&b.size==sizeof(b)&&text(a.slot)&&text(b.slot)&&text(a.uuid)&&text(b.uuid)
    &&text(a.pairing_error)&&text(b.pairing_error)&&a.slot[0]&&a.nvapi_selected&&a.nvapi_ok&&b.nvapi_selected&&b.nvapi_ok
    &&!a.pairing_error[0]&&!b.pairing_error[0]&&!std::strcmp(a.slot,b.slot)&&!std::strcmp(a.uuid,b.uuid)
    &&a.nvapi_handle==b.nvapi_handle&&a.nvapi_devid==b.nvapi_devid&&a.nvapi_subsys==b.nvapi_subsys
    &&a.nvml_selected==b.nvml_selected&&a.nvml_has_devid==b.nvml_has_devid&&a.nvml_devid==b.nvml_devid&&a.nvml_subsys==b.nvml_subsys;}
struct Guard {VoltagePolicyApi& api;dc_gpu* gpu;bool held;Guard(VoltagePolicyApi& a,dc_gpu* g):api(a),gpu(g),held(g&&a.lock&&a.unlock&&a.lock(g)==DC_OK){}~Guard(){if(held)api.unlock(gpu);}};
struct TableRecord {VoltageTableKey key;std::string binding;int32_t before=0,after=0;};
int32_t microvolts(double mv,bool positive){const double scaled=mv*1000;
    if(!std::isfinite(scaled)||std::abs(scaled-std::nearbyint(scaled))>0.000001||scaled<INT32_MIN||scaled>UINT32_MAX||(positive&&scaled<=0))fail("Rail policy units or current state are not understood");
    if(positive)return 0; // Absolute policy may exceed signed range; only qualification is needed.
    if(scaled>INT32_MAX)fail("Signed rail offset is outside int32");return static_cast<int32_t>(std::nearbyint(scaled));
}
} // namespace
bool operator==(const VoltageTableKey& a,const VoltageTableKey& b){return a.family==b.family&&a.rail==b.rail&&a.field==b.field;}
struct VoltageTablesPlan::State {
    std::shared_ptr<int> session;
    std::vector<TableRecord> selected;
};
struct VoltageTablesReceipt::State {
    std::shared_ptr<int> session;
    std::vector<TableRecord> issued,previous_undo;
    std::weak_ptr<State> previous_external;
};
struct VoltageTablesService::Impl {
    struct Snapshot {
        std::vector<uint8_t> info,native;
        std::array<std::vector<uint8_t>,2> public_rows;
        std::array<uint32_t,2> policy_type{},control_type{};
        std::array<std::string,2> issues;
        uint32_t active=0,present=0;int32_t boost=0;
        bool complete()const{return active && active==present;}
    };
    using Record=TableRecord;
    VoltageTablesApi api;dc_gpu* gpu;dc_gpu_identity target;std::optional<dc_target> bound_target;
    VoltagePolicyService gate;
    VoltageTablesData data;
    std::map<std::string,int32_t> initial;
    std::vector<Record> undo,recovery,previous_undo;
    std::shared_ptr<int> session=std::make_shared<int>(0);
    std::weak_ptr<VoltageTablesReceipt::State> external;
    std::weak_ptr<VoltageTablesReceipt::State> previous_external;
    Impl(VoltageTablesApi a,dc_gpu* g,const dc_gpu_identity& id):api(std::move(a)),gpu(g),target(id),gate(api.transport,g,id){data.identity=id;
        data.offsets_message="Read installed offsets to verify this adapter's scaffold receipt and current definitions.";}
    void bind(){
        const auto& a=api.transport;if(!a.identity||!a.bind||!a.architecture_supported)fail("Paired voltage table interfaces unavailable",VoltagePolicyAvailability::unsupported);
        dc_gpu_identity id{};id.size=sizeof(id);if(a.identity(gpu,&id)!=DC_OK)fail("Selected GPU identity unavailable",VoltagePolicyAvailability::transient);
        if(!same_identity(target,id))fail("Voltage table adapter identity or handle changed");
        dc_target value{};value.size=sizeof(value);dc_error error{};error.size=sizeof(error);
        if(a.bind(gpu,1,&value,&error)!=DC_OK)api_failure(error,"Complete paired target unavailable");
        if(value.size!=sizeof(value)||!text(value.slot)||!value.slot[0]||!value.handle||std::strcmp(value.slot,id.slot)||value.handle!=id.nvapi_handle
            ||value.device_id>65535||value.device_id!=id.nvapi_devid||value.subsystem_id!=id.nvapi_subsys)fail("Complete voltage table target disagrees with opened adapter");
        if(a.architecture_supported(value.architecture)!=1)fail("Modern voltage table architecture is unsupported",VoltagePolicyAvailability::unsupported);
        if(bound_target&&(value.architecture!=bound_target->architecture||value.device_id!=bound_target->device_id||value.subsystem_id!=bound_target->subsystem_id||value.handle!=bound_target->handle
            ||std::strcmp(value.slot,bound_target->slot)))fail("Sticky voltage table binding changed");bound_target=value;
    }
    std::vector<uint8_t> get(uint32_t command,size_t size,std::optional<uint32_t> mask={}){
        auto& a=api.transport;bind();if(!a.transport||!a.escape)fail("RM voltage table transport unavailable",VoltagePolicyAvailability::unsupported);
        dc_rm_transport transport{};transport.size=sizeof(transport);dc_error error{};error.size=sizeof(error);
        if(a.transport(gpu,&transport,&error)!=DC_OK)api_failure(error,"Voltage table transport unavailable");
        if(transport.size!=sizeof(transport)||transport.captured!=1||transport.header_count!=17||transport.header[0]!=0x4E564441||transport.header[1]!=0x10002
            ||transport.header[3]!=0x4E562A2A||transport.header[4]!=0x0500002B||uint64_t(transport.header[2])!=68+uint64_t(transport.header[15]))fail("Voltage table transport geometry changed");
        std::vector<uint32_t> guarded(16+(68+size)/4+16,0xA5A5A5A5);auto* packet=guarded.data()+16;
        std::copy(transport.header,transport.header+17,packet);std::fill(packet+17,packet+(68+size)/4,0);
        packet[2]=static_cast<uint32_t>(68+size);packet[14]=command;packet[15]=static_cast<uint32_t>(size);packet[16]=0;packet[5]=packet[11]=0;
        if(mask)packet[18]=*mask;
        std::array<uint32_t,16> envelope;std::copy(packet,packet+16,envelope.begin());bind();int32_t nt=0;
        const auto status=a.escape(gpu,reinterpret_cast<uint8_t*>(packet),68+size,&transport.fields,&nt,&error);bind();
        if(!std::all_of(guarded.begin(),guarded.begin()+16,[](uint32_t v){return v==0xA5A5A5A5;})||!std::all_of(guarded.end()-16,guarded.end(),[](uint32_t v){return v==0xA5A5A5A5;}))fail("Voltage table GET exceeded packet bounds");
        if(status!=DC_OK)api_failure(error,"Voltage table GET escape failed");
        for(size_t i=0;i<16;++i)if(i!=5&&i!=11&&packet[i]!=envelope[i])fail("Voltage table GET changed its envelope");
        if(packet[5]>1)fail("Voltage table context flag is malformed");
        if(nt||packet[11]!=1)fail("Voltage table GET transport failed",VoltagePolicyAvailability::transient);
        if(packet[16])fail("Voltage table GET rejected (RM "+std::to_string(packet[16])+")",packet[16]==0x1F||packet[16]==0x56?VoltagePolicyAvailability::unsupported:VoltagePolicyAvailability::transient);
        auto* start=reinterpret_cast<uint8_t*>(packet+17);std::vector<uint8_t> out(start,start+size);
        if(mask&&word(out,4)!=*mask)fail("Native CONTROL changed its requested inventory");return out;
    }
    Snapshot once(){
        bind();if(!api.rails_blocks||!api.rails_state||!api.rails_deltas||!api.boost)fail("Independent rail request getters unavailable",VoltagePolicyAvailability::unsupported);
        Snapshot s;s.info=get(info_get,info_size);s.active=word(s.info,4);if(!s.active)fail("Rail INFO contains no active inventory",VoltagePolicyAvailability::unsupported);
        dc_rails_block_set blocks{};blocks.size=sizeof(blocks);dc_rails_state_set state{};state.size=sizeof(state);dc_rails_delta_set deltas{};deltas.size=sizeof(deltas);
        if(api.rails_blocks(gpu,DC_RAILS_CONTROL,0x20AC8,&blocks)!=DC_OK||api.rails_state(gpu,&state)!=DC_OK||api.rails_deltas(gpu,&deltas)!=DC_OK
            ||blocks.size!=sizeof(blocks)||state.size!=sizeof(state)||deltas.size!=sizeof(deltas))fail("Independent rail request read failed",VoltagePolicyAvailability::transient);
        if(api.boost(gpu,&s.boost)!=DC_OK||s.boost<0||s.boost>100)fail("Independent voltage boost request unavailable",VoltagePolicyAvailability::transient);
        for(uint32_t rail=0;rail<2;++rail)if(s.active&(1u<<rail))try{
            if(!(blocks.present&(1u<<rail))||!(state.present&(1u<<rail))||!(deltas.present&(1u<<rail)))fail("Independent rail request or policy getter unavailable",VoltagePolicyAvailability::transient);
            std::vector<uint8_t> raw(blocks.blocks[rail],blocks.blocks[rail]+public_size);const size_t at=0x48+static_cast<size_t>(rail)*0x54,info_at=12+static_cast<size_t>(rail)*76;
            const auto type=word(raw,at);const uint32_t native_type=type==0?2u:type==2?4u:type==3?5u:0u;
            if(word(raw,0)!=0x20AC8||word(raw,4)!=(1u<<rail)||!native_type)fail("Exact V2 singleton rail CONTROL layout unavailable",VoltagePolicyAvailability::unsupported);
            const auto& row=state.rails[rail];const auto& delta=deltas.rails[rail];const uint32_t policy=s.info[info_at+0x33];
            if(policy<1||policy>8||row.type!=policy||delta.type<0||static_cast<uint32_t>(delta.type)!=type||s.info[info_at]!=native_type||raw[8]!=s.boost)fail("Independent native/public rail identities or boost disagree");
            for(double value:{row.reliability,row.alt_reliability,row.overvoltage,row.effective,row.vmin})microvolts(value,true);
            const double boost_uv=row.boost_mv*1000;
            if(!std::isfinite(boost_uv)||boost_uv<0||boost_uv>UINT32_MAX||std::abs(boost_uv-std::nearbyint(boost_uv))>0.000001)fail("Independent reported boost metadata has unknown units");
            const double offsets[]={delta.reliability,delta.alt_reliability,delta.overvoltage,delta.vmin};
            for(uint32_t field=0;field<4;++field)if(microvolts(offsets[field],false)!=signed_word(raw,public_at(rail,field)))fail("Public rail requests disagree with independent offsets",VoltagePolicyAvailability::transient);
            s.public_rows[rail]=std::move(raw);s.policy_type[rail]=policy;s.control_type[rail]=type;s.present|=1u<<rail;
        }catch(const Failure& error){s.issues[rail]=error.what();}
        if(!s.present)fail("No independently paired rail requests",VoltagePolicyAvailability::transient);
        s.native=get(native_get,native_size,s.present);
        if(s.native[8]!=s.boost)fail("Native boost request changed during capture",VoltagePolicyAvailability::transient);
        for(uint32_t rail=0;rail<2;++rail)if(s.present&(1u<<rail)){
            const uint32_t expected=s.control_type[rail]==0?2:s.control_type[rail]==2?4:5;
            if(s.native[12+static_cast<size_t>(rail)*32]!=expected)fail("Native CONTROL type disagrees with paired rail");
            for(uint32_t field=0;field<6;++field)if(signed_word(s.native,native_at(rail,field))!=signed_word(s.public_rows[rail],public_at(rail,field)))fail("Public/native six-word requests changed during capture",VoltagePolicyAvailability::transient);
        }
        bind();return s;
    }
    static bool same(const Snapshot& a,const Snapshot& b){return a.info==b.info&&a.native==b.native&&a.public_rows==b.public_rows&&a.policy_type==b.policy_type&&a.control_type==b.control_type&&a.boost==b.boost;}
    Snapshot capture(){for(int attempt=0;attempt<2;++attempt)try{const auto a=once(),b=once();if(!same(a,b))fail("Rail requests changed between fresh captures",VoltagePolicyAvailability::transient);return b;
        }catch(const Failure& error){if(error.kind!=VoltagePolicyAvailability::transient||attempt){stale(error.kind,error.what());throw;}if(api.rails_refresh)api.rails_refresh(gpu);if(api.transport.reset_transport)api.transport.reset_transport(gpu,DC_TRANSPORT_POWER);}
        fail("Bounded voltage table capture failed",VoltagePolicyAvailability::transient);}
    Snapshot sample(){try{return capture();}catch(const Failure&){throw;}
        catch(const std::exception& error){stale(VoltagePolicyAvailability::transient,error.what());throw;}}
    std::string binding(const Snapshot& s,uint32_t rail)const{return "rail-request:"+std::to_string(rail)+":"+std::to_string(s.policy_type[rail])+":"+std::to_string(s.control_type[rail])+":"+hex(s.info);}
    void flags(){data.undo_available=!undo.empty();data.recovery_pending=!recovery.empty();
        const bool writer=data.requests_complete&&data.gate_enabled==true&&recovery.empty()&&api.public_set&&api.interface_present&&api.interface_present(gpu,public_set_id)!=0;
        for(auto& row:data.rows)if(row.key.family==VoltageTableFamily::requests)row.writable=writer&&!row.stale&&row.availability==VoltagePolicyAvailability::available;
        ++data.revision;}
    void stale(VoltagePolicyAvailability kind,const std::string& message){data.requests_complete=false;data.gate_enabled.reset();data.requests_message=message;
        for(auto& row:data.rows)if(row.key.family==VoltageTableFamily::requests){row.stale=row.value.has_value();row.writable=false;row.availability=kind;row.message=message;}flags();}
    void stale_offsets(VoltagePolicyAvailability kind,const std::string& message){data.offsets_recognized=false;data.offsets_message=message;
        for(auto& row:data.rows)if(row.key.family==VoltageTableFamily::offsets){row.stale=row.value.has_value();row.writable=false;row.availability=kind;row.message=message;}}
    void read_offsets(){
        if(!api.scaffold_read)fail("Installed scaffold receipt reader unavailable",VoltagePolicyAvailability::unsupported);
        bind();const auto captured=api.scaffold_read(gpu);bind();const auto& current=*bound_target;
        if(captured.target.size!=sizeof(captured.target)||!text(captured.target.slot)||std::strcmp(captured.target.slot,current.slot)
            ||captured.target.handle!=current.handle||captured.target.architecture!=current.architecture||captured.target.device_id!=current.device_id
            ||captured.target.subsystem_id!=current.subsystem_id||captured.receipt_binding.empty()||captured.image_sha256.empty()
            ||captured.heads.empty()||captured.heads.size()>8)fail("Installed scaffold evidence has an invalid target or manifest");
        std::vector<VoltageTableRow> fresh;
        for(const auto& head:captured.heads){const auto field=static_cast<uint32_t>(head.role);
            if(head.rail>=2||field>=4||head.output!=2||head.index>=8192||head.next>=8192||head.variable>=255
                ||head.value_uv<INT32_MIN||head.value_uv>INT32_MAX||static_cast<int64_t>(static_cast<float>(head.value_uv))!=head.value_uv)
                fail("Installed scaffold head is outside the signed selected-definition contract");
            const VoltageTableKey key{VoltageTableFamily::offsets,head.rail,field};
            if(std::any_of(fresh.begin(),fresh.end(),[&](const auto& row){return row.key==key;}))fail("Installed scaffold contains duplicate policy heads");
            VoltageTableRow row;row.key=key;row.label=(head.rail?"MSVDD ":"NVVDD ")+std::string(fields[field])+" offset";
            row.value=static_cast<int32_t>(head.value_uv);row.binding=captured.receipt_binding+":"+captured.image_sha256+":"+std::to_string(head.index);
            row.availability=VoltagePolicyAvailability::available;row.writable=false;
            row.message="Installed definition recognized; selected Apply, Undo and profiles are pending independent policy/request verification.";
            fresh.push_back(std::move(row));
        }
        data.rows.erase(std::remove_if(data.rows.begin(),data.rows.end(),[](const auto& row){return row.key.family==VoltageTableFamily::offsets;}),data.rows.end());
        data.rows.insert(data.rows.end(),fresh.begin(),fresh.end());data.offsets_recognized=true;
        data.offsets_message=captured.message;data.target=bound_target;
    }
    void publish(const Snapshot& s){data.target=bound_target;
        const auto& g=gate.read(voltage_admission);data.gate_enabled=g.rows[0].availability==VoltagePolicyAvailability::available?g.rows[0].current:std::optional<bool>{};
        data.boost_percent=s.boost;data.requests_complete=s.complete();
        for(uint32_t rail=0;rail<2;++rail)for(uint32_t field=0;field<6;++field){VoltageTableKey key{VoltageTableFamily::requests,rail,field};
            auto found=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& row){return row.key==key;});
            if(!(s.active&(1u<<rail))&&found==data.rows.end())continue;
            if(found==data.rows.end()){data.rows.push_back({});found=data.rows.end()-1;found->key=key;}
            auto& row=*found;row.label="Rail "+std::to_string(rail)+" "+fields[field];
            if(!(s.present&(1u<<rail))){row.stale=row.value.has_value();row.writable=false;row.availability=VoltagePolicyAvailability::transient;row.message=s.issues[rail].empty()?"Rail absent from current inventory":s.issues[rail];continue;}
            row.value=signed_word(s.public_rows[rail],public_at(rail,field));row.policy_type=s.policy_type[rail];row.control_type=s.control_type[rail];row.binding=binding(s,rail);
            const auto inserted=initial.emplace(row.binding+":field:"+std::to_string(field),*row.value);row.initial=inserted.first->second;
            row.stale=false;row.availability=VoltagePolicyAvailability::available;
            row.writable=s.complete()&&data.gate_enabled==true&&api.public_set&&api.interface_present&&api.interface_present(gpu,public_set_id)!=0&&recovery.empty();
            row.message=!s.complete()?"Incomplete active inventory: requests readable; complete preservation required for SET":data.gate_enabled!=true?"Admission gate disabled or unreadable; enable separately":!api.public_set?"Public six-word SET interface unavailable":field>=4?"Stored VCRIT request; effective threshold unverified":"Stored request, not measured voltage";
        }
        data.requests_message=s.complete()?"Complete current request inventory captured":"Partial request inventory captured; writes disabled";flags();
    }
    bool write(const Snapshot& before,const VoltageTableKey& key,int32_t value,Snapshot& after,bool& issued,std::string& message,const std::function<void()>& launch={}){
        issued=false;if(!before.complete()||!api.public_set||!api.interface_present||api.interface_present(gpu,public_set_id)==0){message="Complete public rail setter unavailable";return false;}
        const auto& g=gate.read(voltage_admission);if(g.rows[0].availability!=VoltagePolicyAvailability::available||g.rows[0].current!=true){message="Admission gate disabled or unavailable; no SET issued";return false;}
        auto desired=before;put(desired.public_rows[key.rail],public_at(key.rail,key.field),static_cast<uint32_t>(value));put(desired.native,native_at(key.rail,key.field),static_cast<uint32_t>(value));
        std::vector<uint8_t> canonical(public_size,0);put(canonical,0,0x20AC8);put(canonical,4,before.present);canonical[8]=static_cast<uint8_t>(before.boost);
        for(uint32_t rail=0;rail<2;++rail)if(before.present&(1u<<rail)){const auto at=0x48+static_cast<size_t>(rail)*0x54;put(canonical,at,before.control_type[rail]);
            for(uint32_t field=0;field<6;++field)put(canonical,public_at(rail,field),static_cast<uint32_t>(signed_word(desired.public_rows[rail],public_at(rail,field))));}
        std::vector<uint8_t> storage(public_size+128,0xA5);std::copy(canonical.begin(),canonical.end(),storage.begin()+64);
        try{bind();dc_error error{};error.size=sizeof(error);int32_t nvapi=0;if(launch)launch();issued=true;const auto status=api.public_set(gpu,storage.data()+64,public_size,nvapi,error);
            if(status==DC_E_UNAVAILABLE)issued=false;
            bool packet=std::all_of(storage.begin(),storage.begin()+64,[](uint8_t v){return v==0xA5;})&&std::all_of(storage.end()-64,storage.end(),[](uint8_t v){return v==0xA5;})
                &&std::equal(canonical.begin(),canonical.end(),storage.begin()+64);
            bind();after=capture();publish(after);
            if(status==DC_OK&&nvapi==0&&packet&&same(after,desired)){message="Selected request and preserved complete packets verified";return true;}
            message=!packet?"Rail SET changed caller packet or bounds":status!=DC_OK||nvapi?"Public rail SET failed; request may have changed":"Selected request or preserved metadata readback differs";
        }catch(const Failure& error){message=error.what();stale(error.kind,message);}
        catch(const std::exception& error){message=error.what();stale(VoltagePolicyAvailability::transient,message);}
        return false;
    }
    std::vector<Record> qualify(const Snapshot& before,const std::vector<VoltageTableRequest>& requests,std::optional<bool> projected){
        if(!recovery.empty())fail("Recover pending selected requests before another Apply");
        if(!before.complete()||!api.public_set||!api.interface_present||api.interface_present(gpu,public_set_id)!=1)fail("Complete active inventory and public SET export required");
        const auto& g=gate.read(voltage_admission);
        if(g.rows[0].availability!=VoltagePolicyAvailability::available||!g.rows[0].current)fail("Current admission capability unavailable");
        if(g.rows[0].current!=true&&projected!=true)fail("Admission gate disabled; no SET issued");
        if(requests.empty()||requests.size()>12)fail("Select one or more understood request fields");
        std::vector<Record> selected;
        for(const auto& request:requests){
            if(!valid_key(request.key)||request.value<INT32_MIN||request.value>INT32_MAX||std::any_of(selected.begin(),selected.end(),[&](const auto& r){return r.key==request.key;}))fail("Invalid or duplicate signed microvolt request field");
            if(!(before.present&(1u<<request.key.rail))||request.binding.empty()||request.binding!=binding(before,request.key.rail))fail("Selected rail binding changed; read requests again");
            const auto value=signed_word(before.public_rows[request.key.rail],public_at(request.key.rail,request.key.field));
            if(request.expected&&value!=*request.expected)fail("Selected request changed since the displayed draft");
            selected.push_back({request.key,request.binding,value,static_cast<int32_t>(request.value)});
        }
        return selected;
    }
    void exact(const Snapshot& current,const std::vector<Record>& records,bool original){
        if(!current.complete())fail("Complete current inventory required for selected verification");
        for(const auto& record:records)if(!(current.present&(1u<<record.key.rail))||binding(current,record.key.rail)!=record.binding
            ||signed_word(current.public_rows[record.key.rail],public_at(record.key.rail,record.key.field))!=(original?record.before:record.after))fail("Selected request or binding differs from transaction readback");
    }
    bool repair(std::vector<Record>& records,std::string& message){
        // Reject any already-visible conflict before restoring even one peer.
        // Per-step checks below still guard changes after this whole preflight.
        try{const auto before=sample();publish(before);
            if(!before.complete())fail("Complete active inventory required before selected restoration");
            for(const auto& record:records){
                if(!(before.present&(1u<<record.key.rail))||binding(before,record.key.rail)!=record.binding)fail("Original rail binding changed; selected recovery retained");
                const auto actual=signed_word(before.public_rows[record.key.rail],public_at(record.key.rail,record.key.field));
                if(actual!=record.before&&actual!=record.after)fail("Selected request replaced externally; no restoration issued");
            }
        }catch(const std::exception& error){message=error.what();return false;}
        for(size_t index=records.size();index>0;--index)try{const auto record=records[index-1];const auto now=sample();publish(now);
            if(!now.complete()||!(now.present&(1u<<record.key.rail))||binding(now,record.key.rail)!=record.binding)fail("Original rail binding changed; selected recovery retained");
            const auto actual=signed_word(now.public_rows[record.key.rail],public_at(record.key.rail,record.key.field));
            if(actual!=record.before&&actual!=record.after)fail("Selected request replaced externally; no restoration issued");
            if(actual!=record.before){Snapshot after;bool issued=false;std::string why;
                if(!write(now,record.key,record.before,after,issued,why)){
                    // An accepted-but-failed restoration is verified on the
                    // next explicit recovery; never retry the SET here.
                    message=why;return false;
                }}else publish(now);
        }catch(const std::exception& error){message=error.what();return false;}
        try{const auto final=sample();for(const auto& record:records)if(binding(final,record.key.rail)!=record.binding
            ||signed_word(final.public_rows[record.key.rail],public_at(record.key.rail,record.key.field))!=record.before)fail("Final selected restoration readback differs; all selected Undo retained");
            records.clear();publish(final);return true;
        }catch(const std::exception& error){message=error.what();return false;}
    }
};
VoltageTablesService::VoltageTablesService(VoltageTablesApi api,dc_gpu* gpu,const dc_gpu_identity& target):impl_(std::make_unique<Impl>(std::move(api),gpu,target)){}
VoltageTablesService::~VoltageTablesService()=default;
const VoltageTablesData& VoltageTablesService::state()const{return impl_->data;}
const VoltageTablesData& VoltageTablesService::read(uint32_t families){auto& p=*impl_;Guard guard(p.api.transport,p.gpu);
    if(!families||(families&~3u)){p.data.message="Select understood voltage families";p.flags();return p.data;}
    if(!guard.held){if(families&1)p.stale(VoltagePolicyAvailability::transient,"GPU lock unavailable");if(families&2)p.stale_offsets(VoltagePolicyAvailability::transient,"GPU lock unavailable");p.data.message="GPU lock unavailable";return p.data;}
    if(families&1)try{p.publish(p.capture());}catch(const Failure& error){p.stale(error.kind,error.what());}catch(const std::exception& error){p.stale(VoltagePolicyAvailability::transient,error.what());}
    if(families&2)try{p.read_offsets();}
        catch(const VoltageEquationEvidenceError& error){p.stale_offsets(error.kind,error.what());}
        catch(const Failure& error){p.stale_offsets(error.kind,error.what());}
        catch(const std::exception& error){p.stale_offsets(VoltagePolicyAvailability::invalid,error.what());}
    p.data.message=families==3?p.data.requests_message+"; "+p.data.offsets_message:(families&1)?p.data.requests_message:p.data.offsets_message;p.flags();return p.data;
}
VoltageTablesPlan VoltageTablesService::preview(const std::vector<VoltageTableRequest>& requests,std::optional<bool> projected){auto& p=*impl_;VoltageTablesPlan plan;Guard guard(p.api.transport,p.gpu);
    if(!guard.held){plan.message="GPU lock unavailable";return plan;}
    try{const auto before=p.sample();p.publish(before);auto state=std::make_shared<VoltageTablesPlan::State>();state->session=p.session;
        state->selected=p.qualify(before,requests,projected);plan.state=std::move(state);plan.ok=true;plan.message="All selected request fields qualified without writes";
    }catch(const std::exception& error){plan.message=error.what();}return plan;
}
VoltageTablesResult VoltageTablesService::apply_profile(const VoltageTablesPlan& plan,const std::function<void(const VoltageTablesReceipt&)>& before_write){auto& p=*impl_;VoltageTablesResult result;Guard guard(p.api.transport,p.gpu);
    if(!guard.held){result.message="GPU lock unavailable";return result;}
    VoltageTablesReceipt receipt;
    try{if(!plan.ok||!plan.state||plan.state->session!=p.session)fail("Voltage table plan belongs to another service session");
        std::vector<VoltageTableRequest> requests;for(const auto& r:plan.state->selected)requests.push_back({r.key,r.after,r.binding,r.before});
        const auto before=p.sample();p.publish(before);(void)p.qualify(before,requests,{});
        receipt.state=std::make_shared<VoltageTablesReceipt::State>();receipt.state->session=p.session;receipt.state->previous_undo=p.undo;receipt.state->previous_external=p.external;
        std::vector<Impl::Record> expected=plan.state->selected;
        for(const auto& record:plan.state->selected){if(record.before==record.after)continue;
            const auto fresh=p.sample();p.publish(fresh);p.exact(fresh,expected,true);
            Impl::Snapshot after;bool issued=false;std::string why;bool journaled=false;
            const bool ok=p.write(fresh,record.key,record.after,after,issued,why,[&]{
                receipt.state->issued.push_back(record);journaled=true;
                if(before_write)before_write(receipt);
            });
            if(!issued&&journaled)receipt.state->issued.pop_back();
            if(issued){result.may_have_changed=true;p.external=receipt.state;p.previous_undo=receipt.state->previous_undo;p.recovery=receipt.state->issued;}
            if(!ok)fail(why);
            for(auto& r:expected)if(r.key==record.key)r.before=r.after;
        }
        const auto final=p.sample();p.publish(final);p.exact(final,plan.state->selected,false);
        if(!receipt.state->issued.empty()){p.undo=receipt.state->issued;p.recovery.clear();p.previous_undo.clear();}
        result.ok=true;result.message=receipt.state->issued.empty()?"Selected requests already match; no SET issued":"Selected catalog requests verified; typed receipt retained";
    }catch(const std::exception& error){result.message=error.what();}
    p.data.message=result.message;p.flags();return result;
}
VoltageTablesResult VoltageTablesService::verify(const VoltageTablesPlan& plan){auto& p=*impl_;VoltageTablesResult result;Guard guard(p.api.transport,p.gpu);
    if(!guard.held){result.message="GPU lock unavailable";return result;}
    try{if(!plan.ok||!plan.state||plan.state->session!=p.session)fail("Voltage table plan belongs to another service session");
        const auto now=p.sample();p.publish(now);p.exact(now,plan.state->selected,false);result.ok=true;result.message="All selected desired requests verified";
    }catch(const std::exception& error){result.message=error.what();}return result;
}
VoltageTablesResult VoltageTablesService::verify(const VoltageTablesReceipt& receipt,bool original){auto& p=*impl_;VoltageTablesResult result;Guard guard(p.api.transport,p.gpu);
    if(!guard.held){result.message="GPU lock unavailable";return result;}
    try{if(!receipt.state||receipt.state->session!=p.session||receipt.state->issued.empty())fail("Voltage table receipt belongs to another service session or has no issued requests");
        const auto now=p.sample();p.publish(now);p.exact(now,receipt.state->issued,original);result.ok=true;result.message="Issued selected requests verified";
    }catch(const std::exception& error){result.message=error.what();}return result;
}
VoltageTablesResult VoltageTablesService::restore(const VoltageTablesReceipt& receipt){auto& p=*impl_;VoltageTablesResult result;Guard guard(p.api.transport,p.gpu);
    if(!guard.held){result.message="GPU lock unavailable";return result;}
    if(!receipt.state||receipt.state->session!=p.session||receipt.state->issued.empty()){result.message="Voltage table receipt belongs to another service session or has no issued requests";return result;}
    const bool active=p.external.lock()==receipt.state;
    if(!p.recovery.empty()&&!active){result.message="Resolve another selected recovery before restoring this receipt";return result;}
    auto records=receipt.state->issued;std::string why;result.may_have_changed=true;result.ok=p.repair(records,why);result.rollback_verified=result.ok;
    if(active){if(result.ok){p.undo=receipt.state->previous_undo;p.recovery.clear();p.previous_undo.clear();p.external=receipt.state->previous_external;}
        else{p.recovery=receipt.state->issued;p.previous_undo=receipt.state->previous_undo;}}
    result.message=result.ok?"Issued selected requests restored; earlier Undo preserved":"Selected catalog restoration pending: "+why;
    p.data.message=result.message;p.flags();return result;
}
VoltageTablesResult VoltageTablesService::apply(const VoltageTableRequest& request){return apply(std::vector<VoltageTableRequest>{request});}
VoltageTablesResult VoltageTablesService::apply(const std::vector<VoltageTableRequest>& requests){auto& p=*impl_;VoltageTablesResult result;Guard guard(p.api.transport,p.gpu);
    if(!guard.held){result.message="GPU lock unavailable";return result;}if(!p.recovery.empty()){result.message="Recover pending selected requests before another Apply";return result;}
    if(requests.empty()||requests.size()>12){result.message="Select one or more understood request fields";return result;}
    std::vector<Impl::Record> changed;bool started=false;
    try{const auto before=p.capture();p.publish(before);if(!before.complete())fail("Complete active rail preservation required for SET");
        std::vector<VoltageTableKey> seen;
        for(const auto& request:requests){if(!valid_key(request.key)||request.value<INT32_MIN||request.value>INT32_MAX||std::find(seen.begin(),seen.end(),request.key)!=seen.end())fail("Invalid or duplicate signed microvolt request field");seen.push_back(request.key);
            if(!(before.present&(1u<<request.key.rail))||request.binding.empty()||request.binding!=p.binding(before,request.key.rail))fail("Selected rail binding changed; read requests again");
            const auto value=signed_word(before.public_rows[request.key.rail],public_at(request.key.rail,request.key.field));if(request.expected&&value!=*request.expected)fail("Selected request changed since the displayed draft");
            if(value!=request.value)changed.push_back({request.key,request.binding,value,static_cast<int32_t>(request.value)});
        }
        if(changed.empty()){result.ok=true;result.message="Selected requests already match; no SET issued";return result;}
        p.previous_external=p.external;p.previous_undo=p.undo;p.recovery.clear();started=true;
        for(const auto& record:changed){const auto fresh=p.capture();if(p.binding(fresh,record.key.rail)!=record.binding||signed_word(fresh.public_rows[record.key.rail],public_at(record.key.rail,record.key.field))!=record.before)fail("Selected request changed after whole-table preflight");
            p.recovery.push_back(record);Impl::Snapshot after;bool issued=false;std::string message;
            if(!p.write(fresh,record.key,record.after,after,issued,message,[&]{p.external.reset();})){if(!issued)p.recovery.pop_back();result.may_have_changed|=issued;fail(message,VoltagePolicyAvailability::transient);}result.may_have_changed=true;
        }
        const auto final=p.capture();for(const auto& record:changed)if(p.binding(final,record.key.rail)!=record.binding||signed_word(final.public_rows[record.key.rail],public_at(record.key.rail,record.key.field))!=record.after)fail("Final selected requests differ after another field changed");
        p.undo=changed;p.recovery.clear();p.previous_undo.clear();p.previous_external.reset();p.publish(final);result.ok=true;result.message="Selected signed request fields read back exactly; Undo available";
    }catch(const std::exception& error){result.message=error.what();if(!p.recovery.empty()){std::string why;result.rollback_verified=p.repair(p.recovery,why);
            if(result.rollback_verified){p.undo=p.previous_undo;p.previous_undo.clear();p.external=p.previous_external;p.previous_external.reset();result.message+="; prior selected fields restored and verified";}else result.message+="; selected recovery pending: "+why;
        }else if(started&&!result.may_have_changed){p.external=p.previous_external;p.previous_external.reset();}}p.data.message=result.message;p.flags();return result;
}
VoltageTablesResult VoltageTablesService::recover(){auto& p=*impl_;if(!p.recovery.empty())if(auto receipt=p.external.lock())return restore({receipt});VoltageTablesResult result;Guard guard(p.api.transport,p.gpu);if(!guard.held){result.message="GPU lock unavailable";return result;}
    if(p.recovery.empty()){result.ok=true;result.message="No selected request recovery pending";return result;}
    result.may_have_changed=true;std::string why;result.ok=p.repair(p.recovery,why);result.rollback_verified=result.ok;
    if(result.ok){p.undo=p.previous_undo;p.previous_undo.clear();p.external=p.previous_external;p.previous_external.reset();result.message="Prior selected requests restored and verified";}else result.message="Selected recovery pending: "+why;
    p.data.message=result.message;p.flags();return result;
}
VoltageTablesResult VoltageTablesService::undo(){auto& p=*impl_;if(!p.recovery.empty())return recover();if(auto receipt=p.external.lock())return restore({receipt});VoltageTablesResult result;Guard guard(p.api.transport,p.gpu);
    if(!guard.held){result.message="GPU lock unavailable";return result;}if(p.undo.empty()){result.message="No voltage request Undo";return result;}
    p.recovery=p.undo;p.previous_undo.clear();p.undo.clear();result.may_have_changed=true;std::string why;result.ok=p.repair(p.recovery,why);
    result.message=result.ok?"Prior selected requests restored; current unselected fields preserved":"Selected Undo recovery pending: "+why;p.data.message=result.message;p.flags();return result;
}
VoltageTablesResult VoltageTablesService::restore_initial(const VoltageTableKey& key){auto& p=*impl_;const auto found=std::find_if(p.data.rows.begin(),p.data.rows.end(),[&](const auto& row){return row.key==key;});
    if(found==p.data.rows.end()||!found->initial)return {false,false,false,"Read a qualified captured initial request first"};return apply({key,*found->initial,found->binding,found->value});
}
ProfileJson capture_voltage_tables_profile(const VoltageTablesData& data,const std::vector<VoltageTableKey>& selection){
    if(!text(data.identity.uuid)||!text(data.identity.slot)||!data.identity.uuid[0]||!data.requests_complete||selection.empty())throw std::runtime_error("Qualified complete inventory, target and explicit fields required");
    ProfileJson rows=ProfileJson::array();std::vector<VoltageTableKey> seen;
    for(const auto& key:selection){const auto row=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.key==key;});
        if(!valid_key(key)||std::find(seen.begin(),seen.end(),key)!=seen.end()||row==data.rows.end()||!row->value||row->stale||row->availability!=VoltagePolicyAvailability::available)throw std::runtime_error("Selected current request unavailable");seen.push_back(key);
        rows.push_back({{"rail",key.rail},{"field",key.field},{"policy_type",row->policy_type},{"control_type",row->control_type},{"value_uv",*row->value}});
    }
    return {{"format","druta-native-voltage-requests"},{"version",1},{"target",{{"uuid",data.identity.uuid},{"slot",data.identity.slot}}},{"fields",rows}};
}

namespace {
void table_keys(const ProfileJson& j,std::initializer_list<const char*> required,std::initializer_list<const char*> optional={}){if(!j.is_object())fail("Voltage request profile requires an object");for(auto k:required)if(!j.contains(k))fail("Voltage request profile field missing");for(auto it=j.begin();it!=j.end();++it)if(std::find(required.begin(),required.end(),it.key())==required.end()&&std::find(optional.begin(),optional.end(),it.key())==optional.end())fail("Voltage request profile has unrelated fields");}
int64_t table_integer(const ProfileJson& j,int64_t low,int64_t high){if(!j.is_number_integer()||(j.is_number_unsigned()&&j.get<uint64_t>()>uint64_t(INT64_MAX)))fail("Exact typed integer request required");const auto v=j.get<int64_t>();if(v<low||v>high)fail("Typed voltage request outside bounds");return v;}
std::string table_string(const ProfileJson& j,bool empty=false){if(!j.is_string())fail("Voltage profile needs text");auto v=j.get<std::string>();if((!empty&&v.empty())||v.size()>95||v.find('\0')!=std::string::npos)fail("Voltage profile text malformed");return v;}
std::string table_target(const VoltageTableKey& k){return (k.family==VoltageTableFamily::offsets?"voltage:scaffold-offset:":k.field<4?"rail:":"voltage:request:")+std::to_string(k.rail)+":"+std::to_string(k.field);}
struct TableProfileEntry {VoltageTableKey key;int64_t value=0;uint32_t policy=0,control=0;std::string name;};
struct TableProfile {ProfileSelection selection;bool python=false;std::optional<std::array<uint32_t,3>> adapter;std::vector<TableProfileEntry> entries;};
TableProfile parse_table_profile(const ProfileJson& d){TableProfile p;
    const auto add=[&](TableProfileEntry entry){const auto target=table_target(entry.key);if(std::find(p.selection.targets.begin(),p.selection.targets.end(),target)!=p.selection.targets.end())fail("Duplicate voltage request target");p.selection.targets.push_back(target);p.entries.push_back(std::move(entry));};
    if(d.contains("format")){table_keys(d,{"format","version","target","fields"});if(d["format"]!="druta-native-voltage-requests"||table_integer(d["version"],1,1)!=1)fail("Unsupported native voltage request profile");table_keys(d["target"],{"uuid","slot"});p.selection.uuid=table_string(d["target"]["uuid"]);p.selection.slot=table_string(d["target"]["slot"]);
        if(!d["fields"].is_array()||d["fields"].empty()||d["fields"].size()>12)fail("Invalid voltage request fields");for(const auto& e:d["fields"]){table_keys(e,{"rail","field","policy_type","control_type","value_uv"});TableProfileEntry entry;entry.key={VoltageTableFamily::requests,static_cast<uint32_t>(table_integer(e["rail"],0,1)),static_cast<uint32_t>(table_integer(e["field"],0,5))};entry.value=table_integer(e["value_uv"],INT32_MIN,INT32_MAX);entry.policy=static_cast<uint32_t>(table_integer(e["policy_type"],1,8));entry.control=static_cast<uint32_t>(table_integer(e["control_type"],0,3));if(entry.control==1)fail("Unknown legacy rail control type");add(std::move(entry));}
    }else{p.python=true;table_keys(d,{"schema","scope","voltage_requests"},{"saved_at","saved_ts","device","incomplete"});if(table_integer(d["schema"],2,2)!=2||d["scope"]!="voltage_requests")fail("Unsupported Python voltage request scope");
        if(d.contains("incomplete")&&(!d["incomplete"].is_array()||!d["incomplete"].empty()))fail("Incomplete voltage request profile");if(d.contains("saved_at")&&!d["saved_at"].is_string())fail("Malformed voltage profile timestamp");if(d.contains("saved_ts")&&(!d["saved_ts"].is_number()||d["saved_ts"].is_boolean()||!std::isfinite(d["saved_ts"].get<double>())))fail("Malformed voltage profile timestamp");
        const auto& payload=d["voltage_requests"];table_keys(payload,{"identity","rails"});const auto& identity=payload["identity"];table_keys(identity,{"slot","architecture","device_id","subsystem_id"});p.selection.slot=table_string(identity["slot"]);p.adapter=std::array<uint32_t,3>{static_cast<uint32_t>(table_integer(identity["architecture"],0,UINT32_MAX)),static_cast<uint32_t>(table_integer(identity["device_id"],0,65535)),static_cast<uint32_t>(table_integer(identity["subsystem_id"],0,UINT32_MAX))};
        if(d.contains("device")){const auto& device=d["device"];table_keys(device,{}, {"name","vbios","driver","uuid","slot"});for(auto it=device.begin();it!=device.end();++it)if(!it.value().is_null())table_string(it.value(),true);if(device.contains("uuid")&&!device["uuid"].is_null())p.selection.uuid=table_string(device["uuid"],true);if(device.contains("slot")&&!device["slot"].is_null()&&table_string(device["slot"])!=p.selection.slot)fail("Conflicting saved adapter slots");}
        const auto& rails=payload["rails"];if(!rails.is_object()||rails.empty()||rails.size()>2)fail("Invalid selected voltage rails");for(auto rail=rails.begin();rail!=rails.end();++rail){if(rail.key()!="0"&&rail.key()!="1")fail("Unknown voltage rail");const auto index=rail.key()=="0"?0u:1u;const auto& row=rail.value();table_keys(row,{"identity","offsets_uv"});table_keys(row["identity"],{"name","policy_type","control_type"});const auto name=table_string(row["identity"]["name"]);const auto policy=static_cast<uint32_t>(table_integer(row["identity"]["policy_type"],1,8)),control=static_cast<uint32_t>(table_integer(row["identity"]["control_type"],0,3));if(control==1)fail("Unknown legacy rail control type");
            const auto& values=row["offsets_uv"];if(!values.is_object()||values.empty()||values.size()>6)fail("Invalid voltage request fields");for(auto value=values.begin();value!=values.end();++value){uint32_t field=6;for(uint32_t i=0;i<6;++i)if(value.key()==fields[i])field=i;if(field==6)fail("Unknown voltage request field");add({{VoltageTableFamily::requests,index,field},table_integer(value.value(),INT32_MIN,INT32_MAX),policy,control,name});}}
    }return p;
}
}
ProfileSelection describe_voltage_tables_profile(const ProfileJson& d){return parse_table_profile(d).selection;}
ProfileJson select_voltage_tables_profile(const ProfileJson& d,const std::vector<std::string>& targets){const auto parsed=parse_table_profile(d);if(targets.empty())fail("Select voltage request targets");std::vector<std::string> seen;for(const auto& t:targets){if(std::find(parsed.selection.targets.begin(),parsed.selection.targets.end(),t)==parsed.selection.targets.end()||std::find(seen.begin(),seen.end(),t)!=seen.end())fail("Unknown or duplicate selected voltage target");seen.push_back(t);}auto out=d;
    if(!parsed.python){ProfileJson entries=ProfileJson::array();for(size_t i=0;i<parsed.entries.size();++i)if(std::find(seen.begin(),seen.end(),parsed.selection.targets[i])!=seen.end())entries.push_back(d["fields"][i]);out["fields"]=std::move(entries);}
    else {auto& rails=out["voltage_requests"]["rails"];for(auto rail=rails.begin();rail!=rails.end();){auto& values=rail.value()["offsets_uv"];for(auto value=values.begin();value!=values.end();){uint32_t field=6;for(uint32_t i=0;i<6;++i)if(value.key()==fields[i])field=i;VoltageTableKey key{VoltageTableFamily::requests,rail.key()=="0"?0u:1u,field};if(std::find(seen.begin(),seen.end(),table_target(key))==seen.end())value=values.erase(value);else ++value;}if(values.empty())rail=rails.erase(rail);else ++rail;}}
    (void)parse_table_profile(out);return out;
}
std::vector<VoltageTableRequest> stage_voltage_tables_profile(const ProfileJson& document,const VoltageTablesData& data){const auto parsed=parse_table_profile(document);if(!text(data.identity.uuid)||!text(data.identity.slot)||!data.identity.slot[0]||parsed.selection.slot!=data.identity.slot||(!parsed.selection.uuid.empty()&&parsed.selection.uuid!=data.identity.uuid))fail("Voltage request profile target differs");
    if(parsed.adapter){if(!data.target||data.target->size!=sizeof(dc_target)||(*parsed.adapter)[0]!=data.target->architecture||(*parsed.adapter)[1]!=data.target->device_id||(*parsed.adapter)[2]!=data.target->subsystem_id)fail("Saved complete adapter identity changed");}
    std::vector<VoltageTableRequest> requests;for(const auto& entry:parsed.entries){const auto row=std::find_if(data.rows.begin(),data.rows.end(),[&](const auto& r){return r.key==entry.key;});if(row==data.rows.end()||!row->value||row->stale||row->availability!=VoltagePolicyAvailability::available||row->binding.empty()||row->policy_type!=entry.policy||row->control_type!=entry.control)fail("Current selected voltage request is unavailable or changed identity");
        if(parsed.python){const auto expected_name=row->policy_type==(entry.key.rail==0?1u:3u)?(entry.key.rail==0?"NVVDD":"MSVDD"):"Rail "+std::to_string(entry.key.rail);if(entry.name!=expected_name)fail("Saved rail name no longer matches current discriminator");}
        requests.push_back({entry.key,entry.value,row->binding,row->value});}return requests;
}
}
