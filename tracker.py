import os
import time
import json
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --- CONFIGURATION ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
CHECK_INTERVAL = 300
DB_FILE = "/app/data/tracker_db.json"
STATE_FILE = "/app/data/storage_state.json"

STORES = {
    "inet": {
        "url": (
            "https://www.inet.se/hitta?q=5090"
            "&filter=%7B%22query%22%3A%22RTX%205090%22%2C%22templateId%22%3A17%7D"
            "&sortColumn=search&sortDirection=desc"
        ),
        # Confirmed from debug HTML: products are <li data-test-id="search_product_XXXXXX">
        # This is a stable semantic selector that won't break on CSS class renames.
        "wait_selector": 'li[data-test-id^="search_product"]',
        "card_selector": 'li[data-test-id^="search_product"]',
        # h3 is the title tag. Do NOT use the hashed class (h1jf9kdi) - it changes on deploys.
        "title_selector": "h3",
        # Confirmed: actual shelf price lives in div.pvyf6gm > span[data-test-is-discounted-price]
        # The first price span in each card is a 799kr game voucher promo - we skip it
        # by targeting only the one inside div.pvyf6gm (the price shelf div).
        "price_selector": '[class*="pvyf6gm"] span[data-test-is-discounted-price]',
        "load_event": "domcontentloaded",
    },
    "elgiganten": {
        "url": (
            "https://www.elgiganten.se/gaming/datorkomponenter/grafikkort-gpu"
            "?f=30877%3AGeForce%2520RTX%25205090"
        ),
        "wait_selector": "[class*='product-tile'], [class*='ProductTile'], [data-testid*='product']",
        "card_selector": "[class*='product-tile'], [class*='ProductTile'], [data-testid*='product']",
        "title_selector": "h3, h2, [class*='title'], [class*='name']",
        # Target only elements that contain digits (real prices), not promo text like "SPEL PA KOPET"
        # Use a broad selector here; the digit-filter in the scrape loop handles the rest.
        "price_selector": "[class*='price'], [class*='Price']",
        "load_event": "domcontentloaded",
    },
}

WAIT_FOR_CONTENT_TIMEOUT = 20_000  # ms


def handle_cookie_popup(page):
    selectors = [
        "button:has-text('OK')",
        "button:has-text('Acceptera')",
        "button:has-text('Godkann alla')",
        "button:has-text('Godkann')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Jag forstar')",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "[id*='accept'][class*='cookie']",
    ]
    for sel in selectors:
        try:
            btn = page.wait_for_selector(sel, timeout=3000, state="visible")
            if btn:
                btn.click()
                page.wait_for_timeout(1500)
                print(f"    Cookie popup dismissed: {sel}")
                return
        except Exception:
            pass
    page.evaluate("""() => {
        ['[role="dialog"]', '.modal', '[class*="cookie"]',
         '[class*="overlay"]', '[class*="consent"]', '#onetrust-banner-sdk']
        .forEach(s => document.querySelectorAll(s).forEach(el => el.remove()));
    }""")
    page.wait_for_timeout(500)


def send_discord_alert(webhook_url, store, title, price_text, product_url):
    if not webhook_url:
        print("  [Discord] DISCORD_WEBHOOK env var not set - skipping alert.")
        return
    price_line = f"\n**{price_text}**" if price_text else ""
    payload = {
        "embeds": [{
            "title": "RTX 5090 Found!",
            "description": f"**{title}**{price_line}\n\nStore: {store}",
            "url": product_url,
            "color": 0xE74C3C,
        }]
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"    [Discord] Alert sent: {title[:60]}")
    except Exception as e:
        print(f"    [Discord] Failed: {e}")


def build_browser_and_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    context_kwargs = {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "locale": "sv-SE",
        "timezone_id": "Europe/Stockholm",
    }
    if Path(STATE_FILE).exists():
        context_kwargs["storage_state"] = STATE_FILE
        print("  [Browser] Restored saved cookie state.")
    context = browser.new_context(**context_kwargs)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return browser, context


def extract_price(card, price_selector):
    """
    Return the first element matching price_selector whose text contains a digit.
    This filters out promo labels like 'SPEL PA KOPET' or '799 kr' game vouchers
    that share the same selector as the real shelf price.
    For inet, the selector already targets only the shelf price div so this is
    just a safety net.
    """
    try:
        price_els = card.query_selector_all(price_selector)
        for el in price_els:
            text = el.inner_text().strip()
            if any(c.isdigit() for c in text):
                # Normalise non-breaking spaces
                return text.replace("\u00a0", " ")
    except Exception:
        pass
    return None


def run_tracker():
    os.makedirs("/app/data/debug", exist_ok=True)

    history: dict = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                history = json.load(f)
        except Exception:
            history = {}

    with sync_playwright() as p:
        browser, context = build_browser_and_context(p)
        page = context.new_page()

        for store, info in STORES.items():
            print(f"\n--- Checking {store} ---")
            try:
                page.goto(info["url"], wait_until=info["load_event"], timeout=60_000)
                handle_cookie_popup(page)

                try:
                    page.wait_for_selector(
                        info["wait_selector"],
                        timeout=WAIT_FOR_CONTENT_TIMEOUT,
                        state="visible",
                    )
                    print(f"  Product list visible for {store}.")
                except PlaywrightTimeoutError:
                    print(f"  Timed out waiting for products on {store} - saving debug artefacts.")
                    page.screenshot(path=f"/app/data/debug/{store}_timeout.png")

                with open(f"/app/data/debug/{store}_dump.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print(f"  Debug HTML saved.")

                cards = page.query_selector_all(info["card_selector"])
                print(f"  Found {len(cards)} product cards.")

                if len(cards) == 0:
                    page.screenshot(path=f"/app/data/debug/{store}_empty.png")
                    print("  Screenshot saved (0 cards).")

                for card in cards:
                    title_el = card.query_selector(info["title_selector"])
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title:
                        title = (card.inner_text() or "")[:80].strip()

                    price_text = None
                    if info.get("price_selector"):
                        price_text = extract_price(card, info["price_selector"])

                    print(f"    - {title[:70]} | {price_text or 'no price'}")

                    if "5090" in title:
                        key = f"{store}|{title}|{price_text or 'no-price'}"
                        if key not in history:
                            print(f"    *** NEW 5090 LISTING ***")
                            send_discord_alert(
                                DISCORD_WEBHOOK_URL, store, title, price_text, info["url"]
                            )
                            history[key] = time.time()
                        else:
                            print(f"    (already notified - skipping)")

            except PlaywrightTimeoutError as e:
                print(f"  [Timeout] {store}: {str(e)[:200]}")
                try:
                    page.screenshot(path=f"/app/data/debug/{store}_timeout.png")
                except Exception:
                    pass
            except Exception as e:
                print(f"  [Error] {store}: {str(e)[:200]}")

        try:
            context.storage_state(path=STATE_FILE)
            print("\n  [Browser] Cookie state saved.")
        except Exception as e:
            print(f"\n  [Browser] Could not save cookie state: {e}")

        context.close()
        browser.close()

    with open(DB_FILE, "w") as f:
        json.dump(history, f, indent=2)


if __name__ == "__main__":
    print("RTX 5090 Tracker starting...")
    while True:
        run_tracker()
        print(f"\nDone. Sleeping {CHECK_INTERVAL}s.\n" + "-" * 50)
        time.sleep(CHECK_INTERVAL)
