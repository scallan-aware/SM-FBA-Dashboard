"""One-off diagnostic: dump the raw SP-API bulk (unfiltered) inventory response,
and pull out every entry for ASIN B00DYN8VQ0 (SKU WG-1137) specifically, to see
whether the bulk listing associates more than one sellerSku with this ASIN."""
import os
import json
from dotenv import load_dotenv
load_dotenv()

from sp_api.base import Marketplaces
from sp_api.api import Inventories

creds = dict(
    refresh_token=os.getenv("SP_REFRESH_TOKEN"),
    lwa_app_id=os.getenv("SP_LWA_APP_ID"),
    lwa_client_secret=os.getenv("SP_LWA_CLIENT_SECRET"),
)

TARGET_ASIN = "B00DYN8VQ0"

inv_api = Inventories(credentials=creds, marketplace=Marketplaces.US)

print("--- Bulk (unfiltered) query, same as sp_api_backend.py uses ---")
all_items = []
next_token = None
page = 1
while True:
    kwargs = {"details": True, "marketplaceIds": ["ATVPDKIKX0DER"]}
    if next_token:
        kwargs["nextToken"] = next_token
    resp = inv_api.get_inventory_summary_marketplace(**kwargs).payload
    summaries = resp.get("inventorySummaries", [])
    print(f"Page {page}: {len(summaries)} items")
    all_items.extend(summaries)
    next_token = resp.get("pagination", {}).get("nextToken")
    if not next_token:
        break
    page += 1

print(f"\nTotal items across all pages: {len(all_items)}")

matches = [item for item in all_items if item.get("asin") == TARGET_ASIN]
print(f"\n--- Entries matching ASIN {TARGET_ASIN} in the bulk listing: {len(matches)} ---")
for m in matches:
    print(json.dumps(m, indent=2))

if not matches:
    print(f"NO entries for {TARGET_ASIN} found in the bulk listing at all.")
    print("All ASINs returned by the bulk call:")
    for item in all_items:
        print(" ", item.get("asin"), item.get("sellerSku"), item.get("inventoryDetails", {}).get("fulfillableQuantity"))
