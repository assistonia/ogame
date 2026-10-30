#!/usr/bin/env python3
"""AI 10개 계정의 행성 군집을 규칙봇 구간에 고르게 재배치한다.

기본 실행은 읽기 전용 dry-run이다. ``--apply``는 다음 안전조건을 모두 강제한다.

* 유니버스 루프가 정지해 있어야 한다.
* 목적지는 실제 행성뿐 아니라 진행 중 식민 미션과도 충돌하지 않아야 한다.
* AI 계정의 모든 일반 행성을 같은 목표 시스템으로 옮겨 주행성-식민지 군집을 보존한다.
* 활성 함대의 planet_id_from/to 좌표 스냅샷을 새 좌표로 함께 갱신한다.
* 좌표가 무효가 된 정찰보고는 역사/메시지 외래키 때문에 보존하고, 로컬 scan cache와
  pending preflight만 만료시켜 새 좌표에서 재정찰하게 한다.
* pending battle_guard와 레이더 공격 이력은 동일 행성을 따라 새 좌표로 옮긴다.
* 변경 전 DB 행은 ``ops_ai_rebalance_<timestamp>_*`` 테이블에 남겨 롤백 가능하게 한다.

관리자 비밀번호는 코드/인자에 두지 않고 ``OGAME_DB_ADMIN_PASSWORD`` 환경변수로만 받는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import pymysql


PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from bots.shared import db_reader  # noqa: E402


TARGET_SYSTEMS = {
    "ai01@bots.local": 2,
    "ai02@bots.local": 6,
    "ai03@bots.local": 10,
    "ai04@bots.local": 14,
    "ai05@bots.local": 18,
    "ai06@bots.local": 22,
    "ai07@bots.local": 26,
    "ai08@bots.local": 30,
    # 34는 규칙봇 행성으로 4~12 슬롯이 가득 차 35를 쓴다.
    "ai09@bots.local": 35,
    "ai10@bots.local": 38,
}
SLOT_PREFERENCE = (8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)
RAIDER_STATE_DIR = PROJECT / "bots" / "rule_based" / ".raider_state"
GUARD_STATE_DIR = PROJECT / "bots" / "rule_based" / ".battle_guard_state"
BACKUP_ROOT = PROJECT / "scripts" / "backups"


@dataclass(frozen=True)
class PlanetRow:
    planet_id: int
    email: str
    name: str
    planet_type: int
    galaxy: int
    system: int
    position: int


@dataclass(frozen=True)
class Move:
    planet_id: int
    email: str
    name: str
    old_galaxy: int
    old_system: int
    old_position: int
    new_galaxy: int
    new_system: int
    new_position: int

    @property
    def old_coord(self) -> tuple[int, int, int]:
        return self.old_galaxy, self.old_system, self.old_position

    @property
    def new_coord(self) -> tuple[int, int, int]:
        return self.new_galaxy, self.new_system, self.new_position


def _rows(cur) -> list[PlanetRow]:
    cur.execute(
        "SELECT p.id planet_id,u.email,p.name,p.planet_type,p.galaxy,p.system,"
        "p.planet position FROM planets p JOIN users u ON u.id=p.user_id"
    )
    return [PlanetRow(
        planet_id=int(row["planet_id"]), email=str(row["email"]),
        name=str(row["name"]), planet_type=int(row["planet_type"]),
        galaxy=int(row["galaxy"]), system=int(row["system"]),
        position=int(row["position"]),
    ) for row in cur.fetchall()]


def _colonisation_reservations(cur) -> set[tuple[int, int, int]]:
    cur.execute(
        "SELECT galaxy_to,system_to,position_to FROM fleet_missions "
        "WHERE processed=0 AND canceled=0 AND mission_type=7"
    )
    return {
        (int(row["galaxy_to"]), int(row["system_to"]), int(row["position_to"]))
        for row in cur.fetchall()
    }


def plan_moves(
    planets: Iterable[PlanetRow],
    *,
    reservations: set[tuple[int, int, int]] | None = None,
) -> list[Move]:
    """현재 점유를 보존하면서 AI별 목표 시스템의 빈 슬롯을 결정한다."""
    all_rows = list(planets)
    ai_rows = [row for row in all_rows if row.email in TARGET_SYSTEMS]
    found = {row.email for row in ai_rows}
    missing = set(TARGET_SYSTEMS) - found
    if missing:
        raise RuntimeError(f"AI accounts missing: {sorted(missing)}")
    unsupported = [row for row in ai_rows if row.planet_type != 1]
    if unsupported:
        raise RuntimeError("AI moon/non-planet relocation is not supported")

    # 모든 AI 행성은 빠져나간다고 보고 목적지를 동시에 계산한다. 규칙봇/사람 행성과
    # 진행 중 식민선 목적지만 고정 점유로 남긴다.
    occupied = {
        (row.galaxy, row.system, row.position)
        for row in all_rows if row.email not in TARGET_SYSTEMS
    }
    occupied.update(reservations or set())

    moves: list[Move] = []
    for email, target_system in TARGET_SYSTEMS.items():
        account = sorted(
            (row for row in ai_rows if row.email == email),
            key=lambda row: (row.name.lower() != "homeworld", row.planet_id),
        )
        for row in account:
            choices = (row.position,) + tuple(
                slot for slot in SLOT_PREFERENCE if slot != row.position
            )
            new_coord = next(
                (
                    (1, target_system, slot)
                    for slot in choices
                    if (1, target_system, slot) not in occupied
                ),
                None,
            )
            if new_coord is None:
                raise RuntimeError(f"no free slot for {email} in 1:{target_system}")
            occupied.add(new_coord)
            moves.append(Move(
                planet_id=row.planet_id, email=email, name=row.name,
                old_galaxy=row.galaxy, old_system=row.system, old_position=row.position,
                new_galaxy=new_coord[0], new_system=new_coord[1], new_position=new_coord[2],
            ))
    return moves


def _connect(*, apply: bool):
    if not apply:
        return db_reader._connect()
    password = os.environ.get("OGAME_DB_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("OGAME_DB_ADMIN_PASSWORD is required with --apply")
    return pymysql.connect(
        host=db_reader.DB_HOST,
        port=db_reader.DB_PORT,
        user=os.environ.get("OGAME_DB_ADMIN_USER", "root"),
        password=password,
        database=db_reader.DB_NAME,
        connect_timeout=5,
        read_timeout=15,
        write_timeout=15,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _universe_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "[r]un_universe.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return result.returncode == 0


def _coord_where(column_prefix: str, coords: list[tuple[int, int, int]]) -> tuple[str, list[int]]:
    terms: list[str] = []
    params: list[int] = []
    for galaxy, system, position in coords:
        terms.append(
            f"({column_prefix}galaxy=%s AND {column_prefix}system=%s "
            f"AND {column_prefix}position=%s)"
        )
        params.extend((galaxy, system, position))
    return " OR ".join(terms) or "0=1", params


def _create_db_backups(
    conn, moves: list[Move], stamp: str, *, prefix_base: str = "ops_ai_rebalance",
) -> str:
    prefix = f"{prefix_base}_{stamp}"
    ids = [move.planet_id for move in moves if move.old_coord != move.new_coord]
    placeholders = ",".join(["%s"] * len(ids))
    changed_coords = sorted({
        coord for move in moves if move.old_coord != move.new_coord
        for coord in (move.old_coord, move.new_coord)
    })
    spy_where, spy_params = _coord_where("planet_", changed_coords)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE `{prefix}_planets` AS SELECT * FROM planets "
            f"WHERE id IN ({placeholders})", ids,
        )
        cur.execute(
            f"CREATE TABLE `{prefix}_fleets` AS SELECT * FROM fleet_missions "
            f"WHERE processed=0 AND (planet_id_from IN ({placeholders}) "
            f"OR planet_id_to IN ({placeholders}))", ids + ids,
        )
        cur.execute(
            f"CREATE TABLE `{prefix}_espionage` AS SELECT * FROM espionage_reports "
            f"WHERE {spy_where}", spy_params,
        )
        cur.execute(
            f"CREATE TABLE `{prefix}_moves` ("
            "planet_id BIGINT PRIMARY KEY,email VARCHAR(255),name VARCHAR(255),"
            "old_galaxy INT,old_system INT,old_position INT,"
            "new_galaxy INT,new_system INT,new_position INT)"
        )
        cur.executemany(
            f"INSERT INTO `{prefix}_moves` VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(
                move.planet_id, move.email, move.name,
                *move.old_coord, *move.new_coord,
            ) for move in moves if move.old_coord != move.new_coord],
        )
    return prefix


def _apply_db(
    conn, moves: list[Move], stamp: str, *, prefix_base: str = "ops_ai_rebalance",
) -> dict[str, Any]:
    changed = [move for move in moves if move.old_coord != move.new_coord]
    prefix = _create_db_backups(conn, moves, stamp, prefix_base=prefix_base)
    changed_coords = sorted({
        coord for move in changed for coord in (move.old_coord, move.new_coord)
    })
    spy_where, spy_params = _coord_where("planet_", changed_coords)

    conn.begin()
    try:
        with conn.cursor() as cur:
            # 목적지/원본을 포함한 전체 행성을 잠가 dry-run 뒤 새 점유가 끼어들지 못하게 한다.
            cur.execute("SELECT id FROM planets FOR UPDATE")
            cur.fetchall()
            cur.execute(
                "SELECT id FROM fleet_missions WHERE processed=0 FOR UPDATE"
            )
            cur.fetchall()

            # 유일키 충돌 없는 two-phase 이동. galaxy=99는 트랜잭션 안에서만 쓰인다.
            for move in changed:
                cur.execute(
                    "UPDATE planets SET galaxy=99,system=%s,planet=1 WHERE id=%s",
                    (move.planet_id, move.planet_id),
                )
            for move in changed:
                cur.execute(
                    "UPDATE planets SET galaxy=%s,system=%s,planet=%s WHERE id=%s",
                    (*move.new_coord, move.planet_id),
                )
                cur.execute(
                    "UPDATE fleet_missions SET galaxy_from=%s,system_from=%s,position_from=%s "
                    "WHERE processed=0 AND planet_id_from=%s",
                    (*move.new_coord, move.planet_id),
                )
                cur.execute(
                    "UPDATE fleet_missions SET galaxy_to=%s,system_to=%s,position_to=%s "
                    "WHERE processed=0 AND planet_id_to=%s",
                    (*move.new_coord, move.planet_id),
                )

            # espionage_reports는 messages가 FK로 참조한다. 역사 보고를 지우거나 시각을
            # 변조하지 않고 그대로 보존한다. old 좌표는 이제 빈 슬롯이라 scan cache를
            # 만료하면 타겟 후보에서 사라지고, new 좌표에는 보고가 없어 자동 재정찰된다.
            cur.execute(
                f"SELECT COUNT(*) n FROM espionage_reports WHERE {spy_where}", spy_params
            )
            retained_stale_reports = int(cur.fetchone()["n"])

            # planet_id가 가리키는 좌표와 active mission snapshot이 모두 같아야 한다.
            ids = [move.planet_id for move in changed]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                "SELECT COUNT(*) n FROM fleet_missions fm JOIN planets p "
                "ON p.id=fm.planet_id_from WHERE fm.processed=0 "
                f"AND p.id IN ({placeholders}) AND "
                "(fm.galaxy_from<>p.galaxy OR fm.system_from<>p.system "
                "OR fm.position_from<>p.planet)", ids,
            )
            bad_from = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT COUNT(*) n FROM fleet_missions fm JOIN planets p "
                "ON p.id=fm.planet_id_to WHERE fm.processed=0 "
                f"AND p.id IN ({placeholders}) AND "
                "(fm.galaxy_to<>p.galaxy OR fm.system_to<>p.system "
                "OR fm.position_to<>p.planet)", ids,
            )
            bad_to = int(cur.fetchone()["n"])
            if bad_from or bad_to:
                raise RuntimeError(f"active mission mismatch from={bad_from} to={bad_to}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "backup_prefix": prefix,
        "changed_planets": len(changed),
        "retained_historical_spy_reports": retained_stale_reports,
    }


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, path.stat().st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o600)
    tmp.replace(path)


def _backup_local(path: pathlib.Path, backup_dir: pathlib.Path) -> None:
    relative = path.relative_to(PROJECT)
    destination = backup_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def _migrate_local_state(
    moves: list[Move], stamp: str, *, backup_namespace: str = "ai_position_rebalance",
) -> dict[str, int]:
    changed = [move for move in moves if move.old_coord != move.new_coord]
    coord_map = {
        f"{move.old_galaxy}:{move.old_system}:{move.old_position}":
        f"{move.new_galaxy}:{move.new_system}:{move.new_position}"
        for move in changed
    }
    tuple_map = {move.old_coord: move.new_coord for move in changed}
    backup_dir = BACKUP_ROOT / f"{backup_namespace}_{stamp}"
    counts = {"raider_states": 0, "guard_states": 0, "scan_caches": 0}

    for path in sorted(RAIDER_STATE_DIR.glob("*.json")):
        if path.name.startswith("scan_cache_"):
            match = re.fullmatch(r"scan_cache_(\d+)_(\d+)\.json", path.name)
            if match and (int(match.group(1)), int(match.group(2))) in {
                (coord[0], coord[1]) for coord in (*tuple_map.keys(), *tuple_map.values())
            }:
                os.utime(path, (1, 1))
                counts["scan_caches"] += 1
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        targets = state.get("targets")
        if not isinstance(targets, dict):
            continue
        touched = False
        for old_key, new_key in coord_map.items():
            old = targets.pop(old_key, None)
            if not isinstance(old, dict):
                continue
            # 공격/배싱 이력은 같은 행성을 따라가고, intel/preflight/harvest는 좌표가
            # 바뀌었으므로 폐기한다.
            preserved = {
                key: old[key] for key in ("attack", "attacks") if key in old
            }
            if preserved:
                destination = targets.setdefault(new_key, {})
                if isinstance(destination, dict):
                    destination.update(preserved)
            touched = True
        if touched:
            _backup_local(path, backup_dir)
            _atomic_json(path, state)
            counts["raider_states"] += 1

    for path in sorted(GUARD_STATE_DIR.glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        touched = False
        pending = state.get("pending")
        if isinstance(pending, list):
            for row in pending:
                if not isinstance(row, dict):
                    continue
                target = row.get("target")
                if isinstance(target, list) and len(target) == 3:
                    old = tuple(int(value) for value in target)
                    if old in tuple_map:
                        row["target"] = list(tuple_map[old])
                        touched = True
        if touched:
            _backup_local(path, backup_dir)
            _atomic_json(path, state)
            counts["guard_states"] += 1
    return counts


def _active_mission_count(cur, planet_ids: list[int]) -> int:
    if not planet_ids:
        return 0
    placeholders = ",".join(["%s"] * len(planet_ids))
    cur.execute(
        "SELECT COUNT(*) n FROM fleet_missions WHERE processed=0 AND canceled=0 "
        f"AND (planet_id_from IN ({placeholders}) OR planet_id_to IN ({placeholders}))",
        planet_ids + planet_ids,
    )
    return int(cur.fetchone()["n"])


def build_report(conn) -> tuple[list[Move], dict[str, Any]]:
    with conn.cursor() as cur:
        moves = plan_moves(_rows(cur), reservations=_colonisation_reservations(cur))
        changed = [move for move in moves if move.old_coord != move.new_coord]
        active = _active_mission_count(cur, [move.planet_id for move in changed])
    homes = {
        move.email: move.new_system
        for move in moves if move.name.lower() == "homeworld"
    }
    return moves, {
        "changed_planets": len(changed),
        "active_missions_touched": active,
        "home_systems_after": homes,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and _universe_running():
        print("REFUSED: run_universe.py is still running", file=sys.stderr)
        return 2
    conn = _connect(apply=args.apply)
    try:
        moves, summary = build_report(conn)
        result: dict[str, Any] = {
            "mode": "apply" if args.apply else "dry-run",
            **summary,
            "moves": [asdict(move) for move in moves if move.old_coord != move.new_coord],
        }
        if args.apply:
            if summary["changed_planets"]:
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                result.update(_apply_db(conn, moves, stamp))
                result["local_state"] = _migrate_local_state(moves, stamp)
            else:
                result.update({"backup_prefix": None, "local_state": {}, "no_op": True})
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"{result['mode']} changed={result['changed_planets']} "
                f"active_missions={result['active_missions_touched']}"
            )
            for move in moves:
                if move.old_coord == move.new_coord:
                    continue
                print(
                    f"{move.email} pid={move.planet_id} {move.name}: "
                    f"{move.old_galaxy}:{move.old_system}:{move.old_position} -> "
                    f"{move.new_galaxy}:{move.new_system}:{move.new_position}"
                )
            if args.apply:
                print(f"backup={result['backup_prefix']} local={result['local_state']}")
        return 0
    finally:
        # db_reader의 thread-local read connection은 다른 호출도 재사용하므로 dry-run에서는
        # 닫지 않는다. apply 전용 admin connection만 명시적으로 닫는다.
        if args.apply:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
