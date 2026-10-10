"""Service log and reminders: what was done to a car, when, at what mileage and cost, and what is due next.
Pure helpers; storage in app.py."""

from __future__ import annotations

from datetime import date, timedelta


def due_status(item: dict, today: date, odometer: float | None, soon_days: int = 30, soon_miles: float = 500.0) -> str | None:
    """'overdue', 'soon' or None for a reminder with an optional due_date and/or due_miles."""
    dd, dm = item.get("due_date"), item.get("due_miles")
    if dd and dd < today:
        return "overdue"
    if dm is not None and odometer is not None and odometer >= dm:
        return "overdue"
    if dd and dd <= today + timedelta(days=soon_days):
        return "soon"
    if dm is not None and odometer is not None and odometer >= dm - soon_miles:
        return "soon"
    return None


# Attachments on a service record: the receipt, a photo of the tire, the invoice. Stored
# in the database as bytes so the nightly dump carries them and no path on the host has
# to exist; a ten-megabyte ceiling keeps a phone photo in and a video out.
MAX_UPLOAD = 10 * 1024 * 1024
# The type a file is stored and served as comes from its suffix, against this list, and
# never from what the browser declared: a declared type is the uploader's to choose, and
# "text/html" on a file called photo.png would have come back as a page, scripts and all.
SERVED_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".pdf": "application/pdf",
}


def served_type(filename: str | None) -> str | None:
    """The Content-Type this file will be served with, by suffix; None means not accepted."""
    if not filename or "." not in filename:
        return None
    return SERVED_TYPES.get("." + filename.rsplit(".", 1)[-1].lower())


def allowed_upload(filename: str | None, content_type: str | None, size: int) -> str | None:
    """Why this file is refused, or None when it is fine. `content_type` is what the browser
    said and is not trusted; it is here so a caller can log it."""
    if not filename or not filename.strip():
        return "no file"
    if size <= 0:
        return "empty file"
    if size > MAX_UPLOAD:
        return f"{filename} is over 10 MB"
    if served_type(filename) is None:
        return f"{filename}: photos and PDFs only"
    return None


def size_text(n: int | None) -> str:
    if not n:
        return ""
    return f"{max(1, round(n / 1024))} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def next_reminder(item: dict, done_on: date, odometer: float | None) -> dict | None:
    """The reminder that follows a recurring one just completed: the same work, due again
    `every_months` after today and/or `every_miles` past the odometer now. None when the
    reminder does not repeat. The interval is kept on the row, so each completion
    spawns the next."""
    months, miles = item.get("every_months"), item.get("every_miles")
    if not months and not miles:
        return None
    due_date = None
    if months:
        m = done_on.month - 1 + int(months)
        y, m = done_on.year + m // 12, m % 12 + 1
        last_day = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
        due_date = date(y, m, min(done_on.day, last_day))
    due_miles = (odometer + float(miles)) if miles and odometer is not None else None
    return {"vin": item["vin"], "what": item["what"], "due_date": due_date, "due_miles": due_miles, "every_months": months, "every_miles": miles, "notes": item.get("notes")}
