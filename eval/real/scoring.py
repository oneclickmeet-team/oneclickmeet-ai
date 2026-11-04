"""실제 데이터 평가의 채점 규칙 (외부 호출 없음)."""
from __future__ import annotations

import re

_ROAD = re.compile(r"([가-힣A-Za-z0-9]+(?:로|길)(?:\s*\d+(?:번)?길)?)\s*(\d+(?:-\d+)?)")
_BRANCH_TAIL = re.compile(r"^[가-힣A-Za-z0-9]{0,10}점$")
_NOT_BRANCH = ("면세점", "백화점", "편의점", "서점", "상점", "매점")


def norm_name(s: str | None) -> str:
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def same_name(expected: str, got: str | None, aliases: list[str] | None = None) -> bool:
    """공백·기호를 뺀 이름이 같으면 일치. '○○ 혜화점' 처럼 지점명만 더 붙은 경우도 일치."""
    g = norm_name(got)
    for e in [expected, *(aliases or [])]:
        n = norm_name(e)
        tail = g[len(n):] if g.startswith(n) else None
        if n and (g == n or (tail and _BRANCH_TAIL.match(tail) and not tail.endswith(_NOT_BRANCH))):
            return True
    return False


def road_key(address: str | None) -> str | None:
    """도로명 + 건물번호. '서울 종로구 대학로11길 9-5' → '대학로11길9-5'. 없으면 None."""
    matches = _ROAD.findall(address or "")
    if not matches:
        return None
    road, number = matches[-1]
    return re.sub(r"\s+", "", road) + number


def score_case(expected: list[dict], extracted: list[dict], linked: list[dict]) -> dict:
    """한 케이스 채점.

    expected   정답 [{name, address, area, aliases}]. 빈 목록이면 장소가 없어야 하는 케이스
    extracted  추출 결과 [{name, address}]
    linked     기본 선택될 후보 (묶음의 best) [{name, address}]
    """
    left = list(extracted)
    name_hits = address_hits = address_total = 0
    misses = []
    for exp in expected:
        hit = next((x for x in left if same_name(exp["name"], x.get("name"), exp.get("aliases"))), None)
        if hit is None:
            misses.append(exp["name"])
            continue
        left.remove(hit)
        name_hits += 1
        want = road_key(exp.get("address"))
        if want:
            address_total += 1
            address_hits += road_key(hit.get("address")) == want

    def links_to(exp: dict, place: dict) -> bool:
        """주소가 있으면 주소로, 없으면 이름 + (적어 둔 경우) 지역으로 판정."""
        want = road_key(exp.get("address"))
        if want:
            return road_key(place.get("address")) == want
        if not same_name(exp["name"], place.get("name"), exp.get("aliases")):
            return False
        return not exp.get("area") or exp["area"] in (place.get("address") or "")

    wrong = [p for p in linked if not any(links_to(exp, p) for exp in expected)]
    remaining = [p for p in linked if p not in wrong]
    link_hits = 0
    for exp in expected:
        hit = next((p for p in remaining if links_to(exp, p)), None)
        if hit is not None:
            remaining.remove(hit)
            link_hits += 1
    return {
        "expected": len(expected), "extracted": len(extracted), "name_hits": name_hits,
        "extra_names": [x.get("name") for x in left], "missed_names": misses,
        "address_hits": address_hits, "address_total": address_total,
        "link_hits": link_hits, "wrong_links": [p.get("name") for p in wrong],
    }


def summarize(rows: list[dict]) -> dict:
    """같은 방식의 케이스 결과를 합산. 비율은 분모가 0이면 None."""
    def ratio(a: int, b: int):
        return round(a / b, 3) if b else None

    ran = [r for r in rows if not r.get("skipped")]
    scores = [r["score"] for r in ran]
    expected = sum(s["expected"] for s in scores)
    extracted = sum(s["extracted"] for s in scores)
    hits = sum(s["name_hits"] for s in scores)
    negatives = [r for r in ran if r["score"]["expected"] == 0]
    seconds = sorted(r["seconds"] for r in ran)
    return {
        "cases": len(ran), "skipped": len(rows) - len(ran),
        "name_recall": ratio(hits, expected), "name_precision": ratio(hits, extracted),
        "address_accuracy": ratio(sum(s["address_hits"] for s in scores), sum(s["address_total"] for s in scores)),
        "link_recall": ratio(sum(s["link_hits"] for s in scores), expected),
        "wrong_links": sum(len(s["wrong_links"]) for s in scores),
        "negative_cases": len(negatives),
        "negative_false_positives": sum(1 for r in negatives if r["score"]["extracted"]),
        "failures": sum(1 for r in ran if r["status"] == "failed" or r["errors"]),
        "seconds_avg": round(sum(seconds) / len(seconds), 1) if seconds else None,
        "seconds_max": round(seconds[-1], 1) if seconds else None,
    }
