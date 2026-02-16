import os
import time
import json
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
CHECK_INTERVAL = 300 
DB_FILE = "/app/data/tracker_db.json"

STORES = {
    "inet_fynd": {
        "url": "https://www.inet.se/fyndhornan?search=5090",
        "card_selector": "article", 
        "title_selector": "h3",
        "price_selector": "span.price"
    },
    "elgiganten": {
        "url": "https://www.elgiganten.se/search?q=5090",
        "card_selector": "article.product-tile, [data-testid='product-tile']", 
        "title_selector": "h2, .product-name",
        "price_selector": ".price-value, [class*='price']"
    },
    "netonnet": {
        "url": "https://www.netonnet.se/search?query=5090",
        "card_selector": ".productItem, .product-card-container",
        "title_selector": ".title, h2",
        "price_selector": ".price, .product-price"
    }
}

def bypass_cookies_and_scroll(page):
    """Deep-scan for any button containing 'Accept' or 'Godkänn' and click it, then scroll."""
    # This JS snippet reaches into Shadow DOMs where standard Python often fails
    page.evaluate("""
        () => {
            const findAndClick = (root) => {
                const buttons = root.querySelectorAll('button, a, span');
                for (const b of buttons) {
                    const text = b.innerText.toLowerCase();
                    if (text.includes('acceptera') || text.includes('godkänn') || text.includes('agree') || text.includes('förstår')) {
                        b.click();
                        return true;
                    }
                }
                return false;
            };
            findAndClick(document);
            // Check shadow roots
            document.querySelectorAll('*').forEach(el => {
                if (el.shadowRoot) findAndClick(el.shadowRoot);
            });
            window.scrollTo(0, 500); // Scroll down slightly to trigger lazy loading
        }
    """)
    page.wait_for_timeout(2000)

def run_tracker():
    os.makedirs("/app/data/debug", exist_ok=True)
    history = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f: history = json.load(f)
        except: history = {}

    with sync_playwright() as p:
        # Using Chromium but with a standard 'Window' size
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for store, info in STORES.items():
            try:
                print(f"--- Checking {store} ---")
                page.goto(info['url'], wait_until="networkidle", timeout=60000)
                
                bypass_cookies_and_scroll(page)
                
                # Check for products
                cards = page.query_selector_all(info['card_selector'])
                print(f"  [Log] Found {len(cards)} items on page.")

                if len(cards) == 0:
                    page.screenshot(path=f"/app/data/debug/{store}_empty.png")
                    print(f"  [!] Zero items. Saved debug screenshot.")
                
                for card in cards:
                    try:
                        title_el = card.query_selector(info['title_selector'])
                        price_el = card.query_selector(info['price_selector'])
                        
                        if title_el and price_el:
                            title = title_el.inner_text().strip()
                            price = price_el.inner_text().strip()
                            
                            # Log every item to verify the bot is 'reading'
                            print(f"    - Read: {title[:25]}... | {price}")
                            
                            if "5090" in title:
                                item_id = f"{store}-{title}-{price}"
                                if item_id not in history:
                                    print(f"    !!! 5090 FOUND: {title}")
                                    requests.post(DISCORD_WEBHOOK_URL, json={
                                        "embeds": [{"title": f"🚀 5090 Found!", "description": f"{title}\n{price}", "url": info['url'], "color": 15158332}]
                                    })
                                    history[item_id] = time.time()
                    except: continue # Skip individual card if it fails
                                
            except Exception as e:
                print(f"  [Error] {store}: {str(e)[:50]}")

        browser.close()

    with open(DB_FILE, 'w') as f:
        json.dump(history, f)

if __name__ == "__main__":
    print("GPU Tracker service online. Initializing...")
    startup_test() 
    
    while True:
        run_tracker()
        print(f"Done. Sleeping {CHECK_INTERVAL}s.\n")
        time.sleep(CHECK_INTERVAL)
