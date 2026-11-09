"""OneClickMeet V3 도메인 모델과 규칙 (Figma V3 확정 IA 기준).

모델
  Place        장소 원본(공유 가능한 정보). 여러 사용자의 보관함·코스가 같은 Place 를 가리킴
  SavedPlace   내 보관함 항목(하트 ON). 개인/공동 공유 여부, 원본 링크
  PrivateMemo  내 메모 — 작성자 본인에게만 보임 (공유해도 상대에게 안 보임)
  Course       코스 (장소 3~5곳). 개인 또는 공동
  CourseStop   코스 안의 장소 1곳 (기준 장소 여부, 외부 추천 여부, 장소 스냅샷)
  CourseItem   코스별 금액 항목 (항목명, 2인 합계 금액, 체크) — 코스마다 독립

규칙 (V3 주석 그대로)
  - 코스는 장소 3~5곳. 3곳 미만이면 저장 불가. 자동 재추천 없음. 기준 장소도 삭제 가능
  - 항목은 코스별 독립: 장소 '순서 변경' 시 유지, '교체·삭제' 시 그 장소의 항목만 제거
  - 금액은 사용자 입력 2인 합계. 합계는 '체크한 항목만' 더함. 체크 항목이 없는 장소 수를 따로 표시
  - 코스를 저장해도 장소는 보관함에 자동으로 담기지 않음 (하트로 따로 저장)
모든 편집 함수는 원본을 바꾸지 않고 새 Course 를 돌려줍니다(드래프트 → '코스 저장' 때 확정).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, computed_field, Field

from .. import hours as hours_mod
from ..models import WeeklyHours
from .walking import MAX_LEG_MIN, walk_minutes

MIN_STOPS, MAX_STOPS = 3, 5
HOURS_NOTICE = "방문 전에 운영시간을 확인해주세요."

Scope = Literal["personal", "couple"]
Origin = Literal["link", "screenshot", "search", "recommendation", "demo"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class DomainError(Exception):
    """규칙 위반. code 는 API 에러 코드로 그대로 쓰입니다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- 장소
class Place(BaseModel):
    place_id: str = Field(..., description="'nv_' + 해시 (네이버 지역검색 결과는 고유 ID 가 없어 이름+도로명주소로 생성)")
    name: str
    branch: Optional[str] = None
    category: str = Field("other", description="대분류: restaurant|cafe|bar|culture|activity|nature|shopping|stay|other")
    sub_category: Optional[str] = None
    category_label: str = Field("기타", description="화면 표시용 (맛집, 카페·디저트 …)")
    area_label: Optional[str] = Field(None, description="동네 (연남동 등)")
    road_address: Optional[str] = None
    jibun_address: Optional[str] = None
    lat: float
    lng: float
    phone: Optional[str] = None
    map_url: Optional[str] = None
    provider: str = Field("kakao_local", description="kakao_local | demo | link | backend")
    provider_place_id: Optional[str] = Field(None, description="제공자 장소 ID (카카오 장소 id). 링크·백엔드 장소는 None")
    opening_hours: Optional[WeeklyHours] = None
    tags: list[dict] = Field(default_factory=list,
                             description="블로그 태그 [{label, mention_count}] (링크·스크린샷으로 찾을 때 붙음, 저장 시 함께 보관)")
    tag_post_count: Optional[int] = Field(None, description="태그를 판단한 블로그 글 수. 태그를 구하지 않았으면 None")

    @property
    def pos(self) -> tuple[float, float]:
        return (self.lat, self.lng)

    @computed_field  # 응답 JSON 에 자동으로 포함 (요청에 들어 있으면 무시)
    @property
    def hours_notice(self) -> str:
        """장소 상세 '운영시간' 줄. 확인된 정보가 없으면 V3 안내 문구."""
        if self.opening_hours and not self.opening_hours.partial:
            return hours_mod.summary(self.opening_hours)
        return HOURS_NOTICE

    @computed_field
    @property
    def category_code(self) -> Optional[str]:
        """코스 추천 카테고리 코드 (MEAL·CAFE·BAR·ACTIVITY·CULTURE·WALK·SHOPPING). 숙박·기타는 None."""
        from .categories import code_of

        return code_of(self.category)

    @computed_field
    @property
    def subtitle(self) -> str:
        """카드 부제 '맛집 · 연남동'."""
        return " · ".join(x for x in (self.category_label, self.area_label) if x)


class SavedPlace(BaseModel):
    saved_id: str = Field(default_factory=lambda: new_id("sp"))
    owner_id: str
    place_id: str
    origin: Origin = "search"
    source_url: Optional[str] = Field(None, description="원본 링크 (인스타 릴스·블로그)")
    shared_with_couple: bool = False
    saved_at: str = Field(default_factory=now_iso)


class PrivateMemo(BaseModel):
    owner_id: str
    target_type: Literal["place", "course"]
    target_id: str
    text: str = Field("", max_length=1000)
    updated_at: str = Field(default_factory=now_iso)


# ---------------------------------------------------------------- 코스
class Anchor(BaseModel):
    type: Literal["place", "region"]
    place_id: Optional[str] = None
    region_id: Optional[str] = None


class CourseStop(BaseModel):
    stop_id: str = Field(default_factory=lambda: new_id("st"))
    place: Place
    is_anchor: bool = False
    external: bool = Field(False, description="외부 추천 (추천 시점에 내/공동 보관함에 없던 장소)")


class CourseItem(BaseModel):
    item_id: str = Field(default_factory=lambda: new_id("it"))
    stop_id: str
    name: str = Field(..., min_length=1, max_length=40)
    amount: int = Field(..., ge=0, le=10_000_000, description="2인 합계 금액(원)")
    checked: bool = True
    updated_by: Optional[str] = None
    updated_at: str = Field(default_factory=now_iso)


class Course(BaseModel):
    course_id: str = Field(default_factory=lambda: new_id("co"))
    owner_id: str
    scope: Scope = "personal"
    couple_id: Optional[str] = None
    title: str = "데이트 코스"
    anchor: Optional[Anchor] = None
    stops: list[CourseStop] = Field(default_factory=list)
    items: list[CourseItem] = Field(default_factory=list)
    version: int = Field(0, description="저장할 때마다 +1 (공동 코스 동시 수정 충돌 감지용)")
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class StopSummary(BaseModel):
    stop_id: str
    order: int
    name: str
    is_anchor: bool
    external: bool
    walk_from_prev_min: Optional[int] = None
    item_count: int
    checked_subtotal: int
    has_checked: bool


class CourseSummary(BaseModel):
    stops: list[StopSummary]
    confirmed_total: int = Field(..., description="확인된 금액 = 체크한 항목 합 (2인)")
    unchecked_stop_count: int = Field(..., description="'체크된 항목 없음 N곳'")
    total_walk_min: int
    long_legs: list[int] = Field(default_factory=list, description="도보 10분 초과 구간의 도착 순번")
    can_save: bool
    save_errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 규칙 함수 (순수 함수)
def _copy(c: Course) -> Course:
    return c.model_copy(deep=True)


def _stop_index(c: Course, stop_id: str) -> int:
    for i, s in enumerate(c.stops):
        if s.stop_id == stop_id:
            return i
    raise DomainError("stop_not_found", "코스에 없는 장소입니다.")


def reorder_stops(course: Course, stop_ids: list[str]) -> Course:
    """순서 변경. 항목은 그대로 유지."""
    c = _copy(course)
    if sorted(stop_ids) != sorted(s.stop_id for s in c.stops):
        raise DomainError("invalid_order", "현재 코스의 장소 목록과 순서 목록이 다릅니다.")
    by = {s.stop_id: s for s in c.stops}
    c.stops = [by[i] for i in stop_ids]
    return c


def replace_stop(course: Course, stop_id: str, place: Place, external: bool = False) -> Course:
    """장소 교체. 바뀐 장소의 항목은 제거 (새 장소는 항목 없음)."""
    c = _copy(course)
    i = _stop_index(c, stop_id)
    if any(s.place.place_id == place.place_id for j, s in enumerate(c.stops) if j != i):
        raise DomainError("duplicate_place", "이미 코스에 있는 장소입니다.")
    c.items = [it for it in c.items if it.stop_id != stop_id]
    c.stops[i] = CourseStop(place=place, external=external, is_anchor=False)
    return c


def remove_stop(course: Course, stop_id: str) -> Course:
    """장소 삭제 (기준 장소도 가능). 그 장소의 항목 제거. 3곳 미만이 되면 저장만 막힘."""
    c = _copy(course)
    i = _stop_index(c, stop_id)
    c.items = [it for it in c.items if it.stop_id != stop_id]
    del c.stops[i]
    return c


def add_stop(course: Course, place: Place, index: Optional[int] = None, external: bool = False) -> Course:
    """장소 추가 (보관함에서 고르기). 최대 5곳."""
    c = _copy(course)
    if len(c.stops) >= MAX_STOPS:
        raise DomainError("too_many_stops", f"코스는 최대 {MAX_STOPS}곳까지예요.")
    if any(s.place.place_id == place.place_id for s in c.stops):
        raise DomainError("duplicate_place", "이미 코스에 있는 장소입니다.")
    stop = CourseStop(place=place, external=external)
    c.stops.insert(len(c.stops) if index is None else max(0, min(index, len(c.stops))), stop)
    return c


def add_item(course: Course, stop_id: str, name: str, amount: int, checked: bool = True,
             by: Optional[str] = None) -> Course:
    c = _copy(course)
    _stop_index(c, stop_id)
    c.items.append(CourseItem(stop_id=stop_id, name=name.strip(), amount=amount, checked=checked, updated_by=by))
    return c


def update_item(course: Course, item_id: str, *, name: Optional[str] = None, amount: Optional[int] = None,
                checked: Optional[bool] = None, by: Optional[str] = None) -> Course:
    c = _copy(course)
    for i, it in enumerate(c.items):
        if it.item_id == item_id:
            data = it.model_dump()
            if name is not None:
                data["name"] = name.strip()
            if amount is not None:
                data["amount"] = amount
            if checked is not None:
                data["checked"] = checked
            data.update(updated_by=by or it.updated_by, updated_at=now_iso())
            c.items[i] = CourseItem.model_validate(data)
            return c
    raise DomainError("item_not_found", "항목을 찾을 수 없습니다.")


def remove_item(course: Course, item_id: str) -> Course:
    c = _copy(course)
    before = len(c.items)
    c.items = [it for it in c.items if it.item_id != item_id]
    if len(c.items) == before:
        raise DomainError("item_not_found", "항목을 찾을 수 없습니다.")
    return c


def validate_for_save(course: Course) -> list[str]:
    """'코스 저장' 가능 여부. 빈 리스트면 저장 가능."""
    errs = []
    n = len(course.stops)
    if n < MIN_STOPS:
        errs.append("too_few_stops")
    if n > MAX_STOPS:
        errs.append("too_many_stops")
    ids = [s.place.place_id for s in course.stops]
    if len(ids) != len(set(ids)):
        errs.append("duplicate_place")
    stop_ids = {s.stop_id for s in course.stops}
    if any(it.stop_id not in stop_ids for it in course.items):
        errs.append("orphan_item")
    return errs


def summarize(course: Course) -> CourseSummary:
    """코스 상세 하단 고정 영역(합계·누락·저장 가능)과 장소별 소계."""
    stops, total, unchecked, walk, long_legs = [], 0, 0, 0, []
    for i, s in enumerate(course.stops):
        items = [it for it in course.items if it.stop_id == s.stop_id]
        checked = [it for it in items if it.checked]
        sub = sum(it.amount for it in checked)
        leg = walk_minutes(course.stops[i - 1].place.pos, s.place.pos) if i else None
        if leg:
            walk += leg
            if leg > MAX_LEG_MIN:
                long_legs.append(i + 1)
        total += sub
        unchecked += 0 if checked else 1
        stops.append(StopSummary(stop_id=s.stop_id, order=i + 1, name=s.place.name, is_anchor=s.is_anchor,
                                 external=s.external, walk_from_prev_min=leg, item_count=len(items),
                                 checked_subtotal=sub, has_checked=bool(checked)))
    errs = validate_for_save(course)
    return CourseSummary(stops=stops, confirmed_total=total, unchecked_stop_count=unchecked, total_walk_min=walk,
                         long_legs=long_legs, can_save=not errs, save_errors=errs)
