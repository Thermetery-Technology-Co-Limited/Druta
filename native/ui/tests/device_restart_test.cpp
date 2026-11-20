// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "device_restart.h"
#include <cstdio>
#include <stdexcept>
using namespace druta::ui;
int failures=0;void check(bool b,const char* s){if(!b){++failures;std::printf("FAIL: %s\n",s);}}
int main(){DeviceRestartTarget target{"0000:09:03.0",L"PCI\\VEN_10DE&DEV_ABCD\\ONE",L"GPU"};
 auto expect_refusal=[&](const std::string& slot,const std::vector<DeviceRestartTarget>& rows){try{resolve_restart_target(slot,rows);return false;}catch(...){return true;}};
 check(resolve_restart_target("0000:09:03.0",{target}).instance_id==target.instance_id,"one exact target");
 check(expect_refusal("0001:09:03.0",{target})&&expect_refusal("0000:09:20.0",{target})&&expect_refusal("0000:09:03.0",{}),"unsupported segment, device and absent target refused");
 auto duplicate=target;duplicate.instance_id+=L"2";check(expect_refusal(target.slot,{target,duplicate}),"ambiguous exact slot refused");
 int enumerations=0,launches=0,status_calls=0;bool post_ok=true;std::vector<DeviceRestartTarget> current{target};DeviceRestartResult child;child.ok=true;
 DeviceRestartApi api;api.enumerate=[&]{++enumerations;return current;};api.launch_child=[&](const auto& t,uint32_t ms){++launches;check(t.instance_id==target.instance_id&&ms==7000,"bounded child receives selected target");return child;};
 api.started=[&](const auto&,std::string& why){++status_calls;const bool ok=post_ok;if(!ok)why="not started";return ok;};
 auto success=restart_device(target,api,7000);check(success.ok&&launches==1&&enumerations==2&&status_calls==1,"success requires same identity and started after restart");
 launches=enumerations=status_calls=0;current={duplicate};check(!restart_device(target,api,7000).ok&&launches==0,"changed instance prevents launch");
 current={target};
 child={};child.uncertain=true;child.message="timeout";status_calls=0;auto timed=restart_device(target,api,7000);check(!timed.ok&&timed.uncertain&&status_calls==0,"timeout preserves uncertainty without pretending postcheck success");
 child={};child.reboot_required=true;status_calls=0;check(restart_device(target,api,7000).reboot_required&&status_calls==0,"reboot-required is not success");
 child={};child.ok=true;post_ok=false;status_calls=0;check(!restart_device(target,api,7000).ok&&status_calls==1,"post restart problem refused");post_ok=true;
 status_calls=0;api.launch_child=[&](const auto&,uint32_t){current={duplicate};DeviceRestartResult r;r.ok=true;return r;};check(!restart_device(target,api,7000).ok,"replacement at same slot refused after helper");
 check(!restart_device(target,api,0).ok,"zero timeout refused");std::printf("Device restart: %d failures\n",failures);return failures?1:0;}
