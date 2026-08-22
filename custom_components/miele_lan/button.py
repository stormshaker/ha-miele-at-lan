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

import asyncio
import logging
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
    REMOTE_ENABLE_MOBILE_START_INDEX,
    STARTABLE_STATUSES,
    STOPPABLE_STATUSES,
    WAKEABLE_FAMILY,
    MieleAppliance,
    StateStatus,
)
from .coordinator import MieleLanCoordinator
from .entity import MieleLanEntity

_LOGGER = logging.getLogger(__name__)

# How long to wait for a program-control write to show up in Status before
# calling it ignored. A measured stop moved the appliance within ~2.5s; ten
# seconds is generous without making a button press feel hung.
VERIFY_TIMEOUT = 10.0
VERIFY_INTERVAL = 1.5


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


def _status_name(status: Any) -> str:
    try:
        return StateStatus(status).name.lower()
    except ValueError:
        return str(status)


def mobile_start_armed(state: dict[str, Any]) -> bool:
    """Is the appliance armed to be started remotely, right now?

    ``RemoteEnable[2]`` is the local copy of the cloud's
    `remoteEnable.mobileStart`, which is what actually predicts whether a remote
    start does anything. See `const.REMOTE_ENABLE_MOBILE_START_INDEX` for the
    simultaneous cloud/LAN sampling that established the mapping.

    Absent or short array → unknown, and we do not block on unknowns: a firmware
    that reports a shorter RemoteEnable should still be controllable.
    """
    values = state.get("RemoteEnable")
    if not isinstance(values, (list, tuple)):
        return True
    if len(values) <= REMOTE_ENABLE_MOBILE_START_INDEX:
        return True
    return bool(values[REMOTE_ENABLE_MOBILE_START_INDEX])


def delay_start_pending(state: dict[str, Any]) -> bool:
    """Is the appliance counting down a delay start?

    Not a gate — :func:`mobile_start_armed` is. This exists so the error message
    can say *why* an appliance at Status 4 is not startable, which is otherwise
    the single most confusing case.

    Measured 2026-08-22 on a WWI860 washer (XKM EK057 FW 08.37) sitting at
    Status 4 with ``StartTime: [10, 29]`` and ``RemoteEnable: [15, 0, 0]``:
    ``{"ProcessAction": 1}`` was *accepted* — HTTP 200, no Failure envelope, no
    fallback triggered — and the appliance ignored it completely, still Status 4
    twenty seconds later. On this firmware a write can succeed and mean nothing.
    """
    start_time = state.get("StartTime")
    if not isinstance(start_time, (list, tuple)):
        return False
    return any(bool(value) for value in start_time)


def can_start(state: dict[str, Any]) -> bool:
    """Start is offered only when the appliance is actually waiting on us.

    Both halves are load-bearing. Status alone is not enough — WAITING_TO_START
    also covers a delay-start countdown. The mobileStart bit alone is not enough
    either, since it stays set through a running cycle.
    """
    return action_permitted(state, STARTABLE_STATUSES) and mobile_start_armed(state)


@dataclass(frozen=True, kw_only=True)
class MieleLanButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[MieleLanClient], Awaitable[Any]]
    requires_dop2: bool = False
    # Skip the entity entirely when the appliance's /State lacks this key.
    required_state_key: str | None = None
    # Runtime availability, evaluated against /State. None = always available.
    available_fn: Callable[[dict[str, Any]], bool] | None = None
    # Confirm the appliance actually acted, rather than trusting its ack.
    verify_status_change: bool = False


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
            verify_status_change=True,
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
            verify_status_change=True,
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
            verify_status_change=True,
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
        data = self.coordinator.data
        before = data.state.get("Status") if data else None
        await self.entity_description.press_fn(self.coordinator.client)
        if self.entity_description.verify_status_change:
            await self._raise_if_ignored(before)
        await self.coordinator.async_request_refresh()

    async def _raise_if_ignored(self, before: Any) -> None:
        """Confirm the appliance acted, rather than taking its word for it.

        These appliances answer an accepted write ``200 [{"Success": ...}]``
        whether or not they intend to do anything, so the acknowledgement is not
        evidence. Measured three times on two appliances: a start written to a
        machine on a delay-start countdown, and a pause written to a dryer whose
        model does not support remote pause, were all acknowledged and ignored.

        Status moving is the only reliable confirmation. Without this check a
        solar automation pressing start would log a success and never run the
        cycle — silent failure being the worst outcome available here.
        """
        if not isinstance(before, int):
            return  # nothing to compare against; do not invent a failure
        deadline = asyncio.get_running_loop().time() + VERIFY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(VERIFY_INTERVAL)
            try:
                state = await self.coordinator.client.read_state_dict()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("[%s] could not verify press: %s", self.entity_id, err)
                return  # unreachable appliance is a different problem
            if state.get("Status") != before:
                return
        action = self.entity_description.key
        raise HomeAssistantError(
            f"The appliance accepted '{action}' but did not act on it — its "
            f"status is still '{_status_name(before)}'. Some models do not "
            f"support {action} over the network even though they acknowledge "
            "the command."
        )

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
        if action == "start" and not mobile_start_armed(data.state):
            if delay_start_pending(data.state):
                hours, minutes = (list(data.state["StartTime"]) + [0, 0])[:2]
                raise HomeAssistantError(
                    f"Cannot start: the appliance is counting down a delay start "
                    f"({hours}h{minutes:02d}m to go), so it is not armed for a "
                    "remote start. Cancel the delay on the appliance first."
                )
            raise HomeAssistantError(
                "Cannot start: the appliance is not armed for a remote start. "
                "Select a program and press Start on the panel with "
                "'Mobile controllable' / MobileStart enabled — it then waits "
                "for the remote start instead of running."
            )
        status_name = _status_name(data.state.get("Status"))
        hint = (
            " Select a program and press Start on the appliance itself — it then"
            " waits for the remote start."
            if action == "start"
            else ""
        )
        raise HomeAssistantError(
            f"Cannot {action}: the appliance is '{status_name}'.{hint}"
        )
