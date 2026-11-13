// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main established_annotation_fixture_entry
#include "core_client_test.cpp"
#undef main
#include "python_full_profile.h"
#include "policy_annotation_profile.h"
namespace {
int label_reads=0;bool label_fail=false,label_drift=false,fail_after_memory=false;
dc_status DC_CALL annotation_setter(dc_gpu* gpu,uint32_t type,double value,int32_t preserve,dc_outcome* out){const auto status=setter(gpu,type,value,preserve,out);if(fail_after_memory){fail_after_memory=false;label_fail=true;}return status;}
dc_status DC_CALL label_policies(dc_gpu*,dc_cl_confirm_fn,void*,dc_cl_policy_inputs_fn,void*,dc_policy_row* rows,uint32_t cap,uint32_t* count,dc_error*){++label_reads;if(label_fail)return DC_E_FAILED;if(cap<2)return DC_E_BUFFER;*count=2;rows[0]={};rows[0].policy=3;rows[0].type=4;rows[0].channel=label_drift?8:7;rows[0].unit=1;rows[0].writable=0;rows[0].consistent=0;rows[1]={};rows[1].policy=9;rows[1].type=5;rows[1].channel=6;rows[1].unit=0;rows[1].writable=0;return DC_OK;}
ProfileJson full(){return {{"schema",2},{"device",{{"uuid","GPU-A"},{"slot","0000:02:00.0"}}},{"incomplete",ProfileJson::array()},{"mem_off_units",80},{"mem_off_true_mhz",40},{"mem_off_scale",2},{"power_policy_names",{{"3",{{"name","PCIe"},{"channel",7},{"type",4}}},{"9",{{"name","Board"},{"channel",6},{"type",5}}}}}};}
Command request(const ProfileJson& doc,const PolicyAnnotations& labels,bool preflight=false){Command c;c.operation=preflight?Command::Operation::preflight_catalog_profile:Command::Operation::apply_catalog_profile;c.catalog_profile=std::make_shared<const ProfileJson>(doc);c.annotation_snapshot=std::make_shared<const PolicyAnnotations>(labels);return c;}
#if defined(_MSC_VER)
__declspec(noinline)
#endif
void annotation_tests(){
 auto a=api();a.lock=lock_gpu;a.unlock=unlock_gpu;a.policy_read=label_policies;a.set_offset=annotation_setter;a.capabilities=nullptr;a.power=nullptr;a.power_set=nullptr;a.policy_set=nullptr;a.pins_get=nullptr;a.pins_set=nullptr;auto client=create_core_backend_for_test(a);std::string error;CHECK(client->open("0000:02:00.0",error));PolicyAnnotations labels;auto doc=full();auto selected=select_python_full_profile(doc,{"annotation:power"});
 writes=power_writes=policy_writes=pin_writes=label_reads=0;auto preflight=client->execute(request(selected,labels,true));CHECK(preflight.ok&&!preflight.annotation_effect&&writes==0&&power_writes==0&&policy_writes==0&&pin_writes==0);
 auto applied=client->execute(request(selected,labels));CHECK(applied.ok&&applied.annotation_effect&&applied.annotation_effect->phase==PolicyAnnotationPhase::apply&&applied.annotation_effect->rows.size()==2&&writes==0);CHECK(labels.document().empty());
 Command undo;undo.operation=Command::Operation::undo;auto restored=client->execute(undo);CHECK(restored.ok&&restored.annotation_effect&&restored.annotation_effect->phase==PolicyAnnotationPhase::undo);
 label_drift=true;applied=client->execute(request(selected,labels));CHECK(applied.ok&&applied.annotation_effect&&applied.annotation_effect->rows.size()==1&&applied.annotation_effect->skipped>=1);label_drift=false;
 auto before=writes;auto deselected=select_python_full_profile(doc,{"clock:memory"});auto mem=client->execute(request(deselected,labels,true));CHECK(mem.ok&&!mem.annotation_effect&&writes==before);
 auto bad=doc;bad["power_policy_names"]["3"]["name"]=std::string(81,'x');ProfileJson envelope={{"format","druta-python-full-tune-selection"},{"version",1},{"source",bad},{"targets",ProfileJson::array({"clock:memory"})}};CHECK(!client->execute(request(envelope,labels)).ok&&writes==before);
 bad=doc;bad["mem_off_true_mhz"]=41;CHECK(!client->execute(request(bad,labels)).ok&&writes==before);
 current=40;mode=2;auto mixed=select_python_full_profile(doc,{"clock:memory","annotation:power"});auto partial=client->execute(request(mixed,labels));CHECK(!partial.ok&&partial.annotation_effect&&partial.annotation_effect->phase==PolicyAnnotationPhase::apply&&client->read(false).undo_available);mode=0;auto undone=client->execute(undo);CHECK(undone.ok&&undone.annotation_effect&&undone.annotation_effect->phase==PolicyAnnotationPhase::undo);
 label_fail=true;auto failed=client->execute(request(selected,labels));CHECK(!failed.ok&&!failed.annotation_effect);label_fail=false;
 // Invalid UI storage blocks the complete mixed transaction before any SET.
 PolicyAnnotations unknown(ProfileJson{{"annotations","unknown"}});const auto unknown_before=unknown.document();const auto invalid_writes=writes;auto invalid=client->execute(request(mixed,unknown));CHECK(!invalid.ok&&!invalid.annotation_effect&&writes==invalid_writes&&unknown.document()==unknown_before);
 // A metadata-only Undo survives a failed fresh qualification and retries.
 applied=client->execute(request(selected,labels));CHECK(applied.ok&&applied.annotation_effect);label_fail=true;const auto no_set=writes;auto retry=client->execute(undo);CHECK(!retry.ok&&!retry.annotation_effect&&client->read(false).undo_available&&writes==no_set);retry=client->execute(undo);CHECK(!retry.ok&&!retry.annotation_effect&&client->read(false).undo_available&&writes==no_set);label_fail=false;retry=client->execute(undo);CHECK(retry.ok&&retry.annotation_effect&&retry.annotation_effect->phase==PolicyAnnotationPhase::undo&&writes==no_set);
 // If final metadata capture fails after an accepted memory change, no empty
 // effect is emitted and the independently issued hardware Undo remains.
 current=40;mode=0;fail_after_memory=true;const auto immutable=labels.document();auto late=client->execute(request(mixed,labels));CHECK(!late.ok&&!late.annotation_effect&&current==80&&client->read(false).undo_available&&labels.document()==immutable);label_fail=false;CHECK(client->execute(undo).ok&&current==40);
 // Metadata Undo is emitted once even when the hardware Undo fails; a later
 // hardware-only retry must not repeat that UI effect.
 current=40;auto both=client->execute(request(mixed,labels));CHECK(both.ok&&both.annotation_effect&&current==80);mode=2;auto first=client->execute(undo);CHECK(!first.ok&&first.annotation_effect&&first.annotation_effect->phase==PolicyAnnotationPhase::undo&&client->read(false).undo_available);mode=0;auto second=client->execute(undo);CHECK(second.ok&&!second.annotation_effect&&current==40&&labels.document()==immutable);
 client->close();
}
}
int main(){annotation_tests();std::printf("Policy annotation catalog: %d checks, %d failures\n",checks,failures);return failures?1:0;}
