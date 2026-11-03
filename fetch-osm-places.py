"""Builds the bike-service tiles the Where Is Some Bike? app loads instead of asking Overpass from every phone.

For every city with bike share (CityBikes), takes repair stands, bike shops, drinking
water and bike parking from OpenStreetMap and writes them as 0.1° tiles, the same grid
the app caches on the phone (its src/lib/places.ts).

The places come from QLever (qlever.dev, University of Freiburg), which answers SPARQL
over the whole OSM planet: one query per kind returns every one in the world in under
a minute. Overpass took minutes per city and Geofabrik's extracts (~50 GB for all the
cities) came down too slowly for one GitHub job. Tags are kept raw and each element
carries its bounding-box centre like Overpass's `out center`, so the app parses these
tiles and Overpass answers the same way.

QLever refuses GitHub's servers (HTTP 403), so this runs on a computer: publish.sh
builds the tiles, uploads them and starts .github/workflows/osm-data.yml, which
publishes them on GitHub Pages, where the app reads them (`expo.extra.osmDataUrl` in
its app.json). Tiles missing here, the app still asks Overpass for.

The data is © OpenStreetMap contributors under the ODbL; the folder carries that
notice so it travels with the files.

    python3 fetch-osm-places.py [--out site] [--only bratislava,zilina]
"""
import argparse
import csv
import io
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
QLEVER = 'https://qlever.dev/api/osm-planet'
# last week's tiles, published again when QLever cannot answer
PUBLISHED = 'https://adamusso.github.io/WhereIsSomeBike-data/places/v1'
USER_AGENT = 'WhereIsSomeBike/1.0 data build (+https://github.com/ADAMUSSO/WhereIsSomeBike-data)'
TILE = 0.1  # same as TILE in the app's src/lib/places.ts
RADIUS_KM = 10  # AREA_KM in the app's src/lib/cities.ts: what counts as "in town"
TRIES = 3
BACKOFF_S = 60
# the same four kinds as kindOf() in the app
KINDS = [('amenity', 'bicycle_repair_station'), ('shop', 'bicycle'), ('amenity', 'drinking_water'),
         ('amenity', 'bicycle_parking')]
# everything the app's src/lib/places.ts reads; the rest would only make the tiles bigger
KEEP_TAGS = [
    'amenity', 'shop', 'name', 'access', 'capacity', 'covered', 'seasonal', 'opening_hours',
    'service:bicycle:pump', 'service:bicycle:tools', 'service:bicycle:stand', 'service:bicycle:repair',
]
ATTRIBUTION = '© OpenStreetMap contributors'
LICENSE = 'Open Database License (ODbL) 1.0 – https://opendatacommons.org/licenses/odbl/1-0/'

Tile = tuple[int, int]


def fold(s: str) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode().lower()


def get_json(url: str):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def tiles_around(lat: float, lng: float) -> set[Tile]:
    """Tiles within RADIUS_KM of a point – Math.floor(lat / TILE) like the app, so keys match."""
    dlat = RADIUS_KM / 111.32
    dlng = RADIUS_KM / (111.32 * max(0.2, math.cos(math.radians(lat))))
    ys = range(math.floor((lat - dlat) / TILE), math.floor((lat + dlat) / TILE) + 1)
    xs = range(math.floor((lng - dlng) / TILE), math.floor((lng + dlng) / TILE) + 1)
    return {(y, x) for y in ys for x in xs}


def query(key: str, value: str) -> list[dict[str, str]]:
    """Every OSM node, way and relation with key=value in the world, with its centre and tags."""
    tag_vars = [f't{i}' for i in range(len(KEEP_TAGS))]
    optionals = '\n'.join(
        f'  OPTIONAL {{ ?osm <https://www.openstreetmap.org/wiki/Key:{k}> ?{v} }}' for k, v in zip(KEEP_TAGS, tag_vars))
    sparql = f"""PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX geof: <http://www.opengis.net/def/function/geosparql/>
SELECT ?osm ?lat ?lon {' '.join('?' + v for v in tag_vars)} WHERE {{
  ?osm <https://www.openstreetmap.org/wiki/Key:{key}> "{value}" ; geo:hasGeometry/geo:asWKT ?g .
  BIND((geof:minY(?g) + geof:maxY(?g)) / 2 AS ?lat)
  BIND((geof:minX(?g) + geof:maxX(?g)) / 2 AS ?lon)
{optionals}
}}"""
    body = urllib.parse.urlencode({'query': sparql}).encode()
    for attempt in range(TRIES):
        req = urllib.request.Request(QLEVER, data=body, headers={'User-Agent': USER_AGENT, 'Accept': 'text/csv'})
        try:
            with urllib.request.urlopen(req, timeout=900) as res:
                rows = list(csv.DictReader(io.TextIOWrapper(res, encoding='utf-8', newline='')))
            for row in rows:
                row.update({k: row.pop(v) for k, v in zip(KEEP_TAGS, tag_vars)})
            return rows
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f'    {e}, waiting {BACKOFF_S} s')
            time.sleep(BACKOFF_S)
    raise RuntimeError(f'QLever gave up on {key}={value}')


def keep_published(tile: Tile, tile_dir: Path) -> bool:
    """Copies last week's tile from the live site; False when it was never built."""
    y, x = tile
    req = urllib.request.Request(f'{PUBLISHED}/{y}_{x}.json', headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            (tile_dir / f'{y}_{x}.json').write_bytes(res.read())
        return True
    except (urllib.error.URLError, TimeoutError):
        return False  # the app asks Overpass for it


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(Path(__file__).resolve().parent / 'site'))
    ap.add_argument('--only', help='comma-separated network ids or city names, for a quick test')
    args = ap.parse_args()
    started = time.time()

    out = Path(args.out)
    tile_dir = out / 'places' / 'v1'
    tile_dir.mkdir(parents=True, exist_ok=True)

    networks = get_json(NETWORKS)['networks']
    if args.only:
        wanted = {fold(w.strip()) for w in args.only.split(',')}
        networks = [n for n in networks if n['id'] in wanted or fold(n['location']['city']) in wanted]
    needed: set[Tile] = set()
    for n in networks:
        needed |= tiles_around(n['location']['latitude'], n['location']['longitude'])
    print(f'{len(networks)} cities, {len(needed)} tiles')

    tiles: dict[Tile, dict[str, dict]] = {t: {} for t in needed}
    try:
        for key, value in KINDS:
            t0 = time.time()
            rows = query(key, value)
            added = 0
            for row in rows:
                tags = {k: row[k] for k in KEEP_TAGS if row[k]}
                if tags.get('access') in ('private', 'no'):
                    continue  # the app hides these anyway
                lat, lon = float(row['lat']), float(row['lon'])
                # the same float division as Math.floor(lat / TILE) in the app
                k = (math.floor(lat / TILE), math.floor(lon / TILE))
                if k not in tiles:
                    continue
                # https://www.openstreetmap.org/node/123 → node, 123
                kind, num = row['osm'].rsplit('/', 2)[-2:]
                # a shop that is also a repair stand comes in both queries: keep one
                tiles[k][f'{kind}/{num}'] = {
                    'type': kind, 'id': int(num), 'lat': round(lat, 6), 'lon': round(lon, 6), 'tags': tags}
                added += 1
            print(f'{key}={value}: {len(rows)} in the world, {added} in town, {time.time() - t0:.0f} s')
    except RuntimeError as e:
        # half the kinds would hide the rest from the app's Overpass fallback: publish last week's
        print(f'::warning::{e}, keeping last week\'s tiles')
        kept = sum(keep_published(t, tile_dir) for t in needed)
        print(f'kept {kept}/{len(needed)} tiles')
    else:
        for (y, x), places in tiles.items():
            (tile_dir / f'{y}_{x}.json').write_text(
                json.dumps({'elements': list(places.values())}, separators=(',', ':')))

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
    print(f'{len(list(tile_dir.glob("*.json")))} tiles -> {tile_dir} in {time.time() - started:.0f} s')


if __name__ == '__main__':
    main()
