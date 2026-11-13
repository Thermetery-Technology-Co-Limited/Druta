// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main unused_core_fixture_main
#include "core_client_test.cpp"
#undef main
#define main unused_weight_fixture_main
#include "power_weights_service_test.cpp"
#undef main
namespace {
WeightFake* active_weight=nullptr;
dc_status DC_CALL weight_identity(const dc_gpu*,dc_gpu_identity* out){*out=active_weight->current.adapter;return DC_OK;}
}
int main(){WeightFake fake(true);active_weight=&fake;auto a=api();a.identity=weight_identity;a.power_weights=fake.api();auto client=create_core_backend_for_test(a);std::string error;CHECK(client->open(fake.current.adapter.slot,error)&&fake.reads==0&&writes==0);
 auto initial=client->read(false);CHECK(fake.reads==0);Command read;read.operation=Command::Operation::power_weights_read;auto result=client->execute(read);CHECK(result.ok&&fake.reads==1&&fake.registry_reads==0);auto snapshot=client->read(false).power_weights;CHECK(snapshot&&snapshot->rows.size()==2&&snapshot->rows[0].eligible_for_preview);
 Command preview;preview.operation=Command::Operation::power_weights_preview;preview.power_weight_requests=std::make_shared<const std::vector<PowerWeightRequest>>(std::vector<PowerWeightRequest>{{0,1.25,4096}});const auto old_image=fake.current.image;result=client->execute(preview);CHECK(result.ok&&fake.registry_reads==1&&fake.current.image==old_image&&writes==0);auto published=client->read(false).power_weights;CHECK(published&&published->preview&&published->preview->ok&&published->preview->registry_added.size()==1&&snapshot->preview==nullptr);
 preview.power_weight_requests=std::make_shared<const std::vector<PowerWeightRequest>>(std::vector<PowerWeightRequest>{{0,1.5,4000}});CHECK(!client->execute(preview).ok&&writes==0);fake.current.image_availability=VoltagePolicyAvailability::unsupported;fake.current.image.clear();result=client->execute(read);auto partial=client->read(false).power_weights;CHECK(result.ok&&partial&&partial->info_availability==VoltagePolicyAvailability::available&&!partial->rows.empty()&&!partial->rows[0].eligible_for_preview&&writes==0);client->close();CHECK(writes==0);std::printf("Power weights client: %d checks, %d failures\n",checks,failures);return failures?1:0;}
