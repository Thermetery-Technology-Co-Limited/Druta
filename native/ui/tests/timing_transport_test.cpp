// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "timing_transport.h"
#include <windows.h>
#include <cstdio>
#include <cstring>
using namespace druta::ui;
static int failures=0;
static void check(bool value,const char* message){if(!value){++failures;std::printf("FAIL: %s\n",message);}}
int main(int argc,char** argv){
 // The same hardware-free executable is the selected bounded helper fixture.
 if(argc>1){if(std::strcmp(argv[1],"fields")==0)Sleep(5000);return 0;}
 wchar_t path[32768]{};const auto n=GetModuleFileNameW(nullptr,path,32768);
 check(n>0&&n<32768,"absolute self helper path available");
 auto transport=make_timing_transport(path);std::string error;
 check(transport->pin(error),"self helper pinned");
 auto control=std::make_shared<TimingCaptureControl>();bool cancelled=true;control->cancelled=[&]{return cancelled;};
 transport->set_capture_control(control);
 auto result=transport->run(TimingCommand::fields,"","",0,false,false);
 check(!result.launched&&!result.error.empty(),"cancelled scope launches no child");
 cancelled=false;control->deadline=TimingCaptureControl::Clock::now();
 result=transport->run(TimingCommand::fields,"","",0,false,false);
 check(!result.launched&&result.timed_out,"expired shared scope launches no child");
 control->deadline=TimingCaptureControl::Clock::now()+std::chrono::milliseconds(150);
 const auto start=GetTickCount64();result=transport->run(TimingCommand::fields,"","",0,false,false);
 check(result.launched&&result.timed_out&&!result.error.empty(),"shared deadline interrupts blocked helper");
 check(GetTickCount64()-start<2000,"helper termination wait is bounded");
 check(!transport->pending(),"terminated fixture has no pending ownership");
 result=transport->run(TimingCommand::help,"","",0,false,false);
 check(!result.launched&&result.timed_out,"next command shares exhausted deadline instead of resetting it");
 transport->set_capture_control({});result=transport->run(TimingCommand::help,"","",0,false,false);
 check(result.launched&&!result.timed_out&&result.error.empty()&&result.exit_code==0,"normal helper behavior restored after capture scope clears");
 transport->unpin();std::printf("Timing transport: %d failures\n",failures);return failures?1:0;
}
