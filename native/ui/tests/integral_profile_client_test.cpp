// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Actual CoreClient routing with injected policy packets, never a driver/DLL.
#define main integral_service_fixture_entry
#include "power_integral_service_test.cpp"
#undef main
#include "core_client.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
namespace {
Fake* integral_active=nullptr;
uint32_t DC_CALL integral_version(){return DC_ABI_VERSION;}
dc_status DC_CALL integral_enumerate(dc_gpu_entry*,size_t,size_t* count){*count=0;return DC_OK;}
dc_status DC_CALL integral_open(const char*,dc_gpu** out){*out=integral_active->gpu();return DC_OK;}
void DC_CALL integral_close(dc_gpu*){}
CoreApi integral_client_api(){CoreApi result;result.abi_version=integral_version;result.enumerate=integral_enumerate;result.open=integral_open;result.close=integral_close;result.identity=identity;result.power_integral=api();return result;}
struct TemporaryProfile {
    wchar_t path[MAX_PATH]{};
    TemporaryProfile(){wchar_t directory[MAX_PATH]{};check(GetTempPathW(MAX_PATH,directory)!=0,"temporary directory available");check(GetTempFileNameW(directory,L"dpi",0,path)!=0,"temporary profile path available");}
    ~TemporaryProfile(){if(path[0])DeleteFileW(path);}
};
Command integral_command(Command::Operation operation,const std::vector<PowerIntegralRequest>& requests={}){Command command;command.operation=operation;command.integral_requests=std::make_shared<const std::vector<PowerIntegralRequest>>(requests);return command;}
void integral_client_workflow(){
    Fake fixture;integral_active=&fixture;auto backend=create_core_backend_for_test(integral_client_api());std::string error;check(backend->open(fixture.id.slot,error),"CoreClient opens injected complete paired target");
    check(fixture.reads==0&&fixture.sets==0,"opening backend does not poll integral policies");Command read;read.operation=Command::Operation::power_integral_read;check(backend->execute(read).ok,"explicit CoreClient integral read routed");
    const auto original=backend->read(false).power_integral;check(original&&original->current&&original->rows[0].upper==4096,"read publishes typed integral snapshot");if(!original)return;
    // Save must perform a fresh capture, not serialize the previously published
    // snapshot. Updating fake driver state here performs no API SET.
    put16(fixture.control,upper0,4500);TemporaryProfile file;Command save;save.operation=Command::Operation::power_integral_save;save.profile_path=file.path;save.integral_selection=std::make_shared<const std::vector<PowerIntegralKey>>(std::vector<PowerIntegralKey>{{0,PowerIntegralField::upper}});
    check(backend->execute(save).ok&&fixture.sets==0,"Save fresh selected native document performs no policy writes");auto document=read_profile(file.path);check(document["fields"].size()==1&&document["fields"][0]["raw"]==4500,"Save uses fresh worker capture with selected subset");
    auto captured=backend->read(false).power_integral;check(captured&&captured->rows[0].upper==4500&&original->rows[0].upper==4096,"published current data updates without mutating prior snapshot");if(!captured)return;
    document["fields"][0]["raw"]=6000;const auto staged_requests=stage_power_integral_profile(document,*captured);auto preview=integral_command(Command::Operation::power_integral_preview_profile,staged_requests);check(backend->execute(preview).ok&&fixture.sets==0,"profile staging and real CoreClient Preview are read-only");
    auto apply=integral_command(Command::Operation::power_integral_apply_profile,staged_requests);check(backend->execute(apply).ok&&fixture.sets==1&&get16(fixture.control,upper0)==6000,"immutable selected request vector applies through real client");
    auto changed=backend->read(false).power_integral;check(changed&&changed->profile_undo_available&&!changed->profile_recovery_pending&&changed->rows[0].upper==6000,"Apply publishes confirmed grouped Undo and readback");
    check(backend->execute(integral_command(Command::Operation::power_integral_undo_profile)).ok&&get16(fixture.control,upper0)==4500,"CoreClient grouped Undo returns fresh captured prior");auto restored=backend->read(false).power_integral;check(restored&&!restored->profile_undo_available&&!restored->profile_recovery_pending,"successful Undo clears published group flags");if(!restored)return;
    auto partial=std::vector<PowerIntegralRequest>{staged(*restored,0,PowerIntegralField::upper,6200),staged(*restored,1,PowerIntegralField::upper,7000)};fixture.fail_set_at=fixture.sets+2;
    const auto failed=backend->execute(integral_command(Command::Operation::power_integral_apply_profile,partial));check(!failed.ok&&failed.may_have_changed,"later accepted failed SET reports possible partial mutation");auto pending=backend->read(false).power_integral;check(pending&&pending->profile_undo_available&&pending->profile_recovery_pending,"partial failure publishes retained grouped recovery");const auto failed_writes=fixture.sets;
    check(!backend->execute(integral_command(Command::Operation::power_integral_apply_profile,partial)).ok&&fixture.sets==failed_writes,"pending recovery prevents a second profile from issuing writes");fixture.fail_set_at=0;
    check(backend->execute(integral_command(Command::Operation::power_integral_recover_profile)).ok&&get16(fixture.control,upper0)==4500&&get16(fixture.control,0x14+0xC4+14)==4096,"CoreClient recovery restores exactly the launched selected requests");auto recovered=backend->read(false).power_integral;check(recovered&&!recovered->profile_recovery_pending&&!recovered->profile_undo_available,"recovery publishes cleared flags");
    backend->close();integral_active=nullptr;
}
}
int main(){integral_client_workflow();std::printf("Integral profile CoreClient: %d failures\n",failures);return failures?1:0;}
