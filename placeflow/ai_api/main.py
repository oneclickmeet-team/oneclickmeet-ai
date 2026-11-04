"""OneClickMeet AI 내부 서버 (FastAPI). Spring Boot 가 호출하는 상태 없는 서비스 — 사용자 데이터를 저장하지 않는다.

  python -m placeflow api --port 8001      → http://127.0.0.1:8001/docs (Swagger)
  uvicorn placeflow.ai_api.main:app        (배포 시)

TRD 공통 계약: 경로 /internal/v1 · JSON camelCase · 성공 DTO 직접 반환 · 오류 application/problem+json
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from ..v3.domain import DomainError
from ..v3.search import ExternalSearchError
from .deps import get_search, get_settings, get_tagger, require_key
from .errors import DOMAIN_MAP, ApiProblem, log, problem_response, provider_unavailable
from .routers import places, recommend
from .schemas import Health, Problem

WORKFLOW_VERSION = "2.1"
PREFIX = "/internal/v1"
ERROR_RESPONSES = {code: {"model": Problem, "content": {"application/problem+json": {}}}
                   for code in (400, 401, 404, 413, 415, 503)}


def create_app() -> FastAPI:
    from ..envfile import load_env

    load_env()  # 프로젝트 .env (KAKAO_REST_API_KEY · NAVER_CLIENT_ID 등)
    settings = get_settings()
    app = FastAPI(
        title="OneClickMeet AI Internal API",
        version=WORKFLOW_VERSION,
        description="Spring Boot → FastAPI 내부 API. 장소 후보 검색(P02)·링크/텍스트 인식(P03)·스크린샷 인식(P04)·"
                    "코스 추천(R01)·지원 지역(P01). 장소 검색은 카카오 로컬, 장소 태그는 네이버 블로그 검색(제목·요약). 상태 없음 — 저장 장소는 요청에 담아 보낸다. "
                    "JSON camelCase, 오류는 application/problem+json.",
    )
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET", "POST"],
                           allow_headers=["*"])
    auth = [Depends(require_key)]
    app.include_router(places.router, prefix=PREFIX, dependencies=auth, responses=ERROR_RESPONSES)
    app.include_router(recommend.router, prefix=PREFIX, dependencies=auth, responses=ERROR_RESPONSES)

    @app.get("/health", response_model=Health, tags=["상태"])
    def health(tagger=Depends(get_tagger)):
        from ..ocr import tesseract_status

        s = get_settings()
        try:
            search_name = get_search().name
        except ValueError as e:  # 키 없음 등 설정 오류
            search_name = f"error: {e}"
        return Health(workflow_version=WORKFLOW_VERSION, search_provider=search_name, extractor=s.llm, ocr=s.ocr,
                      ocr_ready=tesseract_status()[0], auth_required=bool(s.api_key),
                      tag_provider=tagger.name if tagger else None)

    @app.exception_handler(ApiProblem)
    def _problem(request: Request, e: ApiProblem):
        return problem_response(request, e)

    @app.exception_handler(DomainError)
    def _domain(request: Request, e: DomainError):
        status, code, reason = DOMAIN_MAP.get(e.code, (400, "COMMON-0400", "INVALID_INPUT"))
        return problem_response(request, ApiProblem(status, code, reason, e.message))

    @app.exception_handler(ExternalSearchError)
    def _external(request: Request, e: ExternalSearchError):
        log.warning("external search failed: %s", e)  # 제공자 원문은 로그에만
        return problem_response(request, provider_unavailable())

    @app.exception_handler(RequestValidationError)
    def _invalid(request: Request, e: RequestValidationError):
        errors = []
        for er in e.errors()[:10]:
            loc = [str(x) for x in er.get("loc", ()) if x not in ("body", "query", "path", "form")]
            errors.append({"field": ".".join(loc) or "request",
                           "message": str(er.get("msg", "")).replace("Value error, ", "")})
        return problem_response(request, ApiProblem(400, "COMMON-0400", "INVALID_INPUT", "요청 형식이 올바르지 않습니다.",
                                                    errors))

    @app.exception_handler(Exception)
    def _internal(request: Request, e: Exception):
        log.exception("internal error")
        return problem_response(request, ApiProblem(500, "COMMON-0500", "INTERNAL_ERROR", "서버 오류가 발생했습니다."))

    return app


app = create_app()
