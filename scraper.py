import os
import json
import time
import traceback
from datetime import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

def fetch_with_retry(url, max_retries=3, base_delay=5):
    """Fetch URL with exponential backoff retry logic."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
            print(f"Retrying in {delay} seconds...")
            time.sleep(delay)

def get_fred_data():
    url = "https://financial-telegram-bot-beryl.vercel.app/api/fred"
    return fetch_with_retry(url)

def get_market_extra_data():
    url = "https://financial-telegram-bot-beryl.vercel.app/api/market-extra"
    return fetch_with_retry(url)

def extract_metrics(fred, market_extra):
    metrics = []
    
    def safe_get(p_func):
        try:
            return p_func()
        except Exception:
            return "N/A"

    def clean_numeric_string(text):
        """Parse formatted strings like '9.55%', '6.5M', '123K' into clean float/int."""
        import re
        if not isinstance(text, str):
            # Already a number, check if it's a whole number
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
            value = float(cleaned)
            value = value * multiplier
            # Return int if whole number, else round to 2 decimals
            if value == int(value):
                return int(value)
            return round(value, 2)
        except (ValueError, TypeError):
            return "N/A"

    # 1. Yield Curve (10Y-2Y)
    val = fred.get('yieldCurve', {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 2. Profit Margin
    val = fred.get('profitMargin', {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 3. Sahm Rule
    val = fred.get('indicators', {}).get('sahmRule', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 4. Consumer Sentiment
    val = fred.get('indicators', {}).get('sentiment', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 5. Initial Claims (4wk) - value is already in thousands (e.g., 215 = 215K)
    val = fred.get('indicators', {}).get('claims', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 6. BBB Credit Spread
    val = fred.get('indicators', {}).get('creditSpread', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 7. Real Yields (10Y TIPS)
    val = fred.get('indicators', {}).get('realYields', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 8. Leading Economic Index
    val = fred.get('indicators', {}).get('lei', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 9. Market Valuation (P/E)
    val = fred.get('peRatio')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 10. System Tightness
    val = fred.get('checklist', {}).get('nfci', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 11. M2 Money Supply
    val = fred.get('checklist', {}).get('m2', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 12. Retail Sales (3mo)
    val = fred.get('checklist', {}).get('retail', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 13. Housing Starts - value is already in thousands
    val = fred.get('checklist', {}).get('housing', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 14. Industrial Production
    val = fred.get('checklist', {}).get('indpro', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 15. Job Openings (JOLTS) - value is already in thousands (e.g., 6946 = 6.946M)
    val = fred.get('checklist', {}).get('jolts', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 16. Durable Goods Orders
    val = fred.get('checklist', {}).get('durable', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 17. Savings Rate
    val = fred.get('checklist', {}).get('savings', {}).get('value')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))

    # --- NEW METRICS FROM MARKET-EXTRA ---
    market_extra = market_extra or {}
    realEstate = market_extra.get('realEstate') or {}
    rates = market_extra.get('rates') or {}
    fx = market_extra.get('fx') or {}
    commodities = market_extra.get('commodities') or {}

    # 18. ZRI US Median Monthly Rent
    val = (realEstate.get('rentIndex') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 19. MTGPMT Estimated Monthly Mortgage
    val = (realEstate.get('mortgagePayment') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 20. MORT30 30-Year Fixed Mortgage Rate
    val = (rates.get('mortgageRate') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 21. TNX 10-Year Treasury Yield
    val = (rates.get('tnx') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 22. T2Y 2-Year Treasury Yield
    val = (rates.get('t2y') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 23. DXY US Dollar Index
    val = (fx.get('dxy') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 24. CL Crude Oil WTI
    val = (commodities.get('cl') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 25. USD/CAD
    val = (fx.get('usdcad') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 26. USD/INR
    val = (fx.get('usdinr') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 27. USD/BDT
    val = (fx.get('usdbdt') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 28. INR/BDT
    val = (fx.get('inrbdt') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 29. CAD/INR
    val = (fx.get('cadinr') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 30. GOLD
    val = (commodities.get('gc') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))
    # 31. BTC
    val = (commodities.get('btc') or {}).get('current')
    metrics.append(safe_get(lambda v=val: clean_numeric_string(v)))

    return metrics

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
        fred = get_fred_data()
        market_extra = get_market_extra_data()
        metrics = extract_metrics(fred, market_extra)
        
        gc = authenticate_gspread()
        sheet_id = "1lA-_yjLMc3qDTt9sogSPQrCohNULIk5wwJYfb5wIHfc"
        doc = gc.open_by_key(sheet_id)
        sheet = doc.sheet1
        
        current_date = datetime.now().strftime("%Y-%m-%d")
        row = [current_date] + metrics
        
        sheet.append_row(row)
        print("Data successfully appended to Google Sheet.")
    
    except Exception as e:
        tb = traceback.format_exc()
        print("Critical error occurred:")
        print(tb)
        # Re-raise so github actions registers the failure
        raise

if __name__ == "__main__":
    main()
