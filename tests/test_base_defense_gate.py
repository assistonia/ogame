"""base-defense 24h/실질자산 추세 운영 게이트."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "base_defense_gate", ROOT / "scripts" / "base_defense_gate.py"
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def _fixtures(tmp_path: pathlib.Path, *, converging: bool, points: int = 8):
    db = tmp_path / "metrics.db"
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "ai01.json").write_text(
        json.dumps({"personality": "miner"}), encoding="utf-8"
    )
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE snapshots(ts TEXT,email TEXT,personality TEXT,score INTEGER,"
        "metal REAL,crystal REAL,deuterium REAL)"
    )
    baseline = gate.GATE_STARTED_AT - dt.timedelta(minutes=3)
    stamps = [baseline] + [
        gate.GATE_STARTED_AT + dt.timedelta(minutes=5 * i) for i in range(1, points + 1)
    ]
    for i, stamp in enumerate(stamps):
        key = stamp.isoformat().replace("+00:00", "Z")
        rb = 1000 + i * 10
        ai = 800 + i * (30 if converging else 8)
        con.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)",
            (key, "bot001@bots.local", "miner", rb, 0, 0, 0),
        )
        con.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)",
            (key, "ai01@bots.local", "ai_agent", ai, 0, 0, 0),
        )
    con.commit()
    con.close()
    flag = tmp_path / "miner_defense.py"
    flag.write_text("MINER_BASE_DEFENSE_ENABLED = False\n", encoding="utf-8")
    return db, policies, flag


def test_positive_real_asset_trend_is_ready_before_deadline(tmp_path):
    db, policies, flag = _fixtures(tmp_path, converging=True)
    now = gate.GATE_STARTED_AT + dt.timedelta(hours=1)
    result = gate.evaluate(db, policies, now=now, flag_file=flag)
    assert result.decision == "READY_TREND"
    assert result.ready and result.trend_confirmed
    assert result.post_points == 8
    assert result.ratio_gain >= gate.MIN_RATIO_GAIN


def test_flat_or_falling_trend_waits_before_deadline(tmp_path):
    db, policies, flag = _fixtures(tmp_path, converging=False)
    now = gate.GATE_STARTED_AT + dt.timedelta(hours=1)
    result = gate.evaluate(db, policies, now=now, flag_file=flag)
    assert result.decision == "WAIT"
    assert not result.ready


def test_24h_deadline_makes_gate_ready_even_without_convergence(tmp_path):
    db, policies, flag = _fixtures(tmp_path, converging=False)
    now = gate.GATE_STARTED_AT + gate.MAX_WAIT
    result = gate.evaluate(db, policies, now=now, flag_file=flag)
    assert result.decision == "READY_DEADLINE"
    assert result.ready
    assert not result.trend_confirmed


def test_main_exit_codes_and_prints_exact_flip_procedure(tmp_path, capsys):
    db, policies, flag = _fixtures(tmp_path, converging=True)
    now = gate.GATE_STARTED_AT + dt.timedelta(hours=1)
    rc = gate.main([
        "--db", str(db), "--policy-dir", str(policies),
        "--flag-file", str(flag), "--now", gate._iso(now)
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert gate.FLAG_FROM in out and gate.FLAG_TO in out
    assert "pkill -f '[r]un_universe.py'" in out
    assert "./scripts/watchdog.sh" in out


def test_active_flag_reports_active_without_repeating_flip_commands(tmp_path, capsys):
    db, policies, flag = _fixtures(tmp_path, converging=True)
    flag.write_text(
        "# False may appear in prose\nMINER_BASE_DEFENSE_ENABLED = True\n",
        encoding="utf-8",
    )
    now = gate.GATE_STARTED_AT + dt.timedelta(hours=1)
    rc = gate.main([
        "--db", str(db), "--policy-dir", str(policies),
        "--flag-file", str(flag), "--now", gate._iso(now), "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["decision"] == "ACTIVE" and payload["active"] is True
    assert payload["operations"] == []


def test_main_returns_two_when_cohort_data_is_missing(tmp_path, capsys):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).execute(
        "CREATE TABLE snapshots(ts TEXT,email TEXT,personality TEXT,score INTEGER,"
        "metal REAL,crystal REAL,deuterium REAL)"
    ).connection.close()
    policies = tmp_path / "policies"
    policies.mkdir()
    flag = tmp_path / "miner_defense.py"
    flag.write_text("MINER_BASE_DEFENSE_ENABLED = False\n", encoding="utf-8")
    rc = gate.main([
        "--db", str(db), "--policy-dir", str(policies), "--flag-file", str(flag)
    ])
    assert rc == 2
    assert "GATE ERROR" in capsys.readouterr().err
