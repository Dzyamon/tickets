import asyncio
from playwright.async_api import async_playwright, TimeoutError
import requests
import os
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_IDS", "").split(",")  # Split comma-separated chat IDs
TCE_URL = "https://tce.by/shows.html?base=RkZDMTE2MUQtMTNFNy00NUIyLTg0QzYtMURDMjRBNTc1ODA0&data=3542"

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

async def check_tickets_with_retry(max_retries=3, timeout=60000):
    for attempt in range(max_retries):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
                )
                page = await context.new_page()
                
                logger.info(f"Attempt {attempt + 1}/{max_retries}: Loading ticket page {TCE_URL}")
                await page.goto(TCE_URL, timeout=timeout)
                await page.wait_for_load_state('networkidle', timeout=timeout)
                
                logger.info("Waiting for seat elements to load...")
                await page.wait_for_selector("table#myHall td.place", timeout=timeout)
                await page.wait_for_timeout(5000)  # Additional wait for dynamic content

                seats = await page.query_selector_all("table#myHall td.place")
                available = []
                for seat in seats:
                    title = await seat.get_attribute("title")
                    if title and "Цена" in title:
                        available.append(seat)

                # Save debug info
                content = await page.content()
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(content)

                await browser.close()
                logger.info(f"Successfully checked tickets. Found {len(available)} available seats")
                return len(available)
                
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

def main():
    try:
        logger.info("Starting ticket check")
        count = asyncio.run(check_tickets_with_retry())
        
        if count > 0:
            msg = f"🎫 Tickets available at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}!\nCount: {count}"
            logger.info(f"Found {count} available tickets")
            send_telegram_message(msg)
        else:
            logger.info("No tickets available")
            
    except Exception as e:
        error_msg = f"Error checking tickets: {str(e)}"
        logger.error(error_msg)
        send_telegram_message(error_msg)

if __name__ == "__main__":
    main()
