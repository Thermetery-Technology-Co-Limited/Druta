// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_cuda_process.h"
#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <cwchar>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
namespace {
int fake_child(int argc,wchar_t** argv){
 if(argc!=5)return 9;const auto map=reinterpret_cast<HANDLE>(static_cast<uintptr_t>(_wcstoui64(argv[2],nullptr,10)));const auto stop=reinterpret_cast<HANDLE>(static_cast<uintptr_t>(_wcstoui64(argv[3],nullptr,10)));const auto cookie=_wcstoui64(argv[4],nullptr,10);
 auto* frame=static_cast<timing_cuda_protocol::Frame*>(MapViewOfFile(map,FILE_MAP_ALL_ACCESS,0,0,sizeof(timing_cuda_protocol::Frame)));if(!frame||frame->cookie!=cookie)return 8;
 if(frame->slot[6]=='2'){UnmapViewOfFile(frame);return 7;}
 if(frame->slot[6]=='3'){frame->signature=0;InterlockedExchange(&frame->state,timing_cuda_protocol::copying);Sleep(2000);return 0;}
 InterlockedExchange(&frame->state,timing_cuda_protocol::copying);
 if(frame->slot[6]=='4'){Sleep(30000);return 0;}
 if(frame->slot[6]=='5'){InterlockedExchange(&frame->state,timing_cuda_protocol::cleanup);std::snprintf(frame->message,sizeof(frame->message),"CUDA driver export unavailable: cuMemcpyDtoD_v2");frame->result_flags=0;InterlockedExchange(&frame->state,timing_cuda_protocol::failed);UnmapViewOfFile(frame);return 1;}
 if(frame->slot[6]=='6'){WaitForSingleObject(stop,30000);InterlockedExchange(&frame->state,timing_cuda_protocol::cleanup);Sleep(30000);return 0;}

 WaitForSingleObject(stop,30000);InterlockedExchange(&frame->state,timing_cuda_protocol::cleanup);frame->result_flags=timing_cuda_protocol::clean|timing_cuda_protocol::cancelled;InterlockedExchange(&frame->state,timing_cuda_protocol::done);UnmapViewOfFile(frame);return 0;
}
TimingCudaProcessStatus wait_state(TimingCudaProcess& p,bool wanted_copy){TimingCudaProcessStatus status;for(int i=0;i<200;++i){status=p.poll();if(status.finished||(wanted_copy&&status.copying))break;Sleep(10);}return status;}
}
int wmain(int argc,wchar_t** argv){
 if(argc>1&&std::wcscmp(argv[1],L"--timing-cuda-worker")==0)return fake_child(argc,argv);
 wchar_t exe[32768]{};CHECK(GetModuleFileNameW(nullptr,exe,32768)>0);std::string error;
 auto good=make_timing_cuda_process(exe);CHECK(good->start("0000:01:00.0",5000,error));CHECK(wait_state(*good,true).copying);auto done=good->stop(500,2000);CHECK(done.finished&&done.ok&&!done.cleanup_pending&&!good->pending());
 auto early=make_timing_cuda_process(exe);CHECK(early->start("0000:02:00.0",5000,error));auto failed=wait_state(*early,false);CHECK(failed.finished&&!failed.ok&&!early->pending());
 auto malformed=make_timing_cuda_process(exe);CHECK(malformed->start("0000:03:00.0",5000,error));failed=wait_state(*malformed,false);CHECK(!failed.ok);failed=malformed->stop(20,2000);CHECK(failed.finished&&!failed.ok&&!malformed->pending());
 auto blocked=make_timing_cuda_process(exe);CHECK(blocked->start("0000:04:00.0",5000,error));CHECK(wait_state(*blocked,true).copying);const auto began=GetTickCount64();failed=blocked->stop(20,2000);CHECK(failed.finished&&!failed.ok&&!failed.cleanup_pending&&!blocked->pending()&&GetTickCount64()-began<4000);
 auto timeout=make_timing_cuda_process(exe);CHECK(timeout->start("0000:04:00.0",100,error));failed=wait_state(*timeout,false);CHECK(!failed.ok);failed=timeout->stop(20,2000);CHECK(failed.finished&&!failed.ok&&!timeout->pending());
 auto missing=make_timing_cuda_process(exe);CHECK(missing->start("0000:05:00.0",5000,error));failed=wait_state(*missing,false);CHECK(failed.finished&&!failed.ok&&failed.message.find("CUDA driver export unavailable: cuMemcpyDtoD_v2")!=std::string::npos&&!missing->pending());
 auto cleanup_blocked=make_timing_cuda_process(exe);CHECK(cleanup_blocked->start("0000:06:00.0",5000,error));CHECK(wait_state(*cleanup_blocked,true).copying);failed=cleanup_blocked->stop(100,2000);CHECK(failed.finished&&!failed.ok&&!failed.cleanup_pending&&!cleanup_blocked->pending());
 auto invalid=make_timing_cuda_process(exe);CHECK(!invalid->start("not-a-slot",100,error)&&!invalid->pending());std::puts("Timing CUDA child protocol passed");return 0;
}
