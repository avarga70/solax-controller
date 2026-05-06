#!/usr/bin/env python3
"""
Download yesterday's energy consumption and production CSVs from pnd.cezdistribuce.cz.
Uses Playwright (headless Firefox) to handle the JavaScript-based login and OAuth flow.
Saves:
  - consumption -> /tmp/pnd_export.csv
  - production  -> /tmp/pnd_export (1).csv
"""

from datetime import date, timedelta
from playwright.sync_api import sync_playwright, Download

USERNAME = "Akos.Varga@mail.com"
PASSWORD = "TbBVp955bPShvXC"

DEVICE_SET_ID = 39558
ASSEMBLY_CONSUMPTION = -1001
ASSEMBLY_PRODUCTION = -1002

OUTPUT_CONSUMPTION = "/tmp/pnd_export.csv"
OUTPUT_PRODUCTION = "/tmp/pnd_export (1).csv"

PND_DASHBOARD_URL = "https://pnd.cezdistribuce.cz/cezpnd2/external/dashboard/view"
PND_EXPORT_URL = "https://pnd.cezdistribuce.cz/cezpnd2/external/data/export"


def date_param(d: date) -> str:
    """Format date as DD.MM.YYYY 00:00 (URL-encoded colon and space)."""
    return d.strftime("%d.%m.%Y") + "%2000%3A00"


def main() -> None:
    yesterday = date.today() - timedelta(days=1)
    today = date.today()
    interval_from = date_param(yesterday)
    interval_to = date_param(today)
    print(f"Downloading data for {yesterday.strftime('%d.%m.%Y')}")

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # Step 1: Navigate to PND — redirects to DIP login page
        print("Loading login page...")
        page.goto(PND_DASHBOARD_URL, wait_until="networkidle")

        # Step 2: Fill and submit login form
        print(f"Current URL: {page.url}")
        page.fill('input[name="username"], input[id="username"], input[type="text"]', USERNAME)
        page.fill('input[name="password"], input[id="password"], input[type="password"]', PASSWORD)
        page.click('button[type="submit"], input[type="submit"], button:has-text("Přihlásit"), button:has-text("Login")')

        # Wait for redirect back to PND dashboard
        page.wait_for_url("**/cezpnd2/**", timeout=30000)
        print(f"Logged in. URL: {page.url}")

        # Step 3: Download consumption CSV
        print("Downloading consumption CSV...")
        url_consumption = (
            f"{PND_EXPORT_URL}?format=csv-simple"
            f"&idAssembly={ASSEMBLY_CONSUMPTION}"
            f"&idDeviceSet={DEVICE_SET_ID}"
            f"&intervalFrom={interval_from}"
            f"&intervalTo={interval_to}"
            f"&compareFrom=&opmId=&electrometerId=&splitStrategy="
        )
        with page.expect_download(timeout=30000) as dl_info:
            page.evaluate(f"window.location.href = {url_consumption!r}")
        download: Download = dl_info.value
        download.save_as(OUTPUT_CONSUMPTION)
        print(f"Saved -> {OUTPUT_CONSUMPTION}")

        # Step 4: Download production CSV
        print("Downloading production CSV...")
        url_production = (
            f"{PND_EXPORT_URL}?format=csv-simple"
            f"&idAssembly={ASSEMBLY_PRODUCTION}"
            f"&idDeviceSet={DEVICE_SET_ID}"
            f"&intervalFrom={interval_from}"
            f"&intervalTo={interval_to}"
            f"&compareFrom=&opmId=&electrometerId=&splitStrategy="
        )
        with page.expect_download(timeout=30000) as dl_info:
            page.evaluate(f"window.location.href = {url_production!r}")
        download = dl_info.value
        download.save_as(OUTPUT_PRODUCTION)
        print(f"Saved -> {OUTPUT_PRODUCTION}")

        browser.close()

    print("Done.")


if __name__ == "__main__":
    main()
