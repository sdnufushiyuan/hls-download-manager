# Changelog

## Unreleased

- Add a full-network completion gate and a verified, fixed-grid historical water mask required before daily processing.
- Add two-consecutive-valid-UTC-date water exclusion, skipping clouds/missing dates, resetting on land and deduplicating same-day sensors; apply the static mask to every product/date and fused provenance.

- Add product-first daily folders (`--layout product-region`) while retaining the existing layout and scientific recipe by default.
- Add offline sample model-domain QC, checksum-bound reviewed exclusions, whole-pixel re-fusion and optional core/full-range NDVI–STR plots.

- Default daily mosaics to full-region, fixed-grid UTC-day images across HLS tiles, keeping L30/S30 separate without metadata network requests.
- Add optional same-day L30-first/S30-fill fusion with whole six-band pixels and verified source/acquisition-time rasters.

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
