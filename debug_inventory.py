"""One-off diagnostic: dump the raw SP-API inventory response for a specific SKU."""
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

SKU_TO_CHECK = "WG-1137"  # the SKU for the Wood Solitaire Game ASIN B00DYN8VQ0

inv_api = Inventories(credentials=creds, marketplace=Marketplaces.US)

print(f"--- Querying with sellerSkus=[{SKU_TO_CHECK!r}] ---")
resp = inv_api.get_inventory_summary_marketplace(
    details=True, marketplaceIds=["ATVPDKIKX0DER"], sellerSkus=[SKU_TO_CHECK]
).payload
print(json.dumps(resp, indent=2))
