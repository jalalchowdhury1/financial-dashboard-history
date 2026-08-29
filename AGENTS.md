# AGENTS.md — financial-dashboard-history

> **This is the single source of truth for anyone (human or AI) touching this repo.**
> Read it fully before changing code. It replaces the old scattered docs
> (`blueprint.md`, `api_inspection.md`, `qa_matrix.md`) — those were point-in-time
> planning/QA artifacts and have been deleted (their still-accurate content is folded
> in below). If something here is wrong, fix *this* file.

This repo is a tiny, single-purpose scraper. It snapshots the sister dashboard's live
market metrics into a Google Sheet twice a day so there is a historical time series.
The whole thing is **one file** (`scraper.py`) plus two GitHub Actions workflows.

---

## 1. What this is

A scheduled **GitHub Action** runs `python scraper.py`. The script:
1. Fetches three JSON endpoints from the live **financial-telegram-bot** dashboard
   (the sister project, deployed on Vercel at
   `https://financial-telegram-bot-beryl.vercel.app`).
2. Extracts **38 metrics** in a fixed order, sanitizes each to a clean number (or a
   verbatim text label for one column), and applies a 3-layer N/A-resilience strategy.
3. **Appends one row** (date + 38 metrics = 39 columns, A–AM) to the bottom of a Google
   Sheet via `gspread`. The sheet is **append-only by column position** — never reorder
   or delete columns.

- **Language/stack:** Python 3.11; deps `gspread`, `google-auth`, `requests`
  (`requirements.txt`). No web framework, no DB — output is the Google Sheet.
- **Trigger:** cron in `.github/workflows/scraper.yml` (workflow name *"Automated Data
  Pipeline"*), `cron: '0 14,2 * * *'` → 14:00 and 02:00 UTC. The workflow comment ("10 AM
  and 10 PM ET") is **EDT-anchored** and exact only during EDT (UTC-4, ~Mar–Nov), when
  14:00 UTC = 10:00 ET and 02:00 UTC = 22:00 ET. During **EST** (UTC-5, ~Nov–Mar) the
  cron is fixed UTC so it actually fires at **9 AM / 9 PM ET**. (As of 2026-06-08, EDT is
  in effect, so the run is at 10 AM / 10 PM ET.) Also runnable on demand via
  `workflow_dispatch`, and — as of 2026-08-29, part of the "One Clock" project — by AWS
  EventBridge dispatching that same `workflow_dispatch` event on its own schedule. See
  the dedupe guard below for why a second trigger landing in the same window is safe.
- **Output sheet:** spreadsheet id `1lA-_yjLMc3qDTt9sogSPQrCohNULIk5wwJYfb5wIHfc`, first
  tab (`doc.sheet1`, i.e. `Sheet1`). Row 1 is a header row; data rows start at row 2.
- **No README, no app, no deploy target of its own.** It just writes to the sheet.

---

## 2. Architecture / data flow

```
GitHub Actions cron (14:00 & 02:00 UTC)
        │  runs: python scraper.py
        ▼
  fetch_merged()  ──▶  GET /api/fred           (FATAL  — RuntimeError if all attempts fail)
                  ──▶  GET /api/market-extra   (FATAL)
                  ──▶  GET /api/sheets         (NON-FATAL — returns {} on failure)
        │            (each endpoint fetched 2x, responses deep-merged keeping first non-null)
        ▼
  extract_metrics()  → 38 metrics in fixed column order
        │   Layer 2 inside: cur_or_hist() falls back current → last history point
        ▼
  authenticate_gspread()  (service-account creds)
        │
        ▼
  read existing sheet rows → build_last_known() → apply_fallbacks()
        │   Layer 3: any leftover "N/A" reuses last-known-good value from the sheet
        ▼
  sheet.append_row([date] + metrics)   → one new row, columns A–AM
        │
        ▼
  build_lkg_pairs(fred) → write_helper_tab(doc)   → rewrites the `dashboard_lkg` tab
       (NON-FATAL — a failure here never blocks the core append above)
```

Before Layer 1, in CI only, a **dedupe guard** (`already_ran_this_slot`) can short-circuit
`main()` and return without fetching/appending anything at all — see below.

### Dedupe guard (added 2026-08-29, "One Clock")

The workflow now has two triggers that both dispatch it: the cron above, and AWS
EventBridge (which also calls `workflow_dispatch`, on its own independent schedule).
Nothing stops the two from landing within the same ~12h scrape slot, which — with no
guard — would **append two rows for the same slot**, corrupting the append-only history's
fixed twice-daily cadence that financial-telegram-bot's fresh-print marks
(`dashboard/lib/marks.js SHEET_METRICS`) depend on to decide what counts as "news."

- **`current_slot_window(now)`** buckets `now` into the current ~12h slot: anchors at
  02:00/14:00 UTC, boundaries at the midpoints 08:00/20:00 UTC (so a trigger firing a
  little early/late still lands in the same bucket as the "official" cron run).
  **Known bounded behavior — a >6h-late run crosses the midpoint into the NEXT slot's
  window** (measured: GitHub fired every run 6–11h late through 2026-08-29, e.g. the
  02:00 cron at 08:26). It then appends there, and that slot's on-time trigger skips.
  Net effect is one *mistimed* row, never a duplicate and never a hole — acceptable for
  a diff-consecutive-rows consumer, so this is documented rather than engineered away.
  With AWS EventBridge as the on-time primary (One Clock, 30 min ahead of each cron),
  the case only arises when AWS misses AND GitHub is >6h late on the same slot.
- **`already_ran_this_slot()`** asks the GitHub REST API whether a prior run of this
  workflow already **succeeded** inside the current slot window, ignoring its own run id.
  **Why GitHub run-history and not the sheet's own latest row** (the normally-preferred,
  no-new-infrastructure signal): column A is `datetime.now().strftime("%Y-%m-%d")` —
  **date-only, no time** — and this scraper legitimately runs twice on the same calendar
  date, so both slots already write the identical date string; telling them apart needs a
  time component the sheet doesn't carry, and adding one would touch a schema
  financial-telegram-bot reads by fixed column position (`SHEET_METRICS`, `/api/history`)
  — a riskier, cross-repo change than this guard warrants. Run-history needs no schema
  change and no new secret (`GITHUB_TOKEN` is already minted per run); it's wired via the
  job's `actions: read` permission + `secrets.GITHUB_TOKEN` passed in as `GH_GUARD_TOKEN`.
- **Failure mode: neither fail-open nor fail-closed — fail LOUD.** If the GitHub API call
  errors (after one retry), `already_ran_this_slot` **raises**, and `main()`'s existing
  critical-error handling prints the traceback and re-raises, so **nothing is appended**
  and the run goes red. This was a deliberate choice, not the default: silently proceeding
  to scrape/append risks the exact duplicate-row corruption this guard exists to prevent
  (worse here than elsewhere, because it's append-only and cadence-sensitive); silently
  skipping would look identical to "this slot correctly already ran," hiding a real
  GitHub-API problem behind a green check — this repo's own convention (`fetch_merged`)
  is "fail loudly rather than write something wrong," and a visibly-red run is something
  Jalal can just re-dispatch. See the docstring in `scraper.py` for the full reasoning.
- **Manual override:** `workflow_dispatch` gained a `force` boolean input (default
  `false`, mirrors `keepalive.yml`'s existing `force` input) that skips the guard entirely
  — for a human re-testing the scraper on demand. EventBridge's dispatch never sets it, so
  it always gets the guarded path.
- **`concurrency: {group: automated-data-pipeline, cancel-in-progress: false}`** on the
  workflow queues an overlapping trigger instead of racing it — the guard can only see
  runs that have already *finished*, so two triggers landing at the same instant would
  otherwise both pass the check before either had appended (this exact race pattern
  already double-sent a message in a sibling project). Queuing means the second run's
  guard check runs strictly after the first completes.
- A useful side effect: if the first run in a slot genuinely **fails** (not just gets
  deduped), `already_ran_this_slot` finds no `status=success` run in the window, so a
  second trigger (EventBridge or manual) in the same slot will actually retry the scrape
  rather than skip it.

### The `dashboard_lkg` helper tab (dashboard last-resort fallback)

Besides the append-only `Sheet1` time-series, each run **rewrites a second tab `dashboard_lkg`**
— a self-describing `key,value` snapshot of the dashboard's FRED tiles consumed by the
financial-telegram-bot dashboard as its **last-resort fallback** (read only when live FRED AND the
dashboard's own `/tmp` last-known-good are both gone — a total outage on a cold instance).

- `build_lkg_pairs(fred, updated_at)` builds `[key, value]` rows from the **rich `/api/fred` dict**
  (not from `Sheet1`'s flat row), carrying `value` + `asOf` (+ `status`/`bullish`/`label`); **no chart history except the
  Four Horsemen block below**; metrics with null/`"N/A"` values are **omitted**. Keys (e.g.
  `indicators.sentiment.value`, `checklist.m2.bullish`) are a **contract** with the dashboard
  reader (`financial-telegram-bot/dashboard/lib/sheetLkg.js` — `parseLkgCsv`/`reconstructFred`):
  **do not rename keys without updating that module.**
- **Four Horsemen block (added 2026-07-25, `build_horsemen_pairs` + `pack_history`).** The
  dashboard's recession-watch card is the one tile that is a **CHART**, so a value alone is
  useless to it: with fewer than 2 history points per line its `hasAnySeries` check fails and
  the whole card renders "N/A - Unavailable". Until this was added, the last-resort path
  faithfully restored every card EXCEPT the single most decision-relevant one. So this block
  is the **only** part of the tab that carries history, packed into ONE cell per line as
  `YYYY-MM-DD:value|YYYY-MM-DD:value|…` (reader: `parsePackedHistory`). Windows are
  per-cadence (`HORSEMEN_HISTORY`): claims 5y weekly, unemployment 10y monthly, spread 5y
  daily thinned to ~weekly, bankruptcies 30y quarterly — largest is ~4.7KB against Google
  Sheets' 50k-character cell limit, and `pack_history` halves resolution rather than emit a
  truncated cell (a truncated cell would deserialize into a corrupt final point). Contract
  keys: `horsemen.{claims,unemployment,bankruptcies}.{value,asOf,history}`,
  `horsemen.bankruptcies.{total,changePct,status}`, and `yieldCurve.history` (the spread line
  rides on the existing `yieldCurve.current`/`.asOf`). The whole block is wrapped in a
  try/except inside `build_lkg_pairs`, so an upstream shape change can never cost the rest of
  the snapshot.
- `write_helper_tab` does get-or-create → `clear()` → rewrite. It **never deletes** the tab, so its
  **gid stays stable** — the dashboard reads it via a fixed `export?format=csv&gid=…` URL (the gviz
  endpoint is NOT used: it merges the header row into the first data row and loses `updated_at`).
- Unlike `Sheet1`, this tab is **safe to fully overwrite** each run (it's a latest-snapshot, not a
  position-mapped append-only history). It does NOT share `Sheet1`'s column-position rules.

### The three N/A-resilience layers (the whole point of this script)
1. **Multi-fetch merge** (`fetch_merged` + `_merge_prefer_nonnull`): each endpoint is
   fetched ~2 times and the responses are deep-merged keeping the **first non-null**
   value per leaf. The dashboard's upstream sources fail independently per request
   (e.g. gold may be null on one call, present on the next), so merging recovers most
   momentary nulls with zero staleness.
2. **History fallback** (`cur_or_hist`): if a metric's `current` is null, use the last
   point of its `history` array. History points use **either** a `price` key
   (market-extra) **or** a `value` key (fred) — both are handled.
3. **Carry-forward backstop** (`build_last_known` / `apply_fallbacks`): anything still
   `"N/A"` reuses the most recent non-empty, non-`"N/A"` value already in that column of
   the sheet. Carried columns are logged each run (`INFO: carried forward …`).

---

## 3. How to run / deploy / test

### CI (the normal path)
- `.github/workflows/scraper.yml` runs `python scraper.py` on the cron above (and on
  manual dispatch). Steps: checkout → setup Python 3.11 (pip cache) → `pip install -r
  requirements.txt` → run with env `GITHUB_ACTIONS=true` and secret `GOOGLE_SHEETS_CREDS`.
  Job timeout: **15 minutes** (the fetch latency budget below is sized to stay under it).
- `.github/workflows/keepalive.yml` (workflow name *"Keepalive"*): makes a tiny empty
  commit when the repo has been idle ≥ 40 days, so GitHub doesn't auto-disable the cron
  after 60 days of no commits. Runs `17 3 1,15 * *` (03:17 UTC on the 1st & 15th); has a
  `workflow_dispatch` `force` input. Needs `contents: write` (granted only to that
  workflow). **This is the only thing that ever pushes to this repo.**
  - *Stale comment heads-up:* `keepalive.yml`'s header comment was copy-pasted from a
    sibling repo and mentions "Daily/Historical scrapers" and "Telegram." This repo has
    **neither a Daily scraper nor any Telegram integration** — it has the single
    twice-daily `scraper.py` that writes only to Google Sheets. Ignore that comment's
    wording; the workflow logic itself is correct.

### Running locally
```bash
pip install -r requirements.txt
python scraper.py
```
Locally (when `GITHUB_ACTIONS` is unset) `authenticate_gspread()` reads a service-account
key file named **`finance-dashboard-history-df2b4bf11659.json`** from the repo root. That
file is **not committed** and is covered by the repo's `.gitignore` (added 2026: excludes
`*.json`, `finance-dashboard-history-*.json`, `.env`) — still, never `git add` it
explicitly. You must supply your own key, and the service account must have edit access
to the target spreadsheet.

**Tests:** `pip install pytest && python -m pytest tests/` — pure-function unit tests for
`build_lkg_pairs`/`pack_history` (`tests/test_lkg.py`) and the dedupe guard
(`tests/test_dedupe_guard.py`, added 2026-08-29). Not wired into CI (no test workflow
exists yet); run manually before pushing a change to `scraper.py`.

### Env vars / secrets (named only — never commit values)
- `GITHUB_ACTIONS` — set to `"true"` by CI; selects the secret-based auth branch.
- `GOOGLE_SHEETS_CREDS` — GitHub Actions secret holding the **full service-account JSON**
  (parsed via `json.loads` → `Credentials.from_service_account_info`). Scope:
  `https://www.googleapis.com/auth/spreadsheets`.
- Local fallback file: `finance-dashboard-history-df2b4bf11659.json` (uncommitted secret;
  gitignored via `*.json` / `finance-dashboard-history-*.json` — still never `git add` it
  explicitly, `git add -f` would bypass the ignore).

---

## 4. Gotchas / hard rules

- **Append-only by column position.** The sheet maps `metrics[i]` → column `i+1` (B…AM)
  purely by index. **Never reorder, insert, or delete a column in the middle** — it would
  misalign every historical row. New metrics go at the **far right only**.
- **To add a metric:** append a new `metrics.append(...)` at the **end** of
  `extract_metrics()` (after the current last one), pulling from the right endpoint, and
  add the matching far-right column to the sheet header. Renumber the trailing column-set
  comments if you like, but do not move existing entries.
- **Retired columns are blanked, not removed.** `RETIRED_COLS = {8}` = column I = the
  **Leading Economic Index (LEI)**, which FRED discontinued. It is kept (history
  preserved) but written **blank** going forward (a clean `""`, never `"N/A"`, never
  carried forward). Its dashboard replacement, **Copper/Gold Ratio**, was added at the
  far right (col AG, metric 32). Do not delete col I.
- **One text column.** `TEXT_COLS = {38}` = column AM = **VIX Fear/Greed** label
  (e.g. `"GREED19"`). It is stored verbatim and is **excluded from numeric cleaning** and
  from numeric carry-forward coercion. Don't run it through `clean_numeric_string`.
- **`/api/fred` and `/api/market-extra` are FATAL; `/api/sheets` is NON-FATAL.** If either
  core feed fails all attempts, the run raises and goes red on purpose — better to fail
  loudly than append a junk row. `/api/sheets` (AAII DIFF + VIX, cols AK–AM) returns `{}`
  on failure → those columns get `"N/A"`/carry-forward but never block the core history.
- **Number parsing** (`clean_numeric_string`): strips `% ~ + < -`-style symbols and
  descriptive text, expands shorthand (`6.5M` → 6500000, `123K` → 123000), returns int
  when whole else rounds to 2 dp, and returns `"N/A"` on unparseable input. Note: JOLTS &
  Initial Claims come from the API already in thousands (e.g. `6946` = 6.946M) and are
  stored as-is (the doc note about "×1000" refers to the generic K/M shorthand, not these).
- **Latency budget (must stay < 15-min CI timeout):** `fetch_with_retry` = ≤2 tries ×60s +
  5s backoff ≈ 125s worst; `fetch_merged` = 2 attempts + 2s gap ≈ 252s/endpoint; all three
  endpoints + sheets ≈ 13 min absolute worst case (a fatal endpoint that exhausts its
  budget aborts the run early). `gs_call` (Sheets-side retry, added after the 2026-08-20
  transient 503 failure) = ≤4 tries with 5/10/20s backoff ≈ +35s worst per Sheets call —
  only reached when the Sheets API itself is erroring. Keep retry/attempt counts in this
  envelope. The per-request timeout is 60s (raised from 30s after the 2026-07-15 failure):
  the Vercel endpoints run under Fluid compute with no maxDuration cap, so slow-upstream
  days can legitimately take >30s and a shorter timeout fails every retry.
- **Sustained-outage masking:** carried-forward values are re-persisted each run, so a
  *prolonged* upstream outage (notably `copperGold` when its source is down) looks
  "frozen-but-current." Watch the run logs for repeated `carried forward` lines — the real
  fix lives upstream in the **financial-telegram-bot** dashboard repo, not here.
- **Type guards everywhere on purpose.** `extract_metrics` guards each section with
  `isinstance(..., dict)` (not `or {}`) so a *truthy non-dict* (e.g. a stray error string
  the merge layer could synthesize) degrades to `"N/A"` instead of crashing. `safe_get`
  wraps each metric extraction in try/except. Keep that defensiveness.
- **Public repo — no secrets in code.** Initial commit history note: the repo was "cleaned
  of secrets." Keep credentials in `GOOGLE_SHEETS_CREDS` (CI) or the local
  `finance-dashboard-history-df2b4bf11659.json` only. The `.gitignore` excludes `*.json` /
  `finance-dashboard-history-*.json` / `.env`, so the key can't be staged accidentally —
  still never `git add -f` it.

---

## 5. Discrepancies found (old docs vs. actual code)

The deleted docs were planning/QA artifacts and drifted from the shipped code. Corrections:
- **No email alerting exists.** `blueprint.md` Phase 4 and `qa_matrix.md` both claim a
  global handler sends a traceback email to `jalal.chowdhury@gmail.com` via `smtplib`
  with a `GMAIL_APP_PASSWORD` secret. **None of this is in `scraper.py`.** The real
  behavior: `main()`'s `except` prints the traceback and **re-raises**, so the GitHub
  Actions run goes red (that is the only failure signal). There is no `GMAIL_APP_PASSWORD`
  in the code or in `scraper.yml`; do not assume it exists.
- **`api_inspection.md` was self-corrected and wrong.** Its original conclusion ("no JSON
  API exists, must DOM-scrape with `scrapling`") was false. The dashboard **does** expose
  JSON endpoints and the scraper reads them directly. `scrapling` was never used and is not
  a dependency.
- **"17 metrics" / "columns B–R" is outdated.** Blueprint Phase 1–3 describe an early
  17-metric design (cols A–R). The code now extracts **38 metrics** (cols A–AM, 39 total):
  17 FRED/checklist + 14 market-extra + 7 added 2026-06-04 (Copper/Gold, ATNHPI, CAD/BDT,
  AAII DIFF, VIX current/3M, VIX Fear/Greed).
- **LEI handling:** older blueprint prose says col I "records N/A going forward." The code
  actually writes it **blank (`""`)**, not `"N/A"` (see `apply_fallbacks` / `RETIRED_COLS`).
- **Tab name:** docs say tab `Sheet1`; code uses `doc.sheet1` (gspread's first-worksheet
  accessor), i.e. whatever the first tab is. Spreadsheet id matches the docs exactly.

---

## 6. Known issues / open items

- **Sustained `copperGold` outage** (and any other prolonged upstream null) will show as a
  silently carried-forward (frozen) value, not an error. Fix is upstream in the dashboard.
- **DST drift:** the cron is fixed UTC (`14,2`), so the ET wall-clock time shifts by an
  hour across daylight-saving transitions. Acceptable; just be aware the comment
  ("10 AM/10 PM ET") is exact only during **EDT** (UTC-4); during **EST** (UTC-5) the run
  actually lands at **9 AM/9 PM ET**.
- **No monitoring of its own.** A failed (or correctly-deduped) run is visible only as a
  red/green Actions run — no alert. If silent-failure detection is wanted, that is unbuilt
  work. (There ARE unit tests, `tests/` — not run in CI, see §3.)

---

## 7. File / module map

- `scraper.py` — the entire pipeline. Key functions:
  - `fetch_with_retry` — single URL GET with bounded exponential-backoff retry.
  - `_merge_prefer_nonnull` / `fetch_merged` — Layer 1 multi-fetch deep-merge (fatal vs
    non-fatal).
  - `get_fred_data` / `get_market_extra_data` / `get_sheets_data` — the three endpoint
    wrappers (first two fatal, sheets non-fatal).
  - `clean_numeric_string` — symbol/shorthand-stripping numeric parser → number or `"N/A"`.
  - `cur_or_hist` — Layer 2 current→history fallback (`price` or `value` key).
  - `extract_metrics` — builds the ordered 38-metric list (the column contract).
  - `build_last_known` / `apply_fallbacks` — Layer 3 carry-forward + retired-column
    blanking; `RETIRED_COLS = {8}` (LEI), `TEXT_COLS = {38}` (VIX Fear/Greed).
  - `gs_call` — bounded retry wrapper for every gspread call (transient 408/429/5xx
    only; added after a transient Sheets 503 killed the 2026-08-20 02:49 UTC run).
  - `current_slot_window` / `already_ran_this_slot` / `_github_api_get` — the dedupe
    guard (added 2026-08-29, see "Dedupe guard" above); CI-only, gated in `main()`.
  - `authenticate_gspread` — env-aware auth (CI secret vs local JSON file).
  - `main` — runs the dedupe guard (CI only), orchestrates the layers, appends the row,
    re-raises on any error (including a guard error — see "Dedupe guard" above).
- `requirements.txt` — `gspread`, `google-auth`, `requests`.
- `tests/test_dedupe_guard.py` — unit tests for the dedupe guard (added 2026-08-29).
- `.github/workflows/scraper.yml` — the twice-daily cron pipeline ("Automated Data
  Pipeline"), `cron: 0 14,2 * * *` **+ `workflow_dispatch`** (manual, and — as of
  2026-08-29 — AWS EventBridge), 15-min timeout, secret `GOOGLE_SHEETS_CREDS`.
  `concurrency: {group: automated-data-pipeline, cancel-in-progress: false}` queues
  overlapping triggers; `workflow_dispatch.inputs.force` bypasses the dedupe guard for
  manual testing.
- `.github/workflows/keepalive.yml` — empty-commit keepalive (≥40-day idle guard) so the
  cron isn't auto-disabled; the only workflow that pushes to this repo.

### Column contract (A–AM) — do not reorder
| Col | # | Metric | Source path |
|---|---|---|---|
| A | — | Date (YYYY-MM-DD) | `datetime.now()` |
| B | 1 | Yield Curve (10Y-2Y) | `fred.yieldCurve` (cur→hist) |
| C | 2 | Profit Margin | `fred.profitMargin` (cur→hist) |
| D | 3 | Sahm Rule | `fred.indicators.sahmRule.value` |
| E | 4 | Consumer Sentiment | `fred.indicators.sentiment.value` |
| F | 5 | Initial Claims (4wk, in thousands) | `fred.indicators.claims.value` |
| G | 6 | BBB Credit Spread | `fred.indicators.creditSpread.value` |
| H | 7 | Real Yields (10Y TIPS) | `fred.indicators.realYields.value` |
| I | 8 | **LEI — RETIRED (blank)** | `fred.indicators.lei.value` |
| J | 9 | Market Valuation (P/E) | `fred.peRatio` |
| K | 10 | System Tightness (NFCI) | `fred.checklist.nfci.value` |
| L | 11 | M2 Money Supply | `fred.checklist.m2.value` |
| M | 12 | Retail Sales (3mo) | `fred.checklist.retail.value` |
| N | 13 | Housing Starts (thousands) | `fred.checklist.housing.value` |
| O | 14 | Industrial Production | `fred.checklist.indpro.value` |
| P | 15 | Job Openings JOLTS (thousands) | `fred.checklist.jolts.value` |
| Q | 16 | Durable Goods Orders | `fred.checklist.durable.value` |
| R | 17 | Savings Rate | `fred.checklist.savings.value` |
| S | 18 | ZRI Median Monthly Rent | `market-extra.realEstate.rentIndex` (cur→hist) |
| T | 19 | Estimated Monthly Mortgage | `market-extra.realEstate.mortgagePayment` (cur→hist) |
| U | 20 | 30-Yr Fixed Mortgage Rate | `market-extra.rates.mortgageRate` (cur→hist) |
| V | 21 | 10-Yr Treasury Yield (TNX) | `market-extra.rates.tnx` (cur→hist) |
| W | 22 | 2-Yr Treasury Yield (T2Y) | `market-extra.rates.t2y` (cur→hist) |
| X | 23 | US Dollar Index (DXY) | `market-extra.fx.dxy` (cur→hist) |
| Y | 24 | Crude Oil WTI (CL) | `market-extra.commodities.cl` (cur→hist) |
| Z | 25 | USD/CAD | `market-extra.fx.usdcad` (cur→hist) |
| AA | 26 | USD/INR | `market-extra.fx.usdinr` (cur→hist) |
| AB | 27 | USD/BDT | `market-extra.fx.usdbdt` (cur→hist) |
| AC | 28 | INR/BDT | `market-extra.fx.inrbdt` (cur→hist) |
| AD | 29 | CAD/INR | `market-extra.fx.cadinr` (cur→hist) |
| AE | 30 | Gold (GC) | `market-extra.commodities.gc` (cur→hist) |
| AF | 31 | BTC | `market-extra.commodities.btc` (cur→hist) |
| AG | 32 | Copper/Gold Ratio (replaced LEI) | `fred.indicators.copperGold.value` |
| AH | 33 | ATNHPI US House Price Index | `market-extra.realEstate.atnhpi` (cur→hist) |
| AI | 34 | CAD/BDT | `market-extra.fx.cadbdt` (cur→hist) |
| AJ | 35 | AAII DIFF | `sheets.AAIIDiff` |
| AK | 36 | VIX (Current) | `sheets.VIX.current` |
| AL | 37 | VIX (3M) | `sheets.VIX.threeMonth` |
| AM | 38 | **VIX Fear/Greed — TEXT (verbatim)** | `sheets.VIX.fearGreed` |
