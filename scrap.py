# main.py

import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
# 👇 Import the specific exception
from selenium.common.exceptions import TimeoutException

URL = "https://tce.by/shows.html?base=RkZDMTE2MUQtMTNFNy00NUIyLTg0QzYtMURDMjRBNTc1ODA0&data=3811"

options = webdriver.ChromeOptions()
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")
options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option('useAutomationExtension', False)

def find_available_seats():
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(URL)
        print("Page loaded in headless mode. Waiting for seat map...")

        seat_selector = 'td.place[title*="Цена"]'
        
        print(f"Searching for elements with selector: '{seat_selector}'")

        wait = WebDriverWait(driver, 20)
        available_seats = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, seat_selector)))

        print(f"✅ Found {len(available_seats)} available seats with a price.")
        
        # Optional: Print details for the first few seats found
        for i, seat in enumerate(available_seats[:5]):
            seat_title = seat.get_attribute('title')
            print(f"  - Seat {i+1}: {seat_title}")

    # 👇 CATCH THE SPECIFIC TIMEOUT EXCEPTION
    except TimeoutException:
        print(f"❌ Timed out waiting for element with selector: '{seat_selector}'")
        print("This means the element was not found on the page within 20 seconds.")
        print("Saving screenshot for debugging...")
        driver.save_screenshot('error_screenshot.png')

    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")
        driver.save_screenshot('error_screenshot.png')

    finally:
        driver.quit()
        print("Browser closed.")

if __name__ == "__main__":
    find_available_seats()