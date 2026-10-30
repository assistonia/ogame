"""OGameX 봇 공용 클라이언트.

로그인 세션 관리 + 게임 조작 HTTP 함수 모음.
규칙봇과 AI 에이전트가 공유해서 사용한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_URL = "http://localhost"
DEFAULT_CONNECT_TIMEOUT_SEC = 5.0
DEFAULT_READ_TIMEOUT_SEC = 30.0
DEFAULT_REQUEST_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT_SEC, DEFAULT_READ_TIMEOUT_SEC)

# 세션 쿠키 저장소. 매 틱 재로그인(bcrypt 검증, 봇당 ~0.9초 CPU)을 생략하기 위해
# 봇별 쿠키를 파일로 보존하고 다음 틱에서 재사용한다.
SESSIONS_DIR = Path(__file__).resolve().parent / ".sessions"


class _TimeoutSession(requests.Session):
    """Session that bounds every request unless the caller supplies a timeout."""

    def request(self, method, url, **kwargs):
        # Treat an explicit None like an omitted timeout: no OGame request may hold
        # an account lease forever.  Numeric/tuple overrides (notably settlement)
        # retain normal requests semantics.
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = DEFAULT_REQUEST_TIMEOUT
        return super().request(method, url, **kwargs)


class OGameClient:
    def __init__(self, email: str, password: str, base_url: str = BASE_URL):
        self.email = email
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.session = _TimeoutSession()
        self.session.headers["User-Agent"] = "OGameX-Bot/0.1"
        self._token: str | None = None  # CSRF 토큰 캐시 (세션 동안 불변 → 요청 절반 절약)
        self._authenticated = False
        self.session.hooks.setdefault("response", []).append(self._observe_auth_response)
        self._load_session()

    def _invalidate_csrf_tokens(self) -> None:
        """Drop all CSRF caches tied to the current Laravel session.

        Fleet helpers keep their AJAX token on this same client under the dynamic
        ``_fleet_csrf`` attribute.  A renewed login must invalidate both caches;
        otherwise a process-reused client can carry a fleet token from the expired
        session into its next tick.
        """
        self._token = None
        if hasattr(self, "_fleet_csrf"):
            self._fleet_csrf = None

    @staticmethod
    def _is_login_response(response) -> bool:
        """Whether an authenticated request was redirected to the login page."""
        try:
            return urlparse(response.url).path.rstrip("/") == "/login"
        except (AttributeError, TypeError, ValueError):
            return False

    def _observe_auth_response(self, response, *args, **kwargs):
        """Mark a reused client stale when any raw Session call reaches login.

        Several rule modules intentionally use ``client.session.get`` directly.
        They cannot safely replay a request in a response hook, but marking the
        client unauthenticated guarantees the next account lease performs a full
        validation/login instead of remaining stuck on an expired session.
        """
        location = ""
        try:
            location = response.headers.get("Location", "")
        except AttributeError:
            pass
        redirected_to_login = False
        if location:
            try:
                redirected_to_login = urlparse(location).path.rstrip("/") == "/login"
            except (TypeError, ValueError):
                redirected_to_login = False
        if response.status_code in (401, 419) or redirected_to_login or self._is_login_response(response):
            self._authenticated = False
            self._invalidate_csrf_tokens()
        return response

    def _recover_auth(self, *, force_relogin: bool = False) -> None:
        """Refresh authentication and CSRF state after a 419/login redirect."""
        self._invalidate_csrf_tokens()
        if force_relogin:
            self.session.cookies.clear()
        # With cookies present login() validates /overview first.  If the server
        # session expired it falls through to a full login; a token-only 419 keeps
        # the valid session and merely obtains a fresh CSRF token on retry.
        self.login()

    # ---------- 인증 ----------

    def _session_file(self) -> Path:
        return SESSIONS_DIR / f"{self.email.replace('/', '_')}.json"

    def _load_session(self) -> None:
        """저장된 세션 쿠키가 있으면 복원한다 (유효성 검사는 login()에서)."""
        f = self._session_file()
        if f.exists():
            try:
                self.session.cookies.update(json.loads(f.read_text()))
            except (ValueError, OSError):
                pass  # 손상된 파일은 무시하고 새로 로그인

    def _save_session(self) -> None:
        SESSIONS_DIR.mkdir(exist_ok=True)
        self._session_file().write_text(
            json.dumps(requests.utils.dict_from_cookiejar(self.session.cookies))
        )

    @staticmethod
    def _csrf_from_html(html: str) -> str | None:
        m = re.search(r'name="_token"\s+value="([^"]+)"', html)
        if not m:
            m = re.search(r'"csrf-token"\s+content="([^"]+)"', html)
        return m.group(1) if m else None

    def _csrf_token(self, path: str = "/login") -> str:
        html = self.session.get(f"{self.base_url}{path}").text
        token = self._csrf_from_html(html)
        if token is None:
            raise RuntimeError(f"CSRF token not found on {path}")
        return token

    def login(self) -> None:
        """로그인 보장. 복원된 세션이 아직 유효하면 재로그인을 생략한다."""
        if self.session.cookies:
            r = self.session.get(f"{self.base_url}/overview", allow_redirects=False)
            if r.status_code == 200:
                # The validation response is already the page batch/browser actions
                # would GET for a token.  Reuse it and avoid a second GlobalGame pass
                # on the first lease after process restart.
                self._token = self._csrf_from_html(r.text)
                self._authenticated = True
                return  # 세션 유효 — bcrypt 검증 생략
            self.session.cookies.clear()
            self._authenticated = False
            self._invalidate_csrf_tokens()

        token = self._csrf_token("/login")
        r = self.session.post(
            f"{self.base_url}/login",
            data={"_token": token, "email": self.email, "password": self.password},
            allow_redirects=True,
        )
        if "/login" in r.url:
            self._authenticated = False
            raise RuntimeError(f"Login failed for {self.email}")
        # 로그인 시 토큰이 재생성되므로 fleet 캐시는 비우되, redirect 최종 overview
        # HTML에 포함된 새 토큰은 즉시 재사용한다.
        self._invalidate_csrf_tokens()
        self._token = self._csrf_from_html(r.text)
        self._authenticated = True
        self._save_session()

    def ensure_login(self) -> None:
        """Authenticate only when this process has not validated the Session yet.

        The scheduler calls this at every lease.  A healthy cached client performs
        no HTTP request; 419/login-redirect observation clears ``_authenticated``
        so the following lease falls back to ``login()``.
        """
        if not self._authenticated:
            self.login()

    def settle_overview(self, *, max_attempts: int = 3, timeout_sec: float = 30.0):
        """Render ``/overview`` once, with bounded retries safe for a GET.

        The universe scheduler calls this only when a read-only DB probe found a
        fleet mission that OGameX's ``GlobalGame`` middleware needs to settle.  An
        explicit authentication rejection is known to happen before game handling,
        while a transport error or temporary 5xx may happen after settlement; both
        cases are safe to retry because this request is a GET and server settlement
        is idempotent under its mission locks.
        """
        attempts = int(max_attempts)
        if attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        for attempt in range(attempts):
            try:
                response = self.session.get(
                    f"{self.base_url}/overview",
                    allow_redirects=True,
                    timeout=timeout_sec,
                )
            except requests.RequestException:
                if attempt + 1 < attempts:
                    continue
                raise

            login_response = self._is_login_response(response)
            auth_rejected = response.status_code in (401, 419) or login_response
            if auth_rejected:
                if attempt + 1 >= attempts:
                    raise RuntimeError(
                        f"overview settlement authentication failed ({response.status_code})"
                    )
                self._recover_auth(
                    force_relogin=response.status_code == 401 or login_response
                )
                continue

            if 500 <= response.status_code < 600 and attempt + 1 < attempts:
                continue

            response.raise_for_status()
            token = self._csrf_from_html(response.text)
            if token:
                self._token = token
            self._authenticated = True
            return response

        raise RuntimeError("overview settlement retry budget exhausted")

    # ---------- 조회 ----------

    def overview_html(self) -> str:
        return self.session.get(f"{self.base_url}/overview").text

    def get_status(self) -> dict:
        """현재 행성의 자원 현황을 overview HTML에서 파싱.

        표시 텍스트는 100만 이상에서 "1.999Mn"으로 축약되므로,
        data-raw 속성의 정밀값을 우선 사용한다 (없으면 표시 텍스트 폴백).
        """
        html = self.overview_html()
        status: dict = {}
        for key in ("metal", "crystal", "deuterium", "energy"):
            m = re.search(rf'id="resources_{key}".*?data-raw="([\d.]+)"', html, re.DOTALL)
            if m:
                status[key] = int(float(m.group(1)))
                continue
            m = re.search(rf"resources_{key}[^>]*>([^<]*)<", html)
            status[key] = int(m.group(1).strip().replace(",", "")) if m else None
        m = re.search(r"<title>([^<]*)</title>", html)
        status["page_title"] = m.group(1) if m else None
        return status

    # ---------- 행동 ----------

    def build(self, technology_id: int) -> dict:
        """자원 건물 건설 요청 (현재 행성 기준). technologyId: 1=메탈광산, 2=크리스탈광산, 3=듀테륨, 4=태양광발전소."""
        return self._add_buildrequest("/resources/add-buildrequest", "/resources", technology_id)

    def build_facility(self, technology_id: int) -> dict:
        """시설(스테이션) 건설 요청. technologyId: 14=로봇공장, 21=조선소, 31=연구소 등 (game_objects.FACILITIES)."""
        return self._add_buildrequest("/facilities/add-buildrequest", "/facilities", technology_id)

    def research(self, technology_id: int) -> dict:
        """연구 요청. technologyId: 113=에너지기술, 115=연소엔진, 124=천체물리 등 (game_objects.RESEARCH)."""
        return self._add_buildrequest("/research/add-buildrequest", "/research", technology_id)

    def build_ships(self, technology_id: int, amount: int) -> dict:
        """함선 건조 요청. technologyId: 202=소형화물선, 204=전투기, 208=식민선 등 (game_objects.SHIPS).

        조선소 addBuildRequest 는 amount 파라미터로 건조 수량을 받는다
        (app/Http/Controllers/Abstracts/AbstractUnitsController.php: `$request->input('amount')`).
        """
        return self._add_buildrequest(
            "/shipyard/add-buildrequest", "/shipyard", technology_id, extra={"amount": amount}
        )

    def select_class(self, class_id: int) -> dict:
        """캐릭터 클래스 선택. class_id: 1=Collector, 2=General, 3=Discoverer.

        엔드포인트: POST /characterclass/select (routes/web.php),
        파라미터 `characterClassId`(1|2|3). 첫 선택은 무료
        (character_class_free_used=0 이면 CharacterClassService 가 비용 0 처리).
        성공 시 {'status':'success','newClass':...}, 실패 시 {'status':'error',...}.

        CSRF 토큰은 세션 동안 불변이라 캐시 재사용하고, 만료(419) 시 한 번 재발급 후 재시도.
        """
        for attempt in (1, 2):
            if not self._token:
                self._token = self._csrf_token("/characterclass")
            r = self.session.post(
                f"{self.base_url}/characterclass/select",
                data={"_token": self._token, "characterClassId": class_id},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if r.status_code == 419 and attempt == 1:
                self._recover_auth()
                continue
            if self._is_login_response(r) and attempt == 1:
                self._recover_auth(force_relogin=True)
                continue
            # 400(자원 부족 등)도 JSON 본문을 담아 반환하므로 raise 하지 않는다.
            if r.status_code >= 500:
                r.raise_for_status()
            return r.json()

    def send_direct_message(
        self,
        recipient_id: int,
        text: str,
        *,
        reply_to_id: int | None = None,
    ) -> dict:
        """Send one in-game direct chat message through the normal game endpoint.

        Recipient eligibility and diplomacy rate limits belong to the AI diplomacy
        executor. This low-level client only handles authentication/CSRF and the
        server response contract.
        """
        recipient = int(recipient_id)
        message = str(text).strip()
        if recipient <= 0:
            raise ValueError("recipient_id must be positive")
        if not message:
            raise ValueError("message text must not be empty")
        if len(message) > 2000:
            raise ValueError("message text exceeds server limit")

        for attempt in (1, 2):
            if not self._token:
                self._token = self._csrf_token("/chat")
            data = {
                "_token": self._token,
                "mode": 1,
                "playerId": recipient,
                "text": message,
                "ajax": 1,
            }
            if reply_to_id is not None:
                data["msg2reply"] = int(reply_to_id)
            response = self.session.post(
                f"{self.base_url}/chat/send",
                data=data,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if response.status_code == 419 and attempt == 1:
                self._recover_auth()
                continue
            if self._is_login_response(response) and attempt == 1:
                self._recover_auth(force_relogin=True)
                continue
            if response.status_code >= 500:
                response.raise_for_status()
            return response.json()
        raise RuntimeError("chat send authentication retry exhausted")

    def _add_buildrequest(
        self, endpoint: str, token_path: str, technology_id: int, extra: dict | None = None
    ) -> dict:
        """add-buildrequest 계열 공통 처리. 성공 시 {'status':'success',...}, 실패 시 {'success':False,...} 형태의 json 반환.

        CSRF 토큰은 Laravel 세션 동안 불변이라 캐시해서 재사용한다 (요청 수 절반).
        토큰이 만료(419)되면 한 번 재발급 후 재시도.
        """
        for attempt in (1, 2):
            if not self._token:
                self._token = self._csrf_token(token_path)
            data = {"_token": self._token, "technologyId": technology_id}
            if extra:
                data.update(extra)
            r = self.session.post(
                f"{self.base_url}{endpoint}",
                data=data,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if r.status_code == 419 and attempt == 1:
                self._recover_auth()
                continue
            if self._is_login_response(r) and attempt == 1:
                self._recover_auth(force_relogin=True)
                continue
            r.raise_for_status()
            return r.json()

    # ---------- 레벨/수량 조회 ----------

    def get_levels(
        self, pages: tuple[str, ...] = ("facilities", "research", "shipyard", "resources")
    ) -> dict[int, int]:
        """건물/시설/연구 레벨 및 함선 수량을 technologyId 기준 dict 로 반환.

        각 인게임 페이지의 HTML 에서 `data-technology="<id>"` 뒤에 오는
        `class="level"|"amount" data-value="<n>"` 스팬을 파싱한다.
        (research-item.blade.php / unit-item.blade.php 의 렌더 구조 기준)

        기본으로 facilities+research+shipyard+resources 를 합쳐서 조회한다.
        연구소(31)/조선소(21)/천체물리(124)/함선 수량 등이 모두 포함된다.
        """
        levels: dict[int, int] = {}
        for page in pages:
            html = self.session.get(f"{self.base_url}/{page}").text
            matches = list(re.finditer(r'data-technology="(\d+)"', html))
            for i, m in enumerate(matches):
                tid = int(m.group(1))
                if tid in levels:
                    continue
                # 다음 data-technology 직전까지로 구간을 제한해 인접 항목 값 오염 방지.
                end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
                seg = html[m.end():end]
                lm = re.search(r'class="(?:level|amount)"\s+data-value="(\d+)"', seg)
                if lm:
                    levels[tid] = int(lm.group(1))
        return levels

    def get_level(self, technology_id: int, pages: tuple[str, ...] | None = None) -> int:
        """단일 technologyId 의 레벨/수량 조회. 없으면 0."""
        kwargs = {"pages": pages} if pages else {}
        return self.get_levels(**kwargs).get(technology_id, 0)


if __name__ == "__main__":
    import sys

    email = sys.argv[1] if len(sys.argv) > 1 else "test2@ogamex.dev"
    c = OGameClient(email, "test")
    c.login()
    print(f"[OK] logged in as {email}")
    html = c.overview_html()
    m = re.search(r"<title>([^<]*)</title>", html)
    print(f"[OK] overview title: {m.group(1) if m else '?'}")
