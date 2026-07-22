"""One-off diagnostic: why are Campaigns/Keywords showing (0) in the ASIN
detail modal? The dashboard links a campaign to an ASIN via campaignId, taken
from both the campaign-level report and the advertised-product report. This
pulls both raw reports directly (bypassing the _map_* functions) and prints
the campaignId values from each side by side, so we can see whether they
actually match. Polls for up to 10 minutes per report (these can be slow).
"""
import time
import json
import gzip
import datetime
import requests
import sp_api_backend as m

region = "NA"
days = 30

token = m._refresh_ads_token()
base = m.ADS_API_ENDPOINTS.get(region, m.ADS_API_ENDPOINTS["NA"])
headers = {
    "Authorization": f"Bearer {token}",
    "Amazon-Advertising-API-ClientId": m.ADS_API_CREDENTIALS["client_id"],
    "Amazon-Advertising-API-Scope": m.ADS_API_CREDENTIALS["profile_id"],
    "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
}
end = datetime.date.today()
start = end - datetime.timedelta(days=days)


def run_report(ad_product, group_by, report_type_id, columns, timeout=600, interval=10):
    is_campaign_level = "campaign" in group_by
    body = {
        "name": f"debug-{'-'.join(group_by)}-report-{int(time.time())}",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "configuration": {
            "adProduct": ad_product,
            "groupBy": group_by,
            "columns": columns,
            "reportTypeId": report_type_id,
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    r = requests.post(f"{base}/reporting/reports", headers=headers, json=body)
    if r.status_code == 425:
        import re
        dup = re.search(r"duplicate of\s*:\s*([a-f0-9-]+)", r.text)
        if dup:
            report_id = dup.group(1)
            print(f"  Duplicate of an earlier request; reusing report {report_id}.")
        else:
            print(f"  Ads API error {r.status_code}: {r.text}")
            r.raise_for_status()
    else:
        if not r.ok:
            print(f"  Ads API error {r.status_code}: {r.text}")
        r.raise_for_status()
        report_id = r.json()["reportId"]
    print(f"  Report {report_id}, polling...")

    waited = 0
    while waited < timeout:
        status_r = requests.get(f"{base}/reporting/reports/{report_id}", headers=headers)
        status = status_r.json()
        if status.get("status") == "COMPLETED":
            raw = requests.get(status["url"]).content
            return json.loads(gzip.decompress(raw).decode("utf-8"))
        if status.get("status") == "FAILURE":
            raise RuntimeError(f"Report {report_id} failed: {status}")
        time.sleep(interval)
        waited += interval
    print(f"  Report {report_id} still not done after {timeout}s.")
    print(f"  Check it later with: python3 debug_check_report.py {report_id}")
    return []


print("--- Campaign-level report (spCampaigns) raw rows ---")
campaign_rows = run_report(
    "SPONSORED_PRODUCTS", ["campaign"], "spCampaigns",
    ["campaignName", "campaignId", "impressions", "clicks", "cost", "sales14d", "purchases14d"],
)
for r in campaign_rows[:5]:
    print(r)
print(f"\nTotal campaign rows: {len(campaign_rows)}")
campaign_ids = set(str(r.get("campaignId")) for r in campaign_rows)
print("Sample campaignId values (campaign report):", list(campaign_ids)[:5])

print("\n--- Advertised-product report (spAdvertisedProduct) raw rows ---")
asin_ad_rows = run_report(
    "SPONSORED_PRODUCTS", ["advertiser"], "spAdvertisedProduct",
    ["campaignId", "advertisedAsin", "impressions", "clicks", "cost", "sales14d", "purchases14d"],
)
for r in asin_ad_rows[:5]:
    print(r)
print(f"\nTotal advertised-product rows: {len(asin_ad_rows)}")
asin_ad_campaign_ids = set(str(r.get("campaignId")) for r in asin_ad_rows)
print("Sample campaignId values (advertised-product report):", list(asin_ad_campaign_ids)[:5])

overlap = campaign_ids & asin_ad_campaign_ids
print(f"\ncampaignId values present in BOTH reports: {len(overlap)} / {len(asin_ad_campaign_ids)}")
if not overlap and campaign_ids and asin_ad_campaign_ids:
    print("NO OVERLAP AT ALL -> confirms the join is broken; compare the raw field types/values printed above.")
