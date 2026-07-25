import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper
from datetime import datetime, timedelta, timezone


def _fred():
    return {
        "_meta": {"fetchedAt": "2026-06-21T14:00:00Z"},
        "peRatio": 32.2,
        "yieldCurve": {"current": 0.27, "asOf": "2026-06-18", "history": []},
        # current null -> should fall back to last history point's value
        "profitMargin": {"current": None, "asOf": "2026-01-01", "history": [{"value": 14.8}]},
        "indicators": {
            "sentiment": {"value": 49.8, "asOf": "2026-04-01", "status": "weak"},
            "claims": {"value": None, "asOf": "2026-06-13", "status": "healthy"},  # N/A -> omitted
            "copperGold": {"value": 1.52, "asOf": "2026-06-20", "status": "rising"},
        },
        "checklist": {
            "m2": {"value": 4.72, "asOf": "2026-04-01", "status": "good", "bullish": True, "label": "M2 Money Supply"},
            "savings": {"value": 2.6, "asOf": "2026-04-01", "status": "weak", "bullish": False, "label": "Savings Rate"},
        },
    }


def test_includes_present_metrics_with_asof_status():
    p = dict(scraper.build_lkg_pairs(_fred(), "2026-06-21T14:00:00Z"))
    assert p["updated_at"] == "2026-06-21T14:00:00Z"
    assert p["peRatio"] == 32.2
    assert p["yieldCurve.current"] == 0.27
    assert p["indicators.sentiment.value"] == 49.8
    assert p["indicators.sentiment.status"] == "weak"


def test_profitmargin_falls_back_to_history_when_current_null():
    p = dict(scraper.build_lkg_pairs(_fred(), "x"))
    assert p["profitMargin.current"] == 14.8


def test_omits_na_metrics():
    p = dict(scraper.build_lkg_pairs(_fred(), "x"))
    assert "indicators.claims.value" not in p  # value was None


def test_checklist_bullish_serialized_as_lowercase_string():
    p = dict(scraper.build_lkg_pairs(_fred(), "x"))
    assert p["checklist.m2.bullish"] == "true"
    assert p["checklist.savings.bullish"] == "false"
    assert p["checklist.m2.label"] == "M2 Money Supply"


def test_handles_garbage_input_without_crashing():
    assert scraper.build_lkg_pairs(None, "x") == [["updated_at", "x"]]
    assert scraper.build_lkg_pairs({"indicators": "oops", "checklist": 5}, "x") == [["updated_at", "x"]]


# ─── Four Horsemen block (added 2026-07-25) ─────────────────────────────────
# The dashboard's recession-watch card is a CHART: without >=2 history points
# per line it renders "N/A - Unavailable". These rows are what keep it alive on
# the last-resort path. Reader: financial-telegram-bot lib/sheetLkg.js.

def _hist(n, start="2026-01-01", step_days=7, value=100.0):
    d = datetime.strptime(start, "%Y-%m-%d")
    return [
        {"date": (d + timedelta(days=i * step_days)).strftime("%Y-%m-%d"), "value": value + i}
        for i in range(n)
    ]


def test_pack_history_round_trip_format():
    packed = scraper.pack_history(_hist(3), 5, 300, now=datetime(2026, 1, 20, tzinfo=timezone.utc))
    assert packed == "2026-01-01:100|2026-01-08:101|2026-01-15:102"


def test_pack_history_drops_points_outside_the_window():
    hist = [{"date": "2015-01-01", "value": 1.0}] + _hist(2, start="2026-01-01")
    packed = scraper.pack_history(hist, 5, 300, now=datetime(2026, 1, 20, tzinfo=timezone.utc))
    assert "2015-01-01" not in packed
    assert packed.count("|") == 1


def test_pack_history_thins_but_always_keeps_the_newest_point():
    packed = scraper.pack_history(_hist(1000, step_days=1), 30, 100,
                          now=datetime(2029, 1, 1, tzinfo=timezone.utc))
    segs = packed.split("|")
    assert len(segs) <= 101
    assert segs[-1].startswith("2028-09-26")  # the 1000th daily point


def test_pack_history_stays_inside_the_google_sheets_cell_limit():
    packed = scraper.pack_history(_hist(5000, step_days=1), 30, 5000,
                          now=datetime(2040, 1, 1, tzinfo=timezone.utc))
    assert 0 < len(packed) <= scraper.MAX_PACKED_CHARS


def test_pack_history_returns_empty_for_unusable_input():
    assert scraper.pack_history(None, 5, 300) == ""
    assert scraper.pack_history([], 5, 300) == ""
    assert scraper.pack_history([{"date": "2026-01-01", "value": 1.0}], 5, 300) == ""  # single point
    assert scraper.pack_history([{"date": "bad", "value": 1.0}] * 3, 5, 300) == ""
    assert scraper.pack_history([{"date": "2026-01-01", "value": None}] * 3, 5, 300) == ""


def test_build_horsemen_pairs_emits_the_reader_contract():
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    fred = {
        "horsemen": {
            "claims": {"current": 187000, "asOf": "2026-07-18", "history": _hist(3, start="2026-07-04")},
            "unemployment": {"current": 4.2, "asOf": "2026-06-01", "history": _hist(3, start="2026-04-01", step_days=30)},
            "bankruptcies": {"current": 25960, "asOf": "2026-03-31", "total": 591850,
                             "changePct": 11.37, "status": "rising",
                             "history": _hist(3, start="2025-09-30", step_days=91)},
        },
        "yieldCurve": {"current": 0.36, "asOf": "2026-07-24", "history": _hist(3, start="2026-07-10")},
    }
    keys = dict(scraper.build_horsemen_pairs(fred, now=now))
    for k in ("horsemen.claims.value", "horsemen.claims.asOf", "horsemen.claims.history",
              "horsemen.unemployment.value", "horsemen.unemployment.history",
              "horsemen.bankruptcies.value", "horsemen.bankruptcies.history",
              "horsemen.bankruptcies.total", "horsemen.bankruptcies.changePct",
              "horsemen.bankruptcies.status", "yieldCurve.history"):
        assert k in keys, f"missing contract key {k}"
    assert keys["horsemen.claims.value"] == 187000
    assert keys["horsemen.bankruptcies.status"] == "rising"
    assert keys["horsemen.claims.history"].count("|") == 2


def test_build_horsemen_pairs_omits_missing_lines_and_never_raises():
    assert scraper.build_horsemen_pairs({}) == []
    assert scraper.build_horsemen_pairs({"horsemen": "not-a-dict"}) == []
    assert scraper.build_horsemen_pairs(None) == []
    # A line present but N/A is omitted entirely, like every other metric here.
    out = dict(scraper.build_horsemen_pairs({"horsemen": {"claims": {"current": None, "history": []}}}))
    assert out == {}


def test_build_lkg_pairs_includes_horsemen_without_disturbing_existing_keys():
    fred = {
        "peRatio": 28.1,
        "yieldCurve": {"current": 0.36, "asOf": "2026-07-24",
                       "history": [{"date": "2026-07-17", "value": 0.3},
                                   {"date": "2026-07-24", "value": 0.36}]},
        "indicators": {"sahmRule": {"value": 0.03, "asOf": "2026-06-01", "status": "safe"}},
        "horsemen": {"claims": {"current": 187000, "asOf": "2026-07-18",
                                "history": [{"date": "2026-07-11", "value": 190000},
                                            {"date": "2026-07-18", "value": 187000}]}},
    }
    keys = dict(scraper.build_lkg_pairs(fred, "2026-07-25T02:00:00Z"))
    assert keys["updated_at"] == "2026-07-25T02:00:00Z"
    assert keys["yieldCurve.current"] == 0.36
    assert keys["indicators.sahmRule.value"] == 0.03
    assert keys["horsemen.claims.value"] == 187000
    assert keys["yieldCurve.history"] == "2026-07-17:0.3|2026-07-24:0.36"
