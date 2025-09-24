# main.py

import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

URL = "https://tce.by/shows.html?base=RkZDMTE2MUQtMTNFNy00NUIyLTg0QzYtMURDMjRBNTc1ODA0&data=3811"

# Setup Chrome options for GitHub Actions
options = webdriver.ChromeOptions()
options.add_argument("--headless")  # 👈 MUST-HAVE for headless execution
options.add_argument("--no-sandbox") # 👈 Recommended for Linux environments
options.add_argument("--disable-dev-shm-usage") # 👈 Overcomes limited resource problems
options.add_argument("--window-size=1920,1080") # 👈 Set a window size
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option('useAutomationExtension', False)

def find_available_seats():
    """
    Initializes a headless Chrome browser, navigates to the URL,
    and finds available seats.
    """
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(URL)
        print("Page loaded in headless mode. Waiting for seat map...")

        # --- IMPORTANT: Change this selector based on your inspection ---
        seat_selector = "div.seat.available" # This is a placeholder!

        # Wait up to 20 seconds for at least one seat element to be present
        wait = WebDriverWait(driver, 20)
        available_seats = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, seat_selector)))

        print(f"✅ Found {len(available_seats)} available seats.")
        # You can add more logic here, like writing to a file or sending a notification

    except Exception as e:
        print(f"❌ An error occurred: {e}")
        # Save a screenshot for debugging if something goes wrong
        driver.save_screenshot('error_screenshot.png')
        print("Screenshot 'error_screenshot.png' saved for debugging.")

    finally:
        driver.quit()
        print("Browser closed.")

if __name__ == "__main__":
    find_available_seats()