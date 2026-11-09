"""내부 모델(placeflow.v3, snake_case) ↔ 내부 API DTO(camelCase) 변환."""
from __future__ import annotations

from typing import Iterable, Optional

from .. import hours as hours_mod
from ..v3.category_infer import infer_category, with_code
from ..v3.domain import Place
from ..v3.recommend import RecommendResult
from ..v3.save_flow import Candidate, ResolveResult
from ..v3.walking import distance_m
from .schemas import (CourseRecommendationResponse, CoursePlaceOut, PlaceCandidate, PlaceSnapshotIn, PlaceTag,
                      RecognitionCandidate, RecognitionGroup, RecognitionResponse, RecommendedCourseOut,
                      SavedPlaceIn, WalkingLegOut)

SAME_PLACE_M = 80
_SRC_IN = {"PERSONAL": "mine", "SHARED": "couple"}
_SRC_OUT = {"mine": "PERSONAL", "couple": "SHARED", "external": "EXTERNAL"}


def saved_key(place_id: int) -> str:
    return f"sp:{place_id}"


def saved_to_place(s: SavedPlaceIn | PlaceSnapshotIn, search=None) -> Place:
    """Spring 의 장소 → 내부 Place. 카테고리가 없으면 검색으로 추론(방법 b)."""
    pid = saved_key(s.place_id) if s.place_id else None
    p = Place(place_id=pid or f"snap:{s.name}:{s.latitude:.6f}:{s.longitude:.6f}", name=s.name,
              road_address=s.address, lat=s.latitude, lng=s.longitude, provider="backend")
    p = with_code(p, s.category_code)
    if not p.category_code and search is not None:
        p = infer_category(p, search)
    return p


def saved_pool(saved: Iterable[SavedPlaceIn], search) -> tuple[list[tuple[Place, str]], dict[str, SavedPlaceIn]]:
    pool, by_key = [], {}
    for s in saved:
        p = saved_to_place(s, search)
        pool.append((p, _SRC_IN[s.source]))
        by_key[p.place_id] = s
    return pool, by_key


def _same(a_name: str, a_pos, b_name: str, b_pos) -> bool:
    return a_name.replace(" ", "") == b_name.replace(" ", "") and distance_m(a_pos, b_pos) <= SAME_PLACE_M


def find_saved(p: Place, saved: list[SavedPlaceIn]) -> Optional[int]:
    for s in saved:
        if _same(p.name, p.pos, s.name, (s.latitude, s.longitude)):
            return s.place_id
    return None


_PROVIDER_OUT = {"kakao_local": "KAKAO", "demo": "DEMO"}


def place_ids(p: Place) -> dict:
    """제공자·제공자 장소 ID·도로명/지번 주소 (후보·코스 장소 공통). 저장 장소(backend)·링크 장소는 제공자 null."""
    provider = _PROVIDER_OUT.get(p.provider)
    return dict(provider=provider, provider_place_id=p.provider_place_id if provider else None,
                road_address=p.road_address, jibun_address=p.jibun_address)


def candidate(p: Place, center: Optional[tuple[float, float]] = None,
              saved: Optional[list[SavedPlaceIn]] = None, cls=PlaceCandidate, **extra):
    hours = hours_mod.summary(p.opening_hours) if p.opening_hours and not p.opening_hours.partial else None
    return cls(name=p.name, address=p.road_address or p.jibun_address, latitude=p.lat, longitude=p.lng,
               source_url=p.map_url, category_label=p.category_label if p.category != "other" else None,
               category_code=p.category_code, opening_hours=hours,
               distance_meters=round(distance_m(center, p.pos)) if center else None,
               saved_place_id=find_saved(p, saved) if saved else None, **place_ids(p), **extra)


def _rcand(c: Candidate, saved: list[SavedPlaceIn], tags: Optional[dict] = None) -> RecognitionCandidate:
    t = (tags or {}).get(c.place.place_id)
    return candidate(c.place, saved=saved, cls=RecognitionCandidate,
                     match_type="EXACT" if c.match_type == "exact" else "SIMILAR", match_reasons=c.reasons,
                     tags=[PlaceTag(label=l, mention_count=n) for l, n in t.tags] if t else [],
                     tag_post_count=t.post_count if t else None)


def recognition_places(res: ResolveResult) -> list[Place]:
    """태그를 붙일 장소: 묶음별 best → others, 그다음 나머지 후보 (응답 순서와 같게)."""
    out = []
    for g in res.groups:
        out += ([g.best.place] if g.best else []) + [o.place for o in g.others]
    return out + [c.place for c in res.candidates]


def recognition(res: ResolveResult, source_url: Optional[str], saved: list[SavedPlaceIn],
                tags: Optional[dict] = None, tag_source: Optional[str] = None) -> RecognitionResponse:
    """내부 상태 → TRD 상태: 후보 있음 CANDIDATES_FOUND / 그 밖(인식 실패·일치 없음·링크 못 읽음) NEEDS_SEARCH.
    외부 검색 장애(search_failed)는 호출하는 쪽에서 503 으로 바꿈. tags = {place_id: PlaceTags} (확장)."""
    groups = []
    for g in res.groups:
        groups.append(RecognitionGroup(
            order=g.extracted.order, extracted_name=g.extracted.name + (f" {g.extracted.branch}" if g.extracted.branch else ""),
            extracted_address=g.extracted.address_hint,
            status={"matched": "MATCHED", "similar_only": "SIMILAR_ONLY"}.get(g.status, "NOT_FOUND"),
            best=_rcand(g.best, saved, tags) if g.best else None, others=[_rcand(o, saved, tags) for o in g.others]))
    cands = [_rcand(c, saved, tags) for c in res.candidates]
    found = bool(cands)
    msg = res.message if not found else None
    if not found and not msg:
        msg = "장소를 직접 검색해 확인해 주세요."
    return RecognitionResponse(status="CANDIDATES_FOUND" if found else "NEEDS_SEARCH",
                               extracted_query=res.suggested_query, candidates=cands, source_url=source_url,
                               message=msg, groups=groups, tag_source=tag_source if cands else None)


def recommendation(res: RecommendResult, by_key: dict[str, SavedPlaceIn]) -> CourseRecommendationResponse:
    courses = []
    for c in res.courses:
        places = []
        for rp in c.places:
            s = by_key.get(rp.place.place_id)
            places.append(CoursePlaceOut(
                visit_order=rp.visit_order, name=rp.place.name, address=rp.place.road_address or rp.place.jibun_address,
                latitude=rp.place.lat, longitude=rp.place.lng, category_code=rp.place.category_code,
                category_label=rp.place.category_label if rp.place.category != "other" else None,
                source=_SRC_OUT[rp.source], place_id=s.place_id if s else None, is_anchor=rp.is_anchor,
                source_url=rp.place.map_url, **place_ids(rp.place)))
        courses.append(RecommendedCourseOut(
            candidate_key=c.candidate_key, title=c.title, places=places, total_walking_minutes=c.total_walk_min,
            walking_legs=[WalkingLegOut(from_visit_order=l.from_visit_order, to_visit_order=l.to_visit_order,
                                        estimated_minutes=l.estimated_minutes) for l in c.walking_legs]))
    return CourseRecommendationResponse(courses=courses, reason=res.reason)
