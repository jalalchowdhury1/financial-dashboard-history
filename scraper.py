import os
import re
import json
import time
import traceback
from datetime import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

# Column index (position in the appended row, 0-based) of metrics that are
# intentionally retired: kept for history but no longer published upstream.
# These are written BLANK going forward (not "N/A") and are never carried forward.
#   8 = column I = Leading Economic Index (LEI) — FRED discontinued the series;
#       Copper/Gold Ratio (col AG) replaced it on the dashboard. No free source exists.
RETIRED_COLS = {8}
# Column index of the one text-valued metric (VIX Fear/Greed label e.g. "GREED19"),
# so the carry-forward backstop doesn't run it through numeric cleaning.
TEXT_COLS = {38}


def fetch_with_retry(url, max_retries=2, base_delay=5, timeout=30):
    """Fetch URL with exponential backoff retry logic (handles transient HTTP errors).
    Bounded so the worst case stays well under the 15-min CI job timeout even when
    layered under fetch_merged (see fetch_merged for the latency budget)."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
            print(f"Retrying in {delay} seconds...")
            time.sleep(delay)


def _merge_prefer_nonnull(a, b):
    """Deep-merge two API responses, keeping the FIRST non-null/non-empty value for
    each leaf. The dashboard's sources fail independently per request (e.g. gold can
    be null on one call and present on the next), so merging a few fetches recovers
    most momentarily-null metrics without any staleness."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, vb in b.items():
            out[k] = _merge_prefer_nonnull(out[k], vb) if k in out else vb
        return out
    # scalar / list: keep `a` unless it is missing/empty, in which case take `b`
    if a is None or a == "" or (isinstance(a, list) and not a):
        return b
    return a


def fetch_merged(url, attempts=2, gap=2, fatal=True):
    """Fetch an endpoint several times and merge the responses, preferring non-null
    values. This is the first line of defence against transient N/A: a metric that is
    momentarily null on one fetch is filled from another fetch.

    Latency budget (keeps us well under the 15-min CI job timeout): each attempt is
    fetch_with_retry (<=2 tries x 30s + 5s backoff = 65s worst); 2 attempts + gap
    ~= 132s/endpoint; the two fatal endpoints + sheets ~= 400s worst case.

    fatal=True  -> raise if EVERY attempt failed (used for the core fred/market-extra
                   feeds: better to fail the run loudly than append a junk row).
    fatal=False -> return {} if everything failed (used for the auxiliary /api/sheets).
    """
    merged = None
    last_err = None
    for i in range(attempts):
        try:
            resp = fetch_with_retry(url)
        except Exception as e:
            last_err = e
            resp = None
        if isinstance(resp, dict):
            merged = resp if merged is None else _merge_prefer_nonnull(merged, resp)
        if i < attempts - 1:
            time.sleep(gap)
    if merged is None:
        if fatal:
            raise RuntimeError(f"All {attempts} fetch attempts failed for {url}: {last_err}")
        print(f"WARN: all {attempts} fetches failed for {url}; continuing empty: {last_err}")
        return {}
    return merged


def get_fred_data():
    return fetch_merged("https://financial-telegram-bot-beryl.vercel.app/api/fred", fatal=True)


def get_market_extra_data():
    return fetch_merged("https://financial-telegram-bot-beryl.vercel.app/api/market-extra", fatal=True)


def get_sheets_data():
    """Auxiliary 'frontrunner card' data (AAII DIFF, VIX). NON-FATAL by design: an
    outage here yields N/A for those columns only and never blocks the core history."""
    return fetch_merged("https://financial-telegram-bot-beryl.vercel.app/api/sheets", fatal=False)


def clean_numeric_string(text):
    """Parse formatted strings like '9.55%', '6.5M', '123K' into clean float/int.
    Returns "N/A" when the input can't be parsed (None, missing, junk)."""
    if not isinstance(text, str):
        # Already a number (or None): coerce, else N/A
        try:
            val = float(text)
            if val == int(val):
                return int(val)
            return round(val, 2)
        except (ValueError, TypeError):
            return "N/A"

    text = text.strip()

    # Detect shorthand multipliers
    multiplier = 1
    if 'M' in text.upper():
        multiplier = 1_000_000
        text = re.sub(r'[Mm]', '', text)
    elif 'K' in text.upper():
        multiplier = 1_000
        text = re.sub(r'[Kk]', '', text)

    # Strip all non-numeric characters except decimal point and negative sign
    cleaned = re.sub(r'[^\d.\-]', '', text)

    try:
        value = float(cleaned) * multiplier
        if value == int(value):
            return int(value)
        return round(value, 2)
    except (ValueError, TypeError):
        return "N/A"


def cur_or_hist(obj):
    """Return a metric's `current` value, falling back to the last point of its
    `history` array when `current` is null/missing. History points use either a
    'price' key (market-extra) or a 'value' key (fred) — both are handled. This
    recovers transient `current=null` blips with the real last-published value."""
    if not isinstance(obj, dict):
        return None
    v = obj.get('current')
    if v is not None:
        return v
    hist = obj.get('history')
    if isinstance(hist, list) and hist:
        last = hist[-1]
        if isinstance(last, dict):
            p = last.get('price')
            return p if p is not None else last.get('value')
    return None


def extract_metrics(fred, market_extra, sheets=None):
    metrics = []
    fred = fred if isinstance(fred, dict) else {}
    # Section guards: isinstance (not `or {}`) so a TRUTHY non-dict (e.g. a stray
    # error string the merge layer could synthesize) degrades to N/A, never crashes.
    indicators = fred.get('indicators')
    indicators = indicators if isinstance(indicators, dict) else {}
    checklist = fred.get('checklist')
    checklist = checklist if isinstance(checklist, dict) else {}

    def safe_get(p_func):
        try:
            return p_func()
        except Exception:
            return "N/A"

    # 1. Yield Curve (10Y-2Y)   [current -> history fallback]
    val = cur_or_hist(fred.get('yieldCurve'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 2. Profit Margin          [current -> history fallback]
    val = cur_or_hist(fred.get('profitMargin'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 3. Sahm Rule
    val = indicators.get('sahmRule', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 4. Consumer Sentiment
    val = indicators.get('sentiment', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 5. Initial Claims (4wk) - value is already in thousands (e.g., 215 = 215K)
    val = indicators.get('claims', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 6. BBB Credit Spread
    val = indicators.get('creditSpread', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 7. Real Yields (10Y TIPS)
    val = indicators.get('realYields', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 8. Leading Economic Index (RETIRED — see RETIRED_COLS; blanked in main())
    val = indicators.get('lei', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 9. Market Valuation (P/E)
    val = fred.get('peRatio')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 10. System Tightness
    val = checklist.get('nfci', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 11. M2 Money Supply
    val = checklist.get('m2', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 12. Retail Sales (3mo)
    val = checklist.get('retail', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 13. Housing Starts - value is already in thousands
    val = checklist.get('housing', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 14. Industrial Production
    val = checklist.get('indpro', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 15. Job Openings (JOLTS) - value is already in thousands (e.g., 6946 = 6.946M)
    val = checklist.get('jolts', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 16. Durable Goods Orders
    val = checklist.get('durable', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 17. Savings Rate
    val = checklist.get('savings', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))

    # --- METRICS FROM MARKET-EXTRA (current -> history fallback via cur_or_hist) ---
    market_extra = market_extra if isinstance(market_extra, dict) else {}
    realEstate = market_extra.get('realEstate')
    realEstate = realEstate if isinstance(realEstate, dict) else {}
    rates = market_extra.get('rates')
    rates = rates if isinstance(rates, dict) else {}
    fx = market_extra.get('fx')
    fx = fx if isinstance(fx, dict) else {}
    commodities = market_extra.get('commodities')
    commodities = commodities if isinstance(commodities, dict) else {}

    # 18. ZRI US Median Monthly Rent
    val = cur_or_hist(realEstate.get('rentIndex'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 19. MTGPMT Estimated Monthly Mortgage
    val = cur_or_hist(realEstate.get('mortgagePayment'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 20. MORT30 30-Year Fixed Mortgage Rate
    val = cur_or_hist(rates.get('mortgageRate'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 21. TNX 10-Year Treasury Yield
    val = cur_or_hist(rates.get('tnx'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 22. T2Y 2-Year Treasury Yield
    val = cur_or_hist(rates.get('t2y'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 23. DXY US Dollar Index
    val = cur_or_hist(fx.get('dxy'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 24. CL Crude Oil WTI
    val = cur_or_hist(commodities.get('cl'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 25. USD/CAD
    val = cur_or_hist(fx.get('usdcad'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 26. USD/INR
    val = cur_or_hist(fx.get('usdinr'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 27. USD/BDT
    val = cur_or_hist(fx.get('usdbdt'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 28. INR/BDT
    val = cur_or_hist(fx.get('inrbdt'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 29. CAD/INR
    val = cur_or_hist(fx.get('cadinr'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 30. GOLD
    val = cur_or_hist(commodities.get('gc'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 31. BTC
    val = cur_or_hist(commodities.get('btc'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))

    # --- METRICS ADDED 2026-06-04 (cols AG..AM; appended at the end to preserve all
    #     existing column positions and historical data) ---

    # 32. Copper/Gold Ratio (replaced LEI on the dashboard) - from /api/fred
    #     No history array upstream; when its copper/gold source is down it goes null
    #     and the carry-forward backstop in main() supplies the last known value.
    val = indicators.get('copperGold', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 33. ATNHPI US House Price Index (index level) - from /api/market-extra
    val = cur_or_hist(realEstate.get('atnhpi'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 34. CAD/BDT Canadian Dollar to Bangladeshi Taka - from /api/market-extra
    val = cur_or_hist(fx.get('cadbdt'))
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))

    # --- AAII DIFF + VIX come from the secondary /api/sheets endpoint ---
    sheets = sheets if isinstance(sheets, dict) else {}
    vix = sheets.get('VIX')
    vix = vix if isinstance(vix, dict) else {}  # guard: a string VIX would crash .get()

    # 35. AAII DIFF (e.g. "0.70%" -> 0.7) - from /api/sheets
    val = sheets.get('AAIIDiff')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 36. VIX (Current) (e.g. "15.37" -> 15.37) - from /api/sheets
    val = vix.get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 37. VIX (3M) (e.g. "19.25" -> 19.25) - from /api/sheets
    val = vix.get('threeMonth')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 38. VIX Fear/Greed (e.g. "GREED19") - text label kept verbatim, NOT numeric
    val = vix.get('fearGreed')
    metrics.append(val if isinstance(val, str) and val.strip() else "N/A")

    return metrics


def build_last_known(existing_rows):
    """From the existing data rows (excluding the header), return {col_index:
    last_good_value} — the most recent non-empty, non-"N/A" cell per column. Used as
    the final carry-forward backstop so a momentarily-unavailable metric reuses its
    last known value instead of writing N/A."""
    last_known = {}
    for row in existing_rows:
        for idx, cell in enumerate(row):
            if idx == 0:
                continue  # date column
            s = str(cell).strip()
            if s and s.upper() != "N/A":
                last_known[idx] = cell
    return last_known


def apply_fallbacks(metrics, last_known):
    """Layer 3: replace any remaining "N/A" with the last known value from the sheet,
    EXCEPT retired columns (blanked). Returns (metrics, carried_cols, retired_cols)."""
    carried, retired = [], []
    for i, val in enumerate(metrics):
        col_idx = i + 1  # metrics[0] -> row column index 1 (B)
        if col_idx in RETIRED_COLS:
            if val == "N/A":
                metrics[i] = ""  # clean blank instead of noisy N/A; never carried
                retired.append(col_idx)
            continue
        if val == "N/A" and col_idx in last_known:
            cf = last_known[col_idx]
            if col_idx in TEXT_COLS:
                metrics[i] = cf
            else:
                cleaned = clean_numeric_string(cf)
                metrics[i] = cleaned if cleaned != "N/A" else cf
            carried.append(col_idx)
    return metrics, carried, retired


def authenticate_gspread():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if os.getenv("GITHUB_ACTIONS"):
        creds_json = os.getenv("GOOGLE_SHEETS_CREDS")
        if not creds_json:
            raise ValueError("GOOGLE_SHEETS_CREDS environment variable missing")
        creds_dict = json.loads(creds_json)
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        # Local Auth
        credentials = Credentials.from_service_account_file("finance-dashboard-history-df2b4bf11659.json", scopes=scopes)

    return gspread.authorize(credentials)


def main():
    try:
        # Layer 1: multi-fetch + merge (transient-null recovery happens inside).
        fred = get_fred_data()
        market_extra = get_market_extra_data()
        sheets = get_sheets_data()  # non-fatal; {} on failure

        # Layer 2: current -> history fallback happens inside extract_metrics.
        metrics = extract_metrics(fred, market_extra, sheets)

        gc = authenticate_gspread()
        sheet_id = "1lA-_yjLMc3qDTt9sogSPQrCohNULIk5wwJYfb5wIHfc"
        doc = gc.open_by_key(sheet_id)
        sheet = doc.sheet1

        # Layer 3: carry-forward backstop from the sheet's last known values.
        all_values = sheet.get_all_values()
        existing_rows = all_values[1:] if len(all_values) > 1 else []
        last_known = build_last_known(existing_rows)
        metrics, carried, retired = apply_fallbacks(metrics, last_known)

        na_left = sum(1 for m in metrics if m == "N/A")
        if carried:
            print(f"INFO: carried forward last-known values for column indices {carried}")
        if retired:
            print(f"INFO: blanked retired column indices {retired} (e.g. LEI)")
        if na_left:
            print(f"WARN: {na_left} metric(s) still N/A after all fallbacks (no prior value to use)")

        current_date = datetime.now().strftime("%Y-%m-%d")
        row = [current_date] + metrics

        sheet.append_row(row)
        print(f"Data successfully appended to Google Sheet. "
              f"({len(metrics)} metrics; carried_forward={len(carried)}, na_remaining={na_left})")

    except Exception:
        tb = traceback.format_exc()
        print("Critical error occurred:")
        print(tb)
        # Re-raise so github actions registers the failure
        raise


if __name__ == "__main__":
    main()
