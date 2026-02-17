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
        # Stable semantic test-id — won't break on CSS deploys
        "wait_selector": 'li[data-test-id^="search_product"]',
        "card_selector": 'li[data-test-id^="search_product"]',
        "title_selector": "h3",
        # Shelf price is in div.pvyf6gm > span[data-test-is-discounted-price]
        # (first price span per card is a 799kr game voucher — pvyf6gm targets only the real price)
        "price_selector": '[class*="pvyf6gm"] span[data-test-is-discounted-price]',
        "price_attr": None,   # use inner text
        "load_event": "domcontentloaded",
        "display_name": "inet.se",
        "store_url": "https://www.inet.se",
    },
    "elgiganten": {
        "url": (
            "https://www.elgiganten.se/gaming/datorkomponenter/grafikkort-gpu"
            "?f=30877%3AGeForce%2520RTX%25205090"
        ),
        # Stable semantic attribute on each product li
        "wait_selector": 'li[data-cro="product-item"]',
        "card_selector": 'li[data-cro="product-item"]',
        # h2 is the product title inside each card
        "title_selector": "h2",
        # Price lives in data-primary-price attribute on a div — raw integer e.g. "35990"
        # Elgiganten uses Tailwind utility classes so [class*='price'] matches nothing
        "price_selector": "[data-primary-price]",
        "price_attr": "data-primary-price",   # read attribute, not inner text
        "load_event": "domcontentloaded",
        "display_name": "Elgiganten",
        "store_url": "https://www.elgiganten.se",
    },
}

WAIT_FOR_CONTENT_TIMEOUT = 20_000

COLOR_GREEN  = 0x2ECC71
COLOR_RED    = 0xE74C3C
COLOR_ORANGE = 0xE67E22
COLOR_GREY   = 0x95A5A6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def extract_price(card, price_selector, price_attr):
    """
    Extract price from a card element.
    If price_attr is set, read that HTML attribute (e.g. data-primary-price="35990").
    Otherwise scan inner text for the first element containing a digit.
    Returns a formatted string like "35 990 kr" or None.
    """
    try:
        el = card.query_selector(price_selector)
        if not el:
            return None

        if price_attr:
            raw = el.get_attribute(price_attr)
            if raw and raw.isdigit():
                # Format: 35990 -> "35 990 kr"
                val = int(raw)
                formatted = f"{val:,}".replace(",", " ") + " kr"
                return formatted
            return None
        else:
            # Scan all matching elements for one containing digits
            for el in card.query_selector_all(price_selector):
                text = el.inner_text().strip().replace("\u00a0", "\u202f")
                if any(c.isdigit() for c in text):
                    return text
    except Exception:
        pass
    return None


def parse_price_value(price_str):
    """Extract numeric value: '35 990 kr' or '35990.-' -> 35990.0"""
    if not price_str:
        return None
    digits = "".join(c for c in price_str if c.isdigit())
    return float(digits) if digits else None


def truncate(text, length=38):
    return text if len(text) <= length else text[:length - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Discord formatting
# ---------------------------------------------------------------------------

def build_table_embed(store_info, current_listings, prev_store_data, changes):
    display_name = store_info["display_name"]
    store_url    = store_info["url"]
    has_changes  = bool(changes)

    # Build monospace table
    header  = f"{'#':<3} {'Product':<38} {'Price':>12}"
    divider = "\u2500" * len(header)
    rows    = [header, divider]

    for i, item in enumerate(current_listings, 1):
        title_col = truncate(item["title"])
        price_col = item["price"] if item["price"] else "\u2014"

        marker = "  "
        for c in changes:
            if c["title"] == item["title"]:
                if c["type"] == "new":
                    marker = "\U0001f195"   # NEW
                elif c["type"] == "price_drop":
                    marker = "\U0001f4c9"   # chart down
                elif c["type"] == "price_up":
                    marker = "\U0001f4c8"   # chart up
                break

        rows.append(f"{marker}{i:<2} {title_col:<38} {price_col:>12}")

    gone_titles = [c["title"] for c in changes if c["type"] == "gone"]
    if gone_titles:
        rows.append(divider)
        rows.append("Gone this run:")
        for t in gone_titles:
            rows.append(f"\u274c  {truncate(t)}")

    table_text = "```\n" + "\n".join(rows) + "\n```"

    # Change summary field
    fields = []
    if changes:
        change_lines = []
        for c in changes:
            if c["type"] == "new":
                p = c.get("price") or "\u2014"
                change_lines.append(f"\U0001f195 **New:** {c['title']}\n    Price: **{p}**")
            elif c["type"] == "price_drop":
                change_lines.append(
                    f"\U0001f4c9 **Price drop:** {truncate(c['title'], 45)}\n"
                    f"    {c['old_price']} \u2192 **{c['new_price']}**"
                )
            elif c["type"] == "price_up":
                change_lines.append(
                    f"\U0001f4c8 **Price increase:** {truncate(c['title'], 42)}\n"
                    f"    {c['old_price']} \u2192 {c['new_price']}"
                )
            elif c["type"] == "gone":
                change_lines.append(f"\u274c **Gone:** {c['title']}")

        fields.append({
            "name": "\u26a1 Changes detected",
            "value": "\n".join(change_lines),
            "inline": False,
        })

    # Colour
    if not current_listings:
        color = COLOR_GREY
    elif any(c["type"] in ("new", "price_drop") for c in changes):
        color = COLOR_RED
    elif changes:
        color = COLOR_ORANGE
    else:
        color = COLOR_GREEN

    if has_changes:
        status = f"\u26a0\ufe0f  {len(changes)} change(s) detected"
    elif not current_listings:
        status = "\u26a0\ufe0f  No listings found"
    else:
        status = f"\u2705  {len(current_listings)} listing(s) \u2014 no changes"

    return {
        "title": f"\U0001f5a5\ufe0f  RTX 5090 \u2014 {display_name}",
        "url": store_url,
        "description": table_text,
        "color": color,
        "fields": fields,
        "footer": {"text": status},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def send_summary(webhook_url, embeds, has_urgent_changes):
    if not webhook_url:
        print("  [Discord] DISCORD_WEBHOOK not set \u2013 skipping.")
        return
    payload = {"embeds": embeds}
    if has_urgent_changes:
        payload["content"] = "@here  New RTX 5090 listing or price drop detected!"
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"  [Discord] Summary sent ({len(embeds)} embed(s)).")
    except Exception as e:
        print(f"  [Discord] Failed: {e}")


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(current_listings, prev_store_data):
    changes = []
    current_titles = {item["title"] for item in current_listings}
    prev_titles    = set(prev_store_data.keys())

    for item in current_listings:
        title = item["title"]
        price = item["price"]
        if title not in prev_store_data:
            changes.append({"type": "new", "title": title, "price": price})
        else:
            old_price_str = prev_store_data[title].get("price")
            old_val = parse_price_value(old_price_str)
            new_val = parse_price_value(price)
            if old_val is not None and new_val is not None:
                if new_val < old_val:
                    changes.append({
                        "type": "price_drop",
                        "title": title,
                        "old_price": old_price_str,
                        "new_price": price,
                    })
                elif new_val > old_val:
                    changes.append({
                        "type": "price_up",
                        "title": title,
                        "old_price": old_price_str,
                        "new_price": price,
                    })

    for title in prev_titles - current_titles:
        changes.append({"type": "gone", "title": title})

    return changes


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main tracker
# ---------------------------------------------------------------------------

def run_tracker():
    os.makedirs("/app/data/debug", exist_ok=True)

    db: dict = {}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                db = json.load(f)
        except Exception:
            db = {}

    all_embeds        = []
    has_urgent_change = False

    with sync_playwright() as p:
        browser, context = build_browser_and_context(p)
        page = context.new_page()

        for store, info in STORES.items():
            print(f"\n--- Checking {store} ---")
            current_listings = []

            try:
                page.goto(info["url"], wait_until=info["load_event"], timeout=60_000)
                handle_cookie_popup(page)

                try:
                    page.wait_for_selector(
                        info["wait_selector"],
                        timeout=WAIT_FOR_CONTENT_TIMEOUT,
                        state="visible",
                    )
                    print(f"  Product list visible.")
                except PlaywrightTimeoutError:
                    print(f"  Timed out waiting for products \u2013 saving debug screenshot.")
                    page.screenshot(path=f"/app/data/debug/{store}_timeout.png")

                with open(f"/app/data/debug/{store}_dump.html", "w", encoding="utf-8") as f:
                    f.write(page.content())

                cards = page.query_selector_all(info["card_selector"])
                print(f"  Found {len(cards)} cards.")

                if not cards:
                    page.screenshot(path=f"/app/data/debug/{store}_empty.png")

                for card in cards:
                    title_el = card.query_selector(info["title_selector"])
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title:
                        title = (card.inner_text() or "")[:80].strip()

                    price = extract_price(card, info["price_selector"], info.get("price_attr"))

                    if "5090" in title:
                        current_listings.append({"title": title, "price": price})
                        print(f"    + {title[:60]} | {price or 'no price'}")

            except PlaywrightTimeoutError as e:
                print(f"  [Timeout] {str(e)[:150]}")
                try:
                    page.screenshot(path=f"/app/data/debug/{store}_timeout.png")
                except Exception:
                    pass
            except Exception as e:
                print(f"  [Error] {str(e)[:150]}")

            prev_store_data = db.get(store, {})
            changes = detect_changes(current_listings, prev_store_data)
            if changes:
                print(f"  Changes: {[c['type'] for c in changes]}")

            embed = build_table_embed(info, current_listings, prev_store_data, changes)
            all_embeds.append(embed)

            if any(c["type"] in ("new", "price_drop") for c in changes):
                has_urgent_change = True

            new_store_data = {}
            for item in current_listings:
                new_store_data[item["title"]] = {
                    "price": item["price"],
                    "first_seen": prev_store_data.get(item["title"], {}).get(
                        "first_seen", time.time()
                    ),
                }
            db[store] = new_store_data

        try:
            context.storage_state(path=STATE_FILE)
        except Exception:
            pass
        context.close()
        browser.close()

    if all_embeds:
        send_summary(DISCORD_WEBHOOK_URL, all_embeds, has_urgent_change)

    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


if __name__ == "__main__":
    print("RTX 5090 Tracker starting...")
    while True:
        run_tracker()
        print(f"\nDone. Sleeping {CHECK_INTERVAL}s.\n" + "-" * 50)
        time.sleep(CHECK_INTERVAL)
