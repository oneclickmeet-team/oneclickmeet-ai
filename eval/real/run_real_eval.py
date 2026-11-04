"""실제 캡쳐·링크 평가: OCR, OCR+비전 LLM 폴백, 비전 LLM 단독을 같은 정답으로 비교합니다.

  python -m eval.real.run_real_eval --dry-run                      # 호출 수만 계산
  python -m eval.real.run_real_eval --modes ocr                    # LLM 호출 없음
  python -m eval.real.run_real_eval --provider openai --model gpt-4o-mini --max-llm-calls 30

데이터는 data/real_eval/ (커밋 안 됨): cases.jsonl, images/, report.md, results.json
llm·ocr_llm 방식은 캡쳐를 외부 LLM 으로 보내고 요금이 나옵니다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.real.scoring import score_case, summarize  # noqa: E402
from placeflow.cli import build_pipeline  # noqa: E402
from placeflow.envfile import load_env  # noqa: E402
from placeflow.v3.save_flow import candidates_from_records  # noqa: E402
from placeflow.v3.search import get_search_provider  # noqa: E402

MODES = ("ocr", "ocr_llm", "llm")
PRICE_PER_1M = {"gpt-4o-mini": (0.15, 0.60)}  # (입력, 출력) USD. 2026-11-04 OpenAI 문서 기준, 캐시 할인 제외


class LLMBudgetExceeded(Exception):
    pass


class CountingLLM:
    """LLM 제공자를 감싸 호출 수와 (OpenAI 면) 토큰 수를 세고, 상한을 넘기면 호출하지 않습니다."""

    def __init__(self, provider, meter: dict):
        self.provider, self.meter = provider, meter
        self.name = provider.name
        self.model = getattr(provider, "model", "")
        self.supports_vision = provider.supports_vision
        completions = getattr(getattr(getattr(provider, "client", None), "chat", None), "completions", None)
        if completions is not None:
            create = completions.create

            def counted(*args, **kwargs):
                resp = create(*args, **kwargs)
                usage = getattr(resp, "usage", None)
                if usage:
                    meter["input_tokens"] += usage.prompt_tokens or 0
                    meter["output_tokens"] += usage.completion_tokens or 0
                return resp

            completions.create = counted

    def extract(self, system, user_text, images, schema):
        if self.meter["calls"] >= self.meter["limit"]:
            raise LLMBudgetExceeded(f"LLM 호출 상한 {self.meter['limit']}회 도달")
        self.meter["calls"] += 1
        return self.provider.extract(system, user_text, images, schema)


def new_meter(limit: int) -> dict:
    return {"limit": limit, "calls": 0, "input_tokens": 0, "output_tokens": 0}


def build_pipelines(modes, provider: str, model: str, meter: dict) -> dict:
    pipes = {}
    if "ocr" in modes:
        pipes["ocr"] = build_pipeline("heuristic", None, "none", "tesseract")
    if "ocr_llm" in modes:
        pipes["ocr_llm"] = build_pipeline("heuristic", None, "none", "tesseract",
                                          vision_fallback=provider, vision_model=model)
        pipes["ocr_llm"].vision_fallback = CountingLLM(pipes["ocr_llm"].vision_fallback, meter)
    if "llm" in modes:
        pipes["llm"] = build_pipeline(provider, model, "none", "none")
        pipes["llm"].llm = CountingLLM(pipes["llm"].llm, meter)
    return pipes


def load_cases(data_dir: Path) -> list[dict]:
    path = data_dir / "cases.jsonl"
    if not path.is_file():
        raise SystemExit(f"{path} 가 없습니다. eval/real/cases.example.jsonl 을 참고해 만드세요.")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise SystemExit("cases.jsonl 의 id 가 중복됩니다.")
    for c in cases:
        if c["kind"] == "image" and not (data_dir / c["input"]).is_file():
            raise SystemExit(f"{c['id']}: 이미지를 찾을 수 없습니다: {c['input']}")
    return cases


def max_llm_calls(cases: list[dict], modes) -> int:
    images = sum(1 for c in cases if c["kind"] == "image")
    return images * ("ocr_llm" in modes) + len(cases) * ("llm" in modes)


def run_one(case: dict, pipe, search, data_dir: Path) -> dict:
    value = str(data_dir / case["input"]) if case["kind"] == "image" else case["input"]
    started = time.perf_counter()
    try:
        out = pipe.run(value, case.get("note"))
        status, records, trace, message = out.status.value, out.records, out.trace, out.message
    except LLMBudgetExceeded:
        raise
    except Exception as e:  # 한 건이 실패해도 나머지는 계속
        status, records, trace, message = "failed", [], [], f"{type(e).__name__}: {e}"
    budget_hit = any("LLM 호출 상한" in str(t.get("error")) for t in trace)
    if budget_hit:
        raise LLMBudgetExceeded("LLM 호출 상한 도달")

    extracted = [{"name": r.place.name, "address": r.place.address.road or r.place.address.jibun} for r in records]
    linked, search_status = [], None
    if search is not None:
        resolved = candidates_from_records(records, search, "screenshot" if case["kind"] == "image" else "link")
        search_status = resolved.status
        linked = [{"name": g.best.place.name, "address": g.best.place.road_address or g.best.place.jibun_address}
                  for g in resolved.groups if g.best]
    seconds = time.perf_counter() - started
    return {
        "id": case["id"], "segment": case.get("segment", "기타"), "status": status, "message": message,
        "seconds": round(seconds, 1), "extractor": records[0].extractor if records else None,
        "used_vision": any(t["step"] == "vision_fallback" for t in trace),
        "errors": [str(t.get("error"))[:200] for t in trace if t["step"] in ("extract_error", "ocr_error")],
        "extracted": extracted, "linked": linked, "search_status": search_status,
        "score": score_case(case.get("expected", []), extracted, linked),
    }


def run_cases(cases: list[dict], pipes: dict, search, data_dir: Path) -> dict:
    """{방식: [케이스 결과]}. 상한에 닿으면 그 방식의 남은 케이스를 skipped 로 표시합니다."""
    results: dict[str, list[dict]] = {}
    for mode, pipe in pipes.items():
        rows, stopped = [], False
        for case in cases:
            segment = case.get("segment", "기타")
            if stopped:
                rows.append({"id": case["id"], "segment": segment, "skipped": True})
                continue
            if mode == "ocr_llm" and case["kind"] != "image" and "ocr" in results:
                # 폴백은 캡쳐에만 적용되므로 링크·텍스트는 ocr 결과와 같음
                rows.append(next(r for r in results["ocr"] if r["id"] == case["id"]))
                continue
            try:
                rows.append(run_one(case, pipe, search, data_dir))
            except LLMBudgetExceeded:
                stopped = True
                rows.append({"id": case["id"], "segment": segment, "skipped": True})
        results[mode] = rows
    return results


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def render_report(cases: list[dict], results: dict, meter: dict, model: str | None) -> str:
    unverified = sum(1 for c in cases if not c.get("verified"))
    lines = ["# 실제 데이터 평가", ""]
    if unverified:
        lines += [f"> ⚠️ 사람이 확인하지 않은 정답 초안 {unverified}/{len(cases)}건 포함. 기준 수치로 쓰지 마세요.", ""]
    lines += ["| 방식 | 케이스 | 이름 재현율 | 이름 정밀도 | 주소 정확도 | 후보 연결 | 잘못된 후보 | 장소 없음 오탐 | 실패 | 평균초 | 최대초 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for mode, rows in results.items():
        s = summarize(rows)
        lines.append(f"| {mode} | {s['cases']}" + (f" (건너뜀 {s['skipped']})" if s["skipped"] else "")
                     + f" | {_pct(s['name_recall'])} | {_pct(s['name_precision'])} | {_pct(s['address_accuracy'])}"
                     f" | {_pct(s['link_recall'])} | {s['wrong_links']}"
                     f" | {s['negative_false_positives']}/{s['negative_cases']} | {s['failures']}"
                     f" | {s['seconds_avg']} | {s['seconds_max']} |")
    lines += ["", "## 유형별 이름 재현율 / 후보 연결", "", "| 유형 | " + " | ".join(results) + " |",
              "|---|" + "---|" * len(results)]
    for segment in dict.fromkeys(c.get("segment", "기타") for c in cases):
        cells = []
        for rows in results.values():
            s = summarize([r for r in rows if r["segment"] == segment])
            cells.append(f"{_pct(s['name_recall'])} / {_pct(s['link_recall'])}")
        lines.append(f"| {segment} | " + " | ".join(cells) + " |")
    lines += ["", "## LLM 사용량", "", f"- 호출 {meter['calls']}회, 입력 {meter['input_tokens']:,} · 출력 {meter['output_tokens']:,} 토큰"]
    price = PRICE_PER_1M.get(model or "")
    if price and meter["input_tokens"]:
        cost = (meter["input_tokens"] * price[0] + meter["output_tokens"] * price[1]) / 1_000_000
        lines.append(f"- 예상 비용 ${cost:.4f} ({model} 목록가 기준. 실제 청구액은 OpenAI 대시보드 확인)")
    lines += ["", "## 케이스별", ""]
    for mode, rows in results.items():
        lines += [f"### {mode}", ""]
        for r in rows:
            if r.get("skipped"):
                lines.append(f"- {r['id']}: 건너뜀")
                continue
            sc = r["score"]
            lines.append(f"- {r['id']}: 이름 {sc['name_hits']}/{sc['expected']}, 후보 {sc['link_hits']}/{sc['expected']}, {r['seconds']}초"
                         + (", 비전 사용" if r["used_vision"] else "")
                         + (f", 누락 {sc['missed_names']}" if sc["missed_names"] else "")
                         + (f", 정답 외 {sc['extra_names']}" if sc["extra_names"] else "")
                         + (f", 잘못된 후보 {sc['wrong_links']}" if sc["wrong_links"] else "")
                         + (f", 오류 {r['errors']}" if r["errors"] else ""))
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "data" / "real_eval"))
    ap.add_argument("--modes", default=",".join(MODES), help="쉼표 구분: ocr,ocr_llm,llm")
    ap.add_argument("--provider", default="openai", help="openai | anthropic | gemini")
    ap.add_argument("--model")
    ap.add_argument("--max-llm-calls", type=int, default=30, help="넘으면 남은 케이스를 건너뜀")
    ap.add_argument("--no-search", action="store_true", help="카카오 후보 찾기 생략 (후보 연결 지표 없음)")
    ap.add_argument("--dry-run", action="store_true", help="외부 호출 없이 케이스 수와 최대 LLM 호출 수만 출력")
    a = ap.parse_args(argv)

    load_env()
    data_dir = Path(a.data)
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    if unknown := set(modes) - set(MODES):
        raise SystemExit(f"알 수 없는 방식: {sorted(unknown)}")
    cases = load_cases(data_dir)
    planned = max_llm_calls(cases, modes)
    print(f"케이스 {len(cases)}건 · 방식 {modes} · 최대 LLM 호출 {planned}회 (상한 {a.max_llm_calls})")
    if a.dry_run:
        return 0

    model = a.model or os.environ.get("AI_VISION_FALLBACK_MODEL") or os.environ.get("PLACEFLOW_LLM_MODEL")
    meter = new_meter(a.max_llm_calls)
    pipes = build_pipelines(modes, a.provider, model, meter)
    search = None if a.no_search else get_search_provider()
    results = run_cases(cases, pipes, search, data_dir)

    (data_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    report = render_report(cases, results, meter, model)
    (data_dir / "report.md").write_text(report, encoding="utf-8")
    print(report.split("## 케이스별")[0])
    print(f"report → {data_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
