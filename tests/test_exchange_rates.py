import json
from datetime import datetime

from openpyxl import Workbook

from exchange_rates import format_rate, load_exchange_rates, parse_gdt_latest_rate


def build_workbook(path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Parallel", None, None, None, None, "Official"])
    sheet.append([2025])
    sheet.append(["March", 4002.3, 4011.9, 4007.1, None, None, 4005])
    sheet.append(["January", 4018, 4032, 4025, None, None, 4024])
    sheet.append(["Annual 2025", None, None, None, None, None, 4050])
    sheet.append(["Annual 2010", None, None, None, None, None, 4200])
    workbook.properties.modified = datetime(2026, 9, 3, 16, 45)
    workbook.save(path)


def test_parser_supports_monthly_and_annual_only_years(tmp_path):
    path = tmp_path / "rates.xlsx"
    build_workbook(path)

    result = load_exchange_rates(path)

    assert list(result["years"]) == [2025, 2010]
    assert result["years"][2025]["annual"] == 4050
    assert [row["month"] for row in result["years"][2025]["months"]] == [
        "Jan",
        "Mar",
    ]
    assert result["years"][2010] == {
        "annual": 4200,
        "months": [],
        "toi_rate_available": True,
    }


def test_parser_does_not_invent_toi_rate_before_fiscal_year_end(tmp_path):
    path = tmp_path / "rates.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([2024])
    sheet.append(["January", 4000, 4010, 4005, None, None, 3990])
    sheet.append(["February", 4020, 4030, 4025, None, None, 4010])
    workbook.save(path)

    result = load_exchange_rates(path)

    assert result["years"][2024]["annual"] is None
    assert result["years"][2024]["toi_rate_available"] is False


def test_parser_uses_year_end_official_rate_for_completed_toi_year(tmp_path):
    path = tmp_path / "rates.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([2024])
    sheet.append(["January", 4080, 4091, 4085.5, None, None, 4083])
    sheet.append(["December", 4024, 4035, 4029.5, None, None, 4025])
    workbook.save(path)

    result = load_exchange_rates(path)

    assert result["years"][2024]["annual"] == 4025
    assert result["years"][2024]["toi_rate_available"] is True


def test_parser_merges_verified_monthly_updates_without_duplicates(tmp_path):
    path = tmp_path / "rates.xlsx"
    build_workbook(path)
    updates = {
        "monthly_rates": [
            {
                "year": 2025,
                "month": "March",
                "purchase": 3996,
                "sale": 4008,
                "midpoint": 4002,
                "official": 4000,
            },
            {
                "year": 2026,
                "month": "January",
                "purchase": 4023,
                "sale": 4034,
                "midpoint": 4029,
                "official": 4026,
            },
        ]
    }
    path.with_name("exchange_rate_updates.json").write_text(
        json.dumps(updates), encoding="utf-8"
    )

    result = load_exchange_rates(path)

    months_2025 = result["years"][2025]["months"]
    assert [month["month"] for month in months_2025] == ["Jan", "Mar"]
    assert months_2025[1]["purchase"] == 3996
    assert result["years"][2026]["months"][0]["official"] == 4026
    assert result["years"][2026]["annual"] is None
    assert result["years"][2026]["toi_rate_available"] is False


def test_rate_formatting_uses_whole_numbers_and_thousands_separators():
    assert format_rate(4002.3) == "4,002"
    assert format_rate(4011.9) == "4,012"
    assert format_rate(4050.0) == "4,050"


def test_gdt_parser_reads_first_official_usd_rate():
    page = """
        <table><tr><th>Release Date</th><th>Symbol</th><th>Official Rate</th></tr>
        <tr><td>September 2, 2026</td><td>USD/KHR</td><td>4,047</td>
        <td>National Bank of Cambodia</td></tr>
        <tr><td>September 1, 2026</td><td>USD/KHR</td><td>4,046</td>
        <td>National Bank of Cambodia</td></tr></table>
    """

    latest = parse_gdt_latest_rate(page)

    assert latest["rate"] == 4047
    assert latest["published_at"] == datetime(2026, 9, 2)
