// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "cuda_driver.h"
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <type_traits>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif
namespace druta::ui {
std::optional<CudaPciSlot> parse_cuda_pci_slot(const std::string& text){CudaPciSlot slot;size_t pos=0;
 const auto number=[&](unsigned& out,char separator,unsigned width,unsigned maximum){size_t begin=pos;out=0;while(pos<text.size()&&text[pos]!=separator){const char c=text[pos++];const unsigned d=c>='0'&&c<='9'?unsigned(c-'0'):c>='a'&&c<='f'?unsigned(c-'a'+10):c>='A'&&c<='F'?unsigned(c-'A'+10):16u;if(d==16||pos-begin>width||out>(maximum-d)/16)return false;out=out*16+d;}if(pos==begin||out>maximum)return false;if(separator){if(pos==text.size()||text[pos++]!=separator)return false;}return true;};
 if(!number(slot.domain,':',8,0xffff)||!number(slot.bus,':',2,0xff)||!number(slot.device,'.',2,0x1f)||!number(slot.function,0,1,7)||pos!=text.size())return {};return slot;
}
bool same_cuda_pci_slot(CudaPciSlot a,CudaPciSlot b){return a.domain==b.domain&&a.bus==b.bus&&a.device==b.device&&a.function==b.function;}
std::string canonical_cuda_pci_slot(CudaPciSlot slot){char text[16]{};std::snprintf(text,sizeof(text),"%04x:%02x:%02x.%x",slot.domain,slot.bus,slot.device,slot.function);return text;}
bool complete_cuda_driver(const CudaDriverApi& a){return a.init&&a.device_by_bus&&a.device_bus&&a.ctx_create&&a.ctx_destroy&&a.ctx_sync&&a.mem_info&&a.mem_alloc&&a.mem_free&&a.copy;}
size_t cuda_copy_buffer_bytes(size_t free_bytes){constexpr size_t mib=size_t(1024)*1024;if(free_bytes<256*mib)return 0;const auto bytes=std::max(64*mib,std::min(1024*mib,(free_bytes/20)*3));return (bytes/mib)*mib;}
CudaDriverLibrary::~CudaDriverLibrary(){
#ifdef _WIN32
 if(module_)FreeLibrary(static_cast<HMODULE>(module_));
#endif
}
bool CudaDriverLibrary::open(CudaDriverApi& api,std::string& error){api={};
#ifdef _WIN32
 if(!module_){wchar_t path[MAX_PATH]{};const auto n=GetSystemDirectoryW(path,MAX_PATH);if(!n||n>=MAX_PATH-12){error="CUDA system-directory path unavailable";return false;}std::wstring full(path,n);full+=L"\\nvcuda.dll";module_=LoadLibraryW(full.c_str());}
 if(!module_){error="CUDA driver DLL unavailable";return false;}
 const auto get=[&](auto& fn,const char* name){fn=reinterpret_cast<std::remove_reference_t<decltype(fn)>>(GetProcAddress(static_cast<HMODULE>(module_),name));if(!fn)error=std::string("CUDA driver export unavailable: ")+name;return fn!=nullptr;};
 return get(api.init,"cuInit")&&get(api.device_by_bus,"cuDeviceGetByPCIBusId")&&get(api.device_bus,"cuDeviceGetPCIBusId")&&get(api.ctx_create,"cuCtxCreate_v2")&&get(api.ctx_destroy,"cuCtxDestroy_v2")&&get(api.ctx_sync,"cuCtxSynchronize")&&get(api.mem_info,"cuMemGetInfo_v2")&&get(api.mem_alloc,"cuMemAlloc_v2")&&get(api.mem_free,"cuMemFree_v2")&&get(api.copy,"cuMemcpyDtoD_v2");
#else
 error="CUDA driver loading unavailable on this platform";return false;
#endif
}
CudaCopyResult run_cuda_copy_workload(const std::string& slot,unsigned max_duration_ms,const CudaDriverApi& api,const CudaCopyCallbacks& callbacks){
 using Clock=std::chrono::steady_clock;const auto deadline=Clock::now()+std::chrono::milliseconds(max_duration_ms);CudaCopyResult result;
 const auto target=parse_cuda_pci_slot(slot);if(!target||!max_duration_ms||!complete_cuda_driver(api)||!callbacks.stopped){result.message="CUDA workload requires an exact PCI slot, duration and complete driver API";return result;}
 CudaDriverApi::Context context=nullptr;CudaDriverApi::Pointer a=0,b=0;bool running=false;
 const auto stop=[&]{if(callbacks.stopped()){result.cancelled=true;return true;}if(Clock::now()>=deadline){result.timed_out=true;return true;}return false;};
 const auto check=[](int status,const char* message){if(status)throw std::runtime_error(message);};
 try{if(!stop()){
  check(api.init(0),"CUDA initialization failed");if(stop())throw std::runtime_error("CUDA stopped during initialization");
  CudaDriverApi::Device device=0;char actual[32]{};check(api.device_by_bus(&device,slot.c_str()),"CUDA could not resolve selected PCI device");if(stop())throw std::runtime_error("CUDA stopped during device lookup");
  check(api.device_bus(actual,sizeof(actual),device),"CUDA PCI identity read failed");if(!std::memchr(actual,0,sizeof(actual)))throw std::runtime_error("CUDA returned an unterminated PCI identity");const auto found=parse_cuda_pci_slot(actual);
  if(!found||!same_cuda_pci_slot(*target,*found))throw std::runtime_error("CUDA returned a different PCI device");if(stop())throw std::runtime_error("CUDA stopped before context creation");
  CudaDriverApi::Context created=nullptr;check(api.ctx_create(&created,0,device),"CUDA context creation failed");context=created;if(!context)throw std::runtime_error("CUDA returned no context");if(stop())throw std::runtime_error("CUDA stopped during context setup");
  size_t free_bytes=0,total_bytes=0;check(api.mem_info(&free_bytes,&total_bytes),"CUDA free-memory query failed");const auto bytes=cuda_copy_buffer_bytes(free_bytes);if(!bytes||free_bytes>total_bytes)throw std::runtime_error("Insufficient or malformed CUDA free-memory information");if(stop())throw std::runtime_error("CUDA stopped before allocation");
  CudaDriverApi::Pointer allocated=0;check(api.mem_alloc(&allocated,bytes),"CUDA first allocation failed");a=allocated;if(!a)throw std::runtime_error("CUDA returned no first buffer");if(stop())throw std::runtime_error("CUDA stopped during allocation");
  allocated=0;check(api.mem_alloc(&allocated,bytes),"CUDA second allocation failed");b=allocated;if(!b||a==b)throw std::runtime_error("CUDA returned invalid copy buffers");if(stop())throw std::runtime_error("CUDA stopped before copying");
  check(api.copy(a,b,bytes),"CUDA first copy failed");if(!stop()){running=true;if(callbacks.copying)callbacks.copying(true);
   while(!stop()){for(unsigned n=0;n<8&&!stop();++n)check(api.copy(a,b,bytes),"CUDA copy failed");if(!stop())check(api.ctx_sync(),"CUDA synchronization failed");}}
 }}catch(const std::exception& e){result.message=e.what();}catch(...){result.message="CUDA workload failed";}
 // A clean requested stop is distinct from deadline expiry or a driver error.
 if(running&&callbacks.copying)try{callbacks.copying(false);}catch(...){if(result.message.empty())result.message="CUDA progress callback failed";}
 const auto cleanup=[&](auto fn,auto resource,const char* message){if(resource)try{if(fn(resource)!=0&&result.message.empty())result.message=message;}catch(...){if(result.message.empty())result.message=message;}};
 cleanup(api.mem_free,b,"CUDA second buffer cleanup failed");if(a!=b)cleanup(api.mem_free,a,"CUDA first buffer cleanup failed");cleanup(api.ctx_destroy,context,"CUDA context cleanup failed");
 if(Clock::now()>=deadline)result.timed_out=true;
 if(result.timed_out){result.ok=false;result.message="CUDA workload duration expired";}
 else if(!result.message.empty())result.ok=false;
 else if(result.cancelled){result.ok=true;result.message="CUDA workload stopped and owned resources cleaned";}
 else{result.ok=false;result.message="CUDA workload ended without an explicit stop";}return result;
}
}
