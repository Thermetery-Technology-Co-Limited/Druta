import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import profiles
from druta.druta import Druta


class GPU:
    def __init__(self):
        self._lock = threading.RLock()
        self.static = {"name": "GPU", "vbios": "v", "driver": "d", "uuid": "id", "slot": "slot"}
        self.nvml = SimpleNamespace(ok=True, has=lambda n: True)
        self.nvapi = SimpleNamespace(ok=True, Pstates20Set=True, ClkDomCtlSet=True, VoltCtrlGet=True, VoltCtrlSet=True)
        self.memory = 17
        self.scale = 16
        self.domains = {7: {"freq_khz": 12000, "mode": 3, "msvdd_uv": 123}}
        self.boost = 23
        self.calls = []

    def slot(self):
        return "slot"

    def _offset_range(self, kind):
        return (-200, 200, self.memory)

    def mem_offset_scale(self):
        return self.scale, "MHz"

    def clkdom_ok(self):
        return True

    def clkdom_layout(self):
        return object()

    def clkdom_domains(self):
        return list(self.domains)

    def read_clk_domain_offsets(self):
        return self.domains, None

    def read_voltage_boost(self):
        return self.boost

    def set_clock_offset(self, kind, value):
        self.calls.append(("memory", kind, value))
        return True, "ok"

    def set_clk_domain_offset(self, index, value):
        self.calls.append(("domain", index, value))
        return True, "ok"

    def set_voltage_boost(self, value):
        self.calls.append(("boost", value))
        self.boost = value
        return True, "ok"


@pytest.mark.parametrize(
    "kind,index,expected",
    [
        ("memory", None, ("memory", 2, 17 / 16)),
        ("domain", 7, ("domain", 7, 12)),
        ("boost", None, ("boost", 23)),
    ],
)
def test_selected_only(kind, index, expected):
    gpu = GPU()
    state = profiles.capture_tuning_action(gpu, kind, index)
    assert profiles.incomplete(state) == []
    assert profiles.restore(gpu, state) == [(True, "ok")]
    assert gpu.calls == [expected]
    assert set(state["tuning_action"]) == (
        {"kind", "index", "value", "scale"} if kind == "memory" else {"kind", "index", "value"}
    )


def test_domain_non_whole_mhz_refused_and_missing_readback():
    gpu = GPU()
    gpu.domains[7]["freq_khz"] = 12001
    with pytest.raises(ValueError, match="exactly"):
        profiles.capture_tuning_action(gpu, "domain", 7)
    gpu.domains = {}
    with pytest.raises(ValueError, match="unavailable"):
        profiles.capture_tuning_action(gpu, "domain", 7)


def test_incomplete_malformed_and_identity_refuse_before_write():
    gpu = GPU()
    state = profiles.capture_tuning_action(gpu, "boost")
    for mutate in (
        lambda s: s["incomplete"].append("missing"),
        lambda s: s["tuning_action"].update(value=True),
        lambda s: s["tuning_action"].update(other=4),
        lambda s: s["device"].update(uuid="different"),
        lambda s: s["device"].update(slot="different"),
    ):
        broken = copy.deepcopy(state)
        mutate(broken)
        assert profiles.restore(gpu, broken)[0][0] is False
    assert gpu.calls == []
    gpu.static.update(name="new", driver="new", vbios="new")
    assert profiles.restore(gpu, state)[0][0]


def test_memory_replays_raw_units_with_changed_scale():
    gpu = GPU()
    state = profiles.capture_tuning_action(gpu, "memory")
    gpu.scale = 2
    assert profiles.restore(gpu, state)[0][0]
    assert gpu.calls == [("memory", 2, 8.5)]


def test_existing_capability_checks_and_failed_read():
    gpu = GPU()
    gpu.nvml.ok = False
    gpu.nvapi.Pstates20Set = None
    with pytest.raises(ValueError, match="setter"):
        profiles.capture_tuning_action(gpu, "memory")
    gpu.nvapi.VoltCtrlSet = None
    with pytest.raises(ValueError, match="unavailable"):
        profiles.capture_tuning_action(gpu, "boost")
    gpu.nvapi.VoltCtrlSet = True
    gpu.boost = None
    with pytest.raises(ValueError, match="boost"):
        profiles.capture_tuning_action(gpu, "boost")


@pytest.mark.parametrize("reason", ["range", "step"])
def test_unreplayable_memory_capture_blocks_ui_write(reason):
    gpu = GPU()
    if reason == "range":
        gpu.memory = 201
    else:
        gpu.memory_offset_step_units = lambda: 2
    app = Druta.__new__(Druta)
    app.gpu = gpu
    app.log = Mock()
    writer = Mock(return_value=(True, "written"))
    result = app.write_tuning_action("memory", 2, writer)
    assert result[0] is False
    assert "Undo" in result[1]
    writer.assert_not_called()
    with pytest.raises(ValueError, match="range|representable"):
        profiles.capture_tuning_action(gpu, "memory")

class RailGPU(GPU):
    RAIL_OFFSET_MAX_MV = 200
    RAIL_OFFSET_XOC_MAX_MV = 500

    def __init__(self):
        super().__init__()
        self.offset_uv = 12501
        self.available = True
        self.voltage_xoc_enabled = False

    def rail_offset_capability(self, rail, domain):
        assert (rail, domain) == (0, 0)
        return {'available': self.available, 'value_mv': self.offset_uv / 1000}

    def set_rail_offset_mv(self, value, domain):
        assert domain == 0
        self.calls.append(('rail_offset', value))
        self.offset_uv = round(value * 1000)
        return True, 'ok'


def test_rail_offset_exact_selected_capture_and_restore():
    gpu = RailGPU()
    gpu.read_clk_domain_offsets = lambda: (_ for _ in ()).throw(AssertionError('unrelated read'))
    state = profiles.capture_tuning_action(gpu, 'rail_offset')
    assert state['tuning_action']['value'] == 12501
    gpu.offset_uv = 0
    assert profiles.restore(gpu, state) == [(True, 'ok')]
    assert gpu.offset_uv == 12501
    assert 'NVVDD offset +12.501 mV' in profiles.summarize(state)
    gpu.available = False
    with pytest.raises(ValueError, match='readback unavailable'):
        profiles.capture_tuning_action(gpu, 'rail_offset')


def test_rail_offset_restore_respects_live_bounds_and_readback():
    gpu = RailGPU()
    state = profiles.capture_tuning_action(gpu, 'rail_offset')
    state['tuning_action']['value'] = 250000
    assert 'bounds' in profiles.preflight(gpu, state)
    assert not gpu.calls
    gpu.voltage_xoc_enabled = True
    assert profiles.restore(gpu, state)[0][0]
    gpu.voltage_xoc_enabled = False
    state['tuning_action']['value'] = 225000
    assert profiles.restore(gpu, state)[0][0]  # lower an existing out-of-normal request
    gpu.set_rail_offset_mv = lambda *a: (True, 'accepted')
    state['tuning_action']['value'] = 20000
    assert 'mismatch' in profiles.restore(gpu, state)[0][1]


def test_rail_offset_action_undo_noop_and_capture_failure(monkeypatch):
    gpu = RailGPU()
    app = Druta.__new__(Druta)
    app.gpu = gpu
    app.log = lambda *a: None
    saved = []
    monkeypatch.setattr(profiles, 'autosave_state', lambda name, state: (saved.append(copy.deepcopy(state)) or ('undo', '', [])))
    def writer():
        return gpu.set_rail_offset_mv(25.125, 0)
    assert app.write_tuning_action('rail_offset', 12.501, writer)[0]
    assert not saved and not gpu.calls
    assert app.write_tuning_action('rail_offset', 25.125, writer)[0]
    assert saved[0]['tuning_action']['value'] == 12501
    assert gpu.offset_uv == 25125
    gpu.available = False
    assert not app.write_tuning_action('rail_offset', 30, writer)[0]
    assert gpu.offset_uv == 25125
    gpu.available = True
    monkeypatch.setattr(profiles, 'autosave_state', lambda *a: (_ for _ in ()).throw(OSError('failed')))
    assert not app.write_tuning_action('rail_offset', 30, writer)[0]
    assert gpu.offset_uv == 25125


def test_nvvdd_apply_keeps_hold_warning_and_undo(monkeypatch):
    from unittest.mock import patch
    gpu = RailGPU()
    gpu.read_vf_lock = lambda: {'volt_mv': 900}
    app = Druta.__new__(Druta)
    app.gpu = gpu
    app.guard = lambda: True
    app.knob_input_value = lambda key, value: value
    app.log = lambda *a: None
    results = []
    app.report = results.append
    snapshots = []
    monkeypatch.setattr(profiles, 'autosave_state', lambda name, state: (snapshots.append(state) or ('undo', '', [])))
    with patch('druta.druta.dpg.does_item_exist', return_value=False):
        app.apply_rail(25.125)
    assert gpu.offset_uv == 25125
    assert snapshots[0]['tuning_action']['value'] == 12501
    assert results[0][0] and 'pinning this rail' in results[0][1]
    assert profiles.restore(gpu, snapshots[0])[0][0]
    assert gpu.offset_uv == 12501
