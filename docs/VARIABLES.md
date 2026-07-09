# iRacing Telemetry Variables — Types & Reference

This is the **frontend contract**. The backend builds the authoritative,
content-exact list at runtime from the binary header and serves it at
`GET /api/v1/variables` (and `GET /api/v1/sessions/{id}/variables`). The tables
below document the stable core set so you can design charts before capturing data.

## The six wire types

Every telemetry variable has exactly one of these types (`irsdk_VarType`). Mapping
used by the backend:

| idx | irSDK type | bytes | Python | JSON | DuckDB | Arrow |
|----:|------------|------:|--------|------|--------|-------|
| 0 | `char` | 1 | `str` | string | `VARCHAR` | string |
| 1 | `bool` | 1 | `bool` | boolean | `BOOLEAN` | bool |
| 2 | `int` | 4 | `int` | number | `INTEGER` | int32 |
| 3 | `bitfield` | 4 | `int` + `{flag: bool}` | number + object | `INTEGER` | int32 |
| 4 | `float` | 4 | `float` | number | `FLOAT` | float32 |
| 5 | `double` | 8 | `float` | number | `DOUBLE` | float64 |

**Arrays** (`count > 1`): count ≤ `RTV_ARRAY_FLATTEN_MAX` (default 6) are flattened
to `Name_0..Name_n` columns; larger arrays (per-car-index = 64) are stored as a
single `LIST` column. **Enums** are `int` on the wire; the API returns
`{value, label}`. **Bitfields** keep the raw int and emit named booleans.

Type legend below: `f`=float, `d`=double, `i`=int, `b`=bool, `bf`=bitfield,
`c`=char, `[N]`=array count. `{C}` = per-corner `{LF,RF,LR,RR}`. **(car)** =
present only on cars with that feature.

## Session / timing / state

| Name | Type | Unit | Description |
|------|------|------|-------------|
| SessionTime | d | s | Seconds since session start (canonical x-axis) |
| SessionTick | i | — | Current sim update number |
| SessionNum | i | — | Session number within the event |
| SessionState | i(enum) | — | invalid/get_in_car/warmup/parade/racing/checkered/cooldown |
| SessionUniqueID | i | — | Unique session id |
| SessionFlags | bf | — | Global flags (green/yellow/red/blue/white/checkered/caution…) |
| SessionTimeRemain | d | s | Time left in session |
| SessionLapsRemain | i | — | Laps left |
| SessionLapsRemainEx | i | — | Improved laps-left |
| SessionTimeTotal | d | s | Total session time |
| SessionLapsTotal | i | — | Total session laps |
| SessionTimeOfDay | f | s | Time of day |
| SessionJokerLapsRemain | i | — | Joker laps left (car/track) |
| SessionOnJokerLap | b | — | On joker lap (car/track) |
| DisplayUnits | i | — | 0 metric, 1 imperial |
| DriverMarker | b | — | Driver marker toggle |
| PushToTalk | b | — | PTT active |
| RadioTransmitCarIdx | i | — | Car transmitting on radio |
| RadioTransmitRadioIdx | i | — | Radio index |
| RadioTransmitFrequencyIdx | i | — | Frequency index |

## Player car — motion / world position

| Name | Type | Unit | Description |
|------|------|------|-------------|
| Lat | d | deg | Latitude |
| Lon | d | deg | Longitude |
| Alt | f | m | Altitude |
| Speed | f | m/s | GPS ground speed |
| VelocityX / VelocityY / VelocityZ | f | m/s | Velocity in car frame |
| Yaw | f | rad | Yaw orientation |
| YawNorth | f | rad | Yaw relative to north |
| Pitch | f | rad | Pitch orientation |
| Roll | f | rad | Roll orientation |
| LongAccel | f | m/s² | Longitudinal acceleration |
| LatAccel | f | m/s² | Lateral acceleration |
| VertAccel | f | m/s² | Vertical acceleration |
| YawRate | f | rad/s | Yaw rate |
| PitchRate | f | rad/s | Pitch rate |
| RollRate | f | rad/s | Roll rate |
| EnterExitReset | i | — | Reset state |

## Lap / distance / deltas

| Name | Type | Unit | Description |
|------|------|------|-------------|
| Lap | i | — | Current lap |
| LapCompleted | i | — | Last completed lap |
| LapDist | f | m | Distance this lap |
| LapDistPct | f | % | Fraction around lap (0..1) — track-map/alignment axis |
| RaceLaps | i | — | Laps completed in race |
| LapBestLap | i | — | Best lap number |
| LapBestLapTime | f | s | Best lap time |
| LapCurrentLapTime | f | s | Current lap elapsed |
| LapLastLapTime | f | s | Last lap time |
| LapBestNLapLap / LapBestNLapTime | i / f | — / s | Best N-lap average |
| LapLastNLapTime | f | s | Last N-lap |
| LapDeltaToBestLap (+_DD, +_OK) | f / f / b | s | Delta to best lap (+rate, +valid) |
| LapDeltaToSessionBestLap (+_DD, +_OK) | f / f / b | s | Delta to session best |
| LapDeltaToOptimalLap (+_DD, +_OK) | f / f / b | s | Delta to optimal |
| LapDeltaToSessionOptimalLap (+_DD, +_OK) | f / f / b | s | Delta to session optimal |
| LapDeltaToSessionLastlLap (+_DD, +_OK) | f / f / b | s | Delta to session last |

## Driver inputs / drivetrain

| Name | Type | Unit | Description |
|------|------|------|-------------|
| Throttle / ThrottleRaw | f | % | Throttle (filtered / raw) |
| Brake / BrakeRaw | f | % | Brake (filtered / raw) |
| BrakeABSactive | b | — | ABS engaging |
| Clutch / ClutchRaw | f | % | Clutch |
| HandbrakeRaw | f | % | Handbrake |
| Gear | i | — | -1 reverse, 0 neutral, 1..n |
| RPM | f | rev/min | Engine RPM |
| ShiftPowerPct | f | % | Shift power |
| ShiftGrindRPM | f | rev/min | Grind RPM |
| ShiftIndicatorPct | f | % | Shift light fraction |
| SteeringWheelAngle | f | rad | Steering angle |
| SteeringWheelAngleMax | f | rad | Max steering angle |
| SteeringWheelTorque | f | N·m | FFB torque |
| SteeringWheelTorque_ST | f[6] | N·m | 360 Hz torque subsamples |
| SteeringWheelPctTorque | f | % | Torque as % |
| SteeringWheelPctTorqueSign / SignStops | f | % | Signed torque |
| SteeringWheelPctSmoothing / PctDamper | f | % | FFB smoothing / damper |
| SteeringWheelMaxForceNm | f | N·m | Max FFB force |
| SteeringWheelUseLinear | b | — | Linear FFB mode |
| dcThrottleShape, dcBrakeBias, dcTractionControl, dcABS, dcAntiRollFront/Rear, dcDiffEntry/Mid/Exit | f / i | varies | (car) driver-adjustable controls; set varies per car |
| dpRFTireChange, dpFuelFill, dpWindshieldTearoff | f | — | (car) pit-display request controls |

## Engine / fluids / status

| Name | Type | Unit | Description |
|------|------|------|-------------|
| FuelLevel | f | l | Fuel remaining |
| FuelLevelPct | f | % | Fuel fraction |
| FuelUsePerHour | f | kg/h | Instantaneous fuel use |
| FuelPress | f | bar | Fuel pressure |
| ManifoldPress | f | bar | Manifold pressure (car) |
| OilLevel | f | l | Oil level |
| OilPress | f | bar | Oil pressure |
| OilTemp | f | C | Oil temperature |
| WaterLevel | f | l | Water level |
| WaterTemp | f | C | Water temperature |
| Voltage | f | V | Battery voltage |
| EngineWarnings | bf | — | water/fuel/oil temp & press, stall, pit limiter, rev limiter |
| IsOnTrack | b | — | Car on track & under control |
| IsOnTrackCar | b | — | Car physically on track |
| IsInGarage | b | — | In garage screen |
| OnPitRoad | b | — | On pit road |

## Tyres / suspension (per corner `{LF,RF,LR,RR}`)

| Name | Type | Unit | Description |
|------|------|------|-------------|
| {C}tempCL / {C}tempCM / {C}tempCR | f | C | Carcass temp inner/middle/outer (live) |
| {C}tempL / {C}tempM / {C}tempR | f | C | Last-measured tread temps L/M/R |
| {C}wearL / {C}wearM / {C}wearR | f | % | Tread remaining L/M/R |
| {C}pressure | f | kPa | Tyre pressure |
| {C}coldPressure | f | kPa | Cold pressure (last set) |
| {C}speed | f | m/s | Tyre contact speed |
| {C}rideHeight | f | m | Ride height |
| {C}shockDefl | f | m | Shock deflection |
| {C}shockDefl_ST | f[6] | m | 360 Hz subsamples |
| {C}shockVel | f | m/s | Shock velocity |
| {C}shockVel_ST | f[6] | m/s | 360 Hz subsamples |
| {C}brakeLinePress | f | bar | Brake line pressure |

## Opponents — per-car-index arrays (`[64]`; player's own index = `PlayerCarIdx`)

| Name | Type | Unit | Description |
|------|------|------|-------------|
| CarIdxLap | i[64] | — | Current lap per car |
| CarIdxLapCompleted | i[64] | — | Last completed lap |
| CarIdxLapDistPct | f[64] | % | Track position fraction |
| CarIdxTrackSurface | i[64](enum) | — | Surface location (TrkLoc) |
| CarIdxTrackSurfaceMaterial | i[64](enum) | — | Surface material |
| CarIdxOnPitRoad | b[64] | — | On pit road |
| CarIdxPosition | i[64] | — | Overall position |
| CarIdxClassPosition | i[64] | — | In-class position |
| CarIdxClass | i[64] | — | Car class id |
| CarIdxF2Time | f[64] | s | Gap (F2) time |
| CarIdxEstTime | f[64] | s | Estimated lap-position time |
| CarIdxGear | i[64] | — | Gear |
| CarIdxRPM | f[64] | rev/min | RPM |
| CarIdxSteer | f[64] | rad | Steering angle |
| CarIdxBestLapNum / CarIdxBestLapTime | i[64] / f[64] | — / s | Best lap |
| CarIdxLastLapTime | f[64] | s | Last lap |
| CarIdxSessionFlags | bf[64] | — | Per-car flags (black/blue/meatball…) |
| CarIdxPaceLine / CarIdxPaceRow / CarIdxPaceFlags | i[64] / i[64] / bf[64] | — | Pace-lap formation |
| CarIdxP2P_Status / CarIdxP2P_Count | b[64] / i[64] | — | Push-to-pass (car) |
| CarIdxTireCompound / CarIdxQualTireCompound | i[64] | — | Tyre compound |
| CarIdxFastRepairsUsed | i[64] | — | Fast repairs used |

## Pit service

| Name | Type | Unit | Description |
|------|------|------|-------------|
| PitsOpen | b | — | Pits open |
| PitstopActive | b | — | Pit stop in progress |
| PitRepairLeft / PitOptRepairLeft | f | s | Mandatory / optional repair time left |
| PitSvFlags | bf | — | Requested services (LF/RF/LR/RR tyre, fuel, tearoff, fast repair) |
| PitSvLFP / PitSvRFP / PitSvLRP / PitSvRRP | f | kPa | Service tyre pressures |
| PitSvFuel | f | l | Fuel to add |
| PitSvTireCompound | i | — | Service compound |
| PlayerCarPitSvStatus | i(enum) | — | Pit service status |
| FastRepairUsed / FastRepairAvailable | i | — | Fast repairs |

## Weather / environment

| Name | Type | Unit | Description |
|------|------|------|-------------|
| AirDensity | f | kg/m³ | Air density |
| AirPressure | f | Pa | Air pressure |
| AirTemp | f | C | Air temperature |
| TrackTemp / TrackTempCrew | f | C | Track temp (measured / crew) |
| RelativeHumidity | f | % | Humidity |
| FogLevel | f | % | Fog |
| Precipitation | f | % | Precipitation |
| Skies | i | — | 0 clear … 3 overcast |
| WeatherType | i | — | Weather model |
| WindDir | f | rad | Wind direction |
| WindVel | f | m/s | Wind velocity |
| SolarAltitude / SolarAzimuth | f | rad | Sun position |

## Player meta / incidents / shift lights

| Name | Type | Unit | Description |
|------|------|------|-------------|
| PlayerCarIdx | i | — | This car's index into CarIdx arrays |
| PlayerCarPosition / PlayerCarClassPosition | i | — | Position |
| PlayerCarClass | i | — | Car class |
| PlayerTrackSurface | i(enum) | — | Surface location |
| PlayerTrackSurfaceMaterial | i(enum) | — | Surface material |
| PlayerCarMyIncidentCount | i | — | My incidents |
| PlayerCarDriverIncidentCount | i | — | Driver incidents |
| PlayerCarTeamIncidentCount | i | — | Team incidents |
| PlayerCarTowTime | f | s | Tow time remaining |
| PlayerCarInPitStall | b | — | In pit stall |
| PlayerCarPowerAdjust | f | % | BoP power |
| PlayerCarWeightPenalty | f | kg | BoP weight |
| PlayerCarSLFirstRPM / SLShiftRPM / SLLastRPM / SLBlinkRPM | f | rev/min | Shift-light thresholds |

## Replay / camera / system

| Name | Type | Unit | Description |
|------|------|------|-------------|
| IsReplayPlaying | b | — | Replay active |
| ReplayFrameNum / ReplayFrameNumEnd | i | — | Replay frame |
| ReplayPlaySpeed | i | — | Playback speed |
| ReplayPlaySlowMotion | b | — | Slow-mo |
| ReplaySessionTime | d | s | Replay session time |
| ReplaySessionNum | i | — | Replay session |
| CamCarIdx | i | — | Camera target car |
| CamCameraNumber / CamGroupNumber | i | — | Camera selection |
| CamCameraState | bf | — | Camera state flags |
| FrameRate | f | fps | Sim FPS |
| CpuUsageFG / CpuUsageBG | f | % | CPU usage |
| GpuUsage | f | % | GPU usage |
| ChanAvgLatency / ChanLatency / ChanQuality / ChanClockSkew | f | s / — | Network channel stats |
| IsDiskLoggingActive / IsDiskLoggingEnabled | b | — | .ibt logging state |

## Hybrid / ERS / DRS — equipped cars only (car)

| Name | Type | Unit | Description |
|------|------|------|-------------|
| DRS_Status | i | — | DRS state |
| EnergyERSBattery / EnergyBatteryToMGU / EnergyBudgetBattToMGU | f | J | ERS energy |
| PowerMGUH / PowerMGUK / TorqueMGUK | f | W / N·m | MGU power/torque |
| MGUKDeployAdapt / MGUKDeployFixed / MGUKRegenGain | f | — | Deploy modes |

## Session-info YAML (separate from time-series)

Parsed to JSON and served at `GET /api/v1/sessions/{id}/info`. Top-level sections:
`WeekendInfo`, `SessionInfo`, `DriverInfo` (per-driver arrays: name, car, class,
iRating, license, …), `QualifyResultsInfo`, `CameraInfo`, `RadioInfo`,
`SplitTimeInfo`, `CarSetup`.

---

**Reminder:** the runtime `/variables` endpoint is the complete, authoritative
source for the loaded content — including any car-specific channels not listed
here. Always read it rather than hard-coding channel names in the frontend.
