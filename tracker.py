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

def notify_match(store_name, title, price
