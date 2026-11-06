import copy

import pytest

from druta.grpg_main_projection import MainProjectionError, profiles


def row(value):
    return {"available": True, "value": value}


def controller(supported):
    return {
        "available": True,
        "supported": supported,
        "states_available": supported,
        "configured_pstate_mask": 0x1234 if supported else None,
    }


def core():
    controllers = {str(i): controller(i in (0, 1, 2, 4, 9, 20)) for i in range(22)}
    features = {
        str(i): row(0) if controllers[str(i)]["supported"] else {"available": False, "value": None} for i in range(22)
    }
    features["9"] = row(8)
    features["20"] = row(0x200000)
    group = {"1:0": row(1)}
    group.update({f"800:{i}": row(int(i in (0, 1, 2))) for i in range(4)})
    group.update({f"806:{i}": row(0x10 << i) for i in range(4)})
    roles = {
        str(i): {"available": i in (0, 2, 3, 6), "supported": True if i in (0, 2, 3, 6) else None} for i in range(11)
    }
    bit4 = {
        "1:0": row(1),
        **{f"801:{i}": row(int(i in (0, 1, 2))) for i in range(3)},
        "805:0": row(0x22),
        "805:1": row(0x33),
    }
    return {
        "identity": {},
        "pstates": {},
        "controllers": controllers,
        "auxiliary": {"feature": row(0x10), "group": group},
        "bit1": {"roles": roles},
        "main": {"features": features, "bit4_group": bit4},
    }


def by_cleared(items):
    return {tuple(item["cleared"]): item for item in items}


def test_all_four_candidates_preserve_peers_and_project_roles():
    before = core()
    saved = copy.deepcopy(before)
    outcomes = by_cleared(profiles(before))
    assert list(outcomes) == [(0,), (0, 2), (0, 4), (0, 2, 4)]
    assert before == saved
    for cleared, result in outcomes.items():
        for index in range(22):
            after = result["controllers"][str(index)]
            if index in cleared:
                assert after == controller(False)
                assert result["main"]["features"][str(index)] == {"available": False, "value": None}
            else:
                assert after == before["controllers"][str(index)]
        assert result["main"]["features"]["9"] == row(8)  # Class-0 controller 20 provides it.
        assert result["bit1"]["roles"]["6"]["supported"] is True
        for role, removed in ((0, True), (3, 2 in cleared), (2, 4 in cleared)):
            assert result["bit1"]["roles"][str(role)]["available"] is (not removed)
        assert result["auxiliary"]["group"]["800:0"] == row(0)
        assert result["auxiliary"]["group"]["800:1"] == row(0 if 4 in cleared else 1)
        assert result["main"]["bit4_group"]["805:0"] == row(0)
        assert result["main"]["bit4_group"]["805:1"] == row(0x33)


def test_filter_clears_bit_only_without_a_surviving_provider():
    source = core()
    source["main"]["features"]["20"] = row(0)
    source["main"]["features"]["2"] = row(0x200000)
    outcomes = by_cleared(profiles(source))
    assert outcomes[(0,)]["main"]["features"]["9"] == row(8)
    assert outcomes[(0, 2)]["main"]["features"]["9"] == row(0)


def test_unknown_provider_refuses_only_unresolved_candidates():
    source = core()
    source["controllers"]["20"] = {
        "available": False,
        "supported": None,
        "states_available": False,
        "configured_pstate_mask": None,
    }
    source["main"]["features"]["20"] = {"available": False, "value": None}
    source["main"]["features"]["2"] = row(0x200000)
    outcomes = by_cleared(profiles(source))
    assert set(outcomes) == {(0,), (0, 4)}
    assert outcomes[(0,)]["controllers"]["20"] == source["controllers"]["20"]


def test_unknown_optional_and_unrelated_support_are_preserved():
    source = core()
    unknown = {"available": False, "supported": None, "states_available": False, "configured_pstate_mask": None}
    source["controllers"]["2"] = unknown
    source["controllers"]["13"] = unknown
    source["main"]["features"]["2"] = {"available": False, "value": None}
    outcomes = by_cleared(profiles(source))
    assert set(outcomes) == {(0,), (0, 4)}
    for result in outcomes.values():
        assert result["controllers"]["2"] == unknown
        assert result["controllers"]["13"] == unknown


def test_controller6_support_preserves_type1_role1_and_unknown_blocks_only_relevant_candidate():
    source = core()
    source["controllers"]["6"] = controller(True)
    assert by_cleared(profiles(source))[(0, 4)]["auxiliary"]["group"]["800:1"] == row(1)
    source["controllers"]["6"] = {
        "available": False,
        "supported": None,
        "states_available": False,
        "configured_pstate_mask": None,
    }
    assert set(by_cleared(profiles(source))) == {(0,), (0, 2)}


def test_empty_type7_group_is_all_unavailable():
    source = core()
    source["main"]["bit4_group"]["801:2"] = row(0)
    outcome = by_cleared(profiles(source))[(0, 4)]
    assert all(value == {"available": False, "value": None} for value in outcome["main"]["bit4_group"].values())


def test_unknown_type7_survivor_qualifies_only_with_a_known_supported_role():
    source = core()
    source["main"]["bit4_group"]["801:2"] = {"available": False, "value": None}
    assert set(by_cleared(profiles(source))) == {(0,), (0, 2)}


def test_bit1_loses_last_witness_and_aux_unknown_are_rejected():
    source = core()
    for i in (2, 3, 6):
        source["bit1"]["roles"][str(i)] = {"available": False, "supported": None}
    with pytest.raises(MainProjectionError):
        profiles(source)
    source = core()
    for i in (1, 2, 3):
        source["auxiliary"]["group"][f"800:{i}"] = {"available": False, "value": None}
    with pytest.raises(MainProjectionError):
        profiles(source)


def test_unrelated_unavailable_feature_stays_unavailable():
    source = core()
    source["controllers"]["13"] = controller(True)
    source["main"]["features"]["13"] = {"available": False, "value": None}
    for result in profiles(source):
        assert result["main"]["features"]["13"] == {"available": False, "value": None}


def test_main_controller_must_be_available_and_supported():
    for controller0 in (
        controller(False),
        {"available": False, "supported": None, "states_available": False, "configured_pstate_mask": None},
    ):
        source = core()
        source["controllers"]["0"] = controller0
        with pytest.raises(MainProjectionError, match="Main controller"):
            profiles(source)


def test_type7_initial_group_must_be_observable_and_active():
    for any_row in ({"available": False, "value": None}, row(0)):
        source = core()
        source["main"]["bit4_group"]["1:0"] = any_row
        with pytest.raises(MainProjectionError, match="No qualified"):
            profiles(source)


def test_type7_group_empty_only_when_last_supported_role_is_removed():
    source = core()
    source["main"]["bit4_group"]["801:2"] = row(0)
    outcomes = by_cleared(profiles(source))
    assert outcomes[(0,)]["main"]["bit4_group"]["1:0"] == row(1)
    assert outcomes[(0, 4)]["main"]["bit4_group"]["1:0"] == {"available": False, "value": None}


def test_controller9_already_clear_does_not_require_provider_knowledge():
    source = core()
    source["main"]["features"]["9"] = row(7)
    source["controllers"]["20"] = {
        "available": False,
        "supported": None,
        "states_available": False,
        "configured_pstate_mask": None,
    }
    source["main"]["features"]["20"] = {"available": False, "value": None}
    assert all(item["main"]["features"]["9"] == row(7) for item in profiles(source))


def test_unmodeled_auxiliary_bit1_and_type7_fields_preserved():
    source = core()
    for item in profiles(source):
        cleared = item["cleared"]
        for i in (2, 3):
            for attr in ("800", "806"):
                key = f"{attr}:{i}"
                assert item["auxiliary"]["group"][key] == source["auxiliary"]["group"][key]
        for i in (1, 4, 5, 6, 7, 8, 9, 10):
            assert item["bit1"]["roles"][str(i)] == source["bit1"]["roles"][str(i)]
        if 4 not in cleared:
            assert item["auxiliary"]["group"]["806:1"] == source["auxiliary"]["group"]["806:1"]
            assert item["main"]["bit4_group"]["801:1"] == source["main"]["bit4_group"]["801:1"]
        assert item["main"]["bit4_group"]["801:2"] == source["main"]["bit4_group"]["801:2"]
        assert item["main"]["bit4_group"]["805:1"] == source["main"]["bit4_group"]["805:1"]


def test_expanded_main_projection_can_observe_every_type4_role_unavailable():
    source = core()
    for i in (2, 3, 6):
        source["bit1"]["roles"][str(i)] = {"available": False, "supported": None}
    with pytest.raises(MainProjectionError):
        profiles(source)
    for result in profiles(source, allow_no_peer=True):
        assert all(not row["available"] for row in result["bit1"]["roles"].values())
