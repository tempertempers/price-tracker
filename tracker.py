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
        "price_selector": None  # fill later
    },
    "elgiganten": {
        "url": "https://www.elgiganten.se/search?q=5090",
        "card_selector": "article.product-tile, [data-testid='product-tile']",
        "title_selector": "h2, .product-name",
        "price_selector": None
    },
    "netonnet": {
        "url": "https://www.netonnet.se/search?query=5090",
        "card_selector": ".productItem, .product-card-container",
        "title_selector": ".title, h2",
        "price_selector": None
    }
}

def bypass_cookies_and_scroll(page):
    page.evaluate("""
        () => {
            document
              .querySelectorAll('button, a, span')
              .forEach(el => {
                const txt = el.innerText.toLowerCase();
                if (["acceptera","godkänn","agree","förstår","accept"].some(v => txt.includes(v))) {
                   el.click();
                }
             });
        }
    """)
    page.wait_for_timeout(2000)

def run_tracker():
    os.makedirs("/app/data/debug", exist_ok=True)
    history = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                history = json.load(f)
        except:
            history = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        for store, info in STORES.items():
            print(f"--- Checking {store} ---")
            try:
                page.goto(info['url'], wait_until="networkidle", timeout=60000)
                bypass_cookies_and_scroll(page)

                # Save HTML dump for manual selector inspection
                html = page.content()
                with open(f"/app/data/debug/{store}_dump.html", "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  Saved raw HTML for {store}.")

                cards = page.query_selector_all(info['card_selector'])
                print(f"  [Log] Found {len(cards)} items.")

                if len(cards) == 0:
                    page.screenshot(path=f"/app/data/debug/{store}_empty.png")
                    print(f"  [!] Zero items found.")

                for card in cards:
                    title_el = card.query_selector(info['title_selector'])
                    title = title_el.inner_text().strip() if title_el else "NO TITLE"

                    # If we haven't filled price_selector yet, log dom around it
                    price_text = None
                    if info['price_selector']:
                        try:
                            page.wait_for_selector(info['price_selector'], timeout=5000)
                            price_el = card.query_selector(info['price_selector'])
                            price_text = price_el.inner_text().strip() if price_el else None
                        except:
                            price_text = None

                    print(f"    - Title: {title[:50]} | Price raw: {price_text}")

            except Exception as e:
                print(f"  [Error] {store}: {str(e)[:150]}")

        browser.close()

    with open(DB_FILE, 'w') as f:
        json.dump(history, f)

if __name__ == "__main__":
    run_tracker()
