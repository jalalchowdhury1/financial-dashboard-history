# QA Verification Matrix - Automated Data Pipeline

## Data Sanitization & Type Casting
- [x] Data is purely numerical or "N/A"
- [x] No letters or symbols (e.g., '%' or '<') are included
- [x] Extracted shorthand numbers (like 1404) are properly parsed to values like 1404000 where applicable (Note: JOLTS and Claims are returned from API in thousands e.g. 6542, representing 6,542,000, which we multiply by 1000)
- [x] Tested directly with production API

## Graceful Degradation / Forced Error Handling
- [x] Implemented a `safe_get` function that wraps every single metric in a try/except block
- [x] Forced failures log "N/A" but do *not* crash the extraction process of subsequent items
- [x] A 500 status code triggers a complete script crash to propagate to the global handler

## Secrets & Hardcoding
- [x] Credentials are NOT hardcoded in `.py` or `.yml` files
- [x] Uses the environment variable `GOOGLE_SHEETS_CREDS` for GH Actions and gracefully falls back to the local `finance-dashboard-history...json` file for dev builds
- [x] `GMAIL_APP_PASSWORD` is injected only from secrets

## Global Failure Routing
- [x] Global `try-except` block wraps `main()`
- [x] Catches critical failure and sends an email via `smtplib` using an App Password
- [x] Re-raises the error after the email to ensure the deployment runner marks it as "failed"

## CI/CD Cron
- [x] YAML exists at `.github/workflows/scraper.yml`
- [x] Installs Python 3.11+
- [x] Cron set to exactly `0 14,2 * * *`

**Status**: [PASS] - All checks completed by Lead Architect directly. Pipeline is architecturally sound and functional.
