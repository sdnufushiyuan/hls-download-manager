# Changelog

## Unreleased

- Add windowed, verified daily GeoTIFF generation: L30 by UTC date/WRS path-row and S30 by UTC date/MGRS tile.
- Add provenance metadata caching, same-observation dedup recovery and an optional low-priority mosaic service.

- Add smallest remaining region scheduling with a completion barrier across all nodes.
- Preserve all cached months and validate them before handing a region to downstream work.
- Show region readiness in the progress panel; default new service deployments to this policy.

## 0.1.0 — 2026-10-19

- Extract the HLS v2 OPTRAM downloader and bounded tile parallelism.
- Preserve checksum, grid, QA and resume validation.
- Include monthly scheduling across master/node1/node2 and finite network retries.
- Include live progress, committed-block throughput and rolling completion estimates.
- Add native proxy failover and conservative, verified small-tile route selection.
- Replace private deployment details with configuration and publish offline tests.
