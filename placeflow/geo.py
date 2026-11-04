"""장소 확정(보강): 추출한 이름/주소 단서로 지도 API를 검색해 정식 정보와 좌표를 붙입니다.

제공자: none | fixture(오프라인) | kakao
"""
from __future__ import annotations

import difflib
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import taxonomy


@dataclass
class GeoCandidate:
    provider: str
    provider_id: str | None
    name: str
    category: str | None
    road_address: str | None
    jibun_address: str | None
    lat: float | None
    lng: float | None
    phone: str | None = None
    url: str | None = None
    kakao_group: str | None = None
    raw: dict = field(default_factory=dict)


class GeoResolver(Protocol):
    name: str

    def search(self, query: str) -> list[GeoCandidate]: ...


def _http_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


class KakaoLocalResolver:
    """카카오 로컬 키워드 검색. KAKAO_REST_API_KEY 필요."""

    name = "kakao"
    ENDPOINT = "https://dapi.kakao.com/v2/local/search/keyword.json"

    def __init__(self):
        self.key = os.environ["KAKAO_REST_API_KEY"]

    def search(self, query: str) -> list[GeoCandidate]:
        url = f"{self.ENDPOINT}?query={urllib.parse.quote(query)}&size=5"
        data = _http_json(url, {"Authorization": f"KakaoAK {self.key}"})
        out = []
        for d in data.get("documents", []):
            out.append(GeoCandidate(
                "kakao", d.get("id"), d.get("place_name", ""), d.get("category_name"),
                d.get("road_address_name") or None, d.get("address_name") or None,
                float(d["y"]) if d.get("y") else None, float(d["x"]) if d.get("x") else None,
                d.get("phone") or None, d.get("place_url"), d.get("category_group_code") or None, raw=d))
        return out


class FixtureResolver:
    """오프라인 테스트용: JSON 파일의 가상 장소 목록에서 이름 부분일치로 검색."""

    name = "fixture"

    def __init__(self, path: str | Path):
        self.places = json.loads(Path(path).read_text(encoding="utf-8"))

    def search(self, query: str) -> list[GeoCandidate]:
        q = _norm(query)
        out = []
        for p in self.places:
            if _norm(p["name"]) in q or q in _norm(p["name"]) or difflib.SequenceMatcher(None, _norm(p["name"]), q).ratio() > 0.6:
                out.append(GeoCandidate("fixture", p.get("id"), p["name"], p.get("category"),
                                        p.get("road_address"), p.get("jibun_address"), p.get("lat"), p.get("lng"),
                                        p.get("phone"), p.get("url"), p.get("kakao_group"), raw=p))
        return out[:5]


def get_resolver(name: str, fixture_path: str | None = None):
    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "kakao":
        return KakaoLocalResolver()
    if name == "fixture":
        return FixtureResolver(fixture_path or Path(__file__).parent.parent / "eval" / "fixtures" / "geo_places.json")
    raise ValueError(f"알 수 없는 지도 제공자: {name}")


# ------------------------------------------------------------------ 매칭
def _norm(s: str | None) -> str:
    return re.sub(r"[\s\-_.·'’&()]+", "", (s or "").lower())


def name_similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _addr_tokens(s: str | None) -> set[str]:
    return {t for t in re.split(r"\s+", s or "") if re.search(r"(시|군|구|동|로|길|읍|면)$", t)}


def score_candidate(name: str, branch: str | None, address_hint: str | None, region_hint: str | None,
                    c: GeoCandidate) -> float:
    s = name_similarity(name, c.name)
    if branch and branch.replace("점", "") in c.name:
        s = min(1.0, s + 0.05)
    addr = f"{c.road_address or ''} {c.jibun_address or ''}"
    if address_hint:
        hint = _addr_tokens(address_hint)
        if hint:
            overlap = len(hint & _addr_tokens(addr)) / len(hint)
            s = 0.6 * s + 0.4 * overlap
    elif region_hint:
        if region_hint in addr or region_hint in (c.name or ""):
            s = min(1.0, s + 0.05)
        else:
            s *= 0.9
    return round(s, 3)


def resolve(resolver, name: str, branch: str | None, address_hint: str | None,
            region_hint: str | None) -> tuple[GeoCandidate | None, float]:
    if resolver is None:
        return None, 0.0
    queries = []
    full = f"{name} {branch}" if branch else name
    if region_hint:
        queries.append(f"{region_hint} {full}")
    queries.append(full)
    best, best_s = None, 0.0
    for q in dict.fromkeys(queries):
        try:
            cands = resolver.search(q)
        except Exception:  # 네트워크/키 오류는 보강 실패로만 처리
            cands = []
        for c in cands:
            sc = score_candidate(name, branch, address_hint, region_hint, c)
            if sc > best_s:
                best, best_s = c, sc
        if best_s >= 0.9:
            break
    return best, best_s


def category_from_candidate(c: GeoCandidate) -> tuple[str, str | None] | None:
    hit = taxonomy.classify_text(c.category)
    if hit:
        return hit
    if c.kakao_group and c.kakao_group in taxonomy.KAKAO_GROUP_MAP:
        return taxonomy.KAKAO_GROUP_MAP[c.kakao_group]
    return None
