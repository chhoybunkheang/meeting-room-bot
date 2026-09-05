import importlib
import os
import sys
from datetime import datetime

import dotenv
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="module")
def mini_app_module():
    original_load_dotenv = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    environment = {
        "BOT_TOKEN": "123456:test-token",
        "DATABASE_URL": "postgresql+psycopg2://test:test@localhost/test",
        "GROUP_CHAT_ID": "-100123456",
        "ADMIN_ID": "42",
        "TELEGRAM_INIT_DATA_MAX_AGE": "3600",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    sys.modules.pop("mini_app", None)
    try:
        yield importlib.import_module("mini_app")
    finally:
        sys.modules.pop("mini_app", None)
        dotenv.load_dotenv = original_load_dotenv
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def render_page(template, selected_year=2026, latest_rate=4048):
    return template.render(
        exchange_years={
            2026: {
                "annual": None,
                "toi_rate_available": False,
                "annual_method": "gdt_year_end",
                "annual_source_url": None,
                "annual_published_at": None,
                "annual_verification": None,
                "months": [
                    {"month": "Jan", "purchase": 4000, "sale": 4010,
                     "midpoint": 4005, "official": 4004},
                ],
            },
            2025: {
                "annual": 4013,
                "toi_rate_available": True,
                "annual_method": "gdt_year_end",
                "annual_source_url": "https://www.tax.gov.kh/en/exchange-rate",
                "annual_published_at": "2025-12-31",
                "annual_verification": "official_publication",
                "months": [
                    {"month": "Dec", "purchase": 4011, "sale": 4015,
                     "midpoint": 4013, "official": 4013},
                ],
            },
        },
        selected_year=selected_year,
        current_year=2026,
        rate_status={
            "stale": False,
            "cached": False,
            "checked_at_label": None,
            "attempted_at_label": None,
            "refresh_throttled": False,
        },
        last_updated=None,
        latest_official_rate={
            "rate": latest_rate,
            "published_at": datetime(2026, 9, 4),
            "source_url": "https://www.tax.gov.kh/gdtwebsiteweb/en/exchange-rate",
        },
    )


def test_year_switch_renders_the_selected_monthly_data(mini_app_module):
    template = mini_app_module.templates.env.get_template("exchange_rate.html")
    html = render_page(template)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        page.select_option("#fiscalYear", "2025")

        assert page.locator("#annualRate").inner_text() == "4,013"
        monthly_text = " ".join(page.locator("#monthlyRates").inner_text().split())
        assert monthly_text == "Dec 4,011 4,015 4,013 4,013"
        browser.close()


def test_refresh_failure_keeps_the_displayed_rate(mini_app_module):
    template = mini_app_module.templates.env.get_template("exchange_rate.html")
    html = render_page(template)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        page.click("#refreshRates")
        page.wait_for_function("document.querySelector('#refreshStatus').textContent.includes('Could not refresh')")

        assert "4,048" in page.locator("#latestOfficialRate").inner_text()
        browser.close()


def test_mobile_sticky_layers_do_not_cover_monthly_table(mini_app_module):
    template = mini_app_module.templates.env.get_template("exchange_rate.html")
    html = render_page(template)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_content(html)
        page.locator("#monthlyTableCard").scroll_into_view_if_needed()

        header = page.locator(".exchange-header").bounding_box()
        summary = page.locator(".exchange-summary-grid").bounding_box()
        table = page.locator("#monthlyTableCard").bounding_box()

        assert header is not None and summary is not None and table is not None
        header_bottom = header["y"] + header["height"]
        summary_top = summary["y"]
        summary_bottom = summary["y"] + summary["height"]
        table_top = table["y"]
        assert summary_top >= header_bottom - 1
        assert table_top >= summary_bottom - 1
        browser.close()