"""Check the status/result of a specific Ads API report ID that already exists,
without creating a new one. Useful when a report just took a while to finish
after our client-side polling gave up."""
import sys
import json
import gzip
import sp_api_backend as m

if len(sys.argv) < 2:
    print("Usage: python3 debug_check_report.py <reportId>")
    sys.exit(1)

report_id = sys.argv[1]
region = "NA"

token = m._refresh_ads_token()
base = m.ADS_API_ENDPOINTS.get(region, m.ADS_API_ENDPOINTS["NA"])
headers = {
    "Authorization": f"Bearer {token}",
    "Amazon-Advertising-API-ClientId": m.ADS_API_CREDENTIALS["client_id"],
    "Amazon-Advertising-API-Scope": m.ADS_API_CREDENTIALS["profile_id"],
}

import requests
status_r = requests.get(f"{base}/reporting/reports/{report_id}", headers=headers)
status = status_r.json()
print("Status:", json.dumps(status, indent=2))

if status.get("status") == "COMPLETED":
    raw = requests.get(status["url"]).content
    rows = json.loads(gzip.decompress(raw).decode("utf-8"))
    print(f"\nTotal rows: {len(rows)}")
    print("First 5 rows:")
    for r in rows[:5]:
        print(r)
