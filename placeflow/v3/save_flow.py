"""장소 저장 흐름 (V3): 링크 / 스크린샷 / 직접 검색 → 후보 확인 → 보관함 저장.

  resolve_candidates()  입력을 받아 '이 장소가 맞나요?' 후보 목록을 만든다 (저장하지 않음)
  save_candidate()      사용자가 고른 후보를 개인 보관함에 저장 (+ 공동 보관함 공유 토글)

결과 상태 (ResolveResult.status)
  candidates          후보 1개 이상 → 후보 확인 화면
  no_match            이름은 뽑았지만 검색 결과 없음 → 직접 검색 화면 (suggested_query 미리 채움)
  recognition_failed  스크린샷에서 장소를 못 찾음 → 스크린샷 인식 실패 화면
  link_unreadable     링크에서 내용을 못 읽음(인스타 로그인 벽 등) → 스크린샷 또는 직접 검색 권유
  search_failed       외부 검색 실패 → 직접 검색 재시도 안내 (링크에서 좌표를 얻었으면 그 후보는 보여줌)
"""
from __future__ import annotations

import difflib
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .. import geo as geo_mod
from .. import taxonomy
from ..models import PlaceRecord
from .domain import Place, SavedPlace
from .search import ExternalSearchError, SearchProvider, make_place_id

MAX_CANDIDATES = 5          # 직접 검색 결과 수
MAX_GROUPS = 10             # 링크 하나에서 후보를 찾는 장소 수 (릴스 '맛집 N곳' 모음 대응)
MAX_OTHERS = 2              # 장소별 '다른 후보' 수

MatchType = Literal["exact", "similar"]

_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def split_share_text(raw: str) -> tuple[Optional[str], Optional[str]]:
    """앱 '공유 → 복사' 문구(예: '[네이버 지도]\n삼형제쭈꾸미 대학로점\n서울 종로구 동숭길 68\nhttps://naver.me/x')
    → (첫 번째 링크, 링크를 뺀 나머지 문구). 나머지 문구는 링크를 못 읽을 때 장소 정보로 쓰입니다."""
    m = _URL_RE.search(raw or "")
    if not m:
        return None, None
    rest = (raw[:m.start()] + " " + raw[m.end():]).strip()
    header = re.match(r"^\[([^\]]{1,20})\]\s*", rest)
    rest = rest[header.end():].strip() if header else rest  # '[네이버 지도]' 머리말 제거
    lines = [l.strip() for l in rest.splitlines() if l.strip()]
    if header and lines and len(lines[0]) <= 30 and not lines[0].startswith("📍"):
        lines[0] = "📍 " + lines[0]  # 지도 앱 공유 문구의 첫 줄 = 상호명
    return m.group(0).rstrip(").,"), ("\n".join(lines) or None)


class Candidate(BaseModel):
    place: Place
    match_score: float = Field(..., ge=0, le=1, description="추출한 이름·주소와 얼마나 맞는지")
    match_type: MatchType = Field("exact", description="exact=확실한 일치(기본 선택) / similar=비슷한 이름(기본 선택 안 함)")
    reasons: list[str] = Field(default_factory=list, description="화면 배지: 주소 일치 · 이름 일치 · 이름 일부만 같아요 · 주소 다름 …")
    already_saved: bool = False
    from_text: Optional[str] = Field(None, description="이 후보를 찾게 한 추출 이름")


class Extracted(BaseModel):
    order: int = Field(1, description="링크(캡션)에 나온 순서, 1부터")
    name: str
    branch: Optional[str] = None
    address_hint: Optional[str] = None
    category: Optional[str] = None


class PlaceGroup(BaseModel):
    """링크에 나온 장소 1곳 = 묶음 1개. 화면은 묶음마다 best(체크) + others(접힘)를 보여줌."""
    extracted: Extracted
    status: Literal["matched", "similar_only", "not_found", "search_failed"]
    best: Optional[Candidate] = Field(None, description="match_type=exact 인 1순위. 없으면 null")
    others: list[Candidate] = Field(default_factory=list)


class ResolveResult(BaseModel):
    status: Literal["candidates", "no_match", "recognition_failed", "link_unreadable", "search_failed"]
    groups: list[PlaceGroup] = Field(default_factory=list, description="링크·스크린샷 결과 (직접 검색은 비어 있음)")
    candidates: list[Candidate] = Field(default_factory=list, description="호환용: 모든 후보를 한 줄로 (best 먼저)")
    suggested_query: Optional[str] = None
    message: Optional[str] = None
    extracted: list[Extracted] = Field(default_factory=list)


def _place_from_record(r: PlaceRecord) -> Optional[Place]:
    """링크(네이버 플레이스 등)에서 좌표까지 얻은 경우, 검색 없이도 후보로 쓸 수 있게 Place 로 변환."""
    if not r.place.location:
        return None
    a = r.place.address
    return Place(place_id=make_place_id(r.place.name, a.road, a.jibun), name=r.place.name, branch=r.place.branch,
                 category=r.classification.primary_category, sub_category=r.classification.sub_category,
                 category_label=r.classification.primary_label, area_label=a.dong, road_address=a.road,
                 jibun_address=a.jibun, lat=r.place.location.lat, lng=r.place.location.lng, phone=r.place.phone,
                 map_url=r.place.map_url, provider="link", opening_hours=r.details.opening_hours)


_ROAD_CORE = re.compile(r"([가-힣0-9]+(?:로|길)\s*\d+(?:-\d+)?)")
# 캡션의 동네 이름 → 구. 주소가 없을 때 같은 이름의 먼 가게를 기본 선택하지 않으려고 씀
_AREA_GUS = {
    "혜화": ("종로구", "성북구"), "대학로": ("종로구",), "서촌": ("종로구",), "북촌": ("종로구",), "삼청": ("종로구",),
    "익선": ("종로구",), "종로": ("종로구",), "잠실": ("송파구",), "송리단길": ("송파구",), "석촌": ("송파구",),
    "방이": ("송파구",), "송파": ("송파구",), "연남": ("마포구", "서대문구"), "홍대": ("마포구",), "합정": ("마포구",),
    "망원": ("마포구",), "상수": ("마포구",), "신촌": ("서대문구", "마포구"), "성신여대": ("성북구",),
    "한성대": ("성북구", "종로구"), "성수": ("성동구",), "건대": ("광진구",), "강남": ("강남구", "서초구"),
    "압구정": ("강남구",), "청담": ("강남구",), "신사": ("강남구",), "이태원": ("용산구",), "한남": ("용산구",),
    "해방촌": ("용산구",), "용산": ("용산구",), "을지로": ("중구",), "문래": ("영등포구",), "여의도": ("영등포구",),
}


def _join_road(addr: Optional[str]) -> str:
    """'와우산로 21길 28-6' 처럼 띄어 쓴 도로명을 붙임."""
    return re.sub(r"(로)\s+(\d+(?:번)?길)", r"\1\2", addr or "")


def _road_core(addr: Optional[str]) -> Optional[str]:
    m = _ROAD_CORE.search(_join_road(addr))
    return re.sub(r"\s+", "", m.group(1)) if m else None


def _score(rec_name: str, branch: Optional[str], addr_hint: Optional[str], p: Place) -> float:
    cand = geo_mod.GeoCandidate("search", p.place_id, p.name, None, p.road_address, p.jibun_address, p.lat, p.lng)
    return round(min(1.0, geo_mod.score_candidate(rec_name, branch, addr_hint, None, cand)), 3)


def search_candidates(search: SearchProvider, query: str, is_saved=lambda pid: False) -> ResolveResult:
    """직접 검색 화면. 결과를 그대로 후보로."""
    q = (query or "").strip()
    if not q:
        return ResolveResult(status="no_match", message="장소 이름이나 주소를 입력해 주세요.")
    try:
        places = search.search(q, MAX_CANDIDATES)
    except ExternalSearchError as e:
        return ResolveResult(status="search_failed", suggested_query=q, message=f"검색을 불러오지 못했어요. ({e})")
    cands = [Candidate(place=p, match_score=_score(q, None, None, p), already_saved=is_saved(p.place_id), from_text=q)
             for p in places]
    if not cands:
        return ResolveResult(status="no_match", suggested_query=q, message="검색 결과가 없어요. 다른 이름이나 주소로 찾아보세요.")
    return ResolveResult(status="candidates", candidates=cands)


_SIGUNGU = re.compile(r"([가-힣]{1,5}(?:구|군))\s")
_BUILDING = re.compile(r"([가-힣A-Za-z0-9]{1,10}?)(상가|빌딩|타워|프라자|플라자|몰|시장|센터|아파트)")
_BRANCH_TAIL = re.compile(r"^[가-힣A-Za-z0-9]{1,8}점$")


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def _sigungu(addr: Optional[str]) -> Optional[str]:
    m = _SIGUNGU.search((addr or "") + " ")
    return m.group(1) if m else None


def _area_gus(hint: Optional[str]) -> tuple[str, ...]:
    if not hint:
        return ()
    if hint.endswith("구"):
        return (hint,)
    return next((gus for area, gus in _AREA_GUS.items() if area in hint), ())


def _region_hint(r: PlaceRecord) -> str:
    return next((e.split(":", 1)[1].strip() for e in r.content.evidence if e.startswith("지역 단서:")), "")


def _block_of(raw: str, name: str, next_name: Optional[str]) -> str:
    """캡션 전체에서 이 장소 설명 부분만 (다음 장소 이름 전까지)."""
    i = raw.find(name)
    if i < 0:
        return ""
    j = raw.find(next_name, i + len(name)) if next_name else -1
    return raw[i:j if j > 0 else i + 600]


def match(name: str, branch: Optional[str], road: Optional[str], buildings: list[str], p: Place,
          name_unsure: bool = False) -> Optional[tuple[MatchType, float, list[str]]]:
    """추출한 장소 ↔ 검색 결과 1건 판정. None = 후보 아님.

    exact   : 도로명+건물번호 일치(이름도 비슷) / 이름 일치(공백·기호 무시, '○○점' 지점명 허용)이고 주소가 다르지 않음
    similar : 이름 일부만 같음(오지다 ⊂ 회포차오지다) / 철자 비슷 / 이름은 같은데 주소 다름(다른 지점)
    """
    n, c = _norm(name), _norm(p.name)
    full = _norm(f"{name}{branch or ''}")
    if not n or not c:
        return None
    tail = p.name.replace(" ", "")[len(name.replace(" ", "")):]
    if c in (n, full) or (c.startswith(n) and (_BRANCH_TAIL.match(tail or "") or tail in _AREA_GUS)) \
            or (branch and c.startswith(n) and _norm(branch) in c):
        name_kind = "same"
    elif n in c or c in n:
        name_kind = "part"
    else:
        ratio = difflib.SequenceMatcher(None, n, c).ratio()
        name_kind = "close" if ratio >= 0.75 else "far"

    cand_road = _road_core(p.road_address)
    addr_same = bool(road and cand_road and cand_road == road)
    addr_diff = bool(road and cand_road and cand_road != road)
    paddr = f"{p.road_address or ''} {p.jibun_address or ''} {p.name}"
    bld = next((b for b in buildings if b[0] in paddr and (b[1] in paddr or b[1] == "상가" and "상가" in paddr)), None)

    reasons: list[str] = []
    if addr_same and name_kind == "far" and not name_unsure:  # 주소는 맞는데 이름이 다름: 오독이거나 같은 건물의 다른 가게
        if difflib.SequenceMatcher(None, n, c).ratio() >= 0.5:  # 가우뚱 → 가우동
            return "exact", 0.85, ["주소 일치", "이름이 비슷해요"]
        return "similar", 0.5, ["주소만 일치"]
    if addr_same and (name_kind != "far" or name_unsure):
        reasons.append("주소 일치")
        if name_kind == "same":
            reasons.append("이름 일치")
        return "exact", (1.0 if name_kind == "same" else 0.92), reasons
    if name_kind == "same" and not addr_diff:
        reasons.append("이름 일치")
        if bld:
            reasons.append(f"{bld[0]}{bld[1]} 일치")
        return "exact", (0.95 if bld else 0.9), reasons
    if name_kind == "far":
        return None
    reasons.append({"same": "이름은 같지만 다른 지점", "part": "이름 일부만 같아요", "close": "이름이 비슷해요"}[name_kind])
    if addr_diff:
        reasons.append("주소 다름")
    score = {"same": 0.7, "part": 0.6, "close": 0.55}[name_kind] - (0.1 if addr_diff else 0)
    return "similar", round(score, 2), reasons


def _queries(r: PlaceRecord, road: Optional[str], buildings: list[str], name_unsure: bool) -> list[str]:
    a = r.place.address
    # 해시태그에서 찾은 이름은 주소가 없으므로 캡션의 지역 단서(#잠실맛집 → 잠실)로 검색 범위를 좁힘
    hint = _region_hint(r)
    area = a.dong or a.sigungu or _sigungu(a.road) or hint
    full = f"{r.place.name} {r.place.branch or ''}".strip()
    name_q = [f"{area} {full}", full]
    road_q = []
    if road and _road_core(a.road):  # 주소로도 찾기: '종로구 동숭길 68' (상호명이 깨진 캡쳐도 주소는 대개 정확)
        tail = _ROAD_CORE.search(_join_road(a.road)).group(1)
        road_q = [f"{a.sigungu or _sigungu(a.road) or ''} {tail}".strip(), f"{tail} {r.place.name}"]
    bld_q = [f"{b[0]}{b[1]} {r.place.name}" for b in buildings[:1]]
    qs = (road_q + name_q) if name_unsure else (name_q + road_q + bld_q)
    return list(dict.fromkeys(q.strip() for q in qs if q.strip()))


def candidates_from_records(records: list[PlaceRecord], search: SearchProvider, input_kind: str,
                            is_saved=lambda pid: False, pipeline_message: Optional[str] = None) -> ResolveResult:
    """추출 파이프라인 결과(레코드들) → 장소별 묶음(PlaceGroup)으로 후보 찾기.

    링크에 장소가 여러 곳이면(릴스 '맛집 4곳') 모두 따로 검색하고, 묶음 제목은 링크에 나온 이름·순서를 그대로 씀.
    이름 일부만 같은 다른 가게(오지다 → 회포차오지다)는 similar 로 내려 기본 선택하지 않음.
    """
    if not records:
        status = "recognition_failed" if input_kind == "screenshot" else "link_unreadable"
        msg = ("장소 이름이나 주소로 직접 찾아보세요. 장소 이름이 잘 보이는 다른 이미지를 선택해도 좋아요."
               if status == "recognition_failed" else
               ((pipeline_message or "링크에서 장소를 찾지 못했어요.") + " "
                + "네이버 지도·인스타 앱의 '공유 → 복사'로 받은 문구 전체를 붙여넣거나, 스크린샷·직접 검색으로 찾아 주세요."))
        return ResolveResult(status=status, message=msg)

    recs = records[:MAX_GROUPS]
    raw = recs[0].content.raw_excerpt or ""
    # 같은 링크의 장소들이 모인 구(예: 송파구) — 같은 이름 가게가 여러 지점일 때 우선
    gus = [g for g in (_sigungu(r.place.address.road) for r in recs) if g]
    home_gu = max(set(gus), key=gus.count) if gus else None

    groups: list[PlaceGroup] = []
    search_err = None
    for i, r in enumerate(recs):
        a = r.place.address
        hint = " ".join(x for x in (a.road, a.jibun) if x) or None
        ex = Extracted(order=i + 1, name=r.place.name, branch=r.place.branch, address_hint=hint,
                       category=r.classification.primary_label)
        name_unsure = any(e.startswith("상호명 인식 불확실") for e in r.content.evidence)
        road = _road_core(a.road)
        expected_gus = _area_gus(_region_hint(r)) or ((home_gu,) if home_gu and len(gus) >= 2 else ())
        road_gu = _sigungu(a.road)
        if road and road_gu and expected_gus and road_gu not in expected_gus and not r.extractor.startswith("heuristic"):
            road = None  # 게시물 지역과 다른 구의 주소는 LLM 이 잘못 읽은 것으로 보고 이름 + 지역으로 찾음
        block = _block_of(raw, r.place.name, recs[i + 1].place.name if i + 1 < len(recs) else None)
        buildings = [m for m in _BUILDING.findall(block) if len(m[0]) >= 2][:2]
        pool: dict[str, Candidate] = {}
        failed = False
        for q in _queries(r, road, buildings, name_unsure):
            try:
                results = search.search(q, MAX_CANDIDATES)
            except ExternalSearchError as e:
                search_err, failed = str(e), True
                break
            for p in results:
                m = match(r.place.name, r.place.branch, road, buildings, p, name_unsure)
                if not m:
                    continue
                mtype, score, reasons = m
                cand_addr = p.road_address or p.jibun_address or ""
                if mtype == "exact" and not road and expected_gus and cand_addr \
                        and not any(gu in cand_addr for gu in expected_gus):
                    mtype, score, reasons = "similar", min(score, 0.6), reasons + ["다른 지역"]
                if home_gu and _sigungu(p.road_address) == home_gu and not road:
                    score, reasons = min(1.0, score + 0.03), reasons + ["같은 동네"]
                old = pool.get(p.place_id)
                rank = lambda t, sc: (t == "exact", sc)  # noqa: E731
                if not old or rank(old.match_type, old.match_score) < rank(mtype, score):
                    pool[p.place_id] = Candidate(place=p, match_score=score, match_type=mtype, reasons=reasons,
                                                 already_saved=is_saved(p.place_id), from_text=r.place.name)
            if any(c.match_type == "exact" and "주소 일치" in c.reasons for c in pool.values()):
                break
        direct = _place_from_record(r)  # 링크에서 좌표까지 얻었으면 그 자체가 후보
        if direct and not any(c.match_type == "exact" for c in pool.values()):
            pool[direct.place_id] = Candidate(place=direct, match_score=0.9, match_type="exact", reasons=["링크 정보"],
                                              already_saved=is_saved(direct.place_id), from_text=r.place.name)
        ranked = sorted(pool.values(), key=lambda c: (c.match_type == "exact", c.match_score), reverse=True)
        best = ranked[0] if ranked and ranked[0].match_type == "exact" else None
        others = [c for c in ranked if c is not best][:MAX_OTHERS if best else MAX_OTHERS + 1]
        for o in others:  # 1순위가 있으면 나머지 '이름 일치'는 다른 지점 → 기본 선택 안 함
            if o.match_type == "exact":
                o.match_type = "similar"
                o.reasons = [x for x in o.reasons if x != "이름 일치"] + ["같은 이름의 다른 지점"]
        status = "matched" if best else "similar_only" if others else "search_failed" if failed else "not_found"
        groups.append(PlaceGroup(extracted=ex, status=status, best=best, others=others))

    flat = [g.best for g in groups if g.best] + [c for g in groups for c in g.others]
    extracted = [g.extracted for g in groups]
    first = recs[0].place.name
    if any(e.startswith("상호명 인식 불확실") for e in recs[0].content.evidence) and not flat:
        first = recs[0].place.address.road or first
    if flat:
        return ResolveResult(status="candidates", groups=groups, candidates=flat, extracted=extracted,
                             suggested_query=first)
    if search_err:
        return ResolveResult(status="search_failed", groups=groups, extracted=extracted, suggested_query=first,
                             message="장소 검색을 불러오지 못했어요. 잠시 후 직접 검색으로 다시 찾아보세요.")
    return ResolveResult(status="no_match", groups=groups, extracted=extracted, suggested_query=first,
                         message=f"'{first}'(으)로 찾은 장소가 없어요. 직접 검색으로 찾아보세요.")


def save_candidate(repo, owner_id: str, place: Place, origin: str, source_url: Optional[str] = None,
                   share_with_couple: bool = False) -> SavedPlace:
    """후보 확인 → '개인 보관함에 저장'. 같은 장소를 다시 저장하면 기존 항목을 돌려줌(멱등)."""
    if not place.category_label or place.category_label == "기타":
        place.category_label = taxonomy.PRIMARY.get(place.category, {"label": "기타"})["label"]
    return repo.save_place(owner_id, place, origin=origin, source_url=source_url, share_with_couple=share_with_couple)
