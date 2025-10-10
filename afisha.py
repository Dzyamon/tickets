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

AFISHA_BASE = "https://puppet-minsk.by"
AFISHA_URL = f"{AFISHA_BASE}/afisha"
# Use different file names for local and GitHub Actions environments
SHOWS_FILE = "local_shows.json" if os.getenv("GITHUB_ACTIONS") != "true" else "shows.json"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_IDS", "").split(",")  # Split comma-separated chat IDs

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

async def get_shows_with_retry(max_retries=3, timeout=30000):
    for attempt in range(max_retries):
        browser = None
        context = None
        try:
            async with async_playwright() as p:
                logger.info(f"Attempt {attempt + 1}/{max_retries}: Launching browser")
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
                page.set_default_timeout(60000)
                logger.info(f"Loading page {AFISHA_URL}")
                try:
                    response = await page.goto(
                        AFISHA_URL,
                        wait_until='domcontentloaded',
                        timeout=60000
                    )
                    if not response:
                        raise Exception("Failed to get response from page")
                    if not response.ok:
                        raise Exception(f"Page returned status {response.status}")
                    logger.info("Waiting for content to load / links to appear")
                    # Detect bot-protection text and wait it out if present
                    try:
                        protection_text = await page.query_selector("text=Making sure you're not a bot!")
                        if protection_text:
                            logger.info("Bot protection detected, waiting up to 60s...")
                            await page.wait_for_selector("a[href*='tce.by'], a:has-text('Купить билет')", timeout=60000)
                    except Exception:
                        pass
                    # Poll for links with light scrolling to trigger lazy content
                    shows = []
                    max_checks = 20
                    for i in range(max_checks):
                        link_elements = await page.query_selector_all("a[href*='tce.by'], a:has-text('Купить билет')")
                        if link_elements:
                            for link_elem in link_elements:
                                try:
                                    href = await link_elem.get_attribute("href")
                                    if href:
                                        shows.append(href)
                                except Exception:
                                    continue
                            if shows:
                                break
                        # Scroll a bit and wait before next check
                        try:
                            await page.evaluate("window.scrollBy(0, document.documentElement.clientHeight);")
                        except Exception:
                            pass
                        await asyncio.sleep(3)
                    if not shows:
                        raise TimeoutError("No ticket links found after polling")
                except Exception as e:
                    # Try to capture a screenshot to aid debugging
                    try:
                        await page.screenshot(path="afisha_error.png", full_page=True)
                        logger.error("Saved failure screenshot to afisha_error.png")
                    except Exception as shot_err:
                        logger.error(f"Failed to take screenshot: {shot_err}")
                    logger.error(f"Error loading page: {str(e)}")
                    raise TimeoutError(f"Timeout or error loading page: {e}")
                logger.info("Collecting show links done")
                
                # Process and enrich shows while browser is still open
                try:
                    unique_links = _dedupe_normalize_filter_to_links(shows)
                    logger.info(f"Successfully retrieved {len(shows)} shows ({len(unique_links)} unique)")
                except Exception:
                    unique_links = []

                # Enrich each show with available dates from its page
                enriched = []
                try:
                    for show_link in unique_links:
                        try:
                            # Open show page
                            await page.goto(show_link, wait_until='domcontentloaded')
                            await page.wait_for_timeout(500)
                            # Extract texts from date-time blocks
                            date_texts = await page.eval_on_selector_all(
                                "div.date-time p",
                                "els => els.map(e => (e.textContent||'').trim()).filter(Boolean)"
                            )
                            parsed_dates = []
                            for dt in date_texts or []:
                                ddmmyyyy = _parse_ru_date_text_to_ddmmyyyy(dt)
                                if ddmmyyyy and ddmmyyyy not in parsed_dates:
                                    parsed_dates.append(ddmmyyyy)
                            enriched.append({"link": show_link, "dates": parsed_dates})
                        except Exception as e:
                            logger.warning(f"Failed to extract dates from {show_link}: {e}")
                            enriched.append({"link": show_link, "dates": []})
                except Exception as e:
                    logger.warning(f"Failed enriching dates: {e}")
                    # Fallback to simple links if date extraction fails
                    enriched = [{"link": link, "dates": []} for link in unique_links]

                logger.info(f"Successfully retrieved {len(enriched)} enriched shows")
                logger.info("Closing browser")
                await context.close()
                await browser.close()
                return enriched
        except Exception as e:
            logger.error(f"Error on attempt {attempt + 1}: {str(e)}")
            if context:
                await context.close()
            if browser:
                await browser.close()
            # Only re-raise on the last attempt
            if attempt == max_retries - 1:
                raise
            logger.info(f"Waiting 5 seconds before retry {attempt + 2}")
            await asyncio.sleep(5)

def load_previous_shows():
    if not os.path.exists(SHOWS_FILE):
        logger.info("No previous shows file found. This might be the first run.")
        return []
    try:
        with open(SHOWS_FILE, "r", encoding="utf-8") as f:
            shows = json.load(f)
            logger.info(f"Loaded {len(shows)} shows from previous run")
            return shows
    except Exception as e:
        logger.error(f"Error loading previous shows: {e}")
        return []

def _is_afisha_path(link: str) -> bool:
    try:
        if not link:
            return False
        if link.strip() == "/afisha":
            return True
        parsed = urlparse(link)
        if parsed.scheme in ("http", "https"):
            return parsed.path.rstrip("/") == "/afisha"
        return False
    except Exception:
        return False

def _dedupe_normalize_filter_to_links(shows):
    seen = set()
    result = []
    for s in shows:
        link = None
        if isinstance(s, dict):
            link = s.get("link")
        elif isinstance(s, str):
            link = s
        if not link:
            continue
        normalized = link if link.startswith("http") else urljoin(AFISHA_BASE, link)
        if _is_afisha_path(link) or _is_afisha_path(normalized):
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result

RU_MONTH_TO_MM = {
    "января": "01", "февраля": "02", "марта": "03", "апреля": "04",
    "мая": "05", "июня": "06", "июля": "07", "августа": "08",
    "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12",
    # Title-case variants
    "Января": "01", "Февраля": "02", "Марта": "03", "Апреля": "04",
    "Мая": "05", "Июня": "06", "Июля": "07", "Августа": "08",
    "Сентября": "09", "Октября": "10", "Ноября": "11", "Декабря": "12",
}

def _parse_ru_date_text_to_ddmmyyyy(text: str) -> str:
    """Parse Russian date text like '11 Октября, Суббота, 11:00' to '11.10.2025' format."""
    try:
        import re
        m = re.search(r"(\d{1,2})\s+([А-Яа-яA-Za-z]+)", text or "")
        if not m:
            return ""
        day = int(m.group(1))
        mon_word = m.group(2)
        mm = RU_MONTH_TO_MM.get(mon_word)
        if not mm:
            return ""
        yyyy = datetime.utcnow().year
        return f"{day:02d}.{mm}.{yyyy}"
    except Exception:
        return ""

def save_shows(shows):
    try:
        # If shows is already enriched (list of dicts with link and dates), use as-is
        if shows and isinstance(shows[0], dict) and "link" in shows[0]:
            enriched_shows = shows
        else:
            # If shows is a list of strings, convert to enriched format
            clean_links = _dedupe_normalize_filter_to_links(shows)
            enriched_shows = [{"link": link, "dates": []} for link in clean_links]
        
        with open(SHOWS_FILE, "w", encoding="utf-8") as f:
            json.dump(enriched_shows, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(enriched_shows)} enriched shows to {SHOWS_FILE}")
    except Exception as e:
        logger.error(f"Error saving shows: {e}")

def find_changed_shows(old, new):
    """Return dict with 'new' and 'changed' shows compared to `old`.

    Returns:
        {
            'new': [list of new show objects],
            'changed': [list of shows with changed dates],
            'unchanged': [list of unchanged shows]
        }
    """
    def extract_link(entry):
        if isinstance(entry, dict):
            return entry.get("link")
        if isinstance(entry, str):
            return entry
        return None

    def normalize_link(link):
        if not link:
            return None
        return link if link.startswith("http") else urljoin(AFISHA_BASE, link)

    def extract_dates(entry):
        if isinstance(entry, dict):
            return set(entry.get("dates", []))
        return set()

    # Build old shows lookup by normalized link
    old_shows_by_link = {}
    for item in old:
        link = extract_link(item)
        if link:
            normalized = normalize_link(link)
            if normalized and not _is_afisha_path(link) and not _is_afisha_path(normalized):
                old_shows_by_link[normalized] = {
                    'link': normalized,
                    'dates': extract_dates(item)
                }

    # Process new items
    new_shows = []
    changed_shows = []
    unchanged_shows = []
    seen_in_result = set()

    for item in new:
        link = extract_link(item)
        if not link:
            continue
        normalized = normalize_link(link)
        if _is_afisha_path(link) or _is_afisha_path(normalized):
            continue
        if normalized in seen_in_result:
            continue
        seen_in_result.add(normalized)

        # Create enriched show object
        if isinstance(item, dict) and "dates" in item:
            new_show = item
        else:
            new_show = {"link": normalized, "dates": []}
        
        new_dates = extract_dates(new_show)

        if normalized in old_shows_by_link:
            old_dates = old_shows_by_link[normalized]['dates']
            if new_dates != old_dates:
                changed_shows.append(new_show)
                logger.info(f"Show dates changed for {normalized}: {sorted(old_dates)} -> {sorted(new_dates)}")
            else:
                unchanged_shows.append(new_show)
        else:
            new_shows.append(new_show)

    logger.info(f"Found {len(new_shows)} new shows, {len(changed_shows)} changed shows, {len(unchanged_shows)} unchanged shows")
    return {
        'new': new_shows,
        'changed': changed_shows,
        'unchanged': unchanged_shows
    }

def main():
    try:
        # Debug: print current working directory and files
        print("Current working directory:", os.getcwd())
        print("Files in cwd:", os.listdir())
        logger.info("Starting show check")
        previous_shows = load_previous_shows()
        
        # Create and set a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            current_shows = loop.run_until_complete(get_shows_with_retry())
        except Exception as e:
            error_msg = f"Error checking shows: {str(e)}"
            # Suppress notification for known loading/browser errors
            if (
                "Timeout or error loading page" in str(e)
                or "Target page, context or browser has been closed" in str(e)
                or "Timeout" in str(e)
            ):
                logger.error(error_msg)
                return  # Prevent further execution
            else:
                logger.error(error_msg)
                send_telegram_message(error_msg)
                return  # Prevent further execution
        finally:
            loop.close()
        
        # If this is the first run (no previous shows), don't send notifications
        if not previous_shows:
            logger.info("First run detected. Saving shows without sending notifications.")
            save_shows(current_shows)
            return
            
        changes = find_changed_shows(previous_shows, current_shows)
        new_shows = changes['new']
        changed_shows = changes['changed']
        
        # Send notifications for new and changed shows
        if new_shows or changed_shows:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            messages = []

            if new_shows:
                if len(new_shows) <= 10:
                    show_list = []
                    for show in new_shows:
                        link = show.get("link", "")
                        dates = show.get("dates", [])
                        if dates:
                            dates_str = ", ".join(dates)
                            show_list.append(f"• {link} ({dates_str})")
                        else:
                            show_list.append(f"• {link}")
                    messages.append(f"🎭 New shows added at {timestamp}:\n\n" + "\n".join(show_list))
                else:
                    first_shows = []
                    for show in new_shows[:5]:
                        link = show.get("link", "")
                        dates = show.get("dates", [])
                        if dates:
                            dates_str = ", ".join(dates)
                            first_shows.append(f"• {link} ({dates_str})")
                        else:
                            first_shows.append(f"• {link}")
                    remaining_count = len(new_shows) - 5
                    messages.append(f"🎭 {len(new_shows)} new shows added at {timestamp}:\n\n" + "\n".join(first_shows) + f"\n\n... and {remaining_count} more shows")

            if changed_shows:
                if len(changed_shows) <= 10:
                    show_list = []
                    for show in changed_shows:
                        link = show.get("link", "")
                        dates = show.get("dates", [])
                        if dates:
                            dates_str = ", ".join(dates)
                            show_list.append(f"• {link} ({dates_str})")
                        else:
                            show_list.append(f"• {link}")
                    messages.append(f"📅 Show dates updated at {timestamp}:\n\n" + "\n".join(show_list))
                else:
                    first_shows = []
                    for show in changed_shows[:5]:
                        link = show.get("link", "")
                        dates = show.get("dates", [])
                        if dates:
                            dates_str = ", ".join(dates)
                            first_shows.append(f"• {link} ({dates_str})")
                        else:
                            first_shows.append(f"• {link}")
                    remaining_count = len(changed_shows) - 5
                    messages.append(f"📅 {len(changed_shows)} show dates updated at {timestamp}:\n\n" + "\n".join(first_shows) + f"\n\n... and {remaining_count} more shows")

            # Send combined message
            combined_msg = "\n\n".join(messages)
            logger.info(f"Found {len(new_shows)} new shows and {len(changed_shows)} changed shows")
            send_telegram_message(combined_msg)
            save_shows(current_shows)
        else:
            logger.info("No new or changed shows found")
            # Still persist if the normalized set changed (e.g., removals or link normalization)
            try:
                prev_norm = set(_dedupe_normalize_filter_to_links(previous_shows))
                curr_norm = set(_dedupe_normalize_filter_to_links(current_shows))
                if prev_norm != curr_norm:
                    logger.info(
                        f"Show list changed (prev={len(prev_norm)}, curr={len(curr_norm)}) without additions; saving updated list"
                    )
                    save_shows(current_shows)
                else:
                    logger.info("Show list unchanged; no save needed")
            except Exception:
                # On any error determining diff, be safe and save
                save_shows(current_shows)
            
    except Exception as e:
        error_msg = f"Error checking shows: {str(e)}"
        logger.error(error_msg)
        send_telegram_message(error_msg)

if __name__ == "__main__":
    main()