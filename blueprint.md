# Master Blueprint - Automated Data Pipeline

> **CURRENT LIVE SCHEMA — updated 2026-06-04**
>
> `scraper.py` appends one row per run to the Google Sheet (tab `Sheet1`), mapped **purely by column position** (row 1 is a header row). The sheet is **append-only by column**: existing columns are never reordered or deleted — new metrics are added at the far right so all historical data stays aligned.
>
> Sources: `/api/fred`, `/api/market-extra`, and (new) `/api/sheets` on `https://financial-telegram-bot-beryl.vercel.app`.
>
> **Columns A–AM (39 total):** A=Date, then B–R = 17 FRED/checklist indicators, S–AF = 14 market-extra (real-estate / rates / FX / commodities), AG–AM = 7 metrics added 2026-06-04.
>
> **2026-06-04 change set:**
> - *Deleted on the dashboard:* **Leading Economic Index (LEI)** — the FRED source dropped the series. Column I is **intentionally kept** (history preserved) and now records `N/A` going forward. It is **not** removed, because removing it would shift every later column and misalign all past rows.
> - *Added (cols AG–AM):* Copper/Gold Ratio (`fred.indicators.copperGold.value`, replaced LEI on the dashboard), ATNHPI US House Price Index level (`market-extra.realEstate.atnhpi.current`), CAD/BDT (`market-extra.fx.cadbdt.current`), AAII DIFF (`sheets.AAIIDiff`), VIX Current / 3M (`sheets.VIX.current` / `.threeMonth`), VIX Fear/Greed (`sheets.VIX.fearGreed`, stored verbatim as text e.g. `GREED19`).
> - `/api/sheets` is fetched **non-fatally**: an outage there yields `N/A` for AG–AM only and never blocks the core FRED/market-extra history.

## Phase 1: Source Discovery & Resilient Extraction

Task 1: System Inspection & Data API Validation
Status: [REJECTED - FIX]
Worker Instructions: Before writing any scraping logic, inspect the local repository's frontend/backend routing (e.g., Next.js `api` folders or data fetching hooks) to determine if the `https://financial-telegram-bot-beryl.vercel.app/` dashboard is populated by a structured JSON endpoint. If a JSON endpoint exists, fetch from the API directly; if and only if there is no API, use the Python `scrapling` library to parse the DOM.
QA Criteria: Verify that the Worker has documented the existence or absence of a structured JSON API for the dashboard data before writing scraping code.
QA Notes: REJECTED. Browser network inspection confirms several active JSON API endpoints (/api/sheets, /api/spy, /api/fear-greed, /api/fred) that populate the dashboard metrics. Documenting "absence" is incorrect; must pivot to API-first extraction.

Task 2: Resilient Extraction of Target Metrics
Status: [TODO]
Worker Instructions: Extract the 17 exact target metrics: Yield Curve (10Y-2Y), Profit Margin, Sahm Rule, Consumer Sentiment, Initial Claims (4wk), BBB Credit Spread, Real Yields (10Y TIPS), Leading Economic Index, Market Valuation (P/E), System Tightness, M2 Money Supply, Retail Sales (3mo), Housing Starts, Industrial Production, Job Openings (JOLTS), Durable Goods Orders, Savings Rate. Wrap the extraction of each individual metric in a `try/except` block; if a specific CSS selector changes or a metric is missing, the script MUST NOT crash. It must assign the string "N/A" to that specific variable and continue extracting the rest.
QA Criteria: Verify the extraction script pulls all 17 specified metrics and that a forced parsing failure on a single metric successfully falls back to assigning "N/A" without terminating the script.
QA Notes: 

## Phase 2: Strict Data Sanitization & Type Casting

Task 3: Strict Data Sanitization & Type Casting
Status: [TODO]
Worker Instructions: Implement robust Regex/parsing to convert raw text into clean floats or integers. Strip all symbols (%, ~, +, <) and all descriptive text (<- Strong, healthy, easy, tight). Convert shorthand to raw numbers (e.g., 1404K becomes 1404000).
QA Criteria: Validate that the final numerical payload is strictly sanitized and contains absolutely no letters, symbols, or unnecessary formatting.
QA Notes: 

## Phase 3: Google Sheets API & Authentication

Task 4: Environment-Aware Authentication
Status: [TODO]
Worker Instructions: Use `gspread` and write logic that detects the environment. If running locally, authenticate using the local JSON file named `Finance Dashboard History`. If running on GitHub Actions, authenticate by parsing a GitHub Secret environment variable into a temporary JSON file/dictionary.
QA Criteria: Confirm that the authentication seamlessly switches based on environment variables and that no credentials or secret keys are hardcoded in the Python files.
QA Notes: 

Task 5: Sequential Row Appending
Status: [TODO]
Worker Instructions: Target the Google Sheet: `https://docs.google.com/spreadsheets/d/1lA-_yjLMc3qDTt9sogSPQrCohNULIk5wwJYfb5wIHfc/edit?gid=0#gid=0`. Hardcode the column insertions. Apppend a single new row at the bottom of the active sheet where Column A is the Current Date (Format: YYYY-MM-DD) and Columns B through R map the 17 sanitized metrics sequentially.
QA Criteria: Ensure the script targets the exact Google Sheet URL and successfully appends all data in the correct columns (Date in A, 17 metrics in B-R).
QA Notes: 

## Phase 4: Critical Failure Alerting

Task 6: Global Exception Handling & Email Notifications
Status: [TODO]
Worker Instructions: Implement a global `try/except` block around the main execution function. If a critical failure occurs (e.g., target website returns 500, Google Sheets API authentication fails), the script must send an email alert to `jalal.chowdhury@gmail.com` detailing the Python traceback. Use Python's built-in `smtplib` with a Gmail App Password (stored as a GitHub Secret) for this notification.
QA Criteria: Simulate a critical failure and verify that an email formatted with the traceback is sent to the target address, with the password loaded from secrets.
QA Notes: 

## Phase 5: DevOps & CI/CD

Task 7: GitHub Actions Workflow Creation
Status: [TODO]
Worker Instructions: Create `.github/workflows/scraper.yml` to define the environment setup (use Python 3.11+, install requirements, and load secrets). Configure the cron job to run at 10:00 AM and 10:00 PM Eastern Time using the exact UTC cron expression `0 14,2 * * *`.
QA Criteria: Verify that the `scraper.yml` exists, installs dependencies, references required repository secrets, and exactly uses the `0 14,2 * * *` cron schedule.
QA Notes: 

## Phase 6: QA Verification Matrix

Task 8: Final Acceptance Testing Checklist
Status: [TODO]
Worker Instructions: Provide system access to QA to validate the pipeline.
QA Criteria: QA must validate the exact checklist before marking the project complete: 1) Verify data is sanitized (no letters/symbols in the numerical payload). 2) Verify a forced error on one metric results in "N/A", not a crash. 3) Verify credentials are not hardcoded in the final `.py` files.
QA Notes: 
