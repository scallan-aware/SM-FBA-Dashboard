"""Definitive check: fetch the two ALREADY-COMPLETED reports directly by ID
(no new report creation, so no timeouts/duplicates) and compare their
campaignId sets exactly the way assemble_dashboard_data() does."""
import json
import gzip
import sp_api_backend as m
import requests

CAMPAIGN_REPORT_ID = "2aa5f5a6-08d3-47d4-81d4-aba1b17ff69c"
ASIN_AD_REPORT_ID = "7932449a-adb2-4338-9491-395da77e3fb4"

region = "NA"
token = m._refresh_ads_token()
base = m.ADS_API_ENDPOINTS.get(region, m.ADS_API_ENDPOINTS["NA"])
headers = {
    "Authorization": f"Bearer {token}",
    "Amazon-Advertising-API-ClientId": m.ADS_API_CREDENTIALS["client_id"],
    "Amazon-Advertising-API-Scope": m.ADS_API_CREDENTIALS["profile_id"],
}


def fetch_rows(report_id):
    status = requests.get(f"{base}/reporting/reports/{report_id}", headers=headers).json()
    if status.get("status") != "COMPLETED":
        print(f"Report {report_id} is not COMPLETED (status={status.get('status')})")
        return []
    raw = requests.get(status["url"]).content
    return json.loads(gzip.decompress(raw).decode("utf-8"))


campaign_rows = fetch_rows(CAMPAIGN_REPORT_ID)
asin_ad_rows = fetch_rows(ASIN_AD_REPORT_ID)

campaign_ids = set(str(r.get("campaignId")) for r in campaign_rows)
asin_ad_campaign_ids = set(str(r.get("campaignId")) for r in asin_ad_rows)

print(f"Campaign report: {len(campaign_rows)} rows, {len(campaign_ids)} distinct campaignIds")
print(f"Advertised-product report: {len(asin_ad_rows)} rows, {len(asin_ad_campaign_ids)} distinct campaignIds")

overlap = campaign_ids & asin_ad_campaign_ids
print(f"\nOverlap: {len(overlap)} campaignIds appear in BOTH reports")
print(f"In advertised-product report but NOT in campaign report: {len(asin_ad_campaign_ids - campaign_ids)}")

# Show what asins would be linked to campaigns, mimicking assemble_dashboard_data
campaign_to_asins = {}
for r in asin_ad_rows:
    cid = str(r.get("campaignId"))
    campaign_to_asins.setdefault(cid, set()).add(r.get("advertisedAsin"))

matched = 0
for cid in campaign_ids:
    if cid in campaign_to_asins:
        matched += 1
print(f"\nCampaigns (from campaign report) that WOULD get an asins[] tag: {matched} / {len(campaign_ids)}")

# Print a few concrete examples
print("\nExample campaign_id -> asins mapping (first 5):")
for cid, asins in list(campaign_to_asins.items())[:5]:
    in_campaign_report = cid in campaign_ids
    print(f"  {cid} -> {sorted(asins)}  (also seen in campaign report: {in_campaign_report})")
