from datetime import datetime, timedelta, timezone

import chargestart

T = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)


def h(x):
    return T + timedelta(hours=x)


def session(i, start=None, end=None, plugged=None, locked=None, frm=None, to=None):
    return {"id": i, "session_start_date": start, "session_end_date": end, "plug_connected_date": plugged, "plug_locked_date": locked, "start_level": frm, "end_level": to}


def test_a_charge_with_an_end_and_no_start_starts_where_the_car_began_charging():
    s = [session(46, end=h(10), plugged=h(0.75), locked=h(0.8), frm=22.9, to=90.0)]
    charging = [{"first": h(-30), "last": h(-29)}, {"first": h(2.2), "last": h(3.1)}, {"first": h(3.15), "last": h(10)}]
    (f,) = chargestart.plan(s, charging)
    assert f["id"] == 46 and f["start"] == h(2.2), "the first CHARGING after the cable went in, not an older one"


def test_fragments_and_non_charges_are_left_alone():
    charging = [{"first": h(1), "last": h(4)}]
    counted = session(41, start=h(1), end=h(4), plugged=h(0.5), frm=74.5, to=90.0)
    fragment = session(42, end=h(4), plugged=h(2), frm=80.0, to=90.0)  # inside a charge already counted
    flat = session(45, end=h(4), plugged=h(0.5), frm=90.0, to=90.0)  # no rise
    unplugged = session(40, plugged=h(0.5))  # never charged: no end, no levels
    open_ = session(47, plugged=h(0.5), frm=20.0)  # still going
    assert chargestart.plan([counted, fragment, flat, unplugged, open_], charging) == []
    assert chargestart.plan([session(48, end=h(4), plugged=h(0.5), frm=20.0, to=60.0)], []) == [], "no CHARGING state: not filled"


class Conn:
    def __init__(self, sessions, charging):
        self.sessions, self.charging, self.sql = sessions, charging, []

    def execute(self, sql, args=()):
        self.sql.append((sql, args))
        rows = self.sessions if "FROM charging_sessions" in sql else self.charging if "FROM charging_states" in sql else []

        class Cur:
            description = [type("D", (), {"name": k}) for k in (rows[0] if rows else {})]

            def fetchall(self):
                return rows

        return Cur()

    def commit(self):
        pass


def test_run_writes_only_the_missing_start_and_a_dry_run_writes_nothing():
    s = [session(46, end=h(10), plugged=h(0.75), frm=22.9, to=90.0)]
    c = Conn(s, [{"first": h(2.2), "last": h(10)}])
    assert chargestart.run(c, "V1", h(-24), dry_run=True)[0]["start"] == h(2.2)
    assert not [q for q, _ in c.sql if q.startswith("UPDATE")]
    chargestart.run(c, "V1", h(-24))
    (upd,) = [(q, a) for q, a in c.sql if q.startswith("UPDATE")]
    assert "session_start_date IS NULL" in upd[0] and upd[1] == (h(2.2), 46)
    assert "charge 46: started" in chargestart.line(chargestart.plan(s, [{"first": h(2.2), "last": h(10)}])[0])
