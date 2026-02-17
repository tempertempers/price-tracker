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
        "wait_selector": 'li[data-test-id^="search_product"]',
        "card_selector": 'li[data-test-id^="search_product"]',
        "title_selector": "h3",
        "price_selector": '[class*="pvyf6gm"] span[data-test-is-discounted-price]',
        "load_event": "domcontentloaded",
        "display_name": "inet.se",
        "store_url": "https://www.inet.se",
    },
    "elgiganten": {
        "url": (
            "https://www.elgiganten.se/gaming/datorkomponenter/grafikkort-gpu"
            "?f=30877%3AGeForce%2520RTX%25205090"
        ),
        "wait_selector": "[class*='product-tile'], [class*='ProductTile'], [data-testid*='product']",
        "card_selector": "[class*='product-tile'], [class*='ProductTile'], [data-testid*='product']",
        "title_selector": "h3, h2, [class*='title'], [class*='name']",
        "price_selector": "[class*='price'], [class*='Price']",
        "load_event": "domcontentloaded",
        "display_name": "Elgiganten",
        "store_url": "https://www.elgiganten.se",
    },
}

WAIT_FOR_CONTENT_TIMEOUT = 20_000

# Discord color codes (decimal)
COLOR_GREEN  = 0x2ECC71   # no changes
COLOR_RED    = 0xE74C3C   # new listing or price drop
COLOR_ORANGE = 0xE67E22   # price increase or listing gone
COLOR_GREY   = 0x95A5A6   # error / no data


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


def extract_price(card, price_selector):
    """Return first price element whose text contains a digit. Normalises nbsp."""
    try:
        for el in card.query_selector_all(price_selector):
            text = el.inner_text().strip().replace("\u00a0", "\u202f")
            if any(c.isdigit() for c in text):
                return text
    except Exception:
        pass
    return None


def parse_price_value(price_str):
    """Extract numeric value from a price string like '59 990 kr' -> 59990.0"""
    if not price_str:
        return None
    digits = "".join(c for c in price_str if c.isdigit())
    return float(digits) if digits else None


def truncate(text, length=38):
    return text if len(text) <= length else text[:length - 1] + "…"


# ---------------------------------------------------------------------------
# Discord formatting
# ---------------------------------------------------------------------------

def build_table_embed(store_info, current_listings, prev_listings, changes):
    """
    Build a single rich Discord embed summarising all listings for one store.

    current_listings: list of {"title": str, "price": str|None}
    prev_listings:    dict keyed by title: {"price": str|None}  (from DB)
    changes:          list of change dicts (new / price_drop / price_up / gone)
    """
    display_name = store_info["display_name"]
    store_url    = store_info["url"]
    has_changes  = bool(changes)

    # ── Build the listing table (monospace code block) ──────────────────────
    # Columns:  # | Product (38 chars) | Price
    header = f"{'#':<3} {'Product':<38} {'Price':>12}"
    divider = "─" * len(header)
    rows = [header, divider]

    for i, item in enumerate(current_listings, 1):
        title_col = truncate(item["title"])
        price_col = item["price"] if item["price"] else "—"

        # Mark changed rows with a leading symbol
        marker = "  "
        for c in changes:
            if c["title"] == item["title"]:
                if c["type"] == "new":
                    marker = "🆕"
                elif c["type"] == "price_drop":
                    marker = "📉"
                elif c["type"] == "price_up":
                    marker = "📈"
                break

        rows.append(f"{marker}{i:<2} {title_col:<38} {price_col:>12}")

    # Add any listings that disappeared this run
    gone_titles = [c["title"] for c in changes if c["type"] == "gone"]
    if gone_titles:
        rows.append(divider)
        rows.append("Gone this run:")
        for t in gone_titles:
            rows.append(f"❌  {truncate(t)}")

    table_text = "```\n" + "\n".join(rows) + "\n```"

    # ── Build change summary fields ─────────────────────────────────────────
    fields = []

    if changes:
        change_lines = []
        for c in changes:
            if c["type"] == "new":
                p = c.get("price") or "—"
                change_lines.append(f"🆕 **New:** {c['title']}\n    Price: **{p}**")
            elif c["type"] == "price_drop":
                change_lines.append(
                    f"📉 **Price drop:** {truncate(c['title'], 45)}\n"
                    f"    {c['old_price']} → **{c['new_price']}**"
                )
            elif c["type"] == "price_up":
                change_lines.append(
                    f"📈 **Price increase:** {truncate(c['title'], 42)}\n"
                    f"    {c['old_price']} → {c['new_price']}"
                )
            elif c["type"] == "gone":
                change_lines.append(f"❌ **Gone:** {c['title']}")

        fields.append({
            "name": "⚡ Changes detected",
            "value": "\n".join(change_lines),
            "inline": False,
        })

    # ── Pick colour ─────────────────────────────────────────────────────────
    if not current_listings:
        color = COLOR_GREY
    elif any(c["type"] in ("new", "price_drop") for c in changes):
        color = COLOR_RED
    elif changes:
        color = COLOR_ORANGE
    else:
        color = COLOR_GREEN

    # ── Status line ─────────────────────────────────────────────────────────
    now_ts = int(time.time())
    if has_changes:
        status = f"⚠️  {len(changes)} change(s) detected"
    elif not current_listings:
        status = "⚠️  No listings found"
    else:
        status = f"✅  {len(current_listings)} listing(s) — no changes"

    embed = {
        "title": f"🖥️  RTX 5090 — {display_name}",
        "url": store_url,
        "description": table_text,
        "color": color,
        "fields": fields,
        "footer": {"text": status},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    return embed


def send_summary(webhook_url, embeds, has_urgent_changes):
    """Send up to 10 embeds in one webhook call. Prepend @here if urgent."""
    if not webhook_url:
        print("  [Discord] DISCORD_WEBHOOK not set – skipping.")
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
    """
    Compare current scrape against previous run data.

    prev_store_data: dict  { title: {"price": str|None, "first_seen": float} }
    Returns list of change dicts.
    """
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

    # DB structure: { "store_name": { "title": {"price": str, "first_seen": ts} } }
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
                    print(f"  Timed out waiting for products – saving debug screenshot.")
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

                    price = None
                    if info.get("price_selector"):
                        price = extract_price(card, info["price_selector"])

                    if "5090" in title:
                        current_listings.append({"title": title, "price": price})
                        print(f"    + {title[:60]} | {price or '—'}")

            except PlaywrightTimeoutError as e:
                print(f"  [Timeout] {str(e)[:150]}")
                try:
                    page.screenshot(path=f"/app/data/debug/{store}_timeout.png")
                except Exception:
                    pass
            except Exception as e:
                print(f"  [Error] {str(e)[:150]}")

            # ── Detect changes against last run ──────────────────────────────
            prev_store_data = db.get(store, {})
            changes = detect_changes(current_listings, prev_store_data)

            if changes:
                print(f"  Changes: {[c['type'] for c in changes]}")

            # ── Build embed for this store ────────────────────────────────────
            embed = build_table_embed(info, current_listings, prev_store_data, changes)
            all_embeds.append(embed)

            if any(c["type"] in ("new", "price_drop") for c in changes):
                has_urgent_change = True

            # ── Update DB for this store ──────────────────────────────────────
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

    # ── Send one webhook call with all store embeds ──────────────────────────
    # Discord allows max 10 embeds per message; we have 2 stores so we're fine.
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
