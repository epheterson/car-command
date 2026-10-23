"""Where the TeslaFi feed goes blind and the car's own stream does not.

The feed sometimes answers carState "Unknown" for hours with no odometer, speed or position
while the car is awake: the car drove and charged and the feed's rows say nothing. The stream
(tesla_stream) saw it. For a car the feed writes, the rebuild takes the stream's samples for
those spans and the feed everywhere else, so the replay finds the drives and charges in them.

Pure: sample lists in, sample lists out. Both are in sample_from_row's shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta

MIN_BLIND = timedelta(minutes=10)  # a shorter silence is a dropped poll or two, not a gap worth another source


def is_blind(s: dict) -> bool:
    """A poll that answered awake but told nothing: no odometer, not asleep, not blank.
    (A blank poll is a logger tick and a sleeping row has no odometer for a good reason.)"""
    return not s.get("blank") and not s["asleep"] and s["odometer"] is None


def blind_spans(feed: list[dict], min_span: timedelta = MIN_BLIND) -> list[tuple[datetime, datetime]]:
    """[start, end) of each run of blind feed samples lasting `min_span` or more. A run ends
    at the first sample that is not blind (or, at the end of the feed, at its last blind one).
    Blank polls inside a run do not break it; a sleeping or readable one does."""
    spans, first, last = [], None, None
    for s in feed:
        if s.get("blank"):
            continue
        if is_blind(s):
            first = first or s["time"]
            last = s["time"]
            continue
        if first is not None and s["time"] - first >= min_span:
            spans.append((first, s["time"]))
        first = last = None
    if first is not None and last - first >= min_span:
        spans.append((first, last + timedelta(microseconds=1)))
    return spans


def _in(t: datetime, spans) -> bool:
    return any(a <= t < b for a, b in spans)


def splice(feed: list[dict], stream: list[dict], spans: list[tuple[datetime, datetime]]) -> list[dict]:
    """The feed's samples with its blind spans swapped for the stream's, in time order.

    The stream numbers its drives and charges from 1 and the feed has TeslaFi's own, so the
    stream's are moved above the feed's largest to keep them apart, except that a session
    running across a span's edge keeps the number of the feed sample next to it (a drive that
    began before the feed went blind is one drive)."""
    if not spans:
        return feed
    kept = [s for s in feed if not _in(s["time"], spans)]
    mine = [s for s in stream if _in(s["time"], spans)]
    if not mine:
        return feed
    top = {k: max([s[k] for s in feed] + [0]) for k in ("drive_no", "charge_no")}
    out = []
    for a, b in spans:
        inside = [dict(s) for s in mine if a <= s["time"] < b]
        before = next((s for s in reversed(kept) if s["time"] < a and not s.get("blank")), None)
        after = next((s for s in kept if s["time"] >= b and not s.get("blank")), None)
        for k in ("drive_no", "charge_no"):
            joins = {}
            if inside and inside[0][k] and before and before[k]:
                joins[inside[0][k]] = before[k]
            if inside and inside[-1][k] and after and after[k]:
                joins[inside[-1][k]] = after[k]
            for s in inside:
                if s[k]:
                    s[k] = joins.get(s[k], top[k] + s[k])
            top[k] = max([top[k]] + [s[k] for s in inside])
        out += inside
    return sorted(kept + out, key=lambda s: s["time"])


def stream_samples(payloads: list[dict], until: datetime) -> list[dict]:
    """The stream's raw payloads as samples, the way tesla_live rebuilds them."""
    import tesla_live
    import tesla_stream

    return tesla_stream.samples(tesla_live.with_silence(tesla_stream.records(payloads), until=until), until=until)
