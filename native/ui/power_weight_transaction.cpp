// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Explicit released weight transaction only. No session tuning replay.
#include "power_weight_transaction.h"
#include <algorithm>
#include <chrono>
#include <cstring>
#include <cwctype>
#include <map>
#include <stdexcept>
namespace druta::ui {
namespace {
void require(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
template<size_t N>std::string text(const char(&s)[N]){const auto* p=static_cast<const char*>(std::memchr(s,0,N));require(p!=nullptr,"Unterminated weight transaction identity");return {s,p};}
bool logical(const PowerRelationshipIdentity& a,const PowerRelationshipIdentity& b){return a.slot==b.slot&&a.architecture==b.architecture&&a.device_id==b.device_id&&a.subsystem_id==b.subsystem_id;}
bool windows(const VoltageScaffoldWindowsTarget& a,const VoltageScaffoldWindowsTarget& b){return a.slot==b.slot&&a.instance_id==b.instance_id&&a.driver_key==b.driver_key;}
bool layout(const PowerRelationshipLayout& a,const PowerRelationshipLayout& b){return a.info_command==b.info_command&&a.info_size==b.info_size&&a.policy_base==b.policy_base&&a.policy_stride==b.policy_stride;}
std::wstring fold(std::wstring s){for(auto& c:s)c=static_cast<wchar_t>(std::towlower(c));return s;}
using Registry=std::map<std::wstring,PowerWeightRegistryValue>;
Registry names(const PowerWeightRegistrySnapshot& values){Registry out;for(const auto& value:values)require(value.name.find(L'\0')==std::wstring::npos&&out.emplace(fold(value.name),value).second,"Invalid or duplicate managed registry names");return out;}
bool equal(const PowerWeightRegistrySnapshot& a,const PowerWeightRegistrySnapshot& b){const auto first=names(a),second=names(b);if(first.size()!=second.size())return false;for(const auto& [name,value]:first){const auto found=second.find(name);if(found==second.end()||value.type!=found->second.type||value.bytes!=found->second.bytes)return false;}return true;}
void binding(const PowerWeightCapture& c,const PowerWeightReceipt& receipt,const dc_gpu_identity& opened,bool allow_absent){require((allow_absent&&opened.size==0)||(opened.size==sizeof(opened)&&text(opened.uuid)==receipt.uuid&&!receipt.uuid.empty()&&text(opened.slot)==receipt.identity.slot),"Weight transaction opened identity differs or is unavailable");require(!receipt.uuid.empty()&&logical(c.identity,receipt.identity)&&c.windows&&windows(*c.windows,receipt.windows),"Weight transaction current logical/Windows target differs");const auto& id=c.adapter;require(id.size==sizeof(id)&&text(id.uuid)==receipt.uuid&&text(id.slot)==receipt.identity.slot&&text(id.pairing_error).empty()&&id.nvapi_ok&&id.nvapi_selected&&id.nvapi_devid==receipt.identity.device_id&&id.nvapi_subsys==receipt.identity.subsystem_id,"Weight transaction paired UUID/PCI identity changed");require(c.target.size==sizeof(c.target)&&text(c.target.slot)==receipt.identity.slot&&c.target.architecture==receipt.identity.architecture&&c.target.device_id==receipt.identity.device_id&&c.target.subsystem_id==receipt.identity.subsystem_id&&c.target.handle&&c.target.handle==id.nvapi_handle,"Weight transaction capture is not self-paired");require(c.registry_availability==VoltagePolicyAvailability::available&&c.registry.has_value(),"Current selected registry snapshot unavailable");}
PowerWeightRegistrySnapshot issued_after(const PowerWeightReceipt& receipt,const PowerWeightRegistrySnapshot& after){const auto original=names(receipt.before),projected=names(after);auto out=receipt.before;std::map<std::wstring,bool> seen;for(const auto& name:receipt.issued_names){const auto key=fold(name);require(seen.emplace(key,true).second,"Duplicate issued weight registry name");const auto wanted=projected.find(key),prior=original.find(key);require(wanted!=projected.end()&&(prior==original.end()||prior->second.type!=wanted->second.type||prior->second.bytes!=wanted->second.bytes),"Issued name is outside selected weight delta");auto found=std::find_if(out.begin(),out.end(),[&](const auto& item){return fold(item.name)==key;});if(found==out.end())out.push_back(wanted->second);else *found=wanted->second;}return out;}

// The driver reads these records once during initialization. Unstage our own
// additions immediately after dispatch, even when restart or verification fails.
// A durable receipt covers a host crash between staging and cleanup.
PowerWeightTransactionResult clock_session(PowerWeightTransactionApi& api,dc_gpu* gpu,
                                          const dc_gpu_identity& opened,PowerWeightOperation op,
                                          PowerWeightReceipt receipt) {
 PowerWeightTransactionResult result;
 const bool applying=op==PowerWeightOperation::apply;
 const bool removing=op==PowerWeightOperation::remove;
 // A replacement owns the recovery pointer before the helper starts.
 bool published=!applying||!receipt.previous_clocks.empty();
 bool cleanup_needed=false;
 PowerWeightReceiptDerived derived;
 const auto persist=[&](bool create=false) {
  require(bool(api.persist),"Clock recovery journal unavailable");
  api.persist(receipt,create);published=true;
 };
 const auto capture=[&]() {
  require(bool(api.capture),"Current clock image capture unavailable");
  auto c=api.capture(gpu,true);
  binding(c,receipt,opened,removing);
  return c;
 };
 const auto registry=[&]() {
  if(api.recovery_registry) {
   auto recovery=api.recovery_registry(receipt);
   require(windows(recovery.windows,receipt.windows),"Clock recovery Windows target changed");
   return recovery.registry;
  }
  return *capture().registry;
 };
 const auto boundary=[&](const PowerWeightBoundaryResult& status) {
  result.reboot_required|=status.reboot_required;
  result.timed_out|=status.timed_out;result.cleanup_pending|=status.cleanup_pending;
  require(status.ok&&!status.reboot_required&&!status.timed_out&&!status.cleanup_pending&&!status.uncertain,
          status.message.empty()?"Clock refresh or verification incomplete":status.message.c_str());
 };
 const auto unstage=[&]() {
  const auto current=registry();
  const auto wanted=restore_power_weight_registry(receipt.before,issued_after(receipt,derived.after),current);
  if(!equal(wanted,current)) {
   require(bool(api.write),"Clock registry cleanup unavailable");
   // The driver consumes consecutively numbered records. Clearing our record
   // ahead of a later third-party append would disable that other override.
   const auto remaining=names(wanted);
   bool gap=false;
   constexpr wchar_t hex[]=L"0123456789abcdef";
   for(unsigned n=0;n<256;++n) {
    std::wstring name=L"romoverride";name+=hex[n>>4];name+=hex[n&15];
    if(!remaining.count(name))gap=true;
    else require(!gap,"Another ROM override follows the staged clock records. Resolve that conflict before cleanup");
   }
   // Every name was recorded durably *before* its corresponding addition.
   const auto owned=[&](const std::wstring& name,std::string& error) {
    for(const auto& issued:receipt.issued_names)if(fold(issued)==fold(name))return true;
    error="Clock cleanup refused a registry name outside this receipt";return false;
   };
   const auto written=api.write(receipt.windows,current,wanted,owned);
   result.may_have_changed|=written.may_have_changed;
   require(written.ok,written.message.c_str());
   require(equal(wanted,registry()),"Clock registry cleanup readback differs");
  }
  cleanup_needed=false;
 };
 try {
  derived=derive_weight_receipt(receipt);
  require(derived.clock_plan.ok&&bool(api.fresh_verify)&&bool(api.persist)&&bool(api.clear_active),
          "Clock receipt or verification boundary unavailable");
  bool original=removing||receipt.restoring;
  if(applying) {
   require(receipt.phase=="prepared"&&receipt.issued_names.empty()&&!receipt.refresh_requested&&
           !receipt.uncertain&&!receipt.restoring,"Clock Apply requires an unused preview");
   const auto& source=derived.clock_source_image.empty()?receipt.image:derived.clock_source_image;
   const auto current=capture();
   require(current.image_availability==VoltagePolicyAvailability::available&&current.image==source&&
           equal(*current.registry,receipt.before),"The clock preview is stale. Read the current GPU and preview again");
   require(derived.clock_plan.image!=source,"The selected clocks already match the running image");
   original=derived.clock_plan.image==receipt.image;
   require((original||bool(api.write))&&bool(api.restart)&&bool(api.fresh_verify_staged),"Clock activation boundaries unavailable");
   receipt.message="Preparing the selected clocks";persist(true);
   receipt.phase="writing";persist();
   const auto record=[&](const std::wstring& name,std::string& error) {
    const auto count=receipt.issued_names.size();
    try {
     receipt.issued_names.push_back(name);receipt.uncertain=true;persist();
     cleanup_needed=true;return true;
    } catch(const std::exception& e) { receipt.issued_names.resize(count);error=e.what();return false; }
   };
   if(!original) {
    const auto written=api.write(receipt.windows,*current.registry,derived.after,record);
    result.may_have_changed=written.may_have_changed;
    require(written.ok,written.message.c_str());
   }
   const auto staged=capture();
   require(staged.image_availability==VoltagePolicyAvailability::available&&staged.image==source&&
           equal(*staged.registry,derived.after),"Clock pre-restart image or registry differs from staged preview");
   receipt.phase="refresh_pending";receipt.refresh_requested=true;receipt.uncertain=true;persist();
   result.may_have_changed=true;
   boundary(api.restart(receipt.windows));
   // DN_STARTED only covers Windows PnP. Wait for NVIDIA's first independent
   // open to consume the selected records before removing those records.
   receipt.phase="clock_initializing";receipt.message="Waiting for the driver to load the staged clock image";persist();
   const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(8);
   for(unsigned attempt=1;;++attempt) {
    const auto proof=api.fresh_verify_staged(receipt);
    if(proof.ok||!proof.retryable||proof.timed_out||proof.cleanup_pending||proof.reboot_required||
       proof.uncertain||attempt==8||std::chrono::steady_clock::now()>=deadline) {
     boundary(proof);break;
    }
    if(api.wait_clock_ready)api.wait_clock_ready();
   }
   unstage();
   receipt.phase="verifying";receipt.message="Clock records removed. Verifying the running image";persist();
  } else if(removing) {
   // Works after a failed restart even when the driver cannot be opened.
   require(bool(api.restart),"Clock reset unavailable");
   try { (void)capture(); }
   catch(const VoltageEquationEvidenceError& error) {
    if(!api.recovery_registry||(error.kind!=VoltagePolicyAvailability::transient&&
                                error.kind!=VoltagePolicyAvailability::unsupported))throw;
   }
   receipt.restoring=true;receipt.phase="removing";receipt.uncertain=true;persist();
   unstage();
   // A normal driver reset may have already restored the complete initial image.
   // Verify first to avoid restarting a recovered card a second time.
   auto already_original=api.fresh_verify(receipt,true);
   if(!already_original.ok||already_original.reboot_required||already_original.timed_out||
      already_original.cleanup_pending||already_original.uncertain) {
    // A timeout may leave a verifier running. Do not reset until it is gone.
    if(already_original.cleanup_pending||already_original.timed_out)boundary(already_original);
    receipt.phase="remove_refresh_pending";receipt.refresh_requested=true;persist();
    result.may_have_changed=true;boundary(api.restart(receipt.windows));
    boundary(api.fresh_verify(receipt,true));
   }
  } else {
   const auto current=capture();
   const auto wanted=restore_power_weight_registry(receipt.before,issued_after(receipt,derived.after),*current.registry);
   require(equal(wanted,*current.registry),"Clock records are still staged. Use Restore to remove them");
   require(current.image_availability==VoltagePolicyAvailability::available,"Current clock image unavailable");
   if(current.image==receipt.image)original=true;
  }
  if(!removing)boundary(api.fresh_verify(receipt,original));
  receipt.phase=original?"removed":"active";receipt.uncertain=false;
  receipt.message=original?"Original clock image verified and staged registry records cleared":
                           "Selected clock image verified. Staged records are cleared for the next driver restart";
  persist();
  if(original) {
   try { require(api.clear_active(receipt),"Clock reset verified, but its recovery pointer could not be cleared"); }
   catch(...) {
    // A crash after publishing "removed" also leaves an active pointer.
    // Verify re-checks the image and owned names before retiring either phase.
    receipt.phase="cleanup_pending";receipt.uncertain=true;result.cleanup_pending=true;
    throw;
   }
  }
  result.ok=true;result.message=receipt.message;
 } catch(const std::exception& failure) {
  result.message=failure.what();
  if(cleanup_needed) {
   try { unstage();result.message+=". Staged clock records cleared"; }
   catch(const std::exception& cleanup) { result.cleanup_pending=true;result.message+=". Clock record cleanup pending: "+std::string(cleanup.what()); }
  }
  result.recovery_required=published;
  receipt.message=result.message;
  if(published)try { persist(); } catch(const std::exception& e) { result.message+=". Could not update recovery journal: "+std::string(e.what()); }
 }
 result.receipt=std::make_shared<const PowerWeightReceipt>(std::move(receipt));
 return result;
}

}
bool power_weight_verify_registry(const PowerWeightReceipt& receipt,const PowerWeightRegistrySnapshot& current,bool original,bool staged) {
 try {
  const auto projected=derive_weight_receipt(receipt).after;
  if(staged)return receipt.is_pascal_clocks()&&!original&&equal(projected,current);
  if(receipt.is_pascal_clocks()&&!original) {
   // Prove the staged bytes are gone for every planned name, including a
   // restarted helper recovering from a partially recorded write.
   return equal(restore_power_weight_registry(receipt.before,projected,current),current);
  }
  if(!original)return equal(projected,current);
  return equal(restore_power_weight_registry(receipt.before,issued_after(receipt,projected),current),current);
 } catch(...) { return false; }
}
PowerWeightTransaction::PowerWeightTransaction(PowerWeightTransactionApi api,dc_gpu* gpu,const dc_gpu_identity& opened):api_(std::move(api)),gpu_(gpu),opened_(opened){}
PowerWeightTransactionResult PowerWeightTransaction::run(PowerWeightOperation operation,const PowerWeightReceipt& input,bool original){PowerWeightTransactionResult result;if(input.is_pascal_clocks())return clock_session(api_,gpu_,opened_,operation,input);
 PowerWeightReceipt receipt=input;bool published=false,journal_attempted=false;
 const auto persist=[&](bool create){require(bool(api_.persist),"Weight receipt persistence unavailable");journal_attempted=true;api_.persist(receipt,create);published=true;};
 const auto capture=[&]{if(!api_.capture)throw VoltageEquationEvidenceError("Fresh weight transaction capture unavailable",VoltagePolicyAvailability::unsupported);auto c=api_.capture(gpu_,true);if(c.adapter.size==sizeof(c.adapter)&&text(c.adapter.uuid).empty())throw VoltageEquationEvidenceError("Current live UUID unavailable",VoltagePolicyAvailability::unsupported);binding(c,receipt,opened_,operation==PowerWeightOperation::remove);return c;};
 const auto boundary=[&](const PowerWeightBoundaryResult& status){result.reboot_required=status.reboot_required;result.timed_out=status.timed_out;result.cleanup_pending=status.cleanup_pending;require(status.ok&&!status.reboot_required&&!status.timed_out&&!status.cleanup_pending&&!status.uncertain,status.message.empty()?"Weight refresh/verification did not complete":status.message.c_str());};
 try{const auto derived=derive_weight_receipt(receipt);require(derived.plan.ok,"Weight receipt does not derive a qualified selected plan");PowerWeightCapture current;try{current=capture();}catch(const VoltageEquationEvidenceError& failure){if(operation!=PowerWeightOperation::remove||!api_.recovery_registry||(failure.kind!=VoltagePolicyAvailability::unsupported&&failure.kind!=VoltagePolicyAvailability::transient))throw;const auto fallback=api_.recovery_registry(receipt);require(windows(fallback.windows,receipt.windows),"Removal recovery Windows target changed");current.windows=fallback.windows;current.registry=fallback.registry;current.registry_availability=VoltagePolicyAvailability::available;}
  const bool removing=operation==PowerWeightOperation::remove;const bool verify_original=removing||(operation==PowerWeightOperation::verify&&(original||receipt.restoring));
  if(operation==PowerWeightOperation::apply){require(receipt.phase=="prepared"&&receipt.issued_names.empty()&&!receipt.refresh_requested&&!receipt.uncertain,"Apply requires a fresh prepared weight receipt");require(current.image_availability==VoltagePolicyAvailability::available&&current.info_availability==VoltagePolicyAvailability::available&&current.image==receipt.image&&current.info==receipt.info&&layout(current.layout,receipt.layout)&&equal(*current.registry,receipt.before),"Weight Preview is stale; original image, INFO or registry changed");
   if(derived.plan.patches.empty()){result.ok=true;result.no_change=true;result.message="Selected weights already match; no journal, registry mutation or refresh";result.receipt=std::make_shared<const PowerWeightReceipt>(receipt);return result;}
   require(bool(api_.write)&&bool(api_.restart)&&bool(api_.fresh_verify),"Weight activation boundaries unavailable");receipt.message="Prepared weight activation";persist(true);
  }else{require(bool(api_.persist)&&bool(api_.fresh_verify),"Weight recovery persistence or fresh verification unavailable");if(verify_original)require(bool(api_.clear_active),"Weight active-pointer cleanup boundary unavailable");published=true;}
  if(operation!=PowerWeightOperation::verify){require(bool(api_.write)&&bool(api_.restart),"Weight write or selected refresh boundary unavailable");PowerWeightRegistrySnapshot desired;
   if(removing){receipt.restoring=true;const auto owned=issued_after(receipt,derived.after);desired=restore_power_weight_registry(receipt.before,owned,*current.registry);receipt.phase="removing";}
   else{desired=derived.after;receipt.phase="writing";}
   receipt.message=removing?"Restoring transaction-owned registry names":"Staging selected weight registry delta";persist(false);
   const auto prior_names=receipt.issued_names;const bool prior_uncertain=receipt.uncertain;bool pending_effect=false;const auto before_effect=[&](const std::wstring& name,std::string& error){const auto prior_count=receipt.issued_names.size();try{if(!removing&&std::none_of(receipt.issued_names.begin(),receipt.issued_names.end(),[&](const auto& old){return fold(old)==fold(name);}))receipt.issued_names.push_back(name);receipt.uncertain=true;receipt.message="Selected registry effect journaled before dispatch";persist(false);pending_effect=true;return true;}catch(const std::exception& e){receipt.issued_names.resize(prior_count);error=e.what();return false;}};
   PowerWeightRegistryWriteResult written;try{written=api_.write(receipt.windows,*current.registry,desired,before_effect);}catch(...){result.may_have_changed=pending_effect;throw;}result.may_have_changed=written.may_have_changed;if(!written.may_have_changed){receipt.issued_names=prior_names;receipt.uncertain=prior_uncertain;}require(written.ok,written.message.empty()?"Weight registry write failed; recovery retained":written.message.c_str());
   try{current=capture();}catch(const VoltageEquationEvidenceError& failure){if(!removing||!api_.recovery_registry||(failure.kind!=VoltagePolicyAvailability::unsupported&&failure.kind!=VoltagePolicyAvailability::transient))throw;const auto fallback=api_.recovery_registry(receipt);require(windows(fallback.windows,receipt.windows),"Removal recovery Windows target changed after staging");current.windows=fallback.windows;current.registry=fallback.registry;current.registry_availability=VoltagePolicyAvailability::available;}require(equal(*current.registry,desired),"Exact weight registry readback differs after staging");receipt.phase=removing?"remove_refresh_pending":"refresh_pending";receipt.refresh_requested=true;receipt.uncertain=true;receipt.message="Selected-device refresh journaled before dispatch";persist(false);
   result.may_have_changed=true;boundary(api_.restart(receipt.windows));
   receipt.phase=removing?"remove_verifying":"verifying";receipt.message="Selected refresh completed; fresh-process proof pending";persist(false);
  }else{if(verify_original){const auto wanted=restore_power_weight_registry(receipt.before,issued_after(receipt,derived.after),*current.registry);require(equal(wanted,*current.registry),"Owned registry names are not restored to their original values");}else require(equal(*current.registry,derived.after),"Active selected registry state differs from the receipt");}
  require(bool(api_.fresh_verify),"Independent fresh-process weight verification unavailable");boundary(api_.fresh_verify(receipt,verify_original));
  receipt.phase=verify_original?"removed":"active";receipt.uncertain=false;receipt.message=verify_original?"Original complete image and INFO verified after removal":"Expected complete image and INFO verified after activation";persist(false);
  if(verify_original){require(bool(api_.clear_active)&&api_.clear_active(receipt),"Original state verified but matching active pointer could not be cleared");}
  result.ok=true;result.message=receipt.message;result.recovery_required=false;
 }catch(const std::exception& error){receipt.message=error.what();if(result.may_have_changed||receipt.uncertain)receipt.uncertain=true;result.message=receipt.message;result.recovery_required=published||journal_attempted||!receipt.issued_names.empty()||receipt.refresh_requested;
  if(published)try{persist(false);}catch(const std::exception& journal){result.message+="; recovery journal update failed: "+std::string(journal.what());result.recovery_required=true;}}
 result.receipt=std::make_shared<const PowerWeightReceipt>(std::move(receipt));return result;}
}
