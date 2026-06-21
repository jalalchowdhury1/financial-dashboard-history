import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper


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
