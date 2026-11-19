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
    auto a = api();
    a.identity = clock_identity;
    a.pascal_clock_capture = [&](dc_gpu*, bool registry) {
        ++reads;
        if (registry) ++registry_reads;
        return c;
    };
    a.power_weights.create_receipt = [&](const PowerWeightReceipt& r) { receipt = r; };
    a.power_weights.discover_receipt = [&](const auto&, const auto&) { return receipt; };
    auto client = create_core_backend_for_test(a);
    std::string error;
    CHECK(client->open(c.adapter.slot, error));
    CHECK(!client->read(false).pascal_clocks && reads == 0 && writes == 0);
    Command read, prepare;
    read.operation = Command::Operation::pascal_clock_read;
    prepare.operation = Command::Operation::pascal_clock_prepare;
    prepare.enabled = true;
    prepare.pascal_clock_edits = std::make_shared<const std::vector<PascalClockEdit>>(
        std::vector<PascalClockEdit>{{PascalClockDomain::xbar, 0, 3600, 3000}});
    CHECK(!client->execute(prepare).ok && reads == 0);
    CHECK(client->execute(read).ok && reads == 1 && registry_reads == 0);
    const auto snapshot = client->read(false);
    CHECK(snapshot.pascal_clock_fresh && snapshot.pascal_clocks && snapshot.pascal_clocks->fields.size() == 11);
    // Even a byte outside the selected tuple invalidates the staged image.
    c.image.back() ^= 1;
    CHECK(!client->execute(prepare).ok && !receipt);
    CHECK(!client->read(false).pascal_clock_fresh && client->read(false).pascal_clocks == snapshot.pascal_clocks);
    CHECK(client->execute(read).ok);
    const auto prepared = client->execute(prepare);
    if (!prepared.ok) std::fprintf(stderr, "Prepare: %s\n", prepared.message.c_str());
    CHECK(prepared.ok && registry_reads == 2 && receipt && receipt->is_pascal_clocks());
    const auto after = client->read(false);
    CHECK(after.power_weights && after.power_weights->receipt && after.power_weights->receipt->id == receipt->id);
    CHECK(after.pascal_clocks && snapshot.pascal_clock_fresh && writes == 0);
    c.image_availability = VoltagePolicyAvailability::transient;
    CHECK(!client->execute(read).ok && !client->read(false).pascal_clock_fresh);
    c.image_availability = VoltagePolicyAvailability::available;
    CHECK(client->execute(read).ok);
    c.identity.device_id = 0x1234;
    CHECK(!client->execute(read).ok && !client->read(false).pascal_clock_fresh);
    c.identity.device_id = 0x1bbc; c.identity.architecture = 7;
    CHECK(!client->execute(read).ok && writes == 0);
    client->close();
    CHECK(writes == 0);
    std::printf("Pascal clock client: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
