# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional, locally confirmed board presentation for an NCT3933U.

The NCT3933U itself only commands current.  These labels and voltage gains are
for one user's meter-confirmed board route, never controller-wide facts or
voltage telemetry.  Tune profiles continue to store native output bytes.
"""
import json
from pathlib import Path

from .controllers.nct3933 import current_step_ua, encode_current_ua
from .startup import atomic_json, read_json, state_dir


RAW_MODE = "Raw outputs (µA)"
VOLTAGE_MODE = "GPU / memory / PEX-PLL (mV)"
MODES = (RAW_MODE, VOLTAGE_MODE)
OUTPUT_ORDER = (3, 1, 2)

_LABELS = {3: "GPU offset (OUT3)", 1: "Memory offset (OUT1)",
           2: "PEX-PLL offset (OUT2)"}
_MV_PER_10UA = {1: 10, 2: 66, 3: 10}
_CONTROLLER = "NCT3933U"
_VERSION = 1


def _channel(channel):
    if type(channel) is not int or channel not in (1, 2, 3):
        raise ValueError("DAC channel must be 1, 2 or 3")
    return channel


def _mode(mode):
    if mode not in MODES or type(mode) is not str:
        raise ValueError("unknown NCT3933U display mode")
    return mode


def _integer(value, label):
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def output_label(channel, mode):
    channel, mode = _channel(channel), _mode(mode)
    return f"OUT{channel}" if mode == RAW_MODE else _LABELS[channel]


def display_value(current_ua, channel, mode):
    channel, mode = _channel(channel), _mode(mode)
    current_ua = _integer(current_ua, "nominal current")
    if mode == RAW_MODE:
        return current_ua
    numerator = -current_ua * _MV_PER_10UA[channel]
    if numerator % 10:
        raise ValueError("nominal current has no exact board-offset display value")
    return numerator // 10


def display_step(configuration, channel, mode):
    channel, mode = _channel(channel), _mode(mode)
    step_ua = current_step_ua(configuration, channel)
    return step_ua if mode == RAW_MODE else step_ua * _MV_PER_10UA[channel] // 10


def native_current(value, channel, mode, configuration):
    """Convert the display request, rejecting off-grid values before any write."""
    channel, mode = _channel(channel), _mode(mode)
    value = _integer(value, "display request")
    step = display_step(configuration, channel, mode)
    if value % step or abs(value) > 127 * step:
        unit = "mV" if mode == VOLTAGE_MODE else "µA"
        raise ValueError(f"Offset must be a multiple of {step} {unit} within ±{127 * step} {unit}")
    if mode == RAW_MODE:
        current_ua = value
    else:
        numerator = -value * 10
        gain = _MV_PER_10UA[channel]
        if numerator % gain:
            raise ValueError(f"offset must be a multiple of {gain} mV")
        current_ua = numerator // gain
    encode_current_ua(current_ua, configuration, channel)
    return current_ua


def _path(path):
    return Path(path) if path is not None else state_dir() / "nct3933-board.json"


def _identity(gpu, rail):
    static = getattr(gpu, "static", None)
    if not isinstance(static, dict):
        raise ValueError("selected GPU identity is unavailable")
    uuid = static.get("uuid")
    if uuid is not None and (type(uuid) is not str or (uuid and not uuid.strip())):
        raise ValueError("selected GPU UUID is malformed")
    profile = getattr(rail, "p", None)
    source = getattr(profile, "src", None)
    if not isinstance(source, dict) or source.get("controller") != _CONTROLLER:
        raise ValueError("selected controller is not NCT3933U")
    port, addr7 = getattr(profile, "port", None), getattr(rail, "addr7", None)
    if (type(port) is not int or not 0 <= port <= 7
            or type(addr7) is not int or not 0x10 <= addr7 <= 0x15
            or source.get("port") != port or source.get("addr7") != addr7):
        raise ValueError("NCT3933U controller route is inconsistent")
    return (uuid or "", _CONTROLLER, port, addr7)


def _key(identity):
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _document(document):
    if (type(document) is not dict or set(document) != {"version", "modes"}
            or type(document["version"]) is not int or document["version"] != _VERSION
            or type(document["modes"]) is not dict):
        raise ValueError("invalid NCT3933U board display settings")
    for key, mode in document["modes"].items():
        if type(key) is not str:
            raise ValueError("invalid NCT3933U board identity key")
        try:
            identity = json.loads(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid NCT3933U board identity key") from exc
        if (type(identity) is not list or len(identity) != 4
                or type(identity[0]) is not str or not identity[0].strip()
                or identity[1] != _CONTROLLER
                or type(identity[2]) is not int or not 0 <= identity[2] <= 7
                or type(identity[3]) is not int or not 0x10 <= identity[3] <= 0x15
                or key != _key(identity)):
            raise ValueError("invalid NCT3933U board identity key")
        _mode(mode)
    return document


def load_mode(gpu, rail, *, path=None):
    identity = _identity(gpu, rail)
    if not identity[0]:
        return RAW_MODE
    document = read_json(_path(path), default=None)
    if document is None:
        return RAW_MODE
    return _document(document)["modes"].get(_key(identity), RAW_MODE)


def save_mode(gpu, rail, mode, *, path=None):
    mode = _mode(mode)
    identity = _identity(gpu, rail)
    if not identity[0]:
        return False
    target = _path(path)
    document = read_json(target, default=None)
    document = {"version": _VERSION, "modes": {}} if document is None else _document(document)
    document["modes"][_key(identity)] = mode
    atomic_json(target, document)
    return True
