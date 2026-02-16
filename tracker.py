import os
import time
import json
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
# This pulls the URL from the 'Variable' you will create in the Unraid Template
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
CHECK_INTERVAL = 300  # 5 minutes
DB_FILE = "/app/tracker_db.json"

STORES = {
    "inet_fynd": {
        "url": "https://www.inet.se/fyndhornan?search=5090",
        "card_selector": "article", 
        "title_selector": "h3",
        "price_selector": "span.price"
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
    if not DISCORD_WEBHOOK_URL:
        print("CRITICAL ERROR: DISCORD_WEBHOOK environment variable is not set in Unraid!")
        return

    payload = {
        "embeds": [{
            "title": f"🚀 5090 Found at {store_name}!",
            "description": f"**Item:** {title}\n**Price:** {price}",
            "url": url,
            "color": 5814783,
            "footer": {"text": "Unraid GPU Tracker Service"}
        }]
    }
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")

def run_tracker():
    history = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                history = json.load(f)
        except:
            history = {}

    with sync_playwright() as p:
        # Launching browser with a standard user agent to avoid bot detection
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for store, info in STORES.items():
            try:
                print(f"Checking {store}...")
                page.goto(info['url'], wait_until="domcontentloaded", timeout=60000)
                # Give the page 3 seconds to execute scripts/load prices
                page.wait_for_timeout(3000) 
                
                cards = page.query_selector_all(info['card_selector'])
                for card in cards:
                    title_el = card.query_selector(info['title_selector'])
                    price_el = card.query_selector(info['price_selector'])
                    
                    if title_el and price_el:
                        title = title_el.inner_text().strip()
                        price = price_el.inner_text().strip()
                        
                        # Verify it's actually a 509
