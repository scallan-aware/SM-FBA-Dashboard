# Setup Guide — Connecting Real Data

The dashboard works right now with sample data. This guide covers connecting it to your real Seller Central account. It's two separate credential sets: SP-API (sales, inventory) and Ads API (campaigns, keywords).

## 1. SP-API (sales + inventory)

Note: Amazon dropped the AWS IAM / Signature requirement for SP-API back in October 2023. No AWS account, IAM role, or access keys needed — just LWA credentials.

1. Go to Seller Central → Apps and Services → Develop Apps for Amazon.
2. Create a new app ("private" / self-authorized is fine for a single account).
3. In Seller Central's developer console, click "View" on your app's LWA credentials to get `lwa_app_id` and `lwa_client_secret`.
4. Self-authorize your app against your own account (must be the Primary User) — this gives you a **refresh token**.
5. You'll end up with: `lwa_app_id`, `lwa_client_secret`, `refresh_token`. That's it.

Amazon's official walkthrough: https://developer-docs.amazon.com/sp-api/docs/registering-your-application

## 2. Advertising API (campaigns + keywords)

1. Go to https://advertising.amazon.com/API/docs/en-us/getting-started/overview — register as an API developer (separate from SP-API).
2. Create a security profile in Amazon's Developer Console to get a `client_id` and `client_secret`.
3. Authorize against your Ads account to get an Ads **refresh token**.
4. Find your **profile ID** by calling the `/v2/profiles` endpoint once authorized — this identifies which advertising account/marketplace to pull from.

You'll end up with: `client_id`, `client_secret`, `refresh_token`, `profile_id`.

## 3. Plug credentials into the script

Set these as environment variables (recommended) or edit the constants directly at the top of `sp_api_backend.py`:

```
SP_REFRESH_TOKEN=...
SP_LWA_APP_ID=...
SP_LWA_CLIENT_SECRET=...

ADS_CLIENT_ID=...
ADS_CLIENT_SECRET=...
ADS_REFRESH_TOKEN=...
ADS_PROFILE_ID=...
```

Easiest way: create a `.env` file in the same folder as the script (the script loads it automatically if `python-dotenv` is installed).

## 4. Run it

```
pip install python-amazon-sp-api requests python-dotenv
python sp_api_backend.py --days 30
```

This writes `dashboard_data.json` next to `amazon-fba-dashboard.html`. Reopen (or refresh) the dashboard in your browser and it will pick up the real numbers automatically — no code changes needed.

## 5. Keep it current

Re-run the script whenever you want fresh numbers (e.g. daily, via cron/Task Scheduler). Until then, the dashboard shows whatever `dashboard_data.json` last contained.

## Notes

- The recommendation engine's ACOS thresholds (scale under 15%, hold 15-25%, optimize 25-35%, cut above 35%) are defined as constants near the top of `sp_api_backend.py` — edit those if your target ACOS differs.
- `--demo` regenerates sample data any time — useful for testing dashboard changes without hitting the real APIs.
