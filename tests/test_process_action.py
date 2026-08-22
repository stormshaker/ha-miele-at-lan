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


def writes(raw):
    """Just the commands, dropping the StandbyState read start() does first."""
    return [c for c in raw.calls if c[0] != "GET"]


def state_resource() -> str:
    return f"/Devices/{ROUTE}/State"


def dop2_resource() -> str:
    return f"/Devices/{ROUTE}/DOP2/2/1583"


@pytest.mark.parametrize(
    ("call", "expected_action"),
    [
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

    Retrying a command after a timeout that in fact landed could start a second
    cycle on a physical appliance.
    """
    client, raw = make_client({state_resource(): NetworkTimeoutError("timed out")})
    with pytest.raises(NetworkTimeoutError):
        asyncio.run(client.stop_program())

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
        asyncio.run(client.stop_program())

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
    asyncio.run(client.stop_program())

    assert len(raw.calls) == 2, "a Failure ack must not be taken for success"
    assert raw.calls[1][1].startswith(dop2_resource())


def test_success_ack_stops_there():
    client, raw = make_client({state_resource(): (200, ACK_SUCCESS)})
    asyncio.run(client.stop_program())
    assert len(raw.calls) == 1


def test_failure_on_both_paths_says_so():
    client, _ = make_client(
        {
            state_resource(): (200, ACK_FAILURE),
            dop2_resource(): ResponseError(500, "refused"),
        }
    )
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(client.stop_program())
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


def test_start_is_disabled_by_default():
    """It is the one button that can set a physical cycle going.

    It is also the one whose availability the appliance, not we, decide — see
    button.py's module docstring for what was measured.
    """
    assert button_def("start").description.entity_registry_enabled_default is False


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


def test_start_gate_is_status_only():
    """The conditions that survived contact with the appliance.

    The write that actually started a dryer did so with the mobile-start bit at
    0 and ProgramType 3 — so neither of those can be required. What was true:
    remote control on, and Status 4.
    """
    assert can_start({"Status": 4, "RemoteEnable": [15, 1, 0], "ProgramType": 3})
    assert can_start({"Status": 6, "RemoteEnable": [15, 0, 0]}), "resumes from pause"

    assert not can_start({"Status": 5, "RemoteEnable": [15, 1, 1]}), "already running"
    assert not can_start({"Status": 3, "RemoteEnable": [15, 1, 1]}), "not armed yet"
    assert not can_start({"Status": 1, "RemoteEnable": [15, 1, 1]}), "off"
    assert not can_start({"Status": 4, "RemoteEnable": [0, 0, 0]}), "remote control off"


def test_the_state_that_actually_started_a_dryer():
    """Captured immediately before the first successful remote start.

    TWL780WP waiting on a SmartStart window with ~2h to run. The DOP2 opcode
    took it to IN_USE in three seconds and zeroed StartTime.
    """
    state = {"Status": 4, "RemoteEnable": [15, 1, 0], "ProgramType": 3,
             "ProgramID": 9, "StartTime": [1, 58], "RemainingTime": [1, 3]}
    assert can_start(state)
    assert delay_start_pending(state), "and a timer was running, which it cut short"
    assert not mobile_start_armed(state), "with the mobile-start bit clear"


def test_start_button_is_not_gated_on_dop2():
    """Both write paths are real, so neither may exclude the button.

    A G7369 whose firmware 404s every DOP2 leaf — it does not even advertise the
    resource — was started successfully by the /State fallback, WAITING_TO_START
    to IN_USE in two seconds. Gating the button on DOP2 availability would have
    removed it from an appliance that can start.
    """
    assert not hasattr(button_def("start").description, "requires_user_request")
    assert button_def("start").description.requires_dop2 is False


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
    # A countdown is exactly what remote start is for — cutting it short — so
    # the gate allows it. What stops the washer is that its firmware blocks the
    # DOP2 leaf entirely, which is filtered at setup, not here.
    assert can_start(washer)
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
    assert mobile_start_armed(state), "and the appliance reports itself capable"
    assert can_start(state)

    # It still could not be started locally — but for a reason invisible in
    # /State: this XKM answers 404 for the DOP2 leaf, which is the only write
    # path that starts anything. Filtered at setup (_dop2_2_1583 in the fixture).
    assert LIVE_STATES["dishwasher_g7369_armed"]["_dop2_2_1583"] == 404
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
    # This one really is startable — and later was, once we stopped writing to
    # /State and used the DOP2 opcode instead. Its DOP2 leaf answers 200.
    assert can_start(state)
    assert LIVE_STATES["dryer_twl780wp_delay_not_armed"]["_dop2_2_1583"] == 200
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


# --------------------------------------------------------------------------
# Start goes DOP2-first — the opposite of stop and pause
# --------------------------------------------------------------------------


def test_start_tries_dop2_before_state():
    """Measured: /State ProcessAction 1 is accepted and ignored, every time.

    Four writes across three appliances, including a fully armed dishwasher the
    cloud started seconds later. The DOP2 opcode moved a TWL780WP from
    WAITING_TO_START to IN_USE in three seconds, cutting a SmartStart timer
    short. Since /State answers Success whether or not it intends to act, a
    /State-first start would never reach the path that works.
    """
    client, raw = make_client()
    asyncio.run(client.start_program())

    sent = writes(raw)
    assert len(sent) == 1, "a successful DOP2 start must not also hit /State"
    method, resource, body = sent[0]
    assert method == "PUT"
    assert resource.startswith(dop2_resource())
    assert body == build_user_request_payload(OPCODE_START)


def test_start_falls_back_to_state_when_dop2_is_blocked():
    """EK057 FW 08.37 returns 404 for the leaf, so try the other path anyway.

    No appliance has been seen where that fallback actually starts anything, but
    it costs one refused request and firmware varies more than we can measure.
    """
    client, raw = make_client({dop2_resource(): ResponseError(404, "blocked")})
    asyncio.run(client.start_program())

    sent = writes(raw)
    assert len(sent) == 2
    assert sent[0][1].startswith(dop2_resource())
    assert sent[1][1] == state_resource()
    assert sent[1][2] == {"ProcessAction": PROCESS_ACTION_START}


def test_stop_and_pause_still_go_via_state_first():
    """They are demonstrably honoured there, and DOP2 is absent on most units."""
    for call in ("stop_program", "pause_program"):
        client, raw = make_client()
        asyncio.run(getattr(client, call)())
        assert raw.calls[0][1] == state_resource(), call


def test_start_surfaces_a_refusal_from_both_paths():
    client, _ = make_client(
        {
            dop2_resource(): ResponseError(404, "blocked"),
            state_resource(): (200, ACK_FAILURE),
        }
    )
    with pytest.raises(HomeAssistantError) as excinfo:
        asyncio.run(client.start_program())
    assert "refused" in str(excinfo.value)


def test_dop2_support_probe_distinguishes_absent_from_refusing():
    """404 means the subsystem is gone; 500 means it is busy saying no.

    EK057 FW 08.37 answered 404 for all twelve leaves probed across four units —
    DOP2 is simply absent. An EK037 answered 200 for the same leaf while armed
    and 500 for every leaf once idle. Treating 500 as unsupported would delete
    the start button from an appliance that can start, based on nothing but when
    Home Assistant happened to restart.
    """
    for status, expected in ((200, True), (500, True), (403, True), (404, False)):
        client, _ = make_client({f"/Devices/{ROUTE}/DOP2": (status, b"")})
        assert asyncio.run(client.supports_user_request()) is expected, status


def test_unreachable_appliance_keeps_its_start_button():
    """A button that errors on press beats one that silently isn't there."""
    client, _ = make_client({f"/Devices/{ROUTE}/DOP2": NetworkTimeoutError("down")})
    assert asyncio.run(client.supports_user_request()) is True


def test_both_write_paths_are_load_bearing():
    """One appliance starts only over DOP2, the other only over /State.

    The dryer's /State ProcessAction 1 was accepted and ignored; its DOP2 opcode
    started it in three seconds. The dishwasher 404s every DOP2 leaf and does not
    advertise the resource at all; its /State write started it in two. Dropping
    either path would leave one of them uncontrollable.
    """
    dryer = LIVE_STATES["dryer_twl780wp_delay_not_armed"]
    dishwasher = LIVE_STATES["dishwasher_g7369_started_ok"]
    assert dryer["_dop2_2_1583"] == 200
    assert dishwasher["_dop2_2_1583"] == 404
    assert dishwasher["_start_succeeded"] is True
    assert can_start(live("dishwasher_g7369_started_ok"))


def test_the_two_dishwasher_states_differ_only_in_phase_and_standby():
    """The pair that solved the mystery.

    Same appliance, same Status, same RemoteEnable, same ProgramType — one start
    refused, one accepted. StandbyState is the difference, and a controlled A/B
    on the sleeping appliance confirmed cause rather than correlation: the write
    was ignored asleep and honoured two seconds after a wake.
    """
    failed = live("dishwasher_g7369_armed")
    worked = live("dishwasher_g7369_started_ok")

    for field in ("Status", "RemoteEnable", "ProgramType"):
        assert failed[field] == worked[field], field

    assert failed["ProgramPhase"] == 0 and worked["ProgramPhase"] == 1792
    assert failed["StandbyState"] == 2 and worked["StandbyState"] == 1

    # Both are still offered the button — being asleep is no longer a reason to
    # refuse, because start_program() wakes the appliance before writing.
    assert can_start(failed) and can_start(worked)


# --------------------------------------------------------------------------
# Waking a sleeping appliance before commanding it
# --------------------------------------------------------------------------


class SleepyClient(FakeRawClient):
    """Serves a chosen StandbyState, and wakes when told to."""

    def __init__(self, standby):
        super().__init__()
        self.standby = standby

    async def _request_bytes(self, method, resource, *, body=None, allowed_status=(200,)):
        self.calls.append((method, resource, body))
        if method == "GET" and resource.endswith("/State"):
            return (200, json.dumps({"Status": 4, "StandbyState": self.standby}).encode())
        if method == "PUT" and body == {"DeviceAction": 2}:
            self.standby = 1
        return (200, b"")


def run_start(standby):
    raw = SleepyClient(standby)
    client = MieleLanClient(raw, ROUTE)  # type: ignore[arg-type]
    asyncio.run(client.start_program())
    return raw


def test_deep_standby_is_woken_before_starting():
    """An appliance asleep on the network will not act on anything.

    The two captures of the same dishwasher differ here — StandbyState 2 when
    the start was refused, 1 when it worked.
    """
    raw = run_start(2)
    wakes = [c for c in raw.calls if c[2] == {"DeviceAction": 2}]
    assert len(wakes) == 1
    assert raw.calls.index(wakes[0]) < next(
        i for i, c in enumerate(raw.calls) if c[1].startswith(dop2_resource())
    ), "the wake must precede the start, not follow it"


def test_going_to_standby_also_counts_as_asleep():
    assert [c for c in run_start(3).calls if c[2] == {"DeviceAction": 2}]


def test_an_awake_appliance_is_not_woken():
    """One read, no pointless write, on the common path."""
    for standby in (0, 1):
        assert not [c for c in run_start(standby).calls if c[2] == {"DeviceAction": 2}], standby


def test_a_failed_wake_does_not_abandon_the_start():
    """If the appliance is awake enough to act, let it — its own error is better."""
    client, raw = make_client({state_resource(): ResponseError(500, "no")})
    asyncio.run(client.start_program())
    assert [c for c in raw.calls if c[1].startswith(dop2_resource())], "start still attempted"
