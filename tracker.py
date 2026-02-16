import os
import time
import json
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
CHECK_INTERVAL = 300 
DB_FILE = "/app/data/tracker_db.json"
DEBUG_DIR = "/app/data/debug"

STORES = {
    "inet_fynd": {
        "url": "https://www.inet.se/fyndhornan?search=5090",
        "card_selector": "article, .product-item, [class*='productCard']", 
        "title_selector": "h3, .title, [class*='name']",
        "price_selector": "span.price, .price, [class*='price']"
    },
    "elgiganten": {
        "url": "https://www.elgiganten.se/search?q=5090",
        "card_selector": "article.product-tile, .product-container", 
        "title_selector": ".product-name, h2",
        "price_selector": ".price-value, .current-price"
    },
    "netonnet": {
        "url": "https://www.netonnet.se/search?query=5090",
        "card_selector": ".productItem, .product-card",
        "title_selector": ".title, .product-title",
        "price_selector": ".price, .product-price"
    }
}

def send_to_discord(payload):
    if not DISCORD_WEBHOOK_URL: return False
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
        return True
    except: return False

def run_tracker():
    os.makedirs(DEBUG_DIR, exist_ok=True)
    history = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f: history = json.load(f)
        except: history = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Use a real browser user agent to avoid bot detection
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        for store, info in STORES.items():
            try:
                print(f"Checking {store}...")
                page.goto(info['url'], wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000) # Wait 5s for dynamic content
                
                cards = page.query_selector_all(info['card_selector'])
                print(f"  [Log] Scanned {len(cards)} potential items.")

                # If no items found, take a screenshot to debug
                if len(cards) == 0:
                    screenshot_path = f"{DEBUG_DIR}/{store}_error.png"
                    page.screenshot(path=screenshot_path)
                    print(f"  [!] Found 0 items. Saved screenshot to: {screenshot_path}")
                
                for card in cards:
                    title_el = card.query_selector(info['title_selector'])
                    price_el = card.query_selector(info['price_selector'])
                    
                    if title_el and price_el:
                        title = title_el.inner_text().strip()
                        price = price_el.inner_text().strip()
                        
                        if "5090" in title:
                            item_id = f"{store}-{title}-{price}"
                            if item_id not in history:
                                print(f"    -> MATCH FOUND: {title} ({price})")
                                notify_payload = {
                                    "embeds": [{
                                        "title": f"🚨 5090 ALERT: {store}",
                                        "description": f"**{title}**\nPrice: {price}",
                                        "url": info['url'],
                                        "color": 15158332
                                    }]
                                }
                                send_to_discord(notify_payload)
                                history[item_id] = time.time()
            except Exception as e:
                print(f"  [Error] {store}: {str(e)[:100]}")

        browser.close()

    with open(DB_FILE, 'w') as f:
        json.dump(history, f)

if __name__ == "__main__":
    print("GPU Tracker service online. Initializing...")
    while True:
        run_tracker()
        print(f"Sleeping for {CHECK_INTERVAL}s...\n")
        time.sleep(CHECK_INTERVAL)
