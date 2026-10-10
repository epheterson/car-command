from datetime import date

from service import allowed_upload, due_status, next_reminder, served_type, size_text


def test_due_status_by_date_or_miles():
    today = date(2026, 9, 5)
    assert due_status({"due_date": date(2026, 9, 1)}, today, 1000) == "overdue"
    assert due_status({"due_date": date(2026, 9, 20)}, today, 1000) == "soon"
    assert due_status({"due_miles": 1400}, today, 1000) == "soon"
    assert due_status({"due_miles": 900}, today, 1000) == "overdue"
    assert due_status({"due_date": date(2027, 1, 1), "due_miles": 5000}, today, 1000) is None
    assert due_status({}, today, None) is None


def test_attachments_are_photos_or_pdfs_under_ten_megabytes():
    assert allowed_upload("receipt.pdf", "application/pdf", 20_000) is None
    assert allowed_upload("IMG_0412.HEIC", "application/octet-stream", 3_000_000) is None, "the suffix carries it when the browser will not"
    assert allowed_upload("tire.jpg", "image/jpeg", 11 * 1024 * 1024) == "tire.jpg is over 10 MB"
    assert allowed_upload("notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 500) == "notes.docx: photos and PDFs only"
    assert allowed_upload("", "image/png", 5) == "no file" and allowed_upload("a.png", "image/png", 0) == "empty file"
    assert size_text(51_200) == "50 KB" and size_text(2_600_000) == "2.5 MB" and size_text(None) == "" and size_text(146) == "1 KB"


def test_the_served_type_comes_from_the_suffix_never_from_the_browser():
    """photo.png declared as text/html is a picture, not a page; page.html is refused whatever it claims."""
    assert served_type("photo.png") == "image/png" and served_type("Scan.PDF") == "application/pdf"
    assert allowed_upload("photo.png", "text/html", 100) is None
    assert served_type("page.html") is None and allowed_upload("page.html", "image/png", 100) == "page.html: photos and PDFs only"
    assert served_type("noext") is None


def test_a_recurring_reminder_spawns_the_next_one_from_today_and_the_odometer():
    from datetime import date

    item = {"vin": "V1", "what": "Tire rotation", "every_months": 6, "every_miles": 6000, "notes": "front to back"}
    nxt = next_reminder(item, date(2026, 8, 31), 95_400.0)
    assert nxt["due_date"] == date(2027, 2, 28) and nxt["due_miles"] == 101_400.0 and nxt["what"] == "Tire rotation"
    assert next_reminder({"vin": "V1", "what": "x", "every_months": 12}, date(2024, 2, 29), None)["due_date"] == date(2025, 2, 28)
    assert next_reminder({"vin": "V1", "what": "x", "every_miles": 5000}, date(2026, 1, 1), None)["due_miles"] is None, "no odometer known: only the date side"
    assert next_reminder({"vin": "V1", "what": "x"}, date(2026, 1, 1), 100.0) is None
