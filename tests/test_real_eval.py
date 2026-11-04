import json

import pytest

from eval.real.run_real_eval import (CountingLLM, LLMBudgetExceeded, load_cases, max_llm_calls, new_meter,
                                     render_report, run_cases)
from eval.real.scoring import road_key, same_name, score_case, summarize
from placeflow import Pipeline
from placeflow.v3.search import DemoSearch


@pytest.mark.parametrize("expected,got,ok", [
    ("투파인드피터", "투파인드피터 혜화점", True),
    ("비스트로 주라", "비스트로주라", True),
    ("가우동", "가우뚱", False),
    ("동화", "동화면세점", False),
    ("로그", "로그인카페", False),
])
def test_same_name(expected, got, ok):
    assert same_name(expected, got) is ok


def test_same_name_alias():
    assert same_name("칠린", "chillin", ["Chillin"])


@pytest.mark.parametrize("address,key", [
    ("서울 종로구 대학로11길 9-5", "대학로11길9-5"),
    ("종로구 대학로11길 9-5", "대학로11길9-5"),
    ("서울 종로구 대학로 133-5", "대학로133-5"),
    ("서울 중구 퇴계로 210-14 2층", "퇴계로210-14"),
    ("서울 종로구", None),
    (None, None),
])
def test_road_key(address, key):
    assert road_key(address) == key


def test_score_finds_wrong_link_with_right_name():
    expected = [{"name": "단골손님", "address": "서울 종로구 대학로12길 10"}]
    extracted = [{"name": "단골손님", "address": None}]
    linked = [{"name": "단골손님", "address": "전남 목포시 옥암로46번길 25"}]
    s = score_case(expected, extracted, linked)
    assert (s["name_hits"], s["link_hits"], s["wrong_links"]) == (1, 0, ["단골손님"])


def test_score_counts_link_found_by_address_despite_misread_name():
    expected = [{"name": "가우동", "address": "서울 종로구 대학로9길 25"}]
    s = score_case(expected, [{"name": "가우뚱", "address": "대학로9길 25"}],
                   [{"name": "가우동", "address": "서울 종로구 대학로9길 25"}])
    assert s["name_hits"] == 0 and s["missed_names"] == ["가우동"] and s["link_hits"] == 1 and not s["wrong_links"]


def test_score_negative_case():
    s = score_case([], [{"name": "혜화맛집", "address": None}], [{"name": "혜화맛집", "address": "서울"}])
    assert s["extra_names"] == ["혜화맛집"] and s["wrong_links"] == ["혜화맛집"]


def test_same_address_expected_twice_needs_two_links():
    expected = [{"name": "혜화도담", "address": "서울 종로구 대명1길 16-6"},
                {"name": "동화", "address": "서울 종로구 대명1길 16-6"}]
    linked = [{"name": "혜화도담", "address": "서울 종로구 대명1길 16-6"}]
    assert score_case(expected, [], linked)["link_hits"] == 1


def test_area_catches_same_name_in_wrong_region():
    expected = [{"name": "별미곱창", "address": None, "area": "송파구"}]
    right = {"name": "별미곱창", "address": "서울 송파구 오금로 1"}
    wrong = {"name": "별미곱창", "address": "부산 해운대구 해운대로 1"}
    assert score_case(expected, [], [right])["link_hits"] == 1
    s = score_case(expected, [], [wrong])
    assert s["link_hits"] == 0 and s["wrong_links"] == ["별미곱창"]


def _row(score, **kw):
    return {"segment": "a", "status": "complete", "errors": [], "seconds": 1.0, "score": score, **kw}


def test_summarize_empty_denominators_and_skips():
    s = summarize([{"id": "x", "segment": "a", "skipped": True}])
    assert s["cases"] == 0 and s["skipped"] == 1 and s["name_recall"] is None and s["seconds_avg"] is None


def test_summarize_counts_negative_false_positive():
    s = summarize([_row(score_case([], [{"name": "x"}], [])),
                   _row(score_case([{"name": "a"}], [{"name": "a"}], []))])
    assert s["negative_cases"] == 1 and s["negative_false_positives"] == 1 and s["name_recall"] == 1.0


class FakeLLM:
    name, supports_vision, model = "fake", True, "v0"

    def __init__(self):
        self.calls = 0

    def extract(self, system, user_text, images, schema):
        self.calls += 1
        return {"places": [{"name": "파스타마켓", "address_hint": "서울 종로구 대학로11길 9-5", "confidence": 0.9}],
                "is_place_content": True}


class EmptyOCR:
    name = "empty"

    def read(self, path):
        return ""


def _cases(tmp_path, n=2):
    (tmp_path / "images").mkdir()
    lines = []
    for i in range(n):
        (tmp_path / "images" / f"{i}.png").write_bytes(b"\x89PNG fake")
        lines.append(json.dumps({"id": f"c{i}", "kind": "image", "input": f"images/{i}.png", "segment": "seg",
                                 "expected": [{"name": "파스타마켓", "address": "서울 종로구 대학로11길 9-5"}]}))
    (tmp_path / "cases.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return load_cases(tmp_path)


def test_run_cases_uses_fallback_and_stops_at_budget(tmp_path):
    cases = _cases(tmp_path, 2)
    meter = new_meter(1)
    fake = FakeLLM()
    pipes = {"ocr": Pipeline(ocr=EmptyOCR()),
             "ocr_llm": Pipeline(ocr=EmptyOCR(), vision_fallback=CountingLLM(fake, meter))}
    results = run_cases(cases, pipes, DemoSearch(), tmp_path)

    assert all(r["score"]["name_hits"] == 0 for r in results["ocr"])
    first, second = results["ocr_llm"]
    assert first["score"]["name_hits"] == 1 and first["used_vision"]
    assert second["skipped"] and fake.calls == 1 and meter["calls"] == 1
    report = render_report(cases, results, meter, "gpt-4o-mini")
    assert "정답 초안 2/2건" in report and "건너뜀 1" in report


def test_counting_llm_refuses_over_limit():
    llm = CountingLLM(FakeLLM(), new_meter(0))
    with pytest.raises(LLMBudgetExceeded):
        llm.extract("", "", [], {})


def test_max_llm_calls():
    cases = [{"kind": "image"}, {"kind": "link"}]
    assert max_llm_calls(cases, ["ocr"]) == 0
    assert max_llm_calls(cases, ["ocr", "ocr_llm", "llm"]) == 3


def test_load_cases_rejects_duplicate_and_missing_image(tmp_path):
    (tmp_path / "cases.jsonl").write_text('{"id":"a","kind":"text","input":"x"}\n{"id":"a","kind":"text","input":"y"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="중복"):
        load_cases(tmp_path)
    (tmp_path / "cases.jsonl").write_text('{"id":"a","kind":"image","input":"no.png"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="찾을 수 없"):
        load_cases(tmp_path)
