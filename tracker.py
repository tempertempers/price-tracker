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

# After the first run per store (initial snapshot), only send Discord
# messages when something has actually changed.
SILENT_IF_NO_CHANGES = True

STORES = {
    "inet": {
        "url": (
            "https://www.inet.se/hitta?q=5090"
            "&filter=%7B%22query%22%3A%22RTX%205090%22%2C%22templateId%22%3A17%7D"
            "&sortColumn=search&sortDirection=desc"
        ),
        "wait_selector": 'li[data-test-id^="search_product"]',
        "card_selector": 'li[data-test-id^="search_product"]',
        "title_selector": "h3",
        "price_selector": '[class*="pvyf6gm"] span[data-test-is-discounted-price]',
        "price_attr": None,
        "load_event": "domcontentloaded",
        "browser": "chromium",          # which browser engine to use
        "display_name": "inet.se",
        "store_url": "https://www.inet.se",
        "title_filter": "5090",
    },
    "elgiganten": {
        "url": (
            "https://www.elgiganten.se/gaming/datorkomponenter/grafikkort-gpu"
            "?f=30877%3AGeForce%2520RTX%25205090"
        ),
        "wait_selector": 'li[data-cro="product-item"]',
        "card_selector": 'li[data-cro="product-item"]',
        "title_selector": "h2",
        "price_selector": "[data-primary-price]",
        "price_attr": "data-primary-price",
        "load_event": "domcontentloaded",
        "browser": "chromium",
        "display_name": "Elgiganten",
        "store_url": "https://www.elgiganten.se",
        "title_filter": "5090",
    },
    "komplett": {
        # Komplett's search page drops headless Chromium connections via HTTP/2
        # fingerprinting at the CDN level. Firefox has a different TLS/H2
        # fingerprint that gets through cleanly.
        "url": "https://www.komplett.se/search?q=rtx+5090",
        # Each product row carries a data-product-id — stable semantic attribute
        "wait_selector": "[data-product-id]",
        "card_selector": "[data-product-id]",
        # Title is in the first anchor with a product path, or an h2/h3
        "title_selector": "a.product-link, h2, h3, [class*='name']",
        # Price rendered as plain text "38 990:-" — caught by digit heuristic
        "price_selector": "span, div",
        "price_attr": None,
        "load_event": "domcontentloaded",
        "browser": "firefox",           # Firefox bypasses Komplett's H2 fingerprint block
        "display_name": "Komplett.se",
        "store_url": "https://www.komplett.se",
        "title_filter": "5090",
    },
    "webhallen": {
        # Use the clean category URL — the long search URL with multiple &f= params
        # causes a navigation abort in Playwright due to URL encoding edge cases.
        # The category page for GPU (47) filtered to RTX 5090 is cleaner and stable.
        "url": "https://www.webhallen.com/se/search?searchString=rtx+5090",
        # React SPA — wait for product cards to render after JS runs
        "wait_selector": "li[class*='product'], article, [data-testid*='product'], li:has(a[href*='/se/product/'])",
        "card_selector": "li:has(a[href*='/se/product/']), article[class*='product']",
        "title_selector": "h3, h2, [class*='title'], [class*='name']",
        "price_selector": "[class*='price'], [class*='Price'], span",
        "price_attr": None,
        "load_event": "domcontentloaded",
        "browser": "chromium",
        "display_name": "Webhallen",
        "store_url": "https://www.webhallen.com",
        "title_filter": "5090",
    },
}

WAIT_FOR_CONTENT_TIMEOUT = 25_000

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
        "button:has-text('Acceptera alla')",
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
    - If price_attr set: read that attribute directly (e.g. data-primary-price="35990")
      and return formatted as "35 990 kr".
    - Otherwise: scan child elements for text that looks like a price
      (contains digits + "kr" or ":-", or is a bare number >= 1000).
    """
    try:
        if price_attr:
            el = card.query_selector(price_selector)
            if not el:
                return None
            raw = el.get_attribute(price_attr)
            if raw and raw.isdigit():
                val = int(raw)
                formatted = f"{val:,}".replace(",", "\u202f") + " kr"
                return formatted
            return None

        for el in card.query_selector_all(price_selector):
            try:
                text = el.inner_text().strip().replace("\u00a0", " ").replace("\u202f", " ")
            except Exception:
                continue
            if not any(c.isdigit() for c in text):
                continue
            digits_only = "".join(c for c in text if c.isdigit())
            if not digits_only:
                continue
            val = int(digits_only)
            if "kr" in text.lower() or ":-" in text:
                return text.strip()
            if val >= 1000:
                return text.strip()
    except Exception:
        pass
    return None


def parse_price_value(price_str):
    if not price_str:
        return None
    digits = "".join(c for c in price_str if c.isdigit())
    return float(digits) if digits else None


def truncate(text, length=38):
    return text if len(text) <= length else text[:length - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Discord formatting
# ---------------------------------------------------------------------------

def build_table_embed(store_info, current_listings, prev_store_data, changes, is_first_run):
    display_name = store_info["display_name"]
    store_url    = store_info["url"]
    has_changes  = bool(changes)

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
                    marker = "\U0001f195"
                elif c["type"] == "price_drop":
                    marker = "\U0001f4c9"
                elif c["type"] == "price_up":
                    marker = "\U0001f4c8"
                break
        rows.append(f"{marker}{i:<2} {title_col:<38} {price_col:>12}")

    gone_titles = [c["title"] for c in changes if c["type"] == "gone"]
    if gone_titles:
        rows.append(divider)
        rows.append("Gone this run:")
        for t in gone_titles:
            rows.append(f"\u274c  {truncate(t)}")

    table_text = "```\n" + "\n".join(rows) + "\n```"

    fields = []
    if has_changes and not is_first_run:
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
        if change_lines:
            fields.append({
                "name": "\u26a1 Changes detected",
                "value": "\n".join(change_lines),
                "inline": False,
            })

    if not current_listings:
        color = COLOR_GREY
    elif has_changes and not is_first_run and any(c["type"] in ("new", "price_drop") for c in changes):
        color = COLOR_RED
    elif has_changes and not is_first_run:
        color = COLOR_ORANGE
    else:
        color = COLOR_GREEN

    if is_first_run:
        status = f"\U0001f4cb Initial snapshot \u2014 {len(current_listings)} listing(s)"
    elif has_changes:
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


def send_summary(webhook_url, embeds, has_urgent_changes, is_first_run):
    if not webhook_url:
        print("  [Discord] DISCORD_WEBHOOK not set \u2013 skipping.")
        return
    payload = {"embeds": embeds}
    if has_urgent_changes and not is_first_run:
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
# Browser setup — one Chromium and one Firefox instance, created on demand
# ---------------------------------------------------------------------------

def get_or_create_page(browsers, contexts, pages, engine, playwright):
    """
    Return the page for the given engine ("chromium" or "firefox").
    Creates the browser + context + page lazily on first use.
    Firefox is used for Komplett to bypass HTTP/2 fingerprint blocking.
    Chromium is used for all other stores.
    """
    if engine not in pages:
        common_args = ["--no-sandbox", "--disable-setuid-sandbox"]

        if engine == "firefox":
            browser = playwright.firefox.launch(headless=True)
            context_kwargs = {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) "
                    "Gecko/20100101 Firefox/122.0"
                ),
                "locale": "sv-SE",
                "timezone_id": "Europe/Stockholm",
            }
        else:
            browser = playwright.chromium.launch(
                headless=True,
                args=common_args + ["--disable-blink-features=AutomationControlled"],
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
            # Restore saved cookie state for Chromium only
            if Path(STATE_FILE).exists():
                context_kwargs["storage_state"] = STATE_FILE
                print(f"  [Browser:{engine}] Restored saved cookie state.")

        context = browser.new_context(**context_kwargs)

        if engine == "chromium":
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )

        browsers[engine] = browser
        contexts[engine] = context
        pages[engine]    = context.new_page()
        print(f"  [Browser:{engine}] Launched.")

    return pages[engine]


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
    send_this_run     = False

    with sync_playwright() as p:
        browsers  = {}
        contexts  = {}
        pages     = {}

        for store, info in STORES.items():
            print(f"\n--- Checking {store} ---")
            current_listings = []
            first_run = store not in db
            engine = info.get("browser", "chromium")

            try:
                page = get_or_create_page(browsers, contexts, pages, engine, p)

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
                print(f"  Debug HTML saved.")

                cards = page.query_selector_all(info["card_selector"])
                print(f"  Found {len(cards)} cards.")

                if not cards:
                    page.screenshot(path=f"/app/data/debug/{store}_empty.png")

                title_filter = info.get("title_filter", "5090")

                for card in cards:
                    title_el = card.query_selector(info["title_selector"])
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title:
                        title = (card.inner_text() or "")[:80].strip()

                    price = extract_price(card, info["price_selector"], info.get("price_attr"))

                    if title_filter in title:
                        current_listings.append({"title": title, "price": price})
                        print(f"    + {title[:60]} | {price or 'no price'}")

            except PlaywrightTimeoutError as e:
                print(f"  [Timeout] {str(e)[:150]}")
                try:
                    pages.get(engine, None) and pages[engine].screenshot(
                        path=f"/app/data/debug/{store}_timeout.png"
                    )
                except Exception:
                    pass
            except Exception as e:
                print(f"  [Error] {str(e)[:200]}")

            prev_store_data = db.get(store, {})
            changes = detect_changes(current_listings, prev_store_data)
            if changes:
                print(f"  Changes: {[c['type'] for c in changes]}")

            embed = build_table_embed(info, current_listings, prev_store_data, changes, first_run)
            all_embeds.append(embed)

            if first_run:
                send_this_run = True
            elif changes:
                send_this_run = True
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

        # Save Chromium cookie state for next run
        try:
            if "chromium" in contexts:
                contexts["chromium"].storage_state(path=STATE_FILE)
                print("\n  [Browser:chromium] Cookie state saved.")
        except Exception as e:
            print(f"\n  [Browser] Could not save cookie state: {e}")

        # Clean up all browsers
        for engine, browser in browsers.items():
            try:
                contexts[engine].close()
                browser.close()
            except Exception:
                pass

    if all_embeds:
        if not SILENT_IF_NO_CHANGES or send_this_run:
            send_summary(DISCORD_WEBHOOK_URL, all_embeds, has_urgent_change, not send_this_run)
        else:
            print("  [Discord] No changes \u2013 silent run.")

    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


if __name__ == "__main__":
    print("RTX 5090 Tracker starting...")
    while True:
        run_tracker()
        print(f"\nDone. Sleeping {CHECK_INTERVAL}s.\n" + "-" * 50)
        time.sleep(CHECK_INTERVAL)
