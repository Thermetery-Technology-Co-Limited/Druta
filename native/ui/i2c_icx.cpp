/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "i2c_driver.h"
#include <algorithm>
#include <cmath>
#include <map>

namespace druta::ui {
namespace {
struct Spec { const char* key; const char* label; uint8_t state, command, index; int min, max, step; const char* unit; };
constexpr Spec specs[] = {
    {"nvvdd", "NVVDD request", 0x19, 0x10, 1, 750, 1150, 5, "mV"},
    {"fbvdd", "FBVDD request", 0x19, 0x18, 8, 1250, 1450, 5, "mV"},
    {"frequency_nvvdd", "NVVDD switching frequency", 0x1c, 0x13, 1, 300, 1000, 100, "kHz"},
    {"frequency_fbvdd", "FBVDD switching frequency", 0x1c, 0x1f, 2, 300, 1000, 100, "kHz"},
    {"ocp_nvvdd", "NVVDD OCP", 0x1b, 0x12, 1, 0, 1, 1, ""},
    {"ocp_fbvdd", "FBVDD OCP", 0x1b, 0x1e, 2, 0, 1, 1, ""},
    {"loadline", "Loadline", 0x1a, 0x11, 1, 0, 15, 1, ""},
};
const Spec* spec(const std::string& key) { for (const auto& s : specs) if (key == s.key) return &s; return nullptr; }
size_t length(uint8_t r) { return r == 0xb1 ? 6 : r == 0xb2 ? 5 : r == 0x19 ? 10 : 3; }
struct Captured { int initial, last, previous; bool auto_initial, auto_last, auto_previous; };
class Icx final : public ControllerDriver {
    ControllerIo& io_; I2cRoute route_; bool verified_ = false, pending_ = false, gate_touched_ = false;
    std::map<std::string, Captured> owned_;
    bool block(uint8_t reg, std::vector<uint8_t>& data, bool retry = true) {
        for (int n = 0; n < (retry ? 3 : 1); ++n) {
            if (n) io_.sleep_ms(n == 1 ? 50 : 100);
            data.clear();
            if (!io_.controller_bound()) return false;
            if (io_.read_bytes(route_, reg, length(reg), data))
                return data.size() == length(reg) && data[0] == data.size() - 1 && io_.controller_bound();
        }
        return false;
    }
    bool identity(bool retry = true) {
        std::vector<uint8_t> b;
        return io_.architecture() == uint32_t(7) && route_.port == 1 && route_.addr7 == 0x2d && block(0xb1, b, retry) &&
               b[0] == 5 && b[1] == 0xfc && b[2] == 0x2d && b[3] == 0x13;
    }
    bool decode(const Spec& s, const std::vector<uint8_t>& b, int& v, bool& automatic) {
        automatic = false;
        if (s.state == 0x19) {
            if (s.index == 1) {
                if (b[1] > 1) return false;
                automatic = b[1] == 0;
                v = (int(b[2]) | int(b[3]) << 8) * 5;
                return automatic || v != 0;
            }
            v = (int(b[8]) | int(b[9]) << 8) * 5;
            return v != 0;
        }
        v = b[s.index];
        if (s.state == 0x1c) { if (v < 3 || v > 10) return false; v *= 100; return true; }
        return v <= s.max;
    }
    bool enabled() {
        std::vector<uint8_t> b;
        if (!block(0xb2, b) || (b[1] != 0xfc && b[1] != 0xfe)) return false;
        if (b[1] == 0xfc) return true;
        gate_touched_ = true;
        if (!io_.write_bytes(I2cFamily::icx, route_, 0xb2, {4,0xc6,0xeb,0xea,0x15})) return false;
        for (unsigned delay : {0u,100u,300u,600u,500u}) {
            io_.sleep_ms(delay);
            if (block(0xb2,b,false) && b[1] == 0xfc) return true;
        }
        return false;
    }
    I2cResult send(const Spec& s, int target, bool automatic, bool restoring) {
        std::vector<uint8_t> before;
        int previous = 0; bool previous_auto = false;
        if (!identity() || !block(s.state, before) || !decode(s, before, previous, previous_auto))
            return {false,false,pending_,"iCX identity or selected state is unavailable or invalid"};
        auto existing = owned_.find(s.key);
        if (existing == owned_.end() && s.state == 0x19 && !previous_auto &&
            (previous < s.min || previous > (s.index == 1 ? 1600 : s.max) || previous % 5))
            return {false,false,pending_,"Captured iCX request cannot be restored through the qualified packet contract; no write sent"};
        if (!restoring && existing != owned_.end() &&
            !(previous_auto == existing->second.auto_last && (previous_auto || previous == existing->second.last)))
            return {false,false,true,"iCX selected state changed outside this session; new request refused"};
        if (restoring && (existing == owned_.end() ||
            !((previous_auto == existing->second.auto_last && (previous_auto || previous == existing->second.last)) ||
              (previous_auto == existing->second.auto_previous && (previous_auto || previous == existing->second.previous)) ||
              (previous_auto == automatic && (automatic || previous == target)))))
            return {false,false,true,"iCX selected state changed outside this session; restoration refused"};
        if (previous_auto == automatic && (automatic || previous == target)) return {true,false,pending_,"iCX request already matches"};
        std::map<uint8_t,std::vector<uint8_t>> collateral;
        if (s.state != 0x19) for (uint8_t reg : {uint8_t(0x19),uint8_t(0x1a),uint8_t(0x1b),uint8_t(0x1c)}) {
            std::vector<uint8_t> b; if (reg != s.state && block(reg,b)) collateral[reg] = b;
        }
        if (!enabled()) return {false,gate_touched_,pending_,"iCX gate enable unavailable or unverified; no setting request sent. Gate may remain enabled."};
        if (existing == owned_.end()) {
            owned_[s.key] = {previous,target,previous,previous_auto,automatic,previous_auto};
        } else { existing->second.previous = previous; existing->second.auto_previous = previous_auto; existing->second.last = target; existing->second.auto_last = automatic; }
        pending_ = true;
        std::vector<uint8_t> payload;
        if (s.state == 0x19) {
            int count = target / 5;
            payload = {3, uint8_t(automatic ? 0 : 1), uint8_t(automatic ? 0 : count & 255), uint8_t(automatic ? 0 : count >> 8)};
        } else payload = {1, uint8_t(s.state == 0x1c ? target / 100 : target)};
        if (!io_.write_bytes(I2cFamily::icx, route_, s.command, payload))
            return {false,true,true,"iCX write outcome uncertain; no retry sent. Restoration remains available."};
        bool matched = false;
        for (unsigned delay : {0u,100u,300u,600u,500u}) {
            io_.sleep_ms(delay); std::vector<uint8_t> after;
            if (!block(s.state,after,false)) continue;
            bool preserved = true;
            for (size_t i = 0; i < before.size(); ++i) {
                bool selected = s.state == 0x19 ? (s.index == 1 ? i >= 1 && i <= 3 : i >= 7 && i <= 9) : i == s.index;
                if (!selected && before[i] != after[i]) preserved = false;
            }
            int value = 0; bool is_auto = false;
            if (!preserved) return {false,true,true,"iCX unrelated or unknown state changed; no further write sent"};
            if (decode(s,after,value,is_auto) && is_auto == automatic && (automatic || value == target)) { matched = true; break; }
        }
        if (!matched) return {false,true,true,"iCX request readback was not verified; no further write sent"};
        for (const auto& [reg, old] : collateral) { std::vector<uint8_t> now; if (block(reg,now) && now != old)
            return {false,true,true,"iCX unrelated setting changed; no further write sent"}; }
        owned_[s.key].previous = target; owned_[s.key].auto_previous = automatic;
        pending_ = false;
        return {true,true,false,"iCX controller request verified; physical voltage is not measured. B2 may remain enabled."};
    }
public:
    Icx(ControllerIo& io, I2cRoute route) : io_(io), route_(route) {}
    bool probe() override { return identity(false); }
    ControllerView read(bool xoc) override {
        ControllerView v; v.name = "EVGA iCX"; v.owned = owned(); v.recovery = pending_; v.verified = verified_;
        v.needs_verification = false;
        if (!identity()) { v.stale = true; v.message = "iCX identity unavailable or changed"; return v; }
        v.readable = v.capable = true; v.message = "Controller requests, not physical voltage; B2 enable has no known disable command.";
        std::map<uint8_t,std::vector<uint8_t>> groups;
        for (uint8_t r : {uint8_t(0x19),uint8_t(0x1a),uint8_t(0x1b),uint8_t(0x1c)}) { std::vector<uint8_t> b; if (block(r,b)) groups[r] = b; }
        for (const auto& s : specs) {
            auto it = groups.find(s.state); int value = 0; bool automatic = false;
            if (it == groups.end() || !decode(s,it->second,value,automatic)) continue;
            I2cField f; f.key=s.key; f.label=s.label; f.unit=s.unit; f.value = automatic ? std::optional<double>{} : std::optional<double>{double(value)};
            f.minimum=s.min; f.maximum=(s.key == std::string("nvvdd") && xoc) ? 1600 : s.max; f.step=s.step; f.writable=true;
            if (s.key == std::string("nvvdd")) { f.resettable=true; f.reset_label="Auto"; if (automatic) f.detail="Auto"; }
            if (s.state == 0x1b) f.choices={{"Disabled",0},{"Enabled",1}};
            v.fields.push_back(f);
        }
        return v;
    }
    I2cResult verify(const ControllerContext&) override {
        if (!identity()) return {false,false,pending_,"iCX identity unavailable or invalid"};
        auto view = read(false); if (view.fields.empty()) return {false,false,pending_,"iCX setting states are unavailable or not understood"};
        verified_ = true; return {true,false,pending_,"iCX identity and readable settings verified read-only; physical voltage requires a meter"};
    }
    I2cResult apply(const ControllerRequest& request, const ControllerContext& context) override {
        const auto* s = spec(request.key);
        if (!s || !context.gate) return {false,false,pending_,"iCX request requires a known field and acknowledged gate"};
        bool automatic = request.reset && request.key == "nvvdd";
        if (request.reset && !automatic) return {false,false,pending_,"No reset is defined for this iCX setting"};
        double value = request.value; int maximum = request.key == "nvvdd" && context.xoc ? 1600 : s->max;
        if (!automatic && (!std::isfinite(value) || value < s->min || value > maximum || std::floor(value) != value || int(value) % s->step))
            return {false,false,pending_,"iCX requested value is outside the understood range or step"};
        return send(*s, automatic ? 0 : int(value), automatic, false);
    }
    I2cResult restore() override {
        for (auto it = owned_.begin(); it != owned_.end();) {
            const auto* s = spec(it->first); auto captured = it->second;
            auto result = send(*s,captured.initial,captured.auto_initial,true);
            if (!result.ok) { pending_=true; result.recovery_pending=true; return result; }
            it = owned_.erase(it);
        }
        pending_=false; return {true,gate_touched_,false,"Captured iCX requests restored. B2 may remain enabled; no disable command is known."};
    }
    bool owned() const override { return !owned_.empty() || pending_; }
    bool needs_clock_clamp() const override { return true; }
};
}
std::unique_ptr<ControllerDriver> make_icx(ControllerIo& io, I2cRoute route) { return std::make_unique<Icx>(io,route); }
}
