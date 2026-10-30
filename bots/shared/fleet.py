"""OGameX 봇 함대 파견 / 은하 정찰 모듈.

`OGameClient` 인스턴스(로그인 완료 상태)를 받아 동작하는 함수 모음이다.
client.py 는 건드리지 않고, 그 안의 안정적인 속성만 사용한다:
  - client.base_url  (문자열, 끝 슬래시 없음)
  - client.session   (로그인 쿠키가 담긴 requests.Session)

제공 기능
  - scan_galaxy(client, galaxy, system)            은하 화면 파싱 (좌표/플레이어/비활성/달/잔해)
  - send_fleet(client, mission_type, target, ...)  함대 파견 (check-target -> send-fleet 2단계)
  - spy / attack / transport / colonize / deploy / harvest   편의 래퍼
  - send_mini_fleet(client, mission_type, target)  은하 화면 단축버튼(정찰/수거) 방식 파견
  - fleet_events(client)                           진행 중 함대 이동 조회

미션 타입 번호 근거: app/GameMissions/*.php 의 `protected static int $typeId`
함선 technologyId 근거: app/GameObjects/{Civil,Military}ShipObjects.php 의 `$obj->id`
행성 타입 근거: app/Models/Enums/PlanetType.php
파견 payload 형식 근거: FleetController::dispatchSendFleet / dispatchCheckTarget 및
  프론트 FleetDispatcher(resources/js/ingame/*.js)의 appendXxxParams()
"""
from __future__ import annotations

import re

# ---------- 미션 타입 (app/GameMissions/*.php $typeId) ----------
MISSION_ATTACK = 1            # AttackMission
MISSION_ACS_ATTACK = 2       # (연합 공격; check-target 에서 union 지정 시에만 활성)
MISSION_TRANSPORT = 3        # TransportMission
MISSION_DEPLOYMENT = 4       # DeploymentMission (자기 행성 주둔/이동)
MISSION_ACS_DEFEND = 5       # AcsDefendMission
MISSION_ESPIONAGE = 6        # EspionageMission (정찰 - 정찰위성 필수)
MISSION_COLONISATION = 7     # ColonisationMission
MISSION_RECYCLE = 8          # RecycleMission (잔해 수거)
MISSION_MOON_DESTRUCTION = 9  # MoonDestructionMission
MISSION_MISSILE = 10         # MissileMission
MISSION_EXPEDITION = 15      # ExpeditionMission

# ---------- 함선 technologyId (app/GameObjects/*ShipObjects.php) ----------
SHIP_SMALL_CARGO = 202
SHIP_LARGE_CARGO = 203
SHIP_LIGHT_FIGHTER = 204
SHIP_HEAVY_FIGHTER = 205
SHIP_CRUISER = 206
SHIP_BATTLESHIP = 207
SHIP_COLONY_SHIP = 208
SHIP_RECYCLER = 209
SHIP_ESPIONAGE_PROBE = 210
SHIP_BOMBER = 211
SHIP_SOLAR_SATELLITE = 212
SHIP_DESTROYER = 213
SHIP_DEATHSTAR = 214
SHIP_BATTLECRUISER = 215
SHIP_CRAWLER = 217
SHIP_REAPER = 218
SHIP_PATHFINDER = 219

# ---------- 행성 타입 (app/Models/Enums/PlanetType.php) ----------
PLANET_TYPE_PLANET = 1
PLANET_TYPE_DEBRIS = 2
PLANET_TYPE_MOON = 3

# 은하 화면에서 빈 슬롯(Deep space)의 playerId (GalaxyController::createEmptySpaceRow)
_DEEP_SPACE_PLAYER_ID = 99999

_CSRF_META_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
_COORD_RE = re.compile(r"\[(\d+):(\d+):(\d+)\]")


# ======================================================================
# 내부 HTTP 헬퍼
# ======================================================================

def _csrf(client) -> str:
    """인게임 meta 태그의 CSRF 토큰. Laravel 세션 토큰이라 세션 내내 고정이므로 캐시한다.

    (프론트는 이 값을 모든 ajax 요청에 X-CSRF-TOKEN 헤더로 실어 보낸다 -
     resources/views/ingame/layouts/main.blade.php 의 $.ajaxSetup 참고)
    """
    tok = getattr(client, "_fleet_csrf", None)
    if tok:
        return tok
    # OGameClient already extracted the same Laravel session token while validating
    # its overview session.  Reuse it across economy/fleet helpers instead of rendering
    # /galaxy solely to rediscover an identical meta value.
    shared = getattr(client, "_token", None)
    if isinstance(shared, str) and shared:
        client._fleet_csrf = shared
        return shared
    for path in ("/galaxy", "/overview"):
        html = client.session.get(f"{client.base_url}{path}").text
        m = _CSRF_META_RE.search(html)
        if m:
            client._fleet_csrf = m.group(1)
            return client._fleet_csrf
    raise RuntimeError("CSRF meta token not found (로그인 여부 확인)")


def _post(client, path: str, data: dict):
    """게임 ajax POST. CSRF 는 헤더로 검증되고, body 의 token/_token 은 프론트 관례상 함께 실어준다.

    JSON 응답이면 dict 를, 아니면 requests.Response 를 돌려준다.
    응답의 newAjaxToken 이 진짜 세션 토큰(40자)이면 캐시를 갱신한다
    (check-target 은 32자 더미를 주므로 무시된다).
    """
    for attempt in range(2):
        token = _csrf(client)
        body = {"_token": token, "token": token}
        body.update(data)
        r = client.session.post(
            f"{client.base_url}{path}",
            data=body,
            headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": token},
        )

        login_checker = getattr(client, "_is_login_response", None)
        login_response = bool(callable(login_checker) and login_checker(r))
        auth_rejected = r.status_code in (401, 419) or login_response
        if auth_rejected:
            if attempt == 0:
                # Auth and CSRF middleware reject before check-target/send-fleet
                # reaches its controller.  This is the only POST failure class that
                # is safe to replay; transport errors and 5xx remain single-shot.
                client._recover_auth(
                    force_relogin=r.status_code == 401 or login_response
                )
                continue
            raise RuntimeError(f"fleet POST authentication failed ({r.status_code})")

        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            return r
        nt = payload.get("newAjaxToken") if isinstance(payload, dict) else None
        if isinstance(nt, str) and len(nt) == 40:
            client._fleet_csrf = nt
        return payload

    raise RuntimeError("fleet POST authentication retry budget exhausted")


def _get(client, path: str):
    token = _csrf(client)
    return client.session.get(
        f"{client.base_url}{path}",
        headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": token},
    )


# ======================================================================
# 파라미터 정규화
# ======================================================================

def _normalize_speed(speed) -> float:
    """서버는 speed 를 1.0~10.0 사이 0.5 단위 값으로 받는다 (1=10%, 10=100%).

    100 처럼 퍼센트로 넘겨도 받아준다.
    """
    s = float(speed)
    if s > 10:          # 100 같은 퍼센트 표기
        s = s / 10.0
    s = round(s * 2) / 2
    return max(1.0, min(10.0, s))


def _normalize_resources(resources):
    """(metal, crystal, deuterium) 튜플로 정규화. dict/tuple/None 모두 허용."""
    if resources is None:
        return 0, 0, 0
    if isinstance(resources, dict):
        return (
            int(resources.get("metal", 0)),
            int(resources.get("crystal", 0)),
            int(resources.get("deuterium", 0)),
        )
    vals = list(resources) + [0, 0, 0]
    return int(vals[0]), int(vals[1]), int(vals[2])


def _ship_params(ships: dict) -> dict:
    """{technologyId: amount} -> {"am<id>": amount} (수량 0 이하는 제외)."""
    return {f"am{int(tid)}": int(amt) for tid, amt in ships.items() if int(amt) > 0}


# ======================================================================
# 1) 은하 정찰
# ======================================================================

def _debris_summary(node: dict) -> dict:
    res = node.get("resources", {}) or {}
    def amt(k):
        return (res.get(k, {}) or {}).get("amount", 0)
    return {
        "metal": amt("metal"),
        "crystal": amt("crystal"),
        "deuterium": amt("deuterium"),
        "required_recyclers": node.get("requiredShips"),
    }


def _moon_summary(node: dict) -> dict:
    return {
        "planet_id": node.get("planetId"),
        "name": node.get("planetName"),
        "size": node.get("size"),
        "available_missions": sorted(
            {m.get("missionType") for m in (node.get("availableMissions") or [])
             if m.get("missionType") is not None}
        ),
    }


def scan_galaxy(client, galaxy: int, system: int) -> list[dict]:
    """은하 galaxy:system 의 슬롯을 스캔해 '점유된' 행성 목록을 돌려준다.

    빈 우주(Deep space) 슬롯은 제외한다.
    각 항목(dict) 필드:
      galaxy, system, position
      player_id, player_name, inactive, long_inactive, vacation,
      admin, newbie, strong, banned
      planet_id, planet_name, planet_type(=1), activity
      has_moon, moon(dict|None), debris(dict|None)
      available_missions(list[int])   # 이 행성으로 보낼 수 있는 미션타입 (galaxy 화면 기준)

    근거: GalaxyController::ajax() -> system.galaxyContent (행 리스트),
          createPlanetRow / getPlayerInfo / getAvailableMissions
    """
    resp = _post(client, "/ajax/galaxy", {"galaxy": int(galaxy), "system": int(system)})
    if not isinstance(resp, dict) or not resp.get("success"):
        raise RuntimeError(f"galaxy scan failed: {str(resp)[:200]}")

    rows = (resp.get("system") or {}).get("galaxyContent") or []
    out: list[dict] = []
    for row in rows:
        player = row.get("player") or {}
        pid = player.get("playerId")
        planets = row.get("planets") or []
        if pid in (None, _DEEP_SPACE_PLAYER_ID) or not planets:
            continue  # 빈 슬롯

        main = None
        moon = None
        debris = None
        for p in planets:
            pt = p.get("planetType")
            if pt == PLANET_TYPE_PLANET and main is None:
                main = p
            elif pt == PLANET_TYPE_MOON:
                moon = p
            elif pt == PLANET_TYPE_DEBRIS:
                debris = p
        if main is None:
            main = planets[0]

        out.append({
            "galaxy": int(galaxy),
            "system": int(system),
            "position": row.get("position"),
            "player_id": pid,
            "player_name": player.get("playerName"),
            "inactive": bool(player.get("isInactive")),
            "long_inactive": bool(player.get("isLongInactive")),
            "vacation": bool(player.get("isOnVacation")),
            "admin": bool(player.get("isAdmin")),
            "newbie": bool(player.get("isNewbie")),
            "strong": bool(player.get("isStrong")),
            "banned": bool(player.get("isBanned")),
            "planet_id": main.get("planetId"),
            "planet_name": main.get("planetName"),
            "planet_type": main.get("planetType"),
            "activity": main.get("activity"),
            "has_moon": moon is not None,
            "moon": _moon_summary(moon) if moon else None,
            "debris": _debris_summary(debris) if debris else None,
            "available_missions": sorted(
                {m.get("missionType") for m in (main.get("availableMissions") or [])
                 if m.get("missionType") is not None}
            ),
        })
    return out


# ======================================================================
# 2) 함대 파견
# ======================================================================

def check_target(client, target, ships=None, target_type=PLANET_TYPE_PLANET, union=0) -> dict:
    """파견 전 대상 검증 (FleetController::dispatchCheckTarget).

    반환 dict 주요 필드:
      status ('success'|'failure'), targetInhabited, targetPlayerName,
      orders {missionType: bool}  # 가능한 미션, errors [...]
    (newAjaxToken 은 더미값이라 신뢰하지 않는다)
    """
    g, s, pos = target
    data = {
        "galaxy": int(g), "system": int(s), "position": int(pos),
        "type": int(target_type), "union": int(union),
    }
    if ships:
        data.update(_ship_params(ships))
    resp = _post(client, "/ajax/fleet/dispatch/check-target", data)
    return resp if isinstance(resp, dict) else {"status": "failure", "raw": resp}


def send_fleet(client, mission_type, target, ships, resources=None, speed=10,
               holding_hours=0, target_type=PLANET_TYPE_PLANET, union=0,
               check=True) -> dict:
    """함대 파견. FleetController::dispatchSendFleet 흐름을 그대로 재현한다.

    인자:
      mission_type : MISSION_* 상수
      target       : (galaxy, system, position)
      ships        : {technologyId: amount}  (예: {SHIP_LIGHT_FIGHTER: 50})
      resources    : (metal, crystal, deuterium) 또는 {"metal":..}; 수송/약탈 적재량
      speed        : 10=100% (1~10, 0.5 단위) — 100 처럼 퍼센트로 줘도 됨
      holding_hours: 원정(15)/ACS방어(5) 대기 시간
      target_type  : 1=행성 2=잔해 3=달
      union        : ACS union id (없으면 0)
      check        : True 면 파견 전 check-target 로 미션 가능여부 사전검증

    반환: {"ok": bool, "message": str, "raw": dict}

    payload 필드 근거: FleetController::dispatchSendFleet 의 주석 예시 및
      프론트 submitFleet2()/appendXxxParams() (am<id>, metal/crystal/deuterium,
      prioMetal.., mission, speed, holdingtime, union ...)
    """
    g, s, pos = target
    ships_p = _ship_params(ships)
    if not ships_p:
        return {"ok": False, "message": "no ships specified", "raw": {}}

    base = {"galaxy": int(g), "system": int(s), "position": int(pos), "type": int(target_type)}

    if check:
        chk = check_target(client, target, ships=ships, target_type=target_type, union=union)
        orders = chk.get("orders") or {}
        allowed = orders.get(str(mission_type))
        if allowed is None:
            allowed = orders.get(mission_type)
        if allowed is False:
            errs = "; ".join(
                e.get("message", "") for e in (chk.get("errors") or []) if isinstance(e, dict)
            )
            return {
                "ok": False,
                "message": f"mission {mission_type} not allowed for target"
                           + (f" ({errs})" if errs else ""),
                "raw": chk,
            }

    metal, crystal, deut = _normalize_resources(resources)
    params = {
        **base,
        **ships_p,
        "metal": metal, "crystal": crystal, "deuterium": deut,
        "prioMetal": 2, "prioCrystal": 3, "prioDeuterium": 4, "prioFood": 1,
        "mission": int(mission_type),
        "speed": _normalize_speed(speed),
        "retreatAfterDefenderRetreat": 0,
        "lootFoodOnAttack": 0,
        "union": int(union),
        "holdingtime": int(holding_hours),
    }
    resp = _post(client, "/ajax/fleet/dispatch/send-fleet", params)
    if not isinstance(resp, dict):
        return {"ok": False, "message": "non-json response", "raw": {}}

    if resp.get("success") is True:
        return {"ok": True, "message": resp.get("message", "sent"), "raw": resp}
    errs = resp.get("errors") or []
    msg = "; ".join(e.get("message", "") for e in errs if isinstance(e, dict)) or "send failed"
    return {"ok": False, "message": msg, "raw": resp}


def send_mini_fleet(client, mission_type, target, ship_count=1,
                    target_type=PLANET_TYPE_PLANET) -> dict:
    """은하 화면 단축버튼 방식 파견 (FleetController::dispatchSendMiniFleet).

    정찰(6)과 잔해수거(8)만 지원한다. 정찰은 서버가 플레이어 설정 정찰위성 수를
    자동으로 사용하므로 ship_count 는 무시된다(수거만 개수 의미 있음).
    반환: {"ok": bool, "message": str, "ships_sent": int|None, "raw": dict}
    """
    g, s, pos = target
    resp = _post(client, "/ajax/fleet/dispatch/send-mini-fleet", {
        "galaxy": int(g), "system": int(s), "position": int(pos),
        "type": int(target_type), "mission": int(mission_type),
        "shipCount": int(ship_count),
    })
    r = (resp.get("response") if isinstance(resp, dict) else None) or {}
    return {
        "ok": bool(r.get("success")),
        "message": r.get("message", ""),
        "ships_sent": r.get("shipsSent"),
        "raw": resp,
    }


# ---------- 편의 래퍼 ----------

def spy(client, target, probes=1, use_mini=False, **kw) -> dict:
    """정찰. 기본은 send_fleet 로 정찰위성 `probes` 대를 보낸다.

    use_mini=True 면 은하 단축버튼(mini-fleet)으로 보내며 서버 설정 정찰위성 수를 쓴다.
    """
    if use_mini:
        return send_mini_fleet(client, MISSION_ESPIONAGE, target,
                               target_type=kw.get("target_type", PLANET_TYPE_PLANET))
    return send_fleet(client, MISSION_ESPIONAGE, target, {SHIP_ESPIONAGE_PROBE: probes}, **kw)


def attack(client, target, ships, **kw) -> dict:
    """공격."""
    return send_fleet(client, MISSION_ATTACK, target, ships, **kw)


def transport(client, target, ships, resources, **kw) -> dict:
    """수송 (적재 자원 필수)."""
    return send_fleet(client, MISSION_TRANSPORT, target, ships, resources=resources, **kw)


def colonize(client, target, ships=None, **kw) -> dict:
    """식민 (기본 식민선 1대)."""
    ships = ships or {SHIP_COLONY_SHIP: 1}
    return send_fleet(client, MISSION_COLONISATION, target, ships, **kw)


def deploy(client, target, ships, resources=None, **kw) -> dict:
    """주둔/이동 (자기 소유 행성 대상)."""
    return send_fleet(client, MISSION_DEPLOYMENT, target, ships, resources=resources, **kw)


def harvest(client, target, recyclers=1, support_ships=None, **kw) -> dict:
    """잔해 수거 (대상은 잔해 필드).

    ``support_ships`` 는 혼합 수확 함대에 동행시킬 화물선 등 추가 함선이다.
    서버 미션 게이트를 우회하지 않도록 recycler 수량은 별도 인자로 고정한다.
    """
    kw.setdefault("target_type", PLANET_TYPE_DEBRIS)
    ships = dict(support_ships or {})
    ships[SHIP_RECYCLER] = int(recyclers)
    return send_fleet(client, MISSION_RECYCLE, target, ships, **kw)


# ======================================================================
# 3) 진행 중 함대 이동 조회
# ======================================================================

_EVENT_ROW_RE = re.compile(r'<tr class="eventFleet".*?</tr>', re.S)


def _parse_event_row(html: str) -> dict:
    def attr(name):
        m = re.search(rf'{name}="([^"]*)"', html)
        return m.group(1) if m else None

    coords = [
        {"galaxy": int(g), "system": int(s), "position": int(p)}
        for g, s, p in _COORD_RE.findall(html)
    ]
    unit_m = re.search(r'class="detailsFleet">\s*<span>(\d+)</span>', html)
    label_m = re.search(r'class="missionFleet">.*?title="([^"]+)"', html, re.S)
    mt = attr("data-mission-type")
    at = attr("data-arrival-time")
    idm = re.search(r'id="eventRow-(\d+)"', html)
    return {
        "id": int(idm.group(1)) if idm else None,
        "mission_type": int(mt) if mt and mt.isdigit() else None,
        "mission_label": (label_m.group(1).strip() if label_m else None),
        "return_flight": attr("data-return-flight") == "true",
        "arrival_ts": int(at) if at and at.isdigit() else None,
        "unit_count": int(unit_m.group(1)) if unit_m else None,
        "coords": coords,
    }


def fleet_events(client) -> dict:
    """진행 중인 함대 이동을 조회한다.

    - eventbox (JSON): 우호/중립/적대 카운트 + 다음 이벤트까지 남은 초/라벨
    - eventlist (HTML): 개별 함대 행 파싱

    반환:
      {
        "counts": {"friendly": int, "neutral": int, "hostile": int},
        "next_event_seconds": int|None,
        "next_event_label": str|None,
        "events": [ {id, mission_type, mission_label, return_flight,
                     arrival_ts, unit_count, coords[...]}, ... ],
      }
    근거: FleetEventsController::fetchEventBox(JSON) / fetchEventList(HTML eventrow.blade)
    """
    summary = {}
    box = _get(client, "/ajax/fleet/eventbox/fetch")
    try:
        summary = box.json()
    except ValueError:
        summary = {}

    events = []
    lst = _get(client, "/ajax/fleet/eventlist/fetch")
    for row_html in _EVENT_ROW_RE.findall(lst.text):
        events.append(_parse_event_row(row_html))

    return {
        "counts": {
            "friendly": summary.get("friendly"),
            "neutral": summary.get("neutral"),
            "hostile": summary.get("hostile"),
        },
        "next_event_seconds": summary.get("eventTime"),
        "next_event_label": summary.get("eventText"),
        "events": events,
    }
