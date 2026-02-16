import time
import json
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
DISCORD_WEBHOOK_URL = "YOUR_DISCORD_WEBHOOK_HERE"
CHECK_INTERVAL = 300  # 5 minutes
DB_FILE = "tracker_db.json"

# Specific Store Logic
STORES = {
    "inet_fynd": {
        "url": "https://www.inet.se/fyndhornan?search=5090",
        "card_selector": "article.product-card",
        "title_selector": "h3",
        "price_selector": ".price"
    },
    "elgiganten": {
        "url": "https://www.elgiganten.se/search?q=5090",
        "card_selector": "article.product-tile", 
        "title_selector": ".product-name",
        "price_selector": ".price-value"
    },
    "netonnet": {
        "url": "https://www.netonnet.se/search?query=5090",
        "card_selector": ".productItem",
        "title_selector": ".title",
        "price_selector": ".price"
    }
}

def notify_discord(store_name, title, price, url):
    payload = {
        "embeds": [{
            "title": f"🚀 New 5090 Listing found at {store_name}!",
            "description": f"**Product:** {title}\n**Price:** {price}",
            "url": url,
            "color": 5814783
        }]
    }
    requests.post(DISCORD_WEBHOOK_URL, json=payload)

def run_tracker():
    # Load previous state
    try:
        with open(DB_FILE, 'r') as f:
            history = json.load(f)
    except FileNotFoundError:
        history = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # Avoid bot detection with a real User-Agent
        page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

        for store, info in STORES.items():
            try:
                page.goto(info['url'], wait_until="networkidle")
                cards = page.query_selector_all(info['card_selector'])
                
                for card in cards:
                    title = card.query_selector(info['title_selector']).inner_text().strip()
                    if "5090" not in title: continue # Filter for target
                    
                    price = card.query_selector(info['price_selector']).inner_text().strip()
                    product_id = f"{store}-{title}-{price}" # Unique key

                    if product_id not in history:
                        print(f"Match found: {title} for {price}")
                        notify_discord(store, title, price, info['url'])
                        history[product_id] = time.time()
            except Exception as e:
                print(f"Error checking {store}: {e}")

        browser.close()

    # Save state
    with open(DB_FILE, 'w') as f:
        json.dump(history, f)

if __name__ == "__main__":
    while True:
        run_tracker()
        time.sleep(CHECK_INTERVAL)
