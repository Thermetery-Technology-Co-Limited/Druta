"""Preview explains MS feature loss while the covered mapping stays active."""
from dearpygui import dearpygui as dpg

from tests.test_registry_panel import bios_field, enabled
from tests.test_registry_panel import harness as harness


def test_mapping_witness_preview_shows_feature_loss_and_retained_support(harness):
    h = harness
    key = "lpwr.ms.0.enabled"
    h.mocks.inspect.return_value["fields"] = [bios_field(
        key, label="MS entry 0 enabled", group="Idle power management", units="",
        current=True, mask=1, minimum=0, maximum=1,
    )]
    h.mocks.prepare.return_value["ms_contracts"] = {
        "schema": 4, "before": {"core": {"controllers": {"4": {"configured_pstate_mask": 0x120}}}},
        "after": {"controllers": {"4": {"supported": True, "configured_pstate_mask": 0}}},
    }
    h.panel.refresh()
    dpg.set_value("reg_field_" + key, False)
    h.panel.prepare()
    text = dpg.get_value("reg_preview")
    assert "Memory clock gating: configured P-states P5/P8 -> none; support retained" in text
    assert "Available MS features are reduced" in text
    assert "remaining mapped capability stays supported" in text
    assert "mapping, related support and all captured peers must match" in text
    assert "shared MS support query remains readable and reports false" not in text
    assert enabled("reg_apply_button")
