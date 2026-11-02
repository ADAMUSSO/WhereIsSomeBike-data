"""Builds the bike-service tiles the Where Is Some Bike? app loads instead of asking Overpass from every phone.

For every city with bike share (CityBikes), downloads repair stands, bike shops,
drinking water and bike parking from OpenStreetMap through Overpass – one city at
a time with pauses, as the Overpass usage policy asks – and writes them as 0.1°
tiles, the same grid the app caches on the phone (its src/lib/places.ts). Tags are
kept raw, so the app parses these tiles and Overpass answers the same way.

.github/workflows/osm-data.yml runs this daily and publishes the output on GitHub
Pages, where the app reads it (`expo.extra.osmDataUrl` in its app.json). Tiles
missing here, the app still asks Overpass for.

All ~800 cities take longer than one GitHub job may run while Overpass is busy, so a
run refreshes the cities built longest ago until --budget-min is up and remembers in
--state when each was built; the workflow keeps the tiles and that file between runs.

The data is © OpenStreetMap contributors under the ODbL; the folder carries that
notice so it travels with the files.

    python3 fetch-osm-places.py [--out site] [--only bratislava,zilina] [--state built.json] [--budget-min 300]
"""
import argparse
import json
import math
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NETWORKS = 'https://api.citybik.es/v2/networks?fields=id,location'
SERVERS = ['https://overpass-api.de/api/interpreter', 'https://overpass.private.coffee/api/interpreter']
# last week's tiles, kept for a city Overpass fails on
PUBLISHED = 'https://adamusso.github.io/WhereIsSomeBike-data/places/v1'
USER_AGENT = 'WhereIsSomeBike/1.0 data build (+https://github.com/ADAMUSSO/WhereIsSomeBike-data)'
TILE = 0.1  # same as TILE in the app's src/lib/places.ts
RADIUS_KM = 10  # AREA_KM in the app's src/lib/cities.ts: what counts as "in town"
PAUSE_S = 3
BACKOFF_S = 60
# at most ~20 minutes on one city, so a run never overshoots its budget by much
TRIES = 4
# a city built less than this long ago is skipped, so each one is refreshed about weekly
MAX_AGE_S = 6 * 24 * 3600
# after this many cities in a row fail, Overpass is down: keep last week's tiles for the rest
MAX_FAILS_IN_ROW = 5
# everything the app's src/lib/places.ts reads; the rest would only make the tiles bigger
KEEP_TAGS = {
    'amenity', 'shop', 'name', 'access', 'capacity', 'covered', 'seasonal', 'opening_hours',
    'service:bicycle:pump', 'service:bicycle:tools', 'service:bicycle:stand', 'service:bicycle:repair',
}
ATTRIBUTION = '© OpenStreetMap contributors'
LICENSE = 'Open Database License (ODbL) 1.0 – https://opendatacommons.org/licenses/odbl/1-0/'


def fold(s: str) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode().lower()


def get_json(url: str):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def tiles_around(lat: float, lng: float) -> set[tuple[int, int]]:
    """Tiles within RADIUS_KM of a point – Math.floor(lat / TILE) like the app, so keys match."""
    dlat = RADIUS_KM / 111.32
    dlng = RADIUS_KM / (111.32 * max(0.2, math.cos(math.radians(lat))))
    ys = range(math.floor((lat - dlat) / TILE), math.floor((lat + dlat) / TILE) + 1)
    xs = range(math.floor((lng - dlng) / TILE), math.floor((lng + dlng) / TILE) + 1)
    return {(y, x) for y in ys for x in xs}


def overpass(bbox: str) -> list[dict]:
    ql = (
        '[out:json][timeout:180];('
        f'nwr["amenity"="bicycle_repair_station"]({bbox});nwr["shop"="bicycle"]({bbox});'
        f'nwr["amenity"="drinking_water"]({bbox});nwr["amenity"="bicycle_parking"]({bbox});'
        ');out center tags;'
    )
    body = urllib.parse.urlencode({'data': ql}).encode()
    for attempt in range(TRIES):
        url = SERVERS[attempt % len(SERVERS)]
        req = urllib.request.Request(url, data=body, headers={'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=240) as res:
                return json.load(res)['elements']
        except urllib.error.HTTPError as e:
            # 429 too many requests, 504 server full: wait as the policy asks
            print(f'    {url.split("/")[2]}: HTTP {e.code}, waiting {BACKOFF_S} s')
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f'    {url.split("/")[2]}: {e}, waiting {BACKOFF_S} s')
        time.sleep(BACKOFF_S)
    raise RuntimeError(f'Overpass gave up on {bbox}')


def keep_published(block: set[tuple[int, int]], tile_dir: Path) -> int:
    """Keeps a block's tiles from the last run, or copies them from the live site; returns how many it had."""
    kept = 0
    for y, x in block:
        if (tile_dir / f'{y}_{x}.json').exists():
            kept += 1
            continue
        req = urllib.request.Request(f'{PUBLISHED}/{y}_{x}.json', headers={'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                (tile_dir / f'{y}_{x}.json').write_bytes(res.read())
            kept += 1
        except (urllib.error.URLError, TimeoutError):
            pass  # never built: the app asks Overpass for it
    return kept


def compact(el: dict) -> dict | None:
    tags = {k: v for k, v in (el.get('tags') or {}).items() if k in KEEP_TAGS}
    if tags.get('access') in ('private', 'no'):
        return None  # the app hides these anyway
    lat = el.get('lat', el.get('center', {}).get('lat'))
    lon = el.get('lon', el.get('center', {}).get('lon'))
    if lat is None or lon is None:
        return None
    return {'type': el['type'], 'id': el['id'], 'lat': round(lat, 6), 'lon': round(lon, 6), 'tags': tags}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(Path(__file__).resolve().parent / 'site'))
    ap.add_argument('--only', help='comma-separated network ids or city names, for a quick test')
    ap.add_argument('--state', help='JSON file remembering when each city was built, kept between runs')
    ap.add_argument('--budget-min', type=float, help='start no new city after this many minutes')
    args = ap.parse_args()
    deadline = time.time() + args.budget_min * 60 if args.budget_min else math.inf

    out = Path(args.out)
    tile_dir = out / 'places' / 'v1'
    tile_dir.mkdir(parents=True, exist_ok=True)
    state = Path(args.state) if args.state else None
    built: dict[str, float] = json.loads(state.read_text()) if state and state.exists() else {}

    networks = get_json(NETWORKS)['networks']
    if args.only:
        wanted = {fold(w.strip()) for w in args.only.split(',')}
        networks = [n for n in networks if n['id'] in wanted or fold(n['location']['city']) in wanted]
    # never-built cities first, then the stalest
    networks.sort(key=lambda n: built.get(n['id'], 0))
    stale = [n for n in networks if built.get(n['id'], 0) < time.time() - MAX_AGE_S]
    print(f'{len(stale)} of {len(networks)} cities to refresh')

    done: set[tuple[int, int]] = set()
    failed: list[str] = []
    fails_in_row = 0
    # nearby systems share their tiles: one Overpass request per still-missing block
    for i, n in enumerate(stale, 1):
        if time.time() > deadline:
            print(f'Out of time, {len(stale) - i + 1} cities wait for the next run')
            break
        loc = n['location']
        missing = tiles_around(loc['latitude'], loc['longitude']) - done
        if not missing:
            built[n['id']] = time.time()
            continue
        south, north = min(y for y, _ in missing), max(y for y, _ in missing) + 1
        west, east = min(x for _, x in missing), max(x for _, x in missing) + 1
        block = {(y, x) for y in range(south, north) for x in range(west, east)} - done
        bbox = f'{south * TILE:.4f},{west * TILE:.4f},{north * TILE:.4f},{east * TILE:.4f}'
        print(f'[{i}/{len(stale)}] {loc["city"]} ({n["id"]}): {len(block)} tiles')

        # one city failing must not cost the whole week's update
        elements = None
        if fails_in_row < MAX_FAILS_IN_ROW:
            try:
                elements = overpass(bbox)
            except RuntimeError:
                pass
        if elements is None:
            kept = keep_published(block, tile_dir)
            failed.append(f'{loc["city"]} ({n["id"]})')
            fails_in_row += 1
            # shows up as a warning on the workflow run
            print(f'::warning::No Overpass data for {loc["city"]} ({n["id"]}), kept {kept}/{len(block)} old tiles')
            done |= block
            continue
        fails_in_row = 0

        tiles: dict[tuple[int, int], list[dict]] = {k: [] for k in block}
        for el in elements:
            c = compact(el)
            # the same float division as Math.floor(lat / TILE) in the app
            k = c and (math.floor(c['lat'] / TILE), math.floor(c['lon'] / TILE))
            if k in tiles:
                tiles[k].append(c)
        for (y, x), elements in tiles.items():
            (tile_dir / f'{y}_{x}.json').write_text(json.dumps({'elements': elements}, separators=(',', ':')))
        done |= block
        built[n['id']] = time.time()
        if state:
            state.write_text(json.dumps(built))
        time.sleep(PAUSE_S)

    if state:
        state.write_text(json.dumps(built))
    (out / 'index.json').write_text(
        json.dumps(
            {
                'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'tiles': len(list(tile_dir.glob('*.json'))),
                'tileDegrees': TILE,
                'attribution': ATTRIBUTION,
                'license': LICENSE,
            },
            indent=1,
        )
        + '\n'
    )
    (out / 'LICENSE.txt').write_text(
        f'Bike services extracted from OpenStreetMap.\n\nData {ATTRIBUTION}, https://www.openstreetmap.org/copyright\n'
        f'Available under the {LICENSE}\n'
    )
    print(f'{len(list(tile_dir.glob("*.json")))} tiles -> {tile_dir}')
    if failed:
        print(f'{len(failed)} cities kept old tiles: {", ".join(failed)}')


if __name__ == '__main__':
    main()
