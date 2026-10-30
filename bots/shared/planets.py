"""OGameX 봇 다중 행성 지원 모듈.

`OGameClient` 인스턴스(로그인 완료)를 받아 동작하는 함수 모음이다.
client.py 는 건드리지 않고 안정적인 속성만 사용한다:
  - client.base_url  (문자열, 끝 슬래시 없음)
  - client.session   (로그인 쿠키가 담긴 requests.Session)
  - client.overview_html()  (overview HTML 문자열)

행성 전환 방식 (서버 소스에서 확인):
  - OGameX 는 `?cp=<planet_id>` 쿼리 파라미터로 현재 행성을 바꾼다.
    (app/Http/Middleware/GlobalGame.php: `$request->query('cp')` → `setCurrentPlanetId()`)
  - setCurrentPlanetId 는 소유 검증 후 users.planet_current 를 **DB 에 저장**한다
    (app/Services/PlayerService.php: `$this->user->save()`). 따라서 한 번 ?cp= 로
    전환하면 이후 cp 없는 요청도 그 행성 기준으로 동작한다(세션 지속).

행성 목록 파싱 근거:
  resources/views/ingame/layouts/main.blade.php 의 좌측 행성바 —
    <div class="smallplanet ..." data-planet-id="N" id="planet-K"> ...
        <span class="planet-name ">NAME</span>
        <span class="planet-koords ">[g:s:p]</span>
  달(moon)은 같은 smallplanet div 안에 `class="moonlink"` 로 중첩되지만
  planet-koords span 이 없으므로 행성 좌표만 안전하게 뽑힌다.
"""
from __future__ import annotations

import re

# smallplanet 블록의 시작(행성 하나당 하나). 이 위치로 HTML 을 잘라 블록별로 파싱한다.
_SMALLPLANET_RE = re.compile(r'class="smallplanet[^"]*"\s+data-planet-id="(\d+)"')
_PNAME_RE = re.compile(r'class="planet-name\s*">\s*([^<]*?)\s*</span>')
_PKOORDS_RE = re.compile(r'class="planet-koords\s*">\s*\[(\d+):(\d+):(\d+)\]')


def list_planets(client) -> list[dict]:
    """현재 계정의 행성 목록을 overview(좌측 행성바)에서 파싱해 돌려준다.

    각 항목(dict):
      id        : 행성 planet_id (int) — switch_planet / ?cp= 에 사용
      name      : 행성 이름 (str)
      galaxy, system, position : 좌표 (int)
      coord     : (galaxy, system, position) 튜플
      current   : 이 행성이 현재 선택된 행성인지 (bool)

    달(moon)은 제외한다. 파싱 실패 시 빈 리스트.
    """
    try:
        html = client.overview_html()
    except Exception:  # noqa: BLE001 - 조회 실패는 치명적이지 않음
        return []
    return _parse_planets(html)


def _parse_planets(html: str) -> list[dict]:
    marks = list(_SMALLPLANET_RE.finditer(html))
    if not marks:
        return []
    out: list[dict] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        block = html[m.start():end]
        pid = int(m.group(1))
        km = _PKOORDS_RE.search(block)
        if not km:
            continue  # 좌표 없는 블록(파싱 이상)은 건너뜀
        nm = _PNAME_RE.search(block)
        # 현재 선택 행성은 smallplanet div 에 hightlightPlanet 클래스가 붙는다
        # (allCount()>1 일 때만 붙으므로, 행성 1개인 계정에서는 아래 폴백으로 처리).
        current = "hightlightPlanet" in html[m.start():m.start() + 200]
        out.append({
            "id": pid,
            "name": (nm.group(1).strip() if nm else ""),
            "galaxy": int(km.group(1)),
            "system": int(km.group(2)),
            "position": int(km.group(3)),
            "coord": (int(km.group(1)), int(km.group(2)), int(km.group(3))),
            "current": current,
        })
    # 행성 1개뿐이면 그 행성이 곧 현재 행성.
    if len(out) == 1:
        out[0]["current"] = True
    return out


def switch_planet(client, planet_id: int) -> bool:
    """현재 행성을 planet_id 로 전환한다 (?cp= 쿼리).

    전환은 DB 에 저장되므로 이후 client 의 build/research/get_status/get_levels 등
    cp 없는 요청도 이 행성 기준으로 동작한다.

    반환: 전환 후 overview 가 실제로 이 행성을 현재 행성으로 보고하면 True.
    소유하지 않은 planet_id 는 서버가 무시하므로 False 가 된다.
    """
    try:
        r = client.session.get(
            f"{client.base_url}/overview",
            params={"cp": int(planet_id)},
            allow_redirects=True,
        )
        html = r.text
    except Exception:  # noqa: BLE001
        return False
    return current_planet_id(client, html=html) == int(planet_id)


def current_planet_id(client, html: str | None = None) -> int | None:
    """현재 선택된 행성의 planet_id. overview 의 ogame-planet-id meta 태그에서 읽는다.

    (main.blade.php: <meta name="ogame-planet-id" content="{{ currentPlanet id }}">)
    """
    if html is None:
        try:
            html = client.overview_html()
        except Exception:  # noqa: BLE001
            return None
    m = re.search(r'name="ogame-planet-id"\s+content="(\d+)"', html)
    if m:
        return int(m.group(1))
    # 폴백: 행성이 1개면 목록의 유일 행성.
    planets = _parse_planets(html)
    return planets[0]["id"] if len(planets) == 1 else None
