# Code scanning notes

CodeQL (security-extended) reports these; each was reviewed and is left as it is. Dismiss them in Security › Code scanning with the reason given.

| Rule | Location | Dismiss as | Why |
| --- | --- | --- | --- |
| `py/partial-ssrf` | `stack/web/mapsclient.py`, `MapsClient._call` | False positive | The host is `MAPS_URL`, set by the operator, never by a request. The only request-supplied part is a region id on add and remove, which `valid_region_id` checks before any call: lowercase letters, digits, `/`, `_` and `-`, at most 80 characters, no `..` segment; search terms and coordinates go in the query string through `urlencode`. |
| `py/overly-permissive-file` | `stack/aio/carcommand.py`, `prepare_dirs`, `os.chmod(DATA, 0o711)` | Won't fix | `/data` is a directory, and 0711 gives others search, not read: Postgres and the connector's user each pass through to their own 0700 directory and cannot list `/data` or open another's; the secrets and the logs are root's alone. |
| `py/overly-permissive-file` | `stack/cc/plugins.py`, `_write` (0640) | Won't fix | pip's lock and constraints files are root's and readable by the plugin user's own group, because pip runs as that user. Root ownership keeps the plugin user from rewriting what pip is about to install; nobody else can read them. |
| `py/overly-permissive-file` | `stack/cc/supervise.py`, `main`, the pip directory (0750) | Won't fix | The directory that holds those two files: root's, searchable by the plugin user's group, nobody else. |
| `py/log-injection` | `stack/web/app.py`, `delete_place`, `places: removed place %s` | False positive | `place_id` is declared `int` on the route, so FastAPI has refused anything that is not an integer before the log line is reached. |

Fixed rather than dismissed: the icon markup in `placecard.js` (built as elements now), Tesla errors answered as their written sentence (`TeslaError.public`), every redirect built from a request through `to_site` (one of the three mounts by name, then `safe_next`), positions out of `discover.py`'s output, the stream's vin stripped of line breaks in the log, TLS 1.2 stated for the Tesla stream check, and the world-readable files and directories the connector and the one container wrote.
