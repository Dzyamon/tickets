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
                logger.info("Closing browser")
                await context.close()
                await browser.close()
                logger.info(f"Successfully retrieved {len(shows)} shows")
                return shows
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

def save_shows(shows):
    try:
        clean_links = _dedupe_normalize_filter_to_links(shows)
        with open(SHOWS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean_links, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(clean_links)} shows to {SHOWS_FILE}")
    except Exception as e:
        logger.error(f"Error saving shows: {e}")

def find_new_shows(old, new):
    """Return list[str] of normalized links that are new compared to `old`.

    Accepts `old` and `new` as lists of either strings (links) or dicts with a
    `link` key. Always returns a list of normalized absolute link strings.
    Filters out `/afisha` path entries.
    """
    def extract_link(entry):
        if isinstance(entry, dict):
            return entry.get("link")
        if isinstance(entry, str):
            return entry
        return None

    # Normalize and collect old links
    old_links_normalized = set(_dedupe_normalize_filter_to_links(old))

    # Normalize new items and collect only those not present in old
    result_links = []
    seen_in_result = set()
    for item in new:
        link = extract_link(item)
        if not link:
            continue
        normalized = link if link.startswith("http") else urljoin(AFISHA_BASE, link)
        if _is_afisha_path(link) or _is_afisha_path(normalized):
            continue
        if normalized in old_links_normalized:
            continue
        if normalized in seen_in_result:
            continue
        seen_in_result.add(normalized)
        result_links.append(normalized)

    logger.info(f"Found {len(result_links)} new shows out of {len(new)} total shows")
    return result_links

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
            
        new_shows = find_new_shows(previous_shows, current_shows)
        
        if new_shows:
            # Create a concise message with links only
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if len(new_shows) <= 10:
                show_list = "\n".join(f"• {link}" for link in new_shows)
                msg = f"🎭 New shows added at {timestamp}:\n\n{show_list}"
            else:
                first_shows = "\n".join(f"• {link}" for link in new_shows[:5])
                remaining_count = len(new_shows) - 5
                msg = f"🎭 {len(new_shows)} new shows added at {timestamp}:\n\n{first_shows}\n\n... and {remaining_count} more shows"

            logger.info(f"Found {len(new_shows)} new shows")
            send_telegram_message(msg)
            save_shows(current_shows)
        else:
            logger.info("No new shows found")
            
    except Exception as e:
        error_msg = f"Error checking shows: {str(e)}"
        logger.error(error_msg)
        send_telegram_message(error_msg)

if __name__ == "__main__":
    main()