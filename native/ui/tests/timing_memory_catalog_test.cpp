// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Actual CoreClient + native helper transport; the child mode only operates on
// this test's owned temporary word file. It never loads a driver or opens a GPU.
#define main established_core_fixture_entry
#include "core_client_test.cpp"
#undef main
#include "profile_catalog.h"
#include "timing_profile.h"
#include "timing_model.h"
#include <cwchar>
#include <string>
namespace {
constexpr wchar_t state_variable[]=L"DRUTA_TIMING_MEMORY_TEST_STATE";
struct Disk {uint32_t word=0xA5A50014,geometry=0,commits=0,count=0,fail_dump=0;char events[256]{};};
std::wstring state_path;
bool bad_band=false,corrupt_timing=false,change_geometry=false,fail_memory=false,ignore_memory=false;
bool race_memory=false,race_domain=false,boost_changes_memory=false;
int reads_after_commit=0;
bool load_disk(Disk& s){HANDLE f=CreateFileW(state_path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);if(f==INVALID_HANDLE_VALUE)return false;DWORD got=0;const bool ok=ReadFile(f,&s,sizeof(s),&got,nullptr)!=0&&got==sizeof(s)&&s.count<=sizeof(s.events);CloseHandle(f);return ok;}
bool store_disk(const Disk& s){HANDLE f=CreateFileW(state_path.c_str(),GENERIC_WRITE,0,nullptr,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);if(f==INVALID_HANDLE_VALUE)return false;DWORD got=0;const bool ok=WriteFile(f,&s,sizeof(s),&got,nullptr)!=0&&got==sizeof(s);CloseHandle(f);return ok;}
Disk disk(){Disk s;CHECK(load_disk(s));return s;}
void event(char value){auto s=disk();CHECK(s.count<sizeof(s.events));if(s.count<sizeof(s.events))s.events[s.count++]=value;CHECK(store_disk(s));}
std::string events(){auto s=disk();return {s.events,s.events+s.count};}
void clear_events(){auto s=disk();s.count=0;CHECK(store_disk(s));}
void reset(){current=40;domain_raw[2]=0;mode=0;writes=advanced_writes=0;changed_id=false;layout_version=4;advanced_refuse=false;xoc_flag=1;range_fail=advanced_fail_reads=0;boost_value=10;
    bad_band=corrupt_timing=change_geometry=fail_memory=ignore_memory=race_memory=race_domain=boost_changes_memory=false;reads_after_commit=0;CHECK(store_disk(Disk{}));}
dc_status DC_CALL tm_telemetry(dc_gpu*,dc_telemetry* out){out->present=DC_T_MEM|DC_T_MEM_OFF|DC_T_PSTATE;out->mem=bad_band?405u:static_cast<uint32_t>(7000+current/2+domain_raw[2]/1000);out->mem_off=current;out->pstate=bad_band?8u:0u;return DC_OK;}
dc_status DC_CALL tm_caps(dc_gpu*,dc_capabilities* out){out->mem_clock_count=1;out->mem_clocks[0]=7000;return DC_OK;}
dc_status DC_CALL tm_range(dc_gpu* gpu,uint32_t type,dc_offset_range* out){if(type==2&&race_memory&&disk().commits&&++reads_after_commit==2){current=120;race_memory=false;}return range(gpu,type,out);}
dc_status DC_CALL tm_domains(dc_gpu* gpu,dc_clkdom_requests* out){if(race_domain&&disk().commits&&++reads_after_commit==2){domain_raw[2]=20000;race_domain=false;}return dom_read(gpu,out);}
void after_memory(){auto s=disk();if(corrupt_timing)s.word=(s.word&~255u)|77u;if(change_geometry)s.geometry=1;CHECK(store_disk(s));}
dc_status DC_CALL tm_memory(dc_gpu*,uint32_t type,double value,int32_t preserve,dc_outcome* out){CHECK(type==2&&preserve==1);++writes;event('M');if(!ignore_memory)current=static_cast<int>(value*2);out->ok=!fail_memory;after_memory();return DC_OK;}
dc_status DC_CALL tm_domain(dc_gpu*,int64_t which,double value,dc_outcome* out){CHECK(which==2);++advanced_writes;event('D');if(!ignore_memory)domain_raw[2]=static_cast<int>(value*1000);out->ok=!fail_memory;after_memory();return DC_OK;}
dc_status DC_CALL tm_boost(dc_gpu* gpu,int64_t value,dc_outcome* out){const auto status=boost_set(gpu,value,out);if(boost_changes_memory)current=120;return status;}
CoreApi test_api(){auto a=api();a.lock=lock_gpu;a.unlock=unlock_gpu;a.telemetry=tm_telemetry;a.capabilities=tm_caps;a.range=tm_range;a.set_offset=tm_memory;a.boost_set=tm_boost;
    a.clkdom_ok=dom_ok;a.clkdom_layout=dom_layout;a.clkdom_domains=dom_list;a.clkdom_controls=dom_controls;a.clkdom_offered=dom_offered;a.clkdom_requests=tm_domains;a.clkdom_set=tm_domain;a.clkdom_flags=flags_get;return a;}
Command catalog(const ProfileJson& document,bool preview=false){Command c;c.operation=preview?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(document);return c;}
ProfileJson memory_document(bool additional,int value){ProfileJson p={{"format","druta-native-daily"},{"version",additional?2:1},{"target",{{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"controls",ProfileJson::object()}};
    if(additional)p["advanced"]=ProfileJson::array({{{"kind","clock_domain"},{"domain",2},{"value",value},{"unit","kHz"}}});
    else p["controls"]["memory"]={{"value",value},{"unit","MHz eff"},{"scale",2}};return p;}
std::unique_ptr<IBackend> client(const wchar_t* executable){auto b=create_core_backend_for_test(test_api());std::string why;CHECK(b->open("0000:02:00.0",why));Command c;c.operation=Command::Operation::timings_select_helper;c.timing_helper_path=executable;auto selected=b->execute(c);if(!selected.ok)std::fprintf(stderr,"select: %s\n",selected.message.c_str());CHECK(selected.ok);c.operation=Command::Operation::timings_read;const auto read=b->execute(c);if(!read.ok)std::fprintf(stderr,"read: %s\n",read.message.c_str());CHECK(read.ok);return b;}
ProfileJson timing_document(IBackend& b,bool legacy=false){if(legacy)return {{"fields",{{"RC",25}}}};const auto data=b.read(false);CHECK(data.timings&&data.timings->snapshot.ok);dc_gpu_identity id{};id.size=sizeof(id);identity(handle,&id);return capture_timing_profile(id,data.timings->snapshot,{{"RC",25}});}
void result_ok(const ActionResult& r,const char* label){if(!r.ok)std::fprintf(stderr,"%s: %s\n",label,r.message.c_str());CHECK(r.ok);}
Command undo(){Command c;c.operation=Command::Operation::undo;return c;}
void run_cases(const wchar_t* executable){
    for(bool additional:{false,true})for(bool reverse:{false,true})for(bool legacy:{false,true}){reset();auto b=client(executable);auto t=timing_document(*b,legacy),m=memory_document(additional,additional?10000:80);
        auto combined=compose_profiles(reverse?std::vector<ProfileComponent>{{"Memory",m},{"Timing",t}}:std::vector<ProfileComponent>{{"Timing",t},{"Memory",m}});
        result_ok(b->execute(catalog(combined,true)),"whole preview");CHECK(events().empty());result_ok(b->execute(catalog(combined)),"mixed Apply");CHECK(events()==(additional?"TD":"TM")&&(disk().word&255)==25);
        result_ok(b->execute(undo()),"memory-first Undo");CHECK(events()==(additional?"TDDT":"TMMT")&&current==40&&domain_raw[2]==0&&disk().word==0xA5A50014&&!b->read(false).undo_available);b->close();}
    for(bool additional:{false,true}){reset();auto b=client(executable);const auto t=timing_document(*b);const auto combined=compose_profiles({{"Memory",memory_document(additional,additional?10000:80)},{"Timing",t}});
        fail_memory=true;const auto failed=b->execute(catalog(combined));CHECK(!failed.ok&&failed.may_have_changed&&events()==(additional?"TD":"TM")&&b->read(false).undo_available);fail_memory=false;result_ok(b->execute(undo()),"possibly-dispatched memory recovery");CHECK(current==40&&domain_raw[2]==0&&disk().word==0xA5A50014);
        result_ok(b->execute(catalog(combined)),"second mixed Apply");clear_events();ignore_memory=true;CHECK(!b->execute(undo()).ok&&events()==(additional?"D":"M")&&(disk().word&255)==25&&b->read(false).undo_available);ignore_memory=false;result_ok(b->execute(undo()),"retry preserves timing receipt");CHECK(events()==(additional?"DDT":"MMT")&&disk().word==0xA5A50014);
        result_ok(b->execute(catalog(combined)),"foreign request fixture");clear_events();if(additional)domain_raw[2]=20000;else current=120;CHECK(!b->execute(undo()).ok&&events().empty()&&b->read(false).undo_available);
        if(additional)domain_raw[2]=10000;else current=80;result_ok(b->execute(undo()),"foreign request resolved");b->close();}
    {reset();auto b=client(executable);auto memory=memory_document(true,10000);memory["controls"]["memory"]={{"value",80},{"unit","MHz eff"},{"scale",2}};
        const auto p=compose_profiles({{"Both memory APIs",memory},{"Timing",timing_document(*b)}});result_ok(b->execute(catalog(p,true)),"same-document memory preview");CHECK(events().empty());
        result_ok(b->execute(catalog(p)),"same-document memory Apply");CHECK(events()=="TMD"&&current==80&&domain_raw[2]==10000);clear_events();
        result_ok(b->execute(undo()),"same-document memory-first Undo");const auto restored=events();CHECK(restored.size()==3&&restored.back()=='T'&&restored.find('M')<2&&restored.find('D')<2&&current==40&&domain_raw[2]==0&&disk().word==0xA5A50014&&!b->read(false).undo_available);b->close();}
    {reset();auto b=client(executable);auto t=timing_document(*b);t["band"]["memory_offset"]=42;const auto p=compose_profiles({{"Memory",memory_document(false,80)},{"Wrong band",t}});CHECK(!b->execute(catalog(p)).ok&&events().empty());
        t=timing_document(*b);t["fields"][0]["value"]=256;bool rejected=false;try{(void)compose_profiles({{"Timing",t},{"Memory",memory_document(false,80)}});}catch(const std::exception&){rejected=true;}CHECK(rejected&&events().empty());b->close();}
    for(bool additional:{false,true}){reset();auto b=client(executable);const auto p=compose_profiles({{"Timing",timing_document(*b)},{"Memory",memory_document(additional,additional?10000:80)}});
        race_memory=!additional;race_domain=additional;const auto r=b->execute(catalog(p));CHECK(!r.ok&&r.may_have_changed&&events()=="T"&&b->read(false).undo_available);CHECK(additional?domain_raw[2]==20000:current==120);
        current=40;domain_raw[2]=0;result_ok(b->execute(undo()),"whole-preflight memory baseline retained");CHECK(events()=="TT");b->close();}
    {reset();auto b=client(executable);const auto p=compose_profiles({{"Timing",timing_document(*b)},{"Memory no-op",memory_document(false,40)}});result_ok(b->execute(catalog(p)),"selected memory no-op");CHECK(events()=="T");result_ok(b->execute(undo()),"no-op memory Undo");CHECK(events()=="TT");b->close();}
    {reset();auto b=client(executable);auto boost=memory_document(false,40);boost["controls"]={{"boost",{{"value",30},{"unit","%"}}}};
        const auto p=compose_profiles({{"Timing",timing_document(*b)},{"Memory no-op",memory_document(false,40)},{"Boost",boost}});boost_changes_memory=true;const auto r=b->execute(catalog(p));CHECK(!r.ok&&r.may_have_changed&&current==120&&events()=="T");boost_changes_memory=false;current=40;result_ok(b->execute(undo()),"selected no-op interference recovery");b->close();}
    {reset();auto b=client(executable);const auto p=compose_profiles({{"Timing",timing_document(*b)},{"Memory",memory_document(false,80)}});corrupt_timing=true;CHECK(!b->execute(catalog(p)).ok&&b->read(false).undo_available);corrupt_timing=false;clear_events();CHECK(!b->execute(undo()).ok&&current==40&&(disk().word&255)==77&&events()=="M");
        CHECK(!b->execute(undo()).ok&&events()=="M");auto s=disk();s.word=(s.word&~255u)|25u;CHECK(store_disk(s));result_ok(b->execute(undo()),"completed memory does not replay during timing retry");CHECK(events()=="MT");b->close();}
    {reset();auto b=client(executable);const auto p=compose_profiles({{"Timing",timing_document(*b)},{"Memory",memory_document(false,80)}});change_geometry=true;CHECK(!b->execute(catalog(p)).ok&&b->read(false).undo_available);change_geometry=false;
        CHECK(!b->execute(undo()).ok&&current==40);auto s=disk();s.geometry=0;CHECK(store_disk(s));result_ok(b->execute(undo()),"original helper geometry restored");b->close();}
}
int helper(int argc,wchar_t** argv){const std::wstring op=argv[1];if(op==L"--help"){std::printf("Everything defaults to a dry run\n--commit is required to touch hardware\n");return 0;}if(op==L"fields"){std::printf("RC REG0 [7:0] 255 Cycles\n");return 0;}
    wchar_t path[32768]{};const auto n=GetEnvironmentVariableW(state_variable,path,32768);if(!n||n>=32768)return 2;state_path=path;Disk s;if(!load_disk(s))return 3;
    if(argc<4||std::wstring(argv[2])!=L"-d"||std::wstring(argv[3])!=L"0000:02:00.0")return 4;
    if(op==L"dump"){if(s.fail_dump)return 5;std::printf("0000:02:00.0 GP102 (Pascal)\n [broadcast] base 0x001000\n REG0 @0x%06X = 0x%08X (DOCUMENTED)\n",0x1000+4*s.geometry,s.word);return 0;}
    if(op==L"save"&&argc==6){HANDLE f=CreateFileW(argv[5],GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);if(f==INVALID_HANDLE_VALUE)return 6;char text[256]{};const auto count=std::snprintf(text,sizeof(text),"{\"slot\":\"0000:02:00.0\",\"codename\":\"GP102\",\"registers\":{\"broadcast\":{\"REG0\":\"0x%08X\"}}}",s.word);DWORD written=0;const bool ok=count>0&&WriteFile(f,text,static_cast<DWORD>(count),&written,nullptr)!=0&&written==static_cast<DWORD>(count);CloseHandle(f);return ok?0:7;}
    if(op==L"set"&&argc>=5&&std::wcsncmp(argv[4],L"RC=",3)==0){const auto value=std::wcstoul(argv[4]+3,nullptr,10);if(value>255)return 8;const auto target=(s.word&~255u)|static_cast<uint32_t>(value);
        if(argc==6&&std::wstring(argv[5])==L"--commit"){s.word=target;++s.commits;if(s.count>=sizeof(s.events))return 9;s.events[s.count++]='T';return store_disk(s)?0:10;}
        std::printf("REG0 @0x%06X 0x%08X -> 0x%08X [would write]\n  RC %u -> %lu\n",0x1000+4*s.geometry,s.word,target,s.word&255u,value);return 0;}
    return 11;
}
}
int wmain(int argc,wchar_t** argv){if(argc>1)return helper(argc,argv);wchar_t path[32768]{},temp[MAX_PATH]{},file[MAX_PATH]{};
    const auto n=GetModuleFileNameW(nullptr,path,32768);CHECK(n&&n<32768);CHECK(GetTempPathW(MAX_PATH,temp)&&GetTempFileNameW(temp,L"tmm",0,file));state_path=file;
    wchar_t previous[32768]{};const auto previous_size=GetEnvironmentVariableW(state_variable,previous,32768);CHECK(previous_size<32768);CHECK(SetEnvironmentVariableW(state_variable,state_path.c_str()));run_cases(path);
    CHECK(SetEnvironmentVariableW(state_variable,previous_size?previous:nullptr));CHECK(DeleteFileW(state_path.c_str()));std::printf("Timing/memory catalog: %d checks, %d failures\n",checks,failures);return failures?1:0;}
