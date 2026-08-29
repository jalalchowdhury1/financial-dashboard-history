import os
import re
import math
import json
import time
import traceback
from datetime import datetime, timedelta, timezone
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


def fetch_with_retry(url, max_retries=2, base_delay=5, timeout=60):
    """Fetch URL with exponential backoff retry logic (handles transient HTTP errors).
    timeout=60 because the Vercel endpoints run under Fluid compute with no
    maxDuration cap: on slow-upstream days a response can legitimately take >30s,
    and a shorter client timeout fails 100% of the time regardless of retries
    (2026-07-15 outage). Bounded so the worst case stays under the 15-min CI job
    timeout even when layered under fetch_merged (see fetch_merged for the budget)."""
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

    Latency budget (keeps us under the 15-min CI job timeout): each attempt is
    fetch_with_retry (<=2 tries x 60s + 5s backoff = 125s worst); 2 attempts + gap
    ~= 252s/endpoint; all three endpoints + sheets ~= 13 min absolute worst case
    (and a fatal endpoint that exhausts its budget aborts the run early anyway).

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


def _is_na(v):
    """True for values the dashboard would render as N/A (None or ''/'N/A' strings)."""
    return v is None or (isinstance(v, str) and v.strip().upper() in ("", "N/A"))


# Four Horsemen history carried in the helper tab.
#
# Every other metric in `dashboard_lkg` is a single number, but the dashboard's
# recession-watch card is a CHART: with fewer than two points per line its
# `hasAnySeries` check fails and the entire card renders "N/A - Unavailable".
# So the one path that exists to keep the dashboard alive during a total FRED
# outage used to drop the single most decision-relevant card on it.
#
# Each line is therefore packed into ONE cell as `YYYY-MM-DD:value|...`, thinned
# to stay far inside Google Sheets' 50k-character cell limit while keeping enough
# resolution for the card's stat chips and its 12-month trend notes.
#
# Windows are per-cadence: claims is weekly, unemployment monthly, the spread
# daily, bankruptcies quarterly. Reader: financial-telegram-bot
# dashboard/lib/sheetLkg.py -> parsePackedHistory.
HORSEMEN_HISTORY = {
    # key -> (years to keep, max points after thinning)
    "claims": (5, 300),          # weekly  -> ~260 points, kept whole
    "unemployment": (10, 150),   # monthly -> ~120 points, kept whole
    "spread": (5, 300),          # daily   -> ~1250 points, thinned to ~weekly
    "bankruptcies": (30, 150),   # quarterly -> ~99 points, kept whole
}
MAX_PACKED_CHARS = 45000  # Google Sheets caps a cell at 50k


def _thin(points, max_points):
    """Down-sample evenly, always keeping the newest point (the current value)."""
    if len(points) <= max_points:
        return points
    step = math.ceil(len(points) / max_points)
    out = points[::step]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def pack_history(history, years, max_points, now=None):
    """Pack an ascending [{date,value}] series into `YYYY-MM-DD:value|...`.

    Returns "" when there is nothing usable, so the caller can omit the key
    entirely (same convention as every other N/A metric in this tab).
    """
    if not isinstance(history, list):
        return ""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d")

    pts = []
    for p in history:
        if not isinstance(p, dict):
            continue
        date = p.get("date")
        value = p.get("value")
        if not isinstance(date, str) or len(date) != 10 or date < cutoff:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        pts.append((date, value))
    if len(pts) < 2:
        return ""

    pts.sort(key=lambda t: t[0])
    # Trim trailing zeros so a float like 4.2 doesn't serialize as 4.2000000001.
    packed = "|".join(f"{d}:{round(v, 6):g}" for d, v in _thin(pts, max_points))
    if len(packed) > MAX_PACKED_CHARS:
        # Defensive: halve resolution until it fits rather than writing a
        # truncated cell that would deserialize into a corrupt final point.
        return pack_history(history, years, max_points // 2, now)
    return packed


def build_horsemen_pairs(fred, now=None):
    """key->value rows for the Four Horsemen block. Never raises; a missing or
    malformed line is simply omitted (the reader tolerates any subset)."""
    fred = fred if isinstance(fred, dict) else {}
    hm = fred.get("horsemen")
    hm = hm if isinstance(hm, dict) else {}
    yc = fred.get("yieldCurve")
    yc = yc if isinstance(yc, dict) else {}

    pairs = []

    for key in ("claims", "unemployment", "bankruptcies"):
        m = hm.get(key)
        m = m if isinstance(m, dict) else {}
        cur = m.get("current")
        years, max_points = HORSEMEN_HISTORY[key]
        packed = pack_history(m.get("history"), years, max_points, now)
        if _is_na(cur) and not packed:
            continue
        if not _is_na(cur):
            pairs.append([f"horsemen.{key}.value", cur])
        if m.get("asOf"):
            pairs.append([f"horsemen.{key}.asOf", m["asOf"]])
        if packed:
            pairs.append([f"horsemen.{key}.history", packed])

    # Bankruptcies extras the stat chip reads (YoY arrow + rising/falling badge).
    bk = hm.get("bankruptcies")
    bk = bk if isinstance(bk, dict) else {}
    for field in ("total", "changePct", "status"):
        if not _is_na(bk.get(field)):
            pairs.append([f"horsemen.bankruptcies.{field}", bk[field]])

    # The spread line rides on yieldCurve (whose current/asOf are already written
    # by build_lkg_pairs) — only its history is needed here.
    years, max_points = HORSEMEN_HISTORY["spread"]
    packed = pack_history(yc.get("history"), years, max_points, now)
    if packed:
        pairs.append(["yieldCurve.history", packed])

    return pairs


def build_lkg_pairs(fred, updated_at):
    """Self-describing key->value snapshot of the dashboard's FRED tiles, for the
    dashboard's LAST-RESORT fallback (read only when live FRED AND its /tmp
    last-known-good are both gone). Values + asOf (+ status/bullish/label) only — NO
    chart history. Metrics with null/'N/A' values are OMITTED. The key names are a
    contract with the dashboard reader (financial-telegram-bot lib/sheetLkg.js):
    DON'T rename keys without updating that module.

    Built from the rich (already null-recovered) /api/fred dict, NOT from Sheet1's
    flattened row, so it carries asOf/status the flat columns drop."""
    fred = fred if isinstance(fred, dict) else {}
    ind = fred.get("indicators")
    ind = ind if isinstance(ind, dict) else {}
    chk = fred.get("checklist")
    chk = chk if isinstance(chk, dict) else {}

    pairs = [["updated_at", updated_at]]
    if not _is_na(fred.get("peRatio")):
        pairs.append(["peRatio", fred.get("peRatio")])

    for tag in ("yieldCurve", "profitMargin"):
        obj = fred.get(tag)
        obj = obj if isinstance(obj, dict) else {}
        cur = cur_or_hist(obj)
        if not _is_na(cur):
            pairs.append([f"{tag}.current", cur])
            if obj.get("asOf"):
                pairs.append([f"{tag}.asOf", obj["asOf"]])

    for k in ("sahmRule", "sentiment", "claims", "creditSpread", "realYields", "copperGold"):
        m = ind.get(k)
        m = m if isinstance(m, dict) else {}
        if _is_na(m.get("value")):
            continue
        pairs.append([f"indicators.{k}.value", m["value"]])
        if m.get("asOf"):
            pairs.append([f"indicators.{k}.asOf", m["asOf"]])
        if m.get("status"):
            pairs.append([f"indicators.{k}.status", m["status"]])

    for k in ("nfci", "m2", "retail", "housing", "indpro", "jolts", "durable", "savings"):
        m = chk.get(k)
        m = m if isinstance(m, dict) else {}
        if _is_na(m.get("value")):
            continue
        pairs.append([f"checklist.{k}.value", m["value"]])
        if m.get("asOf"):
            pairs.append([f"checklist.{k}.asOf", m["asOf"]])
        if m.get("status"):
            pairs.append([f"checklist.{k}.status", m["status"]])
        if isinstance(m.get("bullish"), bool):
            pairs.append([f"checklist.{k}.bullish", "true" if m["bullish"] else "false"])
        if m.get("label"):
            pairs.append([f"checklist.{k}.label", m["label"]])

    # Four Horsemen (values + packed chart history) — guarded so a shape change
    # upstream can never cost us the rest of the snapshot.
    try:
        pairs.extend(build_horsemen_pairs(fred))
    except Exception as e:
        print(f"WARN: horsemen LKG pairs skipped: {e}")

    return pairs


def write_helper_tab(doc, pairs, title="dashboard_lkg"):
    """Get-or-create the helper worksheet, then clear + rewrite it. NEVER deletes the
    tab (its gid is referenced by the dashboard's export URL), so the gid stays stable."""
    try:
        ws = gs_call(doc.worksheet, title)
    except gspread.WorksheetNotFound:
        ws = gs_call(doc.add_worksheet, title=title, rows=max(100, len(pairs) + 10), cols=2)
    gs_call(ws.clear)
    gs_call(ws.update, values=[["key", "value"]] + [[k, v] for k, v in pairs], range_name="A1")


def gs_call(fn, *args, max_retries=4, base_delay=5, **kwargs):
    """Sheets-side counterpart of fetch_with_retry: run one gspread call, retrying
    transient Google API errors (408/429/5xx — e.g. the 2026-08-20 "[503] The service
    is currently unavailable" that killed a run) with bounded exponential backoff.
    Non-transient errors (403 permission, 404, bad request) raise immediately, as do
    non-APIError exceptions. Worst case adds 5+10+20 = 35s per call site, comfortably
    inside the 15-min CI job timeout (see the latency budget in AGENTS.md)."""
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            code = getattr(e, "code", None)
            if code is None:
                code = getattr(getattr(e, "response", None), "status_code", None)
            transient = code in (408, 429) or (isinstance(code, int) and code >= 500)
            if not transient or attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"Sheets API error {code} (attempt {attempt + 1}/{max_retries}): {e}")
            print(f"Retrying in {delay} seconds...")
            time.sleep(delay)


def current_slot_window(now=None):
    """Return (window_start, window_end, label) for the ~12h scrape slot `now` falls
    in. Slots are anchored at the two cron times (02:00 and 14:00 UTC); the boundary
    between them sits at the midpoint (08:00 and 20:00 UTC) so a run firing a little
    early/late (cron jitter, or an EventBridge dispatch landing near-but-not-exactly
    on the minute) still lands in the same bucket as the "official" run for that slot.
    This window IS the de-dupe unit ("did the 02:00 or 14:00 slot already run"), not
    the calendar day -- two rows sharing today's date is the NORMAL, correct outcome
    (one per slot); it's two rows in the SAME slot that would be the bug."""
    now = now or datetime.now(timezone.utc)
    hour = now.hour
    if hour < 8:
        end = now.replace(hour=8, minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=12)
        label = f"{now.date().isoformat()}-AM"
    elif hour < 20:
        start = now.replace(hour=8, minute=0, second=0, microsecond=0)
        end = now.replace(hour=20, minute=0, second=0, microsecond=0)
        label = f"{now.date().isoformat()}-PM"
    else:
        start = now.replace(hour=20, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=12)
        label = f"{(now.date() + timedelta(days=1)).isoformat()}-AM"
    return start, end, label


def _github_api_get(url, params, headers, attempts=2, timeout=15):
    """Small retry wrapper (mirrors fetch_with_retry's shape) for the one dedupe-guard
    call to the GitHub REST API. Only smooths over a transient network blip -- if both
    attempts fail the exception propagates (see already_ran_this_slot's docstring for
    why that's deliberate)."""
    last_err = None
    for i in range(attempts):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(3)
    raise RuntimeError(f"GitHub API request failed after {attempts} attempts: {last_err}")


def already_ran_this_slot(now=None):
    """Dedupe guard: has a successful run of THIS workflow already completed inside
    the current ~12h scrape slot (see current_slot_window)?

    WHY GITHUB RUN-HISTORY AND NOT THE SHEET: the truest "did this slot already run"
    signal would be the sheet's own latest row, but Sheet1's date column (A) is
    date-only ("YYYY-MM-DD", written by datetime.now().strftime -- see main()) with no
    time component, and this scraper legitimately runs TWICE on the same calendar date
    (02:00 and 14:00 UTC), so both slots already write the identical date string. Reading
    the sheet can't tell those two slots apart without adding a new timestamp column to
    Sheet1 -- and Sheet1's columns are read by fixed position from financial-telegram-bot
    (dashboard/app/api/history/route.js fetches the whole CSV; dashboard/lib/marks.js
    SHEET_METRICS indexes it by a hardcoded 1-based column number per metric), so a schema
    change here is a riskier, cross-repo change than this guard needs. So this uses the
    fallback AGENTS.md calls out: GitHub's own run history for this workflow, which needs
    no schema change and no new secret (GITHUB_TOKEN is already minted per-run).

    WHY A GUARD ERROR RAISES INSTEAD OF FAILING OPEN OR CLOSED: this endpoint's history
    is append-only and feeds cadence-sensitive downstream logic (fresh-print marks
    diff consecutive rows; dashboard_lkg is a last-resort fallback) -- a SILENT duplicate
    row corrupts that cadence in a way nothing currently detects, which is worse than a
    visibly-failed (red) CI run Jalal can just re-dispatch. Silently skipping instead
    would be just as bad in the other direction: it would look identical to "this slot
    correctly already ran," masking a real GitHub-API problem behind a green check. So
    when this can't be determined, it does neither silently -- it raises, main()'s
    existing critical-error handling prints the traceback and re-raises, and NOTHING is
    appended. This matches the rest of the file's own convention (fetch_merged: "better
    to fail the run loudly than append a junk row").
    """
    repo = os.environ["GITHUB_REPOSITORY"]
    this_run_id = os.environ["GITHUB_RUN_ID"]
    token = os.environ["GH_GUARD_TOKEN"]
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    start, end, label = current_slot_window(now)

    url = f"{api_base}/repos/{repo}/actions/workflows/scraper.yml/runs"
    params = {
        "status": "success",
        "created": f">={start.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "per_page": 20,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = _github_api_get(url, params, headers)

    for run in data.get("workflow_runs", []):
        if str(run.get("id")) == str(this_run_id):
            continue  # never compare a run against itself
        created = run.get("created_at")
        if not created:
            continue
        try:
            created_dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start <= created_dt < end:
            print(f"INFO: dedupe guard: slot {label} already has a successful run "
                  f"(run {run.get('id')}, created {created}) -- skipping this run.")
            return True

    print(f"INFO: dedupe guard: no prior successful run found in slot {label}; proceeding.")
    return False


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
        if os.getenv("GITHUB_ACTIONS"):
            # One Clock: this workflow can now also be triggered by AWS EventBridge
            # via workflow_dispatch, alongside the existing cron. Skip the dedupe
            # guard only for a HUMAN manually re-running it (workflow_dispatch's
            # `force` input, unset/false by default -- EventBridge never sets it, same
            # as keepalive.yml's existing `force` input in this repo).
            if os.getenv("SCRAPER_FORCE", "").strip().lower() == "true":
                print("INFO: SCRAPER_FORCE=true (manual override) -- skipping the dedupe guard.")
            elif already_ran_this_slot():
                print("INFO: dedupe guard: this scrape slot already completed "
                      "successfully elsewhere; skipping the fetch/append to avoid "
                      "a duplicate row.")
                return

        # Layer 1: multi-fetch + merge (transient-null recovery happens inside).
        fred = get_fred_data()
        market_extra = get_market_extra_data()
        sheets = get_sheets_data()  # non-fatal; {} on failure

        # Layer 2: current -> history fallback happens inside extract_metrics.
        metrics = extract_metrics(fred, market_extra, sheets)

        gc = authenticate_gspread()
        sheet_id = "1lA-_yjLMc3qDTt9sogSPQrCohNULIk5wwJYfb5wIHfc"
        doc = gs_call(gc.open_by_key, sheet_id)
        sheet = gs_call(lambda: doc.sheet1)  # property fetches sheet metadata

        # Layer 3: carry-forward backstop from the sheet's last known values.
        all_values = gs_call(sheet.get_all_values)
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

        gs_call(sheet.append_row, row)
        print(f"Data successfully appended to Google Sheet. "
              f"({len(metrics)} metrics; carried_forward={len(carried)}, na_remaining={na_left})")

        # Helper tab for the dashboard's last-resort fallback (see build_lkg_pairs).
        # Non-fatal: a failure here must never break the core history append.
        try:
            updated_at = (fred.get("_meta", {}) or {}).get("fetchedAt") or datetime.now().isoformat()
            pairs = build_lkg_pairs(fred, updated_at)
            write_helper_tab(doc, pairs)
            print(f"Helper tab 'dashboard_lkg' updated ({len(pairs)} rows).")
        except Exception as e:
            print(f"WARN: helper tab update failed (non-fatal): {e}")

    except Exception:
        tb = traceback.format_exc()
        print("Critical error occurred:")
        print(tb)
        # Re-raise so github actions registers the failure
        raise


if __name__ == "__main__":
    main()
