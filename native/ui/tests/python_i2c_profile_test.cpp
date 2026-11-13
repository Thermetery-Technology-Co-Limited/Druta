// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "python_i2c_profile.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"%d: %s\n",__LINE__,#x);std::abort();}}while(false)
template<class F>void rejects(F f){bool failed=false;try{f();}catch(...){failed=true;}CHECK(failed);}
int main(){
 ProfileJson j={{"profile","NCT3933U - three current offsets"},{"sha256","34cd0411ab79509259c3675d28213d87cb9ad3f4d8ad14a69dd5103f9683d90e"},{"port",1},{"addr7",21},{"rail","OUT1/OUT2/OUT3"},{"format","i2c.current_dac_control"},{"display_name","NCT3933U - three current offsets"},{"current_dac_control",{{"outputs",ProfileJson::array({0,128,255})},{"configuration",0x40}}}};
 auto state=parse_python_nct3933_profile(j);CHECK(state.route.port==1&&state.route.addr7==21&&state.outputs[0]==0&&state.outputs[1]==128&&state.outputs[2]==255&&state.configuration==64&&state.selection==7);
 auto renamed=j;renamed["display_name"]="User label";CHECK(parse_python_nct3933_profile(renamed).outputs==state.outputs);
 auto bad=j;bad["profile"]="custom recipe";rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["port"]=2;rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["sha256"]="04cd0411ab79509259c3675d28213d87cb9ad3f4d8ad14a69dd5103f9683d90e";rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["addr7"]=22;rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["current_dac_control"]["outputs"][1]=true;rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["current_dac_control"]["outputs"][1]=256;rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["current_dac_control"]["outputs"].push_back(0);rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["current_dac_control"]["unknown"]=0;rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["format"]="i2c.multi_state_offset";rejects([&]{parse_python_nct3933_profile(bad);});bad=j;bad["offset_mv"]=1;rejects([&]{parse_python_nct3933_profile(bad);});
 std::puts("Python NCT profile parser passed");return 0;
}
