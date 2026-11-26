// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_effect_verify.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <exception>
#include <limits>
#include <set>
#include <stdexcept>

namespace druta::ui {
namespace {
constexpr unsigned sample_count = 5;
constexpr int32_t step_khz = -45000;
struct P0Unavailable : std::runtime_error {
    P0Unavailable() : std::runtime_error("Verification requires the selected GPU to be at P0") {}
};
void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
struct Lock {
    dc_gpu* gpu;
    const ClockEffectApi& api;
    Lock(dc_gpu* handle, const ClockEffectApi& funcs) : gpu(handle), api(funcs) {
        require(gpu && api.lock && api.unlock && api.lock(gpu) == DC_OK, "Cannot lock the selected GPU session");
    }
    ~Lock() { api.unlock(gpu); }
};
void check_identity(const ClockEffectApi& a, dc_gpu* gpu, const ClockEffectPending& p) {
    dc_gpu_identity id{};
    id.size = sizeof(id);
    require(a.identity && a.identity(gpu, &id) == DC_OK && id.nvapi_ok && id.nvml_ok &&
        id.nvapi_selected && id.nvml_selected && !id.pairing_error[0] &&
        std::memchr(id.nvapi_slot, 0, sizeof(id.nvapi_slot)) && std::memchr(id.nvml_slot, 0, sizeof(id.nvml_slot)) &&
        p.slot == id.nvapi_slot && p.slot == id.nvml_slot &&
        !p.slot.empty() && !p.uuid.empty() && std::memchr(id.slot, 0, sizeof(id.slot)) &&
        std::memchr(id.uuid, 0, sizeof(id.uuid)) && p.slot == id.slot && p.uuid == id.uuid,
        "Clock verification target identity unavailable or changed");
}
dc_clkdom_layout geometry(const ClockEffectApi& a, dc_gpu* gpu) {
    dc_clkdom_layout l{};
    l.size = sizeof(l);
    require(a.layout && a.layout(gpu, &l) == DC_OK && l.version && l.stride && l.block_size,
        "Clock packet layout unavailable");
    return l;
}
dc_clkdom_requests requests(const ClockEffectApi& a, dc_gpu* gpu) {
    dc_clkdom_requests r{};
    r.size = sizeof(r);
    require(a.requests && a.requests(gpu, &r) == DC_OK && r.count && r.count <= DC_CLKDOM_SLOTS,
        "Clock request read failed");
    std::set<unsigned> domains;
    for (unsigned i = 0; i < r.count; ++i)
        require(r.rows[i].domain < DC_CLKDOM_SLOTS && domains.insert(r.rows[i].domain).second,
            "Ambiguous clock request rows");
    return r;
}
const dc_clkdom_request& find_row(const dc_clkdom_requests& r, unsigned domain) {
    for (unsigned i = 0; i < r.count; ++i) if (r.rows[i].domain == domain) return r.rows[i];
    throw std::runtime_error("Clock request row missing");
}
int32_t check_requests(const ClockEffectPending& p, const dc_clkdom_requests& current) {
    require(p.before.count && p.before.count <= DC_CLKDOM_SLOTS && current.count == p.before.count,
        "Clock request domain set changed");
    for (unsigned i = 0; i < p.before.count; ++i) {
        const auto& original = p.before.rows[i];
        const auto& live = find_row(current, original.domain);
        require(original.mode == live.mode && original.has_msvdd_uv == live.has_msvdd_uv &&
            (!live.has_msvdd_uv || live.msvdd_uv == original.msvdd_uv) &&
            (live.domain == p.domain || live.freq_khz == original.freq_khz), "Another clock request or mode changed");
    }
    return find_row(current, p.domain).freq_khz;
}
void check_geometry(const ClockEffectApi& a, dc_gpu* gpu, const ClockEffectPending& p) {
    const auto now = geometry(a, gpu);
    require(std::memcmp(&now, &p.layout, sizeof(now)) == 0, "Clock packet layout changed");
}
dc_telemetry p0(const ClockEffectApi& a, dc_gpu* gpu) {
    dc_telemetry t{};
    t.size = sizeof(t);
    if (!a.telemetry || a.telemetry(gpu, &t) != DC_OK || !(t.present & DC_T_PSTATE) || t.pstate != 0)
        throw P0Unavailable{};
    return t;
}
void qualify(const ClockEffectApi& a, dc_gpu* gpu, const ClockEffectPending& p) {
    dc_outcome cap{};
    require(a.capability && a.capability(gpu, p.domain, find_row(p.before, p.domain).mode, &cap) == DC_OK &&
        cap.ok == 1, "The selected clock field has no qualified offset setter");
}
// Strong aliases identify multiple reporters for one physical clock. Unknown
// domains retain their indices. Approximate frequency agreement is never an alias.
std::string alias(const std::string& name) {
    if (name == "MEM" || name == "memory") return "memory";
    if (name == "GPC" || name == "GPC2CLK" || name == "gpc") return "graphics";
    if (name == "VIDEO" || name == "Video") return "video";
    if (name == "XBAR" || name == "Crossbar" || name == "xbar") return "xbar";
    if (name == "SYSCLK" || name == "SYS" || name == "sys") return "sys";
    return {};
}
void counter(ClockEffectSample& s, const std::string& key, uint32_t khz, const std::string& group = {}) {
    if (!khz || khz > 40000000u) return;
    s.counters_khz[key] = khz;
    if (!group.empty()) s.groups[key] = group;
}
void read_counters(const ClockEffectApi& a, dc_gpu* gpu, const dc_telemetry& t,
                   ClockEffectSample& s, uint32_t arch, bool has_arch) {
    const uint64_t flags[] = {DC_T_CORE, DC_T_MEM, DC_T_VIDEO, DC_T_XBAR};
    const uint32_t mhz[] = {t.core, t.mem, t.video, t.xbar};
    const char* names[] = {"graphics", "memory", "video", "xbar"};
    for (size_t i = 0; i < 4; ++i)
        if ((t.present & flags[i]) && mhz[i] <= 40000)
            counter(s, std::string("telemetry.") + names[i], mhz[i] * 1000);
    dc_public_clock_readings pub{};
    pub.size = sizeof(pub);
    if (a.public_clocks && a.public_clocks(gpu, &pub) == DC_OK) {
        const unsigned types[] = {0, 2, 3};
        for (size_t i = 0; i < 3; ++i) {
            const unsigned type = types[i];
            if ((pub.present & (1u << type)) && pub.mhz[type] <= 40000)
                counter(s, std::string("nvml.") + names[i], pub.mhz[type] * 1000, names[i]);
        }
    }
    if ((t.present & DC_T_PRIV_CLOCKS) && a.classify) {
        dc_clock_domains d{};
        d.size = sizeof(d);
        require(a.classify(t.priv_clocks, DC_PRIV_CLK_WORDS, !!(t.present & DC_T_CORE), t.core,
            !!(t.present & DC_T_MEM), t.mem, has_arch, arch, &d) == DC_OK && d.count <= DC_PRIV_DOMAINS,
            "Private clock counter classification failed");
        std::set<unsigned> seen;
        for (unsigned i = 0; i < d.count; ++i) {
            const auto& row = d.rows[i];
            require(row.domain < DC_PRIV_DOMAINS && seen.insert(row.domain).second &&
                std::memchr(row.name, 0, sizeof(row.name)) && std::memchr(row.grade, 0, sizeof(row.grade)) &&
                std::memchr(row.kind, 0, sizeof(row.kind)), "Malformed private clock counter");
            if (std::strcmp(row.kind, "freq")) continue;
            const std::string id = "private." + std::to_string(row.domain);
            s.private_source_scale[row.domain]={row.srcid,row.scale};
            const auto group = !std::strcmp(row.grade, "confirmed") && has_arch ? alias(row.name) : std::string{};
            counter(s, id + ".A", row.prog_khz, group.empty() ? id : group);
            counter(s, id + ".B", row.meas_khz, group.empty() ? id : group);
        }
    }
    if (a.measurements) {
        dc_clkdom_measurement measured[DC_CLKDOM_SLOTS]{};
        size_t n = 0;
        if (a.measurements(gpu, measured, DC_CLKDOM_SLOTS, &n) == DC_OK && n <= DC_CLKDOM_SLOTS)
            for (size_t i = 0; i < n; ++i) {
                const auto& r = measured[i];
                if (r.has_value && r.name[0] && std::memchr(r.name, 0, sizeof(r.name)))
                    counter(s, "measured." + std::string(r.name), r.value_khz, alias(r.name));
            }
    }
}
void phase(const ClockEffectApi& a, dc_gpu* gpu, const ClockEffectPending& p, int32_t expected,
           std::vector<ClockEffectSample>& out, std::vector<ClockEffectSample>* transition = nullptr) {
    require(bool(a.wait_ms), "A clock sample timer is required");
    uint32_t arch = 0;
    const bool has_arch = a.architecture && a.architecture(gpu, &arch) == DC_OK;
    // B may be a rolling counter with a ~1 s refresh interval. Allow the
    // previous phase to expire and cover multiple refreshes in each window.
    auto take = [&](std::vector<ClockEffectSample>& samples) {
        check_identity(a, gpu, p);
        const auto t = p0(a, gpu);
        ClockEffectSample s;
        s.pstate = t.pstate;
        s.requests = requests(a, gpu);
        require(check_requests(p, s.requests) == expected, "Selected clock request differs from the test phase");
        read_counters(a, gpu, t, s, arch, has_arch);
        samples.push_back(std::move(s));
    };
    if (transition) {
        for (const unsigned delay : {0u,250u,275u,275u}) {
            if (delay) a.wait_ms(delay);
            take(*transition);
        }
        a.wait_ms(450);
    } else a.wait_ms(1250);
    for (unsigned i = 0; i < sample_count; ++i) {
        if (i) a.wait_ms(550);
        take(out);
    }
}
bool setter(const ClockEffectApi& a, dc_gpu* gpu, unsigned domain, int32_t khz) {
    dc_outcome result{};
    return a.set(gpu, domain, khz / 1000.0, &result) == DC_OK && result.ok == 1;
}
bool starts(const std::string& k, const char* prefix) { return k.rfind(prefix, 0) == 0; }
struct Range {
    double low, median, high;
    double width() const { return high - low; }
};
bool range(const std::vector<ClockEffectSample>& phase_samples, const std::string& key, Range& out) {
    if (phase_samples.size() != sample_count) return false;
    std::array<double, sample_count> values{};
    for (size_t i = 0; i < values.size(); ++i) {
        const auto it = phase_samples[i].counters_khz.find(key);
        if (it == phase_samples[i].counters_khz.end()) return false;
        values[i] = it->second;
    }
    std::sort(values.begin(), values.end());
    out = {values[1], values[values.size()/2], values[values.size()-2]};
    return true;
}
bool independent(const ClockEffectResult& r, const std::string& key) {
    if (starts(key, "nvml.") || starts(key, "measured.")) return true;
    if (!starts(key, "private.") || key.back() != 'B') return false;
    const std::string a = key.substr(0, key.size()-1) + 'A';
    // A B field that exactly follows A is also emitted on hardware without
    // an independent counter. Keep the observation but disallow that proof.
    std::vector<double> programmed, residuals;
    const auto scan = [&](const auto& windows, auto action) {
        for (const auto& window : windows) for (const auto& s : window) {
            auto ai=s.counters_khz.find(a), bi=s.counters_khz.find(key);
            if (ai!=s.counters_khz.end() && bi!=s.counters_khz.end())
                action(static_cast<double>(ai->second), static_cast<double>(bi->second));
        }
    };
    auto collect = [&](double av, double){ programmed.push_back(av); };
    scan(r.phases, collect);
    scan(r.transitions, collect);
    if (programmed.empty()) return false;
    auto distinct = [&](double bv) {
        return std::none_of(programmed.begin(),programmed.end(),[&](double av){return std::abs(av-bv)<=1000.0;});
    };
    for (size_t transition=0; transition<2; ++transition) {
        Range previous{}, next{};
        if (!range(r.phases[transition],a,previous) || !range(r.phases[transition+1],a,next)) continue;
        const auto endpoints=std::minmax(previous.median,next.median);
        for (const auto& s:r.transitions[transition]) {
            const auto ai=s.counters_khz.find(a), bi=s.counters_khz.find(key);
            if (ai==s.counters_khz.end() || bi==s.counters_khz.end()) continue;
            const double bv=bi->second;
            if (std::abs(ai->second-next.median)<=1000.0 && bv>endpoints.first+1000.0 &&
                bv<endpoints.second-1000.0 && distinct(bv)) return true;
        }
    }
    scan(r.phases,[&](double av,double bv){if(distinct(bv))residuals.push_back(bv-av);});
    if (residuals.size()<2) return false;
    auto bounds=std::minmax_element(residuals.begin(),residuals.end());
    return *bounds.second-*bounds.first>1000.0;
}
bool comparable(const ClockEffectResult& r, const std::string& key) {
    if (!starts(key,"private.")) return true;
    const auto& base=r.phases[0].front();
    for (const auto& [domain,source_scale]:base.private_source_scale) {
        const auto prefix="private."+std::to_string(domain)+".";
        if (!starts(key,prefix.c_str())) continue;
        const auto unchanged=[&](const auto& windows) {
            for (const auto& window:windows) for (const auto& s:window) {
                const auto it=s.private_source_scale.find(domain);
                if (it==s.private_source_scale.end() || it->second!=source_scale ||
                    !s.groups.count(key) || s.groups.at(key)!=base.groups.at(key)) return false;
            }
            return true;
        };
        return source_scale.second && unchanged(r.phases) && unchanged(r.transitions);
    }
    return false;
}
struct Signature {
    std::set<std::string> groups, independent_groups, programmed_groups;
    std::map<std::string,double> moving;
    bool coherent=true, anchors=true;
};
Signature interpret(ClockEffectResult& r) {
    Signature sig;
    std::vector<std::string> evidence;
    for (const auto& kv : r.phases.front().front().counters_khz) {
        const auto& key = kv.first;
        Range a{}, b{}, c{};
        if (!range(r.phases[0], key, a) || !range(r.phases[1], key, b) || !range(r.phases[2], key, c)) continue;
        const double noise = std::max({1000.0, a.width(), b.width(), c.width()});
        // Windows must be separated in both directions and return to within
        // one baseline noise envelope. A lone perturbed sample earns nothing.
        const double down = std::min(a.low, c.low) - b.high;
        const double up = b.low - std::max(a.high, c.high);
        ClockEffectChange change{key, a.median, b.median, c.median, b.median-a.median};
        change.reversible = std::max(down, up) > noise && std::abs(c.median-a.median) <= std::max({1500.0,a.width(),c.width()});
        const bool comparable_readings=comparable(r,key);
        change.independent = comparable_readings && independent(r, key);
        if (change.reversible && comparable_readings && !starts(key,"telemetry.")) {
            const auto& aliases = r.phases.front().front().groups;
            auto group = aliases.find(key);
            const auto id = group == aliases.end() ? key : group->second;
            sig.moving[key]=change.delta_khz;
            if (change.independent || (starts(key,"private.") && key.back()=='A')) {
                sig.groups.insert(id);
                if (change.delta_khz>=-2000.0) sig.coherent=false;
            }
            if (starts(key,"private.") && key.back()=='A') sig.programmed_groups.insert(id);
            if (change.independent) {
                sig.independent_groups.insert(id);
                evidence.push_back(key);
                if (starts(key,"private.") && key.back()=='B') {
                    const auto ak=key.substr(0,key.size()-1)+'A';
                    const auto prog=sig.moving.find(ak);
                    if (prog==sig.moving.end() || std::abs(prog->second-change.delta_khz)>
                        std::max(5000.0,std::abs(prog->second)*0.35)) sig.coherent=false;
                }
            }
        }
        r.changes.push_back(std::move(change));
    }
    for (const auto* key:{"nvml.graphics","nvml.memory"}) {
        Range x{}, y{}, z{};
        if (!range(r.phases[0],key,x) || !range(r.phases[1],key,y) || !range(r.phases[2],key,z)) {
            sig.anchors=false;
        } else if (std::max({x.high,y.high,z.high})-std::min({x.low,y.low,z.low})>2000.0) {
            sig.anchors=false;
            // An unaccounted performance-state drift can move an unrelated
            // clock inside an apparently clean A/B/A interval.
            if (!sig.moving.count(key)) sig.coherent=false;
        }
    }
    for (const auto& c : r.changes) {
        if (!(c.independent && c.reversible)) continue;
        const auto& aliases=r.phases.front().front().groups;
        const auto it=aliases.find(c.counter);
        const bool supported=it!=aliases.end() && sig.programmed_groups.count(it->second);
        if (!supported && std::abs(c.delta_khz)<std::abs(step_khz)*0.20) sig.coherent=false;
    }
    if (sig.independent_groups.empty()) {
        r.message = "No independently reported clock moved reproducibly and returned at P0";
    } else if (!sig.coherent) {
        r.message = "Independent clock response disagrees with the requested direction or programmed movement";
    } else if (sig.groups.size()>1) {
        r.message = "Multiple clock groups moved; unique attribution is ambiguous";
    } else {
        r.verified=true;
        r.message="P0 clock response verified and original request restored";
    }
    for (const auto& name : evidence) r.message += " " + name;
    r.affected_groups.assign(sig.groups.begin(),sig.groups.end());
    r.physical_groups.assign(sig.independent_groups.begin(),sig.independent_groups.end());
    r.programmed_groups.assign(sig.programmed_groups.begin(),sig.programmed_groups.end());
    return sig;
}
void restore(const ClockEffectApi& a, dc_gpu* gpu, ClockEffectPending& p, bool& attempted) {
    require(p.active, "No outstanding clock verification request");
    require(p.domain < DC_CLKDOM_SLOTS && p.before.count && p.before.count <= DC_CLKDOM_SLOTS &&
        p.original_khz % 1000 == 0 && p.original_khz >= std::numeric_limits<int32_t>::min() - step_khz &&
        p.test_khz == p.original_khz + step_khz && find_row(p.before,p.domain).freq_khz == p.original_khz,
        "Clock restoration receipt is inconsistent");
    check_identity(a, gpu, p);
    check_geometry(a, gpu, p);
    auto live = check_requests(p, requests(a, gpu));
    if (live != p.original_khz) {
        require(live == p.test_khz, "Foreign clock offset found; restoration withheld");
        qualify(a, gpu, p);
        check_identity(a, gpu, p);
        require(a.set && check_requests(p, requests(a, gpu)) == p.test_khz,
            "Request changed before clock restoration");
        (void)p0(a, gpu);
        // A setter may return failure after changing the request. Readback
        // determines whether there is still an outstanding restoration.
        attempted = true;
        (void)setter(a, gpu, p.domain, p.original_khz);
    }
    check_identity(a, gpu, p);
    require(check_requests(p, requests(a, gpu)) == p.original_khz, "Original clock request has not been restored");
    p.active = false;
}
} // namespace

ClockEffectResult restore_clock_effect(const ClockEffectApi& a, dc_gpu* gpu, ClockEffectPending& p) {
    ClockEffectResult result;
    try {
        Lock lock(gpu, a);
        restore(a, gpu, p, result.attempted);
        result.restored = true;
        result.message = "Original clock request verified";
    } catch (const std::exception& e) {
        result.p0_lost = dynamic_cast<const P0Unavailable*>(&e) != nullptr;
        result.message = e.what();
    }
    return result;
}

ClockEffectResult verify_clock_effect(const ClockEffectApi& a, dc_gpu* gpu, const std::string& slot,
                                     const std::string& uuid, uint32_t domain, ClockEffectPending& pending) {
    ClockEffectResult r;
    if (pending.active) { r.message = "An earlier clock request still requires restoration"; return r; }
    try {
        Lock lock(gpu, a);
        ClockEffectPending start{};
        start.slot = slot; start.uuid = uuid; start.domain = domain;
        check_identity(a, gpu, start);
        require(a.set && a.requests && a.telemetry && a.wait_ms, "Clock verification interfaces unavailable");
        start.layout = geometry(a, gpu);
        start.before = requests(a, gpu);
        start.original_khz = find_row(start.before, domain).freq_khz;
        require(start.original_khz % 1000 == 0 && start.original_khz >= std::numeric_limits<int32_t>::min() - step_khz,
            "Clock offset is outside the restorable range");
        start.test_khz = start.original_khz + step_khz;
        auto cycle = [&](ClockEffectResult& cycle_result) {
            qualify(a, gpu, start);
            phase(a, gpu, start, start.original_khz, cycle_result.phases[0]);
            const auto& last = cycle_result.phases[0].back().counters_khz;
            require(std::any_of(last.begin(), last.end(), [](const auto& item) {
                return starts(item.first,"nvml.") || starts(item.first,"measured.") ||
                    (starts(item.first,"private.") && item.first.back()=='B');
            }), "No measured clock or independent public clock is readable");
            check_identity(a, gpu, start);
            check_geometry(a, gpu, start);
            qualify(a, gpu, start);
            require(check_requests(start, requests(a, gpu)) == start.original_khz, "Original clock request changed before test");
            (void)p0(a, gpu);
            pending = start;
            pending.active = true;
            r.attempted = true;
            r.restored = false;
            require(setter(a, gpu, domain, start.test_khz), "Clock setter reported failure");
            require(check_requests(start, requests(a, gpu)) == start.test_khz, "Test offset readback did not match");
            phase(a, gpu, start, start.test_khz, cycle_result.phases[1], &cycle_result.transitions[0]);
            restore(a, gpu, pending, r.attempted);
            r.restored = true;
            phase(a, gpu, start, start.original_khz, cycle_result.phases[2], &cycle_result.transitions[1]);
            return interpret(cycle_result);
        };
        const auto first = cycle(r);
        if (first.groups.size()>1 && !first.independent_groups.empty() && first.coherent && first.anchors) {
            ClockEffectResult second;
            Signature repeat;
            std::exception_ptr failure;
            try { repeat = cycle(second); } catch (...) { failure=std::current_exception(); }
            r.confirmation_phases=std::move(second.phases);
            r.confirmation_transitions=std::move(second.transitions);
            r.confirmation_changes=std::move(second.changes);
            if (failure) std::rethrow_exception(failure);
            // Compare raw B responses too. B may closely track the programmed
            // value in one trial and demonstrate independence in the other.
            bool same = repeat.coherent && repeat.anchors && repeat.groups==first.groups &&
                !repeat.independent_groups.empty();
            for (const auto& [key, delta] : first.moving) {
                const auto it = repeat.moving.find(key);
                same = same && it!=repeat.moving.end() &&
                    std::abs(it->second-delta)<=std::max(4000.0,0.25*std::abs(delta));
            }
            same = same && first.moving.size()==repeat.moving.size();
            if (same) {
                r.verified=r.coupled=true;
                auto physical=first.independent_groups;
                physical.insert(repeat.independent_groups.begin(),repeat.independent_groups.end());
                r.physical_groups.assign(physical.begin(),physical.end());
                r.message="Two restored P0 trials verified a coupled response. Independent clock readings:";
                for (const auto& group:r.physical_groups) r.message+=" "+group;
                if (!r.programmed_groups.empty()) {
                    r.message+=". Programmed clocks:";
                    for (const auto& group:r.programmed_groups) r.message+=" "+group;
                }
            } else r.message="Clock response differed on the second restored trial; attribution remains ambiguous";
        }
        r.completed=true;
    } catch (const std::exception& e) {
        r.p0_lost = dynamic_cast<const P0Unavailable*>(&e) != nullptr;
        r.message = e.what();
        if (pending.active) {
            const auto recovery = restore_clock_effect(a, gpu, pending);
            r.restored = recovery.restored;
            r.p0_lost |= recovery.p0_lost;
            r.message += ". " + recovery.message;
        }
    }
    return r;
}
}
