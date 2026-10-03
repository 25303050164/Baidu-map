# OSM Shanghai Runtime Package

This branch hosts the versioned runtime data package used by the main
Baidu-map project. The application downloads the ZIP file and verifies its
SHA256 before extracting it into `data/osm/`.

- Package: `osm-shanghai-geofabrik-260913-runtime.zip`
- ZIP SHA256: `51b45539592de54ae9d78d5c7e556140711af3a7fd5f004fc83263b15f898d94`
- OSM snapshot: `geofabrik-shanghai-260913`
- Source: https://download.geofabrik.de/asia/china/shanghai-latest.osm.pbf
- Attribution: Copyright OpenStreetMap contributors
- License: https://opendatacommons.org/licenses/odbl/1-0/

The ZIP contains the prepared walking graph cache, coverage boundary, and
OSM-derived Hybrid layers. It is a runtime artifact; rebuilding from the raw
PBF is an advanced workflow documented by the main project.
