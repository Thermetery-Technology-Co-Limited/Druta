// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "cuda_driver.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
namespace {
int inits=0,creates=0,allocs=0,frees=0,destroys=0,copies=0,fail_alloc=0;bool mismatch=false,fail_copy=false,fail_free=false,fail_destroy=false,active=false,stopped=false;
int __stdcall init(unsigned){++inits;return 0;}
int __stdcall lookup(int* d,const char*){*d=7;return 0;}
int __stdcall bus(char* out,int n,int){strncpy_s(out,size_t(n),mismatch?"0000:02:00.0":"0000:01:00.0",_TRUNCATE);return 0;}
int __stdcall create(void** out,unsigned,int){++creates;*out=reinterpret_cast<void*>(1);return 0;}
int __stdcall destroy(void*){++destroys;return fail_destroy?1:0;}
int __stdcall sync(){return 0;}
int __stdcall info(size_t* free,size_t* total){*free=*total=size_t(1024)*1024*1024;return 0;}
int __stdcall alloc(uint64_t* out,size_t){++allocs;if(allocs==fail_alloc)return 1;*out=uint64_t(allocs);return 0;}
int __stdcall free_buffer(uint64_t){++frees;return fail_free?1:0;}
int __stdcall copy(uint64_t,uint64_t,size_t){++copies;if(fail_copy)return 1;return 0;}
CudaDriverApi api(){CudaDriverApi a;a.init=init;a.device_by_bus=lookup;a.device_bus=bus;a.ctx_create=create;a.ctx_destroy=destroy;a.ctx_sync=sync;a.mem_info=info;a.mem_alloc=alloc;a.mem_free=free_buffer;a.copy=copy;return a;}
void reset(){inits=creates=allocs=frees=destroys=copies=fail_alloc=0;mismatch=fail_copy=fail_free=fail_destroy=active=stopped=false;}
CudaCopyCallbacks callbacks(){return {[]{return stopped;},[](bool state){active=state;if(state)stopped=true;}};}
}
int main(){
 reset();auto a=api();auto result=run_cuda_copy_workload("0000:01:00.0",1000,a,callbacks());CHECK(result.ok&&copies==1&&frees==2&&destroys==1&&!active);
 reset();mismatch=true;result=run_cuda_copy_workload("0000:01:00.0",1000,a,callbacks());CHECK(!result.ok&&creates==0&&copies==0);
 reset();a.copy=nullptr;result=run_cuda_copy_workload("0000:01:00.0",1000,a,callbacks());CHECK(!result.ok&&inits==0&&creates==0);a=api();
 reset();fail_alloc=2;result=run_cuda_copy_workload("0000:01:00.0",1000,a,callbacks());CHECK(!result.ok&&allocs==2&&frees==1&&destroys==1&&copies==0);
 reset();fail_copy=true;result=run_cuda_copy_workload("0000:01:00.0",1000,a,callbacks());CHECK(!result.ok&&frees==2&&destroys==1&&!active);
 reset();fail_free=true;result=run_cuda_copy_workload("0000:01:00.0",1000,a,callbacks());CHECK(!result.ok&&frees==2&&destroys==1&&!active);
 reset();fail_destroy=true;result=run_cuda_copy_workload("0000:01:00.0",1000,a,callbacks());CHECK(!result.ok&&frees==2&&destroys==1&&!active);
 std::puts("Timing CUDA worker injection passed");return 0;
}
