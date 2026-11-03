#!/usr/bin/env bash
# Builds the tiles on this computer and publishes them on GitHub Pages.
#
# QLever refuses GitHub's servers (HTTP 403), so the build cannot run in Actions:
# run this weekly, or whenever the data should be fresher. It needs Python 3 and
# the GitHub CLI (`gh`) signed in to an account that can push to this repo.
#
#     ./publish.sh
set -euo pipefail
cd "$(dirname "$0")"

rm -rf site
python3 -u fetch-osm-places.py --out site
tar -czf site.tar.gz -C site .

# the tiles travel as an asset of the "tiles" release, replaced each time
if ! gh release view tiles >/dev/null 2>&1; then
  gh release create tiles --title 'Tiles' \
    --notes 'site.tar.gz is the latest build, deployed to GitHub Pages by the OSM data workflow. Data © OpenStreetMap contributors, ODbL 1.0.'
fi
gh release upload tiles site.tar.gz --clobber
gh workflow run osm-data.yml
echo 'Uploaded; GitHub Pages updates in a minute or two: gh run list --workflow osm-data.yml'
