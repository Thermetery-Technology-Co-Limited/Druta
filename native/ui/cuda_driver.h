// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#ifdef _WIN32
#define DRUTA_CUDA_CALL __stdcall
#else
#define DRUTA_CUDA_CALL
#endif
namespace druta::ui {
struct CudaDriverApi {
    using Device=int;using Context=void*;using Pointer=uint64_t;
    int(DRUTA_CUDA_CALL *init)(unsigned)=nullptr;
    int(DRUTA_CUDA_CALL *device_by_bus)(Device*,const char*)=nullptr;
    int(DRUTA_CUDA_CALL *device_bus)(char*,int,Device)=nullptr;
    int(DRUTA_CUDA_CALL *ctx_create)(Context*,unsigned,Device)=nullptr;
    int(DRUTA_CUDA_CALL *ctx_destroy)(Context)=nullptr;
    int(DRUTA_CUDA_CALL *ctx_sync)()=nullptr;
    int(DRUTA_CUDA_CALL *mem_info)(size_t*,size_t*)=nullptr;
    int(DRUTA_CUDA_CALL *mem_alloc)(Pointer*,size_t)=nullptr;
    int(DRUTA_CUDA_CALL *mem_free)(Pointer)=nullptr;
    int(DRUTA_CUDA_CALL *copy)(Pointer,Pointer,size_t)=nullptr;
};
struct CudaPciSlot {unsigned domain=0,bus=0,device=0,function=0;};
std::optional<CudaPciSlot> parse_cuda_pci_slot(const std::string&);
bool same_cuda_pci_slot(CudaPciSlot,CudaPciSlot);
std::string canonical_cuda_pci_slot(CudaPciSlot);
bool complete_cuda_driver(const CudaDriverApi&);
size_t cuda_copy_buffer_bytes(size_t free_bytes);
class CudaDriverLibrary {
    void* module_=nullptr;
public:
    CudaDriverLibrary()=default;~CudaDriverLibrary();
    CudaDriverLibrary(const CudaDriverLibrary&)=delete;CudaDriverLibrary& operator=(const CudaDriverLibrary&)=delete;
    bool open(CudaDriverApi&,std::string& error);
};
struct CudaCopyCallbacks {std::function<bool()> stopped;std::function<void(bool)> copying;};
struct CudaCopyResult {bool ok=false,cancelled=false,timed_out=false;std::string message;};
// Synchronous child-thread implementation and deterministic injection seam.
// All CUDA calls/cleanup remain on this thread; max duration includes setup.
CudaCopyResult run_cuda_copy_workload(const std::string&,unsigned max_duration_ms,const CudaDriverApi&,const CudaCopyCallbacks&);
}
#undef DRUTA_CUDA_CALL
