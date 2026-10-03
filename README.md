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

## Branch policy

`osm-runtime-data` is a distribution branch for the main project. It is kept
separate from the source branches on purpose:

- Do not merge this branch into a source or development branch.
- Do not add application source code here; keep the branch limited to the
  runtime archive and its license/metadata documentation.
- `dev.py` pins both this branch URL and the archive SHA256. A package update
  must update the archive, this README, `dev.py`, and the main project's OSM
  documentation together.
- Keep the ODbL notice and OpenStreetMap attribution with every replacement
  package, and do not rewrite this branch's history because clients download
  the archive through its branch URL.

For a new snapshot, build and validate the runtime package first, calculate its
SHA256, then make the package and corresponding source/documentation changes in
reviewable commits. The main project should only point to the new package after
the raw branch URL has been tested end to end.
