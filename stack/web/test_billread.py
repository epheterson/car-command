"""Reading a bill: a fictional PG&E-style statement with a community-choice provider's
generation, made as a PDF here (no real account, name or address), and what it becomes."""

from datetime import date

import pytest

import billread
import cost
import urdb


def pdf(lines: list[str]) -> bytes:
    """A one-page PDF of these lines in Helvetica, as plain as a PDF gets."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = "BT /F1 9 Tf 11 TL 36 770 Td " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


BILL = [
    "ENERGY STATEMENT                                   www.pge.com/MyEnergy",
    "Account No: 0000000000-0     Statement Date: 08/04/2026",
    "Service For: ALPHA RESIDENT, 1 ALPHA WAY, SPRINGFIELD",
    "Pacific Gas and Electric Company",
    "Details of PG&E Electric Delivery Charges",
    "07/01/2026 - 07/31/2026 (31 billing days)",
    "Service Agreement ID: 0000000001",
    "Rate Schedule: EV2A Home Charging",
    "Peak 60.000000 kWh @ $0.31012 $18.61",
    "Part Peak 30.000000 kWh @ $0.29010 $8.70",
    "Off Peak 410.000000 kWh @ $0.18120 $74.29",
    "Generation Credit -64.50",
    "Power Charge Indifference Adjustment 9.75",
    "Franchise Fee Surcharge 0.75",
    "Total PG&E Electric Delivery Charges $47.60",
    "Details of MCE Electric Generation Charges",
    "07/01/2026 - 07/31/2026",
    "Rate Schedule: EV2",
    "Peak 60.000000 kWh @ $0.23500 $14.10",
    "Part Peak 30.000000 kWh @ $0.17500 $5.25",
    "Off Peak 410.000000 kWh @ $0.10500 $43.05",
    "Total MCE Electric Generation Charges $62.40",
]


def test_a_bill_reads_as_its_schedule_provider_and_prices():
    bill = billread.read(billread.text_of(pdf(BILL)))
    assert bill["schedule"] == "EV2A" and bill["provider"] == "MCE" and bill["month"] == 7 and bill["start"] == "2026-07-01" and bill["kwh"] == 500.0
    per = (-64.50 + 9.75 + 0.75) / 500  # the totals-only lines, spread over the bill's kWh
    assert bill["prices"] == pytest.approx({"off_peak": 0.1812 + 0.105 + per, "part_peak": 0.2901 + 0.175 + per, "peak": 0.31012 + 0.235 + per}, abs=1e-5)
    assert urdb.utility_in(billread.text_of(pdf(BILL)))["eia"] == 14328


def test_the_child_process_reads_the_bill_and_refuses_what_is_not_one():
    """parse() runs the reading in a child process with a time and memory limit."""
    out = billread.parse(pdf(BILL))
    assert out["bill"]["schedule"] == "EV2A" and out["bill"]["provider"] == "MCE" and out["utility"]["eia"] == 14328
    for bad in (b"%PDF-1.4\nnot really", b"%PDF-1.7\n1 0 obj << /Type /Pages /Kids [1 0 R] /Count 99 >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF"):
        with pytest.raises(billread.BillError, match="Couldn't read this bill"):
            billread.parse(bad)


def test_a_child_that_runs_long_is_stopped_and_refused(monkeypatch):
    import subprocess

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="billread", timeout=billread.TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(billread.BillError, match="Couldn't read this bill"):
        billread.parse(pdf(BILL))


def test_a_pathological_line_reads_fast():
    """Digits by the thousand with no kWh, a period name with no number, totals with no
    amount: every pattern is bounded, so the whole clipped text reads in well under a second."""
    import time

    nasty = "\n".join(["Peak " + "1" * 5000, "Off Peak " + "1," * 3000, "Generation Credit " + "9" * 5000, "Rate Schedule: " + "A" * 5000, "Super Off Peak " + " " * 5000 + "x"] * 200)
    started = time.monotonic()
    got = billread.read(nasty)
    assert time.monotonic() - started < 1.0 and got["prices"] == {}
    assert len(billread.clip(nasty)) <= billread.MAX_TEXT and max(len(x) for x in billread.clip(nasty).splitlines()) <= billread.MAX_LINE


def test_a_bill_without_a_provider_is_delivery_and_generation_from_the_utility():
    plain = [line for line in BILL if "MCE" not in line and "Generation Credit" not in line and "Indifference" not in line][:12]
    bill = billread.read(billread.text_of(pdf(plain)))
    assert bill["provider"] is None and bill["prices"]["peak"] == pytest.approx(0.31012 + 0.75 / 500, abs=1e-5)


@pytest.mark.parametrize(
    "data, says",
    [(b"", "Choose your bill"), (b"hello", "not a PDF"), (b"%PDF-1.4\nnot really", "Couldn't read"), (b"%PDF-" + b"0" * (billread.MAX_BYTES + 1), "over 10 MB")],
)
def test_what_is_not_a_bill_says_why(data, says):
    with pytest.raises(billread.BillError, match=says):
        billread.text_of(data)


def test_the_reading_travels_in_links_and_back():
    bill = {"schedule": "EV2A", "provider": "MCE", "month": 7, "start": "2026-07-01", "prices": {"off_peak": 0.2, "part_peak": 0.36, "peak": 0.44}}
    q = billread.to_query(bill)
    assert q == {"bill": "EV2A", "bp": "off_peak:0.2,part_peak:0.36,peak:0.44", "cca": "MCE", "bm": "7", "bs": "2026-07-01"}
    assert billread.from_query(q) == bill
    assert billread.from_query({"bill": "?", "bp": "peak:9,off_peak:x,evil:0.1", "bs": "nope"}) == {"schedule": None, "provider": None, "month": None, "start": None, "prices": {}}
    assert billread.from_query({}) is None


def test_providers_by_utility_and_by_name_on_a_bill():
    assert {"name": "MCE", "label": "MCE"} in billread.providers_for(14328) and {"name": "Silicon Valley Clean Energy", "label": "SVCE"} in billread.providers_for(14328)
    assert "San Diego Community Power" in [p["name"] for p in billread.providers_for("16609")] and billread.providers_for("x") == []
    assert billread.provider("Generation by Silicon Valley Clean Energy") == "Silicon Valley Clean Energy"
    assert billread.provider("Your SVCE generation") == "Silicon Valley Clean Energy" and billread.provider("Peninsula Clean Energy") == "WestLight Energy"
    assert billread.provider("Ava Smith, 1 Alpha Way") is None, "a short name that is also a word is not read as a provider"
    assert billread.provider("1 ALPHA WAY SPRINGFIELD Community Power Generation") is None, "a provider the list does not know is not named"


@pytest.mark.parametrize(
    "schedule, plan, named",
    [
        ("EV2A", "Electric Vehicle EV2 (Sch)", True),  # the option letter after the plan's number
        ("EV2", "Electric Vehicle EV2 (Sch)", True),
        ("E-TOU-C", "E-TOU-C Residential Time of Use Baseline Region P", True),
        ("E-TOU-C", "E-TOU Option A - Residential Time of Use Service", False),
        ("E-TOU-C", "E-TOU-C3 Residential", False),
        ("EV2A", "Electric Vehicle EV (Sch)", False),
        ("EV2", "Electric Vehicle EV2A", False),  # the plan with a letter more is another plan
    ],
)
def test_a_bill_names_only_its_own_plan(schedule, plan, named):
    assert billread.names({"schedule": schedule}, plan) is named


def test_the_child_sets_its_own_limits_before_reading():
    """No code runs between fork and exec: the child limits itself first thing."""
    import inspect
    import subprocess
    import sys

    assert "preexec_fn" not in inspect.getsource(billread.parse)
    assert inspect.getsource(billread._child).split("\n")[1].strip() == "_limits()"
    out = subprocess.run(
        [sys.executable, "-c", "import billread, resource; billread._limits(); print(resource.getrlimit(resource.RLIMIT_CPU)[0])"], capture_output=True, text=True, cwd=billread.HERE, timeout=30
    )
    assert out.stdout.strip() == str(billread.TIMEOUT_S + 2)


def _seasonal() -> dict:
    """A tariff with summer (Jun–Sep) and winter periods, three each: off, part, peak."""
    summer, winter = [0] * 15 + [1] + [2] * 5 + [1] * 3, [3] * 15 + [4] + [5] * 5 + [4] * 3
    grid = [summer if 5 <= m <= 8 else winter for m in range(12)]
    prices = [0.25, 0.4, 0.5, 0.24, 0.38, 0.42]
    return {
        "name": "Electric Vehicle EV2 (Sch)",
        "utility": "Pacific Gas & Electric Co",
        "source": "urdb:" + "e" * 24,
        "versions": [{"from": "2016-10-01", "periods": [{"key": f"p{i}", "label": f"P{i}", "price": p} for i, p in enumerate(prices)], "weekday": grid, "weekend": grid}],
    }


def test_the_bills_prices_go_on_the_plan_it_names_from_its_first_day():
    """Summer bill: summer periods take its prices, winter ones are scaled and marked as
    estimates; charges before the bill keep the plan in use."""
    from conftest import SPRINGFIELD_SCHEDULE

    typed = cost.compile_rows("Springfield EV2A", [{"from": "2020-01-01", "off_peak": 0.2, "part_peak": 0.3, "peak": 0.4}], SPRINGFIELD_SCHEDULE)
    tariff = _seasonal()
    july = {"schedule": "EV2A", "provider": "MCE", "month": 7, "start": "2026-07-01", "prices": {"off_peak": 0.2, "part_peak": 0.32, "peak": 0.4}}
    plan = billread.apply(tariff, july, date(2026, 10, 13), before=typed)
    assert cost.valid_plan(plan) and plan["source"] == "bill:" + "e" * 24 and plan["name"] == "EV2A · MCE (your bill)"
    assert [v["from"] for v in plan["versions"]] == ["2020-01-01", "2026-07-01"], "earlier charges keep the typed plan"
    new = plan["versions"][-1]["periods"]
    assert [p["price"] for p in new[:3]] == pytest.approx([0.2, 0.32, 0.4]) and not any(p.get("est") for p in new[:3])
    assert all(p.get("est") for p in new[3:]) and new[3]["price"] == pytest.approx(0.24 * 0.2 / 0.25), "winter: scaled from summer, marked"
    # A January bill over it: winter set from that bill, summer kept from July's.
    jan = {**july, "month": 1, "start": "2027-01-01", "prices": {"off_peak": 0.21, "part_peak": 0.31, "peak": 0.35}}
    both = billread.apply(tariff, jan, date(2027, 2, 1), before=plan)
    last = both["versions"][-1]["periods"]
    assert [p["price"] for p in last] == pytest.approx([0.2, 0.32, 0.4, 0.21, 0.31, 0.35]) and not any(p.get("est") for p in last)
    assert [v["from"] for v in both["versions"]] == ["2020-01-01", "2026-07-01"]
    assert billread.names(july, "Electric Vehicle EV2 (Sch)") and not billread.names(july, "E-TOU-C Residential Time of Use")
    with pytest.raises(billread.BillError):
        billread.apply(tariff, {**july, "prices": {}}, date(2026, 10, 13))
    # typing a price over an estimate makes it no estimate
    assert "est" not in cost.with_prices(plan, {(1, 3): 0.3})["versions"][1]["periods"][3]
