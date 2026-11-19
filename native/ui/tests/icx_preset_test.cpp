/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "../icx_preset.h"
#include <cassert>
#include <stdexcept>
using namespace druta::ui;
template<class F> bool rejected(F f) { try { f(); return false; } catch(const std::runtime_error&) { return true; } }
int main() {
    ControllerView v; v.readable=true;
    auto add=[&](const char* key, std::optional<double> value, const char* detail="") { I2cField f; f.key=key; f.value=value; f.detail=detail; v.fields.push_back(f); };
    add("nvvdd",{},"Auto"); add("fbvdd",1300); add("frequency_nvvdd",500); add("frequency_fbvdd",800); add("ocp_nvvdd",1); add("ocp_fbvdd",0); add("loadline",12);
    auto doc=capture_icx_preset(v); assert(doc["schema"]=="druta.icx.preset.v1" && doc["settings"]["nvvdd_request_mv"].is_null());
    auto operations=icx_preset_requests(doc,false); assert(operations.size()==7 && operations.front().key=="nvvdd" && operations.front().reset);
    assert(describe_icx_preset(doc).find("NVVDD Auto")!=std::string::npos);
    auto bad=doc; bad["schema"]="druta.icx.preset.v2"; assert(rejected([&]{icx_preset_requests(bad,false);}));
    bad=doc; bad["settings"]["extra"]=1; assert(rejected([&]{describe_icx_preset(bad);}));
    bad=doc; bad["settings"]["nvvdd_request_mv"]=1000; assert(rejected([&]{describe_icx_preset(bad);}));
    bad=doc; bad["settings"]["nvvdd_ocp_enabled"]=1; assert(rejected([&]{describe_icx_preset(bad);}));
    bad=doc; bad["settings"]["fbvdd_request_mv"]=1301; assert(rejected([&]{describe_icx_preset(bad);}));
    bad=doc; bad["origin"]="   "; assert(rejected([&]{describe_icx_preset(bad);}));
    v.fields.pop_back(); assert(rejected([&]{capture_icx_preset(v);}));
    add("loadline",2); v.fields[0].value=1500; v.fields[0].detail="";
    auto xoc=capture_icx_preset(v); assert(xoc["settings"]["nvvdd_mode"]=="manual");
    assert(rejected([&]{icx_preset_requests(xoc,false);})); assert(icx_preset_requests(xoc,true).size()==7);
    v.fields[1].stale=true; assert(rejected([&]{capture_icx_preset(v);}));
}
