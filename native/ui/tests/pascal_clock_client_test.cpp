// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#define main unused_core_fixture_main
#include "core_client_test.cpp"
#undef main
#define main unused_weight_fixture_main
#include "power_weights_service_test.cpp"
#undef main
#include "pascal_clock_fixture.h"

namespace {
dc_gpu_identity opened_clock_id;
dc_status DC_CALL clock_identity(const dc_gpu*, dc_gpu_identity* out) {
    *out = opened_clock_id;
    return DC_OK;
}
dc_status DC_CALL pascal_architecture(dc_gpu*, uint32_t* out) {
    *out = 4;
    return DC_OK;
}
bool nvvdd_limits_missing = false;
dc_status DC_CALL pascal_rail_limits(dc_gpu* gpu, dc_rails_limit_set* out) {
    if (nvvdd_limits_missing) return DC_E_UNAVAILABLE;
    const auto status = rail_limits(gpu, out);
    out->rails[0].has_effective = 1;
    out->rails[0].effective_mv = 1056.25;
    return status;
}

void test_p0_rail_snapshot() {
    auto a = api();
    a.identity = clock_identity;
    a.i2c.architecture = pascal_architecture;
    a.rails.lock = lock_gpu;
    a.rails.unlock = unlock_gpu;
    a.rails.identity = clock_identity;
    a.rails.fields = rail_fields;
    a.rails.limits = pascal_rail_limits;
    a.rails.state = rail_state;
    auto client = create_core_backend_for_test(a);
    std::string error;
    CHECK(client->open(opened_clock_id.slot, error));
    const int initial_state_calls = rail_state_calls;
    CHECK(client->read(true).pascal_p0_nvvdd_effective_mv == 1056.25);
    CHECK(client->read(false).pascal_p0_nvvdd_effective_mv == 1056.25 && rail_state_calls == initial_state_calls);

    rail_fields_fail = true;
    rail_effective_mv = 1031.25;
    CHECK(client->read(true).pascal_p0_nvvdd_effective_mv == 1031.25 && rail_state_calls == initial_state_calls + 1);

    // The same Classic state read supplies the fallback even without the list of editable requests.
    rail_fields_fail = false;
    nvvdd_limits_missing = true;
    client->set_live_readouts(true);
    rail_effective_mv = 1018.75;
    CHECK(client->read(true).pascal_p0_nvvdd_effective_mv == 1018.75 && rail_state_calls == initial_state_calls + 2);
    rail_state_fail = true;
    CHECK(!client->read(true).pascal_p0_nvvdd_effective_mv && rail_state_calls == initial_state_calls + 4);
    rail_state_fail = false;
    rail_effective_mv = 0;
    CHECK(!client->read(true).pascal_p0_nvvdd_effective_mv);
    CHECK(writes == 0 && advanced_writes == 0 && rail_set_calls == 0);
    client->close();
}
}
int main() {
    WeightFake fake(true);
    auto& c = fake.current;
    c.image = druta::ui::test::pascal_clock_fixture();
    c.info.clear();
    c.info_availability = VoltagePolicyAvailability::unsupported;
    // Live capture returns only the managed names from this device key.
    c.registry = PowerWeightRegistrySnapshot{{L"EnableGpuFirmware", 4, {1, 0, 0, 0}}};
    c.target.architecture = c.identity.architecture = 4;
    c.target.device_id = c.identity.device_id = c.adapter.nvml_devid = c.adapter.nvapi_devid = 0x1bbc;
    c.windows->instance_id = "PCI\\VEN_10DE&DEV_1BBC&SUBSYS_76543210\\ALT";
    opened_clock_id = c.adapter;
    unsigned reads = 0, registry_reads = 0;
    std::optional<PowerWeightReceipt> receipt;
    unsigned replacements = 0;
    bool store_available = true;
    auto a = api();
    a.identity = clock_identity;
    a.pascal_clock_capture = [&](dc_gpu*, bool registry) {
        ++reads;
        if (registry) ++registry_reads;
        return c;
    };
    a.power_weights.create_receipt = [&](const PowerWeightReceipt& r) { receipt = r; };
    a.power_weights.discover_receipt = [&](const auto&, const auto&) {
        if (!store_available) throw std::runtime_error("Saved session unavailable");
        return receipt;
    };
    a.replace_pascal_clock_receipt = [&](const auto& previous, const auto& updated) {
        CHECK(receipt && receipt->id == previous.id && previous.id != updated.id);
        receipt = updated;
        ++replacements;
    };
    auto client = create_core_backend_for_test(a);
    std::string error;
    CHECK(client->open(c.adapter.slot, error));
    CHECK(!client->read(false).pascal_clocks && reads == 0 && writes == 0);
    Command read, prepare;
    read.operation = Command::Operation::pascal_clock_read;
    prepare.operation = Command::Operation::pascal_clock_prepare;
    prepare.enabled = true;
    prepare.pascal_clock_edits = std::make_shared<const std::vector<PascalClockEdit>>(
        std::vector<PascalClockEdit>{{PascalClockDomain::xbar, 0, 3600, 7400},
                                     {PascalClockDomain::xbar, -1, 95, 200},
                                     {PascalClockDomain::sys, -1, 90, 200}});
    CHECK(!client->execute(prepare).ok && reads == 0);
    CHECK(client->execute(read).ok && reads == 1 && registry_reads == 0);
    const auto snapshot = client->read(false);
    CHECK(snapshot.pascal_clock_fresh && snapshot.pascal_clocks && snapshot.pascal_clocks->fields.size() == 11);
    CHECK(snapshot.pascal_clock_availability == Availability::available);
    // Even a byte outside the selected tuple invalidates the staged image.
    c.image.back() ^= 1;
    CHECK(!client->execute(prepare).ok && !receipt);
    CHECK(!client->read(false).pascal_clock_fresh && client->read(false).pascal_clocks == snapshot.pascal_clocks);
    CHECK(client->execute(read).ok);
    for (const auto& edit : {PascalClockEdit{PascalClockDomain::xbar, -1, 95, 201},
                            PascalClockEdit{PascalClockDomain::sys, -1, 90, 201}}) {
        auto invalid = prepare;
        invalid.pascal_clock_edits = std::make_shared<const std::vector<PascalClockEdit>>(1, edit);
        CHECK(!client->execute(invalid).ok && !receipt && writes == 0);
        CHECK(client->execute(read).ok);
    }
    const auto prepared = client->execute(prepare);
    if (!prepared.ok) std::fprintf(stderr, "Prepare: %s\n", prepared.message.c_str());
    CHECK(prepared.ok && registry_reads == 4 && receipt && receipt->is_pascal_clocks());
    const auto after = client->read(false);
    CHECK(after.power_weights && after.power_weights->receipt && after.power_weights->receipt->id == receipt->id);
    CHECK(after.pascal_clocks && snapshot.pascal_clock_fresh && writes == 0);
    if (receipt) {
        const auto initial_image = c.image;
        c.image = derive_weight_receipt(*receipt).clock_plan.image;
        CHECK(client->execute(read).ok && client->read(false).pascal_clock_active_receipt.empty());
        receipt->phase = "active";
        CHECK(client->execute(read).ok);
        const auto installed = client->read(false);
        CHECK(installed.pascal_clock_active_receipt == receipt->id && installed.pascal_clock_fresh && installed.pascal_clocks &&
              std::count_if(installed.pascal_clocks->fields.begin(), installed.pascal_clocks->fields.end(),
                            [](const auto& field) { return field.pstate == -1 && field.current == 200; }) == 2);
        CHECK(std::any_of(installed.pascal_clocks->fields.begin(), installed.pascal_clocks->fields.end(),
                          [](const auto& field) { return field.domain == PascalClockDomain::xbar &&
                              field.pstate == 0 && field.current == 7400 && field.maximum == 7400; }));
        prepare.pascal_clock_edits = std::make_shared<const std::vector<PascalClockEdit>>(
            std::vector<PascalClockEdit>{{PascalClockDomain::xbar, -1, 200, 101}});
        const auto previous = *receipt;
        receipt.reset(); // An existing GUI can have closed the session since Read.
        CHECK(!client->execute(prepare).ok && !receipt && replacements == 0);
        receipt = previous;
        CHECK(client->execute(read).ok);
        CHECK(client->execute(prepare).ok && replacements == 1 && receipt && receipt->image == initial_image);
        CHECK(client->read(false).pascal_clock_active_receipt.empty());
        const auto revised = derive_weight_receipt(*receipt);
        CHECK(revised.clock_source_image == c.image);
        c.image = revised.clock_plan.image;
        receipt->phase = "active";
        CHECK(client->execute(read).ok && client->read(false).pascal_clock_active_receipt == receipt->id);
        c.image.back() ^= 1;
        CHECK(client->execute(read).ok && client->read(false).pascal_clock_active_receipt.empty());
        c.image = initial_image;
        CHECK(client->execute(read).ok && client->read(false).pascal_clock_original_receipt == receipt->id);
        CHECK(!client->execute(prepare).ok && replacements == 1);
        store_available = false;
        CHECK(!client->execute(read).ok && !client->read(false).pascal_clock_fresh);
        store_available = true;
    }
    {
        using Domain=PascalClockDomain;
        receipt.reset();
        c.image=druta::ui::test::pascal_timing_fixture();
        c.timing_sample=PascalTimingSample{3504,0x2966c296,0x38aa};
        CHECK(client->execute(read).ok && !client->read(false).pascal_clocks->timing_sample);
        Command inspect;
        inspect.operation=Command::Operation::pascal_timing_read;
        CHECK(client->execute(inspect).ok && client->read(false).pascal_clocks->timing_sample==c.timing_sample);
        prepare.pascal_clock_edits=std::make_shared<const std::vector<PascalClockEdit>>(
            std::vector<PascalClockEdit>{{Domain::tfaw,0,28,29,c.timing_sample}});
        c.timing_sample->memory_mhz++;
        const auto moved_clock=client->execute(prepare);
        CHECK(!moved_clock.ok&&!receipt&&moved_clock.message.find("memory clock changed")!=std::string::npos);
        c.timing_sample->memory_mhz--;
        CHECK(client->execute(inspect).ok&&client->execute(prepare).ok&&receipt&&writes==0);
        CHECK(receipt_timing_sample(*receipt,PascalTimingPhase::applied)==with_tfaw(*c.timing_sample,29));
        c.image=derive_weight_receipt(*receipt).clock_plan.image;
        receipt->phase="active";
        CHECK(client->execute(read).ok);
        const auto previous_id=receipt->id;
        const auto previous_count=replacements;
        prepare.pascal_clock_edits=std::make_shared<const std::vector<PascalClockEdit>>(
            std::vector<PascalClockEdit>{{Domain::xbar,-1,95,96}});
        const auto programmed=with_tfaw(*c.timing_sample,29);
        c.timing_sample.reset(); // Card is at P8 when the user requests only a clock change.
        CHECK(!client->execute(prepare).ok && replacements==previous_count && receipt->id==previous_id && receipt->phase=="active");
        c.timing_sample=programmed;
        CHECK(client->execute(read).ok&&client->execute(prepare).ok&&replacements==previous_count+1);
        CHECK(receipt_timing_sample(*receipt,PascalTimingPhase::applied)==programmed);
        receipt.reset();
        c.image=druta::ui::test::pascal_timing_fixture();
        const auto full=druta::ui::test::pascal_full_timing_sample();
        c.timing_sample=full;
        CHECK(client->execute(inspect).ok);
        const int rc=pascal_timing_value(full,Domain::trc);
        prepare.pascal_clock_edits=std::make_shared<const std::vector<PascalClockEdit>>(
            std::vector<PascalClockEdit>{{Domain::trc,0,rc,rc+1,full}});
        c.timing_sample->config0^=1;
        c.timing_sample_message="Two matching controller reads at P0 and the same memory clock";
        const auto drifted=client->execute(prepare);
        CHECK(!drifted.ok&&!receipt&&writes==0&&drifted.message.find("timing word changed")!=std::string::npos);
        c.timing_sample=full;
        CHECK(client->execute(inspect).ok&&client->execute(prepare).ok&&receipt&&writes==0);
        CHECK(receipt_timing_sample(*receipt,PascalTimingPhase::applied)==with_pascal_timing(full,Domain::trc,rc+1));

        // The new sample still proves tFAW, but lacks the active tRC word. A
        // narrower inspection must never weaken the existing receipt on revision.
        const PascalTimingSample anchors{full.memory_mhz,full.config1,full.config3};
        c.image=derive_weight_receipt(*receipt).clock_plan.image;
        receipt->phase="active";
        const auto full_receipt_id=receipt->id;
        c.timing_sample=anchors;
        CHECK(client->execute(inspect).ok);
        prepare.pascal_clock_edits=std::make_shared<const std::vector<PascalClockEdit>>(
            std::vector<PascalClockEdit>{{Domain::tfaw,0,28,29,anchors}});
        const auto retained=client->execute(prepare);
        CHECK(!retained.ok&&retained.message.find("previously reviewed CONFIG words")!=std::string::npos&&
              receipt->id==full_receipt_id&&receipt->phase=="active"&&writes==0);

        // Without a committed override the user can reread and choose tFAW
        // from the qualified partial capture. No hidden CONFIG0 is assumed.
        receipt.reset();c.image=druta::ui::test::pascal_timing_fixture();
        c.timing_sample=full;
        CHECK(client->execute(inspect).ok);
        prepare.pascal_clock_edits=std::make_shared<const std::vector<PascalClockEdit>>(
            std::vector<PascalClockEdit>{{Domain::tfaw,0,28,29,full}});
        c.timing_sample=anchors;
        const auto missing_word=client->execute(prepare);
        CHECK(!missing_word.ok&&!receipt&&missing_word.message.find("CONFIG word is unavailable or unstable")!=std::string::npos&&
              missing_word.message.find("Read P0 BIOS timings again")!=std::string::npos);
        CHECK(client->execute(inspect).ok);
        prepare.pascal_clock_edits=std::make_shared<const std::vector<PascalClockEdit>>(
            std::vector<PascalClockEdit>{{Domain::tfaw,0,28,29,anchors}});
        CHECK(client->execute(prepare).ok&&receipt&&receipt_timing_sample(*receipt,PascalTimingPhase::original)==anchors&&writes==0);
        receipt.reset();
    }
    c.image_availability = VoltagePolicyAvailability::transient;
    CHECK(!client->execute(read).ok && !client->read(false).pascal_clock_fresh);
    CHECK(client->read(false).pascal_clock_availability == Availability::transient && client->read(false).pascal_clocks);
    c.image_availability = VoltagePolicyAvailability::unsupported;
    CHECK(!client->execute(read).ok && client->read(false).pascal_clock_availability == Availability::unsupported);
    c.image_availability = VoltagePolicyAvailability::available;
    CHECK(client->execute(read).ok);
    c.identity.device_id = 0x1234;
    CHECK(!client->execute(read).ok && !client->read(false).pascal_clock_fresh);
    c.identity.device_id = 0x1bbc; c.identity.architecture = 7;
    CHECK(!client->execute(read).ok && writes == 0);
    CHECK(client->read(false).pascal_clock_availability == Availability::unsupported);
    client->close();
    CHECK(writes == 0);
    test_p0_rail_snapshot();
    std::printf("Pascal clock client: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
