import os
import time
import json
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
CHECK_INTERVAL = 300
DB_FILE = "/app/data/tracker_db.json"
STATE_FILE = "/app/data/storage_state.json"

STORES = {
    "inet": {
        "url": "https://www.inet.se/hitta?filter=%7B%22query%22%3A%22RTX+5090%22%2C%22templateId%22%3A17%7D&q=5090",
        "card_selector": "li[class*='product-list__item'], div[class*='product']",
        "title_selector": "h3, a span",
        "price_selector": "[data-test-is-discounted-price], [class*='price']"
    },
    "elgiganten": {
        "url": "https://www.elgiganten.se/gaming/datorkomponenter/grafikkort-gpu?f=30877%3AGeForce%2520RTX%25205090",
        "card_selector": "div.product-tile, article.product-tile",
        "title_selector": ".product-name, h3",
        "price_selector": "span.font-headline"
    },
    "netonnet": {
        "url": "https://www.netonnet.se/search?query=RTX%205090%20grafikkort",
        "card_selector": ".product-card-container, .productItem",
        "title_selector": ".title, h2",
        "price_selector": "[class*='price']"
    }
}

def handle_cookie_popup(page):
    selectors = [
        "button:has-text('OK')",
        "button:has-text('Acceptera')",
        "button:has-text('Godkänn')",
        "button:has-text('Accept')",
        "button:has-text('Jag förstår')"
    ]
    for sel in selectors:
        try:
            btn = page.wait_for_selector(sel, timeout=3000)
            btn.click()
            page.wait_for_timeout(1500)
            return
        except:
            pass
    page.evaluate("""
        () => {
            document.querySelectorAll('[role="dialog"], .modal, .cookie, .overlay').forEach(el => el.remove());
        }
    """)
    page.wait_for_timeout(1000)

def create_or_restore_context(playwright):
    if Path(STATE_FILE).exists():
        return playwright.chromium.launch().new_context(storage_state=STATE_FILE)

    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36")
    )
    page = context.new_page()
    page.goto("https://www.inet.se", wait_until="networkidle")
    handle_cookie_popup(page)
    context.storage_state(path=STATE_FILE)
    page.close()
    return browser.new_context(storage_state=STATE_FILE)

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
        context = create_or_restore_context(p)
        page = context.new_page()

        for store, info in STORES.items():
            print(f"--- Checking {store} ---")
            try:
                page.goto(info["url"], wait_until="networkidle", timeout=60000)

                handle_cookie_popup(page)

                try:
                    page.wait_for_selector(info["card_selector"], timeout=15000)
                except:
                    print(f"  No product list rendered yet for {store}")

                html = page.content()
                with open(f"/app/data/debug/{store}_dump.html", "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  Saved debug HTML for {store}.")

                cards = page.query_selector_all(info["card_selector"])
                print(f"  Found {len(cards)} items on page.")

                if len(cards) == 0:
                    page.screenshot(path=f"/app/data/debug/{store}_empty.png")
                    print(f"  No products found, screenshot saved.")

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

                    print(f"    • {title[:60]} | {price_text}")

                    if "5090" in title and price_text:
                        key = f"{store}-{title}-{price_text}"
                        if key not in history:
                            requests.post(DISCORD_WEBHOOK_URL, json={
                                "embeds": [{
                                    "title": "🚀 RTX 5090 prisuppdatering!",
                                    "description": f"{title}\n{price_text}",
                                    "url": info["url"],
                                    "color": 15158332
                                }]
                            })
                            history[key] = time.time()

            except Exception as e:
                print(f"  [Error] {store}: {str(e)[:200]}")

        context.close()

    with open(DB_FILE, "w") as f:
        json.dump(history, f)

if __name__ == "__main__":
    while True:
        run_tracker()
        print(f"Done. Sleeping {CHECK_INTERVAL}s.\n")
        time.sleep(CHECK_INTERVAL)
