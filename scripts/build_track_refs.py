"""Build the per-track reference data in ``src/rtv/tracks/data/`` from OpenStreetMap.

For each venue in :data:`VENUES` this downloads the OSM map data inside the venue's
bounding box (the public 0.6 API, in small tiles), keeps the ``highway=raceway``
ways, and writes one JSON file per venue with:

- the named raceway segments (corners and named straights) as simplified polylines,
- the pit lane polylines.

The start/finish line is not taken from OSM: iRacing's lap-distance zero is the
authority for where a lap starts.

At runtime :mod:`rtv.tracks` matches a session to a venue by the track position in
the telemetry header and names a point on track by its distance to these polylines,
so every corner name the app shows comes from this mapped geometry.

Map data (c) OpenStreetMap contributors, available under the Open Database License
(ODbL 1.0, https://www.openstreetmap.org/copyright). The generated files carry that
attribution and the OSM way ids each segment came from.

    python scripts/build_track_refs.py                 # every venue
    python scripts/build_track_refs.py spa monza       # some venues
    python scripts/build_track_refs.py --offline       # rebuild from the tile cache

Raw tiles are cached under ``data/osm_cache/`` (gitignored), so a rebuild after
editing the filters does not hit the API again.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "src" / "rtv" / "tracks" / "data"
CACHE_DIR = ROOT / "data" / "osm_cache"
API = "https://api.openstreetmap.org/api/0.6/map?bbox={w},{s},{e},{n}"
USER_AGENT = "rtv-track-ref/0.1 (github.com/kruslim/racing-telemetry-visualiser)"
TILE_DEG = 0.015
ATTRIBUTION = (
    "Map data (c) OpenStreetMap contributors, ODbL 1.0 "
    "(https://www.openstreetmap.org/copyright)"
)


@dataclass(frozen=True)
class Venue:
    key: str
    name: str
    country: str
    #: (south, west, north, east) in degrees, covering every layout iRacing runs here.
    bbox: tuple[float, float, float, float]
    #: iRacing ``WeekendInfo.TrackName`` prefixes seen for this venue (lower case).
    iracing_names: tuple[str, ...]
    #: The circuit is public road (Bathurst): its corners are mapped as ordinary
    #: named roads, so corner-like road names count too.
    public_roads: bool = False


VENUES: tuple[Venue, ...] = (
    Venue("spa", "Circuit de Spa-Francorchamps", "Belgium",
          (50.425, 5.955, 50.449, 5.995), ("spa",)),
    Venue("fuji", "Fuji Speedway", "Japan",
          (35.3636, 138.9166, 35.3789, 138.9390), ("fuji",)),
    Venue("lemans", "Circuit des 24 Heures du Mans", "France",
          (47.890, 0.185, 47.965, 0.235), ("lemans",)),
    Venue("nurburgring", "Nürburgring (GP and Nordschleife)", "Germany",
          (50.318, 6.918, 50.392, 7.012), ("nurburgring",)),
    Venue("monza", "Autodromo Nazionale Monza", "Italy",
          (45.605, 9.272, 45.636, 9.297), ("monza",)),
    Venue("silverstone", "Silverstone Circuit", "United Kingdom",
          (52.0621, -1.0256, 52.0816, -1.0062), ("silverstone",)),
    Venue("suzuka", "Suzuka Circuit", "Japan",
          (34.8381, 136.5206, 34.8494, 136.5442), ("suzuka",)),
    Venue("daytona", "Daytona International Speedway", "USA",
          (29.1748, -81.0785, 29.1941, -81.0620), ("daytona",)),
    Venue("watkinsglen", "Watkins Glen International", "USA",
          (42.3255, -76.9306, 42.3473, -76.9169), ("watkinsglen", "watkins")),
    Venue("roadatlanta", "Michelin Raceway Road Atlanta", "USA",
          (34.1357, -83.8215, 34.1564, -83.8091), ("roadatlanta",)),
    Venue("sebring", "Sebring International Raceway", "USA",
          (27.4471, -81.3639, 27.4597, -81.3456), ("sebring",)),
    Venue("redbullring", "Red Bull Ring", "Austria",
          (47.2184, 14.7516, 47.2281, 14.7717), ("spielberg", "redbullring")),
    Venue("barcelona", "Circuit de Barcelona-Catalunya", "Spain",
          (41.5621, 2.2513, 41.5755, 2.2650), ("barcelona", "catalunya")),
    Venue("imola", "Autodromo Internazionale Enzo e Dino Ferrari", "Italy",
          (44.3357, 11.7010, 44.3463, 11.7261), ("imola",)),
    Venue("zandvoort", "Circuit Zandvoort", "Netherlands",
          (52.3831, 4.5355, 52.3925, 4.5543), ("zandvoort",)),
    Venue("bathurst", "Mount Panorama Circuit", "Australia",
          (-33.4620, 149.5460, -33.4340, 149.5660), ("bathurst",), public_roads=True),
    Venue("phillipisland", "Phillip Island Grand Prix Circuit", "Australia",
          (-38.5090, 145.2250, -38.4920, 145.2450), ("phillipisland",)),
    Venue("hockenheim", "Hockenheimring", "Germany",
          (49.3248, 8.5632, 49.3349, 8.5858), ("hockenheim",)),
    Venue("hungaroring", "Hungaroring", "Hungary",
          (47.5754, 19.2407, 47.5894, 19.2600), ("hungaroring",)),
    Venue("misano", "Misano World Circuit", "Italy",
          (43.9574, 12.6759, 43.9671, 12.6916), ("misano",)),
    Venue("mugello", "Autodromo del Mugello", "Italy",
          (43.9899, 11.3634, 44.0092, 11.3799), ("mugello",)),
    Venue("paulricard", "Circuit Paul Ricard", "France",
          (43.2464, 5.7775, 43.2589, 5.8050), ("lecastellet", "paulricard")),
    Venue("portimao", "Autódromo Internacional do Algarve", "Portugal",
          (37.2232, -8.6331, 37.2375, -8.6227), ("algarve", "portimao")),
    Venue("brandshatch", "Brands Hatch", "United Kingdom",
          (51.3519, 0.2525, 51.3631, 0.2695), ("brandshatch",)),
    Venue("lagunaseca", "WeatherTech Raceway Laguna Seca", "USA",
          (36.5794, -121.7584, 36.5892, -121.7484), ("lagunaseca",)),
    Venue("cota", "Circuit of the Americas", "USA",
          (30.1217, -97.6501, 30.1495, -97.6166), ("austin", "cota")),
    Venue("vir", "Virginia International Raceway", "USA",
          (36.5494, -79.2168, 36.5756, -79.1938), ("virginia", "vir")),
    Venue("interlagos", "Autódromo José Carlos Pace (Interlagos)", "Brazil",
          (-23.7073, -46.7012, -23.6961, -46.6916), ("interlagos",)),
    Venue("kyalami", "Kyalami Grand Prix Circuit", "South Africa",
          (-26.0015, 28.0592, -25.9925, 28.0760), ("kyalami",)),
    Venue("okayama", "Okayama International Circuit", "Japan",
          (34.9092, 134.2175, 34.9216, 134.2250), ("okayama",)),
    Venue("donington", "Donington Park", "United Kingdom",
          (52.8222, -1.3960, 52.8337, -1.3629), ("donington",)),
    Venue("limerock", "Lime Rock Park", "USA",
          (41.9250, -73.3903, 41.9308, -73.3751), ("limerock",)),
    Venue("midohio", "Mid-Ohio Sports Car Course", "USA",
          (40.6808, -82.6456, 40.6953, -82.6272), ("midohio",)),
)

#: A raceway way whose name matches is the pit lane, not a corner.
PIT_RE = re.compile(r"\b(pit|pits|pitlane|boxengasse|boxen|corsia box|voie des stands|"
                    r"stands|pitstraat)\b|pit ?lane|pit ?(in|out|entry|exit)", re.I)
#: Raceway ways that are not part of a circuit layout.
SKIP_RE = re.compile(r"kart|paddock|access|zufahrt|anbindung|service|drift|skid|"
                     r"rally|dirt|oval|moto|test track|handling|offroad|supermoto|"
                     # names of whole layouts or venues, not of a corner
                     r"circuit|course|speedway|raceway|layout|grand prix|international|"
                     r"autodromo|autódromo|ring$|^nürburgring|サーキット|コース|ドリフト|"
                     r"go cart|tout-terrain|glisse|terrain d|^stand$|racetrack|short track|"
                     r"flat track|cutting half|connection to|porsche cent|park$|long lap", re.I)
#: A named node or public road that is a corner (or a named straight) by its name.
CORNERISH_RE = re.compile(r"corner|curve|bend|hairpin|kurve|curva|virage|chicane|esses|"
                          r"elbow|dipper|chase|skyline|cutting|straight|turn \d", re.I)
#: Tags that make a corner-named node something else (a toilet block, a car park...).
NOT_A_CORNER_TAGS = ("amenity", "shop", "tourism", "building", "parking", "public_transport",
                     "railway", "office", "craft", "emergency")
#: A named point is a label dropped near the corner, so allow it more slack.
POINT_RADIUS_M = 60.0
#: Named straights: kept, but marked so a corner is not named after one.
STRAIGHT_RE = re.compile(r"straight|gerade|rettifilo|ligne droite|hunaudi|mulsanne|"
                         r"rechte|recta|reta|kemmel|back stretch|front stretch|döttinger", re.I)


def _tiles(bbox: tuple[float, float, float, float]):
    s, w, n, e = bbox
    lat = s
    while lat < n - 1e-9:
        lon = w
        top = min(n, lat + TILE_DEG)
        while lon < e - 1e-9:
            right = min(e, lon + TILE_DEG)
            yield (round(lat, 5), round(lon, 5), round(top, 5), round(right, 5))
            lon = right
        lat = top


def _fetch_tile(tile, offline: bool) -> bytes | None:
    s, w, n, e = tile
    path = CACHE_DIR / f"{s}_{w}_{n}_{e}.osm"
    if path.exists():
        return path.read_bytes()
    if offline:
        return None
    req = urllib.request.Request(API.format(s=s, w=w, n=n, e=e),
                                 headers={"User-Agent": USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
            break
        except OSError as exc:
            print(f"  tile {tile} failed ({exc}), retrying", file=sys.stderr)
            time.sleep(2 ** (attempt + 1))
    else:
        raise RuntimeError(f"could not fetch tile {tile}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    time.sleep(1.0)  # be polite to the public API
    return body


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def _simplify(points: list[tuple[float, float]], spacing_m: float = 8.0):
    """Drop points closer than ``spacing_m`` to the last kept one (keeps the ends)."""
    if len(points) <= 2:
        return points
    kept = [points[0]]
    for p in points[1:-1]:
        if _haversine(kept[-1], p) >= spacing_m:
            kept.append(p)
    kept.append(points[-1])
    return kept


def _norm(name: str) -> str:
    """Spelling key: "Murray's Corner" and "Murrays Corner" are one corner."""
    return re.sub(r"[^0-9a-z]+", "", name.lower())


def _length(points) -> float:
    return sum(_haversine(a, b) for a, b in zip(points, points[1:], strict=False))


def build_venue(venue: Venue, offline: bool) -> dict | None:
    nodes: dict[str, tuple[float, float]] = {}
    named_nodes: dict[str, dict[str, str]] = {}
    ways: dict[str, tuple[list[str], dict[str, str]]] = {}
    missing = 0
    for tile in _tiles(venue.bbox):
        body = _fetch_tile(tile, offline)
        if body is None:
            missing += 1
            continue
        root = ET.fromstring(body)
        for el in root:
            if el.tag == "node":
                nodes[el.get("id")] = (float(el.get("lat")), float(el.get("lon")))
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags.get("name") and not any(k in tags for k in NOT_A_CORNER_TAGS):
                    named_nodes[el.get("id")] = tags
            elif el.tag == "way":
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                road_corner = (venue.public_roads and tags.get("highway")
                               and CORNERISH_RE.search(tags.get("name") or ""))
                if tags.get("highway") == "raceway" or road_corner:
                    ways[el.get("id")] = ([nd.get("ref") for nd in el.findall("nd")], tags)
    if missing:
        print(f"  {venue.key}: {missing} tiles not cached, skipped", file=sys.stderr)
        return None

    segments: dict[str, dict] = {}
    pit_lanes: list[dict] = []
    for way_id, (refs, tags) in sorted(ways.items(), key=lambda kv: int(kv[0])):
        points = [nodes[r] for r in refs if r in nodes]
        if len(points) < 2:
            continue
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        is_pit = (PIT_RE.search(name) is not None or tags.get("raceway") == "pit_lane"
                  or tags.get("service") == "pit_lane")
        if is_pit:
            pit_lanes.append({"osm_way": int(way_id), "name": name or None,
                              "points": [list(p) for p in _simplify(points)]})
            continue
        if (not name or tags.get("sport") == "karting" or SKIP_RE.search(name)
                or not re.search(r"[A-Za-z0-9]", name)):  # untranslated venue attractions
            continue
        seg = segments.setdefault(_norm(name), {
            "name": name,
            "kind": "straight" if STRAIGHT_RE.search(name) else "corner",
            "ref": tags.get("ref"),
            "osm_ways": [],
            "polylines": [],
            "radius_m": None,
        })
        seg["osm_ways"].append(int(way_id))
        seg["polylines"].append([[round(a, 7), round(b, 7)] for a, b in _simplify(points)])
        if tags.get("ref") and not seg["ref"]:
            seg["ref"] = tags["ref"]

    # Corner names mapped as a bare labelled point (Phillip Island) rather than a way.
    for node_id, tags in sorted(named_nodes.items(), key=lambda kv: int(kv[0])):
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        if _norm(name) in segments or not CORNERISH_RE.search(name) or SKIP_RE.search(name):
            continue
        lat, lon = nodes[node_id]
        segments[_norm(name)] = {
            "name": name,
            "kind": "straight" if STRAIGHT_RE.search(name) else "corner",
            "ref": tags.get("ref"),
            "osm_nodes": [int(node_id)],
            "polylines": [[[round(lat, 7), round(lon, 7)]]],
            "radius_m": POINT_RADIUS_M,
        }

    for seg in segments.values():
        seg["length_m"] = round(sum(_length(p) for p in seg["polylines"]), 1)
    s, w, n, e = venue.bbox
    return {
        "key": venue.key,
        "name": venue.name,
        "country": venue.country,
        "iracing_names": list(venue.iracing_names),
        "center": [round((s + n) / 2, 6), round((w + e) / 2, 6)],
        "bbox": list(venue.bbox),
        "source": "OpenStreetMap highway=raceway ways inside bbox",
        "attribution": ATTRIBUTION,
        "segments": sorted(segments.values(), key=lambda s: s["name"].lower()),
        "pit_lanes": pit_lanes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("venues", nargs="*", help="venue keys (default: all)")
    parser.add_argument("--offline", action="store_true", help="use cached tiles only")
    args = parser.parse_args(argv)
    wanted = set(args.venues) or {v.key for v in VENUES}
    unknown = wanted - {v.key for v in VENUES}
    if unknown:
        parser.error(f"unknown venues: {sorted(unknown)}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for venue in VENUES:
        if venue.key not in wanted:
            continue
        print(f"{venue.key}: {venue.name}")
        data = build_venue(venue, args.offline)
        if data is None:
            continue
        corners = [s["name"] for s in data["segments"] if s["kind"] == "corner"]
        print(f"  {len(corners)} named corners, "
              f"{len(data['segments']) - len(corners)} named straights, "
              f"{len(data['pit_lanes'])} pit lane ways")
        path = OUT_DIR / f"{venue.key}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
