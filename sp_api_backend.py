"""
Amazon FBA Dashboard — Data Backend
====================================
Pulls sales, inventory, and advertising (campaign + keyword/search-term)
data from Amazon Selling Partner API and Amazon Ads API, computes ACOS/TACOS,
scaling recommendations, keyword actions, and inventory alerts, then writes
a single dashboard_data.json that amazon-fba-dashboard.html reads.

No credentials yet? Run with --demo to generate realistic sample data so you
can use the full dashboard today. See SETUP_GUIDE.md for how to get real
SP-API + Ads API access.

Requirements
------------
    pip install python-amazon-sp-api requests python-dotenv

Usage
-----
    python sp_api_backend.py --demo                 # sample data, no creds needed
    python sp_api_backend.py --days 30               # live SP-API + Ads API pull
    python sp_api_backend.py --days 60 --marketplace US

Output
------
    dashboard_data.json   <- drop this next to amazon-fba-dashboard.html
"""

import os
import sys
import json
import time
import random
import argparse
import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Tunable business rules ────────────────────────────────────────────────
# Target ACOS band the whole recommendation engine is built around.
# Sean's v1 target: ~20-25% ACOS. Edit these to move the whole engine.
ACOS_SCALE_BELOW   = 15.0   # below this + enough orders -> scale up
ACOS_HOLD_LOW      = 15.0   # 15-25% -> maintain/monitor
ACOS_HOLD_HIGH     = 25.0
ACOS_OPTIMIZE_HIGH = 35.0   # 25-35% -> optimize/trim; above -> cut hard
TACOS_WARNING      = 20.0   # ad_spend / total_sales above this = over-reliant on ads

MIN_ORDERS_FOR_SCALE_CONFIDENCE = 3     # don't scale on a fluke conversion
MIN_CLICKS_FOR_SIGNAL           = 10    # below this, "needs more data"
NEG_KEYWORD_SPEND_THRESHOLD     = 10.0  # $ spent with 0 orders -> negative candidate
HARVEST_MIN_ORDERS              = 2     # search term converts this many times -> harvest to exact

DAYS_OF_COVER_CRITICAL = 14
DAYS_OF_COVER_LOW      = 30
DAYS_OF_COVER_OVERSTOCK = 90

# ── Credentials (env vars, or edit here) ──────────────────────────────────
# Note: SP-API dropped the AWS IAM / SigV4 requirement in Oct 2023. Only LWA
# credentials are needed now — no AWS account, IAM role, or access keys.
SP_API_CREDENTIALS = {
    "refresh_token":     os.getenv("SP_REFRESH_TOKEN",     "YOUR_LWA_REFRESH_TOKEN"),
    "lwa_app_id":        os.getenv("SP_LWA_APP_ID",        "YOUR_LWA_APP_ID"),
    "lwa_client_secret": os.getenv("SP_LWA_CLIENT_SECRET", "YOUR_LWA_CLIENT_SECRET"),
}

ADS_API_CREDENTIALS = {
    "client_id":     os.getenv("ADS_CLIENT_ID",     "YOUR_ADS_CLIENT_ID"),
    "client_secret": os.getenv("ADS_CLIENT_SECRET", "YOUR_ADS_CLIENT_SECRET"),
    "refresh_token": os.getenv("ADS_REFRESH_TOKEN", "YOUR_ADS_REFRESH_TOKEN"),
    "profile_id":    os.getenv("ADS_PROFILE_ID",    "YOUR_ADS_PROFILE_ID"),
}

MARKETPLACE_IDS = {
    "US": "ATVPDKIKX0DER", "UK": "A1F83G8C2ARO7P", "DE": "A1PA6795UKMFR9",
    "JP": "A1VC38T7YXB528", "CA": "A2EUQ1WTGCTBG2", "AU": "A39IBJ37TRP1C6",
}

ADS_API_ENDPOINTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}


# ── SP-API: sales, inventory ───────────────────────────────────────────────
# Note: this uses the FBA Inventory API (Amazon Fulfillment role) + Sales API
# (Inventory and Order Tracking role) rather than the Business Report, since
# GET_SALES_AND_TRAFFIC_REPORT requires the Brand Analytics role which can
# take a while for Amazon to approve/propagate even after it's requested.
# Trade-off: no sessions/CVR data (that's traffic-report-only). Once Brand
# Analytics access clears, swap this back to the Business Report for that.

def _fetch_inventory_via_summaries_api(inv_api, marketplace_id):
    """Fallback inventory source: FBA Inventory API's bulk getInventorySummaries.

    Known issue: when an ASIN has more than one sellerSku (e.g. an old/retired
    SKU and a current active one), this bulk endpoint appears to return only
    ONE summary row per ASIN and it isn't guaranteed to be the current active
    SKU — it can be a years-stale, zero-quantity duplicate, which silently
    makes an in-stock ASIN look out-of-stock in the dashboard. See
    _fetch_inventory_via_report() for the accurate replacement; this is kept
    only as a fallback if that report call fails for some reason.
    """
    inventory_by_asin = {}
    asin_rows = []
    seen_asins = set()
    next_token = None
    page = 1
    while True:
        kwargs = {"details": True, "marketplaceIds": [marketplace_id]}
        if next_token:
            kwargs["nextToken"] = next_token
        inv_resp = inv_api.get_inventory_summary_marketplace(**kwargs).payload
        summaries = inv_resp.get("inventorySummaries", [])
        print(f"  Inventory page {page}: {len(summaries)} items")
        for item in summaries:
            asin = item.get("asin")
            if not asin:
                continue
            available = item.get("inventoryDetails", {}).get("fulfillableQuantity", 0)
            inbound = (item.get("inventoryDetails", {}).get("inboundWorkingQuantity", 0)
                       + item.get("inventoryDetails", {}).get("inboundShippedQuantity", 0))
            if asin in inventory_by_asin:
                inventory_by_asin[asin]["units_available"] += available
                inventory_by_asin[asin]["units_inbound"] += inbound
            else:
                inventory_by_asin[asin] = {"units_available": available, "units_inbound": inbound}
            if asin not in seen_asins:
                seen_asins.add(asin)
                asin_rows.append({"asin": asin, "name": item.get("productName", asin)})
        next_token = inv_resp.get("pagination", {}).get("nextToken")
        if not next_token:
            break
        page += 1
        time.sleep(1)  # be polite between pages
    return asin_rows, inventory_by_asin


def _fetch_inventory_via_report(creds, mp, marketplace_id):
    """Accurate inventory source: the GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA
    report — this is the same report that powers Seller Central's own "Manage
    FBA Inventory" screen, one row per active sellerSku, so it doesn't have
    the per-ASIN SKU-collapsing bug that getInventorySummaries has.
    """
    import csv
    import io
    from sp_api.api import Reports
    from sp_api.base import ReportType

    reports_api = Reports(credentials=creds, marketplace=mp)
    create_resp = reports_api.create_report(
        reportType=ReportType.GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA,
        marketplaceIds=[marketplace_id],
    ).payload
    report_id = create_resp["reportId"]

    waited, timeout, interval = 0, 300, 10
    document_id = None
    while waited < timeout:
        status = reports_api.get_report(reportId=report_id).payload
        processing_status = status.get("processingStatus")
        if processing_status == "DONE":
            document_id = status["reportDocumentId"]
            break
        if processing_status in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"Inventory report {report_id} ended with status {processing_status}")
        time.sleep(interval)
        waited += interval
    if not document_id:
        raise TimeoutError(f"Inventory report {report_id} did not finish in {timeout}s")

    doc = reports_api.get_report_document(reportDocumentId=document_id, download=True).payload
    text = doc["document"]

    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    inventory_by_asin = {}
    asin_rows = []
    seen_asins = set()
    row_count = 0
    for row in reader:
        row_count += 1
        asin = row.get("asin")
        if not asin:
            continue
        available = int(float(row.get("afn-fulfillable-quantity") or 0))
        inbound = (int(float(row.get("afn-inbound-working-quantity") or 0))
                   + int(float(row.get("afn-inbound-shipped-quantity") or 0)))
        if asin in inventory_by_asin:
            # Multiple active SKUs can legitimately share one ASIN - sum them.
            inventory_by_asin[asin]["units_available"] += available
            inventory_by_asin[asin]["units_inbound"] += inbound
        else:
            inventory_by_asin[asin] = {"units_available": available, "units_inbound": inbound}
        if asin not in seen_asins:
            seen_asins.add(asin)
            asin_rows.append({"asin": asin, "name": row.get("product-name", asin)})
    print(f"  Inventory report: {row_count} SKU rows, {len(asin_rows)} unique ASINs")
    return asin_rows, inventory_by_asin


def fetch_sp_api_data(marketplace: str, days: int):
    """Fetch FBA inventory (with ASIN/product names) and per-ASIN sales via Sales API."""
    try:
        from sp_api.base import Marketplaces, Granularity
        from sp_api.api import Sales, Inventories
    except ImportError:
        print("ERROR: pip install python-amazon-sp-api")
        sys.exit(1)

    creds = dict(
        refresh_token=SP_API_CREDENTIALS["refresh_token"],
        lwa_app_id=SP_API_CREDENTIALS["lwa_app_id"],
        lwa_client_secret=SP_API_CREDENTIALS["lwa_client_secret"],
    )
    mp = getattr(Marketplaces, marketplace, Marketplaces.US)
    marketplace_id = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS["US"])

    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)

    print("Requesting FBA inventory report (accurate per-SKU stock levels)...")
    try:
        asin_rows, inventory_by_asin = _fetch_inventory_via_report(creds, mp, marketplace_id)
    except Exception as e:
        print(f"  Warning: inventory report failed ({e}); falling back to inventory summaries API "
              f"(note: that fallback can misreport stock for ASINs with multiple sellerSkus).")
        inv_api = Inventories(credentials=creds, marketplace=mp)
        asin_rows, inventory_by_asin = _fetch_inventory_via_summaries_api(inv_api, marketplace_id)

    print(f"Requesting sales metrics for {len(asin_rows)} ASINs via Sales API...")
    sales_api = Sales(credentials=creds, marketplace=mp)
    interval = (start, end)  # tuple of two datetimes, NOT a pre-joined string
    failed_count = 0
    for row in asin_rows:
        row["organic_sales"] = 0.0
        row["units"] = 0
        row["sessions"] = 0  # not available without the Business Report / Brand Analytics role

        for attempt in range(5):
            try:
                metrics = sales_api.get_order_metrics(
                    interval, Granularity.TOTAL, marketplaceIds=[marketplace_id], asin=row["asin"]
                ).payload
                entry = metrics[0] if isinstance(metrics, list) and metrics else (metrics or {})
                row["organic_sales"] = float(entry.get("totalSales", {}).get("amount", 0))
                row["units"] = int(entry.get("unitCount", 0))
                break
            except Exception as e:
                is_quota = "QuotaExceeded" in str(e) or "429" in str(e)
                if is_quota and attempt < 4:
                    wait = 2 ** (attempt + 1)  # 2, 4, 8, 16s backoff
                    time.sleep(wait)
                    continue
                print(f"  Warning: sales metrics failed for {row['asin']}: {e}")
                failed_count += 1
                break
        time.sleep(1.1)  # Sales API getOrderMetrics is rate-limited to roughly 0.5 req/sec

    if failed_count:
        print(f"  Note: sales metrics failed for {failed_count}/{len(asin_rows)} ASINs after retries.")

    # Authoritative account-wide total, independent of the ASIN list above (which is
    # sourced from current FBA inventory and can miss out-of-stock/new/FBM-only ASINs
    # that still had sales in the period). This is what the top-level summary uses,
    # so it should match Seller Central's own Business Report totals.
    print("Requesting account-wide sales total (ground truth, independent of ASIN list)...")
    account_totals = {"total_sales": 0.0, "total_units": 0}
    for attempt in range(5):
        try:
            metrics = sales_api.get_order_metrics(
                interval, Granularity.TOTAL, marketplaceIds=[marketplace_id]
            ).payload
            entry = metrics[0] if isinstance(metrics, list) and metrics else (metrics or {})
            account_totals["total_sales"] = float(entry.get("totalSales", {}).get("amount", 0))
            account_totals["total_units"] = int(entry.get("unitCount", 0))
            break
        except Exception as e:
            is_quota = "QuotaExceeded" in str(e) or "429" in str(e)
            if is_quota and attempt < 4:
                time.sleep(2 ** (attempt + 1))
                continue
            print(f"  Warning: account-wide sales total failed: {e}")
            break

    return asin_rows, inventory_by_asin, account_totals


# ── Ads API: campaigns + search terms/keywords ────────────────────────────

def fetch_ads_api_data(region: str, days: int):
    """Fetch campaign report and search-term report from Amazon Ads API v3."""
    import requests

    token = _refresh_ads_token()
    base = ADS_API_ENDPOINTS.get(region, ADS_API_ENDPOINTS["NA"])
    headers = {
        "Authorization": f"Bearer {token}",
        "Amazon-Advertising-API-ClientId": ADS_API_CREDENTIALS["client_id"],
        "Amazon-Advertising-API-Scope": ADS_API_CREDENTIALS["profile_id"],
        "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
    }

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    print("Requesting Sponsored Products campaign report...")
    campaign_rows_raw = _run_ads_report(
        requests, base, headers, start, end,
        ad_product="SPONSORED_PRODUCTS", group_by=["campaign"],
    )

    print("Requesting search term report (keyword-level)...")
    keyword_rows_raw = _run_ads_report(
        requests, base, headers, start, end,
        ad_product="SPONSORED_PRODUCTS", group_by=["searchTerm"],
        columns=["campaignId", "campaignName", "adGroupName", "keyword", "matchType", "searchTerm",
                 "impressions", "clicks", "cost", "sales14d", "purchases14d"],
    )

    print("Requesting advertised product report (ASIN-level ad spend/sales)...")
    asin_ad_rows_raw = _run_ads_report(
        requests, base, headers, start, end,
        ad_product="SPONSORED_PRODUCTS", group_by=["advertiser"],
        report_type_id="spAdvertisedProduct",
        columns=["campaignId", "advertisedAsin", "impressions", "clicks", "cost", "sales14d", "purchases14d"],
    )

    return _map_campaign_rows(campaign_rows_raw), _map_keyword_rows(keyword_rows_raw), _map_asin_ad_rows(asin_ad_rows_raw)


def _map_campaign_rows(raw_rows):
    """Translate raw Ads API v3 report fields into the shape the rest of the script expects."""
    mapped = []
    for r in raw_rows:
        mapped.append({
            "campaign_id": str(r.get("campaignId", r.get("campaignName", "unknown"))),
            "campaign_name": r.get("campaignName", "Unknown campaign"),
            "impressions": int(r.get("impressions", 0) or 0),
            "clicks": int(r.get("clicks", 0) or 0),
            "spend": float(r.get("cost", 0) or 0),
            "sales": float(r.get("sales14d", 0) or 0),
            "orders": int(r.get("purchases14d", 0) or 0),
        })
    return mapped


def _map_asin_ad_rows(raw_rows):
    """Translate raw Ads API v3 advertised-product report fields into {asin, spend, sales, campaign_id}.

    campaign_id is kept (not just used for the ASIN spend/sales rollup) so
    assemble_dashboard_data() can build a campaign_id -> ASIN(s) lookup and
    show which campaigns/keywords belong to a given ASIN in its detail view.
    """
    mapped = []
    for r in raw_rows:
        asin = r.get("advertisedAsin")
        if not asin:
            continue
        mapped.append({
            "asin": asin,
            "spend": float(r.get("cost", 0) or 0),
            "sales": float(r.get("sales14d", 0) or 0),
            "campaign_id": str(r.get("campaignId", "")),
        })
    return mapped


def _map_keyword_rows(raw_rows):
    """Translate raw Ads API v3 search-term report fields into the shape the rest of the script expects."""
    mapped = []
    for r in raw_rows:
        mapped.append({
            "campaign_id": str(r.get("campaignId", "")),
            "campaign_name": r.get("campaignName", "Unknown campaign"),
            "ad_group": r.get("adGroupName", ""),
            "keyword_text": r.get("keyword") or r.get("searchTerm") or "(unknown)",
            "match_type": (r.get("matchType") or "").lower(),
            "search_term": r.get("searchTerm", ""),
            "impressions": int(r.get("impressions", 0) or 0),
            "clicks": int(r.get("clicks", 0) or 0),
            "spend": float(r.get("cost", 0) or 0),
            "sales": float(r.get("sales14d", 0) or 0),
            "orders": int(r.get("purchases14d", 0) or 0),
        })
    return mapped


def _refresh_ads_token():
    import requests
    resp = requests.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": ADS_API_CREDENTIALS["refresh_token"],
        "client_id": ADS_API_CREDENTIALS["client_id"],
        "client_secret": ADS_API_CREDENTIALS["client_secret"],
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def _run_ads_report(requests_mod, base, headers, start, end, ad_product, group_by,
                     report_type_id=None, columns=None):
    """Create async report, poll, download. Returns list of row dicts."""
    is_campaign_level = "campaign" in group_by
    if columns is None:
        columns = (
            ["campaignName", "campaignId", "impressions", "clicks", "cost", "sales14d", "purchases14d"]
            if is_campaign_level else
            ["campaignName", "adGroupName", "keyword", "matchType", "searchTerm",
             "impressions", "clicks", "cost", "sales14d", "purchases14d"]
        )
    if report_type_id is None:
        report_type_id = "spCampaigns" if is_campaign_level else "spSearchTerm"
    body = {
        "name": f"{ad_product}-{'-'.join(group_by)}-report",
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
    r = requests_mod.post(f"{base}/reporting/reports", headers=headers, json=body)
    if r.status_code == 425:
        # Amazon dedups on the actual query (dates + config), not the "name"
        # label, so re-running for the same date window gets rejected as a
        # duplicate of an earlier request - reuse that report instead of
        # failing. detail looks like: "The Request is a duplicate of : <id>"
        import re
        m = re.search(r"duplicate of\s*:\s*([a-f0-9-]+)", r.text)
        if m:
            report_id = m.group(1)
            print(f"  Report request was a duplicate of an existing one; reusing report {report_id}.")
        else:
            print(f"  Ads API error {r.status_code}: {r.text}")
            r.raise_for_status()
    else:
        if not r.ok:
            print(f"  Ads API error {r.status_code}: {r.text}")
        r.raise_for_status()
        report_id = r.json()["reportId"]

    waited, timeout, interval = 0, 300, 10
    download_url = None
    while waited < timeout:
        status_r = requests_mod.get(f"{base}/reporting/reports/{report_id}", headers=headers)
        status = status_r.json()
        if status.get("status") == "COMPLETED":
            download_url = status["url"]
            break
        if status.get("status") == "FAILURE":
            raise RuntimeError(f"Ads report {report_id} failed")
        time.sleep(interval)
        waited += interval
    if not download_url:
        raise TimeoutError(f"Ads report {report_id} did not finish in {timeout}s")

    import gzip, io
    raw = requests_mod.get(download_url).content
    rows = json.loads(gzip.decompress(raw).decode("utf-8"))
    return rows


# ── Recommendation engine ─────────────────────────────────────────────────

def acos(spend, sales):
    return round((spend / sales * 100), 2) if sales else (999.0 if spend else 0.0)


def campaign_recommendation(c):
    """Scale / hold / optimize / cut, based on ACOS + statistical confidence."""
    if c["clicks"] < MIN_CLICKS_FOR_SIGNAL:
        return "Needs more data", "Fewer than 10 clicks — let it run before acting."

    a = c["acos"]
    orders = c.get("orders", 0)

    if a == 0 and c["spend"] > 0:
        return "Cut", f"${c['spend']:.0f} spent with zero attributed sales. Pause or restructure."
    if a < ACOS_SCALE_BELOW and orders >= MIN_ORDERS_FOR_SCALE_CONFIDENCE:
        return "Scale", f"ACOS {a}% is well below your {ACOS_SCALE_BELOW:.0f}% floor. Increase budget/bids 15-20%."
    if a <= ACOS_HOLD_HIGH:
        return "Hold", f"ACOS {a}% is within your 15-25% target band. Maintain current bids/budget."
    if a <= ACOS_OPTIMIZE_HIGH:
        return "Optimize", f"ACOS {a}% is above target. Trim underperforming keywords, lower bids ~10%."
    return "Cut", f"ACOS {a}% is well above your 35% ceiling. Cut bids 20-30% or pause the campaign."


def keyword_recommendation(k):
    """Per-keyword/search-term action: raise, lower, pause/negative, or harvest."""
    spend, sales, orders, clicks = k["spend"], k["sales"], k["orders"], k["clicks"]
    a = k["acos"]
    is_search_term = bool(k.get("is_search_term"))

    if orders == 0 and spend >= NEG_KEYWORD_SPEND_THRESHOLD:
        label = "Add as negative"
        return label, f"${spend:.2f} spent, 0 orders. Add as negative exact to stop wasted spend."

    if clicks < MIN_CLICKS_FOR_SIGNAL:
        return "Needs more data", "Fewer than 10 clicks — too early to judge."

    if is_search_term and k.get("already_harvested"):
        return "Already harvested", "This search term already has a matching exact-match keyword live in the account — no further action needed here."

    if is_search_term and orders >= HARVEST_MIN_ORDERS and a < ACOS_HOLD_HIGH:
        return "Harvest to exact", f"Converts well ({orders} orders, {a}% ACOS) but isn't an exact-match keyword yet. Add as new exact-match keyword and monitor separately."

    if a < ACOS_SCALE_BELOW and orders >= MIN_ORDERS_FOR_SCALE_CONFIDENCE:
        return "Raise bid", f"ACOS {a}% is strong. Raise bid 10-20% to capture more volume."
    if a <= ACOS_HOLD_HIGH:
        return "Hold", f"ACOS {a}% is on target. No change."
    if a <= ACOS_OPTIMIZE_HIGH:
        return "Lower bid", f"ACOS {a}% is above target. Lower bid ~15%."
    return "Lower bid / pause", f"ACOS {a}% is well above target. Lower bid 25-30% or pause."


def _acos_text(a):
    return "∞ (no sales)" if a >= 999 else f"{a}%"


def asin_insights(row, keywords=None, campaigns=None):
    """Per-ASIN diagnostic insights + concrete suggestions for the Overview
    detail drill-down: organic/listing health, paid ACOS + which keywords are
    driving it, and inventory position — each with an actionable next step.
    """
    keywords = keywords or []
    campaigns = campaigns or []
    insights = []
    total_sales = row["organic_sales"] + row["paid_sales"]

    # ── Organic / listing health ──────────────────────────────────────────
    if total_sales == 0:
        insights.append({
            "severity": "high", "issue": "No sales in this period",
            "suggestion": "Confirm the listing is active and buyable, check price against competitors, "
                          "and make sure your keywords still match what shoppers are searching for.",
        })
    elif row["organic_sales"] == 0 and row["paid_sales"] > 0:
        insights.append({
            "severity": "high", "issue": "Organic sales are $0 — 100% ad-dependent",
            "suggestion": "Little to no organic ranking. Refresh the title with top keywords, upgrade the "
                          "main + lifestyle images, strengthen bullet points and backend search terms, and "
                          "check price competitiveness.",
        })
    else:
        organic_ratio = (row["organic_sales"] / total_sales) if total_sales else 0
        if organic_ratio < 0.25:
            insights.append({
                "severity": "medium", "issue": f"Only {round(organic_ratio * 100)}% of sales are organic",
                "suggestion": "Heavily ads-reliant. Investing in listing content (title, images, A+ content) "
                              "and backend keywords should lift organic ranking so you rely less on paid traffic.",
            })

    if row["sessions"] and row["sessions"] >= 200 and row["cvr"] < 5:
        insights.append({
            "severity": "medium", "issue": f"Conversion rate is {row['cvr']}% on {row['sessions']} sessions",
            "suggestion": "Traffic is healthy but not converting — review the main image, price, reviews/rating, "
                          "and A+ content.",
        })

    # ── Paid ACOS + the keywords driving it ───────────────────────────────
    if row["ad_spend"] > 0:
        a = row["acos"]
        if a >= 999:
            insights.append({
                "severity": "high", "issue": f"${row['ad_spend']:.0f} spent with $0 attributed paid sales",
                "suggestion": "Pause or restructure these campaigns — check the Keyword Analysis tab (filtered "
                              "to this ASIN) for search terms burning spend with zero orders.",
            })
        elif a >= ACOS_OPTIMIZE_HIGH:
            worst = sorted([k for k in keywords if k["spend"] > 0], key=lambda k: k["acos"], reverse=True)[:5]
            if worst:
                names = ", ".join(f"\"{k['keyword_text']}\" ({_acos_text(k['acos'])})" for k in worst)
                suggestion = f"Cut or pause the worst-performing keywords for this ASIN: {names}."
            else:
                suggestion = "Cut or pause the worst-performing keywords for this ASIN (see Keyword Analysis)."
            insights.append({"severity": "high", "issue": f"Paid ACOS is {_acos_text(a)} — well above target",
                              "suggestion": suggestion})
        elif a > ACOS_HOLD_HIGH:
            insights.append({
                "severity": "medium", "issue": f"Paid ACOS is {_acos_text(a)} — above target",
                "suggestion": "Trim bids ~10-15% on the weakest keywords for this ASIN (see Keyword Analysis, "
                              "filtered to this ASIN).",
            })
        elif a < ACOS_SCALE_BELOW:
            insights.append({
                "severity": "low", "issue": f"Paid ACOS is {_acos_text(a)} — well below target",
                "suggestion": "Strong efficiency. Consider raising bids/budget 15-20% on these campaigns to "
                              "capture more volume.",
            })

    # ── Inventory position ─────────────────────────────────────────────────
    doc = row["days_of_cover"]
    if doc is not None:
        if row["inventory_alert"] == "critical":
            insights.append({
                "severity": "high", "issue": f"Only {doc} days of stock left",
                "suggestion": "Reorder now to avoid a stockout — see Inventory Alerts for the recommended quantity.",
            })
        elif row["inventory_alert"] == "low":
            insights.append({
                "severity": "medium", "issue": f"{doc} days of stock left",
                "suggestion": "Getting low — place a reorder soon so it lands before you run out.",
            })
        elif row["inventory_alert"] == "overstock":
            severe = doc > DAYS_OF_COVER_OVERSTOCK * 2
            insights.append({
                "severity": "high" if severe else "medium",
                "issue": f"{doc} days of cover ({row['units_available']} units on hand)",
                "suggestion": ("Significantly overstocked — consider a meaningful price cut, a limited-time "
                               "promotion/coupon, or removing excess units to avoid long-term storage fees."
                               if severe else
                               "Overstocked. A modest price decrease or a promotion would help move inventory faster."),
            })

    if not insights:
        insights.append({"severity": "low", "issue": "Performing within target",
                          "suggestion": "No action needed right now — keep monitoring."})

    return insights


def inventory_alert_level(days_of_cover):
    if days_of_cover is None:
        return "unknown"
    if days_of_cover < DAYS_OF_COVER_CRITICAL:
        return "critical"
    if days_of_cover < DAYS_OF_COVER_LOW:
        return "low"
    if days_of_cover > DAYS_OF_COVER_OVERSTOCK:
        return "overstock"
    return "ok"


# ── Demo / sample data (no credentials required) ──────────────────────────

def generate_demo_data(days: int):
    random.seed(7)
    products = [
        ("B09XK7P4VL", "Premium Bamboo Cutting Board Set (3-Piece)", 22.0),
        ("B08CHTPJ3D", "Stainless Steel Insulated Water Bottle 32oz", 18.5),
        ("B0BXRQT2MN", "Non-Stick Ceramic Frying Pan 12\"", 31.0),
        ("B07MFZX89K", "Silicone Kitchen Utensil Set (24 pcs)", 13.5),
        ("B0C4WKJD9R", "Electric Spice Grinder Stainless", 38.0),
        ("B08NWKCPQV", "Reusable Produce Mesh Bags (15-pack)", 9.0),
        ("B0D2FKRTYZ", "Cast Iron Skillet Pre-Seasoned 10\"", 27.5),
    ]

    asins, inventory = [], {}
    for asin, name, base_acos in products:
        sessions = random.randint(2800, 9500)
        units = int(sessions * random.uniform(0.04, 0.11))
        organic = round(units * random.uniform(15, 32) * random.uniform(0.55, 0.85), 2)
        paid = round(units * random.uniform(15, 32) * random.uniform(0.15, 0.45), 2)
        asins.append({"asin": asin, "name": name, "organic_sales": organic, "units": units, "sessions": sessions})

        avg_daily_units = max(1, units / days)
        units_available = int(avg_daily_units * random.choice([8, 20, 35, 55, 100]))
        inventory[asin] = {"units_available": units_available, "units_inbound": random.randint(0, 400)}

    campaigns_raw = []
    for asin, name, base_acos in products:
        for suffix, mult in [(" - Auto", 0.8), (" - Exact", 1.0), (" - Broad", 1.3)]:
            spend = round(random.uniform(80, 900), 2)
            target_acos = base_acos * mult / 100
            sales = round(spend / target_acos, 2) if target_acos > 0 else 0
            clicks = random.randint(5, 400)
            orders = max(0, int(sales / random.uniform(18, 35)))
            campaigns_raw.append({
                "campaign_id": f"{asin}{suffix}",
                "campaign_name": f"{name[:28]}{suffix}",
                "asin": asin,
                "impressions": clicks * random.randint(15, 60),
                "clicks": clicks,
                "spend": spend,
                "sales": sales,
                "orders": orders,
            })

    keyword_pool = [
        "bamboo cutting board", "wood cutting board set", "kitchen cutting board",
        "insulated water bottle", "32 oz water bottle", "stainless steel bottle",
        "ceramic frying pan", "non stick pan 12 inch", "cookware set",
        "silicone utensils", "kitchen utensil set", "cooking spatula set",
        "spice grinder electric", "coffee grinder", "salt pepper grinder",
        "mesh produce bags", "reusable grocery bags", "vegetable storage bags",
        "cast iron skillet", "pre seasoned skillet", "cast iron pan 10 inch",
    ]
    keywords_raw = []
    for asin, name, base_acos in products:
        relevant_terms = random.sample(keyword_pool, 4)
        for term in relevant_terms:
            spend = round(random.uniform(5, 220), 2)
            eff = base_acos * random.uniform(0.5, 1.8) / 100
            sales = round(spend / eff, 2) if eff > 0 else 0
            clicks = random.randint(1, 220)
            orders = max(0, int(sales / random.uniform(18, 35)))
            if random.random() < 0.15:
                orders, sales = 0, 0.0  # some pure-spend losers
            keywords_raw.append({
                "campaign_id": f"{asin} - Exact",
                "campaign_name": f"{name[:28]} - Exact",
                "ad_group": f"{name[:20]} AG1",
                "keyword_text": term,
                "match_type": random.choice(["exact", "phrase", "broad"]),
                "search_term": term if random.random() < 0.4 else "",
                "impressions": clicks * random.randint(15, 60),
                "clicks": clicks,
                "spend": spend,
                "sales": sales,
                "orders": orders,
            })

    asin_ad_rows = [{"asin": c["asin"], "spend": c["spend"], "sales": c["sales"],
                      "campaign_id": c["campaign_id"]} for c in campaigns_raw]

    return asins, inventory, campaigns_raw, keywords_raw, asin_ad_rows


# ── Assembly ───────────────────────────────────────────────────────────────

def assemble_dashboard_data(asins, inventory, campaigns_raw, keywords_raw, asin_ad_rows, days, marketplace,
                             account_totals=None):
    # merge ad spend/sales onto asin rows using the ASIN-level ad attribution rows
    # (from the advertised-product report live, or tagged directly in demo data)
    ad_by_asin = {}
    campaign_to_asins = {}
    for r in asin_ad_rows:
        d = ad_by_asin.setdefault(r["asin"], {"spend": 0.0, "sales": 0.0})
        d["spend"] += r["spend"]
        d["sales"] += r["sales"]
        cid = r.get("campaign_id")
        if cid:
            campaign_to_asins.setdefault(cid, set()).add(r["asin"])

    asin_out = []
    inventory_alerts = []
    for row in asins:
        asin = row["asin"]
        ad = ad_by_asin.get(asin, {"spend": 0.0, "sales": 0.0})
        organic = row["organic_sales"]
        paid = ad["sales"]
        units = row["units"]
        sessions = row["sessions"]
        a = acos(ad["spend"], paid)

        inv = inventory.get(asin, {"units_available": 0, "units_inbound": 0})
        avg_daily_units = units / days if days else 0
        days_of_cover = round(inv["units_available"] / avg_daily_units, 1) if avg_daily_units > 0 else None
        alert_level = inventory_alert_level(days_of_cover)

        asin_out.append({
            "asin": asin, "name": row["name"], "organic_sales": round(organic, 2),
            "paid_sales": round(paid, 2), "ad_spend": round(ad["spend"], 2),
            "units": units, "sessions": sessions, "acos": a,
            "cvr": round((units / sessions * 100), 2) if sessions else 0,
            "units_available": inv["units_available"], "units_inbound": inv["units_inbound"],
            "days_of_cover": days_of_cover, "inventory_alert": alert_level,
        })

        if alert_level in ("critical", "low", "overstock"):
            reorder_qty = int(avg_daily_units * 60 - inv["units_available"]) if avg_daily_units > 0 else 0
            inventory_alerts.append({
                "asin": asin, "name": row["name"], "units_available": inv["units_available"],
                "units_inbound": inv["units_inbound"], "avg_daily_units": round(avg_daily_units, 1),
                "days_of_cover": days_of_cover, "alert_level": alert_level,
                "recommended_reorder_qty": max(0, reorder_qty) if alert_level != "overstock" else 0,
            })

    campaigns_out = []
    for c in campaigns_raw:
        a = acos(c["spend"], c["sales"])
        rec, reason = campaign_recommendation({**c, "acos": a})
        campaigns_out.append({
            "campaign_id": c["campaign_id"], "campaign_name": c["campaign_name"],
            "impressions": c["impressions"], "clicks": c["clicks"],
            "spend": round(c["spend"], 2), "sales": round(c["sales"], 2),
            "orders": c["orders"], "acos": a,
            "recommendation": rec, "recommendation_reason": reason,
            "asins": sorted(campaign_to_asins.get(c["campaign_id"], [])),
        })
    campaigns_out.sort(key=lambda x: x["sales"], reverse=True)

    # Detect search terms that already have a live exact-match keyword targeting
    # them, so "Harvest to exact" stops firing once you've actually done it in
    # the Ads console — the very next report pull that includes the new exact
    # keyword's own row is what flips this, no manual tracking needed.
    exact_keywords_by_campaign = {}
    for k in keywords_raw:
        if (k.get("match_type") or "").lower() == "exact":
            camp = k["campaign_name"]
            text = (k.get("keyword_text") or "").strip().lower()
            exact_keywords_by_campaign.setdefault(camp, set()).add(text)

    keywords_out = []
    for k in keywords_raw:
        a = acos(k["spend"], k["sales"])
        is_search_term = bool(k.get("search_term"))
        already_harvested = False
        if is_search_term:
            term_text = (k.get("search_term") or k.get("keyword_text") or "").strip().lower()
            already_harvested = term_text in exact_keywords_by_campaign.get(k["campaign_name"], set())
        rec, reason = keyword_recommendation({
            **k, "acos": a, "is_search_term": is_search_term, "already_harvested": already_harvested,
        })
        keywords_out.append({
            "campaign_name": k["campaign_name"], "ad_group": k["ad_group"],
            "keyword_text": k["keyword_text"], "match_type": k["match_type"],
            "search_term": k.get("search_term", ""),
            "impressions": k["impressions"], "clicks": k["clicks"],
            "spend": round(k["spend"], 2), "sales": round(k["sales"], 2),
            "orders": k["orders"], "acos": a,
            "cvr": round((k["orders"] / k["clicks"] * 100), 2) if k["clicks"] else 0,
            "cpc": round((k["spend"] / k["clicks"]), 2) if k["clicks"] else 0,
            "recommendation": rec, "recommendation_reason": reason,
            "asins": sorted(campaign_to_asins.get(k.get("campaign_id", ""), [])),
        })
    keywords_out.sort(key=lambda x: x["spend"], reverse=True)

    # Index campaigns/keywords by ASIN (via the campaign_id -> ASIN(s) map built
    # above) so the Overview drill-down can show exactly which campaigns and
    # keywords belong to a given product, and so asin_insights() can name the
    # actual worst-performing keywords rather than just pointing at a tab.
    keywords_by_asin = {}
    for k in keywords_out:
        for asin in k.get("asins", []):
            keywords_by_asin.setdefault(asin, []).append(k)
    campaigns_by_asin = {}
    for c in campaigns_out:
        for asin in c.get("asins", []):
            campaigns_by_asin.setdefault(asin, []).append(c)

    for row in asin_out:
        row["insights"] = asin_insights(
            row, keywords_by_asin.get(row["asin"], []), campaigns_by_asin.get(row["asin"], []),
        )
        row["campaign_count"] = len(campaigns_by_asin.get(row["asin"], []))
        row["keyword_count"] = len(keywords_by_asin.get(row["asin"], []))

    # Ground truth for the top-line summary: account-wide sales, independent of the
    # ASIN list (which is sourced from current FBA inventory and can miss ASINs that
    # had sales but aren't currently in stock). Falls back to summing per-ASIN rows
    # in demo mode or if the account-wide call failed.
    summed_sales = sum(r["organic_sales"] + r["paid_sales"] for r in asin_out)
    summed_units = sum(r["units"] for r in asin_out)
    if account_totals and account_totals.get("total_sales"):
        total_sales = account_totals["total_sales"]
        total_units = account_totals["total_units"]
    else:
        total_sales = summed_sales
        total_units = summed_units
    total_ad_spend = sum(r["ad_spend"] for r in asin_out)
    total_sessions = sum(r["sessions"] for r in asin_out)
    overall_acos = acos(total_ad_spend, sum(r["paid_sales"] for r in asin_out))
    tacos = round((total_ad_spend / total_sales * 100), 2) if total_sales else 0

    # Stable IDs (independent of day-to-day numbers like $ spent or ACOS%) so the
    # dashboard can remember which action items a user has already marked done
    # across daily data refreshes, keyed by the underlying entity rather than
    # the rendered text.
    top_line = []
    for k in keywords_out:
        if k["recommendation"] == "Add as negative" and k["spend"] >= NEG_KEYWORD_SPEND_THRESHOLD * 2:
            top_line.append({
                "id": f"negkw::{k['campaign_name']}::{k['keyword_text']}",
                "type": "Negative keyword", "priority": "high",
                "text": f"'{k['keyword_text']}' ({k['campaign_name']}): ${k['spend']:.0f} spent, 0 orders. Add as negative."})
    for c in campaigns_out:
        if c["recommendation"] == "Scale":
            top_line.append({
                "id": f"scale::{c['campaign_id']}",
                "type": "Scale opportunity", "priority": "high",
                "text": f"{c['campaign_name']}: ACOS {c['acos']}%. {c['recommendation_reason']}"})
        elif c["recommendation"] == "Cut":
            top_line.append({
                "id": f"cut::{c['campaign_id']}",
                "type": "Cut / restructure", "priority": "high",
                "text": f"{c['campaign_name']}: {c['recommendation_reason']}"})
    for a in inventory_alerts:
        if a["alert_level"] == "critical":
            top_line.append({
                "id": f"inv::{a['asin']}",
                "type": "Inventory critical", "priority": "high",
                "text": f"{a['name']}: {a['days_of_cover']} days of cover left. Reorder {a['recommended_reorder_qty']} units now."})
    if tacos > TACOS_WARNING:
        top_line.append({
            "id": "tacos::overall",
            "type": "TACOS warning", "priority": "medium",
            "text": f"TACOS is {tacos}% (ads / total sales). Over {TACOS_WARNING:.0f}% suggests over-reliance on paid traffic — invest in organic/SEO."})

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "marketplace": marketplace,
        "period_days": days,
        "summary": {
            "total_sales": round(total_sales, 2), "total_units": total_units,
            "sessions": total_sessions, "ad_spend": round(total_ad_spend, 2),
            "acos": overall_acos, "tacos": tacos,
            "cvr": round((total_units / total_sessions * 100), 2) if total_sessions else 0,
        },
        "asins": sorted(asin_out, key=lambda x: x["organic_sales"] + x["paid_sales"], reverse=True),
        "campaigns": campaigns_out,
        "keywords": keywords_out,
        "inventory_alerts": sorted(inventory_alerts, key=lambda x: (x["days_of_cover"] is None, x["days_of_cover"])),
        "recommendations": top_line,
        "rules": {
            "acos_scale_below": ACOS_SCALE_BELOW, "acos_hold_low": ACOS_HOLD_LOW,
            "acos_hold_high": ACOS_HOLD_HIGH, "acos_optimize_high": ACOS_OPTIMIZE_HIGH,
            "tacos_warning": TACOS_WARNING,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--marketplace", default="US")
    parser.add_argument("--region", default="NA", help="Ads API region: NA, EU, FE")
    parser.add_argument("--demo", action="store_true", help="Generate sample data, no credentials needed")
    parser.add_argument("--out", default="dashboard_data.json")
    parser.add_argument("--html", default="amazon-fba-dashboard.html",
                         help="Dashboard HTML file to update with embedded fresh data (set to '' to skip)")
    args = parser.parse_args()

    account_totals = None
    if args.demo or SP_API_CREDENTIALS["refresh_token"].startswith("YOUR_"):
        if not args.demo:
            print("No credentials configured — falling back to --demo sample data.")
            print("See SETUP_GUIDE.md to connect real SP-API + Ads API data.\n")
        asins, inventory, campaigns_raw, keywords_raw, asin_ad_rows = generate_demo_data(args.days)
    else:
        asins, inventory, account_totals = fetch_sp_api_data(args.marketplace, args.days)
        campaigns_raw, keywords_raw, asin_ad_rows = fetch_ads_api_data(args.region, args.days)

    data = assemble_dashboard_data(asins, inventory, campaigns_raw, keywords_raw, asin_ad_rows, args.days,
                                    args.marketplace, account_totals=account_totals)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(data, indent=2))
    print(f"Wrote {out_path.resolve()}  ({len(data['campaigns'])} campaigns, "
          f"{len(data['keywords'])} keywords, {len(data['inventory_alerts'])} inventory alerts)")

    if args.html:
        embed_data_in_html(data, Path(args.html))


def embed_data_in_html(data, html_path: Path):
    """Bake fresh data directly into the dashboard HTML's SAMPLE_DATA blob.

    This makes the file self-contained, so it works correctly when opened
    directly (file:// URL) — browsers block fetch() of local JSON files for
    security reasons, so relying on that alone would silently keep showing
    stale data forever when double-clicked instead of served over http.
    """
    if not html_path.exists():
        print(f"Note: {html_path} not found, skipping HTML embed.")
        return
    html = html_path.read_text()
    start_marker, end_marker = "/*DATA_START*/", "/*DATA_END*/"
    start = html.find(start_marker)
    end = html.find(end_marker)
    if start == -1 or end == -1:
        print(f"Note: markers not found in {html_path}, skipping HTML embed.")
        return
    start += len(start_marker)
    new_html = html[:start] + json.dumps(data, indent=2) + html[end:]
    html_path.write_text(new_html)
    print(f"Updated {html_path.resolve()} with fresh embedded data (works when opened directly).")


if __name__ == "__main__":
    main()
