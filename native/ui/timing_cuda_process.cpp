// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_cuda_process.h"
#include "cuda_driver.h"
#include <algorithm>
#include <cstring>
#include <cstdio>
#include <limits>
#include <new>
#include <vector>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <wincrypt.h>
#pragma comment(lib,"advapi32.lib")
#endif
namespace druta::ui {
#ifdef _WIN32
namespace {
using namespace timing_cuda_protocol;
bool absolute(const std::wstring& path){return path.size()>3&&path.find(L'\0')==std::wstring::npos&&path.find(L'"')==std::wstring::npos&&((path[1]==L':'&&(path[2]==L'\\'||path[2]==L'/'))||(path[0]==L'\\'&&path[1]==L'\\'));}
bool decimal(const wchar_t* text,uint64_t& value){value=0;if(!text||!*text)return false;for(;*text;++text){if(*text<L'0'||*text>L'9')return false;const auto digit=uint64_t(*text-L'0');if(value>(std::numeric_limits<uint64_t>::max()-digit)/10)return false;value=value*10+digit;}return value!=0;}
bool random_cookie(uint64_t& cookie){HCRYPTPROV provider=0;if(!CryptAcquireContextW(&provider,nullptr,nullptr,PROV_RSA_FULL,CRYPT_VERIFYCONTEXT))return false;const bool ok=CryptGenRandom(provider,sizeof(cookie),reinterpret_cast<BYTE*>(&cookie))!=FALSE;CryptReleaseContext(provider,0);return ok&&cookie!=0;}
class NativeTimingCudaProcess final:public TimingCudaProcess {
    std::wstring executable_;HANDLE executable_file_=INVALID_HANDLE_VALUE,process_=nullptr,job_=nullptr,mapping_=nullptr,stop_=nullptr;Frame* frame_=nullptr;
    uint64_t cookie_=0;ULONGLONG started_=0;unsigned duration_=0;std::string slot_,failure_;bool stop_requested_=false,forced_=false,copy_seen_=false;
    TimingCudaProcessStatus status_;
    void close_resources(){if(process_)CloseHandle(process_);process_=nullptr;if(job_)CloseHandle(job_);job_=nullptr;if(frame_)UnmapViewOfFile(frame_);frame_=nullptr;if(mapping_)CloseHandle(mapping_);mapping_=nullptr;if(stop_)CloseHandle(stop_);stop_=nullptr;if(executable_file_!=INVALID_HANDLE_VALUE)CloseHandle(executable_file_);executable_file_=INVALID_HANDLE_VALUE;}
    void fail(const std::string& why){if(failure_.empty())failure_=why;status_.ok=false;status_.message=failure_;}
    bool valid_frame(){return frame_&&frame_->signature==magic&&frame_->size==sizeof(Frame)&&frame_->revision==version&&frame_->duration_ms==duration_&&frame_->cookie==cookie_&&std::memchr(frame_->slot,0,sizeof(frame_->slot))&&slot_==frame_->slot;}
    void terminate(){forced_=true;if(job_)TerminateJobObject(job_,42);if(process_)TerminateProcess(process_,42);}
public:
    explicit NativeTimingCudaProcess(std::wstring path):executable_(std::move(path)){}
    ~NativeTimingCudaProcess() override {if(process_)stop(0,1000);close_resources();}
    bool start(const std::string& slot,unsigned max_duration_ms,std::string& error) override {
        if(process_){poll();if(process_){error="Previous CUDA worker exit is unconfirmed";return false;}}
        status_={};failure_.clear();copy_seen_=stop_requested_=forced_=false;const auto target=parse_cuda_pci_slot(slot);
        if(!target||!absolute(executable_)||!max_duration_ms||max_duration_ms>180000){error="CUDA worker requires an absolute executable, exact PCI slot and bounded duration";return false;}
        slot_=canonical_cuda_pci_slot(*target);duration_=max_duration_ms;started_=GetTickCount64();
        const auto fail_start=[&](const char* why){fail(why);error=failure_;if(process_){terminate();status_.cleanup_pending=true;}else close_resources();return false;};
        executable_file_=CreateFileW(executable_.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);BY_HANDLE_FILE_INFORMATION info{};
        if(executable_file_==INVALID_HANDLE_VALUE||!GetFileInformationByHandle(executable_file_,&info)||(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY))return fail_start("Could not pin CUDA worker executable");
        if(!random_cookie(cookie_))return fail_start("Could not create private CUDA worker cookie");SECURITY_ATTRIBUTES security{sizeof(security),nullptr,TRUE};
        mapping_=CreateFileMappingW(INVALID_HANDLE_VALUE,&security,PAGE_READWRITE,0,sizeof(Frame),nullptr);stop_=CreateEventW(&security,TRUE,FALSE,nullptr);
        if(!mapping_||!stop_)return fail_start("Could not create private CUDA worker IPC");frame_=static_cast<Frame*>(MapViewOfFile(mapping_,FILE_MAP_ALL_ACCESS,0,0,sizeof(Frame)));if(!frame_)return fail_start("Could not map private CUDA worker IPC");
        new(frame_)Frame{};frame_->cookie=cookie_;frame_->duration_ms=duration_;std::memcpy(frame_->slot,slot_.c_str(),slot_.size()+1);
        job_=CreateJobObjectW(nullptr,nullptr);JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if(!job_||!SetInformationJobObject(job_,JobObjectExtendedLimitInformation,&limits,sizeof(limits)))return fail_start("Could not establish owned CUDA worker job");
        // Resolve Vista+ handle-list functions rather than adding newer imports
        // to legacy binaries. Never fall back to inheriting every host handle.
        const auto kernel=GetModuleHandleW(L"kernel32.dll");
        using Init=BOOL(WINAPI*)(LPPROC_THREAD_ATTRIBUTE_LIST,DWORD,DWORD,PSIZE_T);
        using Update=BOOL(WINAPI*)(LPPROC_THREAD_ATTRIBUTE_LIST,DWORD,DWORD_PTR,PVOID,SIZE_T,PVOID,PSIZE_T);
        using Delete=VOID(WINAPI*)(LPPROC_THREAD_ATTRIBUTE_LIST);
        const auto init=reinterpret_cast<Init>(GetProcAddress(kernel,"InitializeProcThreadAttributeList"));const auto update=reinterpret_cast<Update>(GetProcAddress(kernel,"UpdateProcThreadAttribute"));const auto destroy=reinterpret_cast<Delete>(GetProcAddress(kernel,"DeleteProcThreadAttributeList"));
        if(!init||!update||!destroy)return fail_start("Private CUDA worker handle inheritance is unavailable");SIZE_T bytes=0;init(nullptr,1,0,&bytes);if(!bytes||bytes>65536)return fail_start("Invalid CUDA worker attribute geometry");
        std::vector<uint8_t> storage(bytes);auto* attributes=reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage.data());if(!init(attributes,1,0,&bytes))return fail_start("Could not initialize private CUDA worker attributes");
        struct DeleteAttributes {Delete fn;LPPROC_THREAD_ATTRIBUTE_LIST value;~DeleteAttributes(){fn(value);}}delete_attributes{destroy,attributes};HANDLE inherited[]={mapping_,stop_};
        if(!update(attributes,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,inherited,sizeof(inherited),nullptr,nullptr))return fail_start("Could not restrict CUDA worker handle inheritance");
        const auto arguments=L"\""+executable_+L"\" --timing-cuda-worker "+std::to_wstring(reinterpret_cast<uintptr_t>(mapping_))+L" "+std::to_wstring(reinterpret_cast<uintptr_t>(stop_))+L" "+std::to_wstring(cookie_);
        std::vector<wchar_t> command(arguments.begin(),arguments.end());command.push_back(0);STARTUPINFOEXW startup{};startup.StartupInfo.cb=sizeof(startup);startup.lpAttributeList=attributes;PROCESS_INFORMATION created{};
        if(!CreateProcessW(executable_.c_str(),command.data(),nullptr,nullptr,TRUE,CREATE_NO_WINDOW|CREATE_SUSPENDED|EXTENDED_STARTUPINFO_PRESENT,nullptr,nullptr,&startup.StartupInfo,&created))return fail_start("Could not create exact-target CUDA worker");
        SetHandleInformation(mapping_,HANDLE_FLAG_INHERIT,0);SetHandleInformation(stop_,HANDLE_FLAG_INHERIT,0);
        process_=created.hProcess;const bool assigned=AssignProcessToJobObject(job_,process_)!=FALSE;const bool resumed=assigned&&ResumeThread(created.hThread)!=DWORD(-1);CloseHandle(created.hThread);
        if(!assigned||!resumed)return fail_start("Could not bind CUDA worker to its owned job before execution");
        status_.message="CUDA worker starting on selected PCI device";return true;
    }
    TimingCudaProcessStatus poll() override {
        if(!process_)return status_;const auto wait=WaitForSingleObject(process_,0);const bool exited=wait==WAIT_OBJECT_0;
        if(wait!=WAIT_OBJECT_0&&wait!=WAIT_TIMEOUT)fail("CUDA worker process state is unreadable; exit remains unconfirmed");
        const bool valid=valid_frame();const long phase=valid?InterlockedCompareExchange(&frame_->state,0,0):0;
        if(!valid||phase<setup||phase>failed){fail("CUDA worker protocol or selected target changed");if(!exited)terminate();}
        const bool terminal_frame=valid&&(phase==done||phase==failed);
        const bool valid_terminal=terminal_frame&&!(frame_->result_flags&~(clean|cancelled|timed_out))&&std::memchr(frame_->message,0,sizeof(frame_->message));
        if(terminal_frame&&!valid_terminal){fail("CUDA worker terminal protocol is malformed");if(!exited)terminate();}
        if(valid_terminal&&phase==failed&&frame_->message[0]&&(failure_.empty()||failure_=="CUDA workload ended before an explicit stop")){
            failure_=frame_->message;status_.message=failure_;
        }
        if(GetTickCount64()-started_>=duration_){fail("CUDA workload hard deadline expired");if(!exited)terminate();}
        if(!failure_.empty())status_.copying=false;
        else if(!exited&&phase==copying){copy_seen_=true;status_.copying=true;status_.message="CUDA copy workload active on selected PCI device";}
        else{status_.copying=false;if(!stop_requested_&&phase>=cleanup)fail("CUDA workload ended before an explicit stop");else status_.message=phase==setup?"CUDA worker setup pending":"CUDA worker cleanup pending";}
        if(!exited){status_.cleanup_pending=stop_requested_||forced_||phase>=cleanup||!failure_.empty();return status_;}
        DWORD code=1;const bool exit_known=GetExitCodeProcess(process_,&code)!=FALSE;
        const bool terminal=valid_terminal;
        status_.finished=true;status_.cleanup_pending=false;status_.copying=false;
        status_.ok=failure_.empty()&&!forced_&&stop_requested_&&copy_seen_&&exit_known&&code==0&&terminal&&phase==done&&frame_->result_flags==(clean|cancelled);
        if(status_.ok)status_.message=frame_->message;
        else if(!failure_.empty())status_.message=failure_;
        else if(terminal)status_.message=frame_->message[0]?frame_->message:"CUDA worker exited without qualified cleanup";
        else status_.message="CUDA worker exited early or without a valid cleanup acknowledgement";
        close_resources();return status_;
    }
    TimingCudaProcessStatus stop(unsigned grace_ms,unsigned terminate_wait_ms) override {
        poll();if(!process_)return status_;stop_requested_=true;status_.copying=false;SetEvent(stop_);
        const auto wait=WaitForSingleObject(process_,std::min(grace_ms,10000u));if(wait!=WAIT_OBJECT_0){fail("CUDA worker required termination; capture invalid");terminate();WaitForSingleObject(process_,std::min(terminate_wait_ms,10000u));}
        return poll();
    }
    bool pending()const override{return process_&&WaitForSingleObject(process_,0)!=WAIT_OBJECT_0;}
};
}
std::unique_ptr<TimingCudaProcess> make_timing_cuda_process(const std::wstring& path){return std::make_unique<NativeTimingCudaProcess>(path);}
int timing_cuda_worker_entry(int argc,wchar_t** argv,const CudaDriverApi* injected){
    using namespace timing_cuda_protocol;const auto started=GetTickCount64();uint64_t map_value=0,event_value=0,cookie=0;
    if(argc!=5||!argv||!argv[1]||!argv[2]||!argv[3]||!argv[4]||std::wstring(argv[1])!=L"--timing-cuda-worker"||!decimal(argv[2],map_value)||!decimal(argv[3],event_value)||!decimal(argv[4],cookie))return 2;
    if(static_cast<uint64_t>(static_cast<uintptr_t>(map_value))!=map_value||static_cast<uint64_t>(static_cast<uintptr_t>(event_value))!=event_value)return 2;
    const auto mapping=reinterpret_cast<HANDLE>(static_cast<uintptr_t>(map_value)),stop=reinterpret_cast<HANDLE>(static_cast<uintptr_t>(event_value));SetHandleInformation(mapping,HANDLE_FLAG_INHERIT,0);SetHandleInformation(stop,HANDLE_FLAG_INHERIT,0);auto* frame=static_cast<Frame*>(MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Frame)));if(!frame)return 2;
    struct Close {Frame* frame;HANDLE mapping,stop;~Close(){UnmapViewOfFile(frame);CloseHandle(mapping);CloseHandle(stop);}}close{frame,mapping,stop};
    if(frame->signature!=magic||frame->size!=sizeof(Frame)||frame->revision!=version||frame->cookie!=cookie||!frame->duration_ms||frame->duration_ms>180000||!std::memchr(frame->slot,0,sizeof(frame->slot)))return 2;
    const auto target=parse_cuda_pci_slot(frame->slot);if(!target||canonical_cuda_pci_slot(*target)!=frame->slot||WaitForSingleObject(stop,0)==WAIT_FAILED)return 2;
    CudaDriverLibrary library;CudaDriverApi api;std::string error;CudaCopyResult result;
    if(injected)api=*injected;else if(!library.open(api,error))result.message=error;
    if(result.message.empty()){
        const auto elapsed=GetTickCount64()-started;if(elapsed>=frame->duration_ms){result.timed_out=true;result.message="CUDA setup duration expired";}
        else{CudaCopyCallbacks callbacks;callbacks.stopped=[stop]{return WaitForSingleObject(stop,0)!=WAIT_TIMEOUT;};callbacks.copying=[frame](bool active){InterlockedExchange(&frame->state,active?copying:cleanup);};
            result=run_cuda_copy_workload(frame->slot,frame->duration_ms-static_cast<unsigned>(elapsed),api,callbacks);}
    }
    InterlockedExchange(&frame->state,cleanup);frame->result_flags=(result.ok?clean:0u)|(result.cancelled?cancelled:0u)|(result.timed_out?timed_out:0u);std::snprintf(frame->message,sizeof(frame->message),"%s",result.message.c_str());InterlockedExchange(&frame->state,result.ok?done:failed);return result.ok?0:1;
}
#else
std::unique_ptr<TimingCudaProcess> make_timing_cuda_process(const std::wstring&){return {};}
int timing_cuda_worker_entry(int,wchar_t**,const CudaDriverApi*){return 2;}
#endif
}
