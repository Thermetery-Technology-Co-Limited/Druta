// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_catalog.h"
#include "python_scoped_profile.h"
#include "icx_preset.h"
#include "fan_profile.h"
#include "timing_profile.h"
#include "semantic_policy_profile.h"
#include "saving_profile.h"
#include "power_saving_service.h"
#include "clock_gating_service.h"
#include "voltage_tables_service.h"
#include "voltage_limits_service.h"
#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <stdexcept>
namespace druta::ui {
namespace {
using J=ProfileJson;
[[noreturn]]void fail(const std::string& why){throw std::runtime_error("Profile catalog: "+why);}
void keys(const J& j,std::initializer_list<const char*> names){if(!j.is_object()||j.size()!=names.size())fail("missing or unexpected fields");for(auto n:names)if(!j.contains(n))fail("missing field "+std::string(n));}
std::string text(const J& j){if(!j.is_string())fail("string required");auto s=j.get<std::string>();if(s.empty()||s.size()>4096||s.find('\0')!=std::string::npos)fail("invalid string");return s;}
int64_t number(const J& j,int64_t lo=INT32_MIN,int64_t hi=INT32_MAX){if(!j.is_number_integer()||(j.is_number_unsigned()&&j.get<uint64_t>()>uint64_t(INT64_MAX)))fail("exact integer required");auto n=j.get<int64_t>();if(n<lo||n>hi)fail("integer outside understood range");return n;}
void positive(const J& j){if(!j.is_number()||!std::isfinite(j.get<double>())||j.get<double>()<=0)fail("positive finite number required");}
// Domain 2 is the additional memory request, independent of ordinary memory
// offset. Domain 0 and uniform core editing overlap the core curve request.
std::string domain_target(uint32_t domain){return domain==0?"clock:core":"clock:domain:"+std::to_string(domain);}
void add(ProfileDescription& d,const std::string& t){for(const auto& old:d.targets)if(profile_targets_overlap(old,t))fail("duplicate or overlapping target "+t);d.targets.push_back(t);}
void native_target(ProfileDescription& d,const J& target){keys(target,{"uuid","slot"});d.uuid=text(target["uuid"]);d.slot=text(target["slot"]);}
std::string scoped_target(const J& entry){const auto kind=text(entry.at("kind"));if(kind=="rail")return "rail:"+std::to_string(number(entry.at("rail"),0,1))+":"+std::to_string(number(entry.at("field"),0,3));
 if(kind=="policy")return "power:policy:"+std::to_string(number(entry.at("policy"),0,31));if(kind=="board")return "power:board";fail("unknown scoped control");}
std::string advanced_target(const J& entry){const auto kind=text(entry.at("kind"));auto domain=uint32_t(number(entry.at("domain"),0,31));if(kind=="clock_domain")return domain_target(domain);if(kind=="nvvdd"){if(domain)fail("invalid NVVDD index");return "voltage:nvvdd-offset";}if(kind=="msvdd")return "voltage:msvdd-offset:"+std::to_string(domain);fail("unknown advanced control");}
std::string daily_target(const std::string& key){if(key=="core")return "clock:core";if(key=="memory")return "clock:memory";if(key=="power")return "power:board";if(key=="boost")return "voltage:boost";if(key=="fan")return "fan:policy";fail("unknown daily control");}
bool collection(const J& d){return d.is_object()&&((d.contains("format")&&d["format"]=="druta-native-combined")||(d.contains("scope")&&d["scope"]=="combined"));}
ProfileDescription leaf(const J& d){
 if(!d.is_object()||d.dump().size()>profile_max_bytes)fail("invalid or oversized document");ProfileDescription out;
 const auto semantic=[&](ProfileScope scope,const ProfileSelection& selected){out.scope=scope;out.slot=selected.slot;if(!selected.uuid.empty())out.uuid=selected.uuid;for(const auto& target:selected.targets)add(out,target);};
 if(d.value("format",J())=="druta-native-voltage-policy"||d.value("scope",J())=="clock_vf"||(d.value("scope",J())=="voltage_controls"&&d.contains("voltage_controls")&&d["voltage_controls"].value("kind",J())=="gate")){
  semantic(ProfileScope::voltage_policy,describe_voltage_policy_profile(d));
 }else if(d.value("format",J())=="druta-native-power-integral"||d.value("scope",J())=="power_integral"){
  semantic(ProfileScope::power_integral,describe_power_integral_profile(d));
 }else if(d.value("format",J())=="druta-native-voltage-requests"||d.value("scope",J())=="voltage_requests"){
  semantic(ProfileScope::voltage_tables,describe_voltage_tables_profile(d));
 }else if(d.value("format",J())=="druta-native-voltage-limits"||(d.value("scope",J())=="voltage_controls"&&d.contains("voltage_controls")&&d["voltage_controls"].is_object()&&d["voltage_controls"].value("kind",J())=="switch_delay")){
  semantic(ProfileScope::voltage_limits,describe_voltage_limits_profile(d));
 }else if(d.value("scope",J())=="power_saving_requests"){
  semantic(ProfileScope::power_saving,describe_power_saving_profile(d));
 }else if(d.value("format",J())=="druta-native-clock-gating"||d.value("scope",J())=="pcie_speed"){
  semantic(ProfileScope::clock_gating,describe_clock_gating_profile(d));
 }else if(d.value("scope",J())=="lowpower_requests"){
  const auto request=parse_python_saving_profile(d);
  if(request.clock)semantic(ProfileScope::clock_gating,describe_clock_gating_profile(d));
  else semantic(ProfileScope::power_saving,describe_power_saving_profile(d));
 }else if((d.contains("format")&&d["format"]=="druta-native-fan")||(d.contains("scope")&&d["scope"]=="fan")){
  const auto fan=parse_fan_profile(d);out.scope=ProfileScope::fan;out.slot=fan.slot;if(!fan.uuid.empty())out.uuid=fan.uuid;add(out,"fan:policy");
 }else if(is_legacy_timing_profile(d)){
  // Aliases gain canonical identity only from the selected helper's fresh
  // layout. Treat the exchange document atomically until that preflight.
  out.scope=ProfileScope::legacy_timing;add(out,"timing:*");
 }else if(d.contains("format")){
  const auto format=text(d["format"]);const auto version=number(d.at("version"),1,2);native_target(out,d.at("target"));
  if(format=="druta-native-daily"){
   if(version==1)keys(d,{"format","version","target","controls"});else keys(d,{"format","version","target","controls","advanced"});out.scope=ProfileScope::daily;
   if(!d["controls"].is_object())fail("invalid daily controls");
   for(auto it=d["controls"].begin();it!=d["controls"].end();++it){const auto& c=it.value();const auto key=it.key();add(out,daily_target(key));
    if(key=="memory"){keys(c,{"value","unit","scale"});number(c["value"]);text(c["unit"]);number(c["scale"],1,INT32_MAX);}
    else if(key=="fan"){keys(c,{"mode","value","unit"});if(c["unit"]!="%"||(c["mode"]!="auto"&&c["mode"]!="manual"))fail("invalid fan mode/units");auto n=number(c["value"],0,100);if(c["mode"]=="auto"&&n)fail("automatic fan request must be zero");}
    else {keys(c,{"value","unit"});if(c["unit"]!=(key=="core"?"MHz":key=="power"?"mW":"%"))fail("invalid daily units");number(c["value"],key=="core"?INT32_MIN:key=="power"?1:0,key=="power"?int64_t(UINT32_MAX-1):key=="boost"?100:INT32_MAX);}
   }
   if(version==2){if(!d["advanced"].is_array()||d["advanced"].size()>34)fail("invalid advanced controls");for(const auto& e:d["advanced"]){keys(e,{"kind","domain","value","unit"});add(out,advanced_target(e));number(e["value"]);if(e["unit"]!=(e["kind"]=="clock_domain"?"kHz":"uV"))fail("invalid advanced units");}}
  }else if(format=="druta-native-scoped"){
   if(version!=1)fail("unsupported scoped version");keys(d,{"format","version","target","entries"});out.scope=ProfileScope::scoped;
   if(!d["entries"].is_array()||d["entries"].empty()||d["entries"].size()>41)fail("invalid scoped entries");
   for(const auto& e:d["entries"]){const auto target=scoped_target(e);const auto kind=e["kind"].get<std::string>();add(out,target);if(kind=="rail")keys(e,{"kind","rail","field","binding","unit","value"});else if(kind=="policy")keys(e,{"kind","policy","binding","unit","value"});else keys(e,{"kind","binding","unit","value"});
    text(e["binding"]);if(kind=="rail"){if(e["unit"]!="uV")fail("invalid rail units");number(e["value"]);}else{if(e["unit"]!="mW"&&(kind!="policy"||e["unit"]!="mA"))fail("invalid power/current units");number(e["value"],kind=="board"?1:0,UINT32_MAX-1);}
   }
  }else if(format=="druta-native-curve"){
   if(version!=1)fail("unsupported curve version");keys(d,{"format","version","target","geometry","points"});out.scope=ProfileScope::curve;add(out,"clock:core");
   const auto& g=d["geometry"];keys(g,{"n_entries","freq_div","gpu_indices","other_indices"});const auto n=number(g["n_entries"],1,DC_VFP_POINTS);number(g["freq_div"],1,UINT32_MAX);std::set<int64_t> all,gpu;
   for(auto key:{"gpu_indices","other_indices"}){if(!g[key].is_array())fail("invalid curve index list");for(const auto& idx:g[key]){auto i=number(idx,0,n-1);if(!all.insert(i).second)fail("duplicate curve index");if(std::string(key)=="gpu_indices")gpu.insert(i);}}
   if(gpu.empty()||!d["points"].is_array()||d["points"].size()!=gpu.size())fail("invalid curve points");std::set<int64_t> seen;
   for(const auto& p:d["points"]){keys(p,{"idx","volt_mv","delta_khz"});auto i=number(p["idx"],0,n-1);if(!gpu.count(i)||!seen.insert(i).second)fail("curve point mapping differs");positive(p["volt_mv"]);number(p["delta_khz"]);}
  }else if(format=="druta-native-timing"){
   if(version!=1)fail("unsupported timing version");keys(d,{"format","version","target","codename","band","fields"});out.scope=ProfileScope::timing;text(d["codename"]);
   const auto& b=d["band"];keys(b,{"memory_mhz","pstate_before","pstate_after","memory_offset"});number(b["memory_mhz"],1,UINT32_MAX);for(auto key:{"pstate_before","pstate_after"}){auto n=number(b[key],0,2);if(n!=0&&n!=2)fail("invalid timing operating state");}number(b["memory_offset"]);
   if(!d["fields"].is_array()||d["fields"].empty()||d["fields"].size()>1024)fail("invalid timing fields");std::map<std::pair<std::string,uint32_t>,uint32_t> masks;std::map<std::string,uint32_t> offsets;std::map<uint32_t,std::string> names;
   for(const auto& f:d["fields"]){keys(f,{"field","value","register","offset","lo","hi","maximum"});const auto name=text(f["field"]),reg=text(f["register"]);add(out,"timing:"+name);const auto offset=uint32_t(number(f["offset"],0,UINT32_MAX)),lo=uint32_t(number(f["lo"],0,31)),hi=uint32_t(number(f["hi"],lo,31));if(offset%4)fail("unaligned timing register");const auto maximum=(uint64_t{1}<<(hi-lo+1))-1;if(uint64_t(number(f["maximum"],0,UINT32_MAX))!=maximum)fail("timing width differs");number(f["value"],0,int64_t(maximum));
    if((offsets.count(reg)&&offsets[reg]!=offset)||(names.count(offset)&&names[offset]!=reg))fail("ambiguous timing register identity");offsets[reg]=offset;names[offset]=reg;auto& mask=masks[{reg,offset}];auto bits=uint32_t(maximum<<lo);if(mask&bits)fail("overlapping timing fields");mask|=bits;
   }
  }else fail("unsupported native format: "+format);
 }else if(d.contains("schema")&&d["schema"]=="druta.icx.preset.v1"){
  (void)icx_preset_requests(d,true);out.scope=ProfileScope::icx;add(out,"icx:controller");
 }else if(d.contains("scope")){
  const auto scope=text(d["scope"]);
  if(scope=="rail_limits"){const auto p=parse_python_rail_profile(d);out.scope=ProfileScope::python_rails;out.slot=p.target.slot;if(!p.target.uuid.empty())out.uuid=p.target.uuid;for(const auto& r:p.requests)add(out,"rail:"+std::to_string(r.first.rail)+":"+std::to_string(r.first.field));}
  else if(scope=="power_action"){const auto p=parse_python_power_profile(d);out.scope=ProfileScope::python_power;out.slot=p.target.slot;if(!p.target.uuid.empty())out.uuid=p.target.uuid;add(out,"power:*");}
  else if(scope=="tuning_action"){
   // Validate shared metadata through the exact rail metadata contract.
   auto proxy=d;proxy.erase("tuning_action");proxy["scope"]="rail_limits";proxy["rail_limits"]={{"0",{{"reliability",0}}}};const auto meta=parse_python_rail_profile(proxy);out.slot=meta.target.slot;if(!meta.target.uuid.empty())out.uuid=meta.target.uuid;out.scope=ProfileScope::python_tuning;
   const auto& a=d.at("tuning_action");const auto kind=text(a.at("kind"));if(kind=="memory")keys(a,{"kind","index","value","scale"});else keys(a,{"kind","index","value"});const auto value=number(a["value"]);
   if(kind=="domain"){auto index=uint32_t(number(a["index"],0,31));if(value%1000)fail("clock request not representable in MHz");add(out,domain_target(index));}
   else {if(!a["index"].is_null())fail("unexpected tuning index");if(kind=="memory"){positive(a["scale"]);const auto scale=a["scale"].get<double>();if(scale>=2147483648.0||(value/scale)*scale!=value)fail("memory scale precision differs");add(out,"clock:memory");}else if(kind=="boost"){number(a["value"],0,100);add(out,"voltage:boost");}else if(kind=="rail_offset")add(out,"voltage:nvvdd-offset");else fail("unsupported tuning kind");}
  }else fail("Python scope has no catalog executor: "+scope+" (remaining semantic voltage controls/scaffold offsets and full tunes need typed catalog adapters)");
 }else fail("unrecognized profile format");
 if(out.targets.empty())fail("profile selects no controls");out.valid=true;out.summary=std::string(profile_scope_name(out.scope))+": "+std::to_string(out.targets.size())+" selected targets; fresh preflight required";if(out.scope==ProfileScope::icx)out.summary+="; identity comes from the explicitly selected live controller";return out;
}
void validate_components(const std::vector<ProfileComponent>& components){
 if(components.empty()||components.size()>128)fail("empty or oversized collection");std::vector<std::string> occupied;std::optional<std::string> uuid,slot;
 for(const auto& c:components){text(J(c.name));if(collection(c.document))fail("nested combined profiles are unsupported");const auto d=leaf(c.document);
  if(d.uuid){if(uuid&&uuid!=d.uuid)fail("component GPU UUIDs differ");uuid=d.uuid;}if(d.slot){if(slot&&slot!=d.slot)fail("component adapter slots differ");slot=d.slot;}
  for(const auto& t:d.targets){for(const auto& old:occupied)if(profile_targets_overlap(old,t))fail(c.name+": overlapping profile target "+t);occupied.push_back(t);}
 }
}
}
const char* profile_scope_name(ProfileScope s){switch(s){case ProfileScope::daily:return "Daily / Advanced";case ProfileScope::curve:return "V/F curve";case ProfileScope::scoped:return "Rail / policy requests";case ProfileScope::timing:return "Memory timings";case ProfileScope::icx:return "iCX settings";case ProfileScope::python_tuning:return "Python tuning action";case ProfileScope::python_rails:return "Python rail limits";case ProfileScope::python_power:return "Python coupled power action";case ProfileScope::fan:return "Individual fan requests";case ProfileScope::legacy_timing:return "Timing exchange import";case ProfileScope::voltage_policy:return "Voltage admission / VF flags";case ProfileScope::power_integral:return "Power feedback ratios";case ProfileScope::voltage_tables:return "Voltage requests";case ProfileScope::voltage_limits:return "Voltage definitions / device delays";case ProfileScope::power_saving:return "Power saving";case ProfileScope::clock_gating:return "Clock gating / PCIe speed";case ProfileScope::combined:return "Combined";default:return "Unsupported";}}
bool profile_targets_overlap(const std::string& a,const std::string& b){if(a==b)return true;const auto wildcard=[](const std::string& x,const std::string& y){return x.size()>1&&x.back()=='*'&&y.compare(0,x.size()-1,x,0,x.size()-1)==0;};return wildcard(a,b)||wildcard(b,a);}
std::vector<ProfileComponent> flatten_profile_collection(const J& d){
 if(!collection(d)){auto description=leaf(d);return {{profile_scope_name(description.scope),d}};}
 if(d.contains("format")){keys(d,{"format","version","components"});if(number(d["version"],1,1)!=1)fail("unsupported combined version");}
 else {keys(d,{"schema","scope","saved_at","saved_ts","components"});number(d["schema"],2,2);text(d["saved_at"]);if(!d["saved_ts"].is_number()||!std::isfinite(d["saved_ts"].get<double>()))fail("invalid collection timestamp");}
 if(!d["components"].is_array())fail("invalid components");std::vector<ProfileComponent> result;
 for(const auto& c:d["components"]){keys(c,{"name","state"});if(!d.contains("format")&&(!c["state"].is_object()||!c["state"].contains("schema")||!c["state"]["schema"].is_number_integer()||c["state"]["schema"]!=2))fail("Python combined components require schema 2");result.push_back({text(c["name"]),c["state"]});}validate_components(result);return result;
}
J compose_profiles(const std::vector<ProfileComponent>& components){std::vector<ProfileComponent> flat;
 for(const auto& c:components){if(collection(c.document)){for(auto child:flatten_profile_collection(c.document)){child.name=c.name+" / "+child.name;flat.push_back(std::move(child));}}else flat.push_back(c);}
 validate_components(flat);J children=J::array();for(const auto& c:flat)children.push_back({{"name",c.name},{"state",c.document}});J result={{"format","druta-native-combined"},{"version",1},{"components",std::move(children)}};if(result.dump().size()>profile_max_bytes)fail("combined profile exceeds 1 MiB");return result;
}
ProfileDescription inspect_profile(const J& d){try{if(!collection(d))return leaf(d);auto children=flatten_profile_collection(d);ProfileDescription out;out.scope=ProfileScope::combined;out.valid=true;for(const auto& c:children){const auto child=leaf(c.document);out.targets.insert(out.targets.end(),child.targets.begin(),child.targets.end());if(child.uuid)out.uuid=child.uuid;if(child.slot)out.slot=child.slot;}out.summary="Combined: "+std::to_string(children.size())+" components, "+std::to_string(out.targets.size())+" targets; fresh whole preflight required";return out;}catch(const std::exception& e){ProfileDescription out;out.error=e.what();out.summary=out.error;return out;}}
J select_profile_targets(const J& d,const std::vector<std::string>& selection){const auto desc=inspect_profile(d);if(!desc.valid)fail(desc.error);if(selection.empty())fail("select at least one target");std::set<std::string> selected;
 for(const auto& t:selection)if(std::find(desc.targets.begin(),desc.targets.end(),t)==desc.targets.end()||!selected.insert(t).second)fail("unknown or duplicate target selection");
 if(collection(d)){std::vector<ProfileComponent> children;for(auto c:flatten_profile_collection(d)){auto detail=leaf(c.document);std::vector<std::string> sub;for(const auto& t:detail.targets)if(selected.count(t))sub.push_back(t);if(!sub.empty()){c.document=select_profile_targets(c.document,sub);children.push_back(std::move(c));}}return compose_profiles(children);}
 J out=d;
 if(desc.scope==ProfileScope::daily){for(auto it=out["controls"].begin();it!=out["controls"].end();)if(!selected.count(daily_target(it.key())))it=out["controls"].erase(it);else ++it;
  if(out.contains("advanced")){J entries=J::array();for(const auto& e:out["advanced"])if(selected.count(advanced_target(e)))entries.push_back(e);out["advanced"]=entries;}}
 else if(desc.scope==ProfileScope::scoped){J entries=J::array();for(const auto& e:out["entries"])if(selected.count(scoped_target(e)))entries.push_back(e);out["entries"]=entries;}
 else if(desc.scope==ProfileScope::timing){J fields=J::array();for(const auto& e:out["fields"])if(selected.count("timing:"+e["field"].get<std::string>()))fields.push_back(e);out["fields"]=fields;}
 else if(desc.scope==ProfileScope::voltage_policy)out=select_voltage_policy_profile(out,selection);
 else if(desc.scope==ProfileScope::power_integral)out=select_power_integral_profile(out,selection);
 else if(desc.scope==ProfileScope::voltage_tables)out=select_voltage_tables_profile(out,selection);
 else if(desc.scope==ProfileScope::voltage_limits)out=select_voltage_limits_profile(out,selection);
 else if(desc.scope==ProfileScope::power_saving)out=select_power_saving_profile(out,selection);
 else if(desc.scope==ProfileScope::clock_gating)out=select_clock_gating_profile(out,selection);
 else if(desc.scope==ProfileScope::python_rails){constexpr const char* fields[]={"reliability","alt_reliability","overvoltage","vmin"};J rails=J::object();for(auto r=out["rail_limits"].begin();r!=out["rail_limits"].end();++r)for(uint32_t f=0;f<4;++f)if(r.value().contains(fields[f])&&selected.count("rail:"+r.key()+":"+std::to_string(f)))rails[r.key()][fields[f]]=r.value()[fields[f]];out["rail_limits"]=rails;}
 // Other formats expose one atomic semantic target.
 (void)leaf(out);return out;
}
}
