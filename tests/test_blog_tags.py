"""네이버 블로그 제목·요약 + OpenAI(가짜) → 장소 태그 (네트워크 없이)."""
from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request

import pytest

from placeflow.v3.blog_tags import (BlogPost, BlogTagger, LLMTagJudge, NaverBlogSearch, TagJudgeError, get_tagger,
                                    load_vocab, name_key, rank)
from placeflow.v3.domain import Place
from placeflow.v3.search import ExternalSearchError

TEAM_TAGS = ["뷰가 좋아요", "컨셉이 독특해요", "사진이 잘나와요", "차분한 분위기에요", "야외공간이 멋져요", "아늑해요",
             "혼술하기 좋아요", "집중하기 좋아요", "음식이 맛있어요", "양이 많아요", "특별한 메뉴가 있어요", "디저트가 맛있어요"]


class FakeLLM:
    """OpenAIProvider 대역: 글 목록을 읽고 단어로 태그를 고름. 받은 프롬프트·스키마를 기록."""
    name, model = "openai", "gpt-4o-mini"
    WORDS = {"뷰": "뷰가 좋아요", "아늑": "아늑해요", "양이 많": "양이 많아요", "맛있": "음식이 맛있어요",
             "혼술": "혼술하기 좋아요"}

    def __init__(self, extra=None, fail=False):
        self.calls = []
        self.extra = extra or []
        self.fail = fail

    def extract(self, system, user_text, images, schema):
        self.calls.append((system, user_text, schema))
        if self.fail:
            raise RuntimeError("401 invalid api key")
        posts = re.findall(r"^\[(\d+)\] (.*)$", user_text, re.M)
        out = [{"index": int(i), "tags": [lab for w, lab in self.WORDS.items() if w in t and "않" not in t]}
               for i, t in posts]
        return {"posts": out + self.extra}


def test_vocab_is_team_list():
    v = load_vocab()
    assert [t.label for t in v] == TEAM_TAGS and all(t.hint for t in v)


def test_rank_order_and_top_n():
    v = load_vocab()
    assert rank({"아늑해요": 2, "뷰가 좋아요": 2, "양이 많아요": 3, "없는 태그": 9}, v) == [
        ("양이 많아요", 3), ("뷰가 좋아요", 2), ("아늑해요", 2)]                   # 동률은 목록 순서
    assert len(rank({t: 1 for t in TEAM_TAGS}, v, top_n=5)) == 5


def _place(name="닭튜브 방이점", cat="restaurant", area="방이동"):
    return Place(place_id="p1", name=name, category=cat, category_label="맛집", area_label=area,
                 lat=37.5, lng=127.1, provider="kakao_local")


def test_judge_counts_once_per_post_and_validates():
    llm = FakeLLM(extra=[{"index": 1, "tags": ["뷰가 좋아요", "모르는 태그"]},     # 같은 글 중복·목록 밖 태그
                         {"index": 99, "tags": ["아늑해요"]}])                    # 없는 글 번호
    j = LLMTagJudge(llm)
    counts = j.count(_place(), ["뷰가 멋지고 음식도 맛있어요", "양이 많아요 맛있음", "조용하지 않았다"])
    assert counts == {"뷰가 좋아요": 1, "음식이 맛있어요": 2, "양이 많아요": 1}
    system, user, schema = llm.calls[0]
    assert "긍정적으로" in system and "가게: 닭튜브 방이점 (맛집 · 방이동)" in user and "[3] 조용하지 않았다" in user
    assert schema["properties"]["posts"]["items"]["properties"]["tags"]["items"]["enum"] == TEAM_TAGS
    assert j.count(_place(), []) == {} and len(llm.calls) == 1                   # 글이 없으면 호출 안 함
    with pytest.raises(TagJudgeError, match="401"):
        LLMTagJudge(FakeLLM(fail=True)).count(_place(), ["x"])


def test_name_key():
    assert name_key("닭튜브 방이점") == "닭튜브"
    assert name_key("리사르 커피") == "리사르"
    assert name_key("오지다") == "오지다"
    assert name_key("Cafe Onion 성수점") == "onion"


class _Blog:
    name = "naver_blog"

    def __init__(self, posts):
        self.posts = posts
        self.calls = []

    def search(self, q, display=50):
        self.calls.append(q)
        return self.posts


def test_tagger_filters_posts_without_name_and_caches():
    blog = _Blog([BlogPost("닭튜브 방이점 후기", "음식도 맛있고 양이 많아요"),
                  BlogPost("방이동 치킨집 추천", "양이 많아요 혼술"),          # 가게 이름 없음 → LLM 에 안 보냄
                  BlogPost("닭튜브", "맛있어요 아늑")])
    llm = FakeLLM()
    t = BlogTagger(blog, LLMTagJudge(llm))
    r = t.tag_place(_place())
    assert blog.calls == ["닭튜브 방이점 방이동"]
    assert r.post_count == 2 and r.tags[0] == ("음식이 맛있어요", 2) and ("혼술하기 좋아요", 1) not in r.tags
    assert "방이동 치킨집" not in llm.calls[0][1]
    t.tag_place(_place())
    assert len(blog.calls) == 1 and len(llm.calls) == 1                         # 같은 가게는 캐시
    assert t.name == "naver_blog+openai:gpt-4o-mini"


def test_tag_many_handles_failure():
    class Boom:
        def search(self, q, display=50):
            raise ExternalSearchError("x")

    tags, ok = BlogTagger(Boom(), LLMTagJudge(FakeLLM())).tag_many([_place()])
    assert tags == {} and ok is False
    tags, ok = BlogTagger(_Blog([BlogPost("닭튜브", "맛있")]), LLMTagJudge(FakeLLM(fail=True))).tag_many([_place()])
    assert tags == {} and ok is False                                           # LLM 실패 → 태그 없음 (규칙 대체 없음)
    tags, ok = BlogTagger(_Blog([]), LLMTagJudge(FakeLLM())).tag_many([_place(), _place()])
    assert ok is True and tags["p1"].tags == [] and tags["p1"].post_count == 0


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_naver_blog_http(monkeypatch):
    calls = []

    def fake(req, timeout=0):
        calls.append((req.full_url, dict(req.header_items())))
        return _Resp(json.dumps({"items": [{"title": "<b>오지다</b> &amp; 후기", "description": "맛있어요"}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    b = NaverBlogSearch("ID", "SEC", endpoint="hub")
    posts = b.search("오지다 방이동")
    assert posts == [BlogPost("오지다 & 후기", "맛있어요")]
    url, headers = calls[0]
    assert url.startswith("https://naverapihub.apigw.ntruss.com/search/v1/blog?") and "display=50" in url
    assert "format=json" in url and headers.get("X-ncp-apigw-api-key-id") == "ID"
    b.search("오지다 방이동")
    assert len(calls) == 1                                              # 캐시
    NaverBlogSearch("ID", "SEC", endpoint="legacy").search("x")
    assert calls[1][0].startswith("https://openapi.naver.com/v1/search/blog.json?")


def test_naver_blog_http_error(monkeypatch):
    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ExternalSearchError, match="401"):
        NaverBlogSearch("ID", "SEC").search("x")


def test_get_tagger_env(monkeypatch):
    import sys
    import types

    for k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "BLOG_TAGS", "OPENAI_API_KEY", "TAG_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert get_tagger() is None
    monkeypatch.setenv("NAVER_CLIENT_ID", "a")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "b")
    assert get_tagger() is None                                                 # OpenAI 키 없음 → 꺼짐
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake = types.ModuleType("openai")
    fake.OpenAI = lambda: object()
    monkeypatch.setitem(sys.modules, "openai", fake)
    t = get_tagger()
    assert isinstance(t, BlogTagger) and t.judge.llm.model == "gpt-4o-mini"     # 기본 모델
    monkeypatch.setenv("TAG_LLM_MODEL", "gpt-4o")
    assert get_tagger().judge.llm.model == "gpt-4o"
    monkeypatch.setenv("BLOG_TAGS", "off")
    assert get_tagger() is None
