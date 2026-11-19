// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main established_i2c_fixture_entry
#include "i2c_session_test.cpp"
#undef main
#include "python_i2c_profile.h"
#include <stdexcept>
namespace {
ProfileJson nct_payload(){return {{"profile","NCT3933U - three current offsets"},{"sha256","34cd0411ab79509259c3675d28213d87cb9ad3f4d8ad14a69dd5103f9683d90e"},{"port",1},{"addr7",21},{"rail","OUT1/OUT2/OUT3"},{"format","i2c.current_dac_control"},{"display_name","NCT3933U - three current offsets"},{"current_dac_control",{{"outputs",ProfileJson::array({128,5,0})},{"configuration",0}}}};}
void profile_tests(){
 setup();nct_mode=true;wanted_port=1;wanted_addr=0x15;regs={{0x5d,0x39},{0x5e,0x33},{5,0},{1,0},{2,2},{3,0}};int handovers=0;
 auto a=api();a.read_bytes=block_read;a.controller_write=typed_write;a.before_clock_acquire=[&](std::string&){++handovers;return false;};I2cSession s(a,reinterpret_cast<dc_gpu*>(1),identity);
 auto select=command(Command::Operation::i2c_select);select.i2c_route=I2cRoute{1,0x15};select.i2c_family=I2cFamily::nct3933;CHECK(s.execute(select).ok);
 I2cProfilePlan plan;CHECK(!s.preflight_i2c_profile(nct_payload(),plan).ok&&typed_writes==0);
 auto gate=command(Command::Operation::i2c_gate);gate.enabled=true;CHECK(s.execute(gate).ok);CHECK(s.execute(command(Command::Operation::i2c_verify)).ok);CHECK(s.preflight_i2c_profile(nct_payload(),plan).ok&&typed_writes==0&&handovers==0);
 I2cProfileReceipt receipt;int journals=0;auto result=s.apply_i2c_profile(plan,[&](const auto& r){if(!journals)CHECK(typed_writes==0);receipt=r;++journals;});
 CHECK(result.ok&&journals>0&&regs[1]==128&&regs[2]==5&&regs[3]==0&&typed_writes==2&&s.owned()&&handovers==0);
 CHECK(s.verify_i2c_profile(plan).ok&&s.verify_i2c_profile(receipt).ok&&!s.verify_i2c_profile(receipt,true).ok);
 auto mutation=command(Command::Operation::i2c_zero_nct_outputs);mutation.i2c_route=I2cRoute{1,0x15};mutation.i2c_selection_revision=s.data().selection_revision;const auto issued=typed_writes;CHECK(!s.execute(mutation).ok&&typed_writes==issued);
 CHECK(s.release().ok&&regs[1]==0&&regs[2]==2&&regs[3]==0&&!s.owned());CHECK(s.verify_i2c_profile(receipt,true).ok);
 CHECK(s.preflight_i2c_profile(nct_payload(),plan).ok);const auto before_journal=typed_writes;
 auto refused=s.apply_i2c_profile(plan,[&](const auto&){throw std::runtime_error("journal refused");});
 CHECK(!refused.ok&&!refused.may_have_changed&&!s.owned()&&typed_writes==before_journal&&s.verify_i2c_profile(receipt,true).ok);
 I2cSession other(a,reinterpret_cast<dc_gpu*>(1),identity);CHECK(!other.verify_i2c_profile(receipt,true).ok&&!other.restore_i2c_profile(receipt).ok);
 I2cProfileReceipt forged;CHECK(!s.restore_i2c_profile(forged).ok&&!s.verify_i2c_profile(forged).ok);
 auto alternate=nct_payload();alternate["addr7"]=20;CHECK(!s.preflight_i2c_profile(alternate,plan).ok&&typed_writes==issued+2);
 CHECK(s.preflight_i2c_profile(nct_payload(),plan).ok);regs[5]=1;const auto before=typed_writes;CHECK(!s.apply_i2c_profile(plan,{}).ok&&typed_writes==before);regs[5]=0;
 CHECK(s.preflight_i2c_profile(nct_payload(),plan).ok);CHECK(s.execute(select).ok);CHECK(!s.apply_i2c_profile(plan,{}).ok&&typed_writes==before);CHECK(s.execute(command(Command::Operation::i2c_verify)).ok);
 CHECK(s.preflight_i2c_profile(nct_payload(),plan).ok);identity.nvapi_devid++;CHECK(!s.apply_i2c_profile(plan,{}).ok&&typed_writes==before);identity.nvapi_devid--;
 CHECK(s.preflight_i2c_profile(nct_payload(),plan).ok);typed_fail_reg=2;typed_uncertain=true;journals=0;result=s.apply_i2c_profile(plan,[&](const auto& r){receipt=r;++journals;});CHECK(!result.ok&&result.may_have_changed&&journals>0&&s.owned());fail_reads=0;typed_uncertain=false;CHECK(s.restore_i2c_profile(receipt).ok&&regs[1]==0&&regs[2]==2&&!s.owned()&&handovers==0);
 CHECK(s.preflight_i2c_profile(nct_payload(),plan).ok);CHECK(s.apply_i2c_profile(plan,[&](const auto& r){receipt=r;}).ok);CHECK(s.release().ok&&s.verify_i2c_profile(receipt,true).ok);
 auto scalar=command(Command::Operation::i2c_apply);scalar.i2c_field="out1";scalar.value=20;scalar.i2c_selection_revision=s.data().selection_revision;CHECK(s.execute(scalar).ok);auto scalar2=scalar;scalar2.i2c_field="out2";scalar2.value=-30;CHECK(s.execute(scalar2).ok);const auto scalar_writes=typed_writes;CHECK(!s.restore_i2c_profile(receipt).ok&&typed_writes==scalar_writes);CHECK(s.release().ok);
 auto noop=nct_payload();noop["current_dac_control"]["outputs"]=ProfileJson::array({0,2,0});CHECK(s.preflight_i2c_profile(noop,plan).ok);journals=0;CHECK(s.apply_i2c_profile(plan,[&](const auto&){++journals;}).ok&&journals==0&&!s.owned());
}
}
int main(){profile_tests();std::printf("I2C profile session: %u checks\n",checks);return 0;}
