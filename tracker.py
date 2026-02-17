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
    "inet": {
        "url": "https://www.inet.se/hitta?q=5090&filter=%7B%22query%22%3A%22RTX%205090%22%2C%22templateId%22%3A17%7D&sortColumn=search&sortDirection=desc",
        "card_selector": ".product, .product-list__item",
        "title_selector": ".product__title, .product-name",
        "price_selector": None  # TBD
    },
    "elgiganten": {
        "url": "https://www.elgiganten.se/gaming/datorkomponenter/grafikkort-gpu?gad_campaignid=1506298985&f=30877%3AGeForce%2520RTX%25205090",
        "card_selector": "div.product-tile, article.product-tile",
        "title_selector": "h3, .product-name",
        "price_selector": "#ActivePrice\\.Amount-max"
    },
    "netonnet": {
        "url": "https://www.netonnet.se/search?query=RTX%205090%20grafikkort",
        "card_selector": ".productItem, .product-card-container",
        "title_selector": ".title, h2",
        "price_selector": None
    }
}

def accept_cookies_if_present(page):
    try:
        btn = page.wait_for_selector(
            "button:has-text('Acceptera'), button:has-text('Godkänn'), button:has-text('Accept')",
            timeout=5000
        )
        btn.click()
        page.wait_for_timeout(1000)
    except:
        pass

def run_tracker():
    os.makedirs("/app/data/debug", exist_ok=True)
    history = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                history = json.load(f)
        except:
            history = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for store, info in STORES.items():
            print(f"--- Checking {store} ---")
            try:
                page.goto(info["url"], wait_until="networkidle", timeout=60000)

                accept_cookies_if_present(page)

                # Save HTML for debugging selector
                html = page.content()
                with open(f"/app/data/debug/{store}_dump.html", "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  Saved HTML dump for {store}.")

                cards = page.query_selector_all(info["card_selector"])
                print(f"  Found {len(cards)} cards.")

                if len(cards) == 0:
                    page.screenshot(path=f"/app/data/debug/{store}_empty.png")
                    print(f"  No cards found, saved screenshot.")

                for card in cards:
                    title_el = card.query_selector(info["title_selector"])
                    title = title_el.inner_text().strip() if title_el else "NO TITLE"

                    price_text = None
                    if info["price_selector"]:
                        try:
                            price_el = card.query_selector(info["price_selector"])
                            price_text = price_el.inner_text().strip() if price_el else None
                        except:
                            price_text = None

                    print(f"    - {title[:50]} | {price_text}")

                    if "5090" in title and price_text:
                        key = f"{store}-{title}-{price_text}"
                        if key not in history:
                            requests.post(DISCORD_WEBHOOK_URL, json={
                                "embeds": [{
                                    "title": f"🚀 5090 prisuppdatering!",
                                    "description": f"{title}\n{price_text}",
                                    "url": info["url"],
                                    "color": 15158332
                                }]
                            })
                            history[key] = time.time()

            except Exception as e:
                print(f"  [Error] {store}: {str(e)[:150]}")

        browser.close()

    with open(DB_FILE, "w") as f:
        json.dump(history, f)

if __name__ == "__main__":
    run_tracker()
