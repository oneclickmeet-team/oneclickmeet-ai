"""명령줄 실행.

  python -m placeflow extract <링크|이미지경로|텍스트> [--note 캡션] [--llm heuristic|openai|anthropic|gemini]
                              [--model 이름] [--geo none|fixture|naver|kakao] [--ocr none|sidecar|tesseract]
                              [--fixtures eval/fixtures/pages] [--store data/places.jsonl]
  python -m placeflow export-schema
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import taxonomy
from .fetchers.http import FixtureClient, UrllibClient
from .geo import get_resolver
from .llm import get_provider
from .models import ExtractionResult, PlaceRecord
from .ocr import get_ocr
from .pipeline import Pipeline
from .store import JsonlStore

ROOT = Path(__file__).resolve().parent.parent


def build_pipeline(llm="heuristic", model=None, geo="none", ocr="none", fixtures=None, geo_fixture=None,
                   link_body: bool = False, vision_fallback=None, vision_model=None) -> Pipeline:
    """link_body: 링크 본문까지 읽기 (TRD 상 기본 False — 평가·예전 화면 전용).
    vision_fallback: OCR 로 추출하지 못한 캡쳐만 다시 읽을 비전 LLM 제공자 (기본 끔)."""
    use_fallback = vision_fallback and vision_fallback.lower() != "none"
    return Pipeline(
        llm=get_provider(llm, model),
        geo=get_resolver(geo, geo_fixture),
        ocr=get_ocr(ocr),
        vision_fallback=get_provider(vision_fallback, vision_model) if use_fallback else None,
        http=FixtureClient(fixtures) if fixtures else UrllibClient(),
        link_body=link_body,
    )


def export_schema(out_dir: Path = ROOT / "schema"):
    out_dir.mkdir(exist_ok=True)
    (out_dir / "place_record.schema.json").write_text(
        json.dumps(PlaceRecord.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "extraction_result.schema.json").write_text(
        json.dumps(ExtractionResult.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "taxonomy.json").write_text(json.dumps(taxonomy.as_dict(), ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    print(f"schema written to {out_dir}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="placeflow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("input")
    ex.add_argument("--note", help="사용자가 함께 붙여넣은 캡션/메모")
    ex.add_argument("--llm", default="heuristic")
    ex.add_argument("--model")
    ex.add_argument("--geo", default="none")
    ex.add_argument("--ocr", default="none")
    ex.add_argument("--vision-fallback", help="OCR 로 추출하지 못한 캡쳐를 다시 읽을 비전 LLM 제공자")
    ex.add_argument("--vision-model")
    ex.add_argument("--fixtures", help="오프라인 HTTP 픽스처 폴더")
    ex.add_argument("--geo-fixture")
    ex.add_argument("--store", help="결과를 upsert 할 JSONL 경로")
    ex.add_argument("--trace", action="store_true")
    sub.add_parser("export-schema")
    sv = sub.add_parser("serve", help="실험용 웹 화면 실행")
    sv.add_argument("--port", type=int, default=8000)
    api = sub.add_parser("api", help="AI 서버(FastAPI) 실행 → /docs")
    api.add_argument("--host", default="127.0.0.1", help="외부에서 접속하려면 0.0.0.0 (AI_SERVICE_KEY 설정 권장)")
    api.add_argument("--port", type=int, default=8001)
    api.add_argument("--reload", action="store_true", help="코드 변경 시 자동 재시작 (개발용)")
    a = ap.parse_args(argv)
    from .envfile import load_env

    load_env()

    if a.cmd == "export-schema":
        export_schema()
        return 0
    if a.cmd == "serve":
        from .server import serve

        serve(a.port)
        return 0
    if a.cmd == "api":
        try:
            import uvicorn
        except ImportError:
            print("FastAPI 서버 패키지가 없습니다 → python3 -m pip install -r requirements-api.txt", file=sys.stderr)
            return 2
        print(f"OneClickMeet AI API: http://{a.host}:{a.port}/docs  (종료: Ctrl+C)")
        uvicorn.run("placeflow.ai_api.main:app", host=a.host, port=a.port, reload=a.reload)
        return 0

    try:
        pipe = build_pipeline(a.llm, a.model, a.geo, a.ocr, a.fixtures, a.geo_fixture,
                              vision_fallback=a.vision_fallback, vision_model=a.vision_model)
    except KeyError as e:
        print(f"설정 오류: 환경변수 {e} 가 필요합니다 (.env.example 참고)", file=sys.stderr)
        return 2
    except (ValueError, ImportError) as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    out = pipe.run(a.input, a.note)
    dump = out.model_dump(mode="json", exclude={"trace"} if not a.trace else None)
    print(json.dumps(dump, ensure_ascii=False, indent=2))
    if a.store and out.records:
        st = JsonlStore(a.store)
        for r in out.records:
            _, created = st.upsert(r)
            print(f"[store] {'new' if created else 'merged'}: {r.place.name} ({r.dedupe_key})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
