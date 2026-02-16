import os
import time
import json
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
# Pulls from Unraid 'Variable' named DISCORD_WEBHOOK
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
CHECK_INTERVAL = 300 
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

def send_to_discord(payload):
    """Internal helper to send any payload to Discord"""
    if not DISCORD_WEBHOOK_URL:
        print("CRITICAL: No DISCORD_WEBHOOK found in environment variables!")
        return False
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Discord Error: {e}")
        return False

def startup_test():
    """Sends a one-time message when the script starts to verify connectivity"""
    print("Sending startup test to Discord...")
    payload = {
        "embeds": [{
            "title": "✅ GPU Tracker Online",
            "description": "The service has started successfully on Unraid and is now monitoring for RTX 5090 listings.",
            "color": 3066993 # Green
        }]
    }
    if send_to_discord(payload):
        print("Startup test sent successfully!")
    else:
        print("Startup test failed. Check your Webhook URL in Unraid settings.")

def notify_match(store_name, title, price, url):
    """Sends an alert when a 5090 is found"""
    payload = {
        "embeds": [{
            "title": f"🚨 5090 ALERT at {store_name}!",
            "description": f"**Product:** {title}\n**Price:** {price}",
            "url": url,
            "color": 15158332 # Red
        }]
    }
    send_to_discord(payload)

def run_tracker():
    history = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                history = json.load(f)
        except: history = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()

        for store, info in STORES.items():
            try:
                print(f"Checking {store}...")
                page.goto(info['url'], wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000) 
                
                cards = page.query_selector_all(info['card_selector'])
                for card in cards:
                    title_el = card.query_selector(info['title_selector'])
                    price_el = card.query_selector(info['price_selector'])
                    
                    if title_el and price_el:
                        title = title_el.inner_text().strip()
                        price = price_el.inner_text().strip()
                        
                        if "5090" in title:
                            item_id = f"{store}-{title}-{price}"
                            if item_id not in history:
                                notify_match(store, title, price, info['url'])
                                history[item_id] = time.time()
            except Exception as e:
                print(f"Error checking {store}: {e}")

        browser.close()

    with open(DB_FILE, 'w') as f:
        json.dump(history, f)

if __name__ == "__main__":
    # 1. Run the test first
    startup_test()
    
    # 2. Enter the loop
    while True:
        run_tracker()
        print(f"Sleeping for {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)
