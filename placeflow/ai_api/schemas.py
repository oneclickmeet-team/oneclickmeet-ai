"""AI 내부 API(Spring Boot → FastAPI) DTO. TRD 공통 계약: JSON camelCase · 성공 DTO 직접 반환 · 오류 ProblemDetail.

요청은 camelCase(정식)와 snake_case 를 모두 받고, 응답은 camelCase 로 내보낸다.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from ..v3.categories import CategoryCode

MAX_SAVED_PLACES = 500


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ------------------------------------------------------------------ 오류 (application/problem+json)
class FieldError(CamelModel):
    field: str
    message: str


class Problem(CamelModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str = Field(..., description="사람이 읽는 설명. 클라이언트는 파싱하지 않는다")
    instance: Optional[str] = None
    code: str = Field(..., examples=["COMMON-0400"], description="도메인-HTTP 상태 코드")
    reason: str = Field(..., examples=["INVALID_INPUT"])
    errors: Optional[list[FieldError]] = None


# ------------------------------------------------------------------ 공통 입력
class SavedPlaceIn(CamelModel):
    """Spring 이 보내는 저장 장소 1곳 (개인 또는 공동). AI 서버는 저장하지 않는다."""
    place_id: int = Field(..., gt=0, description="Spring 의 장소 ID (공동 장소도 이 값 하나로 식별)")
    source: Literal["PERSONAL", "SHARED"] = "PERSONAL"
    name: str = Field(..., min_length=1, max_length=255)
    address: Optional[str] = Field(None, max_length=500)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    category_code: Optional[CategoryCode] = Field(None, description="있으면 그대로 사용, 없으면 AI 가 검색으로 추론")


class PlaceSnapshotIn(CamelModel):
    """코스 안의 장소 (저장 장소가 아닐 수도 있음). 좌표 필수."""
    place_id: Optional[int] = Field(None, gt=0, description="저장 장소면 Spring 의 ID")
    source: Literal["PERSONAL", "SHARED", "EXTERNAL"] = "EXTERNAL"
    name: str = Field(..., min_length=1, max_length=255)
    address: Optional[str] = Field(None, max_length=500)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    category_code: Optional[CategoryCode] = None


# ------------------------------------------------------------------ 장소 후보
class PlaceCandidate(CamelModel):
    """저장 전 확인용 장소 후보 (TRD CandidateSearchResponse.items). placeId 를 갖는 저장 본문이 아니다."""
    name: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    source_url: Optional[str] = Field(None, description="장소 상세 페이지 (카카오맵·네이버 등)")
    category_label: Optional[str] = None
    category_code: Optional[CategoryCode] = None
    opening_hours: Optional[str] = Field(None, description="확보했을 때만. 없으면 앱이 '운영시간 확인 필요' 안내")
    distance_meters: Optional[int] = Field(None, description="좌표 검색일 때 기준점까지 거리")
    saved_place_id: Optional[int] = Field(None, description="(확장) 요청의 savedPlaces 중 같은 장소가 있으면 그 placeId")


class CandidateSearchResponse(CamelModel):
    items: list[PlaceCandidate]
    page: int
    size: int
    has_next: bool


class ReplacementRequest(CamelModel):
    """(확장) 코스 편집 '교체': 앞·뒤 장소 양쪽에서 도보 10분 이내인 후보."""
    course_places: list[PlaceSnapshotIn] = Field(..., min_length=1, max_length=5, description="지금 코스 장소 (방문 순서대로)")
    replace_visit_order: int = Field(..., ge=1, le=5, description="교체할 장소의 방문 순서 (1부터)")
    category_code: Optional[CategoryCode] = Field(None, description="없으면 교체할 장소와 같은 카테고리")
    saved_places: list[SavedPlaceIn] = Field(default_factory=list, max_length=MAX_SAVED_PLACES)
    size: int = Field(8, ge=1, le=20)


class ReplacementCandidate(PlaceCandidate):
    place_source: Literal["PERSONAL", "SHARED", "EXTERNAL"]
    place_id: Optional[int] = None
    walk_from_prev_minutes: Optional[int] = None
    walk_to_next_minutes: Optional[int] = None


class ReplacementResponse(CamelModel):
    items: list[ReplacementCandidate]


# ------------------------------------------------------------------ 장소 인식
class RecognitionRequest(CamelModel):
    """P03 링크·텍스트 인식. sourceUrl·sharedText 중 하나 이상 필수."""
    source_url: Optional[str] = Field(None, max_length=2048)
    shared_text: Optional[str] = Field(None, max_length=20000, description="공유 텍스트·캡션 (앱 '공유 → 복사' 문구 포함)")
    saved_places: list[SavedPlaceIn] = Field(default_factory=list, max_length=MAX_SAVED_PLACES,
                                             description="(확장) 보내면 같은 장소 후보에 savedPlaceId 표시")


class PlaceTag(CamelModel):
    """(확장) 네이버 블로그 제목·요약을 OpenAI 가 읽고 고른 장소 태그. 네이버의 실제 리뷰 선택 횟수가 아니다."""
    label: str = Field(..., description="태그 (예: 분위기가 좋아요)")
    mention_count: int = Field(..., ge=1, description="이 태그에 해당한다고 판단된 블로그 글 수")


class RecognitionCandidate(PlaceCandidate):
    match_type: Literal["EXACT", "SIMILAR"] = Field(..., description="EXACT=확실한 일치(기본 선택) / SIMILAR=비슷한 이름")
    match_reasons: list[str] = Field(default_factory=list, description="주소 일치 · 이름 일치 · 이름 일부만 같아요 · 주소 다름 …")
    tags: list[PlaceTag] = Field(default_factory=list,
                                 description="(확장) 블로그 언급이 많은 순 최대 5개. 태그 기능이 꺼졌거나 언급이 없으면 []")
    tag_post_count: Optional[int] = Field(None, description="(확장) 태그를 센 블로그 글 수 (가게 이름이 나온 글). 태그를 못 구했으면 null")


class RecognitionGroup(CamelModel):
    """(확장) 링크에 나온 장소 1곳 = 묶음 1개 (릴스 '맛집 4곳' 대응)."""
    order: int
    extracted_name: str
    extracted_address: Optional[str] = None
    status: Literal["MATCHED", "SIMILAR_ONLY", "NOT_FOUND"]
    best: Optional[RecognitionCandidate] = Field(None, description="matchType=EXACT 1순위. 없으면 null")
    others: list[RecognitionCandidate] = Field(default_factory=list)


class RecognitionResponse(CamelModel):
    status: Literal["CANDIDATES_FOUND", "NEEDS_SEARCH"]
    extracted_query: Optional[str] = Field(None, description="직접 검색 화면에 미리 채울 검색어")
    candidates: list[RecognitionCandidate] = Field(default_factory=list, description="모든 묶음의 best → others 순")
    source_url: Optional[str] = None
    message: Optional[str] = None
    groups: list[RecognitionGroup] = Field(default_factory=list)
    tag_source: Optional[Literal["NAVER_BLOG"]] = Field(
        None, description="(확장) 태그 출처. 태그 기능이 꺼졌거나 블로그 검색이 모두 실패하면 null (인식 결과는 그대로)")


# ------------------------------------------------------------------ 코스 추천
class AnchorIn(CamelModel):
    type: Literal["REGION", "PLACE"]
    region_id: Optional[str] = Field(None, description="REGION: JAMSIL · SUNGSHIN · HANSUNG · HYEHWA · SEOCHON · YEONNAM")
    place_id: Optional[int] = Field(None, gt=0, description="PLACE: 필수 장소의 placeId (anchorPlace 또는 savedPlaces 에 정보 포함)")

    @field_validator("type", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.upper() if isinstance(v, str) else v


class CourseRecommendationRequest(CamelModel):
    anchor: AnchorIn
    place_count: int = Field(..., ge=3, le=5)
    category_sequence: list[CategoryCode] = Field(..., min_length=3, max_length=5)
    required_place_order: Optional[int] = Field(None, ge=1, le=5, description="PLACE 기준일 때 필수 장소 방문 위치")
    anchor_place: Optional[SavedPlaceIn] = Field(None, description="필수 장소 정보. 없으면 savedPlaces 에서 placeId 로 찾음")
    saved_places: list[SavedPlaceIn] = Field(default_factory=list, max_length=MAX_SAVED_PLACES,
                                             description="추천 재료: 본인 개인 장소 + (선택) 관계의 공동 장소")
    exclude_candidate_keys: list[str] = Field(default_factory=list, max_length=200,
                                              description="(확장) '다시 추천받기': 앞에서 받은 candidateKey 들")


class CoursePlaceOut(CamelModel):
    visit_order: int
    name: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    category_code: Optional[CategoryCode] = None
    category_label: Optional[str] = None
    source: Literal["PERSONAL", "SHARED", "EXTERNAL"]
    place_id: Optional[int] = Field(None, description="저장 장소면 요청에서 받은 placeId, 외부 후보면 null")
    is_anchor: bool = False
    source_url: Optional[str] = None


class WalkingLegOut(CamelModel):
    from_visit_order: int
    to_visit_order: int
    estimated_minutes: int


class RecommendedCourseOut(CamelModel):
    candidate_key: str = Field(..., description="이번 응답에서 코스를 구분하는 값. 영구 courseId 아님")
    title: str
    places: list[CoursePlaceOut]
    walking_legs: list[WalkingLegOut]
    total_walking_minutes: int


class CourseRecommendationResponse(CamelModel):
    courses: list[RecommendedCourseOut]
    reason: Optional[Literal["INSUFFICIENT_PLACES", "UNSUPPORTED_REGION", "NO_MORE_COURSES"]] = None
    walking_time_method: Literal["COORDINATE_ESTIMATE"] = "COORDINATE_ESTIMATE"


# ------------------------------------------------------------------ 기타
class RegionOut(CamelModel):
    region_id: str
    name: str
    latitude: float
    longitude: float
    radius_meters: int


class RegionList(CamelModel):
    items: list[RegionOut]


class CategoryOut(CamelModel):
    code: CategoryCode
    label: str


class CategoryList(CamelModel):
    items: list[CategoryOut]


class Health(CamelModel):
    status: Literal["UP"] = "UP"
    workflow_version: str
    search_provider: str
    extractor: str
    ocr: str
    ocr_ready: bool
    auth_required: bool
    tag_provider: Optional[str] = Field(None, description="(확장) 장소 태그 제공자. 예: naver_blog+openai:gpt-4o-mini / 꺼져 있으면 null")
