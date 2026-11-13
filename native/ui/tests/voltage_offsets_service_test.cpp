// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_offsets_service.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <algorithm>
using namespace druta::ui;
namespace {
unsigned checks=0;int sets=0,fail_at=0,read_failures=0,locks=0;bool no_dispatch=false,corrupt_unknown=false;VoltageOffsetSnapshot snapshot;dc_gpu_identity identity{};
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
void put32(std::vector<uint8_t>& b,size_t at,uint32_t value){std::memcpy(b.data()+at,&value,4);}
uint32_t get32(const uint8_t* p){uint32_t value;std::memcpy(&value,p,4);return value;}
void number(std::vector<uint8_t>& b,size_t at,int64_t value){const auto f=static_cast<float>(value);std::memcpy(b.data()+at,&f,4);}
int64_t value(uint32_t index){float f;std::memcpy(&f,snapshot.equations.control.data()+0x408+0x80*index+4,4);return static_cast<int64_t>(f);}
dc_status DC_CALL id(const dc_gpu*,dc_gpu_identity* out){*out=identity;return DC_OK;}
dc_status DC_CALL bind(dc_gpu*,int32_t,dc_target* out,dc_error*){*out=snapshot.scaffold.target;return DC_OK;}
int32_t DC_CALL arch(uint32_t a){return a==7;}
dc_status DC_CALL lock(dc_gpu*){++locks;return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){--locks;return DC_OK;}
dc_status DC_CALL transport(dc_gpu*,dc_rm_transport* out,dc_error*){out->captured=1;out->header_count=17;out->header[0]=0x4e564441;out->header[1]=0x10002;out->header[2]=76;out->header[3]=0x4e562a2a;out->header[4]=0x0500002b;out->header[15]=8;return DC_OK;}
dc_status DC_CALL escape(dc_gpu*,uint8_t* raw,size_t size,const dc_escape_fields*,int32_t* nt,dc_error*){
 auto* packet=reinterpret_cast<uint32_t*>(raw);CHECK(packet[14]==0x2080e0b7&&packet[15]==0x100408&&size==68+0x100408);if(no_dispatch)return DC_E_UNAVAILABLE;
 ++sets;auto* payload=raw+68;uint32_t selected=8192;for(uint32_t i=0;i<8192;++i)if(payload[4+i/8]&(1u<<(i%8))){CHECK(selected==8192);selected=i;}CHECK(selected<8192);
 const size_t at=0x408+selected*0x80;float f;std::memcpy(&f,payload+at+4,4);const auto prior=value(selected);const auto wanted=static_cast<int64_t>(f);
 auto expected=snapshot.equations.control;for(const auto offset:{4u,8u,16u})number(expected,at+offset,wanted);std::fill_n(expected.begin()+4,1024,uint8_t{0});expected[4+selected/8]=uint8_t(1u<<(selected%8));CHECK(std::equal(expected.begin(),expected.end(),payload));
 for(const auto offset:{4u,8u,16u})number(snapshot.equations.control,at+offset,wanted);
 for(auto& head:snapshot.scaffold.heads)if(head.index==selected){head.value_uv=wanted;auto& rail=snapshot.retained.rails[head.rail];const auto field=head.role==VbiosVoltageRole::rel?0u:head.role==VbiosVoltageRole::alt?1u:head.role==VbiosVoltageRole::ov?2u:3u;rail.policy[field]+=wanted-prior;rail.policy[4]+=wanted-prior+17;
  if(snapshot.retained.boost_percent.value_or(0)!=0&&(head.role==VbiosVoltageRole::rel||head.role==VbiosVoltageRole::alt))for(auto& peer:snapshot.retained.rails)if(peer.present){peer.policy[0]+=123;peer.policy[4]+=11;peer.boost_uv+=1;}}
 if(corrupt_unknown)snapshot.equations.control[0x600]^=1;packet[11]=1;*nt=sets==fail_at?1:0;return DC_OK;
}
void reset(){snapshot={};identity={};sets=fail_at=read_failures=locks=0;no_dispatch=corrupt_unknown=false;identity.size=sizeof(identity);std::strcpy(identity.slot,"0000:5a:00.0");std::strcpy(identity.uuid,"GPU-signed-other");identity.nvapi_ok=identity.nvapi_selected=1;identity.nvapi_handle=42;identity.nvapi_devid=0x4567;identity.nvapi_subsys=0xaabbccdd;
 auto& target=snapshot.scaffold.target;target.size=sizeof(target);std::strcpy(target.slot,identity.slot);target.handle=42;target.architecture=7;target.device_id=identity.nvapi_devid;target.subsystem_id=identity.nvapi_subsys;snapshot.equations.target=snapshot.retained.target=target;
 snapshot.scaffold.receipt_binding=std::string(64,'a');snapshot.scaffold.image_sha256=snapshot.retained.image_sha256=std::string(64,'b');snapshot.retained.retained_requests_qualified=true;snapshot.retained.policy_identity={1,2,3};snapshot.retained.rail_requests={4,5};snapshot.retained.clock_requests={6,7};snapshot.retained.boost_percent=0;
 snapshot.retained.rails[0].present=true;snapshot.retained.rails[0].availability=VoltagePolicyAvailability::available;snapshot.retained.rails[0].policy={1000000,1020000,1200000,750000,1000000};snapshot.retained.rails[1].availability=VoltagePolicyAvailability::unsupported;
 snapshot.equations.info.resize(0xe0408);snapshot.equations.control.resize(0x100408);snapshot.equations.info[0]=snapshot.equations.control[0]=1;snapshot.equations.info[4]=snapshot.equations.control[4]=0xc3;
 for(uint32_t n=0;n<2;++n){const uint32_t index=6+n;const size_t at=0x408+0x70*index,ct=0x408+0x80*index;snapshot.equations.info[at]=snapshot.equations.info[at+0x6c]=snapshot.equations.control[ct]=snapshot.equations.control[ct+0x7c]=3;snapshot.equations.info[at+2]=uint8_t(n);snapshot.equations.info[at+0x6d]=7;snapshot.equations.info[at+0x6e]=2;snapshot.equations.info[at+30]=0xa5;snapshot.equations.control[ct+0x60]=0x9d;snapshot.scaffold.heads.push_back({0,index,n,7,2,0x800+13+2*n,0x4000+25*index,n==0?VbiosVoltageRole::ov:VbiosVoltageRole::vmin,0});}
}
VoltageOffsetsApi api(){VoltageOffsetsApi a;a.transport.transport.identity=id;a.transport.transport.bind=bind;a.transport.transport.architecture_supported=arch;a.transport.transport.lock=lock;a.transport.transport.unlock=unlock;a.transport.transport.transport=transport;a.transport.transport.escape=escape;
 a.capture=[](dc_gpu*){if(read_failures){--read_failures;throw VoltageEquationEvidenceError("transient capture",VoltagePolicyAvailability::transient);}return snapshot;};return a;}
VoltageOffsetRequest request(VoltageOffsetsService& service,unsigned row,int64_t wanted){const auto& data=service.read();CHECK(data.rows.size()==2&&data.rows[row].writable);return {data.rows[row].key,wanted,data.rows[row].binding,data.rows[row].value_uv};}
void run(){reset();auto* gpu=reinterpret_cast<dc_gpu*>(1);VoltageOffsetsService service(api(),gpu,identity);CHECK(sets==0);auto first=request(service,0,-5000),second=request(service,1,10000);
 const auto plan=service.preview({first,second});CHECK(plan.ok&&sets==0);VoltageOffsetsReceipt receipt;unsigned journals=0;const auto original=snapshot.equations.control;
 CHECK(service.apply_profile(plan,[&](const auto& issued){receipt=issued;++journals;CHECK(sets<int(journals));}).ok&&sets==2&&journals==2&&value(6)==-5000&&value(7)==10000);
 CHECK(snapshot.equations.control[0x408+6*0x80+0x60]==0x9d&&service.verify(plan).ok&&service.verify(receipt).ok);CHECK(service.restore(receipt).ok&&snapshot.equations.control==original&&service.verify(receipt,true).ok&&locks==0);
 auto bad=first;bad.value_uv=16777217;const auto before=sets;CHECK(!service.preview({first,bad}).ok&&sets==before);CHECK(!service.apply(bad).ok&&sets==before);
 first=request(service,0,-1000);CHECK(service.apply(first).ok);const auto previous=value(6);auto noop=request(service,0,previous);CHECK(service.apply(noop).ok&&service.state().undo_available);no_dispatch=true;auto next=request(service,0,-2000);const auto saved_sets=sets;CHECK(!service.apply(next).ok&&sets==saved_sets&&service.state().undo_available&&!service.state().recovery_pending);no_dispatch=false;CHECK(service.undo().ok&&value(6)==0);
 first=request(service,0,-3000);CHECK(service.apply(first).ok);second=request(service,0,-4000);CHECK(service.apply(second).ok&&service.undo().ok&&value(6)==-3000);CHECK(service.undo().ok&&value(6)==0);
 first=request(service,0,-5000);second=request(service,1,10000);const auto partial=service.preview({first,second});fail_at=sets+2;VoltageOffsetsReceipt pending;auto result=service.apply_profile(partial,[&](const auto& issued){pending=issued;});CHECK(!result.ok&&result.may_have_changed&&service.state().recovery_pending&&value(6)==-5000&&value(7)==10000);CHECK(!service.preview({first}).ok);
 const auto& pending_read=service.read();CHECK(pending_read.recovery_pending&&!pending_read.rows[1].writable&&!pending_read.rows[1].stale&&pending_read.rows[1].availability==VoltagePolicyAvailability::available&&pending_read.rows[1].value_uv==10000);
 fail_at=0;CHECK(service.restore(pending).ok&&value(6)==0&&value(7)==0&&!service.state().recovery_pending);
 first=request(service,0,-5000);const auto before_journal=sets;result=service.apply_profile(service.preview({first}),[](const auto&){throw std::runtime_error("journal rejected");});CHECK(!result.ok&&!result.may_have_changed&&sets==before_journal);
 first=request(service,0,-5000);CHECK(service.apply(first).ok);snapshot.scaffold.heads[0].value_uv=-6000;for(const auto offset:{4u,8u,16u})number(snapshot.equations.control,0x408+6*0x80+offset,-6000);const auto before_conflict=sets;CHECK(!service.undo().ok&&sets==before_conflict);snapshot.scaffold.heads[0].value_uv=-5000;for(const auto offset:{4u,8u,16u})number(snapshot.equations.control,0x408+6*0x80+offset,-5000);CHECK(service.undo().ok);
 read_failures=1;CHECK(service.read().rows[0].stale&&read_failures==0);CHECK(service.read().rows[0].writable);read_failures=2;CHECK(service.read().rows[0].stale&&service.state().rows[0].value_uv==0);CHECK(service.read().rows[0].stale&&read_failures==0);CHECK(service.read().rows[0].writable);
 first=request(service,0,-5000);CHECK(service.apply(first).ok&&service.restore_initial(first.key).ok&&value(6)==0);CHECK(service.undo().ok&&value(6)==-5000);
 auto wrong_identity=identity;wrong_identity.nvapi_subsys^=1;VoltageOffsetsService wrong(api(),gpu,wrong_identity);CHECK(!wrong.read().rows.size());CHECK(!wrong.restore(receipt).ok&&locks==0);
}
void independent_cases(){reset();auto* gpu=reinterpret_cast<dc_gpu*>(1);auto missing=api();missing.transport.transport.escape=nullptr;VoltageOffsetsService readonly(missing,gpu,identity);const auto& inspected=readonly.read();CHECK(inspected.rows.size()==2&&!inspected.rows[0].writable&&!inspected.rows[0].stale&&inspected.rows[0].availability==VoltagePolicyAvailability::available&&inspected.rows[0].initial_uv==0);
 auto unsupported_request=VoltageOffsetRequest{inspected.rows[0].key,-1000,inspected.rows[0].binding,inspected.rows[0].value_uv};CHECK(!readonly.preview({unsupported_request}).ok&&sets==0);
 reset();identity.uuid[0]=0;identity.nvapi_devid=0x6789;snapshot.scaffold.target.device_id=snapshot.equations.target.device_id=snapshot.retained.target.device_id=0x6789;VoltageOffsetsService alternate(api(),gpu,identity);auto req=request(alternate,0,-2000);CHECK(alternate.apply(req).ok&&alternate.undo().ok&&value(6)==0);
 reset();snapshot.scaffold.heads[0].role=VbiosVoltageRole::rel;snapshot.retained.boost_percent=25;snapshot.retained.rails[1]=snapshot.retained.rails[0];VoltageOffsetsService derived(api(),gpu,identity);req=request(derived,0,-1000);const auto policy_before=snapshot.retained.rails[0].policy[0];CHECK(derived.apply(req).ok&&snapshot.retained.rails[0].policy[0]==policy_before-1000+123&&derived.undo().ok);
 reset();snapshot.scaffold.heads[0].role=VbiosVoltageRole::alt;snapshot.scaffold.heads[0].next=1;snapshot.equations.info[0x408+6*0x70+2]=1;snapshot.retained.boost_percent=25;VoltageOffsetsService alt(api(),gpu,identity);req=request(alt,0,1000);const auto old_alt=snapshot.retained.rails[0].policy[1];CHECK(alt.apply(req).ok&&snapshot.retained.rails[0].policy[1]==old_alt+1000&&alt.undo().ok);
 reset();snapshot.scaffold.heads[0].role=VbiosVoltageRole::rel;snapshot.retained.boost_percent.reset();VoltageOffsetsService unknown_boost(api(),gpu,identity);const auto row=unknown_boost.read().rows[0];CHECK(!row.writable&&row.availability==VoltagePolicyAvailability::available&&!row.stale);req={row.key,1000,row.binding,row.value_uv};CHECK(!unknown_boost.preview({req}).ok&&sets==0);
 reset();VoltageOffsetsService foreign(api(),gpu,identity);req=request(foreign,0,-1000);auto plan=foreign.preview({req});corrupt_unknown=true;CHECK(!foreign.apply_profile(plan).ok&&foreign.state().recovery_pending);corrupt_unknown=false;const auto before=sets;CHECK(!foreign.recover().ok&&sets==before);snapshot.equations.control[0x600]^=1;CHECK(foreign.recover().ok&&value(6)==0);
 reset();VoltageOffsetsService noop(api(),gpu,identity);auto noop_request=request(noop,0,0);plan=noop.preview({noop_request});snapshot.scaffold.heads[0].value_uv=1000;for(const auto offset:{4u,8u,16u})number(snapshot.equations.control,0x408+6*0x80+offset,1000);CHECK(!noop.verify(plan).ok&&sets==0);
}
void history_boundary(){reset();auto* gpu=reinterpret_cast<dc_gpu*>(1);VoltageOffsetsService service(api(),gpu,identity);int history=0;bool reject=false;service.set_history_callback([&]{++history;if(reject)throw std::runtime_error("history refused");});auto same=request(service,0,0);CHECK(service.apply(same).ok&&history==0&&sets==0);auto first=request(service,0,-1000);CHECK(service.apply(first).ok&&history==1&&sets==1);auto next=request(service,0,-2000);reject=true;CHECK(!service.apply(next).ok&&history==2&&sets==1&&service.state().undo_available);CHECK(service.undo().ok&&value(6)==0&&history==2);}
}
int main(){history_boundary();run();independent_cases();std::printf("Signed voltage offsets: %u checks passed\n",checks);return 0;}
