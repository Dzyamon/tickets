import os
import json
import time
import logging
from typing import List

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


AFISHA_BASE = "https://puppet-minsk.by"
REMOTE_REPO = os.getenv("REMOTE_REPO", "Dzyamon/tickets")
REMOTE_BRANCH = os.getenv("REMOTE_SHOWS_BRANCH", "state")

SEATS_OUT_FILE = os.getenv("SELENIUM_SEATS_FILE", "selenium_seats.json")


def _strip_fragment(url: str) -> str:
    try:
        return url.split('#')[0] if isinstance(url, str) else url
    except Exception:
        return url


def _is_tce_show_link(url: str) -> bool:
    try:
        if not isinstance(url, str) or not url:
            return False
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        host = (parsed.netloc or '').lower()
        path = (parsed.path or '').lower()
        if 'tce.by' not in host:
            return False
        if not path.endswith('/shows.html') and not path.endswith('shows.html'):
            return False
        qs = parse_qs(parsed.query or '')
        return 'base' in qs and 'data' in qs
    except Exception:
        return False


def _fetch_remote_shows() -> List[str]:
    try:
        raw_url = f"https://raw.githubusercontent.com/{REMOTE_REPO}/{REMOTE_BRANCH}/shows.json"
        logger.info(f"Fetching remote shows from {raw_url}")
        resp = requests.get(raw_url, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Remote shows fetch failed: {resp.status_code}")
            return []
        shows = resp.json()
        links = []
        for s in shows or []:
            link = s.get("link") if isinstance(s, dict) else (s if isinstance(s, str) else None)
            if isinstance(link, str):
                links.append(_strip_fragment(link))
        logger.info(f"Loaded {len(links)} shows from remote state branch")
        return links
    except Exception as e:
        logger.warning(f"Failed to load remote shows: {e}")
        return []


def _discover_ticket_urls_from_show(driver: webdriver.Chrome, show_url: str) -> List[str]:
    urls = []
    try:
        driver.get(show_url)
        time.sleep(1.2)
        anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='tce.by/shows.html']")
        for a in anchors:
            try:
                href = a.get_attribute('href')
                if _is_tce_show_link(href):
                    urls.append(_strip_fragment(href))
            except Exception:
                continue
        iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='tce.by/shows.html']")
        for fr in iframes:
            try:
                src = fr.get_attribute('src')
                if _is_tce_show_link(src):
                    urls.append(_strip_fragment(src))
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Show {show_url} discovery failed: {e}")
    # unique preserve order
    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def build_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,768")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"]) 
    options.add_experimental_option('useAutomationExtension', False)
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def scrape_ticket_page(driver: webdriver.Chrome, url: str) -> dict:
    driver.get(url)
    logger.info(f"Opened ticket page: {url}")

    # Wait for either seat map or Anubis marker to resolve
    seat_selector = 'td.place[title*="Цена"]'
    start = time.time()
    seats: List = []
    try:
        wait = WebDriverWait(driver, 40)
        seats = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, seat_selector)))
    except TimeoutException:
        # Try a broader selector and small delay loops (handle cases where title is added later)
        for _ in range(6):
            time.sleep(2)
            elems = driver.find_elements(By.CSS_SELECTOR, 'table#myHall td.place, td.place')
            if elems:
                # Filter by having title with price
                filtered = [e for e in elems if 'цена' in (e.get_attribute('title') or '').lower()]
                if filtered:
                    seats = filtered
                    break

    result = {
        "url": url,
        "count": 0,
        "seats": []
    }

    if not seats:
        logger.info("No priced seats found")
        return result

    result["count"] = len(seats)
    # Collect titles
    titles = []
    for e in seats:
        try:
            t = e.get_attribute('title')
            if t:
                titles.append(t)
        except Exception:
            continue
    result["seats"] = titles
    logger.info(f"Found {len(seats)} available seats with a price on {url}")
    return result


def main():
    # Load explicit test URLs if provided; else discover from remote shows
    test_urls_env = os.getenv("TCE_TEST_URLS", "").strip()
    ticket_urls: List[str] = []
    if test_urls_env:
        ticket_urls = list({ _strip_fragment(u.strip()) for u in test_urls_env.split(',') if _is_tce_show_link(u.strip()) })
        logger.info(f"Using {len(ticket_urls)} ticket URLs from TCE_TEST_URLS")
    else:
        show_links = _fetch_remote_shows()
        if not show_links:
            logger.info("No show links to process.")
            return
        driver = build_driver()
        discovered = []
        try:
            for s in show_links:
                discovered.extend(_discover_ticket_urls_from_show(driver, s))
        finally:
            driver.quit()
        # unique
        seen = set()
        for u in discovered:
            if u not in seen and _is_tce_show_link(u):
                seen.add(u)
                ticket_urls.append(u)
        logger.info(f"Discovered {len(ticket_urls)} ticket pages from {len(show_links)} shows")

    if not ticket_urls:
        logger.info("No ticket URLs to scrape.")
        return

    driver = build_driver()
    out = {}
    try:
        for u in ticket_urls:
            try:
                data = scrape_ticket_page(driver, u)
                out[u] = data
            except Exception as e:
                logger.warning(f"Failed to scrape {u}: {e}")
                continue
    finally:
        driver.quit()

    try:
        with open(SEATS_OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved selenium seats to {SEATS_OUT_FILE}")
    except Exception as e:
        logger.warning(f"Failed to save output: {e}")


if __name__ == "__main__":
    main()


