# Contributing

Bug reports from cars and sources we don't run ourselves help most: what the connector reported, what the page showed, and what you expected.

Questions and ideas go to [Discussions](https://github.com/epheterson/car-command/discussions), bugs and feature requests to [Issues](https://github.com/epheterson/car-command/issues/new/choose), and security problems to a [private report](SECURITY.md).

## Setting up

You need Python 3.12 (the version in `stack/web/Dockerfile`) and Docker with Compose.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r stack/web/requirements.txt pytest
cd stack/web && python3 -m pytest -q          # the site: every module, every template rendered
cd ../maps && python3 -m pytest -q            # the local maps add-on
cd ../cc && pip install requests && python3 -m pytest -q   # the connector's supervisor and privacy hook
cd ../aio && python3 -m pytest -q          # the one container's supervisor
```

To see a change running: `make setup`, `docker compose -f stack/docker-compose.yml up -d --build db web`, then `make demo` for a car. Port 3044.

## Before you open a pull request

CI runs these on every push:

```sh
pip install black==26.5.1 ruff==0.16.9      # pinned
black --check .
ruff check .
cd stack/web && python3 -m pytest -q
```

CI also installs from scratch and reads every page, and upgrades an install with data in it from the last release to your build.

Screenshots, the README loop and the social card come only from the demo car around Springfield: `make media` re-records them all on a throwaway local site.

## House style

- One statement, one line, up to about 200 columns (`pyproject.toml`).
- Every module gets a pure test, and every template is rendered in `test_app_imports.py`. A bug fix comes with the test that would have caught it.
- Nothing new on a page load. Per-drive and per-charge figures are computed once into the facts tables (`facts.py`). Network calls, geocoding and price downloads run in background workers.
- Schema changes run at startup only; backfills go in batches of 300 rows. Nothing is dropped or renamed, so an older backup restores into a newer build.
- Every source writes through `history.py`.
- No position goes to a third party unless the owner turned that on, and then rounded. Charging prices read a charge's own position, not drive positions.
- Comments and copy say what a thing does, in plain words.
- No real VIN, address, coordinate, host name or personal path in the tree. Tests use the fictional cars Alpha, Bravo and Charlie in Springfield, VINs ending `000001`, `000002`; `stack/web/test_no_secrets.py` checks every tracked file.
- Small commits that say what changed: `Price Supercharger sessions at what Tesla billed, and link the invoice`.

## Make targets

| Target | Does |
|---|---|
| `make setup` | writes `stack/.env` with generated passwords (never overwrites one) |
| `make demo` | six weeks of a fictional car, generated and imported |
| `make test` | the connector's tests |
| `make backup`, `make restore FILE=…` | a checked `pg_dump`, and a restore that refuses to overwrite data |
| `make deploy`, `make web` | rsync `stack/` to the host in `Makefile.local`, rebuild the site container alone |
| `make staging` | a second stack with a copy of production's data, every page read against it |
| `make import FILE=…`, `make import-teslamate` | load a TeslaFi export or a TeslaMate database on the host |
| `make discover` | name the places in the history |
| `make replay VIN=… FROM=…` | rebuild a TeslaFi car's drives and charges from its raw feed rows |
| `make lucid-login`, `make secret-key`, `make tesla-net-key` | credentials, asked for on the host |

`Makefile.local` (untracked) holds your host (`MINI`, `DEST`).

## Releasing

A release is a pull request that raises `VERSION` in `stack/web/version.py`, fills in that version's section of `CHANGELOG.md` with the date, and re-records the media with `make media`. Merging it tags the commit, builds the image as `:<version>`, and drafts the GitHub release from the changelog section. A maintainer reads the draft and publishes it; only then do `:<major>.<minor>` and `:latest` move to the new image. A pre-release (`0.2.0-rc.1`) is published as one and never moves `:latest`.

## License

Car Command is under the [PolyForm Noncommercial License](LICENSE). By opening a pull request you license your contribution to the maintainer under the [MIT License](https://opensource.org/license/mit), so it can be included in every edition of Car Command, and to everyone else under the project's license.
