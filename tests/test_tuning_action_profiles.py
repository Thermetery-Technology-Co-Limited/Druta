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
