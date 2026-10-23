# QA TLS recovery

The daily supervisor keeps the original Fmask cache and the scientific recipe
unchanged when a network connection fails. A successful chunk is committed only
after its exact grid, uint8 encoding and checksum have been validated.

QA diagnostics distinguish `ee-get-download-url`, `qa-tiff-transfer` and
`qa-local-validation`. TLS diagnostics expose only bounded categories and known
reason codes. Exception messages, signed URLs, headers and credentials are never
printed. A historical log containing only `SSLError` cannot establish whether
the cause was a certificate, a prematurely closed connection, or another TLS
protocol failure. A later successful request does not establish that cause.

Known temporary EOF/protocol failures use the existing finite chunk retry budget
(three waits: 5, 15 and 30 seconds). Before retrying, the supervisor discards the
failed sockets in its own Requests or Earth Engine transport. The latter keeps
its proxy configuration, certificates and credentials. It does not restart the
shared proxy, change routing or modify an archive process.

Certificate verification failures remain terminal, including failures wrapped
inside Requests/urllib3 exceptions. Authentication denials, invalid requests,
local storage errors and invalid QA grids retain their terminal behavior. TLS
verification is never disabled. After temporary failures exhaust their budget,
the existing regional supervisor can resume in a later cycle using validated
cache entries.

Validation includes native and wrapped TLS failures, certificate rejection,
finite exhaustion, local-error precedence, safe diagnostics, and a stream that
fails after writing partial bytes and then succeeds with the same pixel payload.
Real network probes must use an independent diagnostic directory and must not
overwrite production QA blocks or fabricate completion receipts.
