import asyncio
import json
import os
from playwright.async_api import async_playwright, TimeoutError
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urljoin, urlparse

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_IDS", "").split(",")  # Split comma-separated chat IDs

AFISHA_BASE = "https://puppet-minsk.by"
CATEGORY_URLS = [
    f"{AFISHA_BASE}/spektakli/spektakli-dlya-detej",
    f"{AFISHA_BASE}/spektakli/spektakli-dlya-vzroslykh",
]

SEATS_FILE = "local_seats.json" if os.getenv("GITHUB_ACTIONS") != "true" else "seats.json"
SHOWS_FILE = "local_shows.json" if os.getenv("GITHUB_ACTIONS") != "true" else "shows.json"
TICKETS_CACHE_FILE = "local_tickets_cache.json" if os.getenv("GITHUB_ACTIONS") != "true" else "tickets_cache.json"

# Remote shows source (prefer remote state branch unless explicitly disabled)
REMOTE_REPO = os.getenv("REMOTE_REPO", "Dzyamon/tickets")
REMOTE_BRANCH = os.getenv("REMOTE_SHOWS_BRANCH", "state")
USE_REMOTE_SHOWS = os.getenv("USE_REMOTE_SHOWS", "true").lower() in ("1", "true", "yes")

# Toggle to enable/disable this Afisha Check pipeline at runtime
AFISHA_CHECK_ENABLED = os.getenv("AFISHA_CHECK_ENABLED", "false").lower() in ("1", "true", "yes")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable must be set")
if not CHAT_IDS or not any(CHAT_IDS):
    raise ValueError("CHAT_IDS environment variable must be set with at least one chat ID")

def send_telegram_message(message):
    success = True
    # Telegram message limit is 4096 characters
    MAX_MESSAGE_LENGTH = 4000  # Leave some buffer
    
    for chat_id in CHAT_IDS:
        chat_id = chat_id.strip()  # Remove any whitespace
        if not chat_id:  # Skip empty chat IDs
            continue
        
        # Split message if it's too long
        if len(message) > MAX_MESSAGE_LENGTH:
            messages = []
            current_message = ""
            lines = message.split('\n')
            
            for line in lines:
                if len(current_message + line + '\n') > MAX_MESSAGE_LENGTH:
                    if current_message:
                        messages.append(current_message.strip())
                    current_message = line + '\n'
                else:
                    current_message += line + '\n'
            
            if current_message:
                messages.append(current_message.strip())
        else:
            messages = [message]
        
        # Send each message part
        for i, msg_part in enumerate(messages):
            if len(messages) > 1:
                part_header = f"Part {i+1}/{len(messages)}:\n"
                msg_part = part_header + msg_part
            
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            data = {"chat_id": chat_id, "text": msg_part}
            try:
                response = requests.post(url, data=data)
                if not response.ok:
                    logger.error(f"Failed to send Telegram message part {i+1} to {chat_id}: {response.text}")
                    success = False
                else:
                    logger.info(f"Successfully sent message part {i+1} to chat {chat_id}")
            except Exception as e:
                logger.error(f"Failed to send Telegram message part {i+1} to {chat_id}: {e}")
                success = False
                
            # Small delay between messages to avoid rate limiting
            if len(messages) > 1:
                import time
                time.sleep(0.5)
    
    return success

def load_previous_seats():
    if not os.path.exists(SEATS_FILE):
        logger.info("No previous seats file found. This might be the first run.")
        return {}
    try:
        with open(SEATS_FILE, "r", encoding="utf-8") as f:
            seats = json.load(f)
            logger.info(f"Loaded seats data for {len(seats)} shows")
            return seats
    except Exception as e:
        logger.error(f"Error loading previous seats: {e}")
        return {}

def save_seats(seats):
    try:
        with open(SEATS_FILE, "w", encoding="utf-8") as f:
            json.dump(seats, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved seats data for {len(seats)} shows")
    except Exception as e:
        logger.error(f"Error saving seats: {e}")

def _is_afisha_path(link: str) -> bool:
    try:
        if not link:
            return False
        # Consider both relative and absolute forms referring to /afisha
        if link.strip() == "/afisha":
            return True
        parsed = urlparse(link)
        if parsed.scheme in ("http", "https"):
            return parsed.path.rstrip("/") == "/afisha"
        # Relative non-empty not exactly '/afisha' already handled
        return False
    except Exception:
        return False

def _normalize_filter_dedupe_links(items):
    try:
        seen = set()
        links = []
        for it in items or []:
            if isinstance(it, str):
                raw = it
            elif isinstance(it, dict):
                raw = it.get("link")
            else:
                continue
            if not raw:
                continue
            normalized = raw if raw.startswith("http") else urljoin(AFISHA_BASE, raw)
            if _is_afisha_path(raw) or _is_afisha_path(normalized):
                continue
            if normalized not in seen:
                seen.add(normalized)
                links.append(normalized)
        return links
    except Exception:
        return []

def _only_string_urls(items):
    try:
        urls = []
        for it in items or []:
            if isinstance(it, str):
                urls.append(it)
        return urls
    except Exception:
        return []

def load_tickets_cache():
    try:
        if not os.path.exists(TICKETS_CACHE_FILE):
            return {"ticket_urls": [], "show_to_tickets": {}}
        with open(TICKETS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Normalize shapes
            ticket_urls = list({u for u in (data.get("ticket_urls") or []) if isinstance(u, str)})
            show_to_tickets = data.get("show_to_tickets") or {}
            if not isinstance(show_to_tickets, dict):
                show_to_tickets = {}
            # Ensure all values are lists of strings
            cleaned_map = {}
            for k, v in show_to_tickets.items():
                if not isinstance(k, str):
                    continue
                if isinstance(v, list):
                    cleaned_map[k] = [s for s in v if isinstance(s, str)]
            return {"ticket_urls": ticket_urls, "show_to_tickets": cleaned_map}
    except Exception as e:
        logger.debug(f"Failed to load tickets cache: {e}")
        return {"ticket_urls": [], "show_to_tickets": {}}

def save_tickets_cache(cache_data):
    try:
        with open(TICKETS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved tickets cache with {len(cache_data.get('ticket_urls', []))} urls")
    except Exception as e:
        logger.warning(f"Failed to save tickets cache: {e}")

def load_shows_from_remote():
    if not USE_REMOTE_SHOWS:
        return []
    try:
        raw_url = f"https://raw.githubusercontent.com/{REMOTE_REPO}/{REMOTE_BRANCH}/shows.json"
        logger.info(f"Fetching remote shows from {raw_url}")
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Remote shows fetch failed with status {resp.status_code}")
            return []
        shows_raw = resp.json()
        links = _normalize_filter_dedupe_links(shows_raw)
        logger.info(f"Loaded {len(links)} shows from remote {REMOTE_BRANCH} branch")
        return links
    except Exception as e:
        logger.warning(f"Failed to load remote shows: {e}")
        return []

def load_shows_from_afisha():
    try:
        # Prefer remote shows.json from the state branch
        remote = load_shows_from_remote()
        if remote:
            return remote
        # Fallback to local file
        if not os.path.exists(SHOWS_FILE):
            logger.info(f"Shows file not found: {SHOWS_FILE}")
            return []
        with open(SHOWS_FILE, "r", encoding="utf-8") as f:
            shows_raw = json.load(f)
            links = _normalize_filter_dedupe_links(shows_raw)
            logger.info(f"Loaded {len(links)} shows from {SHOWS_FILE}")
            return links
    except Exception as e:
        logger.error(f"Failed to load shows from {SHOWS_FILE}: {e}")
        return []

async def check_tickets_for_show(page, url, max_retries=3, timeout=20000):
    for attempt in range(max_retries):
        try:
            logger.debug(f"Loading page {url}")
            response = await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            if not response:
                raise Exception("Failed to get response from page")
            if not response.ok:
                raise Exception(f"Page returned status {response.status}")

            logger.debug("Waiting for page to load and checking for bot protection...")
            
            # Check if we're on the bot protection page
            try:
                protection_text = await page.query_selector("text=Making sure you're not a bot!")
                if protection_text:
                    logger.info("Bot protection detected, waiting for it to complete...")
                    # Wait for the protection to complete - look for either the seat table or the show title
                    await page.wait_for_selector("table#myHall td.place, h1", timeout=60000)  # Longer timeout for protection
                    logger.info("Bot protection completed")
            except Exception as e:
                logger.debug(f"No bot protection detected or already completed: {e}")
            
            # Now wait for seat elements
            logger.debug("Waiting for seat elements to load...")
            
            # Add a small delay to let any remaining protection processes complete
            await asyncio.sleep(2)
            
            try:
                await page.wait_for_selector("table#myHall td.place", timeout=timeout)
            except TimeoutError:
                # If seats don't load, try refreshing the page once
                logger.warning("Seats not found, trying to refresh page...")
                await page.reload()
                await asyncio.sleep(3)
                await page.wait_for_selector("table#myHall td.place", timeout=timeout)
            
            # Get show title
            title_elem = await page.query_selector("h1")
            title = await title_elem.inner_text() if title_elem else "Unknown Show"
            
            seats = await page.query_selector_all("table#myHall td.place")
            available = []
            for seat in seats:
                title_attr = await seat.get_attribute("title")
                if title_attr and "Цена" in title_attr:
                    available.append(title_attr)

            # Single concise log line per show
            logger.info(f"Found {len(available)} seats for {title} at {url}")
            return {
                "title": title,
                "url": url,
                "available_seats": available,
                "count": len(available)
            }
                
        except TimeoutError as e:
            logger.error(f"Timeout error on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(5)

async def check_all_shows():
    browser = None
    context = None
    try:
        async with async_playwright() as p:
            logger.info("Launching browser")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu'
                ]
            )
            
            logger.info("Creating browser context")
            context = await browser.new_context(
                viewport={'width': 1366, 'height': 768},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                ignore_https_errors=True,
                locale='ru-RU',
                timezone_id='Europe/Minsk',
                extra_http_headers={
                    'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'
                }
            )
            
            logger.info("Creating new page")
            page = await context.new_page()
            page.set_default_timeout(30000)

            # Seed URLs from shows file (state). Use direct tce links as tickets,
            # and non-tce links as show pages to resolve their ticket links.
            discovered_ticket_urls = set()
            discovered_show_urls = set()
            shows_from_file = load_shows_from_afisha()
            seeded_direct_ticket_urls = set()
            for link in shows_from_file:
                if not link:
                    continue
                normalized_link = link if link.startswith("http") else urljoin(AFISHA_BASE, link)
                if "tce.by" in normalized_link:
                    discovered_ticket_urls.add(normalized_link)
                    seeded_direct_ticket_urls.add(normalized_link)
                else:
                    discovered_show_urls.add(normalized_link)

            # Clarify seeding breakdown: many remote links can be direct ticket pages
            logger.info(
                f"Seeded {len(discovered_show_urls)} show pages and {len(discovered_ticket_urls)} direct ticket pages from shows source"
            )

            # For parity with show-page logs, list each seeded direct ticket as a single-link result
            for direct_url in sorted(seeded_direct_ticket_urls):
                logger.info(f"Show {direct_url} -> found 1 ticket link (direct)")

            # Load cache and seed from it for speed
            cache = load_tickets_cache()
            cached_ticket_urls = set(cache.get("ticket_urls") or [])
            cached_map = cache.get("show_to_tickets") or {}

            # Reuse cached mapping for known show pages
            for show_url in list(discovered_show_urls):
                if show_url in cached_map:
                    for t in cached_map.get(show_url, []):
                        discovered_ticket_urls.add(t)

            # If still nothing, discover ticket pages by crawling categories (with pagination/scroll), show pages, and buy pages
            if not discovered_ticket_urls and not discovered_show_urls and not cached_ticket_urls:
                for category_url in CATEGORY_URLS:
                    try:
                        logger.debug(f"Opening category {category_url}")
                        visited_pages = set()
                        pages_to_visit = [category_url]
                        while pages_to_visit:
                            cat_page_url = pages_to_visit.pop(0)
                            if cat_page_url in visited_pages:
                                continue
                            visited_pages.add(cat_page_url)
                            await page.goto(cat_page_url, wait_until='domcontentloaded')
                            # Attempt to scroll to load lazy content
                            for _ in range(5):
                                try:
                                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                                    await asyncio.sleep(0.5)
                                except Exception:
                                    break
                            # Extract links to individual show pages that contain '/item/'
                            show_links = await page.eval_on_selector_all(
                                "a[href]",
                                "(els) => Array.from(new Set(els.map(e => e.href))).filter(h => h.includes('/spektakli/') && h.includes('/item/'))"
                            )
                            for show_url in show_links:
                                discovered_show_urls.add(show_url)
                            # Find pagination links on the category page
                            pagination_links = await page.eval_on_selector_all(
                                "a[href]",
                                "els => Array.from(new Set(els.map(e => e.href))).filter(h => h.includes('start=') || h.includes('page='))"
                            )
                            for p_url in pagination_links:
                                if p_url not in visited_pages:
                                    pages_to_visit.append(p_url)
                    except Exception as e:
                        logger.debug(f"Skip category {category_url}: {e}")
                        continue

            # Visit each discovered show page and extract ticket links
            success_with_links = {}
            success_no_links = set()
            failures = {}
            for show_url in sorted(discovered_show_urls):
                try:
                    logger.info(f"Visiting show page {show_url}")
                    await page.goto(show_url, wait_until='domcontentloaded')
                    # Let dynamic content render
                    await page.wait_for_timeout(1000)
                    try:
                        await page.wait_for_load_state('networkidle', timeout=3000)
                    except Exception:
                        pass
                    # Collect direct ticket links
                    ticket_links = await page.eval_on_selector_all(
                        "a[href*='tce.by']",
                        "els => Array.from(new Set(els.map(e => e.href)))"
                    )
                    # Collect specific shows.html links
                    ticket_links_shows = await page.eval_on_selector_all(
                        "a[href*='tce.by/shows.html']",
                        "els => Array.from(new Set(els.map(e => e.href)))"
                    )
                    # Collect iframe srcs that point to tce.by
                    iframe_links = await page.eval_on_selector_all(
                        "iframe[src*='tce.by']",
                        "els => Array.from(new Set(els.map(e => e.src)))"
                    )
                    # Collect URLs from data-* attributes commonly used
                    data_attr_links = await page.evaluate("() => {\n                        const urls = new Set();\n                        const add = u => { try { if (u && u.includes('tce.by')) urls.add(u); } catch(_){} };\n                        document.querySelectorAll('[data-href],[data-url],[data-link]').forEach(el => {\n                          add(el.getAttribute('data-href'));\n                          add(el.getAttribute('data-url'));\n                          add(el.getAttribute('data-link'));\n                        });\n                        return Array.from(urls);\n                    }")
                    # Parse onclick handlers that contain tce.by
                    onclick_links = await page.evaluate("() => {\n                        const urls = new Set();\n                        const re = /(https?:\\/\\/[^'\"\s)]+tce\.by[^'\"\s)]*)/ig;\n                        document.querySelectorAll('[onclick]').forEach(el => {\n                          const txt = el.getAttribute('onclick') || '';\n                          let m;\n                          while ((m = re.exec(txt)) !== null) { urls.add(m[1]); }\n                        });\n                        return Array.from(urls);\n                    }")
                    # Scan all text content and attributes for tce.by patterns
                    text_scan_links = await page.evaluate("() => {\n                        const urls = new Set();\n                        const re = /(https?:\\/\\/[^'\"\s)]+tce\.by[^'\"\s)]*)/ig;\n                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, null, false);\n                        let node;\n                        while (node = walker.nextNode()) {\n                          if (node.nodeType === Node.TEXT_NODE) {\n                            let m;\n                            while ((m = re.exec(node.textContent)) !== null) { urls.add(m[1]); }\n                          } else if (node.nodeType === Node.ELEMENT_NODE) {\n                            for (const attr of node.attributes || []) {\n                              let m;\n                              while ((m = re.exec(attr.value)) !== null) { urls.add(m[1]); }\n                            }\n                          }\n                        }\n                        return Array.from(urls);\n                    }")
                    extracted_raw = [*ticket_links, *ticket_links_shows, *iframe_links, *data_attr_links, *onclick_links, *text_scan_links]
                    extracted = _only_string_urls(extracted_raw)
                    for t_url in extracted:
                        if isinstance(t_url, str):
                            discovered_ticket_urls.add(t_url)
                    # Update cache mapping for this show
                    if extracted:
                        cached_map.setdefault(show_url, [])
                        for t in extracted:
                            if t not in cached_map[show_url]:
                                cached_map[show_url].append(t)
                    # Summary log for this show (no full link listing)
                    unique_count = len(set(extracted))
                    if unique_count:
                        logger.info(f"Show {show_url} -> found {unique_count} ticket links")
                        success_with_links[show_url] = unique_count
                    else:
                        logger.warning(f"Show {show_url} -> no ticket links found")
                        success_no_links.add(show_url)
                    # Collect potential internal buy links by text
                    buy_links = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('a[href]')).map(a => ({href: a.href, text: (a.textContent||'').trim()}))"
                    )
                    for item in buy_links:
                        text = (item.get('text') or '').lower()
                        href = item.get('href')
                        if not href:
                            continue
                        if ('купить' in text) or ('билет' in text):
                            # Follow this local buy link
                            try:
                                await page.goto(href, wait_until='domcontentloaded')
                                await page.wait_for_timeout(800)
                                current_url = page.url
                                if isinstance(current_url, str) and 'tce.by' in current_url:
                                    discovered_ticket_urls.add(current_url)
                                inner_ticket_links = await page.eval_on_selector_all(
                                    "a[href*='tce.by']",
                                    "els => Array.from(new Set(els.map(e => e.href)))"
                                )
                                inner_shows_links = await page.eval_on_selector_all(
                                    "a[href*='tce.by/shows.html']",
                                    "els => Array.from(new Set(els.map(e => e.href)))"
                                )
                                inner_iframe_links = await page.eval_on_selector_all(
                                    "iframe[src*='tce.by']",
                                    "els => Array.from(new Set(els.map(e => e.src)))"
                                )
                                extracted_inner_raw = [*inner_ticket_links, *inner_shows_links, *inner_iframe_links]
                                extracted_inner = _only_string_urls(extracted_inner_raw)
                                for t_url in extracted_inner:
                                    if isinstance(t_url, str):
                                        discovered_ticket_urls.add(t_url)
                                if extracted_inner:
                                    cached_map.setdefault(show_url, [])
                                    for t in extracted_inner:
                                        if isinstance(t, str) and t not in cached_map[show_url]:
                                            cached_map[show_url].append(t)
                            except Exception as e:
                                logger.debug(f"Skip buy link {href}: {e}")
                                continue
                except Exception as e:
                    failures[show_url] = str(e)
                    logger.warning(f"Skip show {show_url}: {e}")
                    continue

            # End-of-crawl summary for show pages
            try:
                total_seeded = len(discovered_show_urls)
                total_visited = len(success_with_links) + len(success_no_links) + len(failures)
                logger.info(
                    f"Show pages summary: seeded={total_seeded}, visited={total_visited}, "
                    f"with_links={len(success_with_links)}, no_links={len(success_no_links)}, "
                    f"failed={len(failures)}"
                )
                if failures:
                    # List failed URLs with reasons (trim reason length)
                    for url, reason in list(failures.items()):
                        trimmed = (reason[:300] + '...') if len(reason) > 300 else reason
                        logger.warning(f"Failed show: {url} — {trimmed}")
            except Exception:
                pass

            # Merge with cached urls and save cache
            all_ticket_urls = sorted(set(list(discovered_ticket_urls) + list(cached_ticket_urls)))
            logger.info(f"Discovered {len(discovered_ticket_urls)} ticket pages from {len(discovered_show_urls)} show pages (cache total {len(all_ticket_urls)})")
            save_tickets_cache({"ticket_urls": all_ticket_urls, "show_to_tickets": cached_map})

            current_seats = {}
            for url in all_ticket_urls:
                try:
                    show_data = await check_tickets_for_show(page, url)
                    current_seats[url] = show_data
                except Exception as e:
                    logger.error(f"Failed to check show at {url}: {e}")
                    continue

            await context.close()
            await browser.close()
            return current_seats

    except Exception as e:
        logger.error(f"Error in check_all_shows: {e}")
        if context:
            await context.close()
        if browser:
            await browser.close()
        raise

def main():
    try:
        if not AFISHA_CHECK_ENABLED:
            logger.info("Afisha Check is disabled via env AFISHA_CHECK_ENABLED. Skipping.")
            return
        logger.info("Starting ticket check")
        previous_seats = load_previous_seats()
        current_seats = asyncio.run(check_all_shows())
        
        # If this is the first run, just save the data
        if not previous_seats:
            logger.info("First run detected. Saving seats data without sending notifications.")
            save_seats(current_seats)
            return
            
        # Check for changes in each show
        for url, current_data in current_seats.items():
            previous_data = previous_seats.get(url, {"count": 0, "available_seats": []})
            
            # If there are new seats available
            if current_data["count"] > previous_data["count"]:
                new_seats = set(current_data["available_seats"]) - set(previous_data["available_seats"])
                # Create a more concise message format
                if len(new_seats) <= 5:
                    # For small numbers of seats, list them all
                    seat_list = "\n".join(f"• {seat}" for seat in new_seats)
                    msg = (
                        f"🎫 Found new tickets for {current_data['title']} — {url}\n"
                        f"New seats: {len(new_seats)}\n"
                        f"Total available: {current_data['count']}\n\n"
                        f"New seats:\n{seat_list}"
                    )
                else:
                    # For many seats, provide a summary and list first few
                    first_seats = "\n".join(f"• {seat}" for seat in list(new_seats)[:5])
                    remaining_count = len(new_seats) - 5
                    msg = (
                        f"🎫 Found new tickets for {current_data['title']} — {url}\n"
                        f"New seats: {len(new_seats)}\n"
                        f"Total available: {current_data['count']}\n\n"
                        f"First 5 new seats:\n{first_seats}\n\n"
                        f"... and {remaining_count} more seats"
                    )
                logger.info(f"Found new seats for {current_data['title']} {url}")
                send_telegram_message(msg)
        
        # Save current state
        save_seats(current_seats)
            
    except Exception as e:
        error_msg = f"Error checking tickets: {str(e)}"
        logger.error(error_msg)
        send_telegram_message(error_msg)

if __name__ == "__main__":
    main()
