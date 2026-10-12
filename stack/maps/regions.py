"""Which OpenStreetMap region covers a point, from Geofabrik's index, decided locally.

The rule (spec, Decisions 3): the largest region containing the point whose extract is at
most 700 MB, never a continent, so a state comes whole when it is small (Nevada, 124 MB)
and in Geofabrik's halves when it is not (California: NorCal, SoCal). When nothing fits, the
smallest containing region."""

from __future__ import annotations

from geo import _points as points, bbox, contains

REGION_CAP_BYTES = 700_000_000


class Index:
    def __init__(self, features: list[dict]) -> None:
        self.regions = []
        for f in features:
            p = f.get("properties") or {}
            pbf = (p.get("urls") or {}).get("pbf")
            if not f.get("geometry") or not pbf or not any(points(f["geometry"])):
                continue  # no download, or an outline with no coordinates: nothing to look up
            self.regions.append({"id": p["id"], "parent": p.get("parent"), "name": p.get("name", p["id"]), "pbf": pbf, "geometry": f["geometry"], "bbox": bbox(f["geometry"])})
        self.by_id = {r["id"]: r for r in self.regions}

    def chain(self, lat: float, lon: float) -> list[dict]:
        hits = [r for r in self.regions if r["bbox"][0] <= lat <= r["bbox"][2] and r["bbox"][1] <= lon <= r["bbox"][3] and contains(r["geometry"], lat, lon)]

        def depth(r):
            d, cur = 0, r
            while cur.get("parent") and cur["parent"] in self.by_id:
                d, cur = d + 1, self.by_id[cur["parent"]]
            return d

        return sorted(hits, key=lambda r: (-depth(r), r["id"]))


def pick(chain: list[dict], sizes: dict[str, int], cap: int = REGION_CAP_BYTES) -> dict | None:
    if not chain:
        return None
    # an unknown size (None, 0: the HEAD failed) never counts as fitting
    fits = [r for r in chain if r.get("parent") and 0 < (sizes.get(r["id"]) or 0) <= cap]
    return fits[-1] if fits else chain[0]
