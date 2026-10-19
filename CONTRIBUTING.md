# Contributing

Use Python 3.11+ on Linux for the full test suite. Install with
`python -m pip install -e . PyYAML build`, then run
`python -m unittest discover -p 'test_*.py'` and
`python tools/check_public_tree.py`.

Tests use synthetic GeoTIFFs, temporary directories, fake transports and mocked
services. Do not add network-dependent tests or require production credentials
in CI. Keep temporary transport failures separate from authorization, NAS and
scientific-validation errors. Changes to masks, grids, scale, deduplication or
cache identity need independent numerical checks and a migration explanation.

Use issues for a reproducible bug or a concrete proposal. Keep real data and
private deployment details out of commits.
