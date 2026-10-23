# Serial QA request batching

The original-QA supervisor can reduce remote requests without adding workers.
Use `--qa-batch-region CODE` for each explicitly selected region. Other regions
keep individual-window transfers. These runtime flags do not modify private
configuration, inventory, exclusion scopes or completed-region configuration
bindings. The archive downloader and shared proxy are separate processes.

Before requesting data, the supervisor verifies existing original-window
contracts, checksums and grids. It packs only missing, contiguous windows into
rectangles up to 4096 by 4096 pixels. Every rectangle is completely covered by
the selected windows: no holes, already committed windows or extra pixels are
requested. A one-band uint8 rectangle contains at most 16 MiB of pixels, below
Earth Engine's documented 32 MB request limit and 10000-pixel dimension limit.
One request runs at a time, using the same project, endpoint, explicit proxy and
certificate verification.

The image expression, nearest 30 m grid, original Fmask values, date grouping,
same-day priority and conflict handling are unchanged. Downloaded rectangles
are validated and retained in `transfer_batches/PRODUCT/DATE/`. Each original
window is copied by integer row/column offsets, fully read back and committed
with its original contract and checksum. Its receipt records the batch path,
SHA and source window. Daily QA files and their scientific contracts are
unchanged. The historical water mask and daily reflectance modules are unchanged.

A restart reuses committed original windows. If a stop interrupts local
splitting, a validated batch is split again without a new network request;
existing committed windows are verified and never overwritten. Partial files,
bad checksums, mismatched contracts and wrong grids retain strict failure
behavior. Completed dates still require every original window and the normal
daily assembly/readback gate.

Within the existing bounded retry budget, a TIFF transfer reuses its generated
download URL rather than invoking `getDownloadURL` again. URLs remain only in
process memory for that transfer. Retry-After, terminal authentication/input/
certificate/storage failures, process-local TLS recovery and the three waits
(5, 15, 30 seconds) retain their existing behavior.

Fewer requests can reduce per-request overhead and rate-limit exposure, but do
not remove project compute restrictions. Larger requests can take longer or hit
per-request memory/time limits. Measure exact pixel/SHA equality and elapsed
time on the actual region before enabling this option. A small warm-cache
sample alone is insufficient to establish a production speedup. Disable the
runtime flags to return to individual transfers while retaining all cache data.

References:

- [Earth Engine getDownloadURL limits](https://developers.google.com/earth-engine/apidocs/ee-image-getdownloadurl)
- [Earth Engine quotas and caching](https://developers.google.com/earth-engine/guides/usage)
- [Noncommercial restricted mode](https://developers.google.com/earth-engine/guides/noncommercial_tiers)
