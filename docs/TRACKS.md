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

## Live labels

The live pitwall uses the same labels. `rtv.tracks.live.LiveCornerMap` keeps the
lap being driven, resampled the way the store resamples a lap, and runs Layer 1's
apex detector and the same name lookup on each clean lap. Once two clean laps agree
on their apexes it becomes `RaceState.track`, and every event that happened
somewhere (lock-up, wheelspin, off-track, incident, recurring issue) carries
`place: {label, name, lap_dist_pct, provisional}`: `T1 La Source`, as the review
shows it. Until then the label is the lap-distance bucket (`C00`-`C19`) with
`provisional: true`. Two laps rather than one because Layer 1 numbers each lap on
its own and one odd lap (a start in traffic, a lift) can add or lose an apex. On
the Spa race the map is ready after lap 5 and equals the review's T1-T8. A live
stream without `Lat`/`Lon` still gets the labels; its names stay empty, as in its
own review.

## One numbering on every lap

Layer 1 detects apexes on each lap afresh, so on its own a label meant "the Nth dip
of this lap": at Spa lap 8 misses Speaker's Corner, so its T4 was Double Gauche, and
lap 1 has an extra dip at Raidillon. `rtv.tracks.service.session_corner_map` runs the
live map's procedure over the stored clean laps in lap order (the first two that
agree fix the list; with no agreeing pair, the fastest clean lap's corners) and
`CoachingService.lap_findings` relabels every lap pair onto it by apex position
(`rtv.tracks.live.MATCH_PCT`, 1.4 % of a lap). A label and its name are therefore the
same corner on every lap pair, in the coaches, `get_corner_detail`, the debrief and
the live events. `LapFindings.corner_map_lap` says which lap the map was built on.
A map corner a lap did not show is missing from that pair, with a note
(`T4 (Speaker's Corner) ... was not detected on lap 8`); an apex the map has no corner
for is numbered after the map's last corner (`T9`), with a note that the label holds
for that pair only. On Spa the stored map is built on lap 3 (the store does not know
laps 3-4 ran under yellow, which the live engine skips), with the same T1-T8 and
names as the live map built on lap 5.

In RTV Desktop's review, the findings table and top opportunities show
`T1 La Source` where a name is mapped, the track map letters each named corner
beside the line, and a line under the map gives the header facts.

## Names are checked facts

A coach that says "La Source" about T7 is wrong even when every number it quotes is
T7's, so corner names are checked like figures (`rtv.grounding.corner_names`, no LLM).
The lap findings carry `venue` (the reference key) and `places`: every named stretch
of the venue located on the main lap in metres, with the detected corner whose
`entry_m`-`exit_m` window holds it (`None` for a straight such as Kemmel or a flat-out
curve such as Eau Rouge). The rules, for the coaches' prose check
(`check_prose`, lap mode) and the pitwall validator (every agent, once it was shown the
live corner map):

- a name next to a label (`T7 Campus`, `T8 (Chicane)`) must be that label's name; at a
  trace-numbered venue the labels have no names, so any name there is invented;
- a name on its own must belong to the corner the text is about (an advice's corner,
  a call's `corner` field or event place, or a label in the same sentence). On the
  pitwall the operator-facing `detail_text` and `rationale` may discuss other corners,
  so there a name is only held to the label it is written next to;
- another circuit's name is not a corner of this session. Generic words several
  circuits use ("the Esses", "Hairpin", "Club", "Turn 5") never count as another
  circuit's name, and corner types ("T8 Hairpin") are not names.

Layer 1 numbers corners per lap pair, so another pair's `T3` can be the map's `T2`.
The pitwall lines a Layer-1 label from `get_corner_detail` up with the live map corner
nearest its apex, and merged findings with two numberings make the second label an
alias, so a name is held to either. R3 (one numbering on every lap) removes this at
the source.

A wrong name is a `name` issue in the prose report and a `contradictions` entry in the
pitwall's grounding report, with the same repair turn and refusal as an invented figure.
The swapped-name golden cases are `evals.pitwall_cases.swapped_name_cases` (Spa and a
trace-numbered map) and the coach eval case `seed_named_corner`.

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
names in OpenStreetMap today. By decision (2026-10-31) they keep the trace
numbering (`T1`, `T2`... from Layer 1, live and in the review) and nobody edits
OpenStreetMap for RTV. Should that change, adding names there (on the circuit's
`highway=raceway` ways) and re-running the build is how they would get in.

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
