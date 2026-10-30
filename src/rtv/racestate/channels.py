"""The channel-name contract between iRacing's runtime catalog and the engine.

Nothing here is required to exist. Every group is probed against the session's
:class:`~rtv.catalog.models.Catalog` at engine start; a group whose *required*
channels are absent is switched off, its state fields stay ``None`` and the
matching :class:`~rtv.racestate.models.Capabilities` flag records why. That is
the same grounded-refusal discipline the Layer-3b coach uses: better to say
"no data" than to synthesise a number.

Names mirror the standard iRacing variable set documented in ``docs/VARIABLES.md``.
"""

from __future__ import annotations

# --- player / session identity ------------------------------------------
SESSION_TIME = "SessionTime"
SESSION_TICK = "SessionTick"
SESSION_STATE = "SessionState"
SESSION_TIME_REMAIN = "SessionTimeRemain"
SESSION_LAPS_REMAIN = "SessionLapsRemainEx"
SESSION_LAPS_REMAIN_FALLBACK = "SessionLapsRemain"
SESSION_LAPS_TOTAL = "SessionLapsTotal"

LAP = "Lap"
LAP_DIST_PCT = "LapDistPct"
LAP_DIST = "LapDist"
LAP_LAST_LAP_TIME = "LapLastLapTime"
LAP_BEST_LAP_TIME = "LapBestLapTime"
SPEED = "Speed"
THROTTLE = "Throttle"
BRAKE = "Brake"
GEAR = "Gear"
RPM = "RPM"
ON_PIT_ROAD = "OnPitRoad"
PLAYER_CAR_IDX = "PlayerCarIdx"
PLAYER_CAR_POSITION = "PlayerCarPosition"
PLAYER_CAR_CLASS_POSITION = "PlayerCarClassPosition"
PLAYER_TRACK_SURFACE = "PlayerTrackSurface"
PLAYER_TRACK_SURFACE_MATERIAL = "PlayerTrackSurfaceMaterial"
PLAYER_INCIDENTS = "PlayerCarMyIncidentCount"

# --- flags ---------------------------------------------------------------
SESSION_FLAGS = "SessionFlags"
CAR_IDX_SESSION_FLAGS = "CarIdxSessionFlags"

# --- per-car-index arrays (standings) ------------------------------------
CAR_IDX_LAP_DIST_PCT = "CarIdxLapDistPct"
CAR_IDX_POSITION = "CarIdxPosition"
CAR_IDX_CLASS_POSITION = "CarIdxClassPosition"
CAR_IDX_LAP = "CarIdxLap"
CAR_IDX_LAP_COMPLETED = "CarIdxLapCompleted"
CAR_IDX_LAST_LAP_TIME = "CarIdxLastLapTime"
CAR_IDX_BEST_LAP_TIME = "CarIdxBestLapTime"
CAR_IDX_ON_PIT_ROAD = "CarIdxOnPitRoad"
CAR_IDX_TRACK_SURFACE = "CarIdxTrackSurface"

# --- fuel ----------------------------------------------------------------
FUEL_LEVEL = "FuelLevel"
FUEL_LEVEL_PCT = "FuelLevelPct"

# --- wheels (lock-up / wheelspin detectors) ------------------------------
WHEEL_SPEED = {"LF": "LFspeed", "RF": "RFspeed", "LR": "LRspeed", "RR": "RRspeed"}
FRONT_WHEELS = ("LF", "RF")
REAR_WHEELS = ("LR", "RR")

# --- tyres ---------------------------------------------------------------
CORNERS = ("LF", "RF", "LR", "RR")
# Middle carcass temperature is the one channel present on every tyre model.
TYRE_TEMP = {c: f"{c}tempCM" for c in CORNERS}
TYRE_PRESSURE = {c: f"{c}pressure" for c in CORNERS}
# Fallback for catalogs that only expose the flattened 4-element array form
# (this is what the synthetic/demo catalogs in tests/ and scripts/ produce).
TYRE_PRESSURE_FLAT = {c: f"TyrePressure_{i}" for i, c in enumerate(CORNERS)}

# --- car health ----------------------------------------------------------
OIL_TEMP = "OilTemp"
WATER_TEMP = "WaterTemp"
OIL_PRESS = "OilPress"
FUEL_PRESS = "FuelPress"
VOLTAGE = "Voltage"
ENGINE_WARNINGS = "EngineWarnings"
TOW_TIME = "PlayerCarTowTime"

# --- conditions ----------------------------------------------------------
AIR_TEMP = "AirTemp"
TRACK_TEMP = "TrackTempCrew"
TRACK_TEMP_FALLBACK = "TrackTemp"
AIR_DENSITY = "AirDensity"
AIR_PRESSURE = "AirPressure"
REL_HUMIDITY = "RelativeHumidity"
WIND_VEL = "WindVel"
WIND_DIR = "WindDir"
TRACK_WETNESS = "TrackWetness"

# --- capability groups: {group: (required channels)} ---------------------
# A group is enabled only when *every* required channel is in the catalog.
REQUIRED: dict[str, tuple[str, ...]] = {
    "session": (SESSION_TIME,),
    "flags": (SESSION_FLAGS,),
    "standings": (CAR_IDX_LAP_DIST_PCT,),
    "player": (LAP, LAP_DIST_PCT),
    "fuel": (FUEL_LEVEL,),
    "car_health": (),  # opportunistic: any of the health channels will do
    "conditions": (),  # opportunistic
    "pit": (ON_PIT_ROAD,),
    "incidents": (PLAYER_INCIDENTS,),
    "offtrack": (PLAYER_TRACK_SURFACE,),
}

# Health/conditions are "any-of" groups rather than "all-of".
CAR_HEALTH_ANY = (OIL_TEMP, WATER_TEMP, OIL_PRESS, FUEL_PRESS, VOLTAGE, ENGINE_WARNINGS)
CONDITIONS_ANY = (AIR_TEMP, TRACK_TEMP, TRACK_TEMP_FALLBACK, AIR_DENSITY, REL_HUMIDITY)
