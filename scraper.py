import os
import json
import traceback
from datetime import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

def get_fred_data():
    url = "https://financial-telegram-bot-beryl.vercel.app/api/fred"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()

def extract_metrics(fred):
    metrics = []
    
    def safe_get(p_func):
        try:
            return p_func()
        except Exception:
            return "N/A"

    # 1. Yield Curve (10Y-2Y)
    metrics.append(safe_get(lambda: float(fred['yieldCurve']['current'])))
    # 2. Profit Margin
    metrics.append(safe_get(lambda: float(fred['profitMargin']['current'])))
    # 3. Sahm Rule
    metrics.append(safe_get(lambda: float(fred['indicators']['sahmRule']['value'])))
    # 4. Consumer Sentiment
    metrics.append(safe_get(lambda: float(fred['indicators']['sentiment']['value'])))
    # 5. Initial Claims (4wk)
    metrics.append(safe_get(lambda: int(float(fred['indicators']['claims']['value']) * 1000)))
    # 6. BBB Credit Spread
    metrics.append(safe_get(lambda: float(fred['indicators']['creditSpread']['value'])))
    # 7. Real Yields (10Y TIPS)
    metrics.append(safe_get(lambda: float(fred['indicators']['realYields']['value'])))
    # 8. Leading Economic Index
    metrics.append(safe_get(lambda: float(fred['indicators']['lei']['value'])))
    # 9. Market Valuation (P/E)
    metrics.append(safe_get(lambda: float(fred['peRatio'])))
    # 10. System Tightness
    metrics.append(safe_get(lambda: float(fred['checklist']['nfci']['value'])))
    # 11. M2 Money Supply
    metrics.append(safe_get(lambda: float(fred['checklist']['m2']['value'])))
    # 12. Retail Sales (3mo)
    metrics.append(safe_get(lambda: float(fred['checklist']['retail']['value'])))
    # 13. Housing Starts
    metrics.append(safe_get(lambda: int(float(fred['checklist']['housing']['value']) * 1000)))
    # 14. Industrial Production
    metrics.append(safe_get(lambda: float(fred['checklist']['indpro']['value'])))
    # 15. Job Openings (JOLTS)
    metrics.append(safe_get(lambda: int(float(fred['checklist']['jolts']['value']) * 1000)))
    # 16. Durable Goods Orders
    metrics.append(safe_get(lambda: float(fred['checklist']['durable']['value'])))
    # 17. Savings Rate
    metrics.append(safe_get(lambda: float(fred['checklist']['savings']['value'])))

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
        metrics = extract_metrics(fred)
        
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
