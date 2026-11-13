// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main voltage_policy_fixture_main
#include "voltage_policy_service_test.cpp"
#undef main
#include <stdexcept>

int main(){
 fresh();{
  VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();
  CHECK(!s.preview({3,0,{}}).ok&&s.state().rows[0].availability==VoltagePolicyAvailability::available&&s.state().rows[1].availability==VoltagePolicyAvailability::available);
  CHECK(!s.preview({2,0,0}).ok&&s.state().rows[1].availability==VoltagePolicyAvailability::available&&s.state().rows[1].current==true);
  transient_get=2;CHECK(!s.preview({2,0,2}).ok&&writes==0);CHECK(s.state().rows[1].availability==VoltagePolicyAvailability::transient&&s.state().rows[1].current==true&&s.state().rows[1].writable);CHECK(s.state().rows[2].availability==VoltagePolicyAvailability::transient&&s.state().rows[0].availability==VoltagePolicyAvailability::available);
  CHECK(s.preview({2,0,2}).ok&&s.state().rows[1].availability==VoltagePolicyAvailability::available);
  transport_kind=DC_ERR_UNSUPPORTED;CHECK(!s.preview({2,0,2}).ok&&s.state().rows[1].availability==VoltagePolicyAvailability::unsupported&&!s.state().rows[1].writable&&s.state().rows[1].current==true&&s.state().rows[0].availability==VoltagePolicyAvailability::available);transport_kind=0;
 }
 fresh();{
  VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();VoltagePolicyReceipt receipt;CHECK(s.apply_profile({6,2,6},[&](const auto& r){receipt=r;}).ok); // monotonicity is selected but initially a no-op
  header[10]=0;CHECK(!s.verify(receipt).ok&&s.state().rows[1].availability==VoltagePolicyAvailability::available); // full requested mask still verified
  CHECK(s.restore(receipt).ok&&header[10]==0&&header[11]==1&&s.verify(receipt,true).ok); // original-state verification is issued-only
  header[10]=1;CHECK(s.apply_profile({2,0,2},[&](const auto& r){receipt=r;}).ok);
  CHECK(!s.verify(VoltagePolicyReceipt{}).ok&&s.state().rows[1].availability==VoltagePolicyAvailability::available);
  VoltagePolicyService other(api(),reinterpret_cast<dc_gpu*>(1),dev);other.read();CHECK(!other.verify(receipt).ok&&other.state().rows[1].availability==VoltagePolicyAvailability::available);
  transient_get=2;CHECK(!s.verify(receipt).ok&&s.state().rows[1].availability==VoltagePolicyAvailability::transient&&s.state().rows[1].current==false&&s.state().rows[1].writable&&s.state().rows[0].availability==VoltagePolicyAvailability::available);
  CHECK(s.verify(receipt).ok&&s.state().rows[1].availability==VoltagePolicyAvailability::available);
 }
 fresh();{
  VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();int journal=0;VoltagePolicyReceipt receipt;
  CHECK(s.preview({2,0,2}).ok&&writes==0);
  CHECK(s.apply_profile({2,2,2},[&](const auto&){++journal;}).ok&&journal==0&&writes==0);
  CHECK(!s.preview({2,0,0}).ok&&writes==0);
  CHECK(s.apply_profile({2,0,2},[&](const auto& r){CHECK(writes==0);++journal;receipt=r;}).ok&&writes==1&&journal==1);
  CHECK(s.verify(receipt).ok&&!s.verify(receipt,true).ok);
  header[11]=0;CHECK(s.verify(receipt).ok); // disjoint flag can change independently
  VoltagePolicyService other(api(),reinterpret_cast<dc_gpu*>(1),dev);other.read();CHECK(!other.verify(receipt).ok&&!other.restore(receipt).ok&&writes==1);
  CHECK(s.restore(receipt).ok&&header[10]==1&&header[11]==0&&writes==2&&s.verify(receipt,true).ok);
 }
 fresh();{
  VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();CHECK(s.apply({4,0,4}).ok);const auto old=writes;
  CHECK(!s.apply_profile({2,0,2},[&](const auto&){throw std::runtime_error("journal refused");}).ok&&writes==old&&s.state().undo_available&&s.state().undo_selection==4);
  VoltagePolicyReceipt receipt;CHECK(s.apply_profile({2,0,2},[&](const auto& r){receipt=r;}).ok&&writes==old+1);
  CHECK(s.restore(receipt).ok&&header[10]==1&&header[11]==0&&s.state().undo_available&&s.state().undo_selection==4);
  CHECK(s.undo().ok&&header[11]==1);
 }
 fresh();{
  VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();VoltagePolicyReceipt receipt;block_after_write=true;
  auto result=s.apply_profile({2,0,2},[&](const auto& r){receipt=r;});CHECK(!result.ok&&result.may_have_changed&&s.state().recovery_pending&&writes==1);
  blocked_get=0;CHECK(s.verify(receipt).ok&&s.state().recovery_pending);CHECK(s.restore(receipt).ok&&header[10]==1&&!s.state().recovery_pending);
 }
 fresh();{
  gate=true;VoltagePolicyService s(api(),reinterpret_cast<dc_gpu*>(1),dev);s.read();VoltagePolicyReceipt receipt;race=malformed_set=true;
  auto result=s.apply_profile({1,0,1},[&](const auto& r){receipt=r;});CHECK(!result.ok&&result.may_have_changed&&s.state().recovery_prior_unknown&&writes==1);
  CHECK(!s.verify(receipt,true).ok&&!s.restore(receipt).ok&&writes==1&&s.state().recovery_pending);
 }
 std::printf("voltage policy catalog service: %u checks\n",checks);return 0;
}
