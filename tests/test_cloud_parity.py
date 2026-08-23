"""Sensors added to close measured gaps against the cloud integration.

Each was verified by reading both integrations for the same appliance at the
same moment, so the assertions below are parity claims, not guesses.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.miele_lan.binary_sensor import BINARY_SENSOR_TYPES  # noqa: E402
from custom_components.miele_lan.const import (  # noqa: E402
    REMOTE_ENABLE_SMART_GRID_INDEX,
    MieleAppliance,
)
from custom_components.miele_lan.sensor import SENSOR_TYPES  # noqa: E402

LIVE = json.loads(
    (ROOT / "tests/fixtures/live_state_ek057_ek037.json").read_text()
)


def sensor_def(key):
    return next(d for d in SENSOR_TYPES if d.description.key == key)


def binary_def(key):
    return next(d for d in BINARY_SENSOR_TYPES if d.description.key == key)


def test_spin_speed_matches_what_the_cloud_reported():
    """Cloud read 1600 rpm; /State carried SpinningSpeed 1600 at the same time."""
    washer = LIVE["washer_wwi860"]
    assert washer.get("SpinningSpeed") == 1600
    value_fn = sensor_def("spin_speed").description.value_fn
    assert value_fn({"Status": 5, "SpinningSpeed": 1600}) == 1600


def test_spin_speed_is_hidden_while_idle():
    """It is programme-dependent, so it follows the same idle gate as the rest.

    The firmware keeps the last value cached after a cycle ends; showing it
    would imply a spin speed for a machine that is not going to spin.
    """
    value_fn = sensor_def("spin_speed").description.value_fn
    assert value_fn({"Status": 1, "SpinningSpeed": 1600}) is None


def test_spin_speed_is_offered_only_to_appliances_that_wash():
    """A tumble dryer has no spin speed and would report the sentinel forever.

    Washer-dryers are included — they wash. This is the TwinDos lesson: do not
    publish a sensor an appliance can never populate.
    """
    types = sensor_def("spin_speed").types
    assert MieleAppliance.WASHING_MACHINE in types
    assert MieleAppliance.WASHER_DRYER in types
    assert MieleAppliance.TUMBLE_DRYER not in types
    assert MieleAppliance.DISHWASHER not in types


def test_target_temperature_now_covers_washing_machines():
    """Cloud read 40.0 °C; /State carried TargetTemperature[0] == 4000."""
    types = sensor_def("target_temperature").types
    assert MieleAppliance.WASHING_MACHINE in types
    assert MieleAppliance.OVEN in types, "ovens must keep it"
    assert MieleAppliance.TUMBLE_DRYER not in types

    value = sensor_def("target_temperature").description.value_fn(
        {"Status": 5, "TargetTemperature": [4000, -32768, -32768]}
    )
    assert value == 40.0


def test_smart_grid_reads_the_byte_the_panel_toggled():
    """RemoteEnable[1], identified by flipping the setting on a dryer's panel.

    It then agreed with the cloud's own smart_grid sensor on all three
    appliances at once: washer [15,1,0] on, dryer [15,1,0] on, dishwasher off.
    """
    is_on = binary_def("smart_grid").description.is_on_fn
    assert is_on({"RemoteEnable": [15, 1, 0]}) is True
    assert is_on({"RemoteEnable": [15, 0, 1]}) is False
    assert is_on({"RemoteEnable": []}) is False
    assert is_on({}) is False
    assert REMOTE_ENABLE_SMART_GRID_INDEX == 1


def test_smart_grid_is_not_confused_with_mobile_start():
    """The two live in adjacent slots and were conflated once already."""
    smart = binary_def("smart_grid").description.is_on_fn
    mobile = binary_def("mobile_start").description.is_on_fn
    only_smart = {"RemoteEnable": [15, 1, 0]}
    only_mobile = {"RemoteEnable": [15, 0, 1]}
    assert smart(only_smart) and not mobile(only_smart)
    assert mobile(only_mobile) and not smart(only_mobile)
