"""Tests for remote program control (start / stop / pause).

Two halves:

* the API write path — /State ``ProcessAction`` first, DOP2 GLOBAL_USER_REQ as
  a fallback, and the deliberate refusal to retry after a network error;
* the button gating — which appliances get the entities, when they are
  available, and what a rejected press tells the user.

Pure Python: the client is driven through a fake ``_request_bytes``, and the
button entities are exercised against hand-built coordinator stubs. No HA
event loop, no network, no appliance.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asyncmiele.exceptions.network import (  # noqa: E402
    NetworkTimeoutError,
    ResponseError,
)
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.miele_lan.api import (  # noqa: E402
    MieleLanClient,
    build_user_request_payload,
)
from custom_components.miele_lan.button import (  # noqa: E402
    BUTTONS,
    MieleLanButton,
    action_permitted,
    remote_control_enabled,
)
from custom_components.miele_lan.const import (  # noqa: E402
    OPCODE_PAUSE,
    OPCODE_START,
    OPCODE_STOP,
    PROCESS_ACTION_PAUSE,
    PROCESS_ACTION_START,
    PROCESS_ACTION_STOP,
    REMOTE_CONTROLLABLE_FAMILY,
    MieleAppliance,
    StateStatus,
)

ROUTE = "000106144430"


# --------------------------------------------------------------------------
# API write path
# --------------------------------------------------------------------------


class FakeRawClient:
    """Stands in for asyncmiele's MieleClient, recording every request.

    ``responses`` maps an HTTP method + resource prefix to either a
    (status, body) tuple or an exception instance to raise.
    """

    def __init__(self, responses=None):
        self.calls: list[tuple[str, str, object]] = []
        self._responses = responses or {}

    async def _request_bytes(self, method, resource, *, body=None, allowed_status=(200,)):
        self.calls.append((method, resource, body))
        for prefix, outcome in self._responses.items():
            if resource.startswith(prefix):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return (200, b"")


def make_client(responses=None) -> tuple[MieleLanClient, FakeRawClient]:
    raw = FakeRawClient(responses)
    return MieleLanClient(raw, ROUTE), raw  # type: ignore[arg-type]


def state_resource() -> str:
    return f"/Devices/{ROUTE}/State"


def dop2_resource() -> str:
    return f"/Devices/{ROUTE}/DOP2/2/1583"


@pytest.mark.parametrize(
    ("call", "expected_action"),
    [
        ("start_program", PROCESS_ACTION_START),
        ("stop_program", PROCESS_ACTION_STOP),
        ("pause_program", PROCESS_ACTION_PAUSE),
    ],
)
def test_happy_path_writes_process_action_to_state(call, expected_action):
    client, raw = make_client()
    asyncio.run(getattr(client, call)())

    assert len(raw.calls) == 1, "a successful /State write must not also hit DOP2"
    method, resource, body = raw.calls[0]
    assert method == "PUT"
    assert resource == state_resource()
    assert body == {"ProcessAction": expected_action}


def test_process_action_numbering_matches_cloud_api():
    """1/2/3 are the Miele cloud's processAction values (HA core MieleActions)."""
    assert (PROCESS_ACTION_START, PROCESS_ACTION_STOP, PROCESS_ACTION_PAUSE) == (1, 2, 3)


def test_rejects_unknown_process_action():
    client, raw = make_client()
    with pytest.raises(ValueError):
        asyncio.run(client.write_process_action(99))
    assert raw.calls == []


@pytest.mark.parametrize(
    ("call", "opcode"),
    [
        ("start_program", OPCODE_START),
        ("stop_program", OPCODE_STOP),
        ("pause_program", OPCODE_PAUSE),
    ],
)
def test_falls_back_to_dop2_when_state_write_is_refused(call, opcode):
    """A firmware with no writable /State ProcessAction still gets the command."""
    client, raw = make_client({state_resource(): ResponseError(404, "nope")})
    asyncio.run(getattr(client, call)())

    assert len(raw.calls) == 2
    assert raw.calls[0][1] == state_resource()
    method, resource, body = raw.calls[1]
    assert method == "PUT"
    assert resource.startswith(dop2_resource())
    assert body == build_user_request_payload(opcode)


def test_network_error_is_not_retried_over_dop2():
    """Safety: a timeout leaves the outcome unknown, so we must not fire again.

    Retrying START after a timeout that in fact landed would start a second
    cycle on a physical appliance.
    """
    client, raw = make_client({state_resource(): NetworkTimeoutError("timed out")})
    with pytest.raises(NetworkTimeoutError):
        asyncio.run(client.start_program())

    assert len(raw.calls) == 1
    assert raw.calls[0][1] == state_resource()


def test_both_paths_refused_reports_both_statuses():
    client, _ = make_client(
        {
            state_resource(): ResponseError(500, "refused"),
            dop2_resource(): ResponseError(500, "refused"),
        }
    )
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(client.start_program())

    message = str(excinfo.value)
    assert "Mobile controllable" in message, "keeps write_user_request's remedy"
    assert "500" in message, "surfaces the /State status too"


def test_process_action_body_survives_padding():
    """The tiny JSON body must still pad to an AES block boundary."""
    from custom_components.miele_lan.api import _pad_request_body

    raw = json.dumps({"ProcessAction": PROCESS_ACTION_START}).encode()
    padded = _pad_request_body(raw)
    assert len(padded) % 16 == 0
    assert json.loads(padded) == {"ProcessAction": 1}


# --------------------------------------------------------------------------
# Button gating
# --------------------------------------------------------------------------


def make_state(status=StateStatus.WAITING_TO_START, remote_enable=(15, 1, 1), **extra):
    state = {"Status": int(status), **extra}
    if remote_enable is not None:
        state["RemoteEnable"] = list(remote_enable)
    return state


def button_def(key):
    return next(d for d in BUTTONS if d.description.key == key)


def make_button(key, state, dop2_supported=True):
    """Build a real MieleLanButton over a stub coordinator.

    MieleLanEntity.__init__ only reads coordinator.client.route, and
    CoordinatorEntity.__init__ only registers a listener, so a stub is enough.
    """
    description = button_def(key).description
    coordinator = SimpleNamespace(
        client=SimpleNamespace(route=ROUTE),
        data=SimpleNamespace(state=state),
        last_update_success=True,
        dop2_supported=dop2_supported,
        async_add_listener=lambda *a, **k: (lambda: None),
    )
    button = MieleLanButton.__new__(MieleLanButton)
    button.coordinator = coordinator  # type: ignore[attr-defined]
    button.entity_description = description
    return button


def test_program_buttons_cover_laundry_and_dishwasher_only():
    for key in ("start", "stop", "pause"):
        types = button_def(key).types
        assert types == REMOTE_CONTROLLABLE_FAMILY
        assert MieleAppliance.DISHWASHER in types
        assert MieleAppliance.WASHING_MACHINE in types
        assert MieleAppliance.TUMBLE_DRYER in types
        # Ovens keep their existing DOP2 `stop_program` button and get no
        # remote start — see const.REMOTE_CONTROLLABLE_FAMILY.
        assert MieleAppliance.OVEN not in types


def test_only_start_is_disabled_by_default():
    assert button_def("start").description.entity_registry_enabled_default is False
    for key in ("stop", "pause"):
        assert button_def(key).description.entity_registry_enabled_default is True


def test_program_buttons_require_remote_enable_at_setup():
    for key in ("start", "stop", "pause"):
        assert button_def(key).description.required_state_key == "RemoteEnable"
    # Pre-existing buttons keep their unconditional setup.
    for key in ("wake", "stop_program"):
        assert button_def(key).description.required_state_key is None


def test_remote_control_enabled_tests_membership_not_index():
    assert remote_control_enabled({"RemoteEnable": [15, 1, 1]})
    assert remote_control_enabled({"RemoteEnable": [1, 1, 15]})
    assert not remote_control_enabled({"RemoteEnable": [0, 0, 0]})
    assert not remote_control_enabled({"RemoteEnable": None})
    assert not remote_control_enabled({})


def test_action_permitted_allows_press_when_status_is_absent():
    """Firmware that reports RemoteEnable but no Status must stay controllable."""
    assert action_permitted({"RemoteEnable": [15, 1, 1]}, {StateStatus.IN_USE})


@pytest.mark.parametrize(
    ("key", "status", "expected"),
    [
        # start is armed only from WaitingToStart, and resumes from Pause
        ("start", StateStatus.WAITING_TO_START, True),
        ("start", StateStatus.PAUSE, True),
        ("start", StateStatus.PROGRAMMED, False),
        ("start", StateStatus.OFF, False),
        ("start", StateStatus.IN_USE, False),
        ("start", StateStatus.PROGRAM_ENDED, False),
        # pause only while actually running
        ("pause", StateStatus.IN_USE, True),
        ("pause", StateStatus.WAITING_TO_START, False),
        ("pause", StateStatus.OFF, False),
        # stop across the whole active span
        ("stop", StateStatus.PROGRAMMED, True),
        ("stop", StateStatus.WAITING_TO_START, True),
        ("stop", StateStatus.IN_USE, True),
        ("stop", StateStatus.PAUSE, True),
        ("stop", StateStatus.RINSE_HOLD, True),
        ("stop", StateStatus.OFF, False),
        ("stop", StateStatus.PROGRAM_ENDED, False),
    ],
)
def test_availability_follows_status(key, status, expected):
    button = make_button(key, make_state(status=status))
    assert button.available is expected


def test_unavailable_when_remote_control_is_off():
    button = make_button("start", make_state(remote_enable=(0, 0, 0)))
    assert button.available is False


def test_wake_button_has_no_status_gate():
    """Wake must stay pressable from deep standby, where Status is Off."""
    button = make_button("wake", make_state(status=StateStatus.OFF))
    assert button.available is True


def test_press_when_remote_control_off_explains_the_panel_setting():
    button = make_button("start", make_state(remote_enable=(0, 0, 0)))
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(button.async_press())
    assert "Mobile controllable" in str(excinfo.value)


def test_press_when_wrong_status_names_the_status():
    button = make_button("start", make_state(status=StateStatus.PROGRAMMED))
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(button.async_press())
    message = str(excinfo.value)
    assert "programmed" in message
    assert "press Start on the appliance" in message


def test_rejected_press_never_reaches_the_appliance():
    """The interlock must fail before any write — not after."""
    button = make_button("start", make_state(status=StateStatus.OFF))
    calls: list[str] = []
    button.coordinator.client.start_program = lambda: calls.append("start")

    with pytest.raises(HomeAssistantError):
        asyncio.run(button.async_press())
    assert calls == []


def test_permitted_press_calls_through_and_refreshes():
    button = make_button("start", make_state(status=StateStatus.WAITING_TO_START))
    calls: list[str] = []

    async def start_program():
        calls.append("start")

    async def async_request_refresh():
        calls.append("refresh")

    button.coordinator.client.start_program = start_program
    button.coordinator.async_request_refresh = async_request_refresh

    asyncio.run(button.async_press())
    assert calls == ["start", "refresh"]
