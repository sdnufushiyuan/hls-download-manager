# Reporting a security issue

Do not include Earth Engine credentials, SSH keys, proxy subscriptions, node
passwords, SMTP authorization codes, signed download URLs, or raw private
configuration in issues or pull requests. Redact affected values before sharing
logs. Report reproducible behavior with synthetic data whenever possible.

Use GitHub's private vulnerability reporting when enabled. Otherwise open an
issue containing only a request for a private reporting channel.

Keep controller endpoints on loopback, use a nonempty controller secret, and
store runtime/private files outside tracked source with permissions 0600.
