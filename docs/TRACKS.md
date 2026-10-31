# Track reference data

RTV works at any iRacing road course: laps, the track map, corner detection, pit
detection and every number the agents use come from the session's own telemetry
and header, not from a track table. What the telemetry cannot tell us is what the
corners are *called*. That is the one thing `rtv.tracks` adds.

## Where each fact comes from

| Fact | Source | Notes |
|---|---|---|
| Layout name, measured and official length, turn count, pit speed limit, track type | `WeekendInfo` in the session-info YAML of every `.ibt` and live stream | `header_facts()`; the authority for the layout actually driven |
| Official sector split points | `SplitTimeInfo.Sectors[].SectorStartPct` | served as `sector_starts_pct`; Layer 1 still reports even thirds |
| Track map, corner positions (apexes), lap distance | the session's own `Lat`/`Lon`/`LapDistPct`/`Speed` channels | unchanged |
| Pit lane in use | `OnPitRoad`, `PlayerTrackSurface` | unchanged |
| Timed-race finish | `SessionInfo` session time and laps, `SessionTimeRemain`, flags | a session rule, not a track fact; the race-state engine handles it |
| **Corner names**, named straights, pit lane geometry | OpenStreetMap `highway=raceway` ways (and corner-named points) in `src/rtv/tracks/data/<venue>.json` | the only pre-loaded data |

## How a session gets its names

1. `resolve(WeekendInfo)` picks the venue whose centre is within 5 km of
   `TrackLatitude`/`TrackLongitude` (falling back to a `TrackName` prefix such as
   `spa`, `spielberg`, `monza`). One venue file covers every layout there
   (Nürburgring GP and Nordschleife, Le Mans, Daytona road course...).
2. `corner_map()` walks one lap's own Lat/Lon trace and gives each point the
   nearest mapped stretch within 30 m (60 m for a corner mapped only as a
   labelled point). Contiguous runs become `{name, kind, start_pct, end_pct}`.
   Stretches from a layout not being driven are never near the trace, so they
   drop out without any per-layout table.
3. Layer 1 names each detected apex the same way (`CornerFinding.name`). An apex
   with no mapped corner within range stays `None`: the app never guesses a name.

`GET /api/v1/sessions/{id}/track` returns the header facts, the matched venue and
the corner map for the fastest clean lap (`?lap=` for another). `GET /api/v1/tracks`
lists the venues and `GET /api/v1/tracks/{key}` returns one venue's geometry.

In RTV Desktop's review, the findings table and top opportunities show
`T1 La Source` where a name is mapped, the track map letters each named corner
beside the line, and a line under the map gives the header facts.

## Verified on real telemetry

- Spa (Audi R8 GT3 race, the CI fixture): all 13 named corners and Kemmel found
  in lap order; Layer 1 names T1 La Source through T8 Chicane (the Bus Stop).
  Median distance from the iRacing trace to the OSM centreline is 5.6 m.
- Mount Panorama (Formula iR-04, libibt test file): Hell Corner, Mountain
  Straight, Griffins Bend, The Cutting, Skyline, The Esses, The Dipper,
  Forrest's Elbow, Conrod Straight, The Chase, Murray's Corner, in order.
- Phillip Island (libibt test file): Gardner Straight, Doohan, Stoner, Honda and
  MG corners, in order.
- Monza, Red Bull Ring and Road Atlanta (short public test files): the header
  resolves to the right venue; the files are too short for a full lap trace.

## Coverage

Names exist where OpenStreetMap contributors have mapped them. Venues with few or
no names still work in full; their corners show as T1, T2... from Layer 1.

| Venue | Key | Named corners | Named straights | Pit lane |
|---|---|---|---|---|
| Autodromo Internazionale Enzo e Dino Ferrari | `imola` | 8 | 0 | yes |
| Autodromo Nazionale Monza | `monza` | 13 | 2 | yes |
| Autodromo del Mugello | `mugello` | 16 | 0 | yes |
| Autódromo Internacional do Algarve | `portimao` | 7 | 0 | yes |
| Autódromo José Carlos Pace (Interlagos) | `interlagos` | 14 | 1 | yes |
| Brands Hatch | `brandshatch` | 15 | 3 | yes |
| Circuit Paul Ricard | `paulricard` | 12 | 1 | yes |
| Circuit Zandvoort | `zandvoort` | 10 | 0 | yes |
| Circuit de Barcelona-Catalunya | `barcelona` | 2 | 0 | yes |
| Circuit de Spa-Francorchamps | `spa` | 13 | 1 | yes |
| Circuit des 24 Heures du Mans | `lemans` | 20 | 1 | yes |
| Circuit of the Americas | `cota` | 20 | 0 | yes |
| Daytona International Speedway | `daytona` | 6 | 0 | yes |
| Donington Park | `donington` | 13 | 1 | yes |
| Fuji Speedway | `fuji` | 0 | 0 | yes |
| Hockenheimring | `hockenheim` | 15 | 0 | yes |
| Hungaroring | `hungaroring` | 0 | 0 | yes |
| Kyalami Grand Prix Circuit | `kyalami` | 12 | 0 | yes |
| Lime Rock Park | `limerock` | 8 | 2 | yes |
| Michelin Raceway Road Atlanta | `roadatlanta` | 1 | 0 | yes |
| Mid-Ohio Sports Car Course | `midohio` | 4 | 0 | yes |
| Misano World Circuit | `misano` | 9 | 0 | no |
| Mount Panorama Circuit | `bathurst` | 9 | 2 | yes |
| Nürburgring (GP and Nordschleife) | `nurburgring` | 47 | 1 | yes |
| Okayama International Circuit | `okayama` | 0 | 0 | yes |
| Phillip Island Grand Prix Circuit | `phillipisland` | 4 | 1 | yes |
| Red Bull Ring | `redbullring` | 7 | 0 | yes |
| Sebring International Raceway | `sebring` | 10 | 1 | yes |
| Silverstone Circuit | `silverstone` | 18 | 3 | yes |
| Suzuka Circuit | `suzuka` | 14 | 2 | yes |
| Virginia International Raceway | `vir` | 12 | 0 | yes |
| Watkins Glen International | `watkinsglen` | 9 | 0 | yes |
| WeatherTech Raceway Laguna Seca | `lagunaseca` | 3 | 1 | yes |

Fuji, Hungaroring, Okayama, Barcelona and Road Atlanta have almost no corner
names in OpenStreetMap today. Adding them there (as names on the circuit's
`highway=raceway` ways) and re-running the build is the way to fill them in.

## Rebuilding the data

    python scripts/build_track_refs.py              # all venues, from the OSM API
    python scripts/build_track_refs.py fuji         # one venue
    python scripts/build_track_refs.py --offline    # from data/osm_cache/ only

To add a venue, add a `Venue(...)` line (key, name, bounding box, iRacing
`TrackName` prefixes) to `VENUES` in the script and run it for that key.

## License

The files in `src/rtv/tracks/data/` are derived from OpenStreetMap:
© OpenStreetMap contributors, available under the Open Database License 1.0
(https://www.openstreetmap.org/copyright). Each file carries that attribution and
the OSM way or node ids every name came from. The ODbL applies to that data, not
to the rest of the repository.
