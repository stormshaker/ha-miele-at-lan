"""DOP2 availability is measured, not assumed from the appliance type."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.miele_lan.sensor import ALL_TYPES, DOP2_SENSORS  # noqa: E402


def test_dop2_sensors_are_offered_to_every_appliance():
    """Whether DOP2 answers depends on the WiFi module, not the machine.

    An EK037 tumble dryer serves leaf 2/119 while EK057 dishwashers and washing
    machines 404 every leaf. Listing appliance families here would guess at
    something the coordinator has already measured — and did: gating on
    OVEN_FAMILY meant a dryer that could answer was never asked.
    """
    for definition in DOP2_SENSORS:
        assert definition.types == ALL_TYPES, definition.description.key


def test_dop2_sensors_still_require_the_data_to_have_arrived():
    """The real filter. No data, no sensor — regardless of appliance type."""
    for definition in DOP2_SENSORS:
        assert definition.description.required_dop2_key, definition.description.key


def test_the_probe_is_not_gated_on_appliance_family():
    """Regression guard for the gate this replaced.

    Parsed rather than grepped, so the docstring explaining the old gate does
    not trip it.
    """
    import ast

    source = (ROOT / "custom_components/miele_lan/coordinator.py").read_text()
    tree = ast.parse(source)
    probe = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_maybe_refresh_dop2"
    )
    body = probe.body[1:] if ast.get_docstring(probe) else probe.body
    code = "\n".join(ast.unparse(n) for n in body)
    assert "OVEN_FAMILY" not in code
    assert "device_type" not in code, "capability is measured, not inferred"


def test_power_switch_follows_measured_dop2_support():
    """Documents a consequence worth not rediscovering by surprise.

    The power switch writes OPCODE_SWITCH_ON over DOP2, so it cannot work where
    DOP2 is absent. It is gated on `coord.dop2_supported`, which used to be an
    untested default of True for anything never probed. Now that every appliance
    is probed, the switch disappears on modules without DOP2 rather than raising
    on every press — the same principle as not offering an unusable start button.
    """
    switch_src = (ROOT / "custom_components/miele_lan/switch.py").read_text()
    assert "if not coord.dop2_supported:" in switch_src
