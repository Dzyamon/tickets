import asyncio
import json
import os
from playwright.async_api import async_playwright, TimeoutError
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urljoin

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

def load_shows_from_afisha():
    try:
        if not os.path.exists(SHOWS_FILE):
            logger.info(f"Shows file not found: {SHOWS_FILE}")
            return []
        with open(SHOWS_FILE, "r", encoding="utf-8") as f:
            shows = json.load(f)
            # Expect list of { title, link }
            valid = [s for s in shows if isinstance(s, dict) and s.get("link")]
            logger.info(f"Loaded {len(valid)} shows from {SHOWS_FILE}")
            return valid
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

            # Prefer ticket URLs from afisha output if available
            discovered_ticket_urls = set()
            shows_from_file = load_shows_from_afisha()
            for s in shows_from_file:
                link = s.get("link")
                if link and "tce.by" in link:
                    discovered_ticket_urls.add(link)

            # If none loaded from file, discover ticket pages by crawling categories (with pagination/scroll), show pages, and buy pages
            discovered_show_urls = set()
            if not discovered_ticket_urls:
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
            for show_url in sorted(discovered_show_urls):
                try:
                    await page.goto(show_url, wait_until='domcontentloaded')
                    await asyncio.sleep(0.5)
                    # Collect direct ticket links
                    ticket_links = await page.eval_on_selector_all(
                        "a[href*='tce.by']",
                        "els => Array.from(new Set(els.map(e => e.href)))"
                    )
                    for t_url in ticket_links:
                        discovered_ticket_urls.add(t_url)
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
                                await asyncio.sleep(0.5)
                                current_url = page.url
                                if 'tce.by' in current_url:
                                    discovered_ticket_urls.add(current_url)
                                inner_ticket_links = await page.eval_on_selector_all(
                                    "a[href*='tce.by']",
                                    "els => Array.from(new Set(els.map(e => e.href)))"
                                )
                                for t_url in inner_ticket_links:
                                    discovered_ticket_urls.add(t_url)
                            except Exception as e:
                                logger.debug(f"Skip buy link {href}: {e}")
                                continue
                except Exception as e:
                    logger.debug(f"Skip show {show_url}: {e}")
                    continue

            current_seats = {}
            for url in sorted(discovered_ticket_urls):
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
