/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../i2c_warmup.h"
#include <atomic>
#include <cassert>
#include <cstring>
#include <thread>
using namespace druta::ui;
namespace {
std::atomic<int> copies{0}, frees{0}, destroys{0}; bool wrong=false, copy_fail=false; size_t free_bytes=size_t(1024)*1024*1024;
int __stdcall init(unsigned) {return 0;}
int __stdcall by_bus(int* dev,const char*) {*dev=3;return 0;}
int __stdcall bus(char* out,int n,int) {strncpy_s(out,size_t(n),wrong?"0000:02:00.0":"0000:01:00.0",_TRUNCATE);return 0;}
int __stdcall create(void** ctx,unsigned,int) {*ctx=reinterpret_cast<void*>(1);return 0;}
int __stdcall destroy(void*) {++destroys;return 0;}
int __stdcall sync() {return 0;}
int __stdcall info(size_t* free,size_t* total) {*free=free_bytes;*total=free_bytes;return 0;}
int __stdcall alloc(uint64_t* p,size_t) {static uint64_t next=1;*p=next++;return 0;}
int __stdcall free_mem(uint64_t) {++frees;return 0;}
int __stdcall copy(uint64_t,uint64_t,size_t) {++copies;std::this_thread::sleep_for(std::chrono::milliseconds(1));return copy_fail?1:0;}
I2cCudaApi api() {I2cCudaApi a; a.init=init;a.device_by_bus=by_bus;a.device_bus=bus;a.ctx_create=create;a.ctx_destroy=destroy;a.ctx_sync=sync;a.mem_info=info;a.mem_alloc=alloc;a.mem_free=free_mem;a.copy=copy;return a;}
}
int main() {
    auto cuda=api(); I2cWarmupCallbacks c; c.bound=[] {return true;}; c.sample=[] {return std::optional<I2cWarmupSample>{{5000,2}};};
    c.voltage_mv=[] {assert(copies>0);return std::optional<double>{950};};
    auto good=i2c_cuda_warmup("00000000:01:00.0",c,&cuda,2000,3000,10);
    assert(good.ok && good.voltage_mv==950 && frees==2 && destroys==1);
    wrong=true; auto mismatch=i2c_cuda_warmup("0000:01:00.0",c,&cuda,2000,3000,10);assert(!mismatch.ok && !mismatch.voltage_mv && destroys==1);wrong=false;
    copy_fail=true; auto bad=i2c_cuda_warmup("0000:01:00.0",c,&cuda,2000,3000,10);assert(!bad.ok && !bad.voltage_mv && frees==4 && destroys==2);copy_fail=false;
    free_bytes=128*size_t(1024)*1024; auto low=i2c_cuda_warmup("0000:01:00.0",c,&cuda,2000,3000,10);assert(!low.ok && frees==4 && destroys==3);
    free_bytes=size_t(1024)*1024*1024; c.voltage_mv=[] {return std::optional<double>{};};
    auto absent=i2c_cuda_warmup("0000:01:00.0",c,&cuda,2000,3000,10); assert(!absent.ok && !absent.voltage_mv && frees==6 && destroys==4);
    int sample_count=0; c.sample=[&] {return std::optional<I2cWarmupSample>{{double(++sample_count),2}};};
    auto unstable=i2c_cuda_warmup("0000:01:00.0",c,&cuda,60,1000,10); assert(!unstable.ok && !unstable.voltage_mv && frees==8 && destroys==5);
    c.cancelled=[] {return true;};auto cancelled=i2c_cuda_warmup("0000:01:00.0",c,&cuda,2000,3000,10);assert(!cancelled.ok && !cancelled.voltage_mv);
}
