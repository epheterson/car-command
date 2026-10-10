## What and why

<!-- What this changes and why. Fixes #… -->

## How it was checked

<!-- Tests added, and what you checked in a running site (make demo gives you a car). -->

- [ ] `cd stack/web && python3 -m pytest -q` passes, with a test for this change
- [ ] `black --check .` and `ruff check .` pass on the pinned versions (black 26.5.1, ruff 0.16.9)
- [ ] Nothing new runs on a page load; schema changes run at startup, backfills in batches
- [ ] No real VIN, address, coordinate, host or personal path (`test_no_secrets.py`)
- [ ] Docs and changelog updated if users will notice
- [ ] I license this contribution as described in [CONTRIBUTING.md](../CONTRIBUTING.md#license)
