// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "backend/clkdom.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>

#include "backend/nvapi.h"

namespace druta::backend {
namespace {

using HandlePtrFn = int(DRUTA_CDECL*)(void*, void*);
constexpr std::uint32_t ClkDomCtlGet = 0xF58938F5;
constexpr std::uint32_t ClkDomCtlSet = 0xD14B69CF;
constexpr std::uint32_t ClkMeasureFreq = 0x527FC458;  // NvAPI_GPU_ClockCtrl_ClkMeasureFreq, read-only
constexpr std::uint32_t kClkMeasureVersion = 0x0001000C;
constexpr std::uint32_t kArchKepler = 2, kArchPascal = 4, kArchTuring = 6, kArchAmpere = 7, kArchAda = 8,
                        kArchBlackwell = 10;
constexpr double kInt32Min = -2147483648.0, kInt32Limit = 2147483648.0;
constexpr double kRetryDelays[] = {1.0, 3.0};  // _CLKDOM_RETRY_DELAYS
constexpr unsigned kRetryCount = 2;

// Control index -> name. Each row was established by writing +45 MHz to the
// control with the clock pinned and recording which private domain moved.
const std::map<std::uint32_t, std::string> kClkDomNames = {{0, "GPC"}, {1, "XBAR"}, {2, "MEM"},
                                                           {3, "SYS"}, {5, "VIDEO"}, {9, "LTC"}};
const std::map<std::uint32_t, std::string> kAmpereControls = {{1, "Crossbar"}, {2, "Memory"}, {3, "SYS"},
                                                              {5, "Video"}, {9, "LTC"}};
const std::map<std::uint32_t, std::string> kAdaControls = {{1, "Crossbar"}, {2, "Memory"}, {3, "SYS"},
                                                           {5, "Video"}, {9, "LTC?"}};
const std::map<std::uint32_t, std::string> kBlackwellControls = {{1, "XBAR"}, {2, "MEM"}, {3, "SYSCLK"}, {4, "VIDEO"}};
// Logical-to-wire signs for the Blackwell controls (the UI sign is the wire sign).
const std::map<std::uint32_t, std::int32_t> kBlackwellPolarity = {{1, 1}, {3, 1}, {4, 1}};
// Which PRIVATE domain each CONTROL moves, per architecture; measured, never inferred from a name.
const std::map<std::uint32_t, std::uint32_t> kPairTuring = {{1, 1}, {3, 2}, {5, 21}, {9, 5}};
const std::map<std::uint32_t, std::uint32_t> kPairPascal = {};  // stored and ignored on GP102/GP106: telemetry only
const std::map<std::uint32_t, std::uint32_t> kPairAmpere = {{1, 1}, {2, 4}, {3, 2}, {5, 21}, {9, 5}};
const std::map<std::uint32_t, std::uint32_t> kPairAda = {{1, 1}, {2, 4}, {3, 2}, {5, 21}, {9, 5}};
// (architecture, control) -> whether the stored delta applies; absent is "not measured".
const std::map<std::pair<std::uint32_t, std::int64_t>, bool> kDeltaApplies = {
    {{kArchPascal, 2}, true}, {{kArchPascal, 1}, false}, {{kArchPascal, 3}, false}, {{kArchPascal, 5}, false},
    {{kArchTuring, 2}, true}, {{kArchBlackwell, 2}, true}};
const std::map<std::pair<std::uint32_t, std::int64_t>, bool> kDeltaClearsCeiling = {{{kArchPascal, 2}, true},
                                                                                    {{kArchBlackwell, 2}, false}};
const char* rail_name(std::int64_t rail) { return rail == 0 ? "core rail (NVVDD)" : "MSVDD"; }

// Python's :g, :+g, :+.0f and :+.2f (the C runtime's rounding of a half is
// an unverified Windows parity edge; see the README).
std::string g_format(double value) { char t[64]; std::snprintf(t, sizeof t, "%g", value); return t; }
std::string plus_g(double value) { char t[64]; std::snprintf(t, sizeof t, "%+g", value); return t; }
std::string plus_0f(double value) { char t[64]; std::snprintf(t, sizeof t, "%+.0f", value); return t; }
std::string plus_2f(double value) { char t[64]; std::snprintf(t, sizeof t, "%+.2f", value); return t; }

std::string int_list(const std::vector<std::uint32_t>& values) {  // Python: str(list)
    std::string out = "[";
    for (std::size_t i = 0; i < values.size(); ++i) {
        if (i) out += ", ";
        out += std::to_string(values[i]);
    }
    return out + "]";
}

std::string arch_text(std::optional<std::uint32_t> architecture) {  // Python: f"{architecture}"
    return architecture ? std::to_string(*architecture) : "None";
}

// Python: -(1 << 31) <= value < (1 << 31), on the double Python computed.
bool fits_i32(double value) { return kInt32Min <= value && value < kInt32Limit; }

bool same_block(const ClkDomBlock& a, const ClkDomBlock& b, const ClkDomLayout& layout) {
    return std::equal(a.words.begin(), a.words.begin() + static_cast<std::ptrdiff_t>(layout.size / 4),
                      b.words.begin());
}

std::vector<std::uint32_t> differing_words(const ClkDomBlock& a, const ClkDomBlock& b, const ClkDomLayout& layout) {
    std::vector<std::uint32_t> out;
    for (std::uint32_t i = 0; i < layout.size / 4; ++i)
        if (a.words[i] != b.words[i]) out.push_back(i);
    return out;
}

}  // namespace

std::int32_t ClkDomBlock::word(std::uint32_t byte_offset) const {
    std::int32_t value;
    std::memcpy(&value, &words[byte_offset / 4], sizeof value);
    return value;
}

void ClkDomBlock::set_word(std::uint32_t byte_offset, std::int32_t value) {
    std::memcpy(&words[byte_offset / 4], &value, sizeof value);
}

bool ClkDom::ok() const {
    const Nvapi& a = gpu_.nvapi;
    return a.ok && a.interface(ClkDomCtlGet);
}

std::pair<int, ClkDomBlock> ClkDom::get(std::uint32_t mask) {
    // (status, buffer): the buffer is the driver's own bytes, untouched.
    ClkDomBlock block;
    block.words.assign(kClkDomBufBytes / 4, 0);
    block.words[0] = kClkDomLayoutTuring.version;
    block.words[kClkDomLayoutTuring.mask_dword] = mask;
    Nvapi& a = gpu_.nvapi;
    auto fn = a.get<HandlePtrFn>(ClkDomCtlGet);
    int status = fn ? fn(a.gpu, block.words.data()) : -1;
    return {status, std::move(block)};
}

bool ClkDom::valid_block(const ClkDomBlock& block, const ClkDomLayout& layout, std::uint32_t mask) {
    if (block.words.size() * 4 < layout.size) return false;
    return block.words[0] == layout.version && block.words[layout.mask_dword] == mask;
}

const std::vector<std::uint32_t>& ClkDom::domains() {
    // Not a constant: the mask rejects the WHOLE call with -1 if any bit is
    // invalid, so the accepted set is probed one bit at a time.
    if (!valid_) {
        std::vector<std::uint32_t> found;
        std::map<std::uint32_t, std::string> errors;
        if (ok()) {
            for (std::uint32_t d = 0; d < kClkDomSlots; ++d) {
                auto [st, block] = get(1u << d);
                if (st == 0 && valid_block(block, kClkDomLayoutTuring, 1u << d))
                    found.push_back(d);
                else
                    errors[d] = st ? "status " + std::to_string(st) : "version/mask echo mismatch";
            }
        }
        valid_ = std::move(found);
        probe_errors_ = std::move(errors);
    }
    return *valid_;
}

bool ClkDom::is_blackwell() {
    // Slider layouts are selected by NVML's architecture enum, never by a
    // marketing name, a device id or a VBIOS.
    auto arch = telemetry_.arch();
    return arch && *arch == kArchBlackwell;
}

const ClkDomLayout* ClkDom::candidate(std::optional<std::uint32_t> architecture) const {
    if (!architecture) return nullptr;
    if (*architecture == kArchBlackwell) return &kClkDomLayoutBlackwell;
    if (*architecture == kArchPascal || *architecture == kArchTuring || *architecture == kArchAmpere ||
        *architecture == kArchAda)
        return &kClkDomLayoutTuring;
    return nullptr;
}

const ClkDomLayout* ClkDom::layout() {
    // Select a measured candidate by architecture, then require a successful
    // one-domain GET with an exact version and mask echo. Unknown and
    // unmeasured generations fail closed: GK104 and GM107 echo Turing-shaped
    // zero records, which does not establish their field meanings.
    auto architecture = telemetry_.arch();
    const ClkDomLayout* layout = candidate(architecture);
    auto fail = [&](const char* kind, std::string text) {
        layout_cache_ = LayoutCache::failed;
        layout_cached_ = nullptr;
        capability_error_ = CapabilityError{kind, std::move(text)};
        return nullptr;
    };
    if (!layout)
        return fail("unsupported_layout",
                    "clock-control field layout is unknown for architecture " + arch_text(architecture));
    if (layout_cache_ != LayoutCache::none) return layout_cache_ == LayoutCache::valid ? layout_cached_ : nullptr;
    if (!ok()) return fail("unavailable", "clock-control getter is unavailable; Refresh capabilities retries discovery");
    if (layout->header + kClkDomSlots * layout->stride != layout->size)
        return fail("unsupported_layout", "clock-control packet geometry is invalid");
    // An accepted one-hot mask rather than an assumed domain 0: read-only.
    const std::vector<std::uint32_t>& accepted = domains();
    if (accepted.empty())
        return fail("unavailable", "no clock-control domain answered; Refresh capabilities retries discovery");
    auto [st, block] = get(1u << accepted[0]);
    if (st != 0)
        return fail("read_failure", "clock-control validation read failed with status " + std::to_string(st));
    if (!valid_block(block, *layout, 1u << accepted[0]))
        return fail("unsupported_response", "clock-control response did not echo the expected version and mask");
    layout_cache_ = LayoutCache::valid;
    layout_cached_ = layout;
    capability_error_.reset();
    return layout;
}

const std::map<std::uint32_t, std::string>& ClkDom::understood(std::optional<std::uint32_t> architecture) {
    // Control indices whose frequency field has a measured effect.
    if (architecture && *architecture == kArchAmpere) return kAmpereControls;
    if (architecture && *architecture == kArchAda) return kAdaControls;
    if (architecture && *architecture == kArchBlackwell) return kBlackwellControls;
    return kClkDomNames;
}

std::vector<std::uint32_t> ClkDom::controls_for_ui() {
    // Understood controls accepted by this driver's validated layout. Getter
    // pairing is optional telemetry, not control authorization.
    if (!layout()) return {};
    const auto& known = understood(telemetry_.arch());
    const std::vector<std::uint32_t>& accepted = domains();
    std::vector<std::uint32_t> out;
    for (const auto& [control, name] : known)
        if (std::find(accepted.begin(), accepted.end(), control) != accepted.end()) out.push_back(control);
    return out;  // map order is sorted order
}

std::int32_t ClkDom::control_polarity(std::int64_t control) {
    if (!is_blackwell()) return 1;
    if (control < 0 || control > 0xFFFFFFFF) return 1;
    auto it = kBlackwellPolarity.find(static_cast<std::uint32_t>(control));
    return it == kBlackwellPolarity.end() ? 1 : it->second;
}

std::string ClkDom::control_label(std::int64_t control) {
    auto arch = telemetry_.arch();
    const std::map<std::uint32_t, std::string>* table = &kClkDomNames;
    const char* fallback = "domain ";
    if (arch && *arch == kArchAmpere) table = &kAmpereControls;
    else if (arch && *arch == kArchAda) table = &kAdaControls;
    else if (arch && *arch == kArchBlackwell) { table = &kBlackwellControls; fallback = "control "; }
    if (control >= 0 && control <= 0xFFFFFFFF) {
        auto it = table->find(static_cast<std::uint32_t>(control));
        if (it != table->end()) return it->second;
    }
    return fallback + std::to_string(control);
}

std::optional<std::uint32_t> ClkDom::step_mhz() {
    // Blackwell's block takes signed kHz requests; the UI keeps 1 MHz request
    // precision rather than borrowing the graphics V/F table's step.
    if (is_blackwell()) return 1;
    return std::nullopt;
}

std::optional<bool> ClkDom::delta_inert(std::optional<std::int64_t> domain) {
    auto arch = telemetry_.arch();
    if (!arch || !domain) return std::nullopt;
    auto it = kDeltaApplies.find({*arch, *domain});
    if (it == kDeltaApplies.end()) return std::nullopt;
    return !it->second;
}

bool ClkDom::offset_slider_offered(std::int64_t control) {
    // Pascal accepts the XBAR, SYS and VIDEO records and ignores them; only
    // the measured memory control 2 keeps a slider there.
    auto arch = telemetry_.arch();
    if (arch && *arch == kArchPascal && control != 2) return false;
    return true;
}

std::optional<bool> ClkDom::delta_clears_ceiling(std::int64_t domain) {
    auto arch = telemetry_.arch();
    if (!arch) return std::nullopt;
    auto it = kDeltaClearsCeiling.find({*arch, domain});
    if (it == kDeltaClearsCeiling.end()) return std::nullopt;
    return it->second;
}

std::map<std::uint32_t, std::uint32_t> ClkDom::pairing(const std::vector<PrivDomainRow>* rows) {
    // Gated on the populated-domain signature, not on which domain the namer
    // called GPC: at deep idle several domains correlate with the core clock
    // equally well, and populated-or-not does not move with the operating point.
    auto arch = telemetry_.arch();
    if (arch && *arch == kArchKepler) return {};
    if (arch && *arch == kArchAmpere) { pair_ = kPairAmpere; return *pair_; }
    if (arch && *arch == kArchAda) { pair_ = kPairAda; return *pair_; }
    if (pair_ && !pair_->empty()) return *pair_;
    std::vector<PrivDomainRow> own;
    if (!rows) {
        Snapshot d = telemetry_.read();
        if (d.priv_clocks) own = read_clock_domains(*d.priv_clocks, d.core, d.mem, is_blackwell(), arch);
        rows = &own;
    }
    std::set<std::uint32_t> live;
    for (const PrivDomainRow& r : *rows)
        if (r.prog_mhz && *r.prog_mhz != 0.0) live.insert(r.domain);
    std::map<std::uint32_t, std::uint32_t> pair;
    if (live.count(0)) pair = kPairTuring;        // TU102 runs its GPU clock at domain 0
    else if (live.count(15)) pair = kPairPascal;  // GP102 puts it at 15, doubled
    else return {};                               // nothing recognised: never cached, only "not yet"
    pair_ = pair;
    return pair;
}

std::optional<std::uint32_t> ClkDom::clk_measure_freq(std::uint32_t mask) {
    Nvapi& a = gpu_.nvapi;
    auto fn = a.ok ? a.get<HandlePtrFn>(ClkMeasureFreq) : nullptr;
    if (!fn) return std::nullopt;
    std::uint32_t words[3] = {kClkMeasureVersion, mask, 0};
    if (fn(a.gpu, words) != 0) return std::nullopt;
    // Garbage guard with headroom above GDDR7's 14.8 GHz memory counter.
    std::uint32_t value = words[2];
    if (value < 1000 || value > 40000000) return std::nullopt;
    return value;
}

std::vector<std::pair<std::string, std::optional<std::uint32_t>>> ClkDom::measurements() {
    if (!is_blackwell()) return {};
    // CLKMEASURE_MASKS are DOMAIN INDICES into the private numbering (domain 4 is MEM), not bits.
    std::vector<std::pair<std::string, std::optional<std::uint32_t>>> out;
    for (const auto& [name, index] : std::vector<std::pair<const char*, std::uint32_t>>{
             {"gpc", 0}, {"xbar", 1}, {"sys", 2}, {"memory", 4}})
        out.emplace_back(name, clk_measure_freq(index));
    return out;
}

std::pair<std::optional<std::map<std::uint32_t, ClkDomRequest>>, std::string> ClkDom::read_requests() {
    // A failed request read stays pending until the retry publisher reports
    // recovery or an explicit refresh resets discovery.
    if (!ok()) return {std::nullopt, "per-domain clock control unavailable (0xF58938F5)"};
    const ClkDomLayout* lay = layout();
    if (!lay) return {std::nullopt, "clock-domain layout is not validated for this generation/runtime ABI"};
    const std::vector<std::uint32_t>& doms = domains();
    if (doms.empty()) return {std::nullopt, "no clock domain accepted by this driver"};
    std::uint32_t mask = 0;
    for (std::uint32_t d : doms) mask |= 1u << d;
    auto [st, block] = get(mask);
    if (st != 0) {
        offset_read_pending_ = true;
        return {std::nullopt, "clock-domain read failed (status " + std::to_string(st) + ")"};
    }
    if (!valid_block(block, *lay, mask)) {
        offset_read_pending_ = true;
        return {std::nullopt, "clock-domain block did not echo its version and requested mask"};
    }
    std::map<std::uint32_t, ClkDomRequest> out;
    for (std::uint32_t d : doms) {
        std::uint32_t base = lay->header + d * lay->stride;
        ClkDomRequest r;
        r.mode = block.word(base + lay->mode);
        // The logical UI sign; the diagnostic probe writes raw values to discover the polarity.
        r.freq_khz = block.word(base + lay->freq_khz) * control_polarity(d);
        if (lay->msvdd_uv) r.msvdd_uv = block.word(base + *lay->msvdd_uv);
        out[d] = r;
    }
    return {std::move(out), ""};
}

std::optional<double> ClkDom::read_xbar_offset_mhz() {
    // Requested, not applied: the driver floors a request to whole clock bins.
    auto [rows, err] = read_requests();
    if (!err.empty() || !rows || !rows->count(kClkDomXbar)) return std::nullopt;
    return (*rows)[kClkDomXbar].freq_khz / 1000.0;
}

Outcome ClkDom::set_clk_domain_offset(std::int64_t domain, double mhz) {
    // Read-modify-write exactly one dword of the block the getter just
    // produced. The diff guard re-reads the block and refuses if anything but
    // the intended dword differs, so a layout change in a future driver turns
    // into a refusal rather than a write into whatever now occupies the offset.
    if (domain < 0 || domain >= kClkDomSlots) return {false, "clock domain must be an integer control index"};
    if (!std::isfinite(mhz) || !fits_i32(mhz * 1000.0))
        return {false, "clock-domain offset exceeds the signed 32-bit kHz range"};
    Nvapi& a = gpu_.nvapi;
    if (!(ok() && a.interface(ClkDomCtlSet))) return {false, "per-domain clock control unavailable"};
    const ClkDomLayout* lay = layout();
    if (!lay) return {false, "clock-domain layout is not validated for this generation/runtime ABI"};
    const std::vector<std::uint32_t>& doms = domains();
    std::uint32_t dom = static_cast<std::uint32_t>(domain);
    if (std::find(doms.begin(), doms.end(), dom) == doms.end())
        return {false, "domain " + std::to_string(domain) + " is not one this driver accepts (" + int_list(doms) + ")"};
    std::int64_t khz = static_cast<std::int64_t>(std::nearbyint(mhz)) * 1000;  // Python: int(round(mhz)) * 1000
    if (!fits_i32(static_cast<double>(khz)))
        return {false, "rounded clock-domain offset exceeds the signed 32-bit kHz range"};
    std::int32_t polarity = control_polarity(domain);
    std::int32_t wire_khz = static_cast<std::int32_t>(khz) * polarity;
    std::uint32_t mask = 1u << dom;
    auto [st, buf] = get(mask);
    if (st != 0) return {false, "clock-domain read failed (status " + std::to_string(st) + ")"};
    if (!valid_block(buf, *lay, mask))
        return {false, "clock-domain block did not echo its version and requested mask"};
    std::uint32_t dw_byte = lay->header + dom * lay->stride + lay->freq_khz;
    std::uint32_t dw = dw_byte / 4;
    std::int32_t was_wire = buf.word(dw_byte);
    std::int32_t was = was_wire * polarity;

    auto [st2, ref] = get(mask);
    if (st2 != 0) return {false, "verification read failed (status " + std::to_string(st2) + ")"};
    if (!valid_block(ref, *lay, mask))
        return {false, "verification block did not echo its version and requested mask"};
    if (!same_block(ref, buf, *lay)) return {false, "clock-domain block changed between reads; no write issued"};
    if (was_wire == wire_khz)
        return {true, control_label(domain) + " offset already " + plus_0f(static_cast<double>(khz) / 1000.0) +
                          " MHz; no write needed"};
    buf.set_word(dw_byte, wire_khz);
    std::vector<std::uint32_t> diffs = differing_words(buf, ref, *lay);
    if (diffs != std::vector<std::uint32_t>{dw}) {
        std::vector<std::uint32_t> head(diffs.begin(), diffs.begin() + static_cast<std::ptrdiff_t>(std::min<std::size_t>(diffs.size(), 8)));
        return {false, "refusing to write: " + std::to_string(diffs.size()) + " dwords differ " + int_list(head) +
                           ", expected only " + std::to_string(dw) + ". The block layout is not what this build measured."};
    }
    int sst = a.get<HandlePtrFn>(ClkDomCtlSet)(a.gpu, buf.words.data());
    if (sst != 0) return {false, "clock-domain write failed (status " + std::to_string(sst) + ")"};
    return {true, control_label(domain) + " offset " + plus_0f(was / 1000.0) + " -> " +
                      plus_0f(static_cast<double>(khz) / 1000.0) +
                      " MHz (requested; the driver floors to whole clock bins)"};
}

RailOffsetCapability ClkDom::rail_offset_capability(std::int64_t rail, std::optional<std::int64_t> domain) {
    // A readable request field, not evidence of a physical rail response. An
    // explicit control index stays bound to that record; the default prefers
    // core control 0, then an understood accepted control.
    RailOffsetCapability out;
    out.domain = domain;
    out.experimental = rail == 1;
    auto unavailable = [&](const char* reason) { out.reason = reason; return out; };
    if (rail != 0 && rail != 1) return unavailable("unknown rail offset index");
    if (domain && (*domain < 0 || *domain >= kClkDomSlots)) return unavailable("invalid clock-control index");
    const ClkDomLayout* lay = layout();
    if (!lay) return unavailable("rail-offset field layout is unavailable");
    std::optional<std::uint32_t> field = rail == 0 ? lay->nvvdd_uv : lay->msvdd_uv;
    if (!field) return unavailable("this layout does not define the rail-offset field");
    const std::vector<std::uint32_t>& accepted_list = domains();
    std::set<std::uint32_t> accepted(accepted_list.begin(), accepted_list.end());
    std::vector<std::uint32_t> ui = controls_for_ui();
    std::set<std::uint32_t> understood_set(ui.begin(), ui.end());
    understood_set.insert(0);
    std::vector<std::uint32_t> candidates;
    if (domain) {
        candidates.push_back(static_cast<std::uint32_t>(*domain));
    } else {
        for (std::uint32_t d : accepted)
            if (understood_set.count(d)) candidates.push_back(d);  // sorted: std::set order
    }
    for (std::uint32_t selected : candidates) {
        if (!accepted.count(selected) || !understood_set.count(selected)) continue;
        auto [status, block] = get(1u << selected);
        if (status != 0 || !valid_block(block, *lay, 1u << selected)) continue;
        double value = block.word(lay->header + selected * lay->stride + *field) / 1000.0;
        if (rail == 1) msvdd_seen_.insert(selected);
        out.available = true;
        out.domain = selected;
        out.value_mv = value;
        out.reason = "request field readable; physical voltage response is unverified";
        return out;
    }
    return unavailable("no understood rail-offset control record answered");
}

std::optional<double> ClkDom::read_rail_offset_mv(std::int64_t domain, std::int64_t rail) {
    // The selected rail's stored request in mV, not measured VOUT.
    if (rail != 0) {
        RailOffsetCapability capability = rail_offset_capability(rail, domain);
        return capability.available ? capability.value_mv : std::nullopt;
    }
    const ClkDomLayout* lay = layout();
    if (!lay || !lay->nvvdd_uv) return std::nullopt;
    if (domain < 0 || domain >= kClkDomSlots) return std::nullopt;  // Python would shift a mask that cannot fit
    auto [st, block] = get(1u << static_cast<std::uint32_t>(domain));
    if (st != 0) return std::nullopt;
    return block.word(lay->header + static_cast<std::uint32_t>(domain) * lay->stride + *lay->nvvdd_uv) / 1000.0;
}

Outcome ClkDom::set_rail_offset_mv(double mv, std::int64_t domain, std::int64_t rail) {
    // Offset the core rail by `mv` millivolts. MEASURED 1:1 on TU102 with the
    // core clock pinned by the NVML frequency lock (never measure it with the
    // V/F POINT lock: that lock changes which point is held as the rail moves).
    // A SECOND mechanism beside set_voltage_boost; nothing here touches the other.
    if (rail == 1) return set_msvdd_offset_mv(mv, domain, false);
    if (rail != 0) return {false, "unknown rail offset index"};
    if (domain < 0 || domain >= kClkDomSlots) return {false, "clock-control index must be an integer from 0 to 31"};
    if (!std::isfinite(mv) || !fits_i32(mv * 1000.0))
        return {false, "NVVDD offset exceeds the signed 32-bit microvolt range"};
    double rounded = std::nearbyint(mv * 1000.0);  // Python: int(round(mv * 1000))
    if (!fits_i32(rounded)) return {false, "rounded NVVDD request exceeds signed microvolts"};
    std::int32_t uv = static_cast<std::int32_t>(rounded);
    Nvapi& a = gpu_.nvapi;
    if (!(ok() && a.interface(ClkDomCtlSet))) return {false, "per-domain clock control unavailable"};
    const ClkDomLayout* lay = layout();
    if (!lay || !lay->nvvdd_uv) return {false, "NVVDD layout is not validated for this generation/runtime ABI"};
    std::uint32_t field = *lay->nvvdd_uv;
    std::uint32_t dom = static_cast<std::uint32_t>(domain);
    std::uint32_t mask = 1u << dom;
    auto [st, buf] = get(mask);
    if (st != 0 || !valid_block(buf, *lay, mask))
        return {false, "NVVDD request read failed or returned an invalid block"};
    std::uint32_t dw_byte = lay->header + dom * lay->stride + field;
    std::uint32_t dw = dw_byte / 4;
    std::int32_t was = buf.word(dw_byte);
    double lower = voltage_xoc_enabled ? -500.0 : -100.0;
    double upper = voltage_xoc_enabled ? kRailOffsetXocMaxMv : kRailOffsetMaxMv;
    lower = std::min(lower, was / 1000.0);
    upper = std::max(upper, was / 1000.0);
    if (!(lower <= mv && mv <= upper))
        return {false, "NVVDD offset " + g_format(mv) + " mV is outside Druta's " + g_format(lower) + ".." +
                           g_format(upper) + " mV bound"};
    if (was == uv) {
        // The diff guard demands exactly one changed dword, so a no-op write
        // would be REFUSED; say the request is already satisfied instead.
        return {true, std::string(rail_name(rail)) + " offset already " + plus_2f(uv / 1000.0) + " mV"};
    }
    buf.set_word(dw_byte, uv);
    auto [st2, ref] = get(mask);
    if (st2 != 0 || !valid_block(ref, *lay, mask))
        return {false, "NVVDD verification read failed or returned an invalid block"};
    std::vector<std::uint32_t> diffs = differing_words(buf, ref, *lay);
    if (diffs != std::vector<std::uint32_t>{dw})
        return {false, "refusing to write: " + std::to_string(diffs.size()) + " dwords differ, expected only " +
                           std::to_string(dw)};
    int sst = a.get<HandlePtrFn>(ClkDomCtlSet)(a.gpu, buf.words.data());
    if (sst != 0) return {false, std::string(rail_name(rail)) + " write failed (status " + std::to_string(sst) + ")"};
    auto [st3, back] = get(mask);
    if (st3 != 0 || !valid_block(back, *lay, mask))
        return {false, "NVVDD request sent, but its readback is unavailable or invalid"};
    std::int32_t observed = back.word(dw_byte);
    if (observed != uv)
        return {false, "NVVDD request readback mismatch: requested " + plus_g(uv / 1000.0) + ", stored " +
                           plus_g(observed / 1000.0) + " mV"};
    return {true, std::string(rail_name(rail)) + " stored request " + plus_2f(was / 1000.0) + " -> " +
                      plus_2f(uv / 1000.0) + " mV; physical voltage response is unverified"};
}

Outcome ClkDom::reset_msvdd_offset_mv(std::int64_t domain) {
    // Explicitly clear a stored MSVDD request, including after XOC is off.
    return set_msvdd_offset_mv(0.0, domain, true);
}

Outcome ClkDom::set_msvdd_offset_mv(double mv, std::int64_t domain, bool reset) {
    // Off by default: this offset has never been shown to move anything while
    // NVVDD as a positive control did at the same magnitude. The register
    // readback is checked; the physical response stays unverified.
    if (domain < 0 || domain >= kClkDomSlots) return {false, "clock-control index must be an integer from 0 to 31"};
    if (!std::isfinite(mv) || !fits_i32(mv * 1000.0))
        return {false, "MSVDD offset exceeds the signed 32-bit microvolt range"};
    Nvapi& a = gpu_.nvapi;
    if (!ok() || !a.interface(ClkDomCtlSet)) return {false, "per-domain clock control unavailable"};
    RailOffsetCapability capability = rail_offset_capability(1, domain);
    if (!capability.available) return {false, capability.reason};
    double rounded = std::nearbyint(mv * 1000.0);
    if (!fits_i32(rounded)) return {false, "rounded MSVDD request exceeds signed microvolts"};
    std::int32_t requested_uv = static_cast<std::int32_t>(rounded);
    double value_mv = *capability.value_mv;
    std::int32_t stored_uv = static_cast<std::int32_t>(std::nearbyint(value_mv * 1000.0));
    if (requested_uv == stored_uv)
        return {true, "MSVDD request already " + plus_g(requested_uv / 1000.0) + " mV; no write needed"};
    if (!(reset && requested_uv == 0) && !msvdd_write_enabled)
        return {false, "MSVDD offset requires the experimental XOC opt-in"};
    double lower = std::min(-500.0, value_mv);
    double upper = std::max(kRailOffsetXocMaxMv, value_mv);
    if (!(lower <= mv && mv <= upper))
        return {false, "MSVDD offset is outside " + g_format(lower) + ".." + g_format(upper) + " mV"};
    const ClkDomLayout* lay = layout();
    std::uint32_t dom = static_cast<std::uint32_t>(domain);
    std::uint32_t mask = 1u << dom;
    auto [status, buf] = get(mask);
    if (status != 0 || !valid_block(buf, *lay, mask))
        return {false, "MSVDD request read failed or returned an invalid block"};
    std::uint32_t word_byte = lay->header + dom * lay->stride + *lay->msvdd_uv;
    if (buf.word(word_byte) != stored_uv) return {false, "MSVDD request changed during discovery; no write issued"};
    auto [status2, ref] = get(mask);
    if (status2 != 0 || !valid_block(ref, *lay, mask) || !same_block(ref, buf, *lay))
        return {false, "MSVDD control block changed between reads; no write issued"};
    buf.set_word(word_byte, requested_uv);
    int sst = a.get<HandlePtrFn>(ClkDomCtlSet)(a.gpu, buf.words.data());
    if (sst != 0) return {false, "MSVDD request write failed (status " + std::to_string(sst) + ")"};
    msvdd_written_.insert(dom);
    auto [status3, back] = get(mask);
    if (status3 != 0 || !valid_block(back, *lay, mask))
        return {false, "MSVDD request sent, but its readback is unavailable"};
    std::int32_t observed = back.word(word_byte);
    if (observed != requested_uv)
        return {false, "MSVDD request readback mismatch: requested " + plus_g(requested_uv / 1000.0) + ", stored " +
                           plus_g(observed / 1000.0) + " mV"};
    return {true, "MSVDD request stored at control " + std::to_string(domain) + ": " + plus_g(requested_uv / 1000.0) +
                      " mV; register readback matched, physical voltage response is unverified"};
}

std::set<std::uint32_t> ClkDom::cached_ui_controls(std::optional<std::uint32_t> architecture) {
    // Validated understood controls without triggering discovery.
    if (layout_cache_ != LayoutCache::valid) return {};
    const auto& known = understood(architecture);
    std::set<std::uint32_t> out;
    if (valid_)
        for (std::uint32_t d : *valid_)
            if (known.count(d)) out.insert(d);
    return out;
}

bool ClkDom::retry_needed(std::optional<std::uint32_t> architecture) {
    if (!architecture) return true;
    if (!candidate(architecture)) return false;
    if (!ok() || layout_cache_ != LayoutCache::valid) return true;
    if (offset_read_pending_) return true;
    const auto& known = understood(architecture);
    for (const auto& [control, name] : known)
        if (!valid_ || std::find(valid_->begin(), valid_->end(), control) == valid_->end()) return true;
    return false;
}

bool ClkDom::retry_clock_capabilities(double now) {
    // At most two delayed, read-only retries of failed clock discovery: the
    // first call arms a one-second delay, a failed or partial attempt arms one
    // final attempt three seconds later. Manual refresh resets the budget.
    if (retry_attempts_ >= kRetryCount) return false;
    // Do not hammer even the public architecture getter while the GPU wakes.
    if (!retry_due_) {
        retry_due_ = now + kRetryDelays[retry_attempts_];
        return false;
    }
    if (now < *retry_due_) return false;

    auto architecture = telemetry_.arch();
    const ClkDomLayout* lay = candidate(architecture);
    if (architecture && !lay) {
        // An identified, unmeasured generation is never tested with a
        // neighbouring architecture's private field layout.
        retry_attempts_ = kRetryCount;
        retry_due_.reset();
        return false;
    }
    if (!retry_needed(architecture)) {
        retry_attempts_ = kRetryCount;
        retry_due_.reset();
        return false;
    }
    std::set<std::uint32_t> before = cached_ui_controls(architecture);
    ++retry_attempts_;
    retry_due_.reset();

    if (architecture && lay && ok() && lay->header + kClkDomSlots * lay->stride == lay->size) {
        const auto& known = understood(architecture);
        std::set<std::uint32_t> accepted;
        if (valid_) accepted.insert(valid_->begin(), valid_->end());
        std::map<std::uint32_t, std::string> errors = probe_errors_;
        std::optional<std::uint32_t> valid_response;  // the mask that answered exactly
        for (const auto& [control, name] : known) {  // sorted(set(understood) - accepted)
            if (accepted.count(control)) continue;
            std::uint32_t mask = 1u << control;
            auto [status, response] = get(mask);
            if (status == 0 && valid_block(response, *lay, mask)) {
                accepted.insert(control);
                errors.erase(control);
                valid_response = mask;
            } else {
                errors[control] = status ? "status " + std::to_string(status) : "version/mask echo mismatch";
            }
        }
        valid_ = std::vector<std::uint32_t>(accepted.begin(), accepted.end());
        probe_errors_ = errors;

        // A previous validation failure caches False even when discovery found
        // accepted domains; a newly accepted exact response already validated.
        if (layout_cache_ != LayoutCache::valid) {
            if (!valid_response && !accepted.empty()) {
                std::uint32_t mask = 1u << *accepted.begin();
                auto [status, response] = get(mask);
                if (status == 0 && valid_block(response, *lay, mask)) {
                    valid_response = mask;
                } else if (status == 0) {
                    capability_error_ = CapabilityError{
                        "unsupported_response", "clock-control response did not echo the expected version and mask"};
                } else {
                    capability_error_ = CapabilityError{
                        "read_failure", "clock-control validation read failed with status " + std::to_string(status)};
                }
            }
            if (valid_response) {
                layout_cache_ = LayoutCache::valid;
                layout_cached_ = lay;
                capability_error_.reset();
            } else if (accepted.empty()) {
                layout_cache_ = LayoutCache::failed;
                layout_cached_ = nullptr;
                capability_error_ = CapabilityError{
                    "unavailable", "no clock-control domain answered; Refresh capabilities retries discovery"};
            }
        }
    }

    bool requests_recovered = false;
    if (offset_read_pending_ && layout_cache_ == LayoutCache::valid) {
        auto [requests, error] = read_requests();
        if (requests && !requests->empty() && error.empty()) {
            offset_read_pending_ = false;
            requests_recovered = true;
        }
    }
    std::set<std::uint32_t> after = cached_ui_controls(architecture);
    // Keep the final round available: newly published controls still need a
    // fresh request read, and its failure belongs to this budget.
    if (retry_attempts_ < kRetryCount) retry_due_ = now + kRetryDelays[retry_attempts_];
    return before != after || requests_recovered;
}

std::optional<CapabilityError> ClkDom::refresh_capabilities() {
    // One deliberate, read-only retry: releases positive and negative
    // discovery caches; settings, saved defaults and ownership holds stay.
    std::optional<CapabilityError> previous = capability_error_;
    layout_cache_ = LayoutCache::none;
    layout_cached_ = nullptr;
    valid_.reset();
    pair_.reset();
    probe_errors_.clear();
    capability_error_.reset();
    retry_attempts_ = 0;
    retry_due_.reset();
    offset_read_pending_ = false;
    return previous;
}

}  // namespace druta::backend
