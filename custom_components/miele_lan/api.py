"""Thin facade over asyncmiele — fixes the bugs that broke writes on real devices.

What this module owns
---------------------
1. The MieleH256 PUT signing: device computes HMAC over the **padded** body, but
   upstream asyncmiele <=0.2.6 signs over the unpadded body and only pads at
   encrypt time → 403. We sign the padded body.
2. Action responses come back as JSON lists (e.g. ``[{"Success":{"DeviceAction":0}}]``).
   asyncmiele expects dicts → pydantic error. We unwrap when possible.
3. GLOBAL_USER_REQ is **not** the /State ``UserRequest`` field (washer-only,
   12141/12142). It's a binary write to DOP2 leaf ``2/1583``. We provide it here.

Everything else (crypto helpers, MieleClient class, response decryption) is
reused from asyncmiele.
"""

from __future__ import annotations

import asyncio
import binascii
import json
import logging
from typing import Any

import aiohttp

from homeassistant.exceptions import HomeAssistantError

from asyncmiele import MieleClient
from asyncmiele.api import client as _client_mod
from asyncmiele.exceptions.network import (
    NetworkConnectionError,
    NetworkTimeoutError,
    ResponseError,
)
from asyncmiele.utils import crypto as _crypto
from asyncmiele.utils.http_consts import (
    ACCEPT_HEADER,
    CONTENT_TYPE_JSON,
    USER_AGENT,
)

from .const import (
    DEVICE_ACTION_WAKE,
    SLEEPING_STANDBY_STATES,
    OPCODE_LIGHT_OFF,
    OPCODE_LIGHT_ON,
    OPCODE_PAUSE,
    OPCODE_START,
    OPCODE_STOP,
    OPCODE_SWITCH_OFF,
    OPCODE_SWITCH_ON,
    PROCESS_ACTION_PAUSE,
    PROCESS_ACTION_START,
    PROCESS_ACTION_STOP,
    USER_REQUEST_LEAF,
    USER_REQUEST_UNIT,
)

_LOGGER = logging.getLogger(__name__)


# 16-byte prefix for DOP2 leaf 2/1583 writes. Decoded:
#   0010 (len=16)  0001 (struct id?)  062f (leaf=1583)  0000 (idx)
#   00 00 00 01 00 01 07 00  (struct header — field 1, type 0x07/e16)
_OVEN_REQ_PREFIX = bytes.fromhex("00100001062f00000000000100010700")
_OVEN_REQ_SUFFIX = b"\x20" * 15  # padding


def _pad_request_body(raw: bytes, blocksize: int = 16, json_min: int = 64) -> bytes:
    """Padding that satisfies both the MieleRESTServer convention and AES-CBC.

    asyncmiele's ``pad_payload`` returns JSON bodies >= 64 chars unchanged,
    which then blow up AES-CBC for non-16-aligned lengths. We always pad to
    the next 16-byte boundary, and additionally keep the ``>= 64-byte``
    minimum for JSON so the device behaves identically to the reference.
    """
    if not raw:
        return raw
    target = max(json_min if raw[:1] == b"{" and raw[-1:] == b"}" else 0, len(raw))
    if target % blocksize:
        target = target + (blocksize - (target % blocksize))
    if raw[:1] == b"{" and raw[-1:] == b"}":
        spaces = target - len(raw)
        if spaces > 0:
            return raw[:-1] + b" " * spaces + b"}"
        return raw
    if target == len(raw):
        return raw
    return raw + b"\x20" * (target - len(raw))


# ProcessAction → the DOP2 GLOBAL_USER_REQ opcode that means the same thing.
# Used only as a fallback when the appliance refuses the /State write — see
# MieleLanClient.write_process_action for why both paths are needed.
_PROCESS_ACTION_FALLBACK_OPCODE: dict[int, int] = {
    PROCESS_ACTION_START: OPCODE_START,
    PROCESS_ACTION_STOP: OPCODE_STOP,
    PROCESS_ACTION_PAUSE: OPCODE_PAUSE,
}


def action_was_refused(raw: bytes) -> bool:
    """Did the appliance answer a /State write with a ``Failure`` envelope?

    The XKM answers an accepted write ``200 [{"Success":{"<Key>":<n>}}]`` and a
    refused or unrecognised one ``200 [{"Failure":{"<Key>":null}}]``. The HTTP
    status does not carry the difference — a refusal is a 200. Measured on a
    G7369 (XKM EK057 FW 08.37) by writing a deliberately bogus key next to a
    real one, 2026-08-22:

        {"ProcessAction": 3}  → 200  [{"Success":{"ProcessAction":0}}]
        {"NotAField": 1}      → 200  [{"Failure":{"NotAField":null}}]

    Note what ``Success`` does and does not mean. That probe ran against an
    appliance that was switched off, with no cycle to pause — the firmware
    acknowledged a write it could not act on. ``Success`` means the field was
    accepted, not that the appliance did anything. Whether the action actually
    took effect can only be read back from ``Status``.
    """
    if not raw:
        return False
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return False
    items = parsed if isinstance(parsed, list) else [parsed]
    return any(isinstance(item, dict) and "Failure" in item for item in items)


def build_user_request_payload(opcode: int) -> bytes:
    """Build the 32-byte DOP2 write payload for a GLOBAL_USER_REQ opcode."""
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode out of range: {opcode}")
    return _OVEN_REQ_PREFIX + bytes([opcode]) + _OVEN_REQ_SUFFIX


async def _patched_request_bytes(
    self: MieleClient,
    method: str,
    resource: str,
    *,
    body: bytes | str | dict[str, Any] | None = None,
    allowed_status: tuple[int, ...] = (200,),
) -> tuple[int, bytes]:
    """Drop-in replacement for MieleClient._request_bytes that signs the padded body.

    Returns (status_code, decrypted_bytes_or_raw).
    """
    method = method.upper()

    if body is None:
        raw_body: bytes = b""
    elif isinstance(body, bytes):
        raw_body = body
    elif isinstance(body, str):
        raw_body = body.encode("utf-8")
    else:
        raw_body = json.dumps(body).encode("utf-8")

    date_str = self._get_date_str()
    # Methods that carry a request body: PUT and POST. The official Miele cloud
    # uses POST with the same Content-Type + encrypted-body protocol as PUT
    # (verified via APK decompile of ISubscriptionApi).
    body_method = method in ("PUT", "POST")
    content_type_header = CONTENT_TYPE_JSON if body_method else ""

    if body_method and raw_body:
        signed_body = _pad_request_body(raw_body)
    else:
        signed_body = raw_body

    auth_header, iv = _crypto.build_auth_header(
        method=method,
        host=self.host,
        resource=resource,
        date=date_str,
        group_id=self.group_id,
        group_key=self.group_key,
        content_type_header=content_type_header,
        body=signed_body,
    )

    if body_method and signed_body:
        aes_key = self.group_key[: len(self.group_key) // 2]
        data_to_send: bytes | None = _crypto.encrypt_payload(signed_body, aes_key, iv)
    else:
        data_to_send = None

    headers = {
        "Accept": ACCEPT_HEADER,
        "User-Agent": USER_AGENT,
        "Host": self.host,
        "Date": date_str,
        "Authorization": auth_header,
    }
    if content_type_header:
        headers["Content-Type"] = content_type_header

    url = f"http://{self.host}{resource}"
    session = await self._get_session()

    try:
        async with session.request(
            method, url, data=data_to_send, headers=headers, timeout=self.timeout
        ) as resp:
            raw = await resp.read()
            status = resp.status

            if status not in allowed_status:
                raise ResponseError(status, f"API error for {resource}")
            if status == 204 or not raw:
                return status, b""

            sig_header = resp.headers.get("X-Signature")
            if not sig_header:
                return status, raw  # unencrypted error/text body

            sig_hex = sig_header.split(":", 1)[1]
            if len(sig_hex) % 2:
                sig_hex = "0" + sig_hex
            sig_bytes = binascii.a2b_hex(sig_hex)
            return status, _crypto.decrypt_response(raw, sig_bytes, self.group_key)
    except asyncio.TimeoutError as exc:
        raise NetworkTimeoutError(str(exc)) from exc
    except aiohttp.ClientConnectorError as exc:
        raise NetworkConnectionError(str(exc)) from exc


def _install_patches() -> None:
    """Monkey-patch asyncmiele's MieleClient with our PUT-signing fix.

    Idempotent — safe to call repeatedly.
    """
    if getattr(_client_mod.MieleClient._request_bytes, "_miele_lan_patched", False):
        return
    _patched_request_bytes._miele_lan_patched = True  # type: ignore[attr-defined]
    _client_mod.MieleClient._request_bytes = _patched_request_bytes  # type: ignore[assignment]


class MieleLanClient:
    """High-level local client for one provisioned Miele appliance.

    Wraps :class:`asyncmiele.MieleClient`. Use as an async context manager.

    >>> async with MieleLanClient.from_hex(host, group_id_hex, group_key_hex, route) as c:
    ...     state = await c.get_state()
    """

    def __init__(self, client: MieleClient, route: str) -> None:
        _install_patches()
        self._client = client
        self._route = route

    @classmethod
    def from_hex(
        cls,
        host: str,
        group_id_hex: str,
        group_key_hex: str,
        route: str,
        *,
        timeout: float = 10.0,
    ) -> "MieleLanClient":
        """Build a client from hex credentials and a known device route."""
        _install_patches()
        client = MieleClient.from_hex(host, group_id_hex, group_key_hex, timeout=timeout)
        return cls(client, route)

    async def __aenter__(self) -> "MieleLanClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.__aexit__(*exc_info)

    @property
    def route(self) -> str:
        return self._route

    @property
    def raw(self) -> MieleClient:
        """Escape hatch to the underlying asyncmiele client for unwrapped calls."""
        return self._client

    # --- reads ---------------------------------------------------------------

    async def get_devices(self) -> dict[str, Any]:
        return await self._client.get_devices()

    async def get_ident(self) -> Any:
        return await self._client.get_device_ident(self._route)

    async def get_state(self) -> Any:
        return await self._client.get_device_state(self._route)

    async def read_state_dict(self) -> dict[str, Any]:
        """GET /State as a plain dict, bypassing asyncmiele's model layer.

        Used to confirm an action actually took effect — see
        `button.py`'s press verification, and :func:`action_was_refused` for why
        the write's own acknowledgement cannot be trusted for that.
        """
        _, raw = await self._client._request_bytes(
            "GET", f"/Devices/{self._route}/State", allowed_status=(200,)
        )
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else {}

    async def read_dop2(self, unit: int, attr: int, idx1: int = 0, idx2: int = 0) -> bytes:
        """Raw DOP2 leaf read. Returns the decrypted body verbatim."""
        resource = f"/Devices/{self._route}/DOP2/{unit}/{attr}?idx1={idx1}&idx2={idx2}"
        status, raw = await self._client._request_bytes("GET", resource, allowed_status=(200,))
        if status != 200:
            raise ResponseError(status, f"DOP2 read {unit}/{attr} returned {status}")
        return raw

    # --- writes --------------------------------------------------------------

    async def _put_state(self, body: dict[str, Any]) -> dict[str, Any]:
        """PUT /State and parse the (list-wrapped) response.

        The device rejects no-op writes (writing the current value) with HTTP 400.
        We treat that as success — the caller's intent is satisfied.
        """
        try:
            status, raw = await self._client._request_bytes(
                "PUT",
                f"/Devices/{self._route}/State",
                body=body,
                allowed_status=(200, 204),
            )
        except ResponseError as e:
            if "HTTP error 400" in str(e):
                _LOGGER.debug("/State write %r returned 400 (likely no-op)", body)
                return {}
            raise
        if status == 204 or not raw:
            return {}
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        if isinstance(parsed, list):
            merged: dict[str, Any] = {}
            for item in parsed:
                if isinstance(item, dict):
                    merged.update(item)
            return merged
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    async def wake(self) -> dict[str, Any]:
        """Wake the appliance from sleep. Returns the device's action ack."""
        return await self._put_state({"DeviceAction": DEVICE_ACTION_WAKE})

    async def write_user_request(self, opcode: int) -> None:
        """Send a GLOBAL_USER_REQ opcode via DOP2 leaf 2/1583.

        Works across oven, laundry, and dishwasher device classes.
        The front-panel "Mobile controllable" setting must be on, else the device
        returns HTTP 500. Some firmwares (e.g. EK057 FW 08.32) block all DOP2
        writes unconditionally and return HTTP 404.

        The two statuses mean genuinely different things, and it is worth not
        conflating them. A 404 means the subsystem is absent: an EK057 FW 08.37
        answered 404 for twelve leaves across four units and does not list DOP2
        in its own resource index at all. A 500 means present but refusing right
        now — an EK037 answered 200 for this leaf while armed and 500 for every
        leaf once idle, so the same appliance gives both answers depending only
        on its state.
        """
        payload = build_user_request_payload(opcode)
        resource = (
            f"/Devices/{self._route}/DOP2/"
            f"{USER_REQUEST_UNIT}/{USER_REQUEST_LEAF}?idx1=0&idx2=0"
        )
        try:
            await self._client._request_bytes(
                "PUT", resource, body=payload, allowed_status=(200, 204)
            )
        except ResponseError as exc:
            status = exc.status_code
            if status == 404:
                raise HomeAssistantError(
                    "This appliance's firmware does not accept remote commands "
                    "over the local API (DOP2 writes are blocked on this hardware/firmware)."
                ) from exc
            if status == 500:
                raise HomeAssistantError(
                    "Remote control was refused. Enable 'Remote control' / "
                    "'Mobile controllable' in the appliance's settings menu and try again."
                ) from exc
            raise HomeAssistantError(
                f"Remote command failed (HTTP {status})."
            ) from exc

    async def write_process_action(self, action: int) -> None:
        """Start / stop / pause the running program via /State ``ProcessAction``.

        Primary path is ``PUT /Devices/{fab}/State {"ProcessAction": n}``, the
        same endpoint and signing this integration already uses successfully for
        ``DeviceAction`` (wake) and ``Light``. Provenance for the field itself:

          * The local /State *reports* ``ProcessAction`` on every cycle
            appliance, using the cloud API's numbering (1 start, 2 stop,
            3 pause) — see `sensor.py`'s ``process_action`` diagnostic.
          * asyncmiele 0.2.6 writes it exactly this way
            (``MieleClient.remote_start`` / ``stop_program`` / ``pause_program``),
            citing akappner/MieleRESTServer as its reference implementation.
          * Confirmed directly against a G7369 (XKM EK057 FW 08.37) on
            2026-08-22: an inert ``{"ProcessAction": 3}`` written while the
            appliance was switched off answered
            ``200 [{"Success":{"ProcessAction":0}}]``, where an unrecognised
            key on the same endpoint answers ``Failure``. See
            :func:`action_was_refused`.

        Fallback path is the DOP2 GLOBAL_USER_REQ opcode (leaf 2/1583) that this
        integration already uses for light and power. Both are needed because
        firmware coverage of the two does not overlap cleanly. Measured on one
        household, 2026-08-22, reading leaf 2/1583:

            G7369  dishwasher  XKM EK057  FW 08.37  → HTTP 404
            WWI860 washer      XKM EK057  FW 08.37  → HTTP 404
            TWL780WP dryer     XKM EK037  FW 04.05  → HTTP 200

        …while the author's H7560BP oven takes DOP2 writes and is the reason
        the opcode path exists at all. On the two EK057 appliances DOP2 is not
        an option, so /State has to come first.

        We fall back only when the appliance answered and declined — either an
        HTTP status, or a ``Failure`` envelope inside a 200. A timeout or
        connection error is deliberately *not* retried: we cannot know whether
        the write landed, and a blind retry of START could begin a second cycle.
        """
        opcode = _PROCESS_ACTION_FALLBACK_OPCODE.get(action)
        if opcode is None:
            raise ValueError(f"unsupported ProcessAction: {action}")

        try:
            _, raw = await self._client._request_bytes(
                "PUT",
                f"/Devices/{self._route}/State",
                body={"ProcessAction": action},
                allowed_status=(200, 204),
            )
            if not action_was_refused(raw):
                return
            refusal = "a Failure response"
        except ResponseError as exc:
            refusal = f"HTTP {exc.status_code}"

        _LOGGER.debug(
            "/State ProcessAction=%d refused with %s — "
            "retrying as DOP2 GLOBAL_USER_REQ opcode 0x%02x",
            action, refusal, opcode,
        )
        try:
            await self.write_user_request(opcode)
        except HomeAssistantError as exc:
            raise HomeAssistantError(
                f"{exc} The appliance also refused ProcessAction {action} on "
                f"/State with {refusal}."
            ) from exc

    async def start_program(self) -> None:
        """Start the queued program — or resume a paused one.

        **DOP2 first, deliberately, and this is the opposite of stop and pause.**

        Measured 2026-08-22/23 on one household. ``{"ProcessAction": 1}`` was
        acknowledged ``200 [{"Success":{"ProcessAction":0}}]`` and ignored on
        every appliance and in every state tried — four times, including on a
        dishwasher that was fully armed and that the cloud started seconds
        later. The DOP2 GLOBAL_USER_REQ opcode moved a TWL780WP from
        WAITING_TO_START to IN_USE **in three seconds**, cutting a SmartStart
        timer short exactly as MieleRESTServer's README describes.

        Note what that implies for the ordering used elsewhere: /State answers
        Success whether or not it intends to act, so a /State-first start would
        never reach the path that actually works. Stop and pause keep /State
        first because /State demonstrably performs those.

        The fallback to /State is retained for firmware where DOP2 is blocked
        but ProcessAction is honoured. No such device has been observed, but the
        cost of trying is one refused request.
        """
        await self.wake_if_sleeping()
        try:
            await self.write_user_request(OPCODE_START)
            return
        except HomeAssistantError as exc:
            _LOGGER.debug(
                "DOP2 start refused (%s) — falling back to /State ProcessAction", exc
            )
        await self._write_process_action_via_state(PROCESS_ACTION_START)

    async def wake_if_sleeping(self) -> bool:
        """Pull the appliance out of deep standby before commanding it.

        A cached ``StandbyState`` is no use here — the point is that the
        appliance may have gone to sleep since the last poll — so this reads
        fresh, and only writes when there is something to wake.

        Returns whether a wake was sent, so callers can say so in an error.

        Never raises. Failing to wake is not itself a reason to abandon the
        command: if the appliance is reachable enough to act, it may well act
        anyway, and if it is not, the command's own error is the better one to
        show.
        """
        try:
            state = await self.read_state_dict()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("could not read StandbyState before command: %s", err)
            return False
        if state.get("StandbyState") not in SLEEPING_STANDBY_STATES:
            return False
        _LOGGER.debug(
            "appliance is in standby (StandbyState=%s) — waking before command",
            state.get("StandbyState"),
        )
        try:
            await self.wake()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("wake failed: %s", err)
            return False
        # Coming out of deep standby is not instant; give it a moment rather
        # than writing into an appliance that is still getting up.
        for _ in range(6):
            await asyncio.sleep(1.0)
            try:
                state = await self.read_state_dict()
            except Exception:  # noqa: BLE001
                break
            if state.get("StandbyState") not in SLEEPING_STANDBY_STATES:
                break
        return True

    async def _write_process_action_via_state(self, action: int) -> None:
        """PUT /State {"ProcessAction": n}, raising on a refusal."""
        try:
            _, raw = await self._client._request_bytes(
                "PUT",
                f"/Devices/{self._route}/State",
                body={"ProcessAction": action},
                allowed_status=(200, 204),
            )
        except ResponseError as exc:
            raise HomeAssistantError(
                f"Remote command failed (HTTP {exc.status_code})."
            ) from exc
        if action_was_refused(raw):
            raise HomeAssistantError(
                f"The appliance refused ProcessAction {action} on /State."
            )

    async def stop_program(self) -> None:
        """Cancel the running or pending program."""
        await self.write_process_action(PROCESS_ACTION_STOP)

    async def pause_program(self) -> None:
        """Pause the running program. Resume with :meth:`start_program`."""
        await self.write_process_action(PROCESS_ACTION_PAUSE)

    # --- light / power convenience ------------------------------------------

    async def light_on(self) -> None:
        """Turn interior light on via the clean /State JSON API (no DOP2 needed)."""
        await self._put_state({"Light": 1})

    async def light_off(self) -> None:
        """Turn interior light off via the clean /State JSON API."""
        await self._put_state({"Light": 2})

    async def switch_on(self) -> None:
        await self.write_user_request(OPCODE_SWITCH_ON)

    async def switch_off(self) -> None:
        await self.write_user_request(OPCODE_SWITCH_OFF)

    # Cooling-family target-temperature writes are not supported via the LAN
    # protocol — see custom_components/miele_lan/climate.py for the RE notes.
    # The climate entity is read-only by design.
