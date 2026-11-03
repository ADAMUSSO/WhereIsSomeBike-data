"""Builds the bike-service tiles the Where Is Some Bike? app loads instead of asking Overpass from every phone.

For every city with bike share (CityBikes), takes repair stands, bike shops, drinking
water and bike parking from the OpenStreetMap extracts Geofabrik publishes daily and
writes them as 0.1° tiles, the same grid the app caches on the phone (its
src/lib/places.ts). Only the smallest Geofabrik regions covering the cities are
downloaded, one at a time, and filtered with osmium-tool – no Overpass, so a busy
server cannot stall the build. Tags are kept raw and each element carries a centre
like Overpass's `out center`, so the app parses these tiles and Overpass answers the
same way.

.github/workflows/osm-data.yml runs this weekly and publishes the output on GitHub
Pages, where the app reads it (`expo.extra.osmDataUrl` in its app.json). Tiles
missing here, the app still asks Overpass for.

The data is © OpenStreetMap contributors under the ODbL; the folder carries that
notice so it travels with the files.

    python3 fetch-osm-places.py [--out site] [--work downloads] [--only bratislava,zilina] [--plan]
"""
import argparse
import json
import math
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NETWORKS = 'https://api.citybik.es/v2/networks?fields=id,location'
REGIONS = 'https://download.geofabrik.de/index-v1.json'
# last week's tiles, kept for a region whose download fails
PUBLISHED = 'https://adamusso.github.io/WhereIsSomeBike-data/places/v1'
USER_AGENT = 'WhereIsSomeBike/1.0 data build (+https://github.com/ADAMUSSO/WhereIsSomeBike-data)'
TILE = 0.1  # same as TILE in the app's src/lib/places.ts
RADIUS_KM = 10  # AREA_KM in the app's src/lib/cities.ts: what counts as "in town"
# points per tile side tested against the region outlines
SAMPLES = 3
TRIES = 3
BACKOFF_S = 60
# a download kept in --work is reused while younger than this (Geofabrik updates daily)
REUSE_S = 20 * 3600
FILTERS = ['nwr/amenity=bicycle_repair_station,drinking_water,bicycle_parking', 'nwr/shop=bicycle']
# everything the app's src/lib/places.ts reads; the rest would only make the tiles bigger
KEEP_TAGS = {
    'amenity', 'shop', 'name', 'access', 'capacity', 'covered', 'seasonal', 'opening_hours',
    'service:bicycle:pump', 'service:bicycle:tools', 'service:bicycle:stand', 'service:bicycle:repair',
}
ATTRIBUTION = '© OpenStreetMap contributors'
LICENSE = 'Open Database License (ODbL) 1.0 – https://opendatacommons.org/licenses/odbl/1-0/'

Tile = tuple[int, int]


def fold(s: str) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode().lower()


def request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={'User-Agent': USER_AGENT})


def get_json(url: str):
    with urllib.request.urlopen(request(url), timeout=60) as res:
        return json.load(res)


def tiles_around(lat: float, lng: float) -> set[Tile]:
    """Tiles within RADIUS_KM of a point – Math.floor(lat / TILE) like the app, so keys match."""
    dlat = RADIUS_KM / 111.32
    dlng = RADIUS_KM / (111.32 * max(0.2, math.cos(math.radians(lat))))
    ys = range(math.floor((lat - dlat) / TILE), math.floor((lat + dlat) / TILE) + 1)
    xs = range(math.floor((lng - dlng) / TILE), math.floor((lng + dlng) / TILE) + 1)
    return {(y, x) for y in ys for x in xs}


class Region:
    def __init__(self, feature: dict):
        p = feature['properties']
        self.id: str = p['id']
        self.parent: str | None = p.get('parent')
        self.url: str = p['urls']['pbf']
        # [[outer ring, hole, …], …] as (lng, lat) pairs
        self.polygons: list[list[list[list[float]]]] = feature['geometry']['coordinates']
        ring = [pt for poly in self.polygons for pt in poly[0]]
        self.bbox = (min(p[1] for p in ring), min(p[0] for p in ring), max(p[1] for p in ring), max(p[0] for p in ring))
        self.area = (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])
        self.centre = ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)

    def contains(self, lat: float, lng: float) -> bool:
        s, w, n, e = self.bbox
        if not (s <= lat <= n and w <= lng <= e):
            return False
        return any(inside(poly[0], lat, lng) and not any(inside(h, lat, lng) for h in poly[1:]) for poly in self.polygons)


def inside(ring: list[list[float]], lat: float, lng: float) -> bool:
    """Ray casting: does the ring of (lng, lat) points enclose the point?"""
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def regions_for(tiles: set[Tile], regions: list[Region]) -> dict[Tile, set[str]]:
    """The smallest regions covering each tile; empty when none does (the tile is left to the app)."""
    # a country's sub-regions cover all its land, so a point only the country (or continent)
    # covers is sea next to the coast – and must not pull in France's 5 GB
    leaves = [r for r in regions if r.id not in {o.parent for o in regions}]
    # groupings like "dach", "great-britain" or "us-midwest" repeat whole countries and win
    # the odd sea point between them; real regions enclose at most two (Brandenburg: Berlin)
    leaves = [r for r in leaves if sum(o.area < r.area and r.contains(*o.centre) for o in leaves) < 3]
    out: dict[Tile, set[str]] = {}
    for y, x in tiles:
        s, w = y * TILE, x * TILE
        near = [r for r in leaves if r.bbox[0] <= s + TILE and r.bbox[2] >= s and r.bbox[1] <= w + TILE and r.bbox[3] >= w]
        # outlines overlap at borders and around enclaves: the smallest one containing a point wins
        near.sort(key=lambda r: r.area)
        found: set[str] = set()
        for i in range(SAMPLES):
            for j in range(SAMPLES):
                lat, lng = s + (i + 0.5) * TILE / SAMPLES, w + (j + 0.5) * TILE / SAMPLES
                hit = next((r for r in near if r.contains(lat, lng)), None)
                if hit:
                    found.add(hit.id)
        out[(y, x)] = found
    return out


def download(url: str, dest: Path) -> None:
    if dest.exists() and time.time() - dest.stat().st_mtime < REUSE_S:
        return
    part = dest.with_suffix('.part')
    for attempt in range(TRIES):
        try:
            with urllib.request.urlopen(request(url), timeout=120) as res, part.open('wb') as f:
                shutil.copyfileobj(res, f, 1 << 20)
            part.rename(dest)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f'    {e}, waiting {BACKOFF_S} s')
            time.sleep(BACKOFF_S)
    raise RuntimeError(f'could not download {url}')


def osm_id(feature_id: str) -> tuple[str, int]:
    """osmium's type_id ids ("n12", "w34", "a69") back to OSM's type and id."""
    kind, num = feature_id[0], int(feature_id[1:])
    if kind == 'a':  # areas: way id × 2, relation id × 2 + 1
        return ('way', num // 2) if num % 2 == 0 else ('relation', num // 2)
    return {'n': 'node', 'w': 'way', 'r': 'relation'}[kind], num


def is_place(tags: dict) -> bool:
    """The same four kinds the app's kindOf() knows."""
    return tags.get('shop') == 'bicycle' or tags.get('amenity') in (
        'bicycle_repair_station', 'drinking_water', 'bicycle_parking')


def centre(geometry: dict) -> tuple[float, float]:
    """Middle of the bounding box, as Overpass's `out center` gives it; (lat, lon)."""
    pts: list[list[float]] = []

    def walk(c):
        if isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for x in c:
                walk(x)

    walk(geometry['coordinates'])
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


def extract(pbf: Path, work: Path, wanted: set[Tile], tiles: dict[Tile, dict[str, dict]]) -> int:
    """Filters one region and adds its places in the wanted tiles; returns how many."""
    filtered, seq = work / 'filtered.osm.pbf', work / 'places.geojsonseq'
    subprocess.run(['osmium', 'tags-filter', str(pbf), *FILTERS, '-o', str(filtered), '--overwrite', '--no-progress'], check=True)
    subprocess.run(
        ['osmium', 'export', str(filtered), '-f', 'geojsonseq', '-u', 'type_id', '-o', str(seq), '--overwrite',
         '--no-progress', '--format-option', 'print_record_separator=false'],
        check=True,
    )
    added = 0
    with seq.open() as f:
        for line in f:
            feature = json.loads(line)
            tags = {k: v for k, v in (feature.get('properties') or {}).items() if k in KEEP_TAGS}
            if not is_place(tags) or tags.get('access') in ('private', 'no'):
                continue  # untagged way nodes, or hidden by the app anyway
            lat, lon = centre(feature['geometry'])
            # the same float division as Math.floor(lat / TILE) in the app
            k = (math.floor(lat / TILE), math.floor(lon / TILE))
            if k not in wanted:
                continue
            kind, num = osm_id(feature['id'])
            # a closed way comes out as both a line and an area: keep one
            tiles[k][f'{kind}/{num}'] = {'type': kind, 'id': num, 'lat': round(lat, 6), 'lon': round(lon, 6), 'tags': tags}
            added += 1
    filtered.unlink()
    seq.unlink()
    return added


def keep_published(tile: Tile, tile_dir: Path) -> bool:
    """Copies last week's tile from the live site; False when it was never built."""
    y, x = tile
    try:
        with urllib.request.urlopen(request(f'{PUBLISHED}/{y}_{x}.json'), timeout=30) as res:
            (tile_dir / f'{y}_{x}.json').write_bytes(res.read())
        return True
    except (urllib.error.URLError, TimeoutError):
        return False  # the app asks Overpass for it


def size_of(url: str) -> int:
    """Download size from a one-byte range request."""
    req = request(url)
    req.add_header('Range', 'bytes=0-0')
    with urllib.request.urlopen(req, timeout=30) as res:
        return int(res.headers.get('Content-Range', '/0').split('/')[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(Path(__file__).resolve().parent / 'site'))
    ap.add_argument('--work', default=str(Path(__file__).resolve().parent / 'downloads'),
                    help='where extracts are downloaded; each is deleted after use unless --keep')
    ap.add_argument('--keep', action='store_true', help='keep the extracts, to rerun without downloading')
    ap.add_argument('--only', help='comma-separated network ids or city names, for a quick test')
    ap.add_argument('--plan', action='store_true', help='only list the regions and their download sizes')
    args = ap.parse_args()
    started = time.time()

    networks = get_json(NETWORKS)['networks']
    if args.only:
        wanted = {fold(w.strip()) for w in args.only.split(',')}
        networks = [n for n in networks if n['id'] in wanted or fold(n['location']['city']) in wanted]
    needed: set[Tile] = set()
    for n in networks:
        needed |= tiles_around(n['location']['latitude'], n['location']['longitude'])

    regions = [Region(f) for f in get_json(REGIONS)['features']]
    by_id = {r.id: r for r in regions}
    tile_regions = regions_for(needed, regions)
    region_tiles: dict[str, set[Tile]] = {}
    for t, ids in tile_regions.items():
        for rid in ids:
            region_tiles.setdefault(rid, set()).add(t)
    print(f'{len(networks)} cities, {len(needed)} tiles, {len(region_tiles)} regions '
          f'({time.time() - started:.0f} s)')

    if args.plan:
        total = 0
        for rid in sorted(region_tiles):
            size = size_of(by_id[rid].url)
            total += size
            print(f'  {rid:32} {size / 1e6:8.0f} MB  {len(region_tiles[rid])} tiles')
        print(f'{total / 1e9:.1f} GB to download')
        return

    out, work = Path(args.out), Path(args.work)
    tile_dir = out / 'places' / 'v1'
    tile_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    tiles: dict[Tile, dict[str, dict]] = {t: {} for t in needed}
    failed: set[str] = set()
    for i, rid in enumerate(sorted(region_tiles), 1):
        t0 = time.time()
        pbf = work / f'{rid}.osm.pbf'
        try:
            download(by_id[rid].url, pbf)
            mb = pbf.stat().st_size / 1e6
            added = extract(pbf, work, region_tiles[rid], tiles)
            print(f'[{i}/{len(region_tiles)}] {rid}: {mb:.0f} MB, {added} places, {time.time() - t0:.0f} s')
        except (RuntimeError, subprocess.CalledProcessError, OSError) as e:
            failed.add(rid)
            # shows up as a warning on the workflow run
            print(f'::warning::{rid}: {e}')
        finally:
            if not args.keep:
                pbf.unlink(missing_ok=True)

    kept = 0
    for t, places in tiles.items():
        if not tile_regions[t]:
            continue  # no extract covers it: the app asks Overpass
        if tile_regions[t] & failed:
            # half a tile would hide the rest from the app's Overpass fallback
            kept += keep_published(t, tile_dir)
            continue
        y, x = t
        (tile_dir / f'{y}_{x}.json').write_text(json.dumps({'elements': list(places.values())}, separators=(',', ':')))

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
    print(f'{len(list(tile_dir.glob("*.json")))} tiles -> {tile_dir} in {(time.time() - started) / 60:.0f} min')
    if failed:
        print(f'{len(failed)} regions failed, kept {kept} old tiles: {", ".join(sorted(failed))}')


if __name__ == '__main__':
    main()
