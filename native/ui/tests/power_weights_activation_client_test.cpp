// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main activation_core_fixture_main
#include "core_client_test.cpp"
#undef main
#define main activation_weight_fixture_main
#include "power_weights_service_test.cpp"
#undef main
namespace {
WeightFake* activation_weight=nullptr;
dc_status DC_CALL activation_identity(const dc_gpu*,dc_gpu_identity* out){*out=activation_weight->current.adapter;return DC_OK;}
struct ActivationStore {
 unsigned creates=0,discovers=0;bool fail_create=false;
 std::optional<PowerWeightReceipt> active;
 PowerWeightsApi bind(WeightFake& fake){auto out=fake.api();out.create_receipt=[this](const PowerWeightReceipt& receipt){++creates;if(fail_create)throw std::runtime_error("Injected durable receipt failure");if(active)throw std::runtime_error("Existing active receipt");active=decode_weight_receipt(encode_weight_receipt(receipt));};out.discover_receipt=[this](const std::string& slot,const std::string& uuid){++discovers;if(active&&(active->identity.slot!=slot||active->uuid!=uuid))throw std::runtime_error("Wrong receipt discovery target");return active;};return out;}
};
void narrow_activation_fake(WeightFake& fake){fake.current.registry=PowerWeightRegistrySnapshot{{L"EnableGpuFirmware",4,{0x11,0,0,0}}};}
Command activation_preview(){Command cmd;cmd.operation=Command::Operation::power_weights_preview;cmd.power_weight_requests=std::make_shared<const std::vector<PowerWeightRequest>>(std::vector<PowerWeightRequest>{{0,1.25,4096},{31,0.5,6144}});return cmd;}
std::unique_ptr<IBackend> activation_client(WeightFake& fake,ActivationStore& store){activation_weight=&fake;auto core=api();core.identity=activation_identity;core.power_weights=store.bind(fake);auto client=create_core_backend_for_test(core);std::string error;CHECK(client->open(fake.current.adapter.slot,error));return client;}
void activation_client_cases(){
 for(bool alternate:{false,true}){
  WeightFake fake(alternate);narrow_activation_fake(fake);ActivationStore store;auto client=activation_client(fake,store);const auto original_image=fake.current.image,original_info=fake.current.info;const auto original_registry=*fake.current.registry;
  auto preview=activation_preview();CHECK(client->execute(preview).ok);auto old=client->read(false).power_weights;CHECK(old&&old->preview&&old->preview->ok&&!old->receipt&&store.creates==0);
  Command prepare;prepare.operation=Command::Operation::power_weights_prepare;prepare.enabled=false;const auto reads=fake.reads;CHECK(!client->execute(prepare).ok&&store.creates==0&&fake.reads==reads);
  prepare.enabled=true;auto result=client->execute(prepare);if(!result.ok)std::fprintf(stderr,"Prepare activation: %s\n",result.message.c_str());CHECK(result.ok&&store.creates==1&&store.active);auto data=client->read(false).power_weights;CHECK(data&&data->receipt&&data->receipt->phase=="prepared"&&!data->preview&&old->preview&&!old->receipt);
  if(store.active){const auto& receipt=*store.active;auto derived=derive_weight_receipt(receipt);CHECK(receipt.uuid==fake.current.adapter.uuid&&receipt.identity.slot==fake.current.adapter.slot&&receipt.info==original_info&&receipt.image==original_image);CHECK(derived.plan.selected.size()==2&&derived.plan.selected[0].after_raw==5120&&derived.plan.selected[1].after_raw==2048&&derived.after.size()==3&&receipt.before.size()==1);}
  CHECK(fake.current.image==original_image&&fake.current.info==original_info&&fake.current.registry->size()==original_registry.size()&&fake.current.registry->front().bytes==original_registry.front().bytes&&writes==0&&power_writes==0&&policy_writes==0&&curve_writes==0);
  // An existing durable receipt must be discovered before the create callback.
  CHECK(client->execute(preview).ok);CHECK(!client->execute(prepare).ok&&store.creates==1);
  // Recovery inspection is independent from new INFO support and telemetry capture.
  if(store.active){store.active->phase="refresh_pending";store.active->message="Driver refresh not yet performed";}
  fake.current.info_availability=VoltagePolicyAvailability::unsupported;fake.current.info.clear();const auto before_inspect=fake.reads;Command inspect;inspect.operation=Command::Operation::power_weights_inspect;CHECK(client->execute(inspect).ok&&fake.reads==before_inspect&&store.creates==1);auto inspected=client->read(false).power_weights;CHECK(inspected&&inspected->receipt&&inspected->receipt->phase=="refresh_pending"&&inspected->receipt_message.find("refresh_pending")!=std::string::npos&&writes==0);client->close();
 }
 {
  WeightFake fake;narrow_activation_fake(fake);ActivationStore store;auto client=activation_client(fake,store);auto preview=activation_preview();CHECK(client->execute(preview).ok);fake.current.info.back()^=1;Command prepare;prepare.operation=Command::Operation::power_weights_prepare;prepare.enabled=true;CHECK(!client->execute(prepare).ok&&store.creates==0&&!store.active);auto data=client->read(false).power_weights;CHECK(data&&!data->receipt&&!data->preview&&writes==0);client->close();
 }
 {
  WeightFake fake;narrow_activation_fake(fake);ActivationStore store;store.fail_create=true;auto client=activation_client(fake,store);CHECK(client->execute(activation_preview()).ok);Command prepare;prepare.operation=Command::Operation::power_weights_prepare;prepare.enabled=true;auto result=client->execute(prepare);CHECK(!result.ok&&result.message.find("durable receipt failure")!=std::string::npos&&store.creates==1&&!store.active);auto data=client->read(false).power_weights;CHECK(data&&!data->receipt&&data->preview&&data->preview->ok&&writes==0);CHECK(fake.current.registry->size()==1);client->close();
 }
}
void activation_projection_case(){WeightFake fake(true);narrow_activation_fake(fake);ActivationStore store;PowerWeightsService service(store.bind(fake),reinterpret_cast<dc_gpu*>(1),fake.current.adapter);auto projection=service.preview({{0,1.25,4096}});CHECK(projection.ok);projection.selected[0].after_raw=65535;projection.selected[0].index=31;projection.registry_added.clear();projection.windows.slot="forged";projection.image_sha256="forged";CHECK(service.prepare_activation().ok&&store.creates==1&&store.active);if(store.active){const auto plan=derive_weight_receipt(*store.active);CHECK(plan.plan.selected.size()==1&&plan.plan.selected[0].index==0&&plan.plan.selected[0].after_raw==5120&&store.active->requests.size()==1&&store.active->requests[0].index==0);}}
}
int main(){activation_client_cases();activation_projection_case();CHECK(writes==0&&power_writes==0&&policy_writes==0&&curve_writes==0);std::printf("Power weights activation client: %d checks, %d failures\n",checks,failures);return failures?1:0;}
