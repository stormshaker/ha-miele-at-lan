"""Per-appliance IP overrides — pure helpers, no Home Assistant imports.

The runtime needs a fab→IP map to reach each appliance. It has three sources:

1. ``entry.data["static_ips"]`` — written once by the direct-keys setup path
   from whatever mDNS saw at config-flow time. Can go stale.
2. Live mDNS results at entry setup — what the appliance is announcing now.
3. ``entry.options["static_ips"]`` — set by the user in the options flow.

They are merged in that order, later sources winning. A user-set IP must beat
mDNS: it is the explicit fix for exactly the cases where mDNS is stale or
absent (e.g. appliances moved to a separate VLAN that multicast doesn't cross).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any

from .const import MieleAppliance

# RFC 1123 hostname: dot-separated labels of 1-63 alnum/hyphen chars, no
# leading/trailing hyphen, 253 chars total. A trailing dot (FQDN) is allowed.
_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def merge_static_ips(
    data_ips: Mapping[str, str] | None,
    mdns_ips: Mapping[str, str] | None,
    option_ips: Mapping[str, str] | None,
) -> dict[str, str]:
    """Effective fab→IP map: config data, then mDNS, then user options last.

    Empty/blank option values are ignored so a cleared field never masks a
    discovered address.
    """
    merged: dict[str, str] = {}
    merged.update(data_ips or {})
    merged.update(mdns_ips or {})
    merged.update({fab: ip for fab, ip in (option_ips or {}).items() if ip and ip.strip()})
    return merged


def normalise_host(value: str | None) -> str | None:
    """Return a cleaned host (IPv4/IPv6 literal or hostname), or None if invalid.

    An empty / whitespace-only value is returned as ``""`` (meaning "no
    override"), distinct from ``None`` (meaning "invalid").
    """
    if value is None:
        return ""
    host = value.strip()
    if not host:
        return ""
    # IPv6 may be pasted in URL brackets: [fe80::1]
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return str(ipaddress.ip_address(bare))
    except ValueError:
        pass
    # Anything that looks like a dotted-quad but isn't a valid IPv4 (e.g.
    # 192.168.3.300) is a typo, not a hostname.
    if re.fullmatch(r"[0-9.]+", host):
        return None
    name = host[:-1] if host.endswith(".") else host
    if not name or len(name) > 253:
        return None
    if all(_HOSTNAME_LABEL.match(label) for label in name.split(".")):
        return host.lower()
    return None


def appliance_label(device: Mapping[str, Any]) -> str:
    """Human label for an appliance in the options form: ``Name (fab)``.

    Falls back to the device-type name when the cloud gave no device name.
    """
    fab = str(device.get("fabNr") or "")
    name = str(device.get("deviceName") or "").strip()
    if not name:
        name = _device_type_name(device.get("deviceType"))
    return f"{name} ({fab})" if fab else name


def _device_type_name(device_type: Any) -> str:
    try:
        member = MieleAppliance(int(device_type))
    except (TypeError, ValueError):
        return "Miele appliance"
    if member is MieleAppliance.UNKNOWN:
        return "Miele appliance"
    return member.name.replace("_", " ").capitalize()
