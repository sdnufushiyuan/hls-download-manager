# Changelog

## Unreleased

- Diagnose QA TLS failures by request stage and bounded exception codes; retry explicit transient TLS failures with fresh process-local connections while preserving certificate verification, checkpoint contracts and finite retry limits.

- Preserve storage-recovery exceptions through the tile pool; log safe HTTP categories and apply bounded retries to explicitly identified EE compute failures while retaining terminal input/authentication protections.

- Label active months with complete download hints as checkpoint verification/reuse; avoid presenting every active monthly process as fresh downloading.

- Retry verified NAS atomic replacements with bounded backoff when CIFS readers temporarily deny rename; preserve mount checks and distinguish storage exit 76 from terminal errors. Resume the same monthly task and let systemd retry exhausted temporary failures.

- Add a read-only Chinese terminal monitor for master/node1/node2, visible processes and commands across users, services, resource usage and separate archive/QA/daily progress, with search, paging and credential redaction.

- Keep transient original-QA transfer failures within the current chunk with bounded backoff, refreshed download URLs and safe numeric HTTP diagnostics; explicitly test proxy routing despite environment bypass settings.

- Support explicit, reasoned per-region month exclusions consistently across download-readiness checks, original-QA history and daily output; preserve the original frozen inventory and source data.

- Add a resumable per-region supervisor: verify a complete region, acquire original daily Fmask in serial fixed-grid chunks, build its complete-history water mask, and deliver L30/S30/FUSED without waiting for other regions.
- Isolate regional failures, preserve frozen source inventory, bind completed-region receipts to sources/history, and provide an optional systemd user service.

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
