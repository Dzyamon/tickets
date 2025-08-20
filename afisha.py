import asyncio
import json
import os
from playwright.async_api import async_playwright, TimeoutError
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

AFISHA_URL = "https://puppet-minsk.by/bilety/afisha"
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
                    viewport={'width': 1280, 'height': 720},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                    ignore_https_errors=True
                )
                logger.info("Creating new page")
                page = await context.new_page()
                page.set_default_timeout(20000)
                logger.info(f"Loading page {AFISHA_URL}")
                try:
                    response = await page.goto(
                        AFISHA_URL,
                        wait_until='domcontentloaded',
                        timeout=20000
                    )
                    if not response:
                        raise Exception("Failed to get response from page")
                    if not response.ok:
                        raise Exception(f"Page returned status {response.status}")
                    logger.info("Waiting for content to load")
                    await page.wait_for_selector("table", timeout=20000)
                except Exception as e:
                    logger.error(f"Error loading page: {str(e)}")
                    raise TimeoutError(f"Timeout or error loading page: {e}")
                logger.info("Looking for show rows in table")
                # Find the table with show information
                tables = await page.query_selector_all("table")
                shows = []
                
                for table in tables:
                    try:
                        # Look for rows that contain show information
                        rows = await table.query_selector_all("tr")
                        for row in rows:
                            try:
                                # Check if this row contains a show link
                                link_elem = await row.query_selector("a[href*='tce.by']")
                                if link_elem:
                                    title = await link_elem.inner_text()
                                    link = await link_elem.get_attribute("href")
                                    if title and link:
                                        shows.append({"title": title.strip(), "link": link})
                            except Exception as e:
                                logger.error(f"Error processing table row: {str(e)}")
                                continue
                    except Exception as e:
                        logger.error(f"Error processing table: {str(e)}")
                        continue
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

def save_shows(shows):
    try:
        with open(SHOWS_FILE, "w", encoding="utf-8") as f:
            json.dump(shows, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(shows)} shows to {SHOWS_FILE}")
    except Exception as e:
        logger.error(f"Error saving shows: {e}")

def find_new_shows(old, new):
    old_set = set((item["title"], item["link"]) for item in old)
    new_shows = [item for item in new if (item["title"], item["link"]) not in old_set]
    logger.info(f"Found {len(new_shows)} new shows out of {len(new)} total shows")
    return new_shows

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
            # Create a more concise message format
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            if len(new_shows) <= 10:
                # For small numbers of shows, list them all
                show_list = "\n".join(f"• {show['title']}\n  {show['link']}" for show in new_shows)
                msg = f"🎭 New shows added at {timestamp}:\n\n{show_list}"
            else:
                # For many shows, provide a summary and list first few
                first_shows = "\n".join(f"• {show['title']}\n  {show['link']}" for show in new_shows[:5])
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