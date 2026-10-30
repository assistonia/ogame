from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import discord_battle_notifier as notifier
from scripts import metrics_collector


FAKE_WEBHOOK = "https://discord.com/api/webhooks/123456/test-token"


def _db(tmp_path):
    con = sqlite3.connect(tmp_path / "metrics.db")
    con.execute("""CREATE TABLE battles (
        ts TEXT NOT NULL,
        battle_id INTEGER UNIQUE,
        attacker_id INTEGER,
        defender_id INTEGER,
        loot_metal INTEGER,
        loot_crystal INTEGER,
        loot_deuterium INTEGER,
        battle_ts TEXT,
        attacker_email TEXT,
        defender_email TEXT,
        attacker_loss REAL,
        defender_loss REAL,
        is_probe_only INTEGER
    )""")
    return con


def _battle(con, battle_id, *, attacker="ai01@bots.local", defender="bot001@bots.local",
            loot=(100, 200, 300), losses=(40, 50), probe_only=0):
    con.execute(
        """INSERT INTO battles (
               ts,battle_id,attacker_id,defender_id,
               loot_metal,loot_crystal,loot_deuterium,battle_ts,
               attacker_email,defender_email,attacker_loss,defender_loss,is_probe_only
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "2026-07-12T08:00:00Z", battle_id, 1, 2,
            *loot, "2026-07-12T08:00:00Z", attacker, defender, *losses, probe_only,
        ),
    )
    con.commit()


def _paths(tmp_path):
    marker = tmp_path / ".battle_notifications_enabled"
    webhook = tmp_path / ".discord_webhook_url"
    cursor = tmp_path / ".battle_notifications_cursor"
    marker.touch()
    webhook.write_text(FAKE_WEBHOOK + "\n", encoding="utf-8")
    return marker, webhook, cursor


def test_default_off_neither_sends_nor_creates_cursor(tmp_path):
    con = _db(tmp_path)
    _battle(con, 1)
    called = []
    result = notifier.notify_new_battles(
        con,
        marker_path=tmp_path / "missing-marker",
        webhook_path=tmp_path / "missing-webhook",
        cursor_path=tmp_path / "cursor",
        sender=lambda *_: called.append(True),
    )
    assert result.status == "disabled"
    assert called == []
    assert not (tmp_path / "cursor").exists()
    con.close()


def test_first_enable_baselines_current_max_without_historical_flood(tmp_path):
    con = _db(tmp_path)
    for battle_id in range(1, 2001):
        _battle(con, battle_id)
    marker, webhook, cursor = _paths(tmp_path)
    called = []

    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: called.append(True),
    )

    assert result.status == "baselined"
    assert result.cursor == 2000
    assert cursor.read_text(encoding="utf-8").strip() == "2000"
    assert called == []
    con.close()


def test_top5_battles_are_sent_as_summary(tmp_path):
    con = _db(tmp_path)
    _battle(con, 3)
    marker, webhook, cursor = _paths(tmp_path)
    # 첫 활성 baseline=3.
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    for battle_id in range(4, 14):
        _battle(con, battle_id, loot=(battle_id * 1000, 0, 0))

    sent = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda url, content: sent.append((url, content)),
    )

    assert result.status == "sent"
    assert result.pending == 10
    assert result.sent == 5
    assert result.cursor == 13
    assert cursor.read_text(encoding="utf-8").strip() == "13"
    assert len(sent) == 1
    url, content = sent[0]
    assert url == FAKE_WEBHOOK
    assert FAKE_WEBHOOK not in content
    lines = content.splitlines()
    assert len(lines) == 10
    assert lines[0] == "⚔️ OGame 전투 파괴 TOP 5"
    assert lines[4] == "01. ⚔️ 07-12 17:00 #4 ai01 → bot001 | 약탈 4k | 손실 공 40 / 방 50 | 파괴 90"
    assert lines[-2] == "05. ⚔️ 07-12 17:00 #8 ai01 → bot001 | 약탈 8k | 손실 공 40 / 방 50 | 파괴 90"
    assert lines[-1].startswith("주의 ")
    assert len(content) < 2000

    sent.clear()
    second = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda url, content: sent.append((url, content)),
    )
    assert second.status == "idle"
    assert second.pending == 0
    assert second.sent == 0
    assert second.cursor == 13
    assert sent == []
    con.close()


def test_send_failure_keeps_cursor_and_retries_same_battle(tmp_path):
    con = _db(tmp_path)
    _battle(con, 1)
    marker, webhook, cursor = _paths(tmp_path)
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    _battle(con, 2)

    def fail_with_secret(url, _content):
        raise RuntimeError(f"failed posting {url}")

    failed = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=fail_with_secret,
    )
    assert failed.status == "retry"
    assert failed.reason == "send-failed"
    assert FAKE_WEBHOOK not in repr(failed)
    assert cursor.read_text(encoding="utf-8").strip() == "1"

    sent = []
    retried = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda _url, content: sent.append(content),
    )
    assert retried.status == "sent"
    assert retried.cursor == 2
    assert "#2 ai01 → bot001" in sent[0]
    assert "약탈 600 | 손실 공 40 / 방 50 | 파괴 90" in sent[0]
    con.close()


def test_probe_only_reports_are_silent_and_cursor_still_advances(tmp_path):
    con = _db(tmp_path)
    _battle(con, 1)
    marker, webhook, cursor = _paths(tmp_path)
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    _battle(con, 2, loot=(0, 0, 0), losses=(7000, 0), probe_only=1)
    _battle(con, 3, loot=(48000, 0, 0), losses=(0, 0), probe_only=0)
    _battle(con, 4, loot=(0, 0, 0), losses=(7000, 0), probe_only=1)

    sent = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda _url, content: sent.append(content),
    )
    assert result.status == "sent"
    assert result.pending == 1 and result.sent == 1
    assert result.cursor == 4
    assert cursor.read_text(encoding="utf-8").strip() == "4"
    assert "#3 ai01 → bot001" in sent[0]
    assert "약탈 48k" in sent[0]
    assert "#2" not in sent[0] and "#4" not in sent[0]
    con.close()


def test_only_probe_reports_advance_without_posting(tmp_path):
    con = _db(tmp_path)
    _battle(con, 10)
    marker, webhook, cursor = _paths(tmp_path)
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    _battle(con, 11, loot=(0, 0, 0), losses=(7000, 0), probe_only=1)
    called = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: called.append(True),
    )
    assert result.status == "filtered"
    assert result.cursor == 11 and result.reason == "probe-only"
    assert called == []
    assert cursor.read_text(encoding="utf-8").strip() == "11"
    con.close()


def test_admin_test_battle_is_sent_with_explicit_label(tmp_path):
    con = _db(tmp_path)
    con.execute("ALTER TABLE battles ADD COLUMN is_admin_test INTEGER DEFAULT 0")
    _battle(con, 1)
    marker, webhook, cursor = _paths(tmp_path)
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    _battle(
        con, 2, attacker="simulation-admin@ogamex.local",
        defender="123123123@naver.com", losses=(100, 200),
    )
    con.execute("UPDATE battles SET is_admin_test=1 WHERE battle_id=2")
    con.commit()

    sent = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda _url, content: sent.append(content),
    )

    assert result.status == "sent"
    assert "simulation-admin → 123123123" in sent[0]
    assert "| 관리자테스트" in sent[0]
    con.close()


def test_missing_webhook_or_uncollected_values_are_not_faked_as_zero(tmp_path):
    con = _db(tmp_path)
    marker = tmp_path / ".battle_notifications_enabled"
    cursor = tmp_path / ".battle_notifications_cursor"
    marker.touch()
    cursor.write_text("0\n", encoding="utf-8")
    _battle(
        con, 1, attacker=None, defender=None,
        loot=(None, None, None), losses=(None, None),
    )

    unavailable = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=tmp_path / "missing",
        cursor_path=cursor, sender=lambda *_: None,
    )
    assert unavailable.status == "unavailable"
    assert unavailable.reason == "webhook-unavailable"
    assert cursor.read_text(encoding="utf-8").strip() == "0"

    webhook = tmp_path / ".discord_webhook_url"
    webhook.write_text(FAKE_WEBHOOK, encoding="utf-8")
    sent = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda _url, content: sent.append(content),
    )
    assert result.status == "sent"
    assert "id:1 → id:2" in sent[0]
    assert "약탈 미수집" in sent[0]
    assert "손실 공 미수집 / 방 미수집" in sent[0]
    con.close()


def test_invalid_cursor_rebaselines_instead_of_flooding(tmp_path):
    con = _db(tmp_path)
    _battle(con, 99)
    marker, webhook, cursor = _paths(tmp_path)
    cursor.write_text("not-an-id\n", encoding="utf-8")
    called = []

    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: called.append(True),
    )
    assert result.status == "rebaselined"
    assert result.cursor == 99
    assert called == []
    con.close()


def test_each_battle_stays_on_its_own_line_without_aggregate_fields(tmp_path):
    con = _db(tmp_path)
    con.executescript("""
        CREATE TABLE debris_harvests (
            message_id INTEGER PRIMARY KEY,
            event_ts TEXT,
            email TEXT,
            metal INTEGER,
            crystal INTEGER,
            deuterium INTEGER
        );
        CREATE TABLE metric_capabilities (
            metric TEXT PRIMARY KEY,
            last_success_ts TEXT NOT NULL
        );
        INSERT INTO metric_capabilities VALUES ('debris_harvests','2026-07-12T08:00:00Z');
        INSERT INTO debris_harvests VALUES (
            1,'2026-07-12T08:00:00Z','ai01@bots.local',4000,2000,0
        );
    """)
    _battle(con, 1)
    marker, webhook, cursor = _paths(tmp_path)
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    _battle(con, 2, attacker="ai01@bots.local", defender="ai07@bots.local",
            loot=(0, 0, 0), losses=(1000, 500))
    _battle(con, 3, attacker="ai02@bots.local", defender="ai07@bots.local",
            loot=(100, 0, 0), losses=(500, 200))
    _battle(con, 4, attacker="ai03@bots.local", defender="ai07@bots.local",
            loot=(200, 0, 0), losses=(0, 100))

    sent = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda _url, content: sent.append(content),
    )

    assert result.status == "sent" and result.sent == 3
    content = sent[0]
    lines = content.splitlines()
    assert len(lines) == 8
    assert "#2 ai01 → ai07 | 약탈 0 | 손실 공 1k / 방 500" in lines[4]
    assert "#3 ai02 → ai07 | 약탈 100 | 손실 공 500 / 방 200" in lines[5]
    assert "#4 ai03 → ai07 | 약탈 200 | 손실 공 0 / 방 100" in lines[6]
    assert "데브리 회수 6k" in content


def test_many_battle_lines_are_chunked_below_discord_limit(tmp_path):
    con = _db(tmp_path)
    _battle(con, 1)
    marker, webhook, cursor = _paths(tmp_path)
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    for battle_id in range(2, 72):
        _battle(
            con, battle_id,
            attacker=f"attacker-{battle_id:03d}@bots.local",
            defender=f"defender-{battle_id:03d}@bots.local",
        )

    sent = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda _url, content: sent.append(content),
    )

    assert result.status == "sent" and result.sent == 5 and result.cursor == 71
    assert len(sent) == 1
    assert all(len(content) <= notifier.DISCORD_MAX_CHARS for content in sent)
    assert len(sent[0].splitlines()) == 10
    assert "#6 attacker-006 → defender-006" in sent[0]
    con.close()


def test_partial_chunk_failure_keeps_cursor_for_complete_retry(tmp_path):
    con = _db(tmp_path)
    _battle(con, 1)
    marker, webhook, cursor = _paths(tmp_path)
    notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda *_: None,
    )
    for battle_id in range(2, 72):
        _battle(con, battle_id)

    attempts = []

    def fail_first(_url, content):
        attempts.append(content)
        raise RuntimeError("network failure")

    failed = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=fail_first,
    )
    assert failed.status == "retry"
    assert failed.sent == 0
    assert cursor.read_text(encoding="utf-8").strip() == "1"

    retried = []
    result = notifier.notify_new_battles(
        con, marker_path=marker, webhook_path=webhook, cursor_path=cursor,
        sender=lambda _url, content: retried.append(content),
    )
    assert result.status == "sent" and result.cursor == 71
    assert result.sent == 5
    con.close()
    con.close()


def test_discord_summary_cadence_is_ten_minutes_without_reducing_metrics_resolution():
    assert metrics_collector.INTERVAL_SEC == 300
    assert metrics_collector.BATTLE_NOTIFICATION_INTERVAL_SEC == 600


def test_discord_sender_uses_json_post_with_mocked_transport(monkeypatch):
    captured = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    notifier.discord_sender(FAKE_WEBHOOK, "hello")
    request = captured["request"]
    assert request.get_method() == "POST"
    assert json.loads(request.data.decode("utf-8")) == {
        "content": "hello", "allowed_mentions": {"parse": []},
    }
    assert captured["timeout"] == notifier.HTTP_TIMEOUT_SECONDS


def test_collector_wrapper_swallows_notification_failure(monkeypatch, tmp_path):
    con = _db(tmp_path)

    def explode(_con):
        raise RuntimeError("network error with secret-like context")

    monkeypatch.setattr(metrics_collector.battle_notifier, "notify_new_battles", explode)
    assert metrics_collector.notify_battles_safely(con) is None
    # 연결/수집 DB는 여전히 정상 사용 가능하다.
    _battle(con, 1)
    assert con.execute("SELECT COUNT(*) FROM battles").fetchone()[0] == 1
    con.close()
