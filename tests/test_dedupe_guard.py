import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper
from datetime import datetime, timezone
from unittest.mock import patch


# ─── current_slot_window ─────────────────────────────────────────────────────
# Slots are anchored at the two cron times (02:00 / 14:00 UTC); boundaries sit at
# the midpoints (08:00 / 20:00 UTC) so a trigger firing a little early/late still
# lands in the same bucket as the "official" cron run for that slot.

def test_slot_window_early_morning_is_the_am_slot():
    now = datetime(2026, 8, 29, 1, 45, tzinfo=timezone.utc)
    start, end, label = scraper.current_slot_window(now)
    assert start == datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    assert label == "2026-08-29-AM"


def test_slot_window_afternoon_is_the_pm_slot():
    now = datetime(2026, 8, 29, 14, 5, tzinfo=timezone.utc)
    start, end, label = scraper.current_slot_window(now)
    assert start == datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc)
    assert label == "2026-08-29-PM"


def test_slot_window_late_night_rolls_to_tomorrows_am_slot():
    now = datetime(2026, 8, 29, 23, 0, tzinfo=timezone.utc)
    start, end, label = scraper.current_slot_window(now)
    assert start == datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    assert label == "2026-08-30-AM"


def test_slot_boundaries_are_exclusive_of_the_next_slot():
    # exactly on a boundary belongs to the slot that STARTS there
    start, end, label = scraper.current_slot_window(datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc))
    assert label == "2026-08-29-PM"
    start, end, label = scraper.current_slot_window(datetime(2026, 8, 29, 19, 59, 59, tzinfo=timezone.utc))
    assert label == "2026-08-29-PM"


# ─── already_ran_this_slot ────────────────────────────────────────────────────

def _env(**overrides):
    base = {
        "GITHUB_REPOSITORY": "jalalchowdhury1/financial-dashboard-history",
        "GITHUB_RUN_ID": "999",
        "GH_GUARD_TOKEN": "fake-token",
    }
    base.update(overrides)
    return base


def test_returns_true_when_a_success_run_falls_inside_the_current_slot():
    now = datetime(2026, 8, 29, 14, 10, tzinfo=timezone.utc)  # PM slot: 08:00-20:00
    runs = {"workflow_runs": [
        {"id": 1, "created_at": "2026-08-29T14:00:05Z"},  # inside PM slot, different run
    ]}
    with patch.dict(os.environ, _env(), clear=False), \
         patch.object(scraper, "_github_api_get", return_value=runs) as mock_get:
        assert scraper.already_ran_this_slot(now=now) is True
    assert mock_get.called


def test_returns_false_when_no_run_falls_inside_the_current_slot():
    now = datetime(2026, 8, 29, 14, 10, tzinfo=timezone.utc)  # PM slot
    runs = {"workflow_runs": [
        {"id": 1, "created_at": "2026-08-29T02:00:05Z"},  # AM slot, not PM
    ]}
    with patch.dict(os.environ, _env(), clear=False), \
         patch.object(scraper, "_github_api_get", return_value=runs):
        assert scraper.already_ran_this_slot(now=now) is False


def test_ignores_its_own_run_id():
    now = datetime(2026, 8, 29, 14, 10, tzinfo=timezone.utc)
    runs = {"workflow_runs": [
        {"id": 999, "created_at": "2026-08-29T14:00:05Z"},  # same as GITHUB_RUN_ID
    ]}
    with patch.dict(os.environ, _env(), clear=False), \
         patch.object(scraper, "_github_api_get", return_value=runs):
        assert scraper.already_ran_this_slot(now=now) is False


def test_propagates_on_api_error_instead_of_silently_choosing_either_way():
    # A guard that can't determine slot state must raise (fail loud), not silently
    # skip (masks a real problem as green) or silently proceed (risks a duplicate
    # row in an append-only sheet). See already_ran_this_slot's docstring.
    now = datetime(2026, 8, 29, 14, 10, tzinfo=timezone.utc)
    with patch.dict(os.environ, _env(), clear=False), \
         patch.object(scraper, "_github_api_get", side_effect=RuntimeError("boom")):
        try:
            scraper.already_ran_this_slot(now=now)
            assert False, "expected already_ran_this_slot to raise"
        except RuntimeError:
            pass


def test_malformed_created_at_is_skipped_not_fatal():
    now = datetime(2026, 8, 29, 14, 10, tzinfo=timezone.utc)
    runs = {"workflow_runs": [
        {"id": 1, "created_at": "not-a-date"},
        {"id": 2, "created_at": None},
    ]}
    with patch.dict(os.environ, _env(), clear=False), \
         patch.object(scraper, "_github_api_get", return_value=runs):
        assert scraper.already_ran_this_slot(now=now) is False


# ─── main() wiring: the guard must actually gate the fetch/append pipeline ────

def test_main_skips_the_whole_pipeline_when_the_slot_already_ran():
    env = _env(GITHUB_ACTIONS="true")
    with patch.dict(os.environ, env, clear=False), \
         patch.object(scraper, "already_ran_this_slot", return_value=True), \
         patch.object(scraper, "get_fred_data") as mock_fred, \
         patch.object(scraper, "authenticate_gspread") as mock_auth:
        scraper.main()
    mock_fred.assert_not_called()
    mock_auth.assert_not_called()


def test_main_bypasses_the_guard_when_scraper_force_is_true():
    env = _env(GITHUB_ACTIONS="true", SCRAPER_FORCE="true")
    with patch.dict(os.environ, env, clear=False), \
         patch.object(scraper, "already_ran_this_slot") as mock_guard, \
         patch.object(scraper, "get_fred_data", side_effect=RuntimeError("stop after guard")):
        try:
            scraper.main()
        except RuntimeError:
            pass  # expected -- we only care that the guard itself was bypassed
    mock_guard.assert_not_called()
