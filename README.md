# Where Is Some Bike? – OpenStreetMap data

Bike services from [OpenStreetMap](https://www.openstreetmap.org) – repair stations, bike
shops, drinking water and bike parking – around every bike-share city listed by
[CityBikes](https://citybik.es), cut into 0.1° tiles for the Where Is Some Bike? Android app.
The app reads them here instead of asking the volunteer-run Overpass servers from every phone.

- Built weekly by [`fetch-osm-places.py`](fetch-osm-places.py) in
  [`.github/workflows/osm-data.yml`](.github/workflows/osm-data.yml) and published on GitHub Pages:
  `https://adamusso.github.io/WhereIsSomeBike-data/places/v1/<y>_<x>.json`, where
  `y = floor(lat / 0.1)` and `x = floor(lon / 0.1)`.
- [`index.json`](https://adamusso.github.io/WhereIsSomeBike-data/index.json) says when the tiles were built.

Each tile holds the raw OSM elements with the tags the app uses:

```json
{"elements":[{"type":"node","id":438496593,"lat":48.152248,"lon":17.141689,"tags":{"shop":"bicycle","name":"Green Bike"}}]}
```

Build locally (Python 3, no packages needed):

```bash
python3 fetch-osm-places.py --only bratislava,zilina
```

## License

The data is © OpenStreetMap contributors, available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
See <https://www.openstreetmap.org/copyright>.
