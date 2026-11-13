// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Real CoreClient dispatch, injected ABI only; no driver or GPU access.
#include "core_client.h"
#include "power_saving_fake.h"
#define main clock_gating_fixture_entry
#include "clock_gating_service_test.cpp"
#undef main
using namespace druta::ui;
namespace {
int failures=0;power_saving_test::Fake* active=nullptr;
void check(bool b,const char* why){if(!b){++failures;std::printf("FAIL: %s\n",why);}}
uint32_t DC_CALL core_version(){return DC_ABI_VERSION;}
dc_status DC_CALL core_enumerate(dc_gpu_entry*,size_t,size_t* n){*n=0;return DC_OK;}
dc_status DC_CALL core_open(const char*,dc_gpu** out){*out=active->gpu();return DC_OK;}
void DC_CALL core_close(dc_gpu*){}
const PowerSavingRow* find_row(const PowerSavingData& data,PowerSavingFamily family,uint32_t index=0){for(const auto& r:data.rows)if(r.key==PowerSavingKey{family,index})return &r;return nullptr;}
CoreApi client_api(){CoreApi a;a.abi_version=core_version;a.enumerate=core_enumerate;a.open=core_open;a.close=core_close;a.identity=power_saving_test::identity;a.power_saving=power_saving_test::api();a.clock_gating=::api();return a;}
#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif
NOINLINE void run(bool missing_uuid){fresh();power_saving_test::Fake f;if(missing_uuid)f.identity.uuid[0]=0;device=f.identity;active=&f;
 auto client=create_core_backend_for_test(client_api());std::string error;check(client->open(f.identity.slot,error),"open fake adapter");
 auto initial=client->read(false);client->read(true);check(f.gets==0&&bind_calls==0&&f.sets==0&&allocations==0,"open and ordinary reads perform no service traffic");
 Command c;c.operation=Command::Operation::power_saving_read;c.saving_families=2;check(client->execute(c).ok,"selected power-saving read");
 auto captured=client->read(false);check(captured.power_saving&&find_row(*captured.power_saving,PowerSavingFamily::individual)&&bind_calls==0,"power-saving read is independent of clock-gating");
 const auto old=captured.power_saving;const auto* original=find_row(*old,PowerSavingFamily::individual);if(!original){client->close();return;}
 PowerSavingRequest request{original->key,1,original->binding,original->value};c={};c.operation=Command::Operation::power_saving_apply;c.power_saving_requests=std::make_shared<const std::vector<PowerSavingRequest>>(std::vector<PowerSavingRequest>{request});
 check(client->execute(c).ok&&f.reasons[0]==1&&client->auxiliary_cleanup_needed(),"selected apply publishes an owned request");
 auto changed=client->read(false);check(!find_row(*old,PowerSavingFamily::individual)->owned && find_row(*changed.power_saving,PowerSavingFamily::individual)->owned,"published snapshot remains immutable");
 c={};c.operation=Command::Operation::power_saving_release;check(client->execute(c).ok&&f.reasons[0]==0&&!client->auxiliary_cleanup_needed(),"explicit release clears only owned request");
 const auto power_reads=f.gets;c={};c.operation=Command::Operation::clock_gating_read;c.clock_families=1;check(client->execute(c).ok,"selected clock-gating read");
 auto clock=client->read(false);check(clock.clock_gating&&clock.clock_gating->families[0].can_disable&&f.gets==power_reads,"clock read independent of power-saving service");
 ClockGatingRequest clock_request{ClockGatingFamily::engine,ClockGatingMode::disabled,0,clock.clock_gating->families[0].binding,{}};
 c={};c.operation=Command::Operation::clock_gating_apply;c.clock_gating_requests=std::make_shared<const std::vector<ClockGatingRequest>>(std::vector<ClockGatingRequest>{clock_request});
 check(client->execute(c).ok&&client->auxiliary_cleanup_needed()&&objects.size()==1,"clock apply retains owned object");
 check(client->prepare_close().ok&&objects.empty()&&!client->auxiliary_cleanup_needed(),"core close preparation frees owned object");
 c={};c.operation=Command::Operation::power_saving_read;c.saving_families=4|8;check(client->execute(c).ok,"idle and adaptive read");auto data=client->read(false);
 const auto* idle=find_row(*data.power_saving,PowerSavingFamily::idle_threshold);const auto* adaptive=find_row(*data.power_saving,PowerSavingFamily::adaptive);
 if(idle&&adaptive){PowerSavingRequest idle_req{idle->key,4000,idle->binding,idle->value};c={};c.operation=Command::Operation::power_saving_apply;c.power_saving_requests=std::make_shared<const std::vector<PowerSavingRequest>>(std::vector<PowerSavingRequest>{idle_req});check(client->execute(c).ok,"explicit idle setting");
  data=client->read(false);adaptive=find_row(*data.power_saving,PowerSavingFamily::adaptive);PowerSavingRequest adapt_req{adaptive->key,0,adaptive->binding,adaptive->value};c.power_saving_requests=std::make_shared<const std::vector<PowerSavingRequest>>(std::vector<PowerSavingRequest>{adapt_req});check(client->execute(c).ok,"explicit adaptive setting");
  const int before=f.sets;check(client->prepare_close().ok&&f.sets==before&&f.raw[0]==4000&&!f.adaptive[0],"close does not restore non-owned idle/adaptive settings");
 }else check(false,"idle and adaptive rows captured");
 client->close();check(client->open(f.identity.slot,error),"reconnect same fake GPU");const int before=f.sets;client->read(true);check(f.sets==before&&objects.empty(),"reconnect does not replay settings");client->close();active=nullptr;
}
}
int main(){run(false);run(true);std::printf("Saving services integration: %d failures\n",failures);return failures?1:0;}
