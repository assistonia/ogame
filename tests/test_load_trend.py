from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import load_trend as trend


def _round(at: str, seconds: float, sizing: int = 0) -> trend.Round:
    return trend.Round(
        at=dt.datetime.strptime(at, "%Y-%m-%d %H:%M:%S"),
        seconds=seconds,
        accounts=100,
        sizing_calls=sizing,
    )


def test_parse_rounds_excludes_one_account_probe_and_resets_interrupted_sizing():
    rows = trend.parse_rounds([
        "2026-07-12 17:46:00 [sizing/shadow] target=1:1:1",
        "2026-07-12 17:47:00 --- 바퀴 완료: built=1 idle=0 skipped=0 (4.0s) ---",
        "2026-07-12 17:48:01 === universe 시작: 100봇, loop=True ===",
        "2026-07-12 17:48:30 [sizing/shadow] target=1:1:2",
        "2026-07-12 17:49:28 --- 바퀴 완료: built=98 idle=2 skipped=0 (87.1s) ---",
    ])

    assert len(rows) == 1
    assert rows[0].seconds == 87.1
    assert rows[0].sizing_calls == 1


def test_percentile_matches_linear_p90_used_in_live_analysis():
    values = [87.1, 86.0, 90.4, 94.9, 78.4, 83.0, 82.6, 87.0, 105.5]
    assert trend.percentile(values, 0.90) == pytest.approx(97.02)


def test_report_has_event_steps_and_never_changes_interval(tmp_path):
    rounds = [
        _round("2026-07-12 17:20:00", 80),
        _round("2026-07-12 17:30:00", 100, 2),
        _round("2026-07-12 17:40:00", 110, 4),
    ]
    events_path = tmp_path / "events.json"
    events_path.write_text(json.dumps({"events": [
        {"at": "2026-07-12 17:28:01", "label": "Part 2"},
    ]}), encoding="utf-8")

    result = trend.report(
        rounds, trend.load_events(events_path), dt.date(2026, 7, 12), 3, 150,
    )

    assert result["events"][0]["label"] == "Part 2"
    assert result["events"][0]["n"] == 2
    assert result["rolling"]["status"] == "OK"
    assert result["sizing"]["live_projection_seconds_per_round"]["reuse_tier_cpu"] == [0, 0]
    assert result["sizing"]["reuse_ready_now"] is False
    assert result["auto_interval_changed"] is False


def test_sizing_observation_keeps_negative_result_instead_of_claiming_cost():
    rounds = [
        _round("2026-07-12 18:00:00", 100, 1),
        _round("2026-07-12 18:04:00", 90, 2),
        _round("2026-07-12 18:08:00", 80, 3),
    ]
    assert trend.sizing_seconds_per_call(rounds) == pytest.approx(-10.0)


def test_within_segment_slope_removes_deployment_level_jump():
    rounds = [
        _round("2026-07-12 17:00:00", 80),
        _round("2026-07-12 17:10:00", 79),
        _round("2026-07-12 17:30:00", 110),
        _round("2026-07-12 17:40:00", 109),
    ]
    events = [
        trend.Event(dt.datetime(2026, 7, 12, 17, 0), "before"),
        trend.Event(dt.datetime(2026, 7, 12, 17, 28), "deploy"),
    ]
    slope = trend.within_segment_slope_seconds_per_hour(
        rounds, events, dt.date(2026, 7, 12),
    )
    assert slope == pytest.approx(-6.0)
