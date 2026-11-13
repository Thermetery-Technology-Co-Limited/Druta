/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "timings_service.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <windows.h>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
namespace {
dc_gpu_identity dev{};uint32_t word=0xABCD182D, mem=7001,pstate=0;int32_t offset=0;int writes=0;bool foreign=false,fail_after=false,drop=false;
dc_status DC_CALL id(const dc_gpu*,dc_gpu_identity* out){*out=dev;if(foreign)out->nvapi_devid++;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){return DC_OK;} dc_status DC_CALL unlock(dc_gpu*){return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* out){out->present=DC_T_MEM|DC_T_PSTATE|DC_T_MEM_OFF;out->mem=mem;out->pstate=pstate;out->mem_off=offset;return DC_OK;}
dc_status DC_CALL caps(dc_gpu*,dc_capabilities* out){out->present=DC_C_MEM_DIV;out->mem_div=4;out->mem_clock_count=2;out->mem_clocks[0]=405;out->mem_clocks[1]=7001;return DC_OK;}
struct Mock:TimingTransport {
    bool pinned=false, changed=false, malformed=false, legacy=false, warning=false, badcontract=false, overlap=false, wrongpreview=false, move_during_dump=false, process_pending=false, save_wrong_slot=false, save_wrong_word=false, save_fail=false, save_foreign=false, save_move_state=false;std::string slot="0000:09:00.0";int previews=0;
    bool pending() const override { return process_pending; }
    bool pin(std::string&) override {pinned=true;return true;}bool unchanged(std::string&) override{return pinned&&!changed;}
    std::string token()const override{return changed?"new":"stable";}void unpin()override{pinned=false;}
    TimingProcessResult run(TimingCommand op,const std::string& target,const std::string& field,uint32_t value,bool,bool) override {
        TimingProcessResult r;r.launched=true;
        if(op==TimingCommand::help)r.output=badcontract ? "unknown helper" : legacy ? "Everything defaults to a dry run\n--commit is required to touch hardware\n" : "nvtune - NVIDIA FBPA memory timing tool\nset FIELD=VALUE... write fields\nA first write snapshots stock values\n";
        else if(op==TimingCommand::fields)r.output=std::string("FAW CONFIG0 [15:8] 255 Four activate\nTRAIN CONFIG0 [7:0] 255 [structural] Training\nREFRESH CONFIG1 [15:8] 255 Refresh\nREFRESH_LO CONFIG1 [7:0] 255 [structural] Refresh lower\nRFC CONFIG2 [7:0] 255 Refresh cycles\n")+(overlap?"OTHER CONFIG0 [12:10] 7 Overlap\n":"");
        else if(op==TimingCommand::dump){CHECK(target=="0000:09:00.0");char b[256];std::snprintf(b,sizeof(b),"%s GP102 (Pascal)\n [broadcast] base 0x001000\n CONFIG0 @0x001000 = 0x%08X (DOCUMENTED)\n",malformed?"0000:08:00.0":slot.c_str(),word);r.output=b;if(move_during_dump)mem=405;}
        else if(op==TimingCommand::save){CHECK(target=="0000:09:00.0");if(save_foreign)foreign=true;if(save_move_state)pstate=2;if(save_fail)r.exit_code=1;else {char b[500];std::snprintf(b,sizeof(b),"{\"slot\":\"%s\",\"codename\":\"GP102\",\"registers\":{\"broadcast\":{\"CONFIG0\":\"0x%08X\"},\"fbpa0\":{\"CONFIG0\":\"0x%08X\"},\"fbpa1\":{}}}",save_wrong_slot?"0000:08:00.0":slot.c_str(),save_wrong_word?word+1:word,word^0x100);r.output=b;}}
        else if(op==TimingCommand::preview){++previews;char p[256];std::snprintf(p,sizeof(p),"CONFIG0 @0x001000 0x%08X -> 0x%08X [would write]\n  FAW %u -> %u\n%s",word,(word&~0xff00u)|(value<<8),(word>>8)&255,value,warning?"  extra warning\n":wrongpreview?" [other-scope]\n":"");r.output=p;}
        else {CHECK(op==TimingCommand::commit&&target=="0000:09:00.0"&&field=="FAW");++writes;if(!drop)word=(word&~0xff00u)|(value<<8);if(fail_after)r.exit_code=1;}
        return r;
    }
};
}
int wmain(int argc,wchar_t** argv){
 if(argc>1){if(std::wstring(argv[1])==L"save" && argc==6 && std::wstring(argv[2])==L"-d" && std::wstring(argv[4])==L"-o") {
     if(std::wstring(argv[3])==L"0000:08:00.0")return 5;
     HANDLE file=CreateFileW(argv[5],GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);if(file==INVALID_HANDLE_VALUE)return 6;
     const char data[]="{\"slot\":\"0000:09:00.0\",\"registers\":{\"broadcast\":{}}}";DWORD written=0;
     const bool ok=WriteFile(file,data,sizeof(data)-1,&written,nullptr)!=0 && written==sizeof(data)-1;CloseHandle(file);return ok?0:7;
 } else if(std::wstring(argv[1])==L"--help")std::printf("mock helper help\n");else if(std::wstring(argv[1])==L"dump" && argc==5 && std::wstring(argv[2])==L"-d" && std::wstring(argv[4])==L"--raw")std::printf("mock selected dump\n");else return 3;return 0;}
 dev.size=sizeof(dev);dev.nvapi_ok=dev.nvapi_selected=1;dev.nvapi_handle=42;std::memcpy(dev.slot,"0000:09:00.0",sizeof("0000:09:00.0"));
 TimingsApi api{};api.identity=id;api.telemetry=telemetry;api.capabilities=caps;api.lock=lock;api.unlock=unlock;Mock mock;
 TimingsService svc(api,reinterpret_cast<dc_gpu*>(1),dev,mock);
 CHECK(!svc.pending()); mock.process_pending=true; CHECK(svc.pending()); mock.process_pending=false; CHECK(!svc.pending());
 auto snap=svc.capture();CHECK(snap.ok&&snap.top_band&&snap.memory_clock_stable&&snap.broadcast.size()==1&&snap.fields.size()==5);
 CHECK(snap.diagnostic_evidence.units.size()==3 && snap.diagnostic_evidence.units[0].cycles && snap.diagnostic_evidence.units[0].nanoseconds && !snap.diagnostic_evidence.units[1].nanoseconds && !snap.diagnostic_evidence.units[2].nanoseconds);
 CHECK(snap.diagnostic_evidence.memory_clock_divisor==4 && snap.diagnostic_evidence.partitions.size()==2 && snap.diagnostic_evidence.partitions[0].words[0].value==(word^0x100));
 mock.save_wrong_slot=true;auto wrong_slot=svc.capture();CHECK(wrong_slot.ok && wrong_slot.diagnostic_evidence.partitions_stale);mock.save_wrong_slot=false;
 mock.save_wrong_word=true;auto wrong_word=svc.capture();CHECK(wrong_word.ok && wrong_word.diagnostic_evidence.partitions_stale);mock.save_wrong_word=false;
 mock.save_fail=true;auto no_partitions=svc.capture();CHECK(no_partitions.ok && no_partitions.broadcast.size()==1 && no_partitions.diagnostic_evidence.partitions_stale);mock.save_fail=false;
 mock.save_move_state=true;auto state_changed=svc.capture();CHECK(state_changed.ok && state_changed.diagnostic_evidence.partitions_stale);pstate=0;mock.save_move_state=false;
 mock.save_foreign=true;auto identity_lost=svc.capture();CHECK(!identity_lost.ok && identity_lost.diagnostic_evidence.partitions.empty());foreign=false;mock.save_foreign=false;
 mock.save_fail=true;word^=0x10000;auto changed_broadcast=svc.capture();CHECK(changed_broadcast.ok && changed_broadcast.diagnostic_evidence.partitions.empty());word^=0x10000;mock.save_fail=false;
 CHECK(!svc.preview("TRAIN",5).ok&&writes==0);CHECK(!svc.preview("FAW",256).ok);
 auto plan=svc.preview("FAW",25);CHECK(plan.ok&&plan.prior==24&&plan.prior_word==0xABCD182D&&plan.target_word==0xABCD192D);
 plan.prior=999; // untrusted preview metadata cannot alter captured Undo
 int callbacks=0;TimingPlan captured;auto result=svc.apply(plan,[&](const TimingPlan& p){++callbacks;captured=p;});
 CHECK(result.ok&&result.undo&&callbacks==1&&writes==1&&word==0xABCD192D&&captured.value==24);
 CHECK(svc.restore(captured).ok&&writes==2&&word==0xABCD182D);
 plan=svc.preview("FAW",25);word^=0x10000;CHECK(!svc.apply(plan).ok&&writes==2);word^=0x10000;
 plan=svc.preview("FAW",25);foreign=true;CHECK(!svc.apply(plan).ok&&writes==2);foreign=false;
 plan=svc.preview("FAW",25);pstate=8;CHECK(!svc.apply(plan).ok&&writes==2);pstate=0;
 plan=svc.preview("FAW",25);mock.changed=true;CHECK(!svc.apply(plan).ok&&writes==2);mock.changed=false;
 mock.malformed=true;CHECK(!svc.capture().ok&&!svc.preview("FAW",25).ok);mock.malformed=false;
 plan=svc.preview("FAW",25);fail_after=true;result=svc.apply(plan);CHECK(!result.ok&&result.may_have_changed&&result.undo&&writes==3);fail_after=false;
 CHECK(svc.restore(*result.undo).ok&&word==0xABCD182D);
 mock.badcontract=true; CHECK(!svc.preview("FAW",25).ok); mock.badcontract=false;
 mock.move_during_dump=true; auto moving=svc.capture(); CHECK(moving.ok && !moving.memory_clock_stable && !moving.top_band);
 mock.move_during_dump=false; mem=7001;
 mock.overlap=true; auto overlapping=svc.capture(); CHECK(overlapping.ok && overlapping.fields[0].overlapping && !svc.preview("FAW",25).ok); mock.overlap=false;
 drop=true; plan=svc.preview("FAW",25); auto dropped=svc.apply(plan); CHECK(!dropped.ok && dropped.undo && word==0xABCD182D);
 int before_drop_restore=writes; CHECK(svc.restore(*dropped.undo).ok && writes==before_drop_restore); drop=false;
 mock.legacy=true; plan=svc.preview("FAW",25); mock.warning=true; CHECK(!svc.apply(plan).ok && writes==5);
 mock.warning=false; mock.wrongpreview=true; CHECK(!svc.apply(plan).ok && writes==5);
 mock.wrongpreview=false; result=svc.apply(plan); CHECK(result.ok && writes==6 && mock.previews==3);
 CHECK(svc.restore(*result.undo).ok && writes==7 && word==0xABCD182D);
 wchar_t path[32768]{};const DWORD path_len=GetModuleFileNameW(nullptr,path,32768);CHECK(path_len>0 && path_len<32768);
 auto native=make_timing_transport(std::wstring(path,path_len)); std::string transport_error;
 CHECK(native->pin(transport_error)); CHECK(native->token().size()>64 && native->unchanged(transport_error));
 auto realhelp=native->run(TimingCommand::help,"","",0,false,false);if(!realhelp.launched || realhelp.exit_code || realhelp.output.find("mock helper help")==std::string::npos) std::fprintf(stderr,"helper launched=%d exit=%u error=%s output=%s\n",realhelp.launched,realhelp.exit_code,realhelp.error.c_str(),realhelp.output.c_str()); CHECK(realhelp.launched && realhelp.exit_code==0 && realhelp.output.find("mock helper help")!=std::string::npos);
 auto realdump=native->run(TimingCommand::dump,"0000:09:00.0","",0,false,false);CHECK(realdump.launched && realdump.exit_code==0 && realdump.output.find("mock selected dump")!=std::string::npos);
 auto realsave=native->run(TimingCommand::save,"0000:09:00.0","",0,false,false);
 CHECK(realsave.launched && !realsave.exit_code && realsave.error.empty() && realsave.output.find("broadcast")!=std::string::npos);
 auto failedsave=native->run(TimingCommand::save,"0000:08:00.0","",0,false,false);CHECK(failedsave.launched && failedsave.exit_code==5);
 CHECK(!native->run(TimingCommand::save,"bad-slot","",0,false,false).launched);
 CHECK(!native->run(TimingCommand::dump,"bad-slot","",0,false,false).launched); native->unpin();
 std::printf("timings_service_test: %u checks passed\n",checks);
}
