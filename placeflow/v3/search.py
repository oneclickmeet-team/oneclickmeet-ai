"""장소 검색 제공자 (v2.1: 카카오 로컬로 고정).

쓰이는 곳
  - 직접 검색 (장소 이름·주소로 찾기)
  - 링크/스크린샷에서 뽑은 이름 → 후보 확인 목록
  - 코스 추천의 '외부 추천' 후보 수집 (좌표 주변 카테고리·검색어)

제공자 (환경변수 SEARCH_PROVIDER: kakao | demo | demo_fail. 실행 중 자동 전환 없음)
  KakaoLocalSearch  카카오 로컬 API 키워드·카테고리 검색 (KAKAO_REST_API_KEY). 좌표·반경 검색 지원, 한 번에 15건
  DemoSearch        키 없이 화면을 확인하기 위한 가상 데이터 (지원 지역 6곳 × 16곳, 이름은 모두 가상)
네이버 API 는 장소 검색에 쓰지 않고, 블로그 검색으로 장소 태그를 만드는 데만 쓴다 (blog_tags.py).

place_id 는 제공자와 관계없이 'nv_' + sha1(공백 뺀 이름|도로명주소) — 같은 가게면 같은 ID (영구 외부 ID 를 저장하지 않음)

공통 메서드
  search(query, display)                              키워드 검색 → list[Place]
  nearby(lat, lng, radius_m, code=None, query=None)  좌표 주변 검색 (code: 카테고리 코드 7개 중 하나) → 가까운 순
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from .. import hours as hours_mod
from .. import taxonomy
from .domain import Place
from .regions import REGIONS, region_of
from .walking import distance_m


class ExternalSearchError(Exception):
    """외부 검색 API 실패 (키 오류·네트워크·한도 초과). 추천은 'external_api_failed' 로 처리."""


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, display: int = 5) -> list[Place]: ...

    def nearby(self, lat: float, lng: float, radius_m: int = 1000, code: Optional[str] = None,
               query: Optional[str] = None, limit: int = 30) -> list[Place]: ...


def _region_nearby(provider, lat: float, lng: float, radius_m: int, code: Optional[str], query: Optional[str],
                   limit: int, per_query: int) -> list[Place]:
    """좌표 검색이 없는 제공자(데모)용: 가까운 지원 지역명 + 검색어로 모은 뒤 반경으로 거름.
    지원 지역 밖 좌표는 빈 결과."""
    from .categories import SEARCH_PLAN

    region = region_of(lat, lng)
    if region is None:
        return []
    words = [query] if query else (SEARCH_PLAN[code]["keywords"] if code else ["맛집", "카페", "놀거리"])
    seen: dict[str, Place] = {}
    for w in words:
        for p in provider.search(f"{region.area_query} {w}", per_query):  # ExternalSearchError 전파
            seen.setdefault(p.place_id, p)
    out = [p for p in seen.values() if distance_m((lat, lng), p.pos) <= radius_m
           and (code is None or p.category_code == code)]
    out.sort(key=lambda p: distance_m((lat, lng), p.pos))
    return out[:limit]


def make_place_id(name: str, road_address: str | None, jibun: str | None = None) -> str:
    n = re.sub(r"\s+", "", name)
    a = re.sub(r"\s+", "", road_address or jibun or "")
    key = n + "|" + a
    return "nv_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _area_from(jibun: str | None, road: str | None) -> str | None:
    for src in (jibun, road):
        for tok in (src or "").split():
            if re.search(r"(동|가)$", tok) and not re.search(r"(시|군|구)$", tok) and len(tok) <= 6:
                return re.sub(r"\d+가$", "", tok) or tok
    return None


# ---------------------------------------------------------------- 카카오
class KakaoLocalSearch:
    """카카오 로컬 API. 앱 관리 > 카카오맵 > 사용 설정 ON 필요 (무료 쿼터는 계정의 첫 앱에만 적용)."""

    name = "kakao_local"
    BASE = "https://dapi.kakao.com/v2/local/search"

    def __init__(self, rest_key: str | None = None, timeout: float = 6.0, cache_ttl: int = 6 * 3600):
        self.key = rest_key or os.environ.get("KAKAO_REST_API_KEY")
        if not self.key:
            raise ValueError("KAKAO_REST_API_KEY 환경변수가 필요합니다 (.env)")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, list[Place]]] = {}

    def _get(self, kind: str, params: dict) -> list[Place]:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.BASE}/{kind}.json?{q}"
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < self.cache_ttl:
            return hit[1]
        req = urllib.request.Request(url, headers={"Authorization": f"KakaoAK {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                body = json.loads(e.read().decode("utf-8"))
                detail = " " + str(body.get("message") or body.get("msg") or "")
            except Exception:
                pass
            hint = {401: " (REST API 키 확인)", 403: " (카카오맵 사용 설정 ON 확인)", 429: " (쿼터 초과)"}.get(e.code, "")
            raise ExternalSearchError(f"카카오 로컬 HTTP {e.code}{detail.rstrip()}{hint}") from e
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise ExternalSearchError(f"카카오 로컬 연결 실패: {e}") from e
        places = [p for p in (self._to_place(d) for d in data.get("documents", [])) if p]
        self._cache[url] = (time.time(), places)
        return places

    @staticmethod
    def _to_place(d: dict) -> Place | None:
        from .categories import KAKAO_GROUP_TO_PRIMARY

        name = (d.get("place_name") or "").strip()
        try:
            lat, lng = float(d.get("y")), float(d.get("x"))
        except (TypeError, ValueError):
            return None
        if not name:
            return None
        road, jibun = d.get("road_address_name") or None, d.get("address_name") or None
        cat_text = (d.get("category_name") or "").replace(">", " ")
        found = taxonomy.classify_text(cat_text, name)
        if found:
            cat, sub = found
        else:
            cat, sub = KAKAO_GROUP_TO_PRIMARY.get(d.get("category_group_code") or "", "other"), "other"
        return Place(place_id=make_place_id(name, road, jibun), name=name, category=cat, sub_category=sub,
                     category_label=taxonomy.PRIMARY[cat]["label"], area_label=_area_from(jibun, road),
                     road_address=road, jibun_address=jibun, lat=lat, lng=lng, phone=d.get("phone") or None,
                     map_url=d.get("place_url") or None, provider="kakao_local")

    def search(self, query: str, display: int = 5) -> list[Place]:
        q = query.strip()
        if not q:
            return []
        return self._get("keyword", {"query": q, "size": min(max(display, 1), 15)})[:display]

    def nearby(self, lat, lng, radius_m=1000, code=None, query=None, limit=30):
        from .categories import SEARCH_PLAN

        base = {"x": f"{lng:.7f}", "y": f"{lat:.7f}", "radius": int(min(max(radius_m, 1), 20000)), "sort": "distance",
                "size": 15}
        seen: dict[str, Place] = {}
        if query:
            for page in (1, 2):
                for p in self._get("keyword", {**base, "query": query, "page": page}):
                    seen.setdefault(p.place_id, p)
        else:
            plan = SEARCH_PLAN.get(code or "", {"kakao_group": None, "keywords": ["맛집", "카페", "놀거리"]})
            if plan["kakao_group"]:
                for page in (1, 2):
                    for p in self._get("category", {**base, "category_group_code": plan["kakao_group"], "page": page}):
                        seen.setdefault(p.place_id, p)
            for w in plan["keywords"]:
                for p in self._get("keyword", {**base, "query": w}):
                    seen.setdefault(p.place_id, p)
        out = [p for p in seen.values() if distance_m((lat, lng), p.pos) <= radius_m
               and (code is None or p.category_code == code)]
        out.sort(key=lambda p: distance_m((lat, lng), p.pos))
        return out[:limit]


# ---------------------------------------------------------------- 데모 (가상 데이터)
_DEMO_TEMPLATES = [
    # (이름 형식, 대분류, 소분류, 동쪽 m, 북쪽 m, 영업시간)
    ("{a} 파스타집", "restaurant", "western", 0, 0, "매일 11:30-21:30 브레이크타임 15:00-17:00"),
    ("{a} 감성 카페", "cafe", "other", 260, 110, "매일 11:00-22:00"),
    ("{a} 보드게임 카페", "activity", "game", -310, 210, "매일 12:00-24:00"),
    ("{a} 숲길 산책", "nature", "park", 150, -290, None),
    ("{a} 소품샵", "shopping", "shop", -130, -210, None),
    ("{a} 이자카야", "bar", "izakaya", 390, -70, "매일 17:00-02:00"),
    ("{a} 디저트 하우스", "cafe", "dessert", -70, 330, "화-일 12:00-21:00 매주 월요일 휴무"),
    ("{a} 국밥", "restaurant", "korean", 520, 260, "매일 07:00-21:00"),
    ("{a} 전시 공간", "culture", "exhibition", -460, -110, "화-일 10:00-19:00 매주 월요일 휴무"),
    ("{a} 와인바", "bar", "wine_bar", 210, 460, "매일 18:00-01:00"),
    ("{a} 반지 공방", "activity", "workshop", -260, -430, None),
    ("{a} 라멘", "restaurant", "japanese", 640, -310, None),
    ("{a} 로스터리", "cafe", "roastery", -610, 160, None),
    ("{a} 포토부스", "activity", "photo", 90, -90, "매일 10:00-24:00"),
    ("{a} 브런치", "restaurant", "brunch", -390, 430, "매일 09:00-16:00"),
    ("{a} 언덕 카페", "cafe", "other", 1600, 1250, None),  # 반경 밖 (거리 필터 확인용)
]
_CAT_WORDS = {
    "맛집": {"restaurant"}, "음식점": {"restaurant"}, "카페": {"cafe"}, "디저트": {"cafe"}, "술집": {"bar"},
    "바": {"bar"}, "공원": {"nature"}, "산책": {"nature"}, "소품샵": {"shopping"}, "쇼핑": {"shopping"},
    "전시": {"culture"}, "놀거리": {"activity", "culture"}, "보드게임": {"activity"}, "공방": {"activity"},
    "이자카야": {"bar"}, "와인바": {"bar"}, "미술관": {"culture"}, "편집숍": {"shopping"}, "방탈출": {"activity"},
    "포토부스": {"activity"}, "보드게임카페": {"activity"},
}


def _demo_places() -> list[Place]:
    out = []
    for r in REGIONS:
        short = r.name.replace("역", "").replace("대입구", "대")
        for tmpl, cat, sub, dx, dy, hrs in _DEMO_TEMPLATES:
            name = tmpl.format(a=short)
            lat = r.lat + dy / 111_320
            lng = r.lng + dx / (111_320 * math.cos(math.radians(r.lat)))
            road = f"서울 (데모) {r.area_query}로 {abs(dx) // 10 + abs(dy) // 10}"
            out.append(Place(place_id=make_place_id(name, road), name=name, category=cat, sub_category=sub,
                             category_label=taxonomy.PRIMARY[cat]["label"], area_label=r.area_query,
                             road_address=road, lat=round(lat, 6), lng=round(lng, 6), provider="demo",
                             opening_hours=hours_mod.parse_hours(hrs) if hrs else None))
    return out


class DemoSearch:
    """오프라인 데모 검색. fail=True 면 외부 API 실패를 흉내냅니다."""

    name = "demo"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.name = "demo_fail" if fail else "demo"
        self.places = _demo_places()

    def search(self, query: str, display: int = 5) -> list[Place]:
        if self.fail:
            raise ExternalSearchError("데모: 외부 검색 실패 시뮬레이션")
        toks = [t for t in re.split(r"\s+", query.strip()) if t]
        if not toks:
            return []
        cats: set[str] = set()
        names = []
        for t in toks:
            if t in _CAT_WORDS:
                cats |= _CAT_WORDS[t]
            else:
                names.append(t)
        res = []
        for p in self.places:
            hay = f"{p.name} {p.area_label} {p.road_address}"
            if cats and p.category not in cats:
                continue
            if all(n in hay or n.replace("동", "") in hay for n in names):
                res.append(p)
        return res[:display]

    def nearby(self, lat, lng, radius_m=1000, code=None, query=None, limit=30):
        if self.fail:
            raise ExternalSearchError("데모: 외부 검색 실패 시뮬레이션")
        return _region_nearby(self, lat, lng, radius_m, code, query, limit, 16)


def get_search_provider(name: str | None = None):
    """SEARCH_PROVIDER 환경변수(또는 인자)로 하나를 고른다: kakao | demo | demo_fail.
    지정하지 않으면(auto) 카카오 키가 있으면 카카오, 없으면 데모. 실행 중 다른 제공자로 자동 전환하지 않는다."""
    name = (name or "auto").lower()
    if name == "auto":
        name = (os.environ.get("SEARCH_PROVIDER") or "auto").lower()
    if name == "auto":
        name = "kakao" if os.environ.get("KAKAO_REST_API_KEY") else "demo"
    if name in ("kakao", "kakao_local"):
        return KakaoLocalSearch()
    if name == "demo":
        return DemoSearch()
    if name == "demo_fail":
        return DemoSearch(fail=True)
    if name in ("naver", "naver_local"):
        raise ValueError("v2.1 부터 장소 검색은 카카오로 고정입니다 (SEARCH_PROVIDER=kakao). "
                         "네이버 키는 블로그 태그(BLOG_TAGS)에만 쓰입니다.")
    raise ValueError(f"알 수 없는 검색 제공자: {name} (kakao | demo | demo_fail)")
