#!/usr/bin/env python3
"""직전 AI 정책 구간을 동급 규칙봇과 비교하는 자기경험 성적표.

이 모듈은 게임 DB를 직접 읽지 않는다. ``scripts/metrics.db``의 수집 완료 데이터만
read-only로 읽고, 다음 정책 프롬프트에 넣을 짧은 블록을 만든다.

활성은 두 개의 독립 게이트를 모두 요구한다.

* ``AI_EXPERIENCE_SCORECARD_ENABLED=1``
* ``AI_TECH_TREE_DEPLOYED=1``

따라서 코드를 먼저 배포해도 테크트리 배포 전에는 정책/메모리에 아무 영향을 주지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import statistics
from collections import Counter, defaultdict
from typing import Any, Mapping


SCHEMA_VERSION = 2
FEATURE_FLAG = "AI_EXPERIENCE_SCORECARD_ENABLED"
TECH_TREE_FLAG = "AI_TECH_TREE_DEPLOYED"
TRUE_VALUES = {"1", "true", "yes", "on"}
KNOWN_PERSONALITIES = {"raider", "turtle", "miner", "explorer", "hybrid"}
MIN_WINDOW_SECONDS = 20 * 60
MAX_PEERS = 5
MAX_STARTING_TIER_RATIO = 2.0
COLLECTION_GRACE_SECONDS = 10 * 60
ACTIVATION_HISTORY_MAX = 200
LEDGER_MAX_OBSERVATIONS = 200
PROMOTION_REPETITIONS = 3
LESSONS_START = "<!-- AI_EXPERIENCE_LESSONS_START -->"
LESSONS_END = "<!-- AI_EXPERIENCE_LESSONS_END -->"

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_METRICS_DB = ROOT / "scripts" / "metrics.db"
FEATURE_MARKER = pathlib.Path(__file__).with_name(".experience_scorecard_enabled")
TECH_TREE_MARKER = pathlib.Path(__file__).with_name(".tech_tree_deployed")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truthy(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in TRUE_VALUES


def gate_status(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """기능/테크트리 이중 게이트. 명시 env가 marker보다 우선하며 기본은 OFF다."""
    values = os.environ if env is None else env
    feature_enabled = (
        _truthy(values.get(FEATURE_FLAG))
        if FEATURE_FLAG in values
        else FEATURE_MARKER.exists()
    )
    tech_tree_deployed = (
        _truthy(values.get(TECH_TREE_FLAG))
        if TECH_TREE_FLAG in values
        else TECH_TREE_MARKER.exists()
    )
    if not feature_enabled:
        return False, "feature-flag-off"
    if not tech_tree_deployed:
        return False, "tech-tree-not-deployed"
    return True, "enabled"


def _atomic_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _atomic_json(path: pathlib.Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _load_json_dict(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def record_policy_activation(
    email: str,
    new_policy: Mapping[str, Any],
    previous_policy: Mapping[str, Any] | None,
    history_path: pathlib.Path,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """서버 시각으로 새 정책 활성화를 기록하고 직전 활성화를 닫는다.

    모델이 출력한 ``updated_at``/``activation_id``는 신뢰하지 않는다. 정책 제어값은
    그대로 보존하되 시간과 활성화 ID만 서버가 다시 발급한다. 이 이력의 닫힌 구간만
    성적표가 읽으므로 정책 구간의 겹침과 수집 지연 오염을 함께 막는다.
    """
    server_now = (now or _utc_now()).astimezone(dt.timezone.utc)
    activated_at = _iso(server_now)
    history = _load_json_dict(history_path) or {}
    raw_activations = history.get("activations")
    activations = [
        dict(item) for item in raw_activations or [] if isinstance(item, dict)
    ]

    previous_id = (
        previous_policy.get("activation_id")
        if isinstance(previous_policy, Mapping)
        else None
    )
    previous_fp = (
        _policy_fingerprint(policy_view(previous_policy))
        if isinstance(previous_policy, Mapping)
        else None
    )
    new_fingerprint = _policy_fingerprint(policy_view(new_policy))
    if (
        isinstance(previous_policy, Mapping)
        and isinstance(previous_id, str)
        and previous_id
        and previous_fp == new_fingerprint
    ):
        # Formatting, key order, timestamps and explanatory notes are not a new
        # experiment. Keep the activation interval open and refresh only policy
        # liveness so an unchanged but healthy policy does not expire.
        refreshed = dict(previous_policy)
        refreshed["updated_at"] = activated_at
        refreshed["refreshed_at"] = activated_at
        return refreshed

    for item in reversed(activations):
        if item.get("deactivated_at") is not None:
            continue
        same_id = isinstance(previous_id, str) and item.get("activation_id") == previous_id
        same_policy = (
            previous_id is None
            and previous_fp is not None
            and item.get("policy_fingerprint") == previous_fp
        )
        if same_id or same_policy:
            item["deactivated_at"] = activated_at
            break

    stamped = dict(new_policy)
    stamped["updated_at"] = activated_at
    stamped["activated_at"] = activated_at
    view = policy_view(stamped)
    fingerprint = _policy_fingerprint(view)
    nonce = server_now.isoformat(timespec="microseconds")
    previous_token = previous_id if isinstance(previous_id, str) else "bootstrap"
    activation_id = hashlib.sha256(
        f"{email}|{nonce}|{previous_token}|{fingerprint}|{len(activations)}".encode("utf-8")
    ).hexdigest()[:24]
    stamped["activation_id"] = activation_id
    activations.append({
        "activation_id": activation_id,
        "activated_at": activated_at,
        "deactivated_at": None,
        "policy_fingerprint": fingerprint,
        "policy": view,
    })
    _atomic_json(history_path, {
        "schema_version": SCHEMA_VERSION,
        "email": email,
        "activations": activations[-ACTIVATION_HISTORY_MAX:],
    })
    return stamped


def _short_list(value: Any, limit: int = 12) -> list[Any]:
    if not isinstance(value, list):
        return []
    out: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            out.append(item)
        elif isinstance(item, dict):
            clean: dict[str, Any] = {}
            for key in ("id", "batch", "max_total", "min_surplus"):
                if key in item and isinstance(item[key], (str, int, float, dict)):
                    clean[key] = item[key]
            if clean:
                out.append(clean)
    return out


def policy_view(policy: Mapping[str, Any]) -> dict[str, Any]:
    """프롬프트/ledger에 필요한 제어 레버만 크기 제한해 복사한다."""
    aggression = policy.get("aggression")
    if not isinstance(aggression, dict):
        aggression = {}
    targeting = policy.get("targeting")
    if not isinstance(targeting, dict):
        targeting = {}
    limits = policy.get("limits")
    if not isinstance(limits, dict):
        limits = {}
    ship_strategy = policy.get("ship_strategy")
    if not isinstance(ship_strategy, dict):
        ship_strategy = {}
    fleetsave = policy.get("fleetsave")
    if not isinstance(fleetsave, dict):
        fleetsave = {}
    defense_strategy = policy.get("defense_strategy")
    if not isinstance(defense_strategy, dict):
        defense_strategy = {}
    return {
        "personality": policy.get("personality"),
        "autonomy": policy.get("autonomy"),
        "action_plan": _short_list(policy.get("action_plan"), limit=20),
        "build_priority": _short_list(policy.get("build_priority")),
        "research_priority": _short_list(policy.get("research_priority")),
        "ship_priority": _short_list(policy.get("ship_priority")),
        "ship_strategy": {
            key: ship_strategy[key]
            for key in ("reinvestment_ratio", "doctrine", "weights")
            if key in ship_strategy
        },
        "aggression": {
            key: aggression[key]
            for key in ("min_loot", "max_active_missions")
            if isinstance(aggression.get(key), (int, float))
            and not isinstance(aggression.get(key), bool)
        },
        "targeting": {
            key: targeting[key]
            for key in (
                "allow_strong_targets", "allow_defended_targets",
                "excluded_systems", "priority_targets",
            )
            if key in targeting
        },
        "limits": {
            key: value
            for key, value in limits.items()
            if isinstance(value, (int, float, bool))
        },
        "defense_ratio": policy.get("defense_ratio"),
        "defense_strategy": {
            key: defense_strategy[key]
            for key in (
                "mode", "stored_loot_multiplier", "production_hours",
                "rebuild_on_attack",
            )
            if key in defense_strategy
        },
        "fleetsave": {
            key: fleetsave[key]
            for key in ("mode", "min_value")
            if key in fleetsave
        },
        # Structured commands and diplomacy are executable controls too. Omitting
        # them from the fingerprint made materially different plans look identical.
        "plans": (
            json.loads(json.dumps(policy.get("plans"), ensure_ascii=False))
            if isinstance(policy.get("plans"), dict)
            else {}
        ),
        "diplomacy": (
            json.loads(json.dumps(policy.get("diplomacy"), ensure_ascii=False))
            if isinstance(policy.get("diplomacy"), dict)
            else {}
        ),
        "energy_target": policy.get("energy_target"),
        "note": str(policy.get("note", ""))[:240],
    }


def _policy_fingerprint(view: Mapping[str, Any]) -> str:
    controls = {key: value for key, value in view.items() if key != "note"}
    raw = json.dumps(controls, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _open_metrics(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


SNAPSHOT_FIELDS = (
    "ts", "score", "building_sum", "metal", "crystal", "deuterium",
)


def _snapshot_rows(
    con: sqlite3.Connection,
    email: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT ts, score, building_sum, metal, crystal, deuterium
           FROM snapshots
           WHERE email=? AND ts>=? AND ts<=?
           ORDER BY ts""",
        (email, _iso(start), _iso(end)),
    ).fetchall()


def _number(row: Mapping[str, Any], key: str) -> float:
    value = row[key]
    return float(value or 0)


def _stock(row: Mapping[str, Any]) -> float:
    return sum(_number(row, key) for key in ("metal", "crystal", "deuterium"))


def _effective_asset(row: Mapping[str, Any]) -> float:
    # highscores.general은 쓴 자원 누적, 보유자원은 아직 반영되지 않는다.
    return _number(row, "score") + _stock(row) / 1000.0


def _growth(rows: list[sqlite3.Row]) -> dict[str, Any] | None:
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    first_ts, last_ts = _parse_time(first["ts"]), _parse_time(last["ts"])
    if first_ts is None or last_ts is None:
        return None
    seconds = (last_ts - first_ts).total_seconds()
    if seconds < MIN_WINDOW_SECONDS:
        return None
    hours = seconds / 3600.0
    deltas = {
        "score_delta": _number(last, "score") - _number(first, "score"),
        # 현 스키마의 building_sum은 광산 3종+태양광이다. 이름에 이를 명시해
        # 순수 광산 합으로 과장하지 않는다.
        "resource_building_levels_delta": (
            _number(last, "building_sum") - _number(first, "building_sum")
        ),
        "resource_stock_delta": _stock(last) - _stock(first),
        "effective_asset_delta": _effective_asset(last) - _effective_asset(first),
    }
    return {
        "sample_count": len(rows),
        "first_ts": _iso(first_ts),
        "last_ts": _iso(last_ts),
        "duration_hours": hours,
        "starting_effective_asset": _effective_asset(first),
        **deltas,
        "per_hour": {key: value / hours for key, value in deltas.items()},
    }


def _ratio(value: float, baseline: float) -> float | None:
    if not math.isfinite(value) or not math.isfinite(baseline) or baseline <= 0:
        return None
    return value / baseline


def _normalized_effective_asset_rate(growth: Mapping[str, Any]) -> float | None:
    """체급이 다른 계정도 공정하게 비교할 시간당 실질자산 성장률."""
    start = float(growth.get("starting_effective_asset") or 0.0)
    hours = float(growth.get("duration_hours") or 0.0)
    delta = float(growth.get("effective_asset_delta") or 0.0)
    if start <= 0 or hours <= 0 or not all(map(math.isfinite, (start, hours, delta))):
        return None
    return delta / start / hours


def _economy(
    con: sqlite3.Connection,
    email: str,
    personality: str,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    if not set(SNAPSHOT_FIELDS).issubset(_table_columns(con, "snapshots")):
        return {"status": "unavailable", "reason": "snapshot-schema-missing"}

    own = _growth(_snapshot_rows(con, email, start, end))
    if own is None:
        return {
            "status": "unavailable",
            "reason": "need-two-snapshots-and-20m-window",
        }

    # 실제 AI가 측정된 first/last에 맞춰 peer도 같은 구간으로 자른다.
    peer_start = _parse_time(own["first_ts"])
    peer_end = _parse_time(own["last_ts"])
    assert peer_start is not None and peer_end is not None
    candidates = con.execute(
        """SELECT DISTINCT email FROM snapshots
           WHERE personality=? AND email LIKE 'bot%@bots.local'
             AND ts>=? AND ts<=?""",
        (personality, _iso(peer_start), _iso(peer_end)),
    ).fetchall()

    same_tier_peers: list[tuple[float, str, dict[str, Any]]] = []
    all_peers: list[tuple[float, str, dict[str, Any]]] = []
    own_start = float(own["starting_effective_asset"])
    for candidate in candidates:
        peer_email = str(candidate["email"])
        growth = _growth(_snapshot_rows(con, peer_email, peer_start, peer_end))
        if growth is None:
            continue
        peer_asset = float(growth["starting_effective_asset"])
        if own_start > 0 and peer_asset > 0:
            tier_ratio = max(own_start, peer_asset) / min(own_start, peer_asset)
            distance = abs(math.log(peer_asset / own_start))
        else:
            tier_ratio = math.inf if own_start != peer_asset else 1.0
            distance = abs(peer_asset - own_start)
        peer = (distance, peer_email, growth)
        all_peers.append(peer)
        if tier_ratio <= MAX_STARTING_TIER_RATIO:
            same_tier_peers.append(peer)

    # 가능한 경우 종전처럼 2배 이내 동체급을 쓰고, 새 계정처럼 동체급이 아직 없을 때는
    # 가장 가까운 같은 personality 규칙봇을 선택해 "시작 자산 대비 시간당 성장률"로
    # 정규화한다. 이 fallback이 없으면 LLM이 규칙봇보다 못해도 피드백이 영원히 unavailable이다.
    normalized_fallback = not same_tier_peers
    peers = same_tier_peers if same_tier_peers else all_peers
    peers.sort(key=lambda item: item[0])
    peers = peers[:MAX_PEERS]

    if not peers:
        return {
            "status": "partial",
            "reason": "no-same-personality-rule-peer",
            "account": own,
            "peer_baseline": None,
            "relative": None,
        }

    delta_keys = (
        "score_delta",
        "resource_building_levels_delta",
        "resource_stock_delta",
        "effective_asset_delta",
    )
    medians = {
        key: statistics.median(float(peer[2][key]) for peer in peers)
        for key in delta_keys
    }
    if normalized_fallback:
        own_rate = _normalized_effective_asset_rate(own)
        peer_rates = [
            rate for peer in peers
            if (rate := _normalized_effective_asset_rate(peer[2])) is not None
        ]
        median_rate = statistics.median(peer_rates) if peer_rates else math.nan
        relative = {
            key.replace("_delta", "_ratio"): None for key in delta_keys
        }
        relative["effective_asset_ratio"] = (
            None if own_rate is None else _ratio(own_rate, median_rate)
        )
        medians["normalized_effective_asset_rate_per_hour"] = median_rate
        selection = (
            "같은 personality의 가장 가까운 규칙봇; 2배 체급 밖이라 "
            "실질자산을 시작 자산 대비 시간당 성장률로 비교"
        )
    else:
        relative = {
            key.replace("_delta", "_ratio"): _ratio(float(own[key]), medians[key])
            for key in delta_keys
        }
        selection = "같은 personality, 시작 실질자산 2배 이내의 가장 가까운 규칙봇"
    return {
        "status": "ready",
        "account": own,
        "peer_baseline": {
            "selection": selection,
            "cohort_size": len(peers),
            "emails": [peer[1] for peer in peers],
            "median": medians,
        },
        "relative": relative,
    }


def _capability_fresh(
    con: sqlite3.Connection,
    metric: str,
    end: dt.datetime,
) -> tuple[bool, str]:
    if not {"metric", "last_success_ts"}.issubset(
        _table_columns(con, "metric_capabilities")
    ):
        return False, "collector-capability-marker-missing"
    row = con.execute(
        "SELECT last_success_ts FROM metric_capabilities WHERE metric=?", (metric,)
    ).fetchone()
    if row is None:
        return False, "collector-never-confirmed-source-read"
    seen = _parse_time(row["last_success_ts"])
    if seen is None:
        return False, "collector-capability-timestamp-invalid"
    # 정책이 닫힌 뒤의 원본 조회가 한 번은 성공해야 해당 구간이 backfill됐다고 본다.
    # 단순히 "끝 시각 근처"인 marker를 허용하면 마지막 수 분의 전투가 빠질 수 있다.
    if seen < end:
        return False, "collector-has-not-backfilled-interval-end"
    return True, "ready"


def _battle_metrics(
    con: sqlite3.Connection,
    email: str,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    needed = {
        "battle_ts", "attacker_email", "defender_email",
        "attacker_loss", "defender_loss",
        "loot_metal", "loot_crystal", "loot_deuterium",
        "is_probe_only",
    }
    if not needed.issubset(_table_columns(con, "battles")):
        return {
            "status": "unavailable",
            "reason": "battle-outcome-schema-not-deployed",
            "attack_count": None,
            "defense_count": None,
            "loot_income": None,
            "attacking_fleet_loss": None,
            "defending_unit_loss": None,
            "probe_attack_count": None,
            "probe_defense_count": None,
            "probe_only_count": None,
            "espionage_cost": None,
        }
    fresh, reason = _capability_fresh(con, "battle_outcomes", end)
    if not fresh:
        return {
            "status": "unavailable",
            "reason": reason,
            "attack_count": None,
            "defense_count": None,
            "loot_income": None,
            "attacking_fleet_loss": None,
            "defending_unit_loss": None,
            "probe_attack_count": None,
            "probe_defense_count": None,
            "probe_only_count": None,
            "espionage_cost": None,
        }

    # 구 metrics.db도 읽을 수 있게 컬럼은 optional로 취급한다. 배포된 최신 스키마에서는
    # 전용 Admin 수동 전투가 AI/규칙봇의 정책 성과로 귀속되지 않도록 양쪽 모두 제외한다.
    admin_test_filter = (
        " AND COALESCE(is_admin_test,0)=0"
        if "is_admin_test" in _table_columns(con, "battles")
        else ""
    )
    conventional_filter = f"{admin_test_filter} AND COALESCE(is_probe_only,0)=0"
    probe_filter = f"{admin_test_filter} AND COALESCE(is_probe_only,0)=1"
    attack = con.execute(
        f"""SELECT COUNT(*) AS n,
                  COALESCE(SUM(loot_metal+loot_crystal+loot_deuterium),0) AS loot,
                  SUM(attacker_loss) AS loss,
                  SUM(CASE WHEN attacker_loss IS NULL THEN 1 ELSE 0 END) AS missing
           FROM battles
           WHERE attacker_email=? AND battle_ts>=? AND battle_ts<=?{conventional_filter}""",
        (email, _iso(start), _iso(end)),
    ).fetchone()
    defense = con.execute(
        f"""SELECT COUNT(*) AS n, SUM(defender_loss) AS loss,
                  SUM(CASE WHEN defender_loss IS NULL THEN 1 ELSE 0 END) AS missing
           FROM battles
           WHERE defender_email=? AND battle_ts>=? AND battle_ts<=?{conventional_filter}""",
        (email, _iso(start), _iso(end)),
    ).fetchone()
    probe_attack = con.execute(
        f"""SELECT COUNT(*) AS n, SUM(attacker_loss) AS loss,
                  SUM(CASE WHEN attacker_loss IS NULL THEN 1 ELSE 0 END) AS missing
           FROM battles
           WHERE attacker_email=? AND battle_ts>=? AND battle_ts<=?{probe_filter}""",
        (email, _iso(start), _iso(end)),
    ).fetchone()
    probe_defense = con.execute(
        f"""SELECT COUNT(*) AS n
           FROM battles
           WHERE defender_email=? AND battle_ts>=? AND battle_ts<=?{probe_filter}""",
        (email, _iso(start), _iso(end)),
    ).fetchone()
    attack_n, defense_n = int(attack["n"]), int(defense["n"])
    probe_attack_n, probe_defense_n = (
        int(probe_attack["n"]), int(probe_defense["n"])
    )
    attack_missing = int(attack["missing"] or 0)
    defense_missing = int(defense["missing"] or 0)
    probe_missing = int(probe_attack["missing"] or 0)
    any_missing = attack_missing or defense_missing or probe_missing
    return {
        "status": "ready" if not any_missing else "partial",
        "reason": (
            None if not any_missing else "one-or-more-battle-loss-values-missing"
        ),
        "attack_count": attack_n,
        "defense_count": defense_n,
        "loot_income": float(attack["loot"] or 0),
        "attacking_fleet_loss": (
            float(attack["loss"] or 0) if attack_missing == 0 else None
        ),
        # battle_reports.defender.resource_loss는 함선+방어를 합친 값이므로 정확한 이름을 쓴다.
        "defending_unit_loss": (
            float(defense["loss"] or 0) if defense_missing == 0 else None
        ),
        # 정찰기-only 교전은 약탈 실패가 아니라 정보 수집 비용이다. 별도 관측값으로만 둔다.
        "probe_attack_count": probe_attack_n,
        "probe_defense_count": probe_defense_n,
        "probe_only_count": probe_attack_n + probe_defense_n,
        "espionage_cost": (
            float(probe_attack["loss"] or 0) if probe_missing == 0 else None
        ),
    }


def _debris_metrics(
    con: sqlite3.Connection,
    email: str,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    needed = {"event_ts", "email", "metal", "crystal", "deuterium"}
    if not needed.issubset(_table_columns(con, "debris_harvests")):
        return {
            "status": "unavailable",
            "reason": "debris-harvest-schema-not-deployed",
            "harvest_count": None,
            "debris_income": None,
        }
    fresh, reason = _capability_fresh(con, "debris_harvests", end)
    if not fresh:
        return {
            "status": "unavailable",
            "reason": reason,
            "harvest_count": None,
            "debris_income": None,
        }
    row = con.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN metal IS NULL OR crystal IS NULL OR deuterium IS NULL
                           THEN 1 ELSE 0 END) AS missing,
                  COALESCE(SUM(metal+crystal+deuterium),0) AS income
           FROM debris_harvests
           WHERE email=? AND event_ts>=? AND event_ts<=?""",
        (email, _iso(start), _iso(end)),
    ).fetchone()
    missing = int(row["missing"] or 0)
    return {
        "status": "ready" if missing == 0 else "partial",
        "reason": None if missing == 0 else "one-or-more-harvest-values-missing",
        "harvest_count": int(row["n"]),
        "debris_income": float(row["income"] or 0) if missing == 0 else None,
    }


def _military(
    con: sqlite3.Connection,
    email: str,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    battle = _battle_metrics(con, email, start, end)
    debris = _debris_metrics(con, email, start, end)
    statuses = {battle["status"], debris["status"]}
    status = "ready" if statuses == {"ready"} else (
        "unavailable" if statuses == {"unavailable"} else "partial"
    )
    loot, debris_income = battle["loot_income"], debris["debris_income"]
    attack_loss = battle["attacking_fleet_loss"]
    defense_loss = battle["defending_unit_loss"]
    # Raid performance belongs to outgoing attacks only.  Subtracting damage
    # suffered while defending made a policy look like a failed raider even in
    # intervals where it launched no conventional attack.
    raid_net = None if loot is None or attack_loss is None else loot - attack_loss
    gross = None if loot is None or debris_income is None else loot + debris_income
    net = (
        None
        if gross is None or attack_loss is None or defense_loss is None
        else gross - attack_loss - defense_loss
    )
    return {
        "status": status,
        "battle": battle,
        "debris": debris,
        "raid_net_after_reported_losses": raid_net,
        "harvest_income": debris_income,
        "gross_military_income": gross,
        "net_after_reported_losses": net,
    }


def _economic_verdict(economy: Mapping[str, Any]) -> str:
    relative = economy.get("relative")
    if not isinstance(relative, dict):
        return "unavailable"
    ratio = relative.get("effective_asset_ratio")
    if not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio)):
        return "unavailable"
    if ratio >= 1.05:
        return "positive"
    if ratio <= 0.95:
        return "negative"
    return "neutral"


def _raid_verdict(military: Mapping[str, Any]) -> str:
    net = military.get("raid_net_after_reported_losses")
    battle = military.get("battle")
    if not isinstance(net, (int, float)) or not isinstance(battle, dict):
        return "unavailable"
    outgoing_attacks = int(battle.get("attack_count") or 0)
    if outgoing_attacks == 0:
        # 정찰기-only 보고가 있는 구간은 명시적 neutral이다. 공격 실패로 승격하지 않는다.
        if int(battle.get("probe_only_count") or 0) > 0:
            return "neutral"
        return "no-activity"
    if net > 0:
        return "positive"
    if net < 0:
        return "negative"
    return "neutral"


def _harvest_verdict(military: Mapping[str, Any]) -> str:
    debris = military.get("debris")
    income = military.get("harvest_income")
    if not isinstance(debris, dict) or not isinstance(income, (int, float)):
        return "unavailable"
    if int(debris.get("harvest_count") or 0) == 0:
        return "no-activity"
    if income > 0:
        return "positive"
    if income < 0:
        return "negative"
    return "neutral"


def _unavailable_scorecard(
    email: str,
    reason: str,
    *,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "reason": reason,
        "email": email,
        "policy": policy_view(policy or {}),
        "economy": {"status": "unavailable", "reason": reason},
        "military": {
            "status": "unavailable",
            "battle": {
                "loot_income": None,
                "attacking_fleet_loss": None,
                "defending_unit_loss": None,
            },
            "debris": {"debris_income": None},
            "gross_military_income": None,
            "net_after_reported_losses": None,
        },
        "policy_outcome_pair": {
            "economic_verdict": "unavailable",
            "raid_verdict": "unavailable",
            "harvest_verdict": "unavailable",
            "military_verdict": "unavailable",
        },
        "promoted_lessons": [],
    }


def _latest_completed_activation(
    history_path: pathlib.Path,
    now: dt.datetime,
    grace_seconds: int,
) -> tuple[dict[str, Any] | None, str | None]:
    history = _load_json_dict(history_path)
    if history is None:
        return None, "policy-activation-history-missing-or-invalid"
    activations = history.get("activations")
    if not isinstance(activations, list):
        return None, "policy-activation-history-invalid"
    cutoff = now - dt.timedelta(seconds=max(0, grace_seconds))
    eligible: list[tuple[dt.datetime, dict[str, Any]]] = []
    for item in activations:
        if not isinstance(item, dict):
            continue
        start = _parse_time(item.get("activated_at"))
        end = _parse_time(item.get("deactivated_at"))
        policy = item.get("policy")
        if start is None or end is None or not isinstance(policy, dict):
            continue
        if end > cutoff or (end - start).total_seconds() < MIN_WINDOW_SECONDS:
            continue
        if policy.get("personality") not in KNOWN_PERSONALITIES:
            continue
        eligible.append((end, item))
    if not eligible:
        return None, "no-completed-policy-past-collection-grace"
    return max(eligible, key=lambda pair: pair[0])[1], None


def build_scorecard(
    email: str,
    policy_path: pathlib.Path,
    metrics_db: pathlib.Path = DEFAULT_METRICS_DB,
    *,
    now: dt.datetime | None = None,
    activation_history_path: pathlib.Path | None = None,
    collection_grace_seconds: int = COLLECTION_GRACE_SECONDS,
) -> dict[str, Any]:
    """완료된 정책 활성 구간을 metrics DB의 backfill 완료 데이터로 채점한다."""
    current_policy = _load_json_dict(policy_path)
    now = (now or _utc_now()).astimezone(dt.timezone.utc)
    activation_id: str | None = None
    interval_source = "legacy-updated-at"

    if activation_history_path is not None:
        activation, reason = _latest_completed_activation(
            activation_history_path, now, collection_grace_seconds
        )
        if activation is None:
            result = _unavailable_scorecard(
                email, str(reason), policy=current_policy or {}
            )
            result["collection_grace_seconds"] = collection_grace_seconds
            return result
        policy = activation["policy"]
        start = _parse_time(activation.get("activated_at"))
        end = _parse_time(activation.get("deactivated_at"))
        activation_id = activation.get("activation_id")
        interval_source = "server-activation-history"
        assert start is not None and end is not None
    else:
        # 직접 호출 호환 경로. 라이브 tick은 반드시 활성화 이력을 넘긴다.
        policy = current_policy
        if policy is None:
            return _unavailable_scorecard(email, "previous-policy-missing-or-invalid")
        start = _parse_time(policy.get("updated_at"))
        if start is None:
            return _unavailable_scorecard(
                email, "previous-policy-updated-at-invalid", policy=policy
            )
        if start >= now:
            return _unavailable_scorecard(
                email, "previous-policy-window-not-finished", policy=policy
            )
        ttl = policy.get("ttl_hours", 2)
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
            ttl = 2
        end = min(now, start + dt.timedelta(hours=float(ttl)))
        if (end - start).total_seconds() < MIN_WINDOW_SECONDS:
            return _unavailable_scorecard(
                email, "previous-policy-window-under-20m", policy=policy
            )

    view = policy_view(policy)
    personality = view.get("personality")
    if personality not in KNOWN_PERSONALITIES:
        return _unavailable_scorecard(email, "previous-personality-invalid", policy=policy)
    if not metrics_db.exists():
        return _unavailable_scorecard(email, "metrics-db-missing", policy=policy)

    try:
        with _open_metrics(metrics_db) as con:
            economy = _economy(con, email, str(personality), start, end)
            military = _military(con, email, start, end)
    except sqlite3.Error as exc:
        return _unavailable_scorecard(
            email, f"metrics-db-read-failed:{type(exc).__name__}", policy=policy
        )

    statuses = {economy["status"], military["status"]}
    status = "ready" if statuses == {"ready"} else (
        "unavailable" if statuses == {"unavailable"} else "partial"
    )
    econ_verdict = _economic_verdict(economy)
    raid_verdict = _raid_verdict(military)
    harvest_verdict = _harvest_verdict(military)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": None,
        "email": email,
        "activation_id": activation_id,
        "interval_source": interval_source,
        "collection_grace_seconds": (
            collection_grace_seconds if activation_history_path is not None else 0
        ),
        "interval": {"start": _iso(start), "end": _iso(end)},
        "policy": view,
        "policy_fingerprint": _policy_fingerprint(view),
        "economy": economy,
        "military": military,
        "policy_outcome_pair": {
            "economic_verdict": econ_verdict,
            "raid_verdict": raid_verdict,
            "harvest_verdict": harvest_verdict,
            # 구 소비자 호환용. 이제 군사 학습 판정은 약탈 전투 판정과 동일하다.
            "military_verdict": raid_verdict,
        },
        "promoted_lessons": [],
    }


def _fmt_number(value: Any) -> str:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return "unavailable/NULL"
    return f"{float(value):,.0f}"


def _fmt_ratio(value: Any) -> str:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return "unavailable/NULL"
    return f"{float(value):.2f}x"


def render_prompt(scorecard: Mapping[str, Any]) -> str:
    """모델이 0과 미수집을 혼동하지 않게 명시적인 짧은 프롬프트를 만든다."""
    policy = scorecard.get("policy") if isinstance(scorecard.get("policy"), dict) else {}
    economy = scorecard.get("economy") if isinstance(scorecard.get("economy"), dict) else {}
    military = scorecard.get("military") if isinstance(scorecard.get("military"), dict) else {}
    battle = military.get("battle") if isinstance(military.get("battle"), dict) else {}
    debris = military.get("debris") if isinstance(military.get("debris"), dict) else {}
    relative = economy.get("relative") if isinstance(economy.get("relative"), dict) else {}
    verdict = (
        scorecard.get("policy_outcome_pair")
        if isinstance(scorecard.get("policy_outcome_pair"), dict)
        else {}
    )
    interval = scorecard.get("interval") if isinstance(scorecard.get("interval"), dict) else {}
    lessons = scorecard.get("promoted_lessons")
    if not isinstance(lessons, list):
        lessons = []
    lesson_lines = "\n".join(f"- {str(item)}" for item in lessons[:20]) or "- 아직 없음"
    peer_baseline = (
        economy.get("peer_baseline")
        if isinstance(economy.get("peer_baseline"), dict)
        else {}
    )
    peer_selection = peer_baseline.get(
        "selection", "같은 personality 규칙봇 비교 자료 없음"
    )
    return f"""## AI 자기경험 정책 성적표 (직전 정책 구간)
측정 상태: {scorecard.get('status', 'unavailable')} / 구간: {interval.get('start', 'unavailable')} ~ {interval.get('end', 'unavailable')}
직전 정책(성과와 한 쌍): {json.dumps(policy, ensure_ascii=False, separators=(',', ':'))}

[경제 — 규칙봇 대비: {peer_selection}]
- 원점수 성장 상대치: {_fmt_ratio(relative.get('score_ratio'))}
- 자원건물 레벨 성장 상대치(광산 3종+태양광): {_fmt_ratio(relative.get('resource_building_levels_ratio'))}
- 보유자원 증감 상대치: {_fmt_ratio(relative.get('resource_stock_ratio'))}
- 실질자산 성장 상대치(점수+보유자원/1000): {_fmt_ratio(relative.get('effective_asset_ratio'))}
- 판정: {verdict.get('economic_verdict', 'unavailable')} (상태={economy.get('status', 'unavailable')}, 사유={economy.get('reason') or '없음'})

[약탈 전투 — 정찰기-only 제외]
- 일반 공격/방어 횟수: {_fmt_number(battle.get('attack_count'))} / {_fmt_number(battle.get('defense_count'))}
- 약탈 수입: {_fmt_number(battle.get('loot_income'))}
- 공격 함대 손실: {_fmt_number(battle.get('attacking_fleet_loss'))}
- 방어측 유닛 손실(함선+방어 합): {_fmt_number(battle.get('defending_unit_loss'))}
- 약탈-손실 순값: {_fmt_number(military.get('raid_net_after_reported_losses'))}
- 약탈 판정: {verdict.get('raid_verdict', 'unavailable')} (상태={battle.get('status', 'unavailable')})

[데브리 수거 — 약탈 판정과 분리]
- 실제 수거 횟수/수입: {_fmt_number(debris.get('harvest_count'))} / {_fmt_number(debris.get('debris_income'))}
- 수거 판정: {verdict.get('harvest_verdict', 'unavailable')} (상태={debris.get('status', 'unavailable')})

[정찰 비용 — 공격 실패가 아닌 관측값]
- 정찰기-only 발신/수신 보고: {_fmt_number(battle.get('probe_attack_count'))} / {_fmt_number(battle.get('probe_defense_count'))}
- 내 정찰기 손실 비용: {_fmt_number(battle.get('espionage_cost'))}

[3회 이상 재현되어 승격된 교훈]
{lesson_lines}

	정책 결정 규칙:
- 최상위 목표는 생산력·연구·행성·사용 가능한 함대·실질자산을 포함한 지속 가능한 순성장률이다.
  약탈액·방어량·함선 수 하나만 최대화하지 말고, 손실·연료·기회비용을 뺀 장기 성장을 비교하라.
- 네 페르소나와 personality={policy.get('personality', '현재값')}는 성적 때문에 바꾸지 마라. 같은 스타일 안에서 실제 제어 레버만 유지/수정한다.
- strategy_version 2 이전의 ship_priority/max_total 유지 교훈은 폐기됐다. 고정 함선 상한을
  복제하지 말고 ship_strategy의 재투자율·교리·가치 비율로 현재 기회에 대응하라.
- unavailable/NULL은 0이 아니라 미수집·표본부족이다. 성공/실패 근거로 쓰지 마라.
- 정찰기-only 손실은 정보 수집 비용이며 약탈 실패가 아니다. 약탈 기준이나 전투함 상한을 위축시키는 근거로 쓰지 마라.
- 데브리 수거 성공은 별도 성과다. 이것만으로 공격 정책이 성공했다고 단정하지 마라.
- 한 구간 결과는 가설일 뿐이다. 3회 이상 같은 정책-성과 방향이 재현된 위 승격 교훈만 장기 기억으로 취급한다.
	- 직전 정책과 성적의 인과를 단정하지 말고, 유지할 필드와 바꿀 필드를 note에 한 줄로 명시하라.
	- autonomy/action_plan/targeting/limits는 실제 행동 레버다. personality는 행동 금지 목록이 아니다.
	- 정책은 홈과 모든 식민지에 전달된다. empire 관측에서 에너지·생산이 낮은 행성도 함께 고려하라.
	"""


def _policy_facets(policy: Mapping[str, Any]) -> dict[str, Any]:
    """성과와 반복 대조할 실제 레버 단위 정책 조각.

    note/updated_at과 대부분 무효인 research_priority는 증거 키에서 제외한다. 전체 정책이
    한 글자도 같아야만 재현으로 세면 모델의 사소한 churn 때문에 영원히 승격되지 않으므로,
    카고 구성·공격성·수비비율·에너지처럼 실제 조정 가능한 레버별로 독립 채점한다.
    """
    facets: dict[str, Any] = {}
    for key in (
        "ship_priority", "ship_strategy", "aggression", "defense_ratio",
        "defense_strategy", "fleetsave", "energy_target",
        "autonomy", "action_plan", "targeting", "limits",
    ):
        value = policy.get(key)
        if value not in (None, [], {}):
            facets[key] = value
    build = policy.get("build_priority")
    if isinstance(build, list) and build:
        # build 순서 대부분은 엔진 보호 대상이나 solar 위치는 살아 있는 레버라 함께 관측한다.
        facets["build_priority"] = build
    return facets


RAID_LIMIT_KEYS = (
    "scan_span",
    "spy_global_cooldown_sec",
    "spy_target_cooldown_sec",
    "attack_target_cooldown_sec",
    "bashing_max_attacks",
    "fleet_risk_percent",
    "min_fleet_value_limit",
    "max_attack_eta_sec",
    "reserve_fleet_slots",
)
ECONOMY_ACTIONS = frozenset({
    "build", "research", "ship", "expedition", "colonize", "transfers",
    "development", "production", "develop_colonies",
})
RAID_ACTIONS = frozenset({"spy", "raid", "harvest"})


def _dimension_facets(
    policy: Mapping[str, Any],
    dimension: str,
) -> dict[str, Any]:
    """Project only levers that can plausibly cause the scored dimension."""
    if dimension == "economy":
        facets = {
            key: policy[key]
            for key in ("build_priority", "energy_target", "ship_strategy")
            if policy.get(key) not in (None, [], {})
        }
        actions = policy.get("action_plan")
        if isinstance(actions, list):
            projected = sorted({
                str(action) for action in actions if str(action) in ECONOMY_ACTIONS
            })
            if projected:
                facets["economy_action_plan"] = projected
        return facets
    if dimension == "raid":
        facets = {
            key: policy[key]
            for key in ("aggression", "targeting")
            if policy.get(key) not in (None, [], {})
        }
        limits = policy.get("limits")
        if isinstance(limits, Mapping):
            projected_limits = {
                key: limits[key] for key in RAID_LIMIT_KEYS if key in limits
            }
            if projected_limits:
                facets["raid_limits"] = projected_limits
        actions = policy.get("action_plan")
        if isinstance(actions, list):
            enabled = sorted({
                str(action) for action in actions if str(action) in RAID_ACTIONS
            })
            # An explicit action_plan is itself a control. Preserve an empty
            # raid projection to learn from raid being disabled deliberately.
            facets["raid_action_plan"] = enabled
        return facets
    return {}


def _facet_fingerprint(name: str, value: Any) -> str:
    raw = json.dumps([name, value], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _lesson_text(
    dimension: str,
    direction: str,
    count: int,
    facet_name: str,
    facet_value: Any,
) -> str:
    action = "유지 후보" if direction == "positive" else "수정/회피 후보"
    label = "동급 대비 실질자산 성장" if dimension == "economy" else "약탈 전투 순손익"
    return (
        f"{label} {direction}가 같은 정책에서 {count}회 재현됨 → {action}: "
        f"{facet_name}={json.dumps(facet_value, ensure_ascii=False, separators=(',', ':'))}"
    )


def promoted_lessons_from_observations(
    observations: list[Any],
) -> list[str]:
    """경제·약탈 전투만 장기 교훈으로 승격한다.

    schema v1의 ``military``는 정찰기 손실과 데브리 수거가 섞인 값이므로 의도적으로
    재사용하지 않는다. 과거 경제 관측은 보존하고, 깨끗한 ``raid`` 증거만 새로 쌓는다.
    """
    grouped: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    facet_values: dict[tuple[str, str], Any] = {}
    for item in observations:
        if not isinstance(item, dict) or not isinstance(item.get("policy_fingerprint"), str):
            continue
        policy = item.get("policy")
        if not isinstance(policy, Mapping):
            continue
        for dimension in ("economy", "raid"):
            direction = item.get(dimension)
            if direction not in {"positive", "negative"}:
                continue
            # Recompute from the stored policy rather than trusting legacy
            # ``facets``. Old ledgers paired every lever with every verdict.
            for facet_name, facet_value in _dimension_facets(policy, dimension).items():
                facet_fp = _facet_fingerprint(str(facet_name), facet_value)
                facet_values[(str(facet_name), facet_fp)] = facet_value
                if direction in {"positive", "negative"}:
                    grouped[(str(facet_name), facet_fp, dimension)][str(direction)] += 1

    promoted: list[str] = []
    for (facet_name, facet_fp, dimension), counts in sorted(grouped.items()):
        for direction in ("positive", "negative"):
            opposite = "negative" if direction == "positive" else "positive"
            if counts[direction] >= PROMOTION_REPETITIONS and counts[opposite] == 0:
                promoted.append(
                    _lesson_text(
                        dimension,
                        direction,
                        counts[direction],
                        facet_name,
                        facet_values[(facet_name, facet_fp)],
                    )
                )
    return promoted


def _managed_memory_block(lessons: list[str]) -> str:
    body = "\n".join(f"- {lesson}" for lesson in lessons) if lessons else "- 아직 3회 재현된 교훈 없음"
    return (
        f"{LESSONS_START}\n"
        "## 검증된 반복 교훈 (자동 승격; 동일 정책-성과 3회 이상)\n"
        f"{body}\n"
        f"{LESSONS_END}"
    )


def _update_memory(memory_path: pathlib.Path, lessons: list[str]) -> None:
    try:
        old = memory_path.read_text(encoding="utf-8")
    except OSError:
        old = ""
    block = _managed_memory_block(lessons)
    start = old.find(LESSONS_START)
    end = old.find(LESSONS_END, start + len(LESSONS_START)) if start >= 0 else -1
    if start >= 0 and end >= 0:
        end += len(LESSONS_END)
        new = old[:start] + block + old[end:]
    else:
        # 기존 append-only 일기는 한 글자도 버리지 않고 아래에 보존한다.
        new = block + ("\n\n" + old.lstrip("\n") if old else "\n")
    _atomic_text(memory_path, new)


def update_learning_ledger(
    scorecard: dict[str, Any],
    ledger_path: pathlib.Path,
    memory_path: pathlib.Path,
) -> list[str]:
    """동일 정책/같은 방향 결과를 고유 정책 구간 기준으로 세어 3회에 승격한다."""
    ledger_data = _load_json_dict(ledger_path) or {
        "schema_version": SCHEMA_VERSION, "observations": []
    }
    observations = ledger_data.get("observations")
    if not isinstance(observations, list):
        observations = []
    interval = scorecard.get("interval")
    fingerprint = scorecard.get("policy_fingerprint")
    pair = scorecard.get("policy_outcome_pair")
    military = scorecard.get("military")
    battle = military.get("battle") if isinstance(military, dict) else None
    if isinstance(interval, dict) and isinstance(fingerprint, str) and isinstance(pair, dict):
        activation_token = scorecard.get("activation_id") or interval.get("start")
        observation_id = (
            f"{scorecard.get('email')}|{activation_token}|{fingerprint}"
        )
        if not any(
            isinstance(item, dict) and item.get("id") == observation_id
            for item in observations
        ):
            observations.append({
                "id": observation_id,
                "activation_id": scorecard.get("activation_id"),
                "interval": interval,
                "policy_fingerprint": fingerprint,
                "policy": scorecard.get("policy", {}),
                "facets": _policy_facets(scorecard.get("policy", {})),
                "dimension_facets": {
                    dimension: _dimension_facets(
                        scorecard.get("policy", {}), dimension
                    )
                    for dimension in ("economy", "raid")
                },
                "economy": pair.get("economic_verdict", "unavailable"),
                "raid": pair.get("raid_verdict", "unavailable"),
                "harvest": pair.get("harvest_verdict", "unavailable"),
                "military": pair.get("raid_verdict", "unavailable"),
                "evidence": {
                    "probe_only_count": (
                        battle.get("probe_only_count") if isinstance(battle, dict) else None
                    ),
                    "espionage_cost": (
                        battle.get("espionage_cost") if isinstance(battle, dict) else None
                    ),
                },
                "observed_at": interval.get("end"),
            })
    observations = observations[-LEDGER_MAX_OBSERVATIONS:]
    promoted = promoted_lessons_from_observations(observations)
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "promotion_rule": (
            "same dimension-relevant policy facet + same economy/raid direction >=3, "
            "no opposite evidence; legacy combined/cross-domain evidence excluded"
        ),
        "observations": observations,
        "promoted_lessons": promoted,
    }
    cleanup = ledger_data.get("last_contamination_cleanup")
    if isinstance(cleanup, dict):
        ledger["last_contamination_cleanup"] = cleanup
        cleaned_at = _parse_time(cleanup.get("cleaned_at"))
        clean_count = sum(
            1
            for item in observations
            if isinstance(item, dict)
            and isinstance(item.get("activation_id"), str)
            and cleaned_at is not None
            and (_parse_time(item.get("observed_at")) or dt.datetime.min.replace(
                tzinfo=dt.timezone.utc
            )) >= cleaned_at
        )
        ledger["post_cleanup_observation_count"] = clean_count
        ledger["post_cleanup_observation_target"] = 4
    _atomic_json(ledger_path, ledger)
    _update_memory(memory_path, promoted)
    return promoted


def generate_artifacts(
    email: str,
    policy_path: pathlib.Path,
    metrics_db: pathlib.Path,
    scorecard_path: pathlib.Path,
    prompt_path: pathlib.Path,
    ledger_path: pathlib.Path,
    memory_path: pathlib.Path,
    *,
    env: Mapping[str, str] | None = None,
    now: dt.datetime | None = None,
    activation_history_path: pathlib.Path | None = None,
    collection_grace_seconds: int = COLLECTION_GRACE_SECONDS,
) -> dict[str, Any]:
    enabled, reason = gate_status(env)
    if not enabled:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "reason": reason,
            "email": email,
        }
        _atomic_json(scorecard_path, result)
        _atomic_text(prompt_path, "")
        return result

    scorecard = build_scorecard(
        email,
        policy_path,
        metrics_db,
        now=now,
        activation_history_path=activation_history_path,
        collection_grace_seconds=collection_grace_seconds,
    )
    if scorecard.get("interval"):
        scorecard["promoted_lessons"] = update_learning_ledger(
            scorecard, ledger_path, memory_path
        )
    _atomic_json(scorecard_path, scorecard)
    _atomic_text(prompt_path, render_prompt(scorecard))
    return scorecard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--metrics-db", type=pathlib.Path, default=DEFAULT_METRICS_DB)
    parser.add_argument("--scorecard", type=pathlib.Path, required=True)
    parser.add_argument("--prompt", type=pathlib.Path, required=True)
    parser.add_argument("--ledger", type=pathlib.Path, required=True)
    parser.add_argument("--memory", type=pathlib.Path, required=True)
    parser.add_argument("--activation-history", type=pathlib.Path)
    parser.add_argument(
        "--collection-grace-seconds", type=int, default=COLLECTION_GRACE_SECONDS
    )
    args = parser.parse_args()
    result = generate_artifacts(
        args.email,
        args.policy,
        args.metrics_db,
        args.scorecard,
        args.prompt,
        args.ledger,
        args.memory,
        activation_history_path=args.activation_history,
        collection_grace_seconds=args.collection_grace_seconds,
    )
    print(f"[scorecard] {result.get('status')}: {result.get('reason') or 'ok'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
