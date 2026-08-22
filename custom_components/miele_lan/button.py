"""Miele@LAN buttons — only entries that don't have a more natural
on/off representation as a switch / light.

* `wake`         — devices with deep-standby. Sends DeviceAction:2 to
  pull the device back to a remote-controllable state.
* `stop_program` — oven-family. Sends DOP2 STOP (no heat risk; cleanly
  ends a running program).
* `start` / `stop` / `pause` — laundry + dishwasher. Sends the /State
  ProcessAction the Miele cloud API uses, with a DOP2 fallback. These
  mirror HA core `miele`'s buttons of the same key, so an automation can
  be pointed at the local entity without changing its shape.

Program control is gated twice, mirroring how HA core gates its own buttons
on the cloud's `/actions` permission list:

  * at setup, on the appliance reporting ``RemoteEnable`` at all;
  * at runtime, on remote control being enabled *and* the appliance being in
    a ``Status`` where the action is accepted (see `const.STARTABLE_STATUSES`
    and friends).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Collection

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import MieleLanClient
from .const import (
    DOMAIN,
    OPCODE_STOP,
    OVEN_FAMILY,
    PAUSABLE_STATUSES,
    REMOTE_CONTROLLABLE_FAMILY,
    REMOTE_ENABLE_FULL,
    STARTABLE_STATUSES,
    STOPPABLE_STATUSES,
    WAKEABLE_FAMILY,
    MieleAppliance,
    StateStatus,
)
from .coordinator import MieleLanCoordinator
from .entity import MieleLanEntity


def remote_control_enabled(state: dict[str, Any]) -> bool:
    """Is full remote control currently on for this appliance?

    ``RemoteEnable`` is a 3-element array; we test membership of 15 rather than
    reading index 0, because the ordering of the trailing SmartGrid /
    MobileStart flags is not firmly established and 15 only ever appears in the
    full-remote-control slot. Same test asyncmiele's `can_remote_start` makes.

    A firmware that doesn't report ``RemoteEnable`` at all never gets these
    buttons — that is filtered at setup — so an absent key here can only mean
    "state not populated yet", which the coordinator already treats as
    unavailable.
    """
    values = state.get("RemoteEnable")
    if not isinstance(values, (list, tuple)):
        return False
    return REMOTE_ENABLE_FULL in values


def action_permitted(state: dict[str, Any], statuses: Collection[StateStatus]) -> bool:
    """Would the appliance accept this ProcessAction in its current state?"""
    if not remote_control_enabled(state):
        return False
    status = state.get("Status")
    if not isinstance(status, int):
        # Reported RemoteEnable but not Status. No basis to judge, and hiding
        # the button would make the appliance uncontrollable — let the press
        # through and let the device answer.
        return True
    return status in statuses


def delay_start_pending(state: dict[str, Any]) -> bool:
    """Is the appliance counting down a delay start, rather than waiting on us?

    ``Status`` 4 (waiting_to_start) covers two different situations — armed for
    a remote start, and delay-start timer running — and /State has no flag that
    cleanly separates them. ``StartTime`` does: it reads [0, 0] unless a delay
    has been programmed.

    Measured 2026-08-22 on a WWI860 washer (XKM EK057 FW 08.37) sitting at
    Status 4 with ``StartTime: [10, 29]``: ``{"ProcessAction": 1}`` was
    *accepted* — HTTP 200, no Failure envelope, no fallback triggered — and the
    appliance ignored it completely, still Status 4 twenty seconds later. A
    countdown is not a release-me state, so start is not offered there.

    This is the concrete reason the buttons gate on more than the write
    succeeding: on this firmware a write can succeed and mean nothing.
    """
    start_time = state.get("StartTime")
    if not isinstance(start_time, (list, tuple)):
        return False
    return any(bool(value) for value in start_time)


def can_start(state: dict[str, Any]) -> bool:
    """Start is offered only when the appliance is actually waiting on us."""
    return action_permitted(state, STARTABLE_STATUSES) and not delay_start_pending(state)


@dataclass(frozen=True, kw_only=True)
class MieleLanButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[MieleLanClient], Awaitable[Any]]
    requires_dop2: bool = False
    # Skip the entity entirely when the appliance's /State lacks this key.
    required_state_key: str | None = None
    # Runtime availability, evaluated against /State. None = always available.
    available_fn: Callable[[dict[str, Any]], bool] | None = None


@dataclass(frozen=True, kw_only=True)
class MieleLanButtonDef:
    types: tuple[MieleAppliance, ...]
    description: MieleLanButtonDescription


BUTTONS: tuple[MieleLanButtonDef, ...] = (
    MieleLanButtonDef(
        types=WAKEABLE_FAMILY,
        description=MieleLanButtonDescription(
            key="wake",
            translation_key="wake",
            requires_dop2=False,
            press_fn=lambda c: c.wake(),
        ),
    ),
    MieleLanButtonDef(
        types=OVEN_FAMILY,
        description=MieleLanButtonDescription(
            key="stop_program",
            translation_key="stop_program",
            requires_dop2=True,
            press_fn=lambda c: c.write_user_request(OPCODE_STOP),
        ),
    ),
    # Program control — laundry + dishwasher. Keys match HA core `miele`'s
    # buttons so the entity IDs line up (`button.<name>_start`, …).
    #
    # `start` is disabled by default: it is the only button here that can set a
    # physical cycle going, and it should be a deliberate opt-in. `stop` and
    # `pause` can only ever halt a machine, so they carry no such risk and are
    # on by default. (HA core disables all three; it has no local-only users
    # for whom these are the *only* control path.)
    MieleLanButtonDef(
        types=REMOTE_CONTROLLABLE_FAMILY,
        description=MieleLanButtonDescription(
            key="start",
            translation_key="start",
            entity_registry_enabled_default=False,
            required_state_key="RemoteEnable",
            available_fn=can_start,
            press_fn=lambda c: c.start_program(),
        ),
    ),
    MieleLanButtonDef(
        types=REMOTE_CONTROLLABLE_FAMILY,
        description=MieleLanButtonDescription(
            key="stop",
            translation_key="stop",
            required_state_key="RemoteEnable",
            available_fn=lambda s: action_permitted(s, STOPPABLE_STATUSES),
            press_fn=lambda c: c.stop_program(),
        ),
    ),
    MieleLanButtonDef(
        types=REMOTE_CONTROLLABLE_FAMILY,
        description=MieleLanButtonDescription(
            key="pause",
            translation_key="pause",
            required_state_key="RemoteEnable",
            available_fn=lambda s: action_permitted(s, PAUSABLE_STATUSES),
            press_fn=lambda c: c.pause_program(),
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, MieleLanCoordinator] = bundle["coordinators"]
    entities = []
    for coord in coordinators.values():
        dt = coord.device_type
        state = coord.data.state if coord.data else {}
        for d in BUTTONS:
            if dt not in d.types:
                continue
            if d.description.requires_dop2 and not coord.dop2_supported:
                continue
            rsk = d.description.required_state_key
            if rsk is not None and rsk not in state:
                continue
            entities.append(MieleLanButton(coord, d.description))
    async_add_entities(entities)


class MieleLanButton(MieleLanEntity, ButtonEntity):
    entity_description: MieleLanButtonDescription

    def __init__(
        self,
        coordinator: MieleLanCoordinator,
        description: MieleLanButtonDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        available_fn = self.entity_description.available_fn
        if available_fn is None:
            return True
        data = self.coordinator.data
        if data is None:
            return False
        return available_fn(data.state)

    async def async_press(self) -> None:
        # HA lets a service call reach an unavailable entity, so re-check here
        # rather than relying on `available` alone. Without this, a start press
        # against an unarmed appliance would surface as a bare HTTP error and
        # look like a protocol failure rather than the interlock it is.
        self._raise_if_not_permitted()
        await self.entity_description.press_fn(self.coordinator.client)
        await self.coordinator.async_request_refresh()

    def _raise_if_not_permitted(self) -> None:
        available_fn = self.entity_description.available_fn
        data = self.coordinator.data
        if available_fn is None or data is None or available_fn(data.state):
            return
        action = self.entity_description.key
        if not remote_control_enabled(data.state):
            raise HomeAssistantError(
                f"Cannot {action}: remote control is off on the appliance. "
                "Enable 'Mobile controllable' / MobileStart in its settings menu."
            )
        if action == "start" and delay_start_pending(data.state):
            hours, minutes = (list(data.state["StartTime"]) + [0, 0])[:2]
            raise HomeAssistantError(
                f"Cannot start: the appliance is counting down a delay start "
                f"({hours}h{minutes:02d}m to go) and ignores a remote start "
                "while it does. Cancel the delay on the appliance first."
            )
        status = data.state.get("Status")
        try:
            status_name = StateStatus(status).name.lower()
        except ValueError:
            status_name = str(status)
        hint = (
            " Select a program and press Start on the appliance itself — it then"
            " waits for the remote start."
            if action == "start"
            else ""
        )
        raise HomeAssistantError(
            f"Cannot {action}: the appliance is '{status_name}'.{hint}"
        )
