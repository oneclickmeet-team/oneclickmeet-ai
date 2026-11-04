"""AI 내부 API(FastAPI, TRD 형식) 계약 테스트. 네트워크 없이: 검색은 가짜/데모, 링크 수집은 차단 클라이언트."""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from placeflow.ai_api import deps  # noqa: E402
from placeflow.fetchers.http import HttpResponse  # noqa: E402
from placeflow.pipeline import Pipeline  # noqa: E402
from placeflow.v3.domain import Place  # noqa: E402
from placeflow.v3.search import DemoSearch, ExternalSearchError, _region_nearby  # noqa: E402

CAPTION = (Path(__file__).parent / "fixtures" / "ig_jamsil_4places.txt").read_text(encoding="utf-8")
P = "/internal/v1"


def _png() -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


def mk(i, name, road, cat="restaurant", lat=37.5131, lng=127.1123):
    return Place(place_id=i, name=name, road_address=road, category=cat, lat=lat, lng=lng, provider="kakao_local")


JAMSIL = [mk("a", "오지다", "서울특별시 송파구 백제고분로48길 24"),
          mk("b", "회포차오지다", "서울특별시 송파구 가락로 241 1층"),
          mk("c", "닭튜브 방이점", "서울특별시 송파구 백제고분로48길 7 1층"),
          mk("d", "일일식탁", "서울특별시 송파구 올림픽로35길 124 장미종합A상가 A-2동 지하1층"),
          mk("f", "리사르커피", "서울특별시 송파구 올림픽로35길 112 B동", "cafe")]


class FakeKakao:
    name = "fake"

    def search(self, q, display=5):
        toks = [x for x in q.split() if not x.endswith(("구", "동"))]
        return [p for p in JAMSIL if any(x.replace(" ", "") in (p.name + p.road_address).replace(" ", "")
                                          for x in toks)][:display]

    def nearby(self, lat, lng, radius_m=1000, code=None, query=None, limit=30):
        return _region_nearby(self, lat, lng, radius_m, code, query, limit, 5)


class Blocked:
    def get(self, u):
        return HttpResponse(u, u, 0, "")


@pytest.fixture
def make_client(monkeypatch):
    def _make(search=None, api_key=None, pipe=None, tagger=None):
        from placeflow.ai_api.main import create_app

        monkeypatch.setattr(deps, "_pipe", pipe or Pipeline(http=Blocked()))
        deps.get_settings.cache_clear()
        if api_key:
            monkeypatch.setenv("AI_SERVICE_KEY", api_key)
        else:
            monkeypatch.delenv("AI_SERVICE_KEY", raising=False)
        app = create_app()
        app.dependency_overrides[deps.get_search] = lambda: (search or FakeKakao())
        app.dependency_overrides[deps.get_tagger] = lambda: tagger  # 기본: 태그 끔 (.env 의 네이버 키로 실제 호출 방지)
        return TestClient(app)
    yield _make
    deps.get_settings.cache_clear()


def _problem(r, status, reason):
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == status and body["reason"] == reason and body["code"] and body["detail"]
    return body


# ------------------------------------------------------------------ 공통·기타
def test_health_regions_categories(make_client):
    c = make_client(search=DemoSearch())
    h = c.get("/health").json()
    assert h["status"] == "UP" and h["workflowVersion"] == "2.1" and h["authRequired"] is False
    assert h["tagProvider"] is None
    r = c.get(f"{P}/regions").json()
    assert [x["regionId"] for x in r["items"]] == ["YEONNAM", "SEOCHON", "HYEHWA", "JAMSIL", "SUNGSHIN", "HANSUNG"]
    assert r["items"][0]["radiusMeters"] > 0 and "latitude" in r["items"][0]
    cats = c.get(f"{P}/categories").json()["items"]
    assert [x["code"] for x in cats] == ["MEAL", "CAFE", "BAR", "ACTIVITY", "CULTURE", "WALK", "SHOPPING"]


def test_validation_error_is_problem_detail(make_client):
    c = make_client()
    body = _problem(c.post(f"{P}/course-recommendations", json={"anchor": {"type": "REGION"}}), 400, "INVALID_INPUT")
    assert body["errors"] and all("field" in e and "message" in e for e in body["errors"])


def test_service_key(make_client):
    c = make_client(api_key="secret")
    _problem(c.get(f"{P}/regions"), 401, "INVALID_SERVICE_KEY")
    assert c.get(f"{P}/regions", headers={"X-AI-Key": "secret"}).status_code == 200
    assert c.get("/health").status_code == 200


# ------------------------------------------------------------------ P02 후보 검색
def test_place_candidates_keyword_and_coords(make_client):
    c = make_client()
    r = c.get(f"{P}/place-candidates", params={"query": "리사르커피"}).json()
    assert r["items"][0]["name"] == "리사르커피" and r["page"] == 0 and r["size"] == 15 and r["hasNext"] is False
    item = r["items"][0]
    assert set(item) >= {"name", "address", "latitude", "longitude", "sourceUrl", "categoryLabel", "openingHours"}
    demo = make_client(search=DemoSearch())
    r = demo.get(f"{P}/place-candidates", params={"latitude": 37.5626, "longitude": 126.9255, "categoryCode": "CAFE",
                                                 "size": 2}).json()
    assert len(r["items"]) == 2 and all(i["categoryCode"] == "CAFE" for i in r["items"]) and r["hasNext"] is True
    assert r["items"][0]["distanceMeters"] <= r["items"][1]["distanceMeters"]
    r = demo.get(f"{P}/place-candidates", params={"regionId": "YEONNAM", "query": "파스타"}).json()
    assert r["items"][0]["name"] == "연남 파스타집"


def test_place_candidates_errors(make_client):
    c = make_client()
    _problem(c.get(f"{P}/place-candidates"), 400, "INVALID_INPUT")
    _problem(c.get(f"{P}/place-candidates", params={"latitude": 37.5}), 400, "INVALID_INPUT")
    _problem(c.get(f"{P}/place-candidates", params={"query": "x", "radiusMeters": 5000}), 400, "INVALID_INPUT")
    fail = make_client(search=DemoSearch(fail=True))
    _problem(fail.get(f"{P}/place-candidates", params={"query": "x"}), 503, "PROVIDER_UNAVAILABLE")


# ------------------------------------------------------------------ P03 링크·텍스트 인식
def test_recognition_caption_groups(make_client):
    c = make_client()
    saved = [{"placeId": 7, "name": "닭튜브 방이점", "latitude": 37.5131, "longitude": 127.1123}]
    r = c.post(f"{P}/place-recognitions", json={"sourceUrl": "https://www.instagram.com/reel/DegiE1ozc74/",
                                                "sharedText": CAPTION, "savedPlaces": saved})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "CANDIDATES_FOUND" and d["sourceUrl"].startswith("https://www.instagram.com")
    assert [g["extractedName"] for g in d["groups"]] == ["오지다", "닭튜브", "일일식탁", "리사르커피"]
    g0 = d["groups"][0]
    assert g0["status"] == "MATCHED" and g0["best"]["name"] == "오지다" and g0["best"]["matchType"] == "EXACT"
    assert all(o["matchType"] == "SIMILAR" for o in g0["others"])
    assert d["groups"][1]["best"]["savedPlaceId"] == 7
    assert d["candidates"][0]["name"] == "오지다"


def test_recognition_share_text_and_needs_search(make_client):
    c = make_client()
    share = "[네이버 지도]\n오지다\n서울 송파구 백제고분로48길 24\nhttps://naver.me/abc"
    d = c.post(f"{P}/place-recognitions", json={"sharedText": share}).json()
    assert d["status"] == "CANDIDATES_FOUND" and d["sourceUrl"] == "https://naver.me/abc"
    d = c.post(f"{P}/place-recognitions", json={"sourceUrl": "https://map.naver.com/p/entry/place/1"}).json()
    assert d["status"] == "NEEDS_SEARCH" and d["candidates"] == [] and d["message"]
    _problem(c.post(f"{P}/place-recognitions", json={}), 400, "INVALID_INPUT")
    _problem(c.post(f"{P}/place-recognitions", json={"sourceUrl": "ftp://x"}), 400, "INVALID_INPUT")
    fail = make_client(search=DemoSearch(fail=True))
    _problem(fail.post(f"{P}/place-recognitions", json={"sharedText": "📍연남 감성 카페 서울 마포구 연남로 1"}), 503,
             "PROVIDER_UNAVAILABLE")


# ------------------------------------------------------------------ P04 스크린샷 인식
def test_image_recognition_limits(make_client, monkeypatch):
    c = make_client()
    _problem(c.post(f"{P}/place-image-recognitions", files={"image": ("a.gif", b"GIF89a....", "image/gif")}), 415,
             "UNSUPPORTED_IMAGE_TYPE")
    monkeypatch.setenv("AI_MAX_IMAGE_MB", "1")
    deps.get_settings.cache_clear()
    big = _png() + b"\x00" * (1024 * 1024 + 10)
    _problem(c.post(f"{P}/place-image-recognitions", files={"image": ("a.png", big, "image/png")}), 413,
             "IMAGE_TOO_LARGE")
    monkeypatch.delenv("AI_MAX_IMAGE_MB")
    deps.get_settings.cache_clear()
    five = [("extraImages", (f"{i}.png", _png(), "image/png")) for i in range(5)]
    _problem(c.post(f"{P}/place-image-recognitions", files=[("image", ("a.png", _png(), "image/png"))] + five), 400,
             "INVALID_INPUT")
    # OCR 없는 서버 → 503
    _problem(c.post(f"{P}/place-image-recognitions", files={"image": ("a.png", _png(), "image/png")}), 503,
             "RECOGNIZER_UNAVAILABLE")


def test_image_recognition_ocr_path(make_client):
    seen = []

    class FakeOCR:
        name = "fake"

        def read(self, path):
            seen.append(path)
            return "place+\nSal 770| 대학로점\n주꾸미요리 ㆍ 4.42 ㆍ 리뷰 2,952\n영업 중\n서울 송파구 백제고분로48길 24\n"

    c = make_client(pipe=Pipeline(http=Blocked(), ocr=FakeOCR()))
    webp = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24
    r = c.post(f"{P}/place-image-recognitions", files=[("image", ("cap.png", io.BytesIO(_png()), "image/png")),
                                                       ("extraImages", ("2.webp", webp, "image/webp"))])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "CANDIDATES_FOUND" and d["groups"][0]["best"]["name"] == "오지다"
    assert len(seen) == 2 and not any(Path(x).exists() for x in seen)  # 2장 모두 읽고 임시 파일 정리


# ------------------------------------------------------------------ R01 코스 추천
def _saved(demo, names, start=100):
    out = []
    for k, p in enumerate([p for p in demo.places if p.name in names]):
        out.append({"placeId": start + k, "source": "PERSONAL", "name": p.name, "address": p.road_address,
                    "latitude": p.lat, "longitude": p.lng})       # categoryCode 없음 → AI 가 추론
    return out


def test_recommendation_place_anchor(make_client):
    demo = DemoSearch()
    c = make_client(search=demo)
    saved = _saved(demo, {"연남 파스타집", "연남 감성 카페", "연남 와인바"})
    cafe = next(s for s in saved if s["name"] == "연남 감성 카페")
    body = {"anchor": {"type": "PLACE", "placeId": cafe["placeId"]}, "placeCount": 4,
            "categorySequence": ["MEAL", "CAFE", "ACTIVITY", "BAR"], "requiredPlaceOrder": 2, "savedPlaces": saved}
    r = c.post(f"{P}/course-recommendations", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["reason"] is None and d["walkingTimeMethod"] == "COORDINATE_ESTIMATE" and 1 <= len(d["courses"]) <= 3
    for co in d["courses"]:
        assert co["candidateKey"] and co["title"] == "연남 맛집·카페·놀거리·술집 코스"
        assert [p["visitOrder"] for p in co["places"]] == [1, 2, 3, 4]
        assert co["places"][1]["isAnchor"] and co["places"][1]["placeId"] == cafe["placeId"]
        assert co["places"][1]["source"] == "PERSONAL"
        assert [p["categoryCode"] for i, p in enumerate(co["places"]) if i != 1] == ["MEAL", "ACTIVITY", "BAR"]
        assert all(leg["estimatedMinutes"] <= 10 for leg in co["walkingLegs"]) and len(co["walkingLegs"]) == 3
        for p in co["places"]:
            assert (p["placeId"] is None) == (p["source"] == "EXTERNAL")
    keys = [co["candidateKey"] for co in d["courses"]]
    again = c.post(f"{P}/course-recommendations", json={**body, "excludeCandidateKeys": keys}).json()
    assert not {co["candidateKey"] for co in again["courses"]} & set(keys)


def test_recommendation_region_and_reasons(make_client):
    demo = DemoSearch()
    c = make_client(search=demo)
    d = c.post(f"{P}/course-recommendations", json={"anchor": {"type": "REGION", "regionId": "JAMSIL"}, "placeCount": 3,
                                                   "categorySequence": ["WALK", "CAFE", "MEAL"]}).json()
    assert d["courses"] and all(p["source"] == "EXTERNAL" for co in d["courses"] for p in co["places"])
    d = c.post(f"{P}/course-recommendations", json={"anchor": {"type": "REGION", "regionId": "JAMSIL"}, "placeCount": 5,
                                                   "categorySequence": ["SHOPPING"] * 5}).json()
    assert d["courses"] == [] and d["reason"] == "INSUFFICIENT_PLACES"
    far = [{"placeId": 9, "name": "부산 카페", "latitude": 35.1, "longitude": 129.0, "categoryCode": "CAFE"}]
    d = c.post(f"{P}/course-recommendations", json={"anchor": {"type": "PLACE", "placeId": 9}, "placeCount": 3,
                                                   "categorySequence": ["CAFE", "MEAL", "BAR"], "requiredPlaceOrder": 1,
                                                   "savedPlaces": far}).json()
    assert d["courses"] == [] and d["reason"] == "UNSUPPORTED_REGION"


def test_recommendation_errors(make_client):
    c = make_client(search=DemoSearch())
    base = {"anchor": {"type": "REGION", "regionId": "JAMSIL"}, "placeCount": 3, "categorySequence": ["WALK", "CAFE", "MEAL"]}
    _problem(c.post(f"{P}/course-recommendations", json={**base, "placeCount": 4}), 400, "INVALID_INPUT")
    _problem(c.post(f"{P}/course-recommendations", json={**base, "requiredPlaceOrder": 1}), 400, "INVALID_INPUT")
    _problem(c.post(f"{P}/course-recommendations", json={**base, "categorySequence": ["WALK", "CAFE", "PARTY"]}), 400,
             "INVALID_INPUT")
    _problem(c.post(f"{P}/course-recommendations", json={**base, "anchor": {"type": "PLACE", "placeId": 1},
                                                         "requiredPlaceOrder": 1}), 404, "RESOURCE_NOT_FOUND")
    fail = make_client(search=DemoSearch(fail=True))
    _problem(fail.post(f"{P}/course-recommendations", json=base), 503, "PROVIDER_UNAVAILABLE")


def test_anchor_place_without_saved_list(make_client):
    """필수 장소를 anchorPlace 로만 보내도 됨 (카테고리 없음 → 이름으로 추론)."""
    demo = DemoSearch()
    c = make_client(search=demo)
    cafe = next(p for p in demo.places if p.name == "연남 감성 카페")
    body = {"anchor": {"type": "PLACE", "placeId": 55}, "placeCount": 3, "categorySequence": ["CAFE", "ACTIVITY", "CAFE"],
            "requiredPlaceOrder": 1,
            "anchorPlace": {"placeId": 55, "name": cafe.name, "latitude": cafe.lat, "longitude": cafe.lng}}
    d = c.post(f"{P}/course-recommendations", json=body).json()
    assert d["courses"] and all(co["places"][0]["placeId"] == 55 for co in d["courses"])
    r = c.post(f"{P}/course-recommendations", json={**body, "anchorPlace": {**body["anchorPlace"], "placeId": 56}})
    _problem(r, 400, "INVALID_INPUT")


# ------------------------------------------------------------------ (확장) 교체 후보
def test_replacements(make_client):
    demo = DemoSearch()
    c = make_client(search=demo)
    ps = [p for p in demo.places if p.name.startswith("연남 ")][:4]
    course = [{"name": p.name, "latitude": p.lat, "longitude": p.lng, "categoryCode": p.category_code} for p in ps]
    r = c.post(f"{P}/place-candidates/replacements", json={"coursePlaces": course, "replaceVisitOrder": 2})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert all(i["name"] not in {p.name for p in ps} for i in items)
    assert all(i["walkFromPrevMinutes"] <= 10 and i["walkToNextMinutes"] <= 10 for i in items)
    _problem(c.post(f"{P}/place-candidates/replacements", json={"coursePlaces": course, "replaceVisitOrder": 5}), 400,
             "INVALID_INPUT")


def test_external_failure_maps_to_503(make_client):
    class Boom(FakeKakao):
        name = "boom"

        def search(self, q, display=5):
            raise ExternalSearchError("down")

    c = make_client(search=Boom())
    body = _problem(c.get(f"{P}/place-candidates", params={"query": "x"}), 503, "PROVIDER_UNAVAILABLE")
    assert "down" not in body["detail"]  # 제공자 원문은 응답에 넣지 않음


# ------------------------------------------------------------------ (확장) 블로그 태그
class FakeBlog:
    """가게 이름별 가짜 블로그 글 (제목, 요약)."""
    POSTS = {
        "오지다": [("<b>오지다</b> 방이동 맛집", "음식이 맛있어요 양이 많아서 좋았고 조용한 편"),
                 ("송파 오지다 후기", "시그니처 모듬초밥, 음식이 정말 맛있었다"),
                 ("다른 가게 이야기", "음식이 맛있어요 혼술")],          # 가게 이름 없음 → 안 셈
        "리사르커피": [("리사르커피 잠실", "디저트도 맛있어요. 아늑하고 사진이 잘 나와요")],
    }

    def __init__(self, fail=False):
        self.fail = fail
        self.queries = []

    def search(self, q, display=50):
        from placeflow.v3.blog_tags import BlogPost

        self.queries.append(q)
        if self.fail:
            raise ExternalSearchError("blog down")
        for name, posts in self.POSTS.items():
            if q.startswith(name):
                return [BlogPost(t, d) for t, d in posts]
        return []


class FakeLLM:
    """OpenAI 대역: 글에 나온 단어로 태그를 고름."""
    name, model = "openai", "test"
    WORDS = {"음식이 맛있": "음식이 맛있어요", "음식이 정말 맛있": "음식이 맛있어요", "양이 많": "양이 많아요",
             "조용": "차분한 분위기에요", "시그니처": "특별한 메뉴가 있어요", "혼술": "혼술하기 좋아요",
             "디저트도 맛있": "디저트가 맛있어요", "아늑": "아늑해요", "사진이 잘": "사진이 잘나와요"}

    def extract(self, system, user_text, images, schema):
        import re
        posts = re.findall(r"^\[(\d+)\] (.*)$", user_text, re.M)
        return {"posts": [{"index": int(i), "tags": [l for w, l in self.WORDS.items() if w in t]} for i, t in posts]}


def _tagger(blog):
    from placeflow.v3.blog_tags import BlogTagger, LLMTagJudge

    return BlogTagger(blog, LLMTagJudge(FakeLLM()))


def test_recognition_has_blog_tags(make_client):
    blog = FakeBlog()
    c = make_client(tagger=_tagger(blog))
    j = c.post(f"{P}/place-recognitions", json={"sharedText": CAPTION}).json()
    assert j["tagSource"] == "NAVER_BLOG"
    by = {x["name"]: x for x in j["candidates"]}
    oj = by["오지다"]
    assert oj["tagPostCount"] == 2
    assert oj["tags"][0] == {"label": "음식이 맛있어요", "mentionCount": 2}
    assert {"label": "특별한 메뉴가 있어요", "mentionCount": 1} in oj["tags"]
    assert {"label": "혼술하기 좋아요", "mentionCount": 1} not in oj["tags"]   # 이름 없는 글은 안 셈
    assert [t["label"] for t in by["리사르커피"]["tags"]] == ["사진이 잘나와요", "아늑해요", "디저트가 맛있어요"]
    assert len(blog.queries) == len(set(blog.queries))                 # 같은 가게는 한 번만 검색
    g = next(g for g in j["groups"] if g["best"] and g["best"]["name"] == "오지다")
    assert g["best"]["tags"] == oj["tags"]                             # 묶음 안 후보에도 같은 태그


def test_recognition_tags_off_or_failed(make_client):
    j = make_client().post(f"{P}/place-recognitions", json={"sharedText": CAPTION}).json()
    assert j["status"] == "CANDIDATES_FOUND" and j["tagSource"] is None
    assert all(x["tags"] == [] and x["tagPostCount"] is None for x in j["candidates"])
    r = make_client(tagger=_tagger(FakeBlog(fail=True))).post(f"{P}/place-recognitions",
                                                                 json={"sharedText": CAPTION})
    assert r.status_code == 200                                        # 블로그 장애는 인식 결과를 막지 않음
    j = r.json()
    assert j["status"] == "CANDIDATES_FOUND" and j["tagSource"] is None
    assert all(x["tags"] == [] for x in j["candidates"])
