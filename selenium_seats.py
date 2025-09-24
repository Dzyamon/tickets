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


def _load_ticket_urls() -> List[str]:
    # Highest priority: explicit env override
    test_urls_env = os.getenv("TCE_TEST_URLS", "").strip()
    if test_urls_env:
        urls = list({ _strip_fragment(u.strip()) for u in test_urls_env.split(",") if _is_tce_show_link(u.strip()) })
        if urls:
            logger.info(f"Using {len(urls)} ticket URLs from TCE_TEST_URLS")
            return urls

    # Next: tickets_cache.json if present
    try:
        cache_path = os.getenv("TICKETS_CACHE_FILE", "local_tickets_cache.json")
        if os.getenv("GITHUB_ACTIONS") == "true":
            cache_path = "tickets_cache.json"
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                urls = [ _strip_fragment(u) for u in (data.get("ticket_urls") or []) if _is_tce_show_link(u) ]
                if urls:
                    logger.info(f"Using {len(urls)} ticket URLs from {cache_path}")
                    return urls
    except Exception as e:
        logger.warning(f"Failed to read tickets cache: {e}")

    # Fallback: remote shows.json and filter direct tce links if any
    try:
        raw_url = f"https://raw.githubusercontent.com/{REMOTE_REPO}/{REMOTE_BRANCH}/shows.json"
        logger.info(f"Fetching remote shows from {raw_url}")
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code == 200:
            shows = resp.json()
            results = []
            for s in shows or []:
                link = s.get("link") if isinstance(s, dict) else (s if isinstance(s, str) else None)
                if not link:
                    continue
                if _is_tce_show_link(link):
                    results.append(_strip_fragment(link))
            if results:
                logger.info(f"Using {len(results)} ticket URLs from remote shows.json")
                return results
    except Exception as e:
        logger.warning(f"Failed to load remote shows: {e}")

    logger.info("No ticket URLs found from any source")
    return []


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
    urls = _load_ticket_urls()
    if not urls:
        logger.info("Nothing to process.")
        return

    driver = build_driver()
    out = {}
    try:
        for u in urls:
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


