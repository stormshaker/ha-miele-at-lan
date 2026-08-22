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
    can_start,
    delay_start_pending,
    mobile_start_armed,
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


# Captured verbatim from a G7369 (XKM EK057 FW 08.37), 2026-08-22, by writing
# a real key and a deliberately bogus one to /State on an appliance that was
# switched off. Both answered HTTP 200 — the outcome is only in the body.
ACK_SUCCESS = b'[\n{"Success":{"ProcessAction":0}}\n]'
ACK_FAILURE = b'[\n\t{"Failure":{"NotAField":null}}\n]'


def test_success_and_failure_acks_are_told_apart():
    from custom_components.miele_lan.api import action_was_refused

    assert action_was_refused(ACK_FAILURE)
    assert not action_was_refused(ACK_SUCCESS)
    assert not action_was_refused(b""), "204 / empty body is an accepted write"
    assert not action_was_refused(b"not json"), "unparseable is not a refusal"


def test_failure_inside_a_200_still_triggers_the_fallback():
    """The refusal is in the body, not the status. Reading only the status
    would report a start that never happened as a success."""
    client, raw = make_client({state_resource(): (200, ACK_FAILURE)})
    asyncio.run(client.start_program())

    assert len(raw.calls) == 2, "a Failure ack must not be taken for success"
    assert raw.calls[1][1].startswith(dop2_resource())


def test_success_ack_stops_there():
    client, raw = make_client({state_resource(): (200, ACK_SUCCESS)})
    asyncio.run(client.start_program())
    assert len(raw.calls) == 1


def test_failure_on_both_paths_says_so():
    client, _ = make_client(
        {
            state_resource(): (200, ACK_FAILURE),
            dop2_resource(): ResponseError(500, "refused"),
        }
    )
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(client.start_program())
    assert "Failure response" in str(excinfo.value)


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
    for key in ("stop", "pause"):
        types = button_def(key).types
        assert types == REMOTE_CONTROLLABLE_FAMILY
        assert MieleAppliance.DISHWASHER in types
        assert MieleAppliance.WASHING_MACHINE in types
        assert MieleAppliance.TUMBLE_DRYER in types
        # Ovens keep their existing DOP2 `stop_program` button and get no
        # remote start — see const.REMOTE_CONTROLLABLE_FAMILY.
        assert MieleAppliance.OVEN not in types


def test_there_is_no_start_button():
    """Remote start is cloud-only; see button.py's module docstring.

    Measured on a genuinely armed G7369: the LAN write was acknowledged and
    ignored, and the cloud integration started the same appliance minutes later.
    Shipping a button that never works would be worse than shipping none.
    """
    assert not [d for d in BUTTONS if d.description.key == "start"]


def test_halting_buttons_are_enabled_by_default():
    """stop and pause can only ever halt a machine, so they carry no risk."""
    for key in ("stop", "pause"):
        assert button_def(key).description.entity_registry_enabled_default is True


def test_program_buttons_require_remote_enable_at_setup():
    for key in ("stop", "pause"):
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
    button = make_button("stop", make_state(status=StateStatus.IN_USE,
                                            remote_enable=(0, 0, 0)))
    assert button.available is False


def test_wake_button_has_no_status_gate():
    """Wake must stay pressable from deep standby, where Status is Off."""
    button = make_button("wake", make_state(status=StateStatus.OFF))
    assert button.available is True


def test_press_when_remote_control_off_explains_the_panel_setting():
    button = make_button("stop", make_state(status=StateStatus.IN_USE,
                                            remote_enable=(0, 0, 0)))
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(button.async_press())
    assert "Mobile controllable" in str(excinfo.value)


def test_press_when_wrong_status_names_the_status():
    button = make_button("pause", make_state(status=StateStatus.PROGRAMMED))
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(button.async_press())
    assert "programmed" in str(excinfo.value)


def test_rejected_press_never_reaches_the_appliance():
    """The interlock must fail before any write — not after."""
    button = make_button("pause", make_state(status=StateStatus.OFF))
    calls: list[str] = []
    button.coordinator.client.pause_program = lambda: calls.append("pause")

    with pytest.raises(HomeAssistantError):
        asyncio.run(button.async_press())
    assert calls == []


def test_permitted_press_calls_through_and_refreshes(monkeypatch):
    import custom_components.miele_lan.button as button_mod

    monkeypatch.setattr(button_mod, "VERIFY_INTERVAL", 0.01)
    state = make_state(status=StateStatus.IN_USE)
    calls: list[str] = []

    button = make_button("stop", state)

    async def stop_program():
        calls.append("stop")

    async def read_state_dict():
        return {**state, "Status": int(StateStatus.PROGRAM_INTERRUPTED)}

    async def async_request_refresh():
        calls.append("refresh")

    button.coordinator.client.stop_program = stop_program
    button.coordinator.client.read_state_dict = read_state_dict
    button.coordinator.async_request_refresh = async_request_refresh

    asyncio.run(button.async_press())
    assert calls == ["stop", "refresh"]


# --------------------------------------------------------------------------
# Live-capture regression
# --------------------------------------------------------------------------

_LIVE_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "live_state_ek057_ek037.json").read_text()
)
# Underscore-prefixed keys are provenance notes, not appliances.
LIVE_STATES = {k: v for k, v in _LIVE_FIXTURE.items() if not k.startswith("_")}


def live(name):
    return {k: v for k, v in LIVE_STATES[name].items() if not k.startswith("_")}


@pytest.mark.parametrize(
    ("appliance", "expected"),
    [
        ("dishwasher_g7369", {"stop": True, "pause": True}),
        ("washer_wwi860", {"stop": True, "pause": False}),
        ("dryer_twl780wp", {"stop": True, "pause": False}),
    ],
)
def test_availability_against_real_appliance_states(appliance, expected):
    state = live(appliance)
    for key, want in expected.items():
        assert make_button(key, state).available is want, f"{appliance}/{key}"


def test_mobile_start_bit_matches_the_cloud_across_the_live_captures():
    """RemoteEnable[2] is the cloud's `remoteEnable.mobileStart`.

    Established by reading both sides at the same moment on 2026-08-22. The
    dishwasher was mid-cycle with the cloud reporting mobileStart on; the other
    two were idle with it off. Each capture's `_cloud_mobile_start` field records
    what HA's cloud integration said at that instant.
    """
    for name, capture in _LIVE_FIXTURE.items():
        if name.startswith("_"):
            continue
        expected = capture["_cloud_mobile_start"]
        assert mobile_start_armed(live(name)) is expected, name


def test_start_needs_both_the_status_and_the_armed_bit():
    """Neither half is sufficient on its own.

    Status 4 also covers a delay-start countdown; the armed bit stays set right
    through a running cycle. Only the conjunction means "waiting on us".
    """
    armed_and_waiting = {"Status": 4, "RemoteEnable": [15, 0, 1], "StartTime": [0, 0]}
    assert can_start(armed_and_waiting)

    # Right status, not armed — the washer's delay-start case.
    assert not can_start({"Status": 4, "RemoteEnable": [15, 0, 0], "StartTime": [10, 29]})
    # Armed, wrong status — the dishwasher mid-cycle case.
    assert not can_start({"Status": 5, "RemoteEnable": [15, 0, 1], "StartTime": [0, 0]})
    # Armed and paused resumes.
    assert can_start({"Status": 6, "RemoteEnable": [15, 0, 1], "StartTime": [0, 0]})


def test_armed_bit_beats_a_delay_start():
    """A delay start does not veto an appliance that says it is armed.

    StartTime explains the common case but is not itself the gate — if firmware
    ever reports both, we defer to the bit the appliance sets.
    """
    assert can_start({"Status": 4, "RemoteEnable": [15, 0, 1], "StartTime": [2, 30]})


def test_short_or_absent_remote_enable_does_not_block():
    assert mobile_start_armed({})
    assert mobile_start_armed({"RemoteEnable": [15]})
    assert mobile_start_armed({"RemoteEnable": None})


def test_mobile_start_binary_sensor_reads_the_same_bit():
    """The sensor and the button gate must not drift apart again.

    It previously read index 0 (fullRemoteControl) while being named
    mobile_start, so it permanently disagreed with HA core's sensor of the same
    name for the same appliance.
    """
    from custom_components.miele_lan.binary_sensor import BINARY_SENSOR_TYPES

    definition = next(
        d for d in BINARY_SENSOR_TYPES if d.description.key == "mobile_start"
    )
    is_on = definition.description.is_on_fn
    for name, capture in _LIVE_FIXTURE.items():
        if name.startswith("_"):
            continue
        assert is_on(live(name)) is capture["_cloud_mobile_start"], name


def test_delay_start_countdown_does_not_offer_start():
    """Regression for a start that was accepted and did nothing.

    The washer was captured at Status 4 with StartTime [10, 29] — a delay-start
    countdown, not an appliance waiting on us. `{"ProcessAction": 1}` was
    accepted (HTTP 200, no Failure envelope, no fallback) and ignored: still
    Status 4 twenty seconds later. Status alone cannot tell the two Status-4
    situations apart, so the button must not offer start here.
    """
    washer = live("washer_wwi860")
    assert washer["Status"] == int(StateStatus.WAITING_TO_START)
    assert delay_start_pending(washer)
    assert not can_start(washer)

    # Stop is unaffected — a countdown is still cancellable.
    assert make_button("stop", washer).available is True


def test_no_delay_means_no_block():
    assert not delay_start_pending({"StartTime": [0, 0]})
    assert not delay_start_pending({}), "firmware without StartTime is not blocked"


def test_armed_appliance_offers_start_even_mid_delay():
    """The capture the gate is really for.

    A dishwasher armed for remote start (RemoteEnable[2] == 1) while a SmartStart
    delay counts down (StartTime [13, 20]). Both true at once — which is the
    normal state for a solar-triggered automation, whose entire job is to
    override the queued delay when conditions are good. A gate that vetoed on
    StartTime would refuse precisely this.
    """
    state = live("dishwasher_g7369_armed")
    assert state["Status"] == int(StateStatus.WAITING_TO_START)
    assert delay_start_pending(state), "a delay really is pending"
    assert can_start(state), "the predicate agrees it is startable"
    assert make_button("stop", state).available is True
    assert make_button("pause", state).available is False


def test_every_live_appliance_reports_the_setup_gate():
    """`required_state_key="RemoteEnable"` must not filter out real hardware."""
    for name in LIVE_STATES:
        assert "RemoteEnable" in live(name), name


def test_remote_enable_is_a_capability_not_a_readiness_flag():
    """Why availability keys off Status rather than any RemoteEnable index.

    Captured live: the washer is armed for remote start (Status 4) and the
    dryer is not (Status 3), yet both report an identical RemoteEnable. The
    running dishwasher differs only in the last element, which therefore does
    not track readiness either. No index of this array distinguishes the armed
    appliance from the unarmed one — only Status does.

    This is also why `binary_sensor.<name>_mobile_start` here (RemoteEnable[0]
    == 15) routinely disagrees with the cloud integration's sensor of the same
    name: the cloud reports readiness, this reports capability.
    """
    washer, dryer = live("washer_wwi860"), live("dryer_twl780wp")
    assert washer["RemoteEnable"] == dryer["RemoteEnable"]
    assert washer["Status"] != dryer["Status"]

    for name in LIVE_STATES:
        assert remote_control_enabled(live(name)), "capability is on everywhere"


def test_panel_advertising_remote_start_is_not_arming():
    """The dryer capture: appliance says one thing, the bit says another.

    Its panel read "Cool Air, Start In 5h, RemoteStart possible" — the machine
    advertising remote start in as many words — while reporting the mobile-start
    bit as 0. A ProcessAction 1 written in that state was accepted and ignored.
    The gate has to believe the bit, not the marketing.
    """
    state = live("dryer_twl780wp_delay_not_armed")
    assert state["Status"] == int(StateStatus.WAITING_TO_START)
    assert remote_control_enabled(state), "full remote control really is on"
    assert not can_start(state)
    assert make_button("stop", state).available is True


# --------------------------------------------------------------------------
# Press verification — the appliance's ack is not evidence
# --------------------------------------------------------------------------


def make_verifying_button(key, state, status_sequence):
    """Button whose appliance reports `status_sequence` on successive reads."""
    button = make_button(key, state)
    remaining = list(status_sequence)

    async def read_state_dict():
        value = remaining.pop(0) if remaining else status_sequence[-1]
        return {**state, "Status": int(value)}

    async def noop():
        return None

    async def async_request_refresh():
        return None

    button.coordinator.client.read_state_dict = read_state_dict
    button.coordinator.client.stop_program = noop
    button.coordinator.client.pause_program = noop
    button.coordinator.client.start_program = noop
    button.coordinator.async_request_refresh = async_request_refresh
    return button


def test_press_raises_when_the_appliance_ignores_it(monkeypatch):
    """The dryer case: pause acknowledged, status never moves.

    Measured on a TWL780WP, whose model does not support remote pause — the
    cloud integration's own pause button has been permanently unavailable for
    it. Without this check the press reports success and nothing happens.
    """
    import custom_components.miele_lan.button as button_mod

    monkeypatch.setattr(button_mod, "VERIFY_TIMEOUT", 0.3)
    monkeypatch.setattr(button_mod, "VERIFY_INTERVAL", 0.05)

    state = make_state(status=StateStatus.IN_USE)
    button = make_verifying_button("pause", state, [StateStatus.IN_USE])

    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(button.async_press())
    message = str(excinfo.value)
    assert "did not act on it" in message
    assert "in_use" in message


def test_press_is_satisfied_as_soon_as_status_moves(monkeypatch):
    """The stop case, measured live: in_use -> program_interrupted in ~2.5s."""
    import custom_components.miele_lan.button as button_mod

    monkeypatch.setattr(button_mod, "VERIFY_TIMEOUT", 5.0)
    monkeypatch.setattr(button_mod, "VERIFY_INTERVAL", 0.05)

    state = make_state(status=StateStatus.IN_USE)
    button = make_verifying_button(
        "stop", state, [StateStatus.IN_USE, StateStatus.PROGRAM_INTERRUPTED]
    )
    asyncio.run(button.async_press())  # must not raise


def test_unreachable_appliance_is_not_reported_as_ignored(monkeypatch):
    """A read failure means we cannot tell, which is not the same as a no-op."""
    import custom_components.miele_lan.button as button_mod

    monkeypatch.setattr(button_mod, "VERIFY_TIMEOUT", 0.3)
    monkeypatch.setattr(button_mod, "VERIFY_INTERVAL", 0.05)

    state = make_state(status=StateStatus.IN_USE)
    button = make_verifying_button("stop", state, [StateStatus.IN_USE])

    async def boom():
        raise OSError("network went away")

    button.coordinator.client.read_state_dict = boom
    asyncio.run(button.async_press())  # must not raise


def test_wake_and_stop_program_are_not_verified():
    """Pre-existing buttons keep their original behaviour untouched."""
    for key in ("wake", "stop_program"):
        assert button_def(key).description.verify_status_change is False
    for key in ("stop", "pause"):
        assert button_def(key).description.verify_status_change is True
