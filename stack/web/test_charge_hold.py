"""The charging hold, tick by tick: pure, no clock, no car."""

from datetime import datetime
from zoneinfo import ZoneInfo

import charge_hold as ch

TZ = ZoneInfo("America/Los_Angeles")
CFG = {"from": "15:00", "until": "00:00"}


def car(status="Charging", home=True, plugged=True, soc=50, target=90):
    return {"name": "Alpha", "status": status, "at_home": home, "plugged": plugged, "soc": soc, "charge_target": target}


def at(h, m=0, day=11, s=0):
    return datetime(2026, 10, day, h, m, s, tzinfo=TZ)


def run(ticks, mem=None, cfg=CFG):
    """[(time, car)] -> the steps, carrying memory from one to the next."""
    mem, out = mem or ch.Memory(), []
    for now, c in ticks:
        s = ch.step(c, mem, now, cfg)
        out.append(s)
        mem = s.memory
    return out


def test_inside_wraps_midnight():
    s, e = ch.clock("15:00", "x"), ch.clock("00:00", "x")
    assert ch.inside(at(15).time(), s, e) and ch.inside(at(23, 59).time(), s, e)
    assert not ch.inside(at(0).time(), s, e) and not ch.inside(at(14, 59).time(), s, e)
    assert not ch.inside(at(12).time(), s, s)


def test_an_evening_charge_is_stopped_after_the_grace_and_started_at_midnight():
    steps = run([(at(20, 9), car()), (at(20, 15), car()), (at(20, 19), car()), (at(20, 21), car("Plugged")), (at(0, 1, 12), car("Plugged")), (at(0, 2, 12), car())])
    assert [s.action for s in steps] == [None, None, "stop", None, "start", None]
    assert "Paused Alpha" in steps[2].say and "12 AM" in steps[2].say
    assert steps[-1].memory == ch.Memory(night=True)  # charging again: the night's charge


def test_a_daytime_charge_is_held_until_midnight_by_default():
    day = ch.rule_for({"V": {}}, "V")
    assert day == {"from": "06:00", "until": "00:00"}
    steps = run([(at(13, 48), car()), (at(13, 55), car()), (at(13, 59), car())], cfg=day)
    assert [s.action for s in steps] == [None, None, "stop"]


def test_the_nights_charge_running_into_the_morning_is_left_to_finish():
    day = {"from": "06:00", "until": "00:00"}
    steps = run([(at(0, 5, 12), car()), (at(5, 59, 12), car()), (at(6, 1, 12), car()), (at(7, 30, 12), car()), (at(8, 4, 12), car("Plugged")), (at(9, 0, 12), car()), (at(9, 11, 12), car())], cfg=day)
    assert [s.action for s in steps] == [None, None, None, None, None, None, "stop"]  # done at 08:04; a new charge at 09:00 is held


def test_the_charge_started_at_midnight_is_the_nights_charge():
    day = {"from": "06:00", "until": "00:00"}
    steps = run([(at(20), car()), (at(20, 10), car()), (at(20, 11), car("Plugged")), (at(0, 1, 12), car("Plugged")), (at(0, 2, 12), car()), (at(6, 30, 12), car())], cfg=day)
    assert [s.action for s in steps] == [None, "stop", None, "start", None, None]
    assert steps[-1].memory.night


def test_a_charge_stopped_by_hand_inside_the_hours_is_started_at_midnight():
    steps = run([(at(13, 48), car()), (at(13, 56), car("Plugged")), (at(0, 1, 12), car("Plugged")), (at(0, 2, 12), car("Plugged", soc=90))], cfg={"from": "06:00", "until": "00:00"})
    assert [s.action for s in steps] == [None, None, "start", None]
    assert ch.step(car("Plugged", soc=90), steps[1].memory, at(0, 1, 12), CFG).action is None, "full by midnight: nothing to start"


def test_a_tesla_blip_shorter_than_the_grace_is_left_alone():
    steps = run([(at(17, 46), car()), (at(17, 50), car()), (at(17, 52), car("Plugged")), (at(18, 30), car("Plugged"))])
    assert all(s.action is None and s.say is None for s in steps)


def test_started_by_hand_after_the_stop_is_let_go_and_not_restarted_at_midnight_if_full():
    steps = run(
        [
            (at(18), car()),
            (at(18, 10), car()),  # stopped
            (at(18, 11), car("Plugged")),  # took
            (at(18, 30), car()),
            (at(19, 30), car()),  # the owner started it again
            (at(0, 1, 12), car("Plugged", soc=90)),
        ]
    )
    assert [s.action for s in steps] == [None, "stop", None, None, None, None]
    assert steps[3].memory.let_go and steps[-1].memory == ch.Memory()


def test_a_stop_that_does_not_take_is_sent_again_once_then_said():
    steps = run([(at(18), car()), (at(18, 10), car()), (at(18, 12), car()), (at(18, 14), car()), (at(18, 18), car()), (at(18, 30), car())])
    assert [s.action for s in steps] == [None, "stop", None, "stop", None, None]
    assert steps[4].kind == "command" and "after two stops" in steps[4].say
    assert steps[5].say is None, "said once"


def test_away_from_home_or_outside_the_hours_is_none_of_its_business():
    assert all(s.action is None for s in run([(at(18), car(home=False)), (at(18, 30), car(home=False))]))
    assert all(s.action is None for s in run([(at(1), car()), (at(14, 40), car())]))


def test_unplugging_forgets_the_stop_so_the_car_is_held_again_when_it_comes_back():
    steps = run([(at(18), car()), (at(18, 10), car()), (at(18, 11), car("Plugged")), (at(18, 40), car("Parked", plugged=False)), (at(21), car()), (at(21, 10), car())])
    assert [s.action for s in steps] == [None, "stop", None, None, None, "stop"]


def test_a_resume_that_does_not_start_is_said_ten_minutes_later():
    steps = run([(at(18), car()), (at(18, 10), car()), (at(18, 11), car("Plugged")), (at(0, 1, 12), car("Plugged")), (at(0, 5, 12), car("Plugged")), (at(0, 11, 12), car("Plugged"))])
    assert steps[3].action == "start"
    assert steps[4].say is None and steps[5].kind == "command" and "didn't start" in steps[5].say


def test_a_car_gone_by_midnight_is_not_started_and_the_memory_clears():
    steps = run([(at(18), car()), (at(18, 10), car()), (at(18, 11), car("Plugged")), (at(0, 1, 12), car("Plugged", home=False))])
    assert steps[-1].action is None and steps[-1].memory == ch.Memory()


def test_memory_round_trips_and_ignores_unknown_keys():
    m = ch.Memory(since="2026-10-11T18:00:00-07:00", tries=1)
    assert ch.Memory.of({**m.as_dict(), "future": 1}) == m


def test_the_start_waits_half_a_minute_past_the_end_of_the_hours():
    held = run([(at(18), car()), (at(18, 10), car()), (at(18, 11), car("Plugged"))])[-1].memory
    assert ch.step(car("Plugged"), held, at(0, 0, 12, 10), CFG).action is None
    assert ch.step(car("Plugged"), held, at(0, 0, 12, 40), CFG).action == "start"


def test_a_rule_is_read_per_car_with_its_hours_bounded():
    assert ch.rule_for({}, "V1") is None
    assert ch.rule_for({"V1": {}}, "V1") == {"from": "06:00", "until": "00:00"}
    assert ch.rule_for({"V1": {"from": "16:30", "until": "25:99"}}, "V1") == {"from": "16:30", "until": "00:00"}
    assert ch.rule_for({"V1": "on"}, "V1") is None


class Fake:
    def __init__(self, status="Charging", ok=True):
        self.state, self.sent, self.said, self.status, self.ok = {}, [], [], status, ok
        self.clock = [at(18)]

    def worker(self, rules=None, enabled=True):
        return ch.Worker(
            rules=lambda: {"V1": {"from": "15:00", "until": "00:00"}} if rules is None else rules,
            enabled=lambda: enabled,
            car=lambda vin: car(self.status),
            send=lambda vin, key: (self.sent.append((vin, key)) or (self.ok, None if self.ok else "The car is asleep and didn't wake up.")),
            notify=lambda text, kind: self.said.append((kind, text)),
            get=self.state.get,
            set=self.state.__setitem__,
            now=lambda: self.clock[0],
        )


def test_the_worker_stops_a_charge_past_the_grace_says_so_and_remembers_across_a_restart():
    f = Fake()
    f.worker().tick()
    f.clock[0] = at(18, 10)
    f.worker().tick()  # a new worker: the memory came from cc_state
    assert f.sent == [("V1", "charge_stop")]
    assert f.said[0][0] == "charge_hold" and "Paused Alpha" in f.said[0][1]


def test_the_worker_sends_nothing_while_commands_are_off_or_no_car_has_a_rule():
    f = Fake()
    for w in (f.worker(enabled=False), f.worker(rules={})):
        f.clock[0] = at(18)
        w.tick()
        f.clock[0] = at(18, 30)
        w.tick()
    assert f.sent == [] and f.said == []


def test_a_stop_that_fails_is_said_as_a_command_alert():
    f = Fake(ok=False)
    w = f.worker()
    w.tick()
    f.clock[0] = at(18, 10)
    w.tick()
    assert f.sent == [("V1", "charge_stop")]
    assert f.said == [("command", "Car Command couldn't pause Alpha charging: The car is asleep and didn't wake up.")]
