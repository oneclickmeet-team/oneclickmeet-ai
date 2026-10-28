"""장소 후보 검색(P02) · 링크·텍스트 인식(P03) · 스크린샷 인식(P04) · (확장) 교체 후보."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from ...v3.save_flow import split_share_text
from ...v3.recommend import replacement_candidates
from ...v3.regions import get_region
from ...v3.save_flow import candidates_from_records
from ...v3.search import ExternalSearchError
from ...v3.walking import walk_minutes
from .. import convert
from ..deps import get_pipeline, get_search, get_settings, get_tagger, ocr_gate
from ..errors import ApiProblem, invalid, provider_unavailable
from ..schemas import (CandidateSearchResponse, RecognitionRequest, RecognitionResponse, ReplacementCandidate,
                       ReplacementRequest, ReplacementResponse, SavedPlaceIn)

router = APIRouter(tags=["장소"])

MAX_EXTRA_IMAGES = 4
# 확장자·Content-Type 이 아니라 파일 앞부분(매직 바이트)으로 형식 판정
_MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"))


def _image_ext(data: bytes) -> Optional[str]:
    for sig, ext in _MAGIC:
        if data.startswith(sig):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


# ------------------------------------------------------------------ P02 장소 후보 검색
@router.get("/place-candidates", response_model=CandidateSearchResponse, summary="P02 장소 후보 검색")
def place_candidates(query: Optional[str] = Query(None, max_length=100, description="키워드"),
                     latitude: Optional[float] = Query(None, ge=-90, le=90),
                     longitude: Optional[float] = Query(None, ge=-180, le=180),
                     region_id: Optional[str] = Query(None, alias="regionId"),
                     radius_meters: int = Query(1000, alias="radiusMeters", ge=1, le=3000),
                     category_code: Optional[str] = Query(None, alias="categoryCode",
                                                          description="(확장) 좌표 검색을 카테고리로 제한"),
                     page: int = Query(0, ge=0),
                     size: int = Query(15, ge=1, le=30),
                     search=Depends(get_search)):
    """키워드 검색이면 `query`, 주변 놀거리·교체 후보면 선택한 장소의 `latitude`·`longitude`.
    둘 다 보내면 그 좌표 주변에서 키워드 검색. `regionId`만 있고 좌표가 없으면 지역 중심 좌표를 씀.
    사용자 실시간 위치는 받지 않는다."""
    q = (query or "").strip() or None
    if (latitude is None) != (longitude is None):
        raise invalid("latitude 와 longitude 는 함께 보내야 합니다.", "latitude")
    center = (latitude, longitude) if latitude is not None else None
    if center is None and region_id:
        r = get_region(region_id)
        if r is None:
            raise invalid("알 수 없는 regionId 입니다.", "regionId")
        center = (r.lat, r.lng)
    if center is None and not q:
        raise invalid("query 또는 latitude·longitude 가 필요합니다.", "query")
    if category_code and category_code not in ("MEAL", "CAFE", "BAR", "ACTIVITY", "CULTURE", "WALK", "SHOPPING"):
        raise invalid("알 수 없는 categoryCode 입니다.", "categoryCode")
    try:
        if center is not None:
            places = search.nearby(center[0], center[1], radius_meters, code=category_code, query=q, limit=90)
        else:
            places = search.search(q, 15)
    except ExternalSearchError:
        raise provider_unavailable()
    start, end = page * size, page * size + size
    items = [convert.candidate(p, center) for p in places[start:end]]
    return CandidateSearchResponse(items=items, page=page, size=size, has_next=len(places) > end)


# ------------------------------------------------------------------ P03 링크·텍스트 인식
def _finish(out, kind: str, source_url: Optional[str], saved: list[SavedPlaceIn], search,
            tagger=None) -> RecognitionResponse:
    res = candidates_from_records(out.records, search, kind, pipeline_message=out.message)
    if res.status == "search_failed":
        raise provider_unavailable()
    tags, source = None, None
    if tagger is not None:  # (확장) 블로그 태그. 실패해도 인식 결과는 그대로 돌려줌
        tags, ok = tagger.tag_many(convert.recognition_places(res))
        source = "NAVER_BLOG" if ok else None
    return convert.recognition(res, source_url, saved, tags, source)


@router.post("/place-recognitions", response_model=RecognitionResponse, summary="P03 링크·텍스트 인식")
def place_recognitions(body: RecognitionRequest, search=Depends(get_search), tagger=Depends(get_tagger)):
    """링크·제목·공유 텍스트에서 검색어를 얻어 실제 장소 후보를 찾는다.
    링크는 robots.txt 를 지키며 **메타 정보(og:title·og:description)만** 읽고, 본문·가격·메뉴는 수집하지 않는다.
    인식 불일치는 200 + NEEDS_SEARCH, 외부 검색 장애는 503. 저장하지 않는다.
    (확장) 후보마다 네이버 블로그 제목·요약에서 센 리뷰 키워드 `tags` 를 붙인다 (블로그 검색 실패는 태그만 빈 목록)."""
    url_from_text, rest = split_share_text(body.shared_text or "")
    url = (body.source_url or "").strip() or url_from_text
    if url and not url.lower().startswith(("http://", "https://")):
        raise invalid("sourceUrl 은 http(s) 주소여야 합니다.", "sourceUrl")
    text = rest if url_from_text and not body.source_url else (body.shared_text or "").strip() or None
    if not url and not text:
        raise invalid("sourceUrl 또는 sharedText 중 하나 이상이 필요합니다.", "sourceUrl")
    pipe = get_pipeline()
    out = pipe.run(url, text) if url else pipe.run(text)
    return _finish(out, "link" if url else "text", url, body.saved_places, search, tagger)


# ------------------------------------------------------------------ P04 스크린샷 인식
@router.post("/place-image-recognitions", response_model=RecognitionResponse, summary="P04 스크린샷 인식")
def place_image_recognitions(image: UploadFile = File(..., description="스크린샷 (JPEG·PNG·WebP)"),
                             extra_images: list[UploadFile] = File(default_factory=list, alias="extraImages",
                                                                   description="(확장) 같은 장소의 추가 화면 최대 4장"),
                             shared_text: Optional[str] = Form(None, alias="sharedText",
                                                               description="(확장) 함께 붙여넣은 캡션"),
                             search=Depends(get_search), tagger=Depends(get_tagger)):
    """multipart/form-data. 이미지는 장당 최대 5MiB(AI_MAX_IMAGE_MB). 업로드 파일은 처리 후 바로 지운다."""
    files = [image] + list(extra_images or [])
    if len(files) > 1 + MAX_EXTRA_IMAGES:
        raise invalid(f"추가 이미지는 최대 {MAX_EXTRA_IMAGES}장입니다.", "extraImages")
    limit = get_settings().max_image_bytes
    paths: list[str] = []
    try:
        for f in files:
            data = f.file.read(limit + 1)
            if not data:
                raise invalid("빈 이미지 파일입니다.", "image")
            if len(data) > limit:
                raise ApiProblem(413, "PLACE-0413", "IMAGE_TOO_LARGE",
                                 f"이미지는 장당 {limit // (1024 * 1024)}MiB 까지 올릴 수 있습니다.")
            ext = _image_ext(data)
            if ext is None:
                raise ApiProblem(415, "PLACE-0415", "UNSUPPORTED_IMAGE_TYPE", "JPEG·PNG·WebP 이미지만 지원합니다.")
            fd, tmp = tempfile.mkstemp(suffix=ext, prefix="ai_upload_")
            with os.fdopen(fd, "wb") as w:
                w.write(data)
            paths.append(tmp)
        pipe = get_pipeline()
        with ocr_gate():
            out = pipe.run(paths[0], (shared_text or "").strip() or None, more_images=paths[1:] or None)
    finally:
        for p in paths:
            Path(p).unlink(missing_ok=True)
    if not out.records and out.status.value == "failed" and "OCR" in (out.message or ""):
        # 서버에 OCR(tesseract+kor)·비전 LLM 이 없음 → 사용자 탓이 아닌 의존 서비스 문제
        raise ApiProblem(503, "EXTERNAL-0503", "RECOGNIZER_UNAVAILABLE", "이미지 인식 기능을 사용할 수 없습니다.")
    return _finish(out, "screenshot", None, [], search, tagger)


# ------------------------------------------------------------------ (확장) 교체 후보
@router.post("/place-candidates/replacements", response_model=ReplacementResponse, summary="(확장) 교체 후보")
def replacements(body: ReplacementRequest, search=Depends(get_search)):
    """코스 편집 '교체': 앞·뒤 장소 **양쪽**에서 추정 도보 10분 이내이고 코스에 없는 장소. 저장 장소 먼저."""
    if body.replace_visit_order > len(body.course_places):
        raise invalid("replaceVisitOrder 가 코스 장소 수를 넘습니다.", "replaceVisitOrder")
    idx = body.replace_visit_order - 1
    course = [convert.saved_to_place(p, search) for p in body.course_places]
    pool, by_key = convert.saved_pool(body.saved_places, search)
    try:
        cands = replacement_candidates(course, idx, pool, search, body.size, code=body.category_code)
    except ExternalSearchError:
        raise provider_unavailable()
    prev_p = course[idx - 1] if idx > 0 else None
    next_p = course[idx + 1] if idx + 1 < len(course) else None
    items = []
    for c in cands:
        s = by_key.get(c.place.place_id)
        items.append(convert.candidate(
            c.place, course[idx].pos, cls=ReplacementCandidate,
            place_source={"mine": "PERSONAL", "couple": "SHARED"}.get(c.source, "EXTERNAL"),
            place_id=s.place_id if s else None,
            walk_from_prev_minutes=walk_minutes(prev_p.pos, c.place.pos) if prev_p else None,
            walk_to_next_minutes=walk_minutes(c.place.pos, next_p.pos) if next_p else None))
    return ReplacementResponse(items=items)
