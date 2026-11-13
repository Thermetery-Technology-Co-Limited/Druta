// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "drutacore/vbios_image.h"
#include "backend/vbios_image.h"
#include "core/abi_internal.h"
#include "core/mutex.h"
#include <cstring>
namespace {
void diagnostic(dc_error* out,int kind,const std::string& message){if(!out)return;dc_error error{};error.size=sizeof(error);error.kind=kind;error.fixed=kind==DC_ERR_UNSUPPORTED||kind==DC_ERR_MALFORMED?1:0;DC_COPY(error.message,message);*out=error;}
}
extern "C" DC_API dc_status DC_CALL dc_gpu_vbios_image_get(dc_gpu* handle,uint8_t* image,uint32_t capacity,uint32_t* image_size,dc_error* error){
 if(!handle||!image||!image_size||(error&&error->size!=sizeof(dc_error)))return DC_E_ARGUMENT;
 if(capacity<32)return DC_E_BUFFER;
 try{druta::LockGuard guard(handle->gpu.lock());const auto captured=druta::backend::read_vbios_image(handle->gpu);
  if(!captured.ok()){diagnostic(error,static_cast<int>(captured.error.kind),captured.error.message);return captured.error.kind==druta::backend::RmErrorKind::unsupported?DC_E_UNAVAILABLE:DC_E_FAILED;}
  if(captured.image.size()>capacity){diagnostic(error,DC_ERR_INVALID,"Caller buffer cannot hold the captured VBIOS image");return DC_E_BUFFER;}
  std::memcpy(image,captured.image.data(),captured.image.size());*image_size=static_cast<uint32_t>(captured.image.size());diagnostic(error,DC_ERR_NONE,{});return DC_OK;
 }catch(const std::exception& e){try{diagnostic(error,DC_ERR_TRANSIENT,std::string("VBIOS image capture failed: ")+e.what());}catch(...){}return DC_E_FAILED;}
 catch(...){try{diagnostic(error,DC_ERR_TRANSIENT,"VBIOS image capture failed");}catch(...){}return DC_E_FAILED;}
}
