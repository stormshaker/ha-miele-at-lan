"""Options flow (per-appliance IP overrides) and the runtime merge in setup.

Drives the handlers directly against a small fake `hass` — no running Home
Assistant, no network. Needs the `homeassistant` package (CI installs it).
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402

import custom_components.miele_lan as integration  # noqa: E402
from custom_components.miele_lan.config_flow import (  # noqa: E402
    MieleLanConfigFlow,
    MieleLanOptionsFlowHandler,
)
from custom_components.miele_lan.const import CONF_STATIC_IPS  # noqa: E402

DISHWASHER = "000111111111"
WASHER = "000222222222"
DRYER = "000333333333"

DEVICES = [
    {"fabNr": DISHWASHER, "deviceName": "Dishwasher", "deviceType": 7},
    {"fabNr": WASHER, "deviceName": "Washing machine", "deviceType": 1},
    {"fabNr": DRYER, "deviceName": "", "deviceType": 2},
]
LABELS = {
    DISHWASHER: f"Dishwasher ({DISHWASHER})",
    WASHER: f"Washing machine ({WASHER})",
    DRYER: f"Tumble dryer ({DRYER})",
}


def _entry(*, options=None, state=ConfigEntryState.LOADED, devices=DEVICES):
    return SimpleNamespace(
        entry_id="entry1",
        domain="miele_lan",
        title="Miele@LAN",
        state=state,
        disabled_by=None,
        data={
            "flow_kind": "cloud",
            "group_id": "AABBCCDDEEFF0011",
            "group_key": "00" * 64,
            "ha_fab": "000999123456",
            "devices": devices,
            CONF_STATIC_IPS: {DISHWASHER: "192.168.1.50", WASHER: "192.168.1.51"},
        },
        options=options or {},
    )


def _flow(entry):
    config_entries = MagicMock()
    config_entries.async_get_known_entry.return_value = entry
    hass = SimpleNamespace(config_entries=config_entries)
    flow = MieleLanOptionsFlowHandler()
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "flow1"
    flow.context = {}
    return flow, config_entries


def _run(coro):
    return asyncio.run(coro)


def _suggested(result):
    return {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }


def test_config_flow_exposes_options_flow() -> None:
    handler = MieleLanConfigFlow.async_get_options_flow(_entry())
    assert isinstance(handler, MieleLanOptionsFlowHandler)


def test_form_lists_every_appliance_with_current_overrides() -> None:
    flow, _ = _flow(_entry(options={CONF_STATIC_IPS: {WASHER: "192.168.3.252"}}))
    result = _run(flow.async_step_init())
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert _suggested(result) == {
        LABELS[DISHWASHER]: "",
        LABELS[WASHER]: "192.168.3.252",
        LABELS[DRYER]: "",
    }
    # Setup-time addresses are shown for reference.
    discovered = result["description_placeholders"]["discovered"]
    assert "192.168.1.50" in discovered and f"{LABELS[DRYER]}: none" in discovered


def test_save_stores_ips_by_fab_and_drops_empty() -> None:
    flow, config_entries = _flow(_entry())
    result = _run(flow.async_step_init({
        LABELS[DISHWASHER]: "192.168.3.205",
        LABELS[WASHER]: " 192.168.3.252 ",
        LABELS[DRYER]: "",
    }))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_STATIC_IPS: {DISHWASHER: "192.168.3.205", WASHER: "192.168.3.252"}
    }
    # Loaded entry: the update listener reloads it; the flow doesn't.
    config_entries.async_schedule_reload.assert_not_called()


def test_clearing_a_field_removes_the_override() -> None:
    flow, _ = _flow(_entry(options={CONF_STATIC_IPS: {DRYER: "192.168.3.253"}}))
    result = _run(flow.async_step_init({LABELS[DRYER]: ""}))
    assert result["data"] == {CONF_STATIC_IPS: {}}


def test_invalid_ip_is_rejected_and_input_kept() -> None:
    flow, _ = _flow(_entry())
    result = _run(flow.async_step_init({
        LABELS[DISHWASHER]: "192.168.3.205",
        LABELS[WASHER]: "192.168.3.999",
    }))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {LABELS[WASHER]: "invalid_host"}
    assert _suggested(result)[LABELS[WASHER]] == "192.168.3.999"
    assert _suggested(result)[LABELS[DISHWASHER]] == "192.168.3.205"


def test_not_loaded_entry_is_saved_and_reloaded_by_the_flow() -> None:
    """Appliances moved VLAN → setup is in retry → no update listener exists."""
    entry = _entry(state=ConfigEntryState.SETUP_RETRY)
    flow, config_entries = _flow(entry)
    result = _run(flow.async_step_init({LABELS[DRYER]: "192.168.3.253"}))
    expected = {CONF_STATIC_IPS: {DRYER: "192.168.3.253"}}
    assert result["data"] == expected
    config_entries.async_update_entry.assert_called_once_with(entry, options=expected)
    config_entries.async_schedule_reload.assert_called_once_with("entry1")


def test_entry_without_devices_aborts() -> None:
    flow, _ = _flow(_entry(devices=[]))
    result = _run(flow.async_step_init())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_appliances"


def test_update_listener_reloads_entry() -> None:
    hass = SimpleNamespace(config_entries=MagicMock())
    hass.config_entries.async_reload = AsyncMock()
    _run(integration._async_options_updated(hass, _entry()))
    hass.config_entries.async_reload.assert_awaited_once_with("entry1")


def test_setup_merge_order_options_beat_mdns_beat_data() -> None:
    """_setup_cloud hands the resolver data < mDNS < options."""
    entry = _entry(options={CONF_STATIC_IPS: {DISHWASHER: "192.168.3.205"}})
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock()
    mdns = [
        {"fabNr": DISHWASHER, "host": "192.168.1.60"},  # stale: options win
        {"fabNr": WASHER, "host": "192.168.1.61"},      # beats data
    ]
    captured = {}

    def fake_resolver(*, static, zeroconf):
        captured["static"] = dict(static)
        return MagicMock()

    listener = MagicMock()
    listener.start = AsyncMock()
    listener.stop = AsyncMock()
    detect = MagicMock(return_value="192.168.1.109")
    with (
        patch.object(integration.ha_zeroconf, "async_get_async_instance", AsyncMock()),
        patch.object(integration, "mdns_discover_household", AsyncMock(return_value=mdns)),
        patch.object(integration, "MielePushListener", return_value=listener),
        patch.object(integration, "DeviceIpResolver", side_effect=fake_resolver),
        patch.object(integration, "enroll_all", AsyncMock(return_value=[])),
        patch.object(integration, "detect_lan_ip", detect),
    ):
        with pytest.raises(ConfigEntryNotReady):
            _run(integration._setup_cloud(SimpleNamespace(), entry))

    assert captured["static"] == {
        DISHWASHER: "192.168.3.205",
        WASHER: "192.168.1.61",
    }
    # Route toward the user-set address: it's the one we'll actually use.
    detect.assert_called_once_with("192.168.3.205")
