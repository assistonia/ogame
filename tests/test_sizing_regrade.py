from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import sizing_regrade as gate


FAKE_WEBHOOK = "https://discord.com/api/webhooks/123456/test-token"


def _write_log(path: pathlib.Path) -> None:
    path.write_text(
        "\n".join([
            "2026-07-12 17:40:00 [1/100] bot001@bots.local [레이더] raid PREFLIGHT SPY 1:2:3 (정찰기7, 다음 틱 full-intel 재검증)",
            "2026-07-12 17:41:00 [1/100] bot001@bots.local [레이더] raid PREFLIGHT WAIT 1:2:3 (요청 이후 full-intel 대기)",
            "2026-07-12 17:42:00 [1/100] bot002@bots.local [레이더] raid PREFLIGHT HOLD (재정찰에 방어/복귀함대 탐지)",
            "2026-07-12 17:43:00 [1/100] bot003@bots.local [레이더] raid PREFLIGHT HOLD (24h peak stress: 승률 0%<85%)",
            "2026-07-12 17:44:00 [1/100] bot001@bots.local [레이더] raid ATTACK[PREFLIGHT] 1:2:3 (함대가치44000, ETA67s, 24h-peak)",
        ]) + "\n",
        encoding="utf-8",
    )


def _metrics(path: pathlib.Path, *, battle_id: int = 42) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE battles(battle_id INTEGER,battle_ts TEXT,attacker_email TEXT)"
    )
    # ATTACK 17:44 KST 뒤 3분(UTC 08:47)에 생성된 같은 계정 전투.
    con.execute(
        "INSERT INTO battles VALUES(?,?,?)",
        (battle_id, "2026-07-12T08:47:00Z", "bot001@bots.local"),
    )
    con.commit()
    con.close()


def test_log_summary_reports_each_stage_and_return_risk(tmp_path):
    log = tmp_path / "universe.log"
    _write_log(log)

    summary = gate.summarize_events(gate.parse_preflight_events(log))

    assert summary["counts"] == {"SPY": 1, "WAIT": 1, "HOLD": 2, "ATTACK": 1}
    evidence = summary["stale_return_evidence"]
    assert evidence["returned_fleet_or_defense_blocks"] == 1
    assert evidence["history_peak_stress_blocks"] == 1
    assert evidence["direct_risk_blocks"] == 2


def test_legacy_preflight_requires_log_metrics_and_guard_exact_match(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "COHORT", gate.LEGACY_INFERENCE_COHORT)
    log = tmp_path / "universe.log"
    _write_log(log)
    db = tmp_path / "metrics.db"
    _metrics(db)
    states = tmp_path / "states"
    states.mkdir()
    (states / "bot001_bots_local.json").write_text(json.dumps({
        "pending": [],
        "resolved": [{
            "report_id": 42, "mode": "log", "predicted_loss": 0,
            "actual_loss": 0, "ratio": 0, "resolved_at": 1,
        }],
    }), encoding="utf-8")

    result = gate.run_regrade(
        state_dir=states, log_path=log, metrics_db=db,
        now=gate._local_epoch("2026-07-12 18:44:00"),
    )

    assert result["eligible_reports"] == 1
    assert result["cohort_origin_counts"]["legacy_inferred_resolved"] == 1
    assert result["aggregate_loss_error"] == 0
    assert result["gate_status"] == "COLLECTING"
    assert result["sizing_live_auto_changed"] is False


def test_legacy_row_without_dispatch_log_is_excluded(tmp_path):
    log = tmp_path / "universe.log"
    log.write_text("", encoding="utf-8")
    db = tmp_path / "metrics.db"
    _metrics(db)
    states = tmp_path / "states"
    states.mkdir()
    (states / "unknown.json").write_text(json.dumps({
        "pending": [],
        "resolved": [{"report_id": 42, "predicted_loss": 0, "actual_loss": 0}],
    }), encoding="utf-8")

    result = gate.run_regrade(state_dir=states, log_path=log, metrics_db=db, now=1)

    assert result["eligible_reports"] == 0


def test_explicit_cohort_needs_no_metrics_join(tmp_path):
    states = tmp_path / "states"
    states.mkdir()
    (states / "bot001_bots_local.json").write_text(json.dumps({
        "pending": [{
            "email": "bot001@bots.local", "sent_at": 150,
            "target": [1, 2, 3], "attacker": {"204": 4},
            "predicted_loss": 0, "cohort": gate.COHORT,
        }],
        "resolved": [{
            "email": "bot001@bots.local", "report_id": 99, "sent_at": 100,
            "target": [1, 2, 3], "attacker": {"204": 4},
            "predicted_loss": 100, "actual_loss": 120, "cohort": gate.COHORT,
        }],
    }), encoding="utf-8")

    result = gate.run_regrade(
        state_dir=states, log_path=tmp_path / "missing.log",
        metrics_db=tmp_path / "missing.db", now=3700, min_reports=1,
    )

    assert result["eligible_reports"] == 1
    assert result["pending_predictions"] == 1
    assert result["gate_status"] == "FAIL"
    assert round(result["aggregate_loss_error"], 3) == 0.167
    assert result["cohort_origin_counts"]["explicit_resolved"] == 1


def test_v1_fixed_cap_samples_are_not_mixed_into_dynamic_v2(tmp_path):
    states = tmp_path / "states"
    states.mkdir()
    (states / "old.json").write_text(json.dumps({
        "pending": [],
        "resolved": [{
            "email": "bot001@bots.local", "report_id": 98, "sent_at": 100,
            "target": [1, 2, 3], "attacker": {"204": 4},
            "predicted_loss": 0, "actual_loss": 0, "cohort": "preflight_v1",
        }],
    }), encoding="utf-8")

    result = gate.run_regrade(
        state_dir=states, log_path=tmp_path / "missing.log",
        metrics_db=tmp_path / "missing.db", now=3700,
    )

    assert gate.COHORT == "preflight_v2_dynamic_cap"
    assert result["eligible_reports"] == 0
    assert result["pending_predictions"] == 0


def test_admin_test_battles_are_not_available_for_sizing_match(tmp_path):
    db = tmp_path / "metrics.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE battles(
               battle_id INTEGER,battle_ts TEXT,attacker_email TEXT,is_admin_test INTEGER
           )"""
    )
    con.executemany(
        "INSERT INTO battles VALUES(?,?,?,?)",
        [
            (41, "2026-07-12T08:47:00Z", "bot001@bots.local", 0),
            (42, "2026-07-12T08:48:00Z", "bot001@bots.local", 1),
        ],
    )
    con.commit()
    con.close()

    assert gate.load_battle_times(db) == {
        41: (gate._iso_epoch("2026-07-12T08:47:00Z"), "bot001@bots.local")
    }


def _notify_result(status: str = "PASS", eligible: int = 57) -> dict:
    return {
        "cohort": gate.COHORT,
        "eligible_reports": eligible,
        "pending_predictions": 0,
        "predicted_loss_total": 0.0,
        "actual_loss_total": 0.0,
        "aggregate_loss_error": 0.0 if status == "PASS" else 1.0,
        "gate_status": status,
        "gate_min_reports": 57,
        "gate_max_loss_error": 0.15,
        "generated_at": "2026-07-12T18:00:00+09:00",
        "preflight": {"stale_return_evidence": {
            "direct_risk_blocks": 2,
            "returned_fleet_or_defense_blocks": 1,
            "history_peak_stress_blocks": 1,
        }},
    }


def test_notify_is_idle_before_n57_and_dedupes_threshold_and_verdict(tmp_path):
    webhook = tmp_path / ".discord_webhook_url"
    webhook.write_text(FAKE_WEBHOOK, encoding="utf-8")
    state = tmp_path / "notify.json"
    sent: list[str] = []
    sender = lambda _url, content: sent.append(content)

    assert gate.maybe_notify(
        _notify_result("COLLECTING", 56), state_path=state,
        webhook_path=webhook, sender=sender,
    ) == "idle"
    assert not state.exists() and sent == []

    assert gate.maybe_notify(
        _notify_result("PASS", 57), state_path=state,
        webhook_path=webhook, sender=sender,
    ) == "sent"
    assert len(sent) == 1
    assert "자동 변경하지 않음" in sent[0]
    assert gate.maybe_notify(
        _notify_result("PASS", 58), state_path=state,
        webhook_path=webhook, sender=sender,
    ) == "idle"
    assert len(sent) == 1

    assert gate.maybe_notify(
        _notify_result("FAIL", 59), state_path=state,
        webhook_path=webhook, sender=sender,
    ) == "sent"
    assert len(sent) == 2
    assert json.loads(state.read_text(encoding="utf-8"))["notified_verdict"] == "FAIL"
    assert state.stat().st_mode & 0o777 == 0o600


def test_notify_dedupe_resets_for_a_new_cohort(tmp_path):
    webhook = tmp_path / ".discord_webhook_url"
    webhook.write_text(FAKE_WEBHOOK, encoding="utf-8")
    state = tmp_path / "notify.json"
    sent: list[str] = []
    sender = lambda _url, content: sent.append(content)

    old = _notify_result("PASS", 57)
    old["cohort"] = "preflight_v1"
    new = _notify_result("PASS", 57)

    assert gate.maybe_notify(
        old, state_path=state, webhook_path=webhook, sender=sender,
    ) == "sent"
    assert gate.maybe_notify(
        new, state_path=state, webhook_path=webhook, sender=sender,
    ) == "sent"
    assert len(sent) == 2
    assert json.loads(state.read_text(encoding="utf-8"))["cohort"] == gate.COHORT


def test_send_failure_never_advances_dedupe_or_returns_secret(tmp_path):
    webhook = tmp_path / ".discord_webhook_url"
    webhook.write_text(FAKE_WEBHOOK, encoding="utf-8")
    state = tmp_path / "notify.json"

    def fail(url: str, _content: str) -> None:
        raise RuntimeError(f"failed {url}")

    status = gate.maybe_notify(
        _notify_result(), state_path=state, webhook_path=webhook, sender=fail,
    )

    assert status == "retry"
    assert "test-token" not in status
    assert not state.exists()
