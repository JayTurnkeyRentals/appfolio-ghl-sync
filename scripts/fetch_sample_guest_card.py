"""One-off helper: fetch a single sample Guest Card Inquiries record from the
AppFolio Reports API and print it, so we can confirm the actual field names
returned before finalizing app/mapper.py.

Usage:
    Set these environment variables (or put them in a .env file in the repo root):
        APPFOLIO_CLIENT_ID
        APPFOLIO_CLIENT_SECRET
        APPFOLIO_DATABASE   (e.g. "turnkeyrentals" for turnkeyrentals.appfolio.com)
    Then run:
        python scripts/fetch_sample_guest_card.py
"""
import json
import os
import sys
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

client_id = os.getenv("APPFOLIO_CLIENT_ID")
client_secret = os.getenv("APPFOLIO_CLIENT_SECRET")
database = os.getenv("APPFOLIO_DATABASE", "turnkeyrentals")
report_name = os.getenv("APPFOLIO_GUEST_CARD_REPORT", "guest_card_inquiries")

if not client_id or not client_secret:
    print(
        "Missing APPFOLIO_CLIENT_ID / APPFOLIO_CLIENT_SECRET. "
        "Set them in a .env file (see .env.example) or as environment variables, then re-run.",
        file=sys.stderr,
    )
    sys.exit(1)

url = f"https://{database}.appfolio.com/api/v2/reports/{report_name}.json"

lookback_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
received_on_from = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
received_on_to = date.today().strftime("%Y-%m-%d")

body = {
    "property_visibility": "active",
    "guest_card_sources": ["all"],
    "guest_card_statuses": ["all"],
    "guest_card_lead_types": ["all"],
    "assigned_user": "All",
    "filter_date_range_by": "inquiry",
    "received_on_from": received_on_from,
    "received_on_to": received_on_to,
}

print(f"POST {url}")
print(f"body: {json.dumps(body, indent=2)}")

response = requests.post(url, json=body, auth=(client_id, client_secret), timeout=30)
print(f"status: {response.status_code}")

try:
    payload = response.json()
except ValueError:
    print("Non-JSON response:")
    print(response.text)
    sys.exit(1)

if not response.ok:
    print(json.dumps(payload, indent=2))
    sys.exit(1)

results = payload.get("results", [])
print(f"\nTotal results returned: {len(results)}")

if results:
    print("\nSample guest card record:")
    print(json.dumps(results[0], indent=2, default=str))
else:
    print("No guest cards found in this date range. Try a larger lookback, e.g.:")
    print("    python scripts/fetch_sample_guest_card.py 90")
