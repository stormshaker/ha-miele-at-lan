"""Miele@LAN — constants."""

from __future__ import annotations

from enum import IntEnum

DOMAIN = "miele_lan"
DEFAULT_NAME = "Miele@LAN"
DEFAULT_TIMEOUT = 10.0
DEFAULT_POLL_INTERVAL = 30  # seconds — fallback when state is unknown
ACTIVE_POLL_INTERVAL = 1   # seconds — when oven is running / state is "interesting"
IDLE_POLL_INTERVAL = 30    # seconds — when oven is Off
# Status values that indicate "interesting" — keep polling fast.
# 1 = Off, 2 = StandBy, 3 = Programmed, 4 = WaitingToStart, 5 = Running,
# 6 = Paused, 7 = EndProgrammed, 8 = Failure, 9 = Programming.
ACTIVE_STATUSES = {3, 4, 5, 6, 7, 9}

# Status values where program/phase/time fields are stale and should be hidden
# from the user. Mirrors the official Miele app's gating (RE'd from
# Oven.UI.1.dll:45992 BuildStateFor(OvenState) and
# LaundryCareMappi.UI.dll:38047 GetActionBarLeftText): the app's action bar
# only shows ProgramPhase when state == Running (5). We extend this to also
# hide for Off / On / Service / Default / Locked / NotConnected — the firmware
# keeps the last ProgramID/ProgramPhase cached until the next cycle starts.
IDLE_STATUSES = {1, 2, 8, 12, 144, 145, 255}

CONF_GROUP_ID = "group_id"
CONF_GROUP_KEY = "group_key"
CONF_ROUTE = "route"

# Cloud-pair config-entry keys.
CONF_COUNTRY = "country"
CONF_REGION = "region"              # EU / AS / EU2 (sales-company region)
CONF_REFRESH_TOKEN = "refresh_token"
CONF_DEVICES = "devices"            # list[{fabNr, deviceType, deviceName}] from cloud
CONF_HA_FAB = "ha_fab"              # our synthetic fab number
CONF_HA_PORT = "ha_port"            # which unprivileged port our listener uses
CONF_STATIC_IPS = "static_ips"      # optional dict fab→ip for users whose mDNS is flaky

CONF_FLOW_KIND = "flow_kind"        # "cloud" vs "manual" (single-device legacy path)
DEFAULT_HA_PUSH_PORT = 18082

# /State action keys.
DEVICE_ACTION_WAKE = 2

# /State ``ProcessAction`` values. Mirror of HA core `miele.const.MieleActions`,
# which mirrors the Miele cloud API's `processAction`. The local XKM accepts the
# same numbering on ``PUT /Devices/{fab}/State`` — see
# `api.py::write_process_action` for the provenance of that write path.
# 1 doubles as *resume*: the cloud API has no separate resume action, and
# asyncmiele's `resume_program()` sends 1 as well.
PROCESS_ACTION_START = 1
PROCESS_ACTION_STOP = 2
PROCESS_ACTION_PAUSE = 3

# ``RemoteEnable`` is /State's 3-element mirror of the cloud API's
# `remoteEnable` object: [fullRemoteControl, smartGrid, mobileStart].
#
# Index 0 carries the full-remote-control code — 15 "full", 7 "enabled but not
# possible", 0 "disabled". It tracks whether remote control is available at all
# and drops to 7 during transitions (cycle start, pause, program end).
#
# Index 2 is the one that decides whether a remote *start* will do anything. It
# is the same bit HA core surfaces as `binary_sensor.<name>_mobile_start` from
# the cloud. Established 2026-08-22 by sampling both sides simultaneously on one
# household:
#
#     appliance / state              RemoteEnable    cloud mobileStart
#     dishwasher, mid-cycle          [15, 0, 1]      on
#     dishwasher, off                [15, 0, 0]      off
#     washer, delay-start counting   [15, 0, 0]      off
#     dryer, idle                    [15, 0, 0]      off
#
# and corroborated against the cloud's own history, where mobileStart turned on
# at the instant the dishwasher entered WAITING_TO_START and off at the instant
# it went IN_USE — the arming window exactly.
#
# Index 1 is smartGrid, confirmed causally rather than inferred: enabling the
# SmartGrid setting on a TWL780WP's own panel flipped it 0 -> 1 immediately,
# with nothing else changed. It reads 1 on a dishwasher queued with a
# SmartStart delay and 0 on a washer queued with a plain delay timer.
REMOTE_ENABLE_FULL = 15
REMOTE_ENABLE_MOBILE_START_INDEX = 2

# /State ``ProgramType`` — 0 normal, 1 own programme, 2 automatic, 3 care.
#
# Recorded here only because it is a tempting dead end. MieleRESTServer gates
# "remote start enabled" on ProgramType == 2, but the write that actually
# started an appliance here did so at ProgramType 3, so that test would have
# refused it. The cloud integration also reported `automatic_program` for the
# same appliance at the same instant the LAN reported 3, so the two sources do
# not even agree on this field. Not used as a gate.

# DOP2 GLOBAL_USER_REQ leaf — universal across oven, laundry, dishwasher.
USER_REQUEST_UNIT = 2
USER_REQUEST_LEAF = 1583

# Opcode → label.  Sourced from MieleDop2Structures.UserRequestOven (akappner/MieleRESTServer)
# and confirmed alive on H7560BP via DOP2 writes (light, switch on/off).
OPCODE_NOP = 0x00
OPCODE_START = 0x01
OPCODE_STOP = 0x02
OPCODE_PAUSE = 0x03
OPCODE_START_DELAY = 0x08
OPCODE_DOOR_OPEN = 0x0B
OPCODE_DOOR_CLOSE = 0x0C
OPCODE_LIGHT_ON = 0x0D
OPCODE_LIGHT_OFF = 0x0E
OPCODE_FACTORY_RESET = 0x0F
OPCODE_SWITCH_ON = 0x10
OPCODE_NEXT = 0x11
OPCODE_BACK = 0x12
OPCODE_SWITCH_OFF = 0x13
OPCODE_KEEPALIVE = 0x15
OPCODE_PROGRAM_STOP = 0x36
OPCODE_PROGRAM_ABORT = 0x37
OPCODE_PROGRAM_FINALIZE = 0x38

# ---------------------------------------------------------------------------
# Appliance taxonomy
# ---------------------------------------------------------------------------
# Mirror of HA core `miele.const.MieleAppliance`, which itself mirrors the
# Miele API enum. We use this to filter per-entity availability — not every
# entity makes sense for every device type.


class MieleAppliance(IntEnum):
    """Miele device-type enum (matches the cloud's `deviceType` field)."""

    WASHING_MACHINE = 1
    TUMBLE_DRYER = 2
    WASHING_MACHINE_SEMI_PROFESSIONAL = 3
    TUMBLE_DRYER_SEMI_PROFESSIONAL = 4
    WASHING_MACHINE_PROFESSIONAL = 5
    DRYER_PROFESSIONAL = 6
    DISHWASHER = 7
    DISHWASHER_SEMI_PROFESSIONAL = 8
    DISHWASHER_PROFESSIONAL = 9
    OVEN = 12
    OVEN_MICROWAVE = 13
    HOB_HIGHLIGHT = 14
    STEAM_OVEN = 15
    MICROWAVE = 16
    COFFEE_SYSTEM = 17
    HOOD = 18
    FRIDGE = 19
    FREEZER = 20
    FRIDGE_FREEZER = 21
    ROBOT_VACUUM_CLEANER = 23
    WASHER_DRYER = 24
    DISH_WARMER = 25
    HOB_INDUCTION = 27
    STEAM_OVEN_COMBI = 31
    WINE_CABINET = 32
    WINE_CONDITIONING_UNIT = 33
    WINE_STORAGE_CONDITIONING_UNIT = 34
    STEAM_OVEN_MICRO = 45
    DIALOG_OVEN = 67
    WINE_CABINET_FREEZER = 68
    STEAM_OVEN_MK2 = 73
    HOB_INDUCT_EXTR = 74
    UNKNOWN = 2147483647  # int.MaxValue sentinel — our own HA peer uses this


# Pre-computed family tuples so per-platform entity tables stay readable.
OVEN_FAMILY: tuple[MieleAppliance, ...] = (
    MieleAppliance.OVEN,
    MieleAppliance.OVEN_MICROWAVE,
    MieleAppliance.STEAM_OVEN,
    MieleAppliance.MICROWAVE,
    MieleAppliance.STEAM_OVEN_COMBI,
    MieleAppliance.STEAM_OVEN_MICRO,
    MieleAppliance.DIALOG_OVEN,
    MieleAppliance.STEAM_OVEN_MK2,
)

LAUNDRY_FAMILY: tuple[MieleAppliance, ...] = (
    MieleAppliance.WASHING_MACHINE,
    MieleAppliance.WASHING_MACHINE_SEMI_PROFESSIONAL,
    MieleAppliance.WASHING_MACHINE_PROFESSIONAL,
    MieleAppliance.TUMBLE_DRYER,
    MieleAppliance.TUMBLE_DRYER_SEMI_PROFESSIONAL,
    MieleAppliance.DRYER_PROFESSIONAL,
    MieleAppliance.WASHER_DRYER,
)

DISHWASHER_FAMILY: tuple[MieleAppliance, ...] = (
    MieleAppliance.DISHWASHER,
    MieleAppliance.DISHWASHER_SEMI_PROFESSIONAL,
    MieleAppliance.DISHWASHER_PROFESSIONAL,
)

# Cycle devices = devices that run a "program" with phases, remaining time, etc.
CYCLE_FAMILY: tuple[MieleAppliance, ...] = (
    *OVEN_FAMILY, *LAUNDRY_FAMILY, *DISHWASHER_FAMILY,
)

HOB_FAMILY: tuple[MieleAppliance, ...] = (
    MieleAppliance.HOB_HIGHLIGHT,
    MieleAppliance.HOB_INDUCTION,
    MieleAppliance.HOB_INDUCT_EXTR,
)

COOLING_FAMILY: tuple[MieleAppliance, ...] = (
    MieleAppliance.FRIDGE,
    MieleAppliance.FREEZER,
    MieleAppliance.FRIDGE_FREEZER,
)

WINE_FAMILY: tuple[MieleAppliance, ...] = (
    MieleAppliance.WINE_CABINET,
    MieleAppliance.WINE_CONDITIONING_UNIT,
    MieleAppliance.WINE_STORAGE_CONDITIONING_UNIT,
    MieleAppliance.WINE_CABINET_FREEZER,
)

# Devices that have a cavity / cabinet light controllable via /State.Light.
LIGHTABLE_FAMILY: tuple[MieleAppliance, ...] = (
    *OVEN_FAMILY,
    MieleAppliance.COFFEE_SYSTEM,
    MieleAppliance.HOOD,
    *WINE_FAMILY,
)

# Devices with deep-standby (need a Wake action before remote control wakes them).
#
# RE notes (from live /State probes 2026-05-21):
#  - oven: has DeviceAction + StandbyState   → Wake supported ✓
#  - dryer: has DeviceAction + StandbyState  → Wake supported ✓
#  - hob: no DeviceAction in /State          → Wake DOES NOT apply ✗
#  - fridge: no DeviceAction (always running)→ Wake DOES NOT apply ✗
#
# Wake is meaningful only for appliances that genuinely go to deep standby
# (cycle devices + coffee + dish-warmer).
WAKEABLE_FAMILY: tuple[MieleAppliance, ...] = (
    *CYCLE_FAMILY,
    MieleAppliance.COFFEE_SYSTEM,
    MieleAppliance.DISH_WARMER,
)

# Devices that physically have a door we can sense.
DOORED_FAMILY: tuple[MieleAppliance, ...] = (
    *OVEN_FAMILY, *LAUNDRY_FAMILY, *DISHWASHER_FAMILY,
    *COOLING_FAMILY, *WINE_FAMILY,
    MieleAppliance.DISH_WARMER,
)

# Devices that report Light as a meaningful value (1=on, 2=off) instead of 0=unsupported.
HAS_LIGHT_STATE: tuple[MieleAppliance, ...] = LIGHTABLE_FAMILY

# Power switch — devices that can be switched on/off remotely (panel power),
# not the same as Start/Stop of a program (which is heat-gated).
POWERABLE_FAMILY: tuple[MieleAppliance, ...] = (
    *OVEN_FAMILY, *DISHWASHER_FAMILY,
    MieleAppliance.DISH_WARMER,
    MieleAppliance.COFFEE_SYSTEM,
    MieleAppliance.HOOD,
)

# Remote program control (start / stop / pause) — laundry + dishwasher only.
#
# Deliberately excludes the oven family, for two reasons:
#  1. Ovens already expose `button.<name>_stop_program` (DOP2 OPCODE_STOP),
#     proven on the author's H7560BP. Adding a second stop button would be
#     redundant and would churn existing users' entity IDs.
#  2. Remote *start* on an oven means remote ignition of a heating element with
#     no interlock beyond MobileStart. Laundry and dishwashers are sealed,
#     water-bearing, and are exactly what Miele's own MobileStart is designed
#     for; ovens are not. HA core's `miele` integration does offer oven start,
#     but it does so behind the cloud's own `/actions` permission list, which
#     we have no local equivalent of.
REMOTE_CONTROLLABLE_FAMILY: tuple[MieleAppliance, ...] = (
    *LAUNDRY_FAMILY, *DISHWASHER_FAMILY,
)


# ---------------------------------------------------------------------------
# /State Status codes
# ---------------------------------------------------------------------------
# Mirror of HA core `miele.const.StateStatus` (which mirrors the cloud API's
# `status`). The local /State reports the same numbering — the existing
# ACTIVE_STATUSES / IDLE_STATUSES sets above were derived from it.


class StateStatus(IntEnum):
    """Appliance status as reported in /State ``Status``."""

    RESERVED = 0
    OFF = 1
    ON = 2
    PROGRAMMED = 3
    WAITING_TO_START = 4
    IN_USE = 5
    PAUSE = 6
    PROGRAM_ENDED = 7
    FAILURE = 8
    PROGRAM_INTERRUPTED = 9
    IDLE = 10
    RINSE_HOLD = 11
    SERVICE = 12
    SUPERFREEZING = 13
    SUPERCOOLING = 14
    SUPERHEATING = 15
    SUPERCOOLING_SUPERFREEZING = 146
    AUTOCLEANING = 147
    NOT_CONNECTED = 255


# When each ProcessAction is actually accepted by the appliance.
#
# HA core gates its start/stop/pause buttons on the cloud's `/actions`
# endpoint, which returns the currently-permitted `processAction` list. The
# local API has no `/actions` equivalent, so we derive the same gate from
# ``Status``:
#
#  * START (1) — WAITING_TO_START, plus PAUSE, because the cloud API has no
#    separate resume action: resuming is START from a paused cycle. PROGRAMMED
#    (3) is excluded — a program is selected but remote start is not armed.
#    Status is necessary but NOT sufficient here: WAITING_TO_START also covers
#    a delay-start countdown, which accepts ProcessAction 1 and ignores it. The
#    mobileStart bit above is the sufficient part; see button.can_start.
#  * PAUSE (3) — only meaningful while a cycle is actually running.
#  * STOP (2) — anything from "program selected" through to "interrupted".
#
# Verified against asyncmiele's `MieleClient.can_remote_start`, which tests
# exactly `status == 4 and 15 in remote_enable`.
STARTABLE_STATUSES = frozenset({StateStatus.WAITING_TO_START, StateStatus.PAUSE})
PAUSABLE_STATUSES = frozenset({StateStatus.IN_USE})
STOPPABLE_STATUSES = frozenset(
    {
        StateStatus.PROGRAMMED,
        StateStatus.WAITING_TO_START,
        StateStatus.IN_USE,
        StateStatus.PAUSE,
        StateStatus.PROGRAM_INTERRUPTED,
        StateStatus.RINSE_HOLD,
    }
)


# Known-good DOP2 leaves on the H7560BP, per protocol_findings memory.
LEAF_DEVICE_COMBINED_STATE = (2, 1586)   # 24 B — modern
LEAF_DEVICE_COMBINED_LEGACY = (2, 256)   # 232 B — deprecated alias
LEAF_HOURS_OF_OPERATION = (2, 119)       # 47 B
LEAF_CYCLE_COUNTER = (2, 138)            # 49 B
LEAF_USER_REQUEST_STATUS = (2, 1583)     # readable echo of last request
LEAF_SF_VALUE = (2, 105)                 # indexed by SfValueId in idx1
LEAF_UNKNOWN_2_1577 = (2, 1577)          # 278 B — TBD
LEAF_PROGRAM_LIST = (14, 1570)           # 62 B — oven program list (legacy layout)
LEAF_OPTION_LIST = (14, 1571)            # 212 B — options for selected program
LEAF_GENERAL_1_17 = (1, 17)              # 402 B — general capabilities/info
