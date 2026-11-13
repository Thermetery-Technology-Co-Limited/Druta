// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timings_service.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
namespace {
dc_gpu_identity gpu_id{};uint32_t mem=7000,ps=0;bool foreign=false;
dc_status DC_CALL identity(const dc_gpu*,dc_gpu_identity* out){*out=gpu_id;if(foreign)out->nvapi_devid++;return DC_OK;}
dc_status DC_CALL lock(dc_gpu*){return DC_OK;}dc_status DC_CALL unlock(dc_gpu*){return DC_OK;}
dc_status DC_CALL telemetry(dc_gpu*,dc_telemetry* out){out->present=DC_T_MEM|DC_T_MEM_OFF|DC_T_PSTATE;out->mem=mem;out->mem_off=0;out->pstate=ps;return DC_OK;}
dc_status DC_CALL caps(dc_gpu*,dc_capabilities* out){out->mem_clock_count=1;out->mem_clocks[0]=7000;return DC_OK;}
struct Mock:TimingTransport {
    uint32_t words[2]={0xA5A51234,0xCAFE3456};unsigned writes=0,reads=0,previews=0,saves=0;
    bool held=false,changed=false,process_pending=false,change_unknown=false,fail_dump=false,malformed_save=false;
    std::string fail_preview,fail_launch,fail_write,pending_field,throw_field;
    bool pending()const override{return process_pending;}
    bool pin(std::string&)override{held=true;return true;}void unpin()override{held=false;}
    bool unchanged(std::string&)override{return held;}
    std::string token()const override{return changed?"helper-new":"helper-original";}
    TimingProcessResult run(TimingCommand op,const std::string& slot,const std::string& f,uint32_t value,bool commit,bool)override{
        TimingProcessResult r;r.launched=true;
        if(op==TimingCommand::help)r.output="Everything defaults to a dry run\n--commit is required to touch hardware\n";
        else if(op==TimingCommand::fields)r.output="A REG0 [7:0] 255 First\nB REG0 [15:8] 255 Second\nC REG1 [7:0] 255 Third\nSTRUCT REG1 [31:24] 255 [structural] Mode\n";
        else if(op==TimingCommand::dump){++reads;CHECK(slot=="0000:09:00.0");if(fail_dump){r.exit_code=1;r.output="driver could not be opened";return r;}
            char text[512]{};std::snprintf(text,sizeof(text),"0000:09:00.0 GP102 (Pascal)\n [broadcast] base 0x001000\n REG0 @0x001000 = 0x%08X (DOCUMENTED)\n REG1 @0x001004 = 0x%08X (DOCUMENTED)\n",words[0],words[1]);r.output=text;
        }else if(op==TimingCommand::save){
            ++saves;CHECK(slot=="0000:09:00.0" && f.empty() && !commit);
            r.output=malformed_save?"{broken":"{\"slot\":\"0000:09:00.0\",\"codename\":\"GP102\",\"registers\":{\"broadcast\":{}}}";
        }else{
            CHECK(slot=="0000:09:00.0"&&(f=="A"||f=="B"||f=="C"));const unsigned index=f=="C"?1:0,shift=f=="B"?8:0;
            const uint32_t target=(words[index]&~(255u<<shift))|(value<<shift);
            if(op==TimingCommand::preview){++previews;char text[512]{};
                std::snprintf(text,sizeof(text),"REG%u @0x%06X 0x%08X -> 0x%08X [would write]\n  %s %u -> %u\n%s",index,0x1000+4*index,words[index],target,f.c_str(),(words[index]>>shift)&255,value,f==fail_preview?"warning: refused\n":"");r.output=text;
            }else{CHECK(op==TimingCommand::commit&&commit);if(f==fail_launch){r.launched=false;r.error="launch failed";return r;}
                ++writes;words[index]=target;if(change_unknown)words[1]^=0x100000;
                if(f==throw_field)throw std::runtime_error("transport exception after dispatch");
                if(f==fail_write)r.exit_code=1;if(f==pending_field)process_pending=true;
            }
        }return r;
    }
};
}
int main(){
    gpu_id.nvapi_ok=gpu_id.nvapi_selected=1;gpu_id.nvapi_handle=7;
    std::snprintf(gpu_id.slot,sizeof(gpu_id.slot),"%s","0000:09:00.0");std::snprintf(gpu_id.uuid,sizeof(gpu_id.uuid),"%s","GPU-batch");
    TimingsApi api;api.identity=identity;api.telemetry=telemetry;api.capabilities=caps;api.lock=lock;api.unlock=unlock;
    Mock m;TimingsService service(api,reinterpret_cast<dc_gpu*>(1),gpu_id,m);
    const std::vector<TimingRequest> requests={{"B",25},{"C",80},{"A",60}};
    const auto initial=service.capture();CHECK(initial.ok&&initial.top_band&&service.initial_snapshot());
    CHECK(m.saves==1 && m.writes==0 && initial.diagnostic_evidence.partitions.empty());
    m.malformed_save=true;const auto partial=service.capture();CHECK(partial.ok && partial.broadcast.size()==2 && partial.diagnostic_evidence.partitions.empty() && m.writes==0);m.malformed_save=false;
    auto plan=service.preview(requests);CHECK(plan.ok&&plan.fields.size()==3&&plan.fields[0].field=="A");
    CHECK(plan.fields[1].prior_word==0xA5A5123C&&plan.fields[1].target_word==0xA5A5193C);
    unsigned captured=0;auto result=service.apply(plan,[&](const auto& undo){++captured;CHECK(undo.fields.size()==captured);});
    CHECK(result.ok&&result.undo&&captured==3&&m.writes==3&&result.launched_fields.size()==3);
    CHECK(m.words[0]==0xA5A5193C&&m.words[1]==0xCAFE3450);
    CHECK(service.restore(*result.undo).ok&&m.words[0]==0xA5A51234&&m.words[1]==0xCAFE3456);
    const auto before=m.writes;
    CHECK(!service.preview({{"A",1},{"A",2}}).ok&&!service.preview({{"STRUCT",2}}).ok&&!service.preview({{"A",256}}).ok&&m.writes==before);
    plan=service.preview(requests);m.fail_preview="C";CHECK(!service.apply(plan).ok&&m.writes==before);m.fail_preview.clear();
    plan=service.preview(requests);m.fail_launch="B";result=service.apply(plan);
    CHECK(!result.ok&&result.may_have_changed&&result.undo&&result.launched_fields==std::vector<std::string>{"A"}&&result.undo->fields.size()==1);
    m.fail_launch.clear();m.words[0]^=0x100000;const auto changed_unknown=m.words[0]&0xFFFF0000;
    CHECK(service.restore(*result.undo).ok&&(m.words[0]&0xFFFF0000)==changed_unknown&&(m.words[0]&255)==0x34);
    plan=service.preview(requests);m.fail_write="B";result=service.apply(plan);CHECK(!result.ok&&result.undo&&result.launched_fields.size()==2);
    m.fail_write.clear();CHECK(service.restore(*result.undo).ok);
    plan=service.preview(requests);m.throw_field="B";TimingBatchPlan recorded;
    result=service.apply(plan,[&](const auto& partial){recorded=partial;});
    CHECK(!result.ok&&result.may_have_changed&&result.undo&&result.launched_fields.size()==2&&recorded.fields.size()==2);
    CHECK(result.undo->fields.size()==2&&result.undo->fields.back().field=="B");m.throw_field.clear();
    m.words[0]=(m.words[0]&~255u)|99;const auto alien_writes=m.writes;
    CHECK(!service.restore(*result.undo).ok&&m.writes==alien_writes); // foreign selected request is preserved
    m.words[0]=(m.words[0]&~255u)|60;CHECK(service.restore(*result.undo).ok);
    plan=service.preview(requests);m.pending_field="A";result=service.apply(plan);CHECK(!result.ok&&result.undo&&service.pending());
    const auto pending_reads=m.reads;CHECK(!service.restore(*result.undo).ok&&m.reads==pending_reads);
    m.process_pending=false;m.pending_field.clear();CHECK(service.restore(*result.undo).ok);
    plan=service.preview(requests);m.change_unknown=true;result=service.apply(plan);
    CHECK(!result.ok&&result.undo&&result.message.find("unselected")!=std::string::npos);m.change_unknown=false;
    CHECK(service.restore(*result.undo).ok); // only selected fields restored; changed unknown bits stay
    plan=service.preview(requests);m.changed=true;const auto helper_writes=m.writes;CHECK(!service.apply(plan).ok&&m.writes==helper_writes);
    CHECK(!service.preview_initial().ok&&m.writes==helper_writes); // old initial cannot cross helper identity
    m.changed=false;service.capture();plan=service.preview(requests);foreign=true;CHECK(!service.apply(plan).ok);foreign=false;
    plan=service.preview(requests);mem=405;CHECK(!service.apply(plan).ok);mem=7000;
    m.fail_dump=true;const auto failed=service.capture();CHECK(!failed.ok&&failed.message.find("helper ran but the operation failed")!=std::string::npos);m.fail_dump=false;
    // Captured initial is a separate first-qualified baseline, not the most
    // recent Undo target or a guessed factory register image.
    Mock clean;TimingsService separate(api,reinterpret_cast<dc_gpu*>(1),gpu_id,clean);CHECK(separate.capture().ok);
    CHECK(separate.apply(separate.preview(requests)).ok);auto restore_initial=separate.preview_initial();CHECK(restore_initial.ok);
    CHECK(separate.restore_initial().ok&&clean.words[0]==0xA5A51234&&clean.words[1]==0xCAFE3456);
    std::printf("timings_batch_service_test: %u checks passed\n",checks);
}
