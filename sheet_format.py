"""Friendly spreadsheet formatting: database rows -> simple, readable rows.

All times are shown in US-Eastern (the venues' timezone). Class times render
as ranges like "9-10am" / "9:30-10:45am"; end times are derived from each
platform's raw payload (Trybe stores an end timestamp; Mariana Tek and Arketa
store a duration in minutes).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# Columns actually written to the simple sheets. simple_row() computes a few
# extras (notes) that stay available if ever re-added.
SIMPLE_COLUMNS = [
    "brand", "location", "class", "date", "time",
    "observed", "price", "spots_left", "capacity",
]

HEADERS = {
    "brand": "Brand", "location": "Location", "class": "Class", "date": "Date",
    "time": "Time", "price": "Price", "spots_left": "Spots Left",
    "capacity": "Total Spots", "notes": "Notes", "observed": "Observed",
}

COLUMN_WIDTHS = {
    "brand": 11, "location": 16, "class": 40, "date": 12, "time": 15,
    "price": 9, "spots_left": 11, "capacity": 11, "observed": 15,
}

# Extra PostgREST select expressions that pull end-time ingredients out of the
# stored raw json (missing fields simply come back null).
EXTRA_SELECTS = [
    "raw_end:raw->>end_time",                 # trybe: ISO end timestamp
    "raw_dur:raw->>duration",                 # arketa: minutes
    "raw_ct_dur:raw->class_type->>duration",  # mariana_tek: minutes
]


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutes(val) -> int | None:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _clock(dt: datetime, with_ampm: bool = True) -> str:
    hour = dt.strftime("%I").lstrip("0")
    out = hour if dt.minute == 0 else f"{hour}:{dt.minute:02d}"
    return out + (dt.strftime("%p").lower() if with_ampm else "")


def _time_range(start: datetime, end: datetime | None) -> str:
    if end is None or end <= start:
        return _clock(start)
    same_half = start.strftime("%p") == end.strftime("%p")
    return f"{_clock(start, not same_half)}-{_clock(end)}"


def _money(price) -> str:
    if price is None or price == "":
        return ""
    p = float(price)
    return f"${int(p)}" if p == int(p) else f"${p:.2f}"


def simple_row(row: dict) -> dict:
    start = _parse(row.get("start_time"))
    end = _parse(row.get("raw_end"))
    if end is None and start is not None:
        mins = _minutes(row.get("raw_ct_dur")) or _minutes(row.get("raw_dur"))
        if mins:
            end = start + timedelta(minutes=mins)
    start_et = start.astimezone(EASTERN) if start else None
    end_et = end.astimezone(EASTERN) if end else None
    observed_et = None
    observed = _parse(row.get("observed_at"))
    if observed:
        observed_et = observed.astimezone(EASTERN)

    notes = row.get("price_tier") or ""
    if row.get("is_waitlist") in (True, 1, "true", "True"):
        notes = f"{notes}; FULL" if notes else "FULL"

    return {
        "brand": (row.get("brand") or "").capitalize(),
        "location": row.get("location") or "",
        "class": row.get("class_name") or "",
        "date": f"{start_et.strftime('%a %b')} {start_et.day}" if start_et else "",
        "time": _time_range(start_et, end_et) if start_et else "",
        "price": _money(row.get("price")),
        "spots_left": row.get("spots_available"),
        "capacity": row.get("capacity"),
        "notes": notes,
        "observed": f"{observed_et.strftime('%b')} {observed_et.day}, {_clock(observed_et)}"
        if observed_et
        else "",
    }


def simple_rows(rows: list[dict]) -> list[dict]:
    """Sort by class start (then brand/location/observation) and format."""
    ordered = sorted(
        rows,
        key=lambda r: (
            r.get("start_time") or "",
            r.get("brand") or "",
            str(r.get("location") or ""),
            r.get("observed_at") or "",
        ),
    )
    return [simple_row(r) for r in ordered]


def xlsx_bytes(rows: list[dict]) -> bytes:
    """Styled Excel workbook: bold banded header, frozen top row, filters."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Sessions"

    ws.append([HEADERS[c] for c in SIMPLE_COLUMNS])
    header_font = Font(bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    for rec in simple_rows(rows):
        ws.append([rec.get(c) for c in SIMPLE_COLUMNS])

    for i, col in enumerate(SIMPLE_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = COLUMN_WIDTHS.get(col, 12)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
