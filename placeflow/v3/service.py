"""V3 서비스 계층 — 화면(엔드포인트) 단위의 함수 모음.

백엔드는 이 클래스를 그대로 import 해서 쓰거나(같은 Python 서버), 메서드 시그니처를 참고해 다른 언어로 옮기면 됩니다.
모든 메서드는 JSON 직렬화 가능한 dict 를 돌려주고, 규칙 위반은 DomainError(code, message) 를 올립니다.
자세한 계약은 docs/API_CONVENTION.md 참고.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .domain import (Anchor, Course, DomainError, Place, add_item, add_stop, remove_item, remove_stop, reorder_stops,
                     replace_stop, summarize, update_item, validate_for_save)
from .recommend import (RecommendedCourse, RecommendRequest, nearby_places, recommend, replacement_candidates,
                        to_course_draft)
from .categories import CODES, LABELS
from .category_infer import infer_category
from .regions import REGIONS, get_region
from .repo import JsonRepo
from .save_flow import candidates_from_records, save_candidate, search_candidates
from .search import ExternalSearchError, SearchProvider
from .walking import distance_m


def _place_card(p: Place, **extra) -> dict:
    d = p.model_dump(mode="json")
    d.update(extra)
    return d


class OneClickMeetService:
    def __init__(self, repo: JsonRepo, search: SearchProvider, pipeline_factory: Optional[Callable] = None,
                 tagger=None):
        """pipeline_factory(): 링크·스크린샷 추출 파이프라인(placeflow.Pipeline)을 돌려주는 함수.
        tagger: (선택) blog_tags.BlogTagger — 링크·스크린샷 후보에 네이버 블로그 태그를 붙임."""
        self.repo = repo
        self.search = search
        self.pipeline_factory = pipeline_factory
        self.tagger = tagger

    # ============================================================ 홈 · 지역
    def home(self, user_id: str) -> dict:
        saved = self.repo.list_saved(user_id, "personal")
        courses = self.repo.list_courses(user_id, "personal") + self.repo.list_courses(user_id, "couple")
        courses.sort(key=lambda c: c.updated_at, reverse=True)
        return {
            "saved_place_count": len(saved),
            "saved_course_count": len(courses),
            "couple_linked": self.repo.couple_id_of(user_id) is not None,
            "recent_courses": [self._course_card(c) for c in courses[:2]],
            "recent_places": [_place_card(p, saved_at=sp.saved_at) for sp, p in saved[:6]],
        }

    def regions(self, user_id: str) -> list[dict]:
        """기준 지역 선택 화면: 지원 지역 6곳 + 각 지역의 내 저장 장소 수."""
        pool = self.repo.recommendation_pool(user_id)
        return [{"region_id": r.region_id, "name": r.name,
                 "saved_count": sum(1 for p, _ in pool if distance_m((r.lat, r.lng), p.pos) <= r.radius_m)}
                for r in REGIONS]

    # ============================================================ 장소 저장
    def resolve_place(self, user_id: str, kind: str, *, url: Optional[str] = None, text: Optional[str] = None,
                      image_paths: Optional[list[str]] = None, query: Optional[str] = None) -> dict:
        """링크 / 스크린샷 / 직접 검색 → 후보 확인 목록. kind: link | screenshot | search | text"""
        is_saved = lambda pid: self.repo.is_saved(user_id, pid)  # noqa: E731
        if kind == "search":
            return self._with_cards(search_candidates(self.search, query or "", is_saved))
        if self.pipeline_factory is None:
            raise DomainError("extractor_unavailable", "추출 파이프라인이 설정되지 않았습니다.")
        pipe = self.pipeline_factory()
        if kind == "screenshot":
            if not image_paths:
                raise DomainError("invalid_input", "이미지를 선택해 주세요.")
            out = pipe.run(image_paths[0], text, more_images=image_paths[1:] or None)
        elif kind == "link":
            if not url:
                raise DomainError("invalid_input", "링크를 붙여넣어 주세요.")
            out = pipe.run(url, text)
        elif kind == "text":
            out = pipe.run(text or "")
        else:
            raise DomainError("invalid_input", f"알 수 없는 입력 종류: {kind}")
        res = candidates_from_records(out.records, self.search, kind, is_saved, out.message)
        tags, source = None, None
        if self.tagger is not None:  # 블로그 태그: 실패해도 후보는 그대로
            places = [c.place for g in res.groups for c in ([g.best] if g.best else []) + list(g.others)]
            tags, ok = self.tagger.tag_many(places + [c.place for c in res.candidates])
            source = "naver_blog" if ok else None
        return self._with_cards(res, tags, source)

    @staticmethod
    def _with_cards(res, tags: Optional[dict] = None, tag_source: Optional[str] = None) -> dict:
        """후보 Place 에 화면 표시용 subtitle·hours_notice (+ 블로그 태그) 를 붙임 (groups 와 candidates 모두)."""
        d = res.model_dump(mode="json")
        d["tag_source"] = tag_source

        def card(p: Place) -> dict:
            t = (tags or {}).get(p.place_id)
            return _place_card(p, tags=[{"label": l, "mention_count": n} for l, n in t.tags] if t else [],
                               tag_post_count=t.post_count if t else None)

        cards = {}
        for c in res.candidates:
            cards[c.place.place_id] = card(c.place)
        for g in res.groups:
            for c in ([g.best] if g.best else []) + list(g.others):
                cards.setdefault(c.place.place_id, card(c.place))
        for c in d["candidates"]:
            c["place"] = cards[c["place"]["place_id"]]
        for g in d.get("groups", []):
            for c in ([g["best"]] if g.get("best") else []) + g["others"]:
                c["place"] = cards[c["place"]["place_id"]]
        return d

    def save_place(self, user_id: str, place: dict, origin: str = "search", source_url: Optional[str] = None,
                   share_with_couple: bool = False) -> dict:
        """후보 확인 → 저장 (하트 ON). share_with_couple 은 커플 연결 중일 때만 의미가 있음."""
        p = Place.model_validate(place)
        if share_with_couple and self.repo.couple_id_of(user_id) is None:
            share_with_couple = False
        sp = save_candidate(self.repo, user_id, p, origin, source_url, share_with_couple)
        return {"saved": sp.model_dump(mode="json"), "place": _place_card(self.repo.get_place(p.place_id))}

    def save_places(self, user_id: str, places: list[dict], origin: str = "link", source_url: Optional[str] = None,
                    share_with_couple: bool = False) -> dict:
        """후보 확인(장소 여러 곳) → '선택한 N곳 저장'. 하나라도 형식이 틀리면 아무것도 저장하지 않음."""
        if not places:
            raise DomainError("invalid_input", "저장할 장소를 선택해 주세요.")
        if len(places) > 10:
            raise DomainError("invalid_input", "한 번에 10곳까지 저장할 수 있어요.")
        parsed = [Place.model_validate(p) for p in places]  # 검증 먼저
        items = [self.save_place(user_id, p.model_dump(mode="json"), origin, source_url, share_with_couple)
                 for p in parsed]
        return {"count": len(items), "items": items}

    def unsave_place(self, user_id: str, place_id: str) -> dict:
        """하트 OFF (내 보관함에서 저장 해제). 코스 안의 장소에는 영향 없음."""
        return {"removed": self.repo.unsave_place(user_id, place_id)}

    def list_saved(self, user_id: str, scope: str = "personal") -> dict:
        items = [{"saved": sp.model_dump(mode="json"), "place": _place_card(p)}
                 for sp, p in self.repo.list_saved(user_id, scope)]
        return {"scope": scope, "count": len(items), "items": items}

    def place_detail(self, user_id: str, place_id: str) -> dict:
        p = self.repo.get_place(place_id)
        if not p:
            raise DomainError("place_not_found", "장소를 찾을 수 없습니다.")
        sp = next((s for s, pp in self.repo.list_saved(user_id, "personal") if pp.place_id == place_id), None)
        memo = self.repo.get_memo(user_id, "place", place_id)
        near = nearby_places(p, self.repo.recommendation_pool(user_id), self.search)
        for c in near:  # 외부 장소도 상세를 열 수 있게 장소 원본만 등록 (보관함 저장 아님)
            if c.source == "external":
                self.repo.upsert_place(c.place)
        return {
            "place": _place_card(p),
            "hearted": sp is not None,
            "source_url": sp.source_url if sp else None,
            "shared_with_couple": bool(sp and sp.shared_with_couple),
            "my_memo": memo.text if memo else "",
            "nearby": [_place_card(c.place, source=c.source) for c in near],
        }

    def set_memo(self, user_id: str, target_type: str, target_id: str, text: str) -> dict:
        """내 메모 (나만 보여요). 빈 문자열이면 삭제."""
        return self.repo.set_memo(user_id, target_type, target_id, text).model_dump(mode="json")

    # ============================================================ 추천
    def recommend(self, user_id: str, anchor: dict, place_count: int, category_sequence: list[str],
                  required_place_order: Optional[int] = None, exclude_keys: Optional[list[str]] = None) -> dict:
        """코스 추천 (R01). 실험 화면용: 외부 검색 실패는 reason=EXTERNAL_API_FAILED 로 돌려줌 (AI 서버는 503)."""
        a = Anchor.model_validate(anchor)
        anchor_place = self.repo.get_place(a.place_id) if a.type == "place" and a.place_id else None
        if anchor_place is not None:
            anchor_place = infer_category(anchor_place, self.search)
        req = RecommendRequest(anchor=a, place_count=place_count, category_sequence=category_sequence,
                               required_place_order=required_place_order if a.type == "place" else None,
                               exclude_keys=exclude_keys or [])
        try:
            pool = [(infer_category(p, self.search), src) for p, src in self.repo.recommendation_pool(user_id)]
            res = recommend(req, pool, self.search, anchor_place)
        except ExternalSearchError as e:
            return {"courses": [], "reason": "EXTERNAL_API_FAILED", "walking_time_method": "COORDINATE_ESTIMATE",
                    "region": None, "candidates_count": 0, "steps": [],
                    "message": f"주변 장소를 불러오지 못했어요. 이번에는 코스를 만들지 않아요. ({e})"}
        return res.model_dump(mode="json")

    @staticmethod
    def categories() -> list[dict]:
        """추천 카테고리 코드 7개 (화면의 카테고리 순서 선택용)."""
        return [{"code": c, "label": LABELS[c]} for c in CODES]

    def recommend_conditions(self, user_id: str, anchor: dict) -> dict:
        """추천 조건 확인 화면 문구."""
        a = Anchor.model_validate(anchor)
        if a.type == "place":
            p = self.repo.get_place(a.place_id or "")
            base = {"label": "기준 장소", "value": p.name if p else None, "note": "코스에 꼭 포함돼요"}
        else:
            r = get_region(a.region_id or "")
            base = {"label": "기준 지역", "value": r.name if r else None, "note": "이 지역 안에서 찾아요"}
        if a.type == "place" and p is not None:
            p = infer_category(p, self.search)
            base["category_code"] = p.category_code
        return {"anchor": base, "categories": self.categories(),
                "rules": ["도보로 걷는 데이트 코스예요", "기준 반경 약 1km 안에서 찾아요",
                          "장소 사이는 걸어서 10분 이내예요 (좌표로 추정한 시간)", "장소 수와 방문 순서별 카테고리를 골라요 (3~5곳)"]}

    def replacement_candidates(self, user_id: str, place_ids: list[str], index: int) -> dict:
        places = [self.repo.get_place(pid) for pid in place_ids]
        if any(p is None for p in places) or not 0 <= index < len(places):
            raise DomainError("invalid_input", "코스 장소 정보가 올바르지 않습니다.")
        cands = replacement_candidates(places, index, self.repo.recommendation_pool(user_id), self.search)
        return {"candidates": [_place_card(c.place, source=c.source) for c in cands]}

    # ============================================================ 코스 (드래프트 → 저장)
    def draft_from_recommendation(self, user_id: str, recommended: dict, anchor: dict) -> dict:
        """'이 코스 선택' → 코스 상세 드래프트. 추천에 쓰인 장소는 Place 원본으로 등록(보관함 저장은 아님)."""
        rec = RecommendedCourse.model_validate(recommended)
        for s in rec.places:
            self.repo.upsert_place(s.place)
        c = to_course_draft(rec, user_id, Anchor.model_validate(anchor))
        return self._course_payload(c, user_id)

    def apply_op(self, user_id: str, course: dict, op: dict) -> dict:
        """드래프트 편집 1건 적용 (저장 전). op.type:
        reorder{stop_ids} | replace{stop_id, place, external} | remove_stop{stop_id} | add_stop{place, index?}
        | add_item{stop_id, name, amount, checked?} | update_item{item_id, name?, amount?, checked?} | remove_item{item_id}"""
        c = Course.model_validate(course)
        t = op.get("type")
        if t == "reorder":
            c = reorder_stops(c, op["stop_ids"])
        elif t == "replace":
            place = Place.model_validate(op["place"])
            self.repo.upsert_place(place)
            c = replace_stop(c, op["stop_id"], place, external=not self.repo.is_saved(user_id, place.place_id))
        elif t == "remove_stop":
            c = remove_stop(c, op["stop_id"])
        elif t == "add_stop":
            place = Place.model_validate(op["place"])
            c = add_stop(c, place, op.get("index"), external=not self.repo.is_saved(user_id, place.place_id))
        elif t == "add_item":
            c = add_item(c, op["stop_id"], op["name"], int(op["amount"]), op.get("checked", True), by=user_id)
        elif t == "update_item":
            c = update_item(c, op["item_id"], name=op.get("name"),
                            amount=int(op["amount"]) if op.get("amount") is not None else None,
                            checked=op.get("checked"), by=user_id)
        elif t == "remove_item":
            c = remove_item(c, op["item_id"])
        else:
            raise DomainError("invalid_op", f"알 수 없는 편집: {t}")
        return self._course_payload(c, user_id, include_memo=False)

    def save_course(self, user_id: str, course: dict, expected_version: Optional[int] = None,
                    memo: Optional[str] = None) -> dict:
        """'코스 저장'. 3~5곳 검사 → 저장(version+1). 코스 안 장소를 보관함에 자동으로 담지 않음."""
        c = Course.model_validate(course)
        errs = validate_for_save(c)
        if errs:
            raise DomainError(errs[0], {"too_few_stops": "3곳 미만이면 저장할 수 없어요.",
                                        "too_many_stops": "코스는 최대 5곳까지예요.",
                                        "duplicate_place": "같은 장소가 두 번 들어 있어요.",
                                        "orphan_item": "코스에 없는 장소의 항목이 있어요."}[errs[0]])
        existing = self.repo.get_course(c.course_id)
        if existing and existing.owner_id not in self.repo.couple_members(user_id):
            raise DomainError("forbidden", "이 코스를 수정할 권한이 없어요.")
        if existing is None:
            c.owner_id = user_id
        for s in c.stops:
            self.repo.upsert_place(s.place)
        saved = self.repo.save_course(c, expected_version)
        if memo is not None:
            self.repo.set_memo(user_id, "course", saved.course_id, memo)
        return self._course_payload(saved, user_id)

    def list_courses(self, user_id: str, scope: str = "personal") -> dict:
        cs = self.repo.list_courses(user_id, scope)
        return {"scope": scope, "items": [self._course_card(c) for c in cs]}

    def get_course(self, user_id: str, course_id: str) -> dict:
        c = self.repo.get_course(course_id)
        if not c or c.owner_id not in self.repo.couple_members(user_id):
            raise DomainError("course_not_found", "코스를 찾을 수 없습니다.")
        return self._course_payload(c, user_id)

    def delete_course(self, user_id: str, course_id: str) -> dict:
        c = self.repo.get_course(course_id)
        if not c or c.owner_id != user_id:
            raise DomainError("course_not_found", "코스를 찾을 수 없습니다.")
        return {"deleted": self.repo.delete_course(course_id)}

    # ============================================================ 실험용
    def seed_demo(self, user_id: str, region_id: str = "YEONNAM", n: int = 4) -> dict:
        """데모: 지역의 가상 장소 몇 곳을 내 보관함에 저장 (검색 제공자가 demo 일 때)."""
        r = get_region(region_id)
        if not r:
            raise DomainError("invalid_input", "알 수 없는 지역")
        picked = self.search.search(f"{r.area_query} 맛집", 2) + self.search.search(f"{r.area_query} 카페", 1) \
            + self.search.search(f"{r.area_query} 놀거리", 1)
        for p in picked[:n]:
            self.repo.save_place(user_id, p, origin="demo")
        return {"saved": [p.name for p in picked[:n]]}

    # ============================================================ 내부
    def _course_card(self, c: Course) -> dict:
        return {"course_id": c.course_id, "title": c.title, "scope": c.scope, "stop_count": len(c.stops),
                "stop_names": [s.place.name for s in c.stops], "updated_at": c.updated_at, "version": c.version}

    def _course_payload(self, c: Course, user_id: str, include_memo: bool = True) -> dict:
        out = {"course": c.model_dump(mode="json"), "summary": summarize(c).model_dump(mode="json")}
        if include_memo:
            m = self.repo.get_memo(user_id, "course", c.course_id)
            out["my_memo"] = m.text if m else ""
        return out
