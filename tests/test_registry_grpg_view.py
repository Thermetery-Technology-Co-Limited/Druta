# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The existing GR PG checkbox previews configured eligibility and its scope."""

import pytest
from dearpygui import dearpygui as dpg

from tests.test_registry_panel import bios_field, enabled
from tests.test_registry_panel import harness as harness


def setup_field(h, *, index=1, before=0x120, after=0x100, contract=True):
    key = f"lpwr.gr_pg.{index}.enabled"
    h.mocks.inspect.return_value["fields"] = [
        bios_field(
            key,
            label=f"GR_PG entry {index} enabled",
            group="Idle power management",
            units="",
            current=True,
            mask=1,
            minimum=0,
            maximum=1,
        )
    ]
    if contract:
        h.mocks.prepare.return_value["grpg_contracts"] = {
            "before": {"controllers": {"0": {"configured_pstate_mask": before}}},
            "after": {"0": {"configured_pstate_mask": after}},
        }
    h.panel.refresh()
    dpg.set_value("reg_field_" + key, False)
    return key


@pytest.mark.parametrize(
    "index,before,after,wanted",
    [(1, 0x120, 0x100, "P5/P8 -> P8"), (3, 0x201, 0x200, "P0/P9 -> P9"), (2, 0x20, 0, "P5 -> none")],
)
def test_existing_checkbox_previews_actual_pstate_ids_and_configuration_scope(harness, index, before, after, wanted):
    h = harness
    key = setup_field(h, index=index, before=before, after=after)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(h.app.gpu, {key: False}, named_changes={}, flag_changes={})
    text = dpg.get_value("reg_preview")
    assert "configured P-state eligibility " + wanted in text
    assert "Loaded routing, controller support and the other low-power controller masks must stay unchanged" in text
    assert "Shared capability flags are retained" in text
    assert "Physical residency and power savings remain unmeasured" in text
    assert enabled("reg_apply_button")


def test_unqualified_dependency_transition_keeps_apply_disabled(harness):
    h = harness
    setup_field(h)
    h.mocks.prepare.side_effect = RuntimeError("Removing the last shared capability contributor is unqualified")
    h.panel.prepare()
    assert h.panel.preview is None
    assert not enabled("reg_apply_button")
    h.mocks.launch.assert_not_called()
    assert "last shared capability contributor" in dpg.get_value("reg_status")


def test_field_explains_the_shared_capability_boundary(harness):
    key = setup_field(harness)
    detail = harness.panel.fields[key]["detail"]
    assert "separate transaction" in detail
    assert "last shared auxiliary contributor" in detail
    assert "last enabled entry" in detail


def test_auxiliary_transition_preview_describes_its_additional_verification(harness):
    h = harness
    setup_field(h, before=0x120, after=0)
    h.mocks.prepare.return_value["grpg_contracts"]["schema"] = 2
    h.panel.prepare()
    text = dpg.get_value("reg_preview")
    assert "P5/P8 -> none" in text
    assert "last contributor to one shared auxiliary role will be removed" in text
    assert "support, configured P-state mask and controller feature" in text
    assert "other auxiliary roles remain unchanged" in text
    assert "Shared capability flags are retained" not in text
    assert enabled("reg_apply_button")


def test_bit1_auxiliary_preview_distinguishes_unavailable_support_from_false(harness):
    h = harness
    setup_field(h, before=0x120, after=0x100)
    h.mocks.prepare.return_value["grpg_contracts"]["schema"] = 3
    h.panel.prepare()
    text = dpg.get_value("reg_preview")
    assert "P5/P8 -> P8" in text
    assert "support query must become unavailable while another supported role remains readable" in text
    assert "separate per-P-state mask for this role is unavailable" in text
    assert "Shared capability flags are retained" not in text
    assert enabled("reg_apply_button")


def test_combined_preview_discloses_both_independent_auxiliary_checks(harness):
    h = harness
    setup_field(h, before=0x120, after=0)
    h.mocks.prepare.return_value["grpg_contracts"]["schema"] = 4
    h.panel.prepare()
    text = dpg.get_value("reg_preview")
    assert "P5/P8 -> none" in text
    assert "last contributors to two shared auxiliary roles will be removed" in text
    assert "support, configured P-state mask and controller feature" in text
    assert "support query must become unavailable while another supported role remains readable" in text
    assert "Both checks must pass" in text
    assert "Shared capability flags are retained" not in text
    assert enabled("reg_apply_button")


@pytest.mark.parametrize(
    "cleared,expected",
    [
        ([[0]], (False, False)),
        ([[0], [0, 2]], (True, False)),
        ([[0], [0, 4]], (False, True)),
        ([[0], [0, 2], [0, 4], [0, 2, 4]], (True, True)),
    ],
)
def test_main_support_loss_preview_describes_candidate_profiles(harness, cleared, expected):
    h = harness
    setup_field(h, before=0x120)
    contract = h.mocks.prepare.return_value["grpg_contracts"]
    contract.update(schema=5, after_profiles=[{"cleared": row} for row in cleared])
    del contract["after"]
    h.panel.prepare()
    text = dpg.get_value("reg_preview")
    assert "main controller 0 support enabled -> unsupported" in text
    assert "eligibility P5/P8 -> unavailable" in text
    assert ("controller 2 (GR RG)" in text) is expected[0]
    assert ("controller 4 (MS CG)" in text) is expected[1]
    assert "may also become unsupported" in text if any(expected) else "may also become unsupported" not in text
    assert "controller support and the other low-power controller masks must stay unchanged" not in text
    assert enabled("reg_apply_button")


def test_unrelated_preview_does_not_claim_grpg_verification(harness):
    h = harness
    setup_field(h, contract=False)
    h.panel.prepare()
    assert "GR power gating: configured P-state eligibility" not in dpg.get_value("reg_preview")
