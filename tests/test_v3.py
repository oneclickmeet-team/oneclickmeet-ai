"""V3 (Figma V3 기준) 장소 저장 · 도보 코스 추천 · 코스 편집 테스트. 네트워크 없이 DemoSearch 로 실행."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from placeflow.v3.domain import (Anchor, Course, CourseStop, DomainError, Place, add_item, add_stop, remove_stop,
                                 reorder_stops, replace_stop, summarize, update_item, validate_for_save)
from placeflow.v3.recommend import RecommendRequest, candidate_key_of, recommend, replacement_candidates
from placeflow.v3.regions import get_region, region_of
from placeflow.v3.repo import JsonRepo
from placeflow.v3.save_flow import candidates_from_records, search_candidates
from placeflow.v3.search import DemoSearch, ExternalSearchError, make_place_id
from placeflow.v3.service import OneClickMeetService
from placeflow.v3.walking import ANCHOR_RADIUS_M, MAX_LEG_MIN, distance_m, walk_minutes


@pytest.fixture
def demo():
    return DemoSearch()


@pytest.fixture
def svc(tmp_path, demo):
    return OneClickMeetService(JsonRepo(tmp_path / "db.json"), demo, None)


def _places(demo, area="연남", n=5):
    return [p for p in demo.places if p.name.startswith(area + " ")][:n]


def _course(demo, n=4):
    return Course(owner_id="me", stops=[CourseStop(place=p) for p in _places(demo, n=n)])


# ------------------------------------------------------------------ 도보 · 지역
def test_walk_minutes_rule():
    a, b = (37.5626, 126.9255), (37.5626, 126.9255 + 0.0113)  # 약 1km 동쪽
    assert 990 < distance_m(a, b) < 1010
    assert walk_minutes(a, b) == 18  # 1km × 1.3 ÷ 4.5km/h = 17.3 → 올림
    assert walk_minutes(a, a) == 1


def test_region_lookup():
    r = get_region("YEONNAM")
    assert region_of(r.lat, r.lng).region_id == "YEONNAM" and get_region("yeonnam") is r
    assert region_of(35.1, 129.0) is None  # 부산 → 미지원


def test_place_id_stable_and_space_insensitive():
    assert make_place_id("연남 카페", "서울 마포구 1") == make_place_id("연남카페", "서울  마포구 1")
    assert make_place_id("A", "x").startswith("nv_")


# ------------------------------------------------------------------ 코스 항목 규칙
def test_items_follow_reorder_but_not_replace_or_delete(demo):
    c = _course(demo)
    s0, s1 = c.stops[0].stop_id, c.stops[1].stop_id
    c = add_item(c, s0, "파스타 2개", 30000)
    c = add_item(c, s1, "커피", 12000, checked=False)
    c2 = reorder_stops(c, [s.stop_id for s in reversed(c.stops)])
    assert len(c2.items) == 2 and c2.stops[-1].stop_id == s0
    c3 = replace_stop(c2, s0, _places(demo, n=6)[5])
    assert [i.stop_id for i in c3.items] == [s1]
    c4 = remove_stop(c3, s1)
    assert c4.items == []


def test_summary_confirmed_total_and_unchecked(demo):
    c = _course(demo)
    s = [x.stop_id for x in c.stops]
    c = add_item(c, s[0], "a", 10000)
    c = add_item(c, s[0], "b", 5000, checked=False)
    c = add_item(c, s[1], "c", 7000, checked=False)
    sm = summarize(c)
    assert sm.confirmed_total == 10000
    assert sm.unchecked_stop_count == 3  # s1(체크 없음) + 항목 없는 2곳
    c = update_item(c, c.items[2].item_id, checked=True)
    assert summarize(c).confirmed_total == 17000


def test_validate_3_to_5_and_duplicates(demo):
    assert "too_few_stops" in validate_for_save(_course(demo, 2))
    assert validate_for_save(_course(demo, 5)) == []
    c = _course(demo, 5)
    with pytest.raises(DomainError) as e:
        add_stop(c, _places(demo, n=6)[5])
    assert e.value.code == "too_many_stops"
    with pytest.raises(DomainError) as e:
        add_stop(_course(demo, 3), c.stops[0].place)
    assert e.value.code == "duplicate_place"


def test_reorder_requires_same_set(demo):
    c = _course(demo)
    with pytest.raises(DomainError):
        reorder_stops(c, [c.stops[0].stop_id])


def test_item_amount_bounds(demo):
    c = _course(demo)
    with pytest.raises(Exception):
        add_item(c, c.stops[0].stop_id, "x", -1)


# ------------------------------------------------------------------ 저장소
def test_repo_version_conflict_and_memo_privacy(tmp_path, demo):
    repo = JsonRepo(tmp_path / "r.json")
    c = repo.save_course(_course(demo))
    assert c.version == 1
    repo.save_course(c, expected_version=1)
    with pytest.raises(DomainError) as e:
        repo.save_course(c, expected_version=1)
    assert e.value.code == "version_conflict"
    repo.set_memo("me", "place", "p1", "비밀")
    assert repo.get_memo("me", "place", "p1").text == "비밀"
    assert repo.get_memo("you", "place", "p1") is None
    repo.set_memo("me", "place", "p1", "")
    assert repo.get_memo("me", "place", "p1") is None


def test_saving_course_does_not_fill_box(svc, demo):
    svc.save_course("me", _course(demo).model_dump(mode="json"))
    assert svc.list_saved("me")["count"] == 0


def test_save_place_idempotent(svc, demo):
    p = demo.places[0].model_dump(mode="json")
    svc.save_place("me", p)
    svc.save_place("me", p)
    assert svc.list_saved("me")["count"] == 1
    assert svc.unsave_place("me", demo.places[0].place_id)["removed"]


# ------------------------------------------------------------------ 추천
def _pool(demo, names):
    return [(p, "mine") for p in demo.places if p.name in names]


def _check_course(c, center, seq, anchor_id=None, anchor_order=None):
    ids = [s.place.place_id for s in c.places]
    assert len(c.places) == len(seq) and 3 <= len(c.places) <= 5
    assert len(set(ids)) == len(ids)
    for k, s in enumerate(c.places):
        assert s.visit_order == k + 1 and s.slot_category == seq[k]
        assert distance_m(center, s.place.pos) <= ANCHOR_RADIUS_M + 1
        if not s.is_anchor:
            assert s.place.category_code == seq[k]          # 자리마다 지정 카테고리
    for a, b in zip(c.places, c.places[1:]):
        assert walk_minutes(a.place.pos, b.place.pos) <= MAX_LEG_MIN
    assert [l.estimated_minutes for l in c.walking_legs] == [s.walk_from_prev_min for s in c.places[1:]]
    if anchor_id:
        assert sum(s.is_anchor for s in c.places) == 1
        assert c.places[anchor_order - 1].place.place_id == anchor_id   # 필수 장소는 지정 위치


def _req(anchor, seq, order=None, exclude=()):
    return RecommendRequest(anchor=anchor, place_count=len(seq), category_sequence=seq, required_place_order=order,
                            exclude_keys=list(exclude))


def test_recommend_place_anchor_fixed_position(demo):
    pool = _pool(demo, {"연남 파스타집", "연남 감성 카페", "연남 와인바"})
    anchor = next(p for p, _ in pool if p.name == "연남 감성 카페")
    seq = ["MEAL", "CAFE", "ACTIVITY", "BAR"]
    res = recommend(_req(Anchor(type="place", place_id=anchor.place_id), seq, 2), pool, demo, anchor)
    assert res.reason is None and 1 <= len(res.courses) <= 3
    assert len({c.candidate_key for c in res.courses}) == len(res.courses)
    for c in res.courses:
        _check_course(c, anchor.pos, seq, anchor.place_id, 2)
        assert c.title == "연남 맛집·카페·놀거리·술집 코스"
    assert res.walking_time_method == "COORDINATE_ESTIMATE"
    # 저장 장소 우선: 첫 코스는 저장한 파스타집·와인바를 씀
    assert res.courses[0].saved_count >= 3
    # 다시 추천: 이전 코스 제외
    again = recommend(_req(Anchor(type="place", place_id=anchor.place_id), seq, 2,
                           [c.candidate_key for c in res.courses]), pool, demo, anchor)
    assert not {c.candidate_key for c in again.courses} & {c.candidate_key for c in res.courses}
    assert again.courses or again.reason == "NO_MORE_COURSES"


def test_anchor_category_mismatch_kept(demo):
    """필수 장소(카페)를 MEAL 자리에 둬도 그대로 넣음 (경고 없음)."""
    pool = _pool(demo, {"연남 감성 카페"})
    anchor = pool[0][0]
    seq = ["MEAL", "ACTIVITY", "CAFE"]
    res = recommend(_req(Anchor(type="place", place_id=anchor.place_id), seq, 1), pool, demo, anchor)
    assert res.courses and all(c.places[0].place.place_id == anchor.place_id for c in res.courses)


def test_recommend_region_anchor(demo):
    r = get_region("JAMSIL")
    seq = ["WALK", "CAFE", "MEAL"]
    res = recommend(_req(Anchor(type="region", region_id="JAMSIL"), seq), [], demo)
    assert res.reason is None and res.courses
    for c in res.courses:
        _check_course(c, (r.lat, r.lng), seq)
        assert all(s.source == "external" for s in c.places)


def test_recommend_failures(demo):
    far = demo.places[0].model_copy(update={"lat": 35.1, "lng": 129.0, "place_id": "far"})
    res = recommend(_req(Anchor(type="place", place_id="far"), ["MEAL", "CAFE", "BAR"], 1), [(far, "mine")], demo, far)
    assert res.reason == "UNSUPPORTED_REGION" and res.courses == []
    with pytest.raises(ExternalSearchError):
        recommend(_req(Anchor(type="region", region_id="SEOCHON"), ["MEAL", "CAFE", "BAR"]), [], DemoSearch(fail=True))
    with pytest.raises(DomainError):
        recommend(_req(Anchor(type="place", place_id="nope"), ["MEAL", "CAFE", "BAR"], 1), [], demo, None)
    res = recommend(_req(Anchor(type="region", region_id="JAMSIL"), ["SHOPPING"] * 5), [], demo)
    assert res.reason == "INSUFFICIENT_PLACES" and res.courses == []


def test_request_validation():
    a = Anchor(type="region", region_id="JAMSIL")
    with pytest.raises(Exception):
        RecommendRequest(anchor=a, place_count=4, category_sequence=["MEAL"] * 3)      # 개수 불일치
    with pytest.raises(Exception):
        RecommendRequest(anchor=a, place_count=3, category_sequence=["MEAL"] * 3, required_place_order=1)
    p = Anchor(type="place", place_id="x")
    with pytest.raises(Exception):
        RecommendRequest(anchor=p, place_count=3, category_sequence=["MEAL"] * 3)      # 위치 누락
    with pytest.raises(Exception):
        RecommendRequest(anchor=p, place_count=3, category_sequence=["MEAL"] * 3, required_place_order=4)
    with pytest.raises(Exception):
        RecommendRequest(anchor=a, place_count=3, category_sequence=["MEAL", "CAFE", "PARTY"])


def test_candidate_key_depends_on_order():
    assert candidate_key_of(["a", "b"]) != candidate_key_of(["b", "a"])
    assert candidate_key_of(["a", "b"]) == candidate_key_of(["a", "b"])


def test_replacement_candidates_walkable(demo):
    ps = _places(demo, n=4)
    cands = replacement_candidates(ps, 1, [(ps[2], "mine")], demo)
    for c in cands:
        assert c.place.place_id not in {p.place_id for p in ps}
        assert walk_minutes(ps[0].pos, c.place.pos) <= MAX_LEG_MIN
        assert walk_minutes(c.place.pos, ps[2].pos) <= MAX_LEG_MIN


# ------------------------------------------------------------------ 저장 흐름
def test_search_candidates_statuses(demo):
    assert search_candidates(demo, "연남 카페").status == "candidates"
    assert search_candidates(demo, "없는장소이름").status == "no_match"
    assert search_candidates(DemoSearch(fail=True), "연남 카페").status == "search_failed"


def test_candidates_from_empty_records(demo):
    assert candidates_from_records([], demo, "screenshot").status == "recognition_failed"
    assert candidates_from_records([], demo, "link").status == "link_unreadable"


# ------------------------------------------------------------------ 서비스
def test_service_flow(svc):
    svc.seed_demo("me", "YEONNAM")
    pid = svc.list_saved("me")["items"][0]["place"]["place_id"]
    rec = svc.recommend("me", {"type": "place", "place_id": pid}, 3, ["MEAL", "CAFE", "WALK"], 1)
    assert rec["reason"] is None and rec["courses"]
    d = svc.draft_from_recommendation("me", rec["courses"][0], {"type": "place", "place_id": pid})
    c = svc.apply_op("me", d["course"], {"type": "add_item", "stop_id": d["course"]["stops"][0]["stop_id"],
                                         "name": "점심", "amount": 20000})
    assert c["summary"]["confirmed_total"] == 20000
    saved = svc.save_course("me", c["course"], memo="m")
    assert saved["course"]["version"] == 1 and saved["my_memo"] == "m"
    with pytest.raises(DomainError):
        svc.save_course("me", saved["course"], expected_version=0)
    with pytest.raises(DomainError):
        svc.get_course("intruder", saved["course"]["course_id"])


# ------------------------------------------------------------------ OCR 보강 (상호명이 깨진 플레이스 캡쳐)
def test_ocr_merge_fixes_garbled_name_and_address():
    from placeflow.ocr import merge_variants, ocr_quality

    bad = "place+\nAPSARA] 대학로점\n주꾸미요리 ㆍ ※4.42 ㆍ리뷰 2,952\n영업 중\n서울 종로구 SSE 68\n02-763-1204"
    good = "place+\n삼형제쭈꾸미 대학로점\n주꾸미요리 ㆍ 4.42 ㆍ 리뷰 2,952\n영업 중\n서울 종로구 동숭길 68\n"
    assert ocr_quality(good) > ocr_quality(bad)
    merged = merge_variants([bad, "주 꾸 미 요 리 ㆍ 리 뷰\nSAFO 대학로점\n서울 종로구 동숭길 68"])
    assert "APSARA" not in merged.splitlines()[1] or "동숭길 68" in merged
    assert "동숭길 68" in merged


def test_garbled_place_capture_resolves_by_address():
    from placeflow.pipeline import Pipeline
    from placeflow.v3.domain import Place

    text = "place+\nSal 770| 대학로점\n주꾸미요리 ㆍ 4.42 ㆍ 리뷰 2,952\n영업 중 ㆍ 22:00에 영업 종료\n서울 종로구 동숭길 68\n02-763-1204 복사"
    out = Pipeline().run(text)
    assert out.records and out.records[0].place.address.road == "서울 종로구 동숭길 68"

    target = Place(place_id="nv_x", name="삼형제쭈꾸미 대학로점", category="restaurant", category_label="맛집",
                   road_address="서울특별시 종로구 동숭길 68", lat=37.5826, lng=127.0023, provider="kakao_local")
    other = Place(place_id="nv_y", name="동숭길 카페", category="cafe", road_address="서울특별시 종로구 동숭길 12",
                  lat=37.58, lng=127.0, provider="kakao_local")

    class Fake:
        name = "fake"
        def search(self, q, display=5):
            return [target, other] if "동숭길" in q else []

    res = candidates_from_records(out.records, Fake(), "screenshot")
    assert res.status == "candidates" and res.candidates[0].place.place_id == "nv_x"
    assert all(c.place.place_id != "nv_y" for c in res.candidates)


def test_share_text_used_when_link_blocked_by_robots():
    from placeflow.fetchers.http import ROBOTS_BLOCKED, HttpResponse
    from placeflow.pipeline import Pipeline
    from placeflow.v3.save_flow import split_share_text

    url, rest = split_share_text("[네이버 지도]\n삼형제쭈꾸미 대학로점\n서울 종로구 동숭길 68\nhttps://naver.me/5abcDEF")
    assert url == "https://naver.me/5abcDEF" and "삼형제쭈꾸미 대학로점" in rest
    assert split_share_text("링크 없음") == (None, None)

    class Blocked:
        def get(self, u):
            return HttpResponse(u, u, ROBOTS_BLOCKED, "")

    pipe = Pipeline(http=Blocked())
    out = pipe.run("https://map.naver.com/p/entry/place/1234567")
    assert not out.records and "robots.txt" in (out.message or "")
    res = candidates_from_records(out.records, DemoSearch(), "link", pipeline_message=out.message)
    assert res.status == "link_unreadable" and "공유" in res.message
    out2 = pipe.run(url, rest)
    assert out2.records and out2.records[0].place.name.startswith("삼형제쭈꾸미")
    assert out2.records[0].place.address.road == "서울 종로구 동숭길 68"


# ------------------------------------------------------------------ 링크 하나에 장소 여러 곳 (인스타 '맛집 4곳' 릴스)
def _jamsil_db():
    from placeflow.v3.domain import Place

    def P(i, name, road, cat="restaurant"):
        return Place(place_id=i, name=name, road_address=road, category=cat, lat=37.51, lng=127.11,
                     provider="kakao_local")
    return [P("a", "오지다", "서울특별시 송파구 백제고분로48길 24"),
            P("b", "회포차오지다", "서울특별시 송파구 가락로 241 1층"),
            P("c", "닭튜브 방이점", "서울특별시 송파구 백제고분로48길 7 1층"),
            P("d", "일일식탁", "서울특별시 송파구 올림픽로35길 124 장미종합A상가 A-2동 지하1층"),
            P("e", "일일식탁 강남점", "서울특별시 강남구 테헤란로 1"),
            P("f", "리사르커피", "서울특별시 송파구 올림픽로35길 112 B동", "cafe")]


class _FakeNaver:
    name = "fake"

    def __init__(self, db):
        self.db, self.calls = db, []

    def search(self, q, display=5):
        self.calls.append(q)
        toks = [x for x in q.split() if not x.endswith(("구", "동"))]
        return [p for p in self.db if any(x.replace(" ", "") in (p.name + p.road_address).replace(" ", "")
                                           for x in toks)][:display]


def test_multi_place_caption_groups():
    from pathlib import Path

    from placeflow.pipeline import Pipeline

    caption = (Path(__file__).parent / "fixtures" / "ig_jamsil_4places.txt").read_text(encoding="utf-8")
    recs = Pipeline().run(caption).records
    assert [r.place.name for r in recs] == ["오지다", "닭튜브", "일일식탁", "리사르커피"]
    cats = {r.place.name: (r.classification.primary_category, r.classification.sub_category) for r in recs}
    assert cats["닭튜브"] == ("restaurant", "chicken") and cats["일일식탁"] == ("restaurant", "korean")

    res = candidates_from_records(recs, _FakeNaver(_jamsil_db()), "link")
    assert res.status == "candidates"
    assert [g.extracted.name for g in res.groups] == ["오지다", "닭튜브", "일일식탁", "리사르커피"]  # 4번째도 검색됨
    assert [g.extracted.order for g in res.groups] == [1, 2, 3, 4]
    best = {g.extracted.name: g.best.place.place_id for g in res.groups}
    assert best == {"오지다": "a", "닭튜브": "c", "일일식탁": "d", "리사르커피": "f"}
    g0 = res.groups[0]
    assert "주소 일치" in g0.best.reasons
    hoe = next(c for c in g0.others if c.place.place_id == "b")  # 회포차오지다: 기본 선택 안 함
    assert hoe.match_type == "similar" and "이름 일부만 같아요" in hoe.reasons and "주소 다름" in hoe.reasons
    gangnam = next(c for c in res.groups[2].others if c.place.place_id == "e")
    assert gangnam.match_type == "similar"
    assert all(c.match_type == "similar" for g in res.groups for c in g.others)


def test_match_rules():
    from placeflow.v3.save_flow import match

    db = {p.place_id: p for p in _jamsil_db()}
    assert match("닭튜브", None, None, [], db["c"])[0] == "exact"            # '○○점' 지점명 허용
    assert match("오지다", None, None, [], db["b"])[0] == "similar"          # 이름 일부만
    assert match("오지다", None, "백제고분로48길24", [], db["b"])[2][-1] == "주소 다름"
    assert match("오지다", None, None, [], db["c"]) is None                  # 무관한 가게
    assert match("일일식탁", None, None, [("장미", "상가")], db["d"])[2] == ["이름 일치", "장미상가 일치"]


def test_similar_only_group_has_no_best():
    from placeflow.pipeline import Pipeline

    recs = Pipeline().run("📍오지다 서울 송파구 가락로 999").records
    res = candidates_from_records(recs, _FakeNaver(_jamsil_db()), "link")
    g = res.groups[0]
    assert g.best is None and g.status == "similar_only" and g.others


def test_batch_save(svc, demo):
    ps = [p.model_dump(mode="json") for p in demo.places[:3]]
    out = svc.save_places("me", ps, origin="link", source_url="https://instagram.com/reel/x")
    assert out["count"] == 3 and svc.list_saved("me")["count"] == 3
    with pytest.raises(DomainError):
        svc.save_places("me", [])


def test_embedded_cuisine_word_ignored():
    from placeflow import taxonomy

    assert taxonomy.classify_text("일일식탁") is None
    assert taxonomy.classify_text("일식당 하루") == ("restaurant", "japanese")


# ------------------------------------------------------------------ 카카오 · 제공자 선택 · 카테고리 추론
_KAKAO_DOCS = {"documents": [
    {"id": "1", "place_name": "오지다", "category_name": "음식점 > 일식 > 초밥,롤", "category_group_code": "FD6",
     "phone": "02-1", "address_name": "서울 송파구 방이동 1", "road_address_name": "서울 송파구 백제고분로48길 24",
     "x": "127.1123", "y": "37.5131", "place_url": "http://place.map.kakao.com/1"},
    {"id": "2", "place_name": "방이 호프", "category_name": "음식점 > 술집 > 호프,요리주점", "category_group_code": "FD6",
     "address_name": "서울 송파구 방이동 2", "road_address_name": "", "x": "127.1130", "y": "37.5135"},
    {"id": "3", "place_name": "레드버튼 잠실점", "category_name": "스포츠,레저 > 보드카페", "category_group_code": "",
     "address_name": "서울 송파구 잠실동 1", "x": "127.1100", "y": "37.5120"}]}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_kakao_provider_parsing_and_nearby(monkeypatch):
    from placeflow.v3.search import KakaoLocalSearch

    calls = []

    def fake(req, timeout=0):
        calls.append((req.full_url, req.headers.get("Authorization")))
        return _Resp(json.dumps(_KAKAO_DOCS).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    k = KakaoLocalSearch("KEY")
    ps = k.search("오지다", 5)
    assert [(p.name, p.category_code) for p in ps] == [("오지다", "MEAL"), ("방이 호프", "BAR"),
                                                       ("레드버튼 잠실점", "ACTIVITY")]
    assert ps[0].area_label == "방이동" and ps[0].map_url and calls[0][1] == "KakaoAK KEY"
    assert "/keyword.json" in calls[0][0] and "size=5" in calls[0][0]
    near = k.nearby(37.5131, 127.1123, 1000, code="BAR")
    assert [p.name for p in near] == ["방이 호프"]
    assert any("/category.json" in u for u, _ in calls) is False  # BAR 는 카카오 그룹 없음 → 키워드만
    k.nearby(37.5131, 127.1123, 1000, code="MEAL")
    assert any("/category.json" in u and "category_group_code=FD6" in u for u, _ in calls)


def test_kakao_http_error(monkeypatch):
    from placeflow.v3.search import KakaoLocalSearch

    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "x", {}, io.BytesIO(b'{"message":"App disabled"}'))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ExternalSearchError) as e:
        KakaoLocalSearch("KEY").search("x")
    assert "403" in str(e.value) and "카카오맵" in str(e.value)


def test_provider_selection(monkeypatch):
    """v2.1: 장소 검색은 카카오 고정 (네이버 키만 있으면 데모, naver 지정은 오류)."""
    from placeflow.v3.search import KakaoLocalSearch, get_search_provider

    for k in ("SEARCH_PROVIDER", "KAKAO_REST_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert get_search_provider().name == "demo"
    monkeypatch.setenv("NAVER_CLIENT_ID", "a")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "b")
    assert get_search_provider().name == "demo"               # 네이버 키는 장소 검색에 안 씀
    monkeypatch.setenv("KAKAO_REST_API_KEY", "k")
    assert isinstance(get_search_provider(), KakaoLocalSearch)
    monkeypatch.setenv("SEARCH_PROVIDER", "demo")             # 명시하면 그 하나만
    assert get_search_provider().name == "demo"
    assert get_search_provider("demo_fail").name == "demo_fail"
    with pytest.raises(ValueError, match="카카오"):
        get_search_provider("naver")
    with pytest.raises(ValueError):
        get_search_provider("google")


def test_demo_nearby_uses_region_name(demo):
    """좌표 검색이 없는 데모 제공자: 가까운 지원 지역명 + 검색어, 지역 밖은 빈 결과."""
    r = get_region("YEONNAM")
    near = demo.nearby(r.lat, r.lng, 1000, code="CAFE")
    assert near and all(p.category_code == "CAFE" and distance_m((r.lat, r.lng), p.pos) <= 1000 for p in near)
    assert demo.nearby(35.1, 129.0, 1000, code="CAFE") == []


def test_category_inference(demo):
    from placeflow.v3.category_infer import infer_category, with_code

    cafe = next(p for p in demo.places if p.name == "연남 감성 카페")
    bare = Place(place_id="sp:1", name=cafe.name, lat=cafe.lat, lng=cafe.lng, provider="backend")
    assert bare.category_code is None
    assert infer_category(bare, demo).category_code == "CAFE"             # 검색으로 같은 가게를 찾아서
    odd = Place(place_id="sp:2", name="무명 분식", lat=37.0, lng=127.0, provider="backend")
    assert infer_category(odd, DemoSearch(fail=True)).category_code == "MEAL"  # 검색 실패 → 이름 글자로
    unknown = Place(place_id="sp:3", name="ZZZ", lat=37.0, lng=127.0, provider="backend")
    assert infer_category(unknown, demo).category_code is None
    assert with_code(unknown, "WALK").category_code == "WALK"


# ------------------------------------------------------------------ 해시태그로만 가게를 적은 캡션 (인스타 '잠실 맛집 베스트 9')
def test_hashtag_only_caption():
    from pathlib import Path

    from placeflow.pipeline import Pipeline
    from placeflow.v3.domain import Place
    from placeflow.v3.save_flow import candidates_from_records

    caption = (Path(__file__).parent / "fixtures" / "ig_songridan_hashtags.txt").read_text(encoding="utf-8")
    recs = Pipeline().run(caption).records
    names = [r.place.name for r in recs]
    assert names == ["장미맨숀", "쿠차라", "배키욘방", "차알", "단디", "팔레트커리", "미자식당", "라이언하트", "니엔테"]
    assert all("지역 단서: 잠실" in r.content.evidence for r in recs)          # #잠실맛집·#송리단길 → 지역 단서
    assert not {"잠실맛집", "잠실맛집추천", "송리단길맛집", "송리단길"} & set(names)  # 일반·지역 태그는 이름 아님

    def P(i, name, road, cat="restaurant"):
        return Place(place_id=i, name=name, road_address=road, category=cat, lat=37.51, lng=127.1, provider="kakao_local")
    db = [P("k", "쿠차라 잠실점", "서울 송파구 백제고분로45길 3"), P("d1", "단디", "서울 송파구 오금로16길 9"),
          P("d2", "단디", "부산 해운대구 구남로 1"), P("c", "차알 송리단길점", "서울 송파구 백제고분로41길 8", "cafe")]
    s = _FakeNaver(db)
    res = candidates_from_records(recs, s, "link")
    by = {g.extracted.name: g for g in res.groups}
    assert s.calls[0] == "잠실 장미맨숀" and "잠실 쿠차라" in s.calls             # 지역 단서로 검색 범위를 좁힘
    assert by["쿠차라"].status == "matched" and by["쿠차라"].best.place.name == "쿠차라 잠실점"
    assert by["차알"].best.place.name == "차알 송리단길점"
    assert by["장미맨숀"].status == "not_found" and by["니엔테"].status == "not_found"


def test_hashtags_ignored_when_caption_has_place_markers():
    from pathlib import Path

    from placeflow.pipeline import Pipeline

    caption = (Path(__file__).parent / "fixtures" / "ig_jamsil_4places.txt").read_text(encoding="utf-8")
    recs = Pipeline().run(caption).records                                    # 번호 목록이 있으면 해시태그는 안 씀
    assert all(not any(e.startswith("해시태그") for e in r.content.evidence) for r in recs)


def test_instagram_link_hashtag_caption_from_og_meta():
    """실제 인스타 링크 응답 형태: og:title 이 'Instagram의 ○○님 : "캡션"' 이어도 제목을 상호명으로 쓰지 않고 해시태그를 읽음."""
    import html as h
    from pathlib import Path

    from placeflow.fetchers.http import HttpResponse
    from placeflow.pipeline import Pipeline

    cap = (Path(__file__).parent / "fixtures" / "ig_songridan_hashtags.txt").read_text(encoding="utf-8").strip()
    desc = f'1,619 likes, 610 comments - inssa_elf - May 21, 2020: "{cap}". '
    title = f'Instagram의 인싸요정🧚🏻님 : "{cap}"'
    page = (f'<html><head><meta property="og:title" content="{h.escape(title)}">'
            f'<meta property="og:description" content="{h.escape(desc)}"></head><body></body></html>')

    class Client:
        def get(self, u):
            return HttpResponse(u, u, 200, page)

    recs = Pipeline(http=Client()).run("https://www.instagram.com/p/CAch34iB5RW/?utm_source=ig_web_copy_link").records
    assert [r.place.name for r in recs][:3] == ["장미맨숀", "쿠차라", "배키욘방"] and len(recs) == 9


# ---------------------------------------------------------------- 후보 찾기: 오독 보정 · 다른 지역
def _rec(name, road=None, region=None):
    from placeflow import Pipeline

    class LLM:
        supports_vision, model = True, "v0"
        name = "x"

        def extract(self, *a):
            return {"places": [{"name": name_, "address_hint": road, "region_hint": region, "confidence": 0.9}]}

    name_ = name
    return Pipeline(llm=LLM()).run("텍스트").records


def _found(*places):
    class S:
        name = "fake"

        def search(self, q, n=5):
            return list(places)

    return S()


def test_misread_name_is_fixed_by_address():
    from placeflow.v3.save_flow import candidates_from_records

    p = Place(place_id="a", name="가우동", road_address="서울 종로구 대학로9길 25", lat=37.5, lng=127.0, provider="kakao_local")
    g = candidates_from_records(_rec("가우뚱", "서울 종로구 대학로9길 25"), _found(p), "screenshot").groups[0]
    assert g.status == "matched" and g.best.place.name == "가우동" and "이름이 비슷해요" in g.best.reasons


def test_other_store_at_same_address_is_not_default():
    from placeflow.v3.save_flow import candidates_from_records

    p = Place(place_id="a", name="완전다른집", road_address="서울 종로구 대학로9길 25", lat=37.5, lng=127.0, provider="kakao_local")
    g = candidates_from_records(_rec("가우동", "서울 종로구 대학로9길 25"), _found(p), "screenshot").groups[0]
    assert g.best is None and g.others[0].match_type == "similar" and "주소만 일치" in g.others[0].reasons


def test_same_name_in_other_region_is_not_default():
    from placeflow.v3.save_flow import candidates_from_records

    far = Place(place_id="a", name="미자식당", road_address="경기 이천시 향교로 84", lat=37.2, lng=127.4, provider="kakao_local")
    near = Place(place_id="b", name="미자식당", road_address="서울 송파구 석촌호수로 1", lat=37.5, lng=127.1, provider="kakao_local")
    g = candidates_from_records(_rec("미자식당", None, "잠실"), _found(far), "screenshot").groups[0]
    assert g.best is None and "다른 지역" in g.others[0].reasons
    g = candidates_from_records(_rec("미자식당", None, "잠실"), _found(far, near), "screenshot").groups[0]
    assert g.best.place.place_id == "b"


def test_spaced_road_name_matches():
    from placeflow.v3.save_flow import _road_core

    assert _road_core("서울 마포구 와우산로 21길 28-6") == _road_core("서울 마포구 와우산로21길 28-6")


def test_address_outside_post_region_is_ignored():
    from placeflow.v3.save_flow import candidates_from_records

    p = Place(place_id="a", name="청화원", road_address="서울 종로구 대명길 36", lat=37.5, lng=127.0, provider="kakao_local")
    g = candidates_from_records(_rec("청화원", "서울 동대문구 제기로 36", "혜화"), _found(p), "screenshot").groups[0]
    assert g.status == "matched" and g.best.place.place_id == "a"


def test_name_with_area_suffix_is_exact_and_other_area_is_not_default():
    from placeflow.v3.save_flow import candidates_from_records

    near = Place(place_id="a", name="뉴오더클럽 연남", road_address="서울 마포구 동교로34길 3", lat=37.5, lng=126.9, provider="kakao_local")
    far = Place(place_id="b", name="뉴오더클럽 한남", road_address="서울 용산구 이태원로54길 58-14", lat=37.5, lng=127.0, provider="kakao_local")
    g = candidates_from_records(_rec("뉴오더클럽", None, "연남"), _found(far, near), "screenshot").groups[0]
    assert g.best.place.place_id == "a"


def test_spaced_road_query_keeps_building_number():
    from placeflow.v3.save_flow import candidates_from_records

    queries = []

    class S:
        name = "fake"

        def search(self, q, n=5):
            queries.append(q)
            return []

    candidates_from_records(_rec("미엔아이", "서울 송파구 오금로 16길 5"), S(), "screenshot")
    assert any("오금로16길 5" in q for q in queries)
