"""설정과 공유 객체(검색 제공자·추출 파이프라인). 서버당 한 번만 만들고, 테스트에서는 dependency_overrides 로 바꿉니다."""
from __future__ import annotations

import hmac
import os
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from fastapi import Header

from ..v3.domain import DomainError


@dataclass(frozen=True)
class Settings:
    api_key: Optional[str] = None                 # AI_SERVICE_KEY: 설정하면 X-AI-Key 헤더 필수
    search: str = "auto"                          # SEARCH_PROVIDER: kakao | demo | demo_fail (auto: 카카오 키 있으면 kakao)
    llm: str = "heuristic"                        # AI_LLM: heuristic | openai | anthropic | gemini
    llm_model: Optional[str] = None               # AI_LLM_MODEL (없으면 PLACEFLOW_LLM_MODEL)
    ocr: str = "auto"                             # AI_OCR: auto | tesseract | none
    vision_fallback: str = "none"                 # AI_VISION_FALLBACK: none | openai | anthropic | gemini
    vision_model: Optional[str] = None            # AI_VISION_FALLBACK_MODEL (없으면 AI_LLM_MODEL)
    ocr_concurrency: int = 2                      # AI_OCR_CONCURRENCY: 동시에 돌릴 스크린샷 인식 수
    max_image_mb: int = 5                         # AI_MAX_IMAGE_MB: 장당 최대 크기 (TRD 5MiB)
    cors_origins: tuple[str, ...] = field(default_factory=tuple)  # AI_CORS_ORIGINS: 쉼표 구분 (기본 끔)

    @property
    def max_image_bytes(self) -> int:
        return self.max_image_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(api_key=e("AI_SERVICE_KEY") or None,
                   search=e("SEARCH_PROVIDER") or e("AI_SEARCH_PROVIDER") or "auto",
                   llm=e("AI_LLM", "heuristic"),
                   llm_model=e("AI_LLM_MODEL") or e("PLACEFLOW_LLM_MODEL") or None,
                   ocr=e("AI_OCR", "auto"),
                   vision_fallback=e("AI_VISION_FALLBACK", "none"),
                   vision_model=e("AI_VISION_FALLBACK_MODEL") or e("AI_LLM_MODEL") or e("PLACEFLOW_LLM_MODEL") or None,
                   ocr_concurrency=max(1, int(e("AI_OCR_CONCURRENCY", "2"))),
                   max_image_mb=max(1, int(e("AI_MAX_IMAGE_MB", "5"))),
                   cors_origins=tuple(o.strip() for o in e("AI_CORS_ORIGINS", "").split(",") if o.strip()))


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache
def get_search():
    from ..v3.search import get_search_provider

    return get_search_provider(get_settings().search)


@lru_cache
def get_tagger():
    """(확장) 블로그 태그. 네이버 키가 없거나 BLOG_TAGS=off 면 None → 태그 없이 응답."""
    from ..v3.blog_tags import get_tagger as make

    return make()


_pipe_lock = threading.Lock()
_pipe = None


def get_pipeline():
    """링크·스크린샷 추출 파이프라인 (처음 쓸 때 한 번 생성). 설정 오류는 extractor_unavailable."""
    global _pipe
    with _pipe_lock:
        if _pipe is None:
            from ..cli import build_pipeline

            s = get_settings()
            try:
                _pipe = build_pipeline(s.llm, s.llm_model, "none", s.ocr,
                                       vision_fallback=s.vision_fallback, vision_model=s.vision_model)
            except (KeyError, ValueError, ImportError) as e:
                raise DomainError("extractor_unavailable", f"추출기 설정 오류: {e}")
        return _pipe


_ocr_gate: Optional[threading.BoundedSemaphore] = None


def ocr_gate() -> threading.BoundedSemaphore:
    """스크린샷 인식(Tesseract 3방식 동시 실행)이 CPU 를 독점하지 않게 동시 처리 수 제한."""
    global _ocr_gate
    if _ocr_gate is None:
        _ocr_gate = threading.BoundedSemaphore(get_settings().ocr_concurrency)
    return _ocr_gate


def require_key(x_ai_key: Optional[str] = Header(None, alias="X-AI-Key",
                                                  description="AI_SERVICE_KEY 를 설정한 경우 필수 (Spring → FastAPI 내부 인증)")) -> None:
    from .errors import ApiProblem

    key = get_settings().api_key
    if key and not (x_ai_key and hmac.compare_digest(x_ai_key, key)):
        raise ApiProblem(401, "AUTH-0401", "INVALID_SERVICE_KEY", "내부 서비스 키가 없거나 올바르지 않습니다.")
