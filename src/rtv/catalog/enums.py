"""Bitfield and enum decode tables — the only embedded iRacing-specific constants.

The binary header tells us a variable is a ``bitField`` or an ``int`` but not
*which* one, so we classify by variable name. Bitfields decode the raw int into
named booleans; enums map the raw int to a label. Values mirror the iRacing SDK
(``irsdk_defines.h`` / pyirsdk ``Flags`` & enum classes).
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Bitfields:  {variable name: {flag label: bitmask}}
# --------------------------------------------------------------------------

# Global session flags (irsdk_Flags).
SESSION_FLAGS: dict[str, int] = {
    "checkered": 0x00000001,
    "white": 0x00000002,
    "green": 0x00000004,
    "yellow": 0x00000008,
    "red": 0x00000010,
    "blue": 0x00000020,
    "debris": 0x00000040,
    "crossed": 0x00000080,
    "yellow_waving": 0x00000100,
    "one_lap_to_green": 0x00000200,
    "green_held": 0x00000400,
    "ten_to_go": 0x00000800,
    "five_to_go": 0x00001000,
    "random_waving": 0x00002000,
    "caution": 0x00004000,
    "caution_waving": 0x00008000,
    # driver black flags
    "black": 0x00010000,
    "disqualify": 0x00020000,
    "servicible": 0x00040000,
    "furled": 0x00080000,
    "repair": 0x00100000,
    # start lights
    "start_hidden": 0x10000000,
    "start_ready": 0x20000000,
    "start_set": 0x40000000,
    "start_go": 0x80000000,
}

# Engine warning bitfield (irsdk_EngineWarnings).
ENGINE_WARNINGS: dict[str, int] = {
    "water_temp_warning": 0x0001,
    "fuel_pressure_warning": 0x0002,
    "oil_pressure_warning": 0x0004,
    "engine_stalled": 0x0008,
    "pit_speed_limiter": 0x0010,
    "rev_limiter_active": 0x0020,
    "oil_temp_warning": 0x0040,
}

# Pit service request flags (irsdk_PitSvFlags).
PIT_SV_FLAGS: dict[str, int] = {
    "lf_tire_change": 0x0001,
    "rf_tire_change": 0x0002,
    "lr_tire_change": 0x0004,
    "rr_tire_change": 0x0008,
    "fuel_fill": 0x0010,
    "windshield_tearoff": 0x0020,
    "fast_repair": 0x0040,
}

# Per-car pace flags (irsdk_PaceFlags).
PACE_FLAGS: dict[str, int] = {
    "end_of_line": 0x0001,
    "free_pass": 0x0002,
    "waved_around": 0x0004,
}

# Camera state (irsdk_CameraState).
CAMERA_STATE: dict[str, int] = {
    "is_session_screen": 0x0001,
    "is_scenic_active": 0x0002,
    "cam_tool_active": 0x0004,
    "ui_hidden": 0x0008,
    "use_auto_shot_selection": 0x0010,
    "use_temporary_edits": 0x0020,
    "use_key_acceleration": 0x0040,
    "use_key10x_acceleration": 0x0080,
    "use_mouse_aim_mode": 0x0100,
}

# Registry of bitfield variable name -> flag table.
BITFIELD_TABLES: dict[str, dict[str, int]] = {
    "SessionFlags": SESSION_FLAGS,
    "EngineWarnings": ENGINE_WARNINGS,
    "PitSvFlags": PIT_SV_FLAGS,
    "CamCameraState": CAMERA_STATE,
    "CarIdxSessionFlags": SESSION_FLAGS,  # per-car array of session flags
    "CarIdxPaceFlags": PACE_FLAGS,
}


# --------------------------------------------------------------------------
# Enums:  {variable name: {raw value: label}}
# --------------------------------------------------------------------------

SESSION_STATE: dict[int, str] = {
    0: "invalid",
    1: "get_in_car",
    2: "warmup",
    3: "parade_laps",
    4: "racing",
    5: "checkered",
    6: "cool_down",
}

# Track surface / location (irsdk_TrkLoc).
TRK_LOC: dict[int, str] = {
    -1: "not_in_world",
    0: "off_track",
    1: "in_pit_stall",
    2: "approaching_pits",
    3: "on_track",
}

# Track surface material (irsdk_TrkSurf), abbreviated to the common set.
TRK_SURF: dict[int, str] = {
    -1: "surface_not_in_world",
    0: "undefined",
    1: "asphalt_1",
    2: "asphalt_2",
    3: "asphalt_3",
    4: "asphalt_4",
    5: "concrete_1",
    6: "concrete_2",
    7: "racing_dirt_1",
    8: "racing_dirt_2",
    9: "paint_1",
    10: "paint_2",
    11: "rumble_1",
    12: "rumble_2",
    13: "rumble_3",
    14: "rumble_4",
    15: "grass_1",
    16: "grass_2",
    17: "grass_3",
    18: "grass_4",
    19: "dirt_1",
    20: "dirt_2",
    21: "dirt_3",
    22: "dirt_4",
    23: "sand",
    24: "gravel_1",
    25: "gravel_2",
    26: "grasscrete",
    27: "astroturf",
}

# Pit service status (irsdk_PitSvStatus).
PIT_SV_STATUS: dict[int, str] = {
    0: "none",
    1: "in_progress",
    2: "complete",
    100: "too_far_left",
    101: "too_far_right",
    102: "too_far_forward",
    103: "too_far_back",
    104: "bad_angle",
    105: "cant_fix_that",
}

# Pace mode (irsdk_PaceMode).
PACE_MODE: dict[int, str] = {
    0: "single_file_start",
    1: "double_file_start",
    2: "single_file_restart",
    3: "double_file_restart",
    4: "not_pacing",
}

# Car left/right proximity (irsdk_CarLeftRight).
CAR_LEFT_RIGHT: dict[int, str] = {
    0: "off",
    1: "clear",
    2: "car_left",
    3: "car_right",
    4: "car_left_right",
    5: "two_cars_left",
    6: "two_cars_right",
}

# Registry of enum variable name -> value/label table.
ENUM_TABLES: dict[str, dict[int, str]] = {
    "SessionState": SESSION_STATE,
    "PlayerTrackSurface": TRK_LOC,
    "PlayerTrackSurfaceMaterial": TRK_SURF,
    "CarIdxTrackSurface": TRK_LOC,
    "CarIdxTrackSurfaceMaterial": TRK_SURF,
    "PlayerCarPitSvStatus": PIT_SV_STATUS,
    "PaceMode": PACE_MODE,
    "CarLeftRight": CAR_LEFT_RIGHT,
}


# --------------------------------------------------------------------------
# Decoders
# --------------------------------------------------------------------------


def decode_bitfield(table: dict[str, int], raw: int) -> dict[str, bool]:
    """Expand a raw bitfield int into ``{flag: bool}`` using its table."""
    raw = int(raw) & 0xFFFFFFFF
    return {label: bool(raw & mask) for label, mask in table.items()}


def decode_enum(table: dict[int, str], raw: int) -> str:
    """Map a raw enum int to its label (falls back to ``value:<n>``)."""
    return table.get(int(raw), f"value:{int(raw)}")
