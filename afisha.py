import asyncio
import json
import os
from playwright.async_api import async_playwright, TimeoutError
import requests
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

AFISHA_URL = "https://puppet-minsk.by/afisha"
SHOWS_FILE = "shows.json"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_IDS", "").split(",")  # Split comma-separated chat IDs

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable must be set")
if not CHAT_IDS or not any(CHAT_IDS):
    raise ValueError("CHAT_IDS environment variable must be set with at least one chat ID")

def send_telegram_message(message):
    success = True
    for chat_id in CHAT_IDS:
        chat_id = chat_id.strip()  # Remove any whitespace
        if not chat_id:  # Skip empty chat IDs
            continue
            
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        try:
            response = requests.post(url, data=data)
            if not response.ok:
                logger.error(f"Failed to send Telegram message to {chat_id}: {response.text}")
                success = False
            else:
                logger.info(f"Successfully sent message to chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
            success = False
    return success

async def get_shows_with_retry(max_retries=3, timeout=60000):
    for attempt in range(max_retries):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
                )
                page = await context.new_page()
                
                logger.info(f"Attempt {attempt + 1}/{max_retries}: Loading page {AFISHA_URL}")
                await page.goto(AFISHA_URL, timeout=timeout)
                await page.wait_for_load_state('networkidle', timeout=timeout)
                
                # Find all show blocks
                show_blocks = await page.query_selector_all(".afisha_item")
                shows = []
                for block in show_blocks:
                    # Get the title
                    title_elem = await block.query_selector(".afisha_item-title")
                    title = await title_elem.inner_text() if title_elem else "No title"
                    # Get the ticket link
                    link_elem = await block.query_selector("a.afisha_item-hover")
                    link = await link_elem.get_attribute("href") if link_elem else None
                    if link and not link.startswith("http"):
                        link = "https://puppet-minsk.by" + link
                    shows.append({"title": title, "link": link})

                await browser.close()
                logger.info(f"Successfully retrieved {len(shows)} shows")
                return shows
                
        except TimeoutError as e:
            logger.error(f"Timeout error on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(5)  # Wait 5 seconds before retrying
        except Exception as e:
            logger.error(f"Error on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(5)

def load_previous_shows():
    if not os.path.exists(SHOWS_FILE):
        return []
    try:
        with open(SHOWS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading previous shows: {e}")
        return []

def save_shows(shows):
    try:
        with open(SHOWS_FILE, "w", encoding="utf-8") as f:
            json.dump(shows, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving shows: {e}")

def find_new_shows(old, new):
    old_set = set((item["title"], item["link"]) for item in old)
    return [item for item in new if (item["title"], item["link"]) not in old_set]

def main():
    try:
        logger.info("Starting show check")
        previous_shows = load_previous_shows()
        current_shows = asyncio.run(get_shows_with_retry())
        new_shows = find_new_shows(previous_shows, current_shows)
        
        if new_shows:
            msg = f"🎭 New shows added at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}:\n" + "\n".join(f"{show['title']}: {show['link']}" for show in new_shows)
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