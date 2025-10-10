import os
import json
import time
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any

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

SEATS_OUT_FILE = os.getenv("SELENIUM_SEATS_FILE", "seats.json")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = [c.strip() for c in os.getenv("CHAT_IDS", "").split(",") if c.strip()]


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


def _fetch_remote_shows() -> List[Dict[str, Any]]:
    try:
        raw_url = f"https://raw.githubusercontent.com/{REMOTE_REPO}/{REMOTE_BRANCH}/shows.json"
        logger.info(f"Fetching remote shows from {raw_url}")
        resp = requests.get(raw_url, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Remote shows fetch failed: {resp.status_code}")
            return []
        shows = resp.json()
        enriched = []
        for s in shows or []:
            if isinstance(s, str):
                enriched.append({"link": _strip_fragment(s), "dates": []})
            elif isinstance(s, dict):
                link = s.get("link") or s.get("url")
                if isinstance(link, str):
                    rec = {"link": _strip_fragment(link), "dates": s.get("dates") or []}
                    enriched.append(rec)
        logger.info(f"Loaded {len(enriched)} shows from remote state branch")
        return enriched
    except Exception as e:
        logger.warning(f"Failed to load remote shows: {e}")
        return []


def _filter_shows_for_weekend(shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter shows to only include those scheduled for the upcoming weekend (Saturday/Sunday)."""
    weekend_dates = set(_upcoming_weekend_dates())
    filtered = []
    for show in shows:
        show_dates = set(show.get("dates", []))
        if show_dates.intersection(weekend_dates):
            filtered.append(show)
    logger.info(f"Filtered {len(filtered)} weekend shows from {len(shows)} total shows")
    return filtered


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


def _extract_show_date(driver: webdriver.Chrome) -> str:
    """Try to extract show date in DD.MM.YYYY format from the current page.
    Tries common selectors first, then falls back to regex over the full HTML.
    """
    # Try obvious date containers by CSS
    candidate_selectors = [
        "div.date, span.date, .performance-date, .event-date, .show-date",
        "h2, h3, .subtitle, .info, .details",
    ]
    date_pattern = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
    for sel in candidate_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elements:
                text = (el.text or "").strip()
                m = date_pattern.search(text)
                if m:
                    return m.group(1)
        except Exception:
            pass
    # Fallback: search the entire page source
    try:
        html = driver.page_source or ""
        m = date_pattern.search(html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _date_sort_key(date_str: str):
    """Return a tuple (yyyy, mm, dd) for sorting; unknown dates go last."""
    try:
        m = re.search(r"^(\d{2})\.(\d{2})\.(\d{4})$", (date_str or "").strip())
        if not m:
            return (9999, 12, 31)
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (yyyy, mm, dd)
    except Exception:
        return (9999, 12, 31)


def _upcoming_weekend_dates() -> List[str]:
    """Return dates for the upcoming Saturday and Sunday in DD.MM.YYYY."""
    today = datetime.utcnow().date()
    # weekday(): Monday=0 ... Sunday=6; we want next Saturday (5) and Sunday (6)
    days_until_sat = (5 - today.weekday()) % 7
    days_until_sun = (6 - today.weekday()) % 7
    sat = today + timedelta(days=days_until_sat)
    sun = today + timedelta(days=days_until_sun)
    def fmt(d):
        return d.strftime("%d.%m.%Y")
    return [fmt(sat), fmt(sun)]


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

    # Title of the show if present
    title_text = ''
    try:
        title_el = driver.find_element(By.CSS_SELECTOR, 'h1')
        title_text = (title_el.text or '').strip()
    except Exception:
        title_text = ''

    # Attempt to capture the show date as early as possible
    date_text = _extract_show_date(driver)

    result = {
        "url": url,
        "title": title_text or 'Unknown Show',
        "count": 0,
        "seats": [],
        "date": date_text
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
    # Build one driver and reuse for both discovery and scraping to avoid re-downloading drivers
    driver = build_driver()
    if test_urls_env:
        ticket_urls = list({ _strip_fragment(u.strip()) for u in test_urls_env.split(',') if _is_tce_show_link(u.strip()) })
        logger.info(f"Using {len(ticket_urls)} ticket URLs from TCE_TEST_URLS")
    else:
        show_items = _fetch_remote_shows()
        if not show_items:
            logger.info("No show links to process.")
            driver.quit()
            return
        
        # Determine if we should restrict to nearest weekend dates (Friday check workflow)
        workflow_name = os.getenv("GITHUB_WORKFLOW", "").strip()
        weekend_only = (workflow_name == "Friday check") or (datetime.utcnow().weekday() == 4)
        
        if weekend_only:
            logger.info("Weekend-only mode: filtering shows for upcoming Saturday/Sunday")
            show_items = _filter_shows_for_weekend(show_items)
            if not show_items:
                logger.info("No shows scheduled for upcoming weekend.")
                driver.quit()
                return
        
        discovered = []
        for s in show_items:
            link = s.get("link") if isinstance(s, dict) else None
            if not link:
                continue
            discovered.extend(_discover_ticket_urls_from_show(driver, link))
        # unique
        seen = set()
        for u in discovered:
            if u not in seen and _is_tce_show_link(u):
                seen.add(u)
                ticket_urls.append(u)
        logger.info(f"Discovered {len(ticket_urls)} ticket pages from {len(show_items)} shows")
        
        # For weekend-only mode, filter ticket URLs to only include weekend dates
        if weekend_only:
            weekend_dates = set(_upcoming_weekend_dates())
            weekend_ticket_urls = []
            for url in ticket_urls:
                try:
                    # Quick scrape to get the date for this specific ticket page
                    driver.get(url)
                    time.sleep(0.5)  # Short delay for page load
                    date_text = _extract_show_date(driver)
                    if date_text in weekend_dates:
                        weekend_ticket_urls.append(url)
                        logger.debug(f"Ticket page {url} is for weekend date {date_text}")
                    else:
                        logger.debug(f"Ticket page {url} is for non-weekend date {date_text}, skipping")
                except Exception as e:
                    logger.warning(f"Failed to check date for ticket page {url}: {e}")
                    # If we can't determine the date, include it to be safe
                    weekend_ticket_urls.append(url)
            
            ticket_urls = weekend_ticket_urls
            logger.info(f"Filtered to {len(ticket_urls)} weekend ticket pages from {len(discovered)} total discovered")

    if not ticket_urls:
        logger.info("No ticket URLs to scrape.")
        driver.quit()
        return
    out = {}
    for u in ticket_urls:
        try:
            data = scrape_ticket_page(driver, u)
            # Store compact structure with title and count only for seats.json
            out[u] = {
                "title": data.get("title", "Unknown Show"),
                "count": int(data.get("count", 0)),
                "date": data.get("date", "")
            }
        except Exception as e:
            logger.warning(f"Failed to scrape {u}: {e}")
            continue
    driver.quit()

    # Compare with previous seats and optionally send Telegram
    previous = {}
    try:
        if os.path.exists(SEATS_OUT_FILE):
            with open(SEATS_OUT_FILE, "r", encoding="utf-8") as f:
                previous = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load previous seats: {e}")

    def send_telegram_message(message: str) -> bool:
        if not BOT_TOKEN or not CHAT_IDS:
            logger.info("BOT_TOKEN/CHAT_IDS not set; skipping Telegram notification")
            return False
        ok = True
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        for chat_id in CHAT_IDS:
            try:
                resp = requests.post(url, data={"chat_id": chat_id, "text": message})
                if not resp.ok:
                    logger.error(f"Telegram send failed for {chat_id}: {resp.text}")
                    ok = False
            except Exception as ex:
                logger.error(f"Telegram send error for {chat_id}: {ex}")
                ok = False
        return ok

    # Send notifications for all shows with tickets available (>0), sorted by date
    # Build a list of items with current count, only those with count > 0
    notify_items = []
    for url, curr in out.items():
        try:
            curr_count = int(curr.get("count", 0))
            if curr_count <= 0:
                continue
            title = curr.get("title", "Unknown Show")
            date_str = curr.get("date", "") or ""
            notify_items.append((url, title, curr_count, date_str))
        except Exception:
            continue

    # Sort by date ascending (unknown dates last)
    notify_items.sort(key=lambda item: _date_sort_key(item[3]))

    # Send per-item messages in requested format
    for url, title, count, date_str in notify_items:
        if date_str:
            msg = f"{date_str} - {title} - {count} tickets available - {url}"
        else:
            msg = f"{title} - {count} tickets available - {url}"
        logger.info(f"Notifying availability for {title} {url}: {count}")
        send_telegram_message(msg)
        
    # Save current seats with smart filtering
    try:
        from collections import OrderedDict
        
        # Determine if this is weekend-only mode (Friday check)
        workflow_name = os.getenv("GITHUB_WORKFLOW", "").strip()
        weekend_only = (workflow_name == "Friday check") or (datetime.utcnow().weekday() == 4)
        
        if weekend_only:
            # Friday check: Only update weekend shows, preserve others
            logger.info("Weekend-only mode: merging weekend show data with existing seats.json")
            
            # Load existing data
            existing_data = {}
            if os.path.exists(SEATS_OUT_FILE):
                try:
                    with open(SEATS_OUT_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except Exception as e:
                    logger.warning(f"Failed to load existing seats for merge: {e}")
            
            # Get weekend dates for filtering
            weekend_dates = set(_upcoming_weekend_dates())
            
            # Update only weekend shows in existing data
            updated_count = 0
            for url, new_data in out.items():
                # Check if this show is for the weekend
                show_date = new_data.get("date", "")
                if show_date in weekend_dates:
                    existing_data[url] = new_data
                    updated_count += 1
                    logger.info(f"Updated weekend show: {new_data.get('title', 'Unknown')} ({show_date})")
            
            # Use existing data (now with weekend updates)
            final_data = existing_data
            logger.info(f"Updated {updated_count} weekend shows, preserved {len(existing_data) - updated_count} existing shows")
        else:
            # Monday check: Update all shows (complete overwrite)
            logger.info("Full update mode: replacing all data in seats.json")
            final_data = out
        
        # Save final data, ordered by show date
        sorted_items = sorted(final_data.items(), key=lambda kv: (_date_sort_key(kv[1].get("date", "")), kv[1].get("title", "").lower(), kv[0]))
        ordered = OrderedDict(sorted_items)
        with open(SEATS_OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(ordered, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved selenium seats to {SEATS_OUT_FILE} ({len(ordered)} total shows)")
    except Exception as e:
        logger.warning(f"Failed to save output: {e}")


if __name__ == "__main__":
    main()