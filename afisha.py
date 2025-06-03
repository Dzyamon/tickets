import asyncio
import json
import os
from playwright.async_api import async_playwright
import requests

AFISHA_URL = "https://puppet-minsk.by/afisha"
SHOWS_FILE = "shows.json"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("BOT_TOKEN and CHAT_ID environment variables must be set")

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        response = requests.post(url, data=data)
        return response.ok
    except Exception as e:
        print("Failed to send Telegram message:", e)
        return False

async def get_shows():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(AFISHA_URL)
        await page.wait_for_timeout(3000)  # Wait for content to load

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
        return shows

def load_previous_shows():
    if not os.path.exists(SHOWS_FILE):
        return []
    with open(SHOWS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_shows(shows):
    with open(SHOWS_FILE, "w", encoding="utf-8") as f:
        json.dump(shows, f, ensure_ascii=False, indent=2)

def find_new_shows(old, new):
    old_set = set((item["title"], item["link"]) for item in old)
    return [item for item in new if (item["title"], item["link"]) not in old_set]

def main():
    previous_shows = load_previous_shows()
    current_shows = asyncio.run(get_shows())
    new_shows = find_new_shows(previous_shows, current_shows)
    if new_shows:
        msg = "New shows added:\n" + "\n".join(f"{show['title']}: {show['link']}" for show in new_shows)
        print(msg)
        send_telegram_message(msg)
        save_shows(current_shows)
    else:
        print("No new shows.")

if __name__ == "__main__":
    main()