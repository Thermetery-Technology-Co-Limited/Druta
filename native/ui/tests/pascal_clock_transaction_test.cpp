// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "pascal_clock_fixture.h"
#include "power_weight_transaction.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <stdexcept>

using namespace druta::ui;
namespace {
void check(bool ok,const char* message) { if(!ok)throw std::runtime_error(message); }
struct Bench {
 PowerWeightCapture current;
 PowerWeightReceipt durable;
 bool active=false,refuse_restart=false,fail_stage=false,fail_cleanup=false,swap_target=false,fail_clear=false;
 unsigned restarts=0,changes=0,staged_reads=0,readiness_failures=0;
 bool fatal_staged_failure=false;
 Bench() {
  auto& id=current.adapter;
  id.size=sizeof(id);std::strcpy(id.slot,"0000:12:00.0");std::strcpy(id.uuid,"GPU-clock-test");
  id.nvapi_ok=id.nvapi_selected=1;id.nvapi_handle=12;id.nvapi_devid=0x1bbc;id.nvapi_subsys=0x12345678;
  auto& t=current.target;t.size=sizeof(t);std::strcpy(t.slot,id.slot);t.handle=id.nvapi_handle;
  t.device_id=id.nvapi_devid;t.subsystem_id=id.nvapi_subsys;t.architecture=4;
  current.identity={id.slot,t.architecture,t.device_id,t.subsystem_id};
  current.windows=VoltageScaffoldWindowsTarget{id.slot,"PCI\\VEN_10DE&DEV_1BBC&SUBSYS_12345678\\FIXTURE",
   "SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0028"};
  current.image_availability=current.registry_availability=VoltagePolicyAvailability::available;
  current.info_availability=VoltagePolicyAvailability::unsupported;
  current.registry=PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{0x11,0,0,0}}};
  current.image=test::pascal_clock_fixture();
 }
 PowerWeightReceipt make() {
  return make_pascal_clock_receipt(current,{{PascalClockDomain::gpc,0,3800,3300},
                                             {PascalClockDomain::sys,5,1800,1600},
                                             {PascalClockDomain::xbar,-1,95,105},
                                             {PascalClockDomain::sys,-1,90,105}});
 }
 PowerWeightTransactionApi api() {
  PowerWeightTransactionApi out;
  out.capture=[&](dc_gpu*,bool) { return current; };
  out.recovery_registry=[&](const PowerWeightReceipt&) {
   auto target=*current.windows;if(swap_target)target.instance_id+="X";
   return PowerWeightRegistryRecovery{target,*current.registry};
  };
  out.persist=[&](const PowerWeightReceipt& r,bool create) {
   if(create)check(!active,"a second transaction acquired the device");
   else check(active&&r.id==durable.id,"lost receipt ownership");
   active=true;durable=decode_weight_receipt(encode_weight_receipt(r));
  };
  out.clear_active=[&](const PowerWeightReceipt& r) {
   check(active&&r.id==durable.id&&durable.phase=="removed","cleared an unverified receipt");if(fail_clear)return false;active=false;return true;
  };
  out.write=[&](const auto& target,const auto& expected,const auto& desired,const auto& before_effect) {
   PowerWeightRegistryWriteApi writer;
   writer.read=[&](const std::wstring&){return *current.registry;};
   writer.write=[&](const std::wstring&,const std::wstring& name,const std::optional<PowerWeightRegistryValue>& value,std::string& error) {
    check(active&&std::find(durable.issued_names.begin(),durable.issued_names.end(),name)!=durable.issued_names.end(),
          "mutation missing from durable journal");
    if(fail_cleanup&&!value){error="simulated failed cleanup";return false;}
    auto& values=*current.registry;
    auto it=std::find_if(values.begin(),values.end(),[&](const auto& v){return v.name==name;});
    if(value) { if(it!=values.end())*it=*value;else values.push_back(*value); }
    else if(it!=values.end())values.erase(it);
    ++changes;
    if(fail_stage&&value){error="error after accepted stage";return false;}
    return true;
   };
   return write_power_weight_registry(target,expected,desired,before_effect,writer);
  };
  out.restart=[&](const auto&) {
   check(durable.refresh_requested,"missing durable refresh intent");++restarts;
   if(refuse_restart)return PowerWeightBoundaryResult{false,true,false,false,true,"reboot required"};
   current.image=current.registry->size()==1?durable.image:derive_weight_receipt(durable).clock_plan.image;
   return PowerWeightBoundaryResult{true};
  };
  out.fresh_verify_staged=[&](const PowerWeightReceipt& r) {
   ++staged_reads;
   check(r.phase=="clock_initializing"&&durable.phase==r.phase,"staged proof requires durable phase");
   check(power_weight_verify_registry(r,*current.registry,false,true),"staged records removed before driver proof");
   if(fatal_staged_failure)return PowerWeightBoundaryResult{false,false,false,false,false,"foreign target",false};
   if(staged_reads<=readiness_failures)return PowerWeightBoundaryResult{false,false,false,false,false,"still initializing",true};
   const bool expected=current.image==derive_weight_receipt(r).clock_plan.image;
   return PowerWeightBoundaryResult{expected,false,false,false,false,expected?"exact":"image differs"};
  };
  out.fresh_verify=[&](const PowerWeightReceipt& r,bool original) {
   const auto expected=original?r.image:derive_weight_receipt(r).clock_plan.image;
   const bool ok=current.image==expected&&power_weight_verify_registry(r,*current.registry,original);
   return PowerWeightBoundaryResult{ok,false,false,false,false,ok?"exact":"image/registry differs"};
  };
  return out;
 }
};
void tests() {
 {
  Bench b;const auto r=b.make();
  const auto copy=decode_weight_receipt(encode_weight_receipt(r));
  check(copy.is_pascal_clocks()&&copy.clocks.size()==4&&copy.info.empty(),"clock receipt roundtrip failed");
  check(copy.clocks[2].expected==95&&copy.clocks[2].value==105&&
        copy.clocks[3].expected==90&&copy.clocks[3].value==105,"clock receipt lost the raised ratios");
  check(!power_weight_verify_registry(r,derive_weight_receipt(r).after,false),"active verification accepted staged records");
  PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  const auto result=worker.run(PowerWeightOperation::apply,r);
  check(result.ok&&b.restarts==1&&b.active&&b.durable.phase=="active"&&b.current.registry->size()==1,
        "apply did not unstage verified clocks");
  check(b.staged_reads==1,"activation must verify the image before removing the staged records");
  check(b.current.image==derive_weight_receipt(r).clock_plan.image,"applied image differs");
  const auto installed=inspect_pascal_clocks(b.current.image);
  check(installed.ok&&std::count_if(installed.fields.begin(),installed.fields.end(),[](const auto& field) {
   return field.pstate==-1&&field.current==105;
  })==2,"the running table lost the raised ratios");
  check(worker.run(PowerWeightOperation::verify,b.durable).ok&&b.restarts==1,"read-only verification changed session");
  const auto cleared=worker.run(PowerWeightOperation::remove,b.durable);
  check(cleared.ok&&b.current.image==r.image&&b.restarts==2&&!b.active,"explicit restore incomplete");
 }
 {
  Bench b;
  b.current.image=derive_weight_receipt(b.make()).clock_plan.image;
  const auto r=make_pascal_clock_receipt(b.current,{{PascalClockDomain::xbar,-1,105,100},
                                                   {PascalClockDomain::sys,-1,105,100}});
  PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  check(worker.run(PowerWeightOperation::apply,r).ok,"cannot lower an installed 105 percent ratio");
  check(worker.run(PowerWeightOperation::remove,b.durable).ok&&b.current.image==r.image&&!b.active,
        "restore did not recover both previously installed 105 percent ratios");
 }
 {
  Bench b;const auto r=b.make();PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  check(worker.run(PowerWeightOperation::apply,r).ok,"cannot apply clocks");
  b.current.image=r.image; // an unrelated normal driver restart
  check(worker.run(PowerWeightOperation::verify,b.durable).ok&&!b.active&&b.restarts==1,
        "normal restart must retire clock session without another restart");
 }
 {
  Bench b;const auto r=b.make();PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  check(worker.run(PowerWeightOperation::apply,r).ok,"cannot apply for pointer cleanup test");
  b.fail_clear=true;
  const auto pending=worker.run(PowerWeightOperation::remove,b.durable);
  check(!pending.ok&&pending.cleanup_pending&&pending.recovery_required&&b.active&&
        b.durable.phase=="cleanup_pending"&&b.current.image==r.image,"failed pointer clear lost recoverability");
  b.fail_clear=false;
  const auto n=b.restarts;
  check(worker.run(PowerWeightOperation::verify,b.durable).ok&&!b.active&&b.restarts==n,
        "retry of pointer cleanup restarted the GPU");
 }
 {
  Bench b;const auto r=b.make();PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  check(worker.run(PowerWeightOperation::apply,r).ok,"cannot apply for interrupted removal test");
  b.current.image=r.image;b.durable.phase="removed";b.durable.restoring=true;b.durable.uncertain=false;
  check(worker.run(PowerWeightOperation::verify,b.durable).ok&&!b.active&&b.restarts==1,
        "crash between durable removal and pointer clear stranded the receipt");
 }
 {
  Bench b;const auto r=b.make();b.readiness_failures=2;PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  check(worker.run(PowerWeightOperation::apply,r).ok&&b.staged_reads==3&&b.restarts==1&&b.current.registry->size()==1,
        "bounded readiness should re-read only, then unstage and verify");
 }
 {
  Bench b;const auto r=b.make();b.readiness_failures=20;PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  const auto result=worker.run(PowerWeightOperation::apply,r);
  check(!result.ok&&result.recovery_required&&b.staged_reads==8&&b.restarts==1&&b.current.registry->size()==1,
        "exhausted fresh-image readiness must still remove staged records");
 }
 {
  Bench b;const auto r=b.make();b.fatal_staged_failure=true;PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  const auto result=worker.run(PowerWeightOperation::apply,r);
  check(!result.ok&&result.recovery_required&&b.staged_reads==1&&b.current.registry->size()==1,"identity failure must unstage without retry");
 }
 {
  Bench b;const auto r=b.make();b.refuse_restart=true;
  PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);const auto v=worker.run(PowerWeightOperation::apply,r);
  check(!v.ok&&v.reboot_required&&v.recovery_required&&b.current.registry->size()==1,"restart failure left staged clocks");
  check(worker.run(PowerWeightOperation::remove,b.durable).ok&&!b.active&&b.restarts==1,"recovered card restarted unnecessarily");
 }
 {
  Bench b;const auto r=b.make();b.fail_stage=true;b.fail_cleanup=true;
  PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);const auto v=worker.run(PowerWeightOperation::apply,r);
  check(!v.ok&&v.cleanup_pending&&b.active&&b.current.registry->size()==2,"partial write lost durable ownership");
  b.fail_stage=b.fail_cleanup=false;
  b.swap_target=true;
  check(!worker.run(PowerWeightOperation::remove,b.durable).ok&&b.current.registry->size()==2,
        "recovery followed a replacement Windows target");
  b.swap_target=false;
  (*b.current.registry)[1].bytes.back()^=0x10;
  check(!worker.run(PowerWeightOperation::remove,b.durable).ok&&b.current.registry->size()==2,
        "recovery deleted externally changed record");
  (*b.current.registry)[1].bytes.back()^=0x10;
  auto foreign=(*b.current.registry)[1];foreign.name=L"romOverride01";b.current.registry->push_back(foreign);
  check(!worker.run(PowerWeightOperation::remove,b.durable).ok&&b.current.registry->size()==3,
        "cleanup must not strand a later external override after a numbering gap");
  b.current.registry->pop_back();
  check(worker.run(PowerWeightOperation::remove,b.durable).ok&&!b.active&&b.restarts==0,"failed partial stage recovery");
 }
 {
  Bench b;const auto r=b.make();b.current.image.back()^=1;
  PowerWeightTransaction worker(b.api(),nullptr,b.current.adapter);
  check(!worker.run(PowerWeightOperation::apply,r).ok&&!b.active&&b.changes==0&&b.restarts==0,"stale clock image changed hardware");
 }
}
}
int main() { try { tests();return 0; } catch(const std::exception& e) { std::fprintf(stderr,"%s\n",e.what());return 1; } }
