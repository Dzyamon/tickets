import asyncio
from playwright.async_api import async_playwright
import requests

BOT_TOKEN = "7362832295:AAF0AklqV_3XNLe8RgMfJGX_p4hvLSyI4lc"
CHAT_ID = "1053125115"
TCE_URL = "https://tce.by/shows.html?base=RkZDMTE2MUQtMTNFNy00NUIyLTg0QzYtMURDMjRBNTc1ODA0&data=3542"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        response = requests.post(url, data=data)
        return response.ok
    except Exception as e:
        print("Failed to send Telegram message:", e)
        return False

async def check_tickets():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Loading ticket page...")
        await page.goto(TCE_URL)
        await page.wait_for_selector("table#myHall td.place")
        await page.wait_for_timeout(5000)  # Wait 5 seconds

        seats = await page.query_selector_all("table#myHall td.place")
        available = []
        for seat in seats:
            title = await seat.get_attribute("title")
            if title and "Цена" in title:
                available.append(seat)

        content = await page.content()
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(content)

        await browser.close()
        return len(available)

def main():
    count = asyncio.run(check_tickets())
    if count > 0:
        msg = f"Tickets available! Count: {count}"
        print(msg)
        send_telegram_message(msg)
    else:
        print("No tickets available.")

if __name__ == "__main__":
    main()
