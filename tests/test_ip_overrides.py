"""Tests for the per-appliance IP override helpers (no network)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.miele_lan.ip_overrides import (  # noqa: E402
    appliance_label,
    merge_static_ips,
    normalise_host,
)

DISHWASHER = "000111111111"
WASHER = "000222222222"
DRYER = "000333333333"


def test_merge_order_options_beat_mdns_beat_data() -> None:
    data = {DISHWASHER: "192.168.1.50", WASHER: "192.168.1.51", DRYER: "192.168.1.52"}
    mdns = {DISHWASHER: "192.168.1.60", WASHER: "192.168.1.61"}
    options = {DISHWASHER: "192.168.3.205"}
    merged = merge_static_ips(data, mdns, options)
    assert merged == {
        DISHWASHER: "192.168.3.205",  # options win over mDNS and data
        WASHER: "192.168.1.61",       # mDNS wins over data
        DRYER: "192.168.1.52",        # data only
    }


def test_merge_vlan_case_no_mdns() -> None:
    """Appliances moved to another VLAN: mDNS sees nothing, data is stale."""
    data = {DISHWASHER: "192.168.1.50", WASHER: "192.168.1.51", DRYER: "192.168.1.52"}
    options = {
        DISHWASHER: "192.168.3.205",
        WASHER: "192.168.3.252",
        DRYER: "192.168.3.253",
    }
    assert merge_static_ips(data, {}, options) == options


def test_merge_options_add_fab_missing_from_data() -> None:
    assert merge_static_ips(None, None, {DRYER: "192.168.3.253"}) == {DRYER: "192.168.3.253"}


def test_merge_blank_option_does_not_mask_discovery() -> None:
    merged = merge_static_ips({WASHER: "10.0.0.1"}, {WASHER: "10.0.0.2"}, {WASHER: "  "})
    assert merged == {WASHER: "10.0.0.2"}


def test_merge_all_empty() -> None:
    assert merge_static_ips(None, None, None) == {}


def test_merge_does_not_mutate_inputs() -> None:
    data = {WASHER: "10.0.0.1"}
    merge_static_ips(data, {WASHER: "10.0.0.2"}, {WASHER: "10.0.0.3"})
    assert data == {WASHER: "10.0.0.1"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.3.205", "192.168.3.205"),
        ("  192.168.3.252 ", "192.168.3.252"),
        ("fd00::1", "fd00::1"),
        ("[fd00::1]", "fd00::1"),
        ("FD00:0:0::1", "fd00::1"),
        ("dishwasher.iot.home.arpa", "dishwasher.iot.home.arpa"),
        ("Dishwasher.Local.", "dishwasher.local."),
        ("miele-dryer", "miele-dryer"),
        ("", ""),
        ("   ", ""),
        (None, ""),
    ],
)
def test_normalise_host_valid(raw, expected) -> None:
    assert normalise_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "192.168.3.300",
        "192.168.3",
        "1.2.3.4.5",
        "http://192.168.3.205",
        "192.168.3.205:80",
        "-bad.example",
        "bad-.example",
        "has space",
        "under_score.example",
        "a" * 64,
        "a..b",
        "fd00::zz",
    ],
)
def test_normalise_host_invalid(raw) -> None:
    assert normalise_host(raw) is None


def test_appliance_label_uses_name_and_fab() -> None:
    assert appliance_label(
        {"fabNr": DISHWASHER, "deviceName": "Dishwasher", "deviceType": 7}
    ) == f"Dishwasher ({DISHWASHER})"


def test_appliance_label_falls_back_to_type() -> None:
    assert appliance_label(
        {"fabNr": DRYER, "deviceName": "", "deviceType": 2}
    ) == f"Tumble dryer ({DRYER})"


def test_appliance_label_unknown_type() -> None:
    assert appliance_label(
        {"fabNr": WASHER, "deviceType": 999}
    ) == f"Miele appliance ({WASHER})"
