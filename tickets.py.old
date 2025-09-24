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
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

AFISHA_BASE = "https://puppet-minsk.by"
CATEGORY_URLS = [
    f"{AFISHA_BASE}/spektakli/spektakli-dlya-detej",
    f"{AFISHA_BASE}/spektakli/spektakli-dlya-vzroslykh",
]

# External ticketing domains to detect (restricted to tce.by)
PARTNER_DOMAINS = [
    "tce.by",
]

def _is_partner_url(url: str) -> bool:
    try:
        if not isinstance(url, str) or not url:
            return False
        u = url.lower()
        return any(domain in u for domain in PARTNER_DOMAINS)
    except Exception:
        return False

SEATS_FILE = "local_seats.json" if os.getenv("GITHUB_ACTIONS") != "true" else "seats.json"
SHOWS_FILE = "local_shows.json" if os.getenv("GITHUB_ACTIONS") != "true" else "shows.json"
TICKETS_CACHE_FILE = "local_tickets_cache.json" if os.getenv("GITHUB_ACTIONS") != "true" else "tickets_cache.json"

# Toggle cache usage for tickets discovery (disable to force fresh crawl)
# Default is disabled per user request; set USE_TICKETS_CACHE=true to enable
USE_TICKETS_CACHE = os.getenv("USE_TICKETS_CACHE", "false").lower() in ("1", "true", "yes")

# Remote shows source (prefer remote state branch unless explicitly disabled)
REMOTE_REPO = os.getenv("REMOTE_REPO", "Dzyamon/tickets")
REMOTE_BRANCH = os.getenv("REMOTE_SHOWS_BRANCH", "state")
USE_REMOTE_SHOWS = os.getenv("USE_REMOTE_SHOWS", "true").lower() in ("1", "true", "yes")

if not DRY_RUN:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable must be set")
    if not CHAT_IDS or not any(CHAT_IDS):
        raise ValueError("CHAT_IDS environment variable must be set with at least one chat ID")

def send_telegram_message(message):
    if DRY_RUN:
        logger.info("DRY_RUN: Skipping Telegram message send")
        return True
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
            # Gracefully handle empty/invalid JSON as first run
            try:
                seats = json.load(f)
            except json.JSONDecodeError:
                logger.info("Seats file is empty or invalid JSON. Treating as first run.")
                return {}
            logger.info(f"Loaded seats data for {len(seats)} shows")
            return seats
    except Exception as e:
        logger.warning(f"Error loading previous seats, continuing as first run: {e}")
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

def _strip_fragment(url: str) -> str:
    try:
        if isinstance(url, str):
            return url.split('#')[0]
        return url
    except Exception:
        return url

def _is_tce_show_link(url: str) -> bool:
    try:
        if not isinstance(url, str) or not url:
            return False
        parsed = urlparse(url)
        host = (parsed.netloc or '').lower()
        path = (parsed.path or '').lower()
        if 'tce.by' not in host:
            return False
        if not path.endswith('/shows.html') and not path.endswith('shows.html'):
            return False
        # Must contain both base and data query params
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query or '')
        return 'base' in qs and 'data' in qs
    except Exception:
        return False

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

async def check_tickets_for_show(page, url, max_retries=3, timeout=40000):
    for attempt in range(max_retries):
        try:
            logger.debug(f"Loading page {url}")
            response = await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            # Some pages behind bot protection might return null response; rely on DOM checks instead
            if response and not response.ok:
                raise Exception(f"Page returned status {response.status}")

            logger.debug("Waiting for page to load and checking for bot protection...")
            
            # Enhanced bot protection handling for Anubis
            logger.info("Checking for bot protection and waiting for page to fully load...")
            
            # Always wait for the page to be fully loaded, regardless of protection detection
            try:
                # Wait for network to be idle (no requests for 500ms)
                await page.wait_for_load_state('networkidle', timeout=15000)
                logger.info("Page network idle state reached")
            except Exception:
                logger.debug("Network idle timeout, continuing...")
            
            # Check for bot protection indicators and wait for Anubis to complete
            protection_detected = False
            try:
                body_text = (await page.evaluate("() => document.body.innerText")).lower()
                if any(k in body_text for k in [
                    "making sure you're not a bot!".lower(),
                    "anubis",
                    "proof-of-work",
                    "why am i seeing this?"
                ]):
                    protection_detected = True
            except Exception:
                pass
            if protection_detected:
                logger.info("Waiting for Anubis protection to complete (PoW)")
                # Poll up to 3 minutes for real content markers to appear
                try:
                    await page.wait_for_function("""
                        () => {
                          const t = (document.body.innerText||'').toLowerCase();
                          const stillProtected = t.includes('making sure you\'re not a bot!') || t.includes('anubis') || t.includes('proof-of-work');
                          const hasSeats = document.querySelector('table#myHall td.place') !== null;
                          const hasTitle = document.querySelector('h1') !== null;
                          return (!stillProtected) && (hasSeats || hasTitle);
                        }
                    """, timeout=180000)
                    logger.info("Anubis challenge passed")
                except Exception:
                    logger.info("Anubis wait timed out; continuing best-effort")
            
            # Always wait for content to load, even if no protection detected
            if protection_detected:
                logger.info("Waiting for Anubis protection to complete (up to 3 minutes)...")
            else:
                logger.info("Waiting for page content to fully load (up to 2 minutes)...")
            
            # Wait for the page to be fully loaded with multiple strategies
            try:
                # Strategy 1: Wait for either seats or title to appear
                await page.wait_for_function("""
                    () => {
                        // Check if protection page is gone
                        const protectionTexts = [
                            "Making sure you're not a bot!",
                            "Loading...",
                            "Why am I seeing this?",
                            "Anubis"
                        ];
                        const bodyText = document.body.innerText;
                        const hasProtection = protectionTexts.some(text => bodyText.includes(text));
                        
                        // Check if actual content is loaded
                        const hasSeats = document.querySelector("table#myHall td.place") !== null;
                        const hasTitle = document.querySelector("h1") !== null;
                        const hasAnyContent = document.querySelector("body").innerText.length > 100;
                        
                        return !hasProtection && (hasSeats || hasTitle || hasAnyContent);
                    }
                """, timeout=180000)  # 3 minutes
                logger.info("Page content loaded successfully")
            except Exception as e:
                logger.warning(f"Content wait failed: {e}")
                
                # Strategy 2: Try reloading and waiting again
                logger.info("Trying page reload to ensure proper loading...")
                try:
                    await page.reload()
                    await asyncio.sleep(5)
                    
                    # Wait for either seats or title to appear
                    await page.wait_for_selector("table#myHall td.place, h1", timeout=120000)
                    logger.info("Content loaded after reload")
                except Exception as e2:
                    logger.warning(f"Reload also failed: {e2}")
            
            # Additional wait to ensure all JavaScript has finished
            logger.info("Waiting for JavaScript to complete...")
            await asyncio.sleep(5)
            
            # Try to scroll to trigger any lazy loading
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(2)
            except Exception:
                pass
            
            # Try to target the correct frame if seats are rendered inside an iframe (tce.by)
            target_context = page
            try:
                # Prefer a frame whose URL contains tce.by/shows.html
                for fr in page.frames:
                    try:
                        fr_url = (fr.url or '').lower()
                    except Exception:
                        continue
                    if 'tce.by' in fr_url and 'shows.html' in fr_url:
                        target_context = fr
                        break
                # Fallback: first frame that points to tce.by
                if target_context is page:
                    for fr in page.frames:
                        try:
                            fr_url = (fr.url or '').lower()
                        except Exception:
                            continue
                        if 'tce.by' in fr_url:
                            target_context = fr
                            break
            except Exception:
                pass

            # Now wait for seat elements with improved detection
            logger.info("Looking for seat elements...")
            
            # Try multiple selectors for seats with longer timeouts
            seat_selectors = [
                "table#myHall td.place",
                "td.place",
                ".place",
                "[title*='Цена']",
                "td[title*='Цена']",
                "td[onclick]",
                ".seat",
                "[onclick*='seat']"
            ]
            
            seats_found = False
            for selector in seat_selectors:
                try:
                    elements = await target_context.query_selector_all(selector)
                    if elements:
                        logger.info(f"Found {len(elements)} elements with selector: {selector}")
                        seats_found = True
                        break
                except Exception as e:
                    logger.debug(f"Selector {selector} failed: {e}")
                    continue
            
            if not seats_found:
                logger.warning("No seat elements found, trying page refresh...")
                try:
                    await page.reload()
                    await asyncio.sleep(5)
                    
                    # Try again after refresh
                    for selector in seat_selectors:
                        try:
                            elements = await target_context.query_selector_all(selector)
                            if elements:
                                logger.info(f"Found {len(elements)} elements after refresh with selector: {selector}")
                                seats_found = True
                                break
                        except Exception:
                            continue
                except Exception as e:
                    logger.warning(f"Page refresh failed: {e}")
            
            # Get show title
            title_elem = await page.query_selector("h1")
            title = await title_elem.inner_text() if title_elem else "Unknown Show"
            
            # Try multiple approaches to find seats
            available = []
            logger.info("Searching for available seats...")
            
            # Method 0: Heuristic scan for availability by classes/data/text
            try:
                heuristic = await target_context.evaluate("""
                    () => {
                        const results = [];
                        const cells = Array.from(document.querySelectorAll('table#myHall td.place, td.place'));
                        const isAvailableClass = (cls) => {
                            if (!cls) return false;
                            const c = ' ' + String(cls).toLowerCase() + ' ';
                            return / free | svobod | свобод | available /.test(c) && !/ busy | sold | reserved | booked | занято | занята | заняты | occupied | продан /.test(c);
                        };
                        for (const td of cells) {
                            try {
                                const cls = td.className || '';
                                const ds = td.dataset || {};
                                const text = (td.textContent || '').trim();
                                const title = td.getAttribute('title') || '';
                                const onclick = td.getAttribute('onclick') || '';
                                const hasPrice = /Цена|цена/.test(title) || /Цена|цена/.test(onclick) || ds.price || ds.cost || ds.sum;
                                const numericSeat = text && /^\d+$/.test(text) && text !== '0';
                                // Treat as available only if classes suggest free OR numeric with no sold/busy marks
                                const notBusy = !/ busy | sold | reserved | booked | занято | занята | заняты | occupied | продан /i.test(' ' + cls + ' ');
                                const isAvail = (isAvailableClass(cls) || (numericSeat && notBusy)) || ds.available === 'true' || ds.free === 'true';
                                if (isAvail) {
                                    const row = ds.row || ds.r || '';
                                    const seat = ds.seat || ds.s || (numericSeat ? text : '');
                                    const price = ds.price || ds.cost || ds.sum || '';
                                    let info = title || onclick;
                                    if (!info) {
                                        const parts = [];
                                        if (row) parts.push(`Ряд: ${row}`);
                                        if (seat) parts.push(`Место: ${seat}`);
                                        if (price) parts.push(`Цена: ${price}`);
                                        info = parts.join(', ');
                                    }
                                    if (info) results.push(info);
                                }
                            } catch (_) {}
                        }
                        return Array.from(new Set(results));
                    }
                """)
                if isinstance(heuristic, list) and heuristic:
                    for it in heuristic:
                        if isinstance(it, str):
                            available.append(it)
                    logger.info(f"Heuristic availability found {len(heuristic)} seats")
            except Exception:
                pass

            # Method 1: Look for td.place elements
            seats = await target_context.query_selector_all("table#myHall td.place")
            logger.info(f"Found {len(seats)} td.place elements")
            for seat in seats:
                title_attr = await seat.get_attribute("title")
                if title_attr and "Цена" in title_attr:
                    available.append(title_attr)
                    logger.debug(f"Found seat: {title_attr}")
            
            # Method 2: If no seats found, try broader search
            if not available:
                all_places = await target_context.query_selector_all("td.place, .place")
                logger.info(f"Found {len(all_places)} place elements")
                for place in all_places:
                    title_attr = await place.get_attribute("title")
                    if title_attr and "Цена" in title_attr:
                        available.append(title_attr)
                        logger.debug(f"Found seat: {title_attr}")
            
            # Method 3: Look for any element with price information
            if not available:
                price_elements = await target_context.query_selector_all("[title*='Цена'], [title*='цена']")
                logger.info(f"Found {len(price_elements)} price elements")
                for elem in price_elements:
                    title_attr = await elem.get_attribute("title")
                    if title_attr and ("Цена" in title_attr or "цена" in title_attr):
                        available.append(title_attr)
                        logger.debug(f"Found price element: {title_attr}")
            
            # Method 4: Check if there are any clickable seat elements
            if not available:
                clickable_seats = await target_context.query_selector_all("td[onclick], .seat[onclick], [onclick*='seat']")
                logger.info(f"Found {len(clickable_seats)} clickable seat elements")
                for seat in clickable_seats:
                    title_attr = await seat.get_attribute("title")
                    onclick_attr = await seat.get_attribute("onclick")
                    if title_attr and "Цена" in title_attr:
                        available.append(title_attr)
                        logger.debug(f"Found clickable seat: {title_attr}")
                    elif onclick_attr and "Цена" in onclick_attr:
                        available.append(onclick_attr)
                        logger.debug(f"Found clickable seat onclick: {onclick_attr}")
            
            # Method 5: Look for any table cells that might contain seat information
            if not available:
                all_tds = await target_context.query_selector_all("td")
                logger.info(f"Checking {len(all_tds)} table cells for seat information")
                for td in all_tds:
                    title_attr = await td.get_attribute("title")
                    onclick_attr = await td.get_attribute("onclick")
                    if title_attr and ("Цена" in title_attr or "цена" in title_attr):
                        available.append(title_attr)
                        logger.debug(f"Found seat in td: {title_attr}")
                    elif onclick_attr and ("Цена" in onclick_attr or "цена" in onclick_attr):
                        available.append(onclick_attr)
                        logger.debug(f"Found seat onclick in td: {onclick_attr}")

            # Method 5b: Robust DOM scan for data-* attributes and availability classes
            if not available:
                try:
                    extracted = await target_context.evaluate("""
                        () => {
                            const results = [];
                            const cells = Array.from(document.querySelectorAll("table#myHall td, td.place, td[onclick], td[title], .place"));
                            for (const td of cells) {
                                try {
                                    const title = td.getAttribute('title') || '';
                                    const onclick = td.getAttribute('onclick') || '';
                                    const cls = td.className || '';
                                    const ds = td.dataset || {};
                                    const text = (td.textContent || '').trim();
                                    const price = ds.price || ds.cost || ds.sum || '';
                                    const row = ds.row || ds.r || '';
                                    const seat = ds.seat || ds.s || '';
                                    const looksFree = /\bfree\b/i.test(cls) || /\bsvobod/i.test(cls) || /\bfree\b/i.test(text);
                                    const hasPriceHint = /Цена|цена/.test(title) || /Цена|цена/.test(onclick) || !!price;
                                    if (looksFree || hasPriceHint) {
                                        let info = title || onclick;
                                        if (!info) {
                                            const parts = [];
                                            if (row) parts.push(`Ряд: ${row}`);
                                            if (seat) parts.push(`Место: ${seat}`);
                                            if (price) parts.push(`Цена: ${price}`);
                                            if (!parts.length && text && /^\d+$/.test(text)) parts.push(`Место: ${text}`);
                                            info = parts.join(', ');
                                        }
                                        if (info) results.push(info);
                                    }
                                } catch (_) {}
                            }
                            return Array.from(new Set(results));
                        }
                    """)
                    if isinstance(extracted, list) and extracted:
                        for it in extracted:
                            if isinstance(it, str):
                                available.append(it)
                except Exception:
                    pass
            
            # Method 6: Check page content for seat information
            if not available:
                try:
                    page_content = await page.content()
                    if "Цена" in page_content or "цена" in page_content:
                        logger.info("Found price information in page content, but couldn't extract specific seats")
                        # Try to extract seat information from the page content
                        import re
                        price_matches = re.findall(r'Цена:\s*(\d+)\s*руб', page_content)
                        if price_matches:
                            for price in price_matches:
                                available.append(f"Цена: {price} руб")
                                logger.debug(f"Extracted price from content: {price} руб")
                except Exception as e:
                    logger.debug(f"Content analysis failed: {e}")
            
            # Method 7: Check if the API call is being blocked (bot protection)
            if not available:
                try:
                    # Try to make the API call that loads ticket data
                    api_url = f"https://tce.by/index.php?view=shows&action=ticket&kind=json&base={url.split('base=')[1].split('&')[0]}&data={url.split('data=')[1]}&_cb={int(__import__('time').time())}"
                    logger.info(f"Attempting to load ticket data from API: {api_url}")
                    
                    api_result = await page.evaluate(f"""
                        async () => {{
                            try {{
                                const res = await fetch('{api_url}', {{
                                    method: 'GET',
                                    credentials: 'include',
                                    headers: {{
                                        'Accept': 'application/json, text/plain, */*',
                                        'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
                                        'X-Requested-With': 'XMLHttpRequest',
                                        'Referer': '{url}'
                                    }}
                                }});
                                const text = await res.text();
                                try {{ return JSON.parse(text); }} catch(_) {{ return {{ raw: text, status: res.status }}; }}
                            }} catch (e) {{
                                return {{ error: String(e) }};
                            }}
                        }}
                    """)
                    
                    if isinstance(api_result, dict):
                        if api_result.get('success') == False:
                            logger.warning(f"API call blocked by bot protection: {api_result.get('data', 'Unknown error')}")
                            # This is likely why no seats are found - the API is being blocked
                        elif api_result.get('success') == True and 'data' in api_result:
                            logger.info("API call successful, trying to load tickets...")
                            # Try to call loadTickets with the API data
                            try:
                                load_result = await page.evaluate(f"loadTickets({json.dumps(api_result)})")
                                if load_result:
                                    logger.info("Successfully loaded tickets from API")
                                    # Re-check seats after loading from API
                                    seats_after_api = await target_context.query_selector_all("td.place")
                                    for seat in seats_after_api:
                                        title_attr = await seat.get_attribute("title")
                                        onclick_attr = await seat.get_attribute("onclick")
                                        inner_text = await seat.inner_text()
                                        
                                        if title_attr and "Цена" in title_attr:
                                            available.append(title_attr)
                                        elif onclick_attr and "Цена" in onclick_attr:
                                            available.append(onclick_attr)
                                        elif inner_text and inner_text != "0" and inner_text.isdigit():
                                            available.append(f"Seat available (text: {inner_text})")
                            except Exception as e:
                                logger.debug(f"Error calling loadTickets with API data: {e}")
                        else:
                            logger.debug(f"Unexpected API response: {api_result}")
                    else:
                        logger.debug(f"API call failed: {api_result}")
                        
                except Exception as e:
                    logger.debug(f"Error checking API: {e}")

            # Method 8: Parse embedded loadTickets JSON when API is blocked
            if not available:
                try:
                    embedded = await target_context.evaluate("""
                        () => {
                            // Look for a script tag calling loadTickets({...}) and extract JSON
                            const scripts = Array.from(document.querySelectorAll('script'));
                            const re = /loadTickets\((\{[\s\S]*?\})\)/;
                            for (const s of scripts) {
                                const txt = s.textContent || '';
                                const m = re.exec(txt);
                                if (m) {
                                    try { return JSON.parse(m[1]); } catch (_) {}
                                }
                            }
                            return null;
                        }
                    """)
                    if isinstance(embedded, dict):
                        try:
                            # Attempt to invoke loadTickets in page context with embedded data
                            await target_context.evaluate("data => { try { loadTickets(data); } catch(e) {} }", embedded)
                            # After load, re-scan td.place for seats
                            seats_after = await target_context.query_selector_all("td.place")
                            for seat in seats_after:
                                title_attr = await seat.get_attribute("title")
                                onclick_attr = await seat.get_attribute("onclick")
                                inner_text = await seat.inner_text()
                                if title_attr and "Цена" in title_attr:
                                    available.append(title_attr)
                                elif onclick_attr and "Цена" in onclick_attr:
                                    available.append(onclick_attr)
                                elif inner_text and inner_text != "0" and inner_text.isdigit():
                                    available.append(f"Seat available (text: {inner_text})")
                        except Exception as e2:
                            logger.debug(f"Embedded loadTickets parse/apply failed: {e2}")
                except Exception as e:
                    logger.debug(f"Embedded loadTickets search failed: {e}")

            # Single concise log line per show
            if len(available) == 0:
                logger.warning(f"Found 0 seats for {title} at {url} - likely blocked by bot protection")
            else:
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
                    '--disable-gpu',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-features=VizDisplayCompositor',
                    '--disable-web-security',
                    '--disable-features=TranslateUI',
                    '--disable-ipc-flooding-protection',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--disable-default-apps',
                    '--disable-popup-blocking',
                    '--disable-extensions',
                    '--disable-plugins',
                    '--disable-images',
                    '--disable-javascript-harmony-shipping',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--disable-field-trial-config',
                    '--disable-back-forward-cache',
                    '--disable-hang-monitor',
                    '--disable-prompt-on-repost',
                    '--disable-sync',
                    '--metrics-recording-only',
                    '--no-report-upload',
                    '--safebrowsing-disable-auto-update',
                    '--enable-automation',
                    '--password-store=basic',
                    '--use-mock-keychain'
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
                    'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Upgrade-Insecure-Requests': '1'
                },
                # Add stealth settings to avoid detection
                java_script_enabled=True,
                bypass_csp=True,
                # Simulate a real browser more closely
                device_scale_factor=1,
                is_mobile=False,
                has_touch=False
            )
            
            logger.info("Creating new page")
            page = await context.new_page()
            page.set_default_timeout(30000)
            
            # Add stealth measures to avoid detection
            await page.add_init_script("""
                // Remove webdriver property
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                
                // Mock plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                
                // Mock languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['ru-RU', 'ru', 'en-US', 'en'],
                });
                
                // Mock permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
                
                // Override the `plugins` property to use a custom getter
                Object.defineProperty(navigator, 'plugins', {
                    get: function() {
                        return [1, 2, 3, 4, 5];
                    },
                });
                
                // Override the `languages` property to use a custom getter
                Object.defineProperty(navigator, 'languages', {
                    get: function() {
                        return ['ru-RU', 'ru', 'en-US', 'en'];
                    },
                });
                
                // Mock chrome runtime
                window.chrome = {
                    runtime: {},
                };
            """)

            # Optional: override URLs for focused testing via env TCE_TEST_URLS (comma-separated)
            test_urls_env = os.getenv("TCE_TEST_URLS", "").strip()
            if test_urls_env:
                discovered_ticket_urls = set(u.strip() for u in test_urls_env.split(",") if u.strip())
                discovered_show_urls = set()
                logger.info(f"TCE_TEST_URLS active: {len(discovered_ticket_urls)} urls will be tested")

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
                # Strip any URL fragment for show pages
                normalized_link_no_fragment = normalized_link.split('#')[0] if isinstance(normalized_link, str) else normalized_link
                if _is_partner_url(normalized_link):
                    discovered_ticket_urls.add(normalized_link)
                    seeded_direct_ticket_urls.add(normalized_link)
                else:
                    discovered_show_urls.add(normalized_link_no_fragment)

            # Clarify seeding breakdown: many remote links can be direct ticket pages
            logger.info(
                f"Seeded {len(discovered_show_urls)} show pages and {len(discovered_ticket_urls)} direct ticket pages from shows source"
            )

            # For parity with show-page logs, list each seeded direct ticket as a single-link result
            for direct_url in sorted(seeded_direct_ticket_urls):
                logger.info(f"Show {direct_url} -> found 1 ticket link (direct)")

            # Load cache and seed from it for speed (optional)
            cache = {"ticket_urls": [], "show_to_tickets": {}}
            cached_ticket_urls = set()
            cached_map = {}
            if USE_TICKETS_CACHE:
                cache = load_tickets_cache()
                cached_ticket_urls = set(cache.get("ticket_urls") or [])
                cached_map = cache.get("show_to_tickets") or {}

            # Reuse cached mapping for known show pages (optional)
            if USE_TICKETS_CACHE:
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
                    visit_url = show_url.split('#')[0] if isinstance(show_url, str) else show_url
                    await page.goto(visit_url, wait_until='domcontentloaded')
                    # Let dynamic content render
                    await page.wait_for_timeout(1000)
                    try:
                        await page.wait_for_load_state('networkidle', timeout=3000)
                    except Exception:
                        pass
                    # Try to jump to tickets section and expand in-page controls
                    try:
                        await page.evaluate("window.location.hash = 'tickets'")
                        await page.wait_for_timeout(300)
                    except Exception:
                        pass
                    try:
                        buy_us_btns = await page.query_selector_all("text=Купить у нас")
                        if buy_us_btns:
                            for btn in buy_us_btns:
                                try:
                                    await btn.click()
                                    await page.wait_for_timeout(400)
                                except Exception:
                                    continue
                            # After expanding, directly query for the tce shows links under the section
                            tce_direct_after_click = await page.eval_on_selector_all(
                                "a[href*='tce.by/shows.html']",
                                "els => Array.from(new Set(els.map(e => e.href)))"
                            )
                            direct_links_norm = []
                            for u in _only_string_urls(tce_direct_after_click):
                                u_nf = _strip_fragment(u)
                                if _is_tce_show_link(u_nf):
                                    discovered_ticket_urls.add(u_nf)
                                    found_links_for_log.add(u_nf)
                                    direct_links_norm.append(u_nf)
                            if direct_links_norm:
                                cached_map.setdefault(visit_url, [])
                                for t in direct_links_norm:
                                    if t not in cached_map[visit_url]:
                                        cached_map[visit_url].append(t)
                            await page.wait_for_timeout(300)
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
                    extracted = [ _strip_fragment(u) for u in _only_string_urls(extracted_raw) if _is_tce_show_link(u) ]
                    # Poll and scroll lightly to ensure lazy content anchors appear
                    try:
                        for _ in range(3):
                            await page.evaluate("window.scrollBy(0, document.body.scrollHeight/2)")
                            await page.wait_for_timeout(300)
                            more_links = await page.eval_on_selector_all(
                                "a[href*='tce.by/shows.html']",
                                "els => Array.from(new Set(els.map(e => e.href)))"
                            )
                            for u in _only_string_urls(more_links):
                                u_nf = _strip_fragment(u)
                                if _is_tce_show_link(u_nf) and u_nf not in extracted:
                                    extracted.append(u_nf)
                    except Exception:
                        pass
                    found_links_for_log = set(extracted)
                    for t_url in extracted:
                        if isinstance(t_url, str):
                            discovered_ticket_urls.add(t_url)
                    # Additionally, collect partner links (e.g., ticketpro.by) from anchors/iframes/data-attrs
                    try:
                        partner_anchor_links = await page.eval_on_selector_all(
                            "a[href]",
                            "(els) => Array.from(new Set(els.map(e => e.href)))"
                        )
                    except Exception:
                        partner_anchor_links = []
                    try:
                        partner_iframe_links = await page.eval_on_selector_all(
                            "iframe[src]",
                            "(els) => Array.from(new Set(els.map(e => e.src)))"
                        )
                    except Exception:
                        partner_iframe_links = []
                    try:
                        partner_data_attr_links = await page.evaluate("() => {\n                            const urls = new Set();\n                            const add = u => { try { if (u) urls.add(u); } catch(_){} };\n                            document.querySelectorAll('[data-href],[data-url],[data-link]').forEach(el => {\n                              add(el.getAttribute('data-href'));\n                              add(el.getAttribute('data-url'));\n                              add(el.getAttribute('data-link'));\n                            });\n                            return Array.from(urls);\n                        }")
                    except Exception:
                        partner_data_attr_links = []
                    partner_candidates = []
                    for u in [*(partner_anchor_links or []), *(partner_iframe_links or []), *(partner_data_attr_links or [])]:
                        try:
                            if isinstance(u, str) and _is_partner_url(u):
                                partner_candidates.append(_strip_fragment(u))
                        except Exception:
                            pass
                    if partner_candidates:
                        for t_url in partner_candidates:
                            if _is_tce_show_link(t_url):
                                discovered_ticket_urls.add(t_url)
                                found_links_for_log.add(t_url)
                    # Update cache mapping for this show
                    if extracted:
                        cached_map.setdefault(visit_url, [])
                        for t in extracted:
                            if isinstance(t, str) and t not in cached_map[visit_url]:
                                cached_map[visit_url].append(t)
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
                            # If the href already points to a tce shows link, record without navigation
                            if isinstance(href, str) and _is_tce_show_link(href):
                                href_nf = _strip_fragment(href)
                                discovered_ticket_urls.add(href_nf)
                                cached_map.setdefault(visit_url, [])
                                if href_nf not in cached_map[visit_url]:
                                    cached_map[visit_url].append(href_nf)
                                found_links_for_log.add(href_nf)
                                continue
                            # Follow this local buy link
                            try:
                                await page.goto(href, wait_until='domcontentloaded')
                                await page.wait_for_timeout(800)
                                current_url = page.url
                                if isinstance(current_url, str) and _is_tce_show_link(current_url):
                                    discovered_ticket_urls.add(_strip_fragment(current_url))
                                inner_ticket_links = await page.eval_on_selector_all(
                                    "a[href]",
                                    "(els) => Array.from(new Set(els.map(e => e.href)))"
                                )
                                inner_shows_links = await page.eval_on_selector_all(
                                    "a[href]",
                                    "(els) => Array.from(new Set(els.map(e => e.href)))"
                                )
                                inner_iframe_links = await page.eval_on_selector_all(
                                    "iframe[src]",
                                    "(els) => Array.from(new Set(els.map(e => e.src)))"
                                )
                                # Filter inner links by partner domains
                                inner_all = [*(inner_ticket_links or []), *(inner_shows_links or []), *(inner_iframe_links or [])]
                                extracted_inner = [ _strip_fragment(u) for u in inner_all if isinstance(u, str) and _is_tce_show_link(u) ]
                                for t_url in extracted_inner:
                                    discovered_ticket_urls.add(t_url)
                                if extracted_inner:
                                    cached_map.setdefault(visit_url, [])
                                    for t in extracted_inner:
                                        if t not in cached_map[visit_url]:
                                            cached_map[visit_url].append(t)
                                for t in extracted_inner:
                                    found_links_for_log.add(t)
                            except Exception as e:
                                logger.debug(f"Skip buy link {href}: {e}")
                                continue
                    # Summary log for this show after all attempts
                    unique_count = len({t for t in found_links_for_log if isinstance(t, str)})
                    if unique_count:
                        logger.info(f"Show {visit_url} -> found {unique_count} ticket links")
                        success_with_links[visit_url] = unique_count
                    else:
                        logger.warning(f"Show {visit_url} -> no ticket links found")
                        success_no_links.add(visit_url)
                except Exception as e:
                    failures[visit_url if 'visit_url' in locals() else show_url] = str(e)
                    logger.warning(f"Skip show {visit_url if 'visit_url' in locals() else show_url}: {e}")
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
            # Normalize by stripping fragments to avoid duplicates like trailing '#'
            all_ticket_urls = sorted(set([_strip_fragment(u) for u in list(discovered_ticket_urls)] + ([
                _strip_fragment(u) for u in list(cached_ticket_urls)
            ] if USE_TICKETS_CACHE else [])))
            logger.info(f"Discovered {len(discovered_ticket_urls)} ticket pages from {len(discovered_show_urls)} show pages (cache total {len(all_ticket_urls)})")
            if USE_TICKETS_CACHE:
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
