"""Read exchange-rate records from the supplied NBC workbook."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from statistics import mean

from openpyxl import load_workbook


MONTHS = {
    "january": (1, "Jan"),
    "february": (2, "Feb"),
    "march": (3, "Mar"),
    "april": (4, "Apr"),
    "may": (5, "May"),
    "june": (6, "Jun"),
    "july": (7, "Jul"),
    "august": (8, "Aug"),
    "september": (9, "Sep"),
    "october": (10, "Oct"),
    "november": (11, "Nov"),
    "december": (12, "Dec"),
}


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _year(value):
    number = _number(value)
    if number is not None and number.is_integer() and 1900 <= number <= 2200:
        return int(number)
    return None


def _annual_year(value):
    if not isinstance(value, str) or "annual" not in value.casefold():
        return None
    for part in value.replace("/", " ").replace("-", " ").split():
        if part.isdigit() and 1900 <= int(part) <= 2200:
            return int(part)
    return None


def load_exchange_rates(path: str | Path) -> dict:
    """Return annual and available monthly rates without assuming fixed rows."""
    workbook = load_workbook(path, data_only=True, read_only=True)
    years: dict[int, dict] = {}

    for sheet in workbook.worksheets:
        current_year = None
        for values in sheet.iter_rows(values_only=True):
            first = values[0] if values else None
            marker_year = _year(first)
            if marker_year:
                current_year = marker_year
                years.setdefault(marker_year, {"annual": None, "months": []})
                continue

            annual_year = next(
                (_annual_year(value) for value in values if _annual_year(value)),
                None,
            )
            if annual_year:
                numbers = [_number(value) for value in values]
                annual_value = next(
                    (number for number in reversed(numbers) if number is not None),
                    None,
                )
                if annual_value is not None:
                    years.setdefault(annual_year, {"annual": None, "months": []})[
                        "annual"
                    ] = annual_value
                continue

            month_key = str(first).strip().casefold() if first is not None else ""
            if current_year is None or month_key not in MONTHS:
                continue

            purchase = _number(values[1]) if len(values) > 1 else None
            sale = _number(values[2]) if len(values) > 2 else None
            midpoint = _number(values[3]) if len(values) > 3 else None
            # NBC uses purchase/sale/midpoint in older blocks and one rate in G
            # from 2012 onward. G is the comparable official midpoint in both.
            official = _number(values[6]) if len(values) > 6 else None
            if purchase is None or sale is None or midpoint is None or official is None:
                continue
            month_number, month_label = MONTHS[month_key]
            years[current_year]["months"].append(
                {
                    "number": month_number,
                    "month": month_label,
                    "purchase": purchase,
                    "sale": sale,
                    "midpoint": midpoint,
                    "official": official,
                }
            )

    modified = workbook.properties.modified
    workbook.close()

    for year, record in years.items():
        record["months"].sort(key=lambda month: month["number"])
        if record["annual"] is None and record["months"]:
            record["annual"] = mean(month["official"] for month in record["months"])

    return {
        "years": dict(sorted(years.items(), reverse=True)),
        "last_updated": modified if isinstance(modified, datetime) else None,
    }


def format_rate(value) -> str:
    """Format with Excel-style half-up rounding and thousands separators."""
    if value is None:
        return "—"
    return f"{int(float(value) + 0.5):,}"
