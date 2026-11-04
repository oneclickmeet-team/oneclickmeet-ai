"""네이버 블로그 검색 + OpenAI 로 장소 태그 만들기 (C 방식의 A 단계).

TRD: 네이버 블로그 검색의 **제목·요약만** 사용, 블로그 본문 크롤링 없음.
  1) '가게 이름 + 동네' 로 블로그 검색 (최대 50건)
  2) 제목·요약에 가게 이름이 들어 있는 글만 남김
  3) OpenAI(LLM)가 글마다 review_tags.json 의 태그 12개 중 그 글이 **긍정적으로** 말하는 태그를 고름
  4) 태그별로 고른 글 수를 세어 많은 순 상위 N개 → [(태그, 언급한 글 수)]
네이버의 실제 리뷰 선택 횟수가 아니라 '블로그 언급 수' 이므로 앱에는 그렇게 표시한다.
B 단계(우리 앱 사용자가 고른 태그 집계)는 백엔드가 DB 로 처리하고, 언제 바꿔 보여줄지도 백엔드가 정한다.
LLM 호출이 실패하면 태그 없이 응답한다(규칙 기반으로 대신하지 않음).

환경변수
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET   네이버 블로그 검색 (없으면 태그 기능 꺼짐)
  NAVER_SEARCH_ENDPOINT=hub|legacy        (기본 hub: NAVER API HUB 키 / legacy: 개발자센터 기존 키)
  OPENAI_API_KEY                          태그 판단 (없으면 태그 기능 꺼짐)
  TAG_LLM_MODEL                           OpenAI 모델 (기본 gpt-4o-mini)
  BLOG_TAGS=off                           (키가 있어도 끄기)
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from .domain import Place
from .search import ExternalSearchError

VOCAB_PATH = Path(__file__).with_name("review_tags.json")
TOP_N = 5
BLOG_DISPLAY = 50
MAX_PLACES_PER_REQUEST = 12
DEFAULT_TAG_MODEL = "gpt-4o-mini"
_TAG_RE = re.compile(r"<[^>]+>")
log = logging.getLogger("placeflow.blog_tags")


class TagJudgeError(Exception):
    """LLM 태그 판단 실패 (키·한도·네트워크·응답 형식)."""


# ------------------------------------------------------------------ 태그 목록
@dataclass(frozen=True)
class TagDef:
    label: str
    hint: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


@lru_cache
def load_vocab(path: str = str(VOCAB_PATH)) -> tuple[TagDef, ...]:
    """review_tags.json 의 태그 목록 (모든 카테고리 공통)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    seen, out = set(), []
    for it in data.get("tags", []):
        if it["label"] not in seen:
            seen.add(it["label"])
            out.append(TagDef(it["label"], it.get("hint", "")))
    return tuple(out)


def rank(counts: dict[str, int], vocab: Iterable[TagDef], top_n: int = TOP_N) -> list[tuple[str, int]]:
    """많은 순 상위 top_n. 동률은 태그 목록 순서."""
    order = [t.label for t in vocab]
    ranked = sorted((l for l in order if counts.get(l, 0) > 0), key=lambda l: (-counts[l], order.index(l)))
    return [(l, counts[l]) for l in ranked[:top_n]]


# ------------------------------------------------------------------ LLM 태그 판단
_SYSTEM = """너는 장소 블로그 글의 제목·요약을 읽고 장소 태그를 고르는 분류기다.
규칙
- 글마다, 그 글이 해당 가게에 대해 **긍정적으로** 말하는 태그만 고른다. 근거가 글에 직접 드러나야 한다.
- 부정("조용하지 않았다", "양이 적다"), 의문, 기대만 하는 말, 다른 가게 이야기는 고르지 않는다.
- 해당하는 태그가 없으면 빈 목록이다. 억지로 고르지 않는다.
- 태그는 주어진 목록의 문자열을 그대로 쓴다.
- 모든 글에 대해 index 를 빠짐없이 돌려준다."""


def _schema(labels: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"posts": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"},
                           "tags": {"type": "array", "items": {"type": "string", "enum": labels}}},
            "required": ["index", "tags"], "additionalProperties": False}}},
        "required": ["posts"], "additionalProperties": False,
    }


class LLMTagJudge:
    """글 목록 → 태그별 글 수. llm 은 extract(system, user_text, images, schema) -> dict 를 가진 객체
    (placeflow.llm.providers.OpenAIProvider)."""

    def __init__(self, llm, vocab: Optional[tuple[TagDef, ...]] = None):
        self.llm = llm
        self.vocab = vocab or load_vocab()

    @property
    def name(self) -> str:
        return f"{getattr(self.llm, 'name', 'llm')}:{getattr(self.llm, 'model', '')}".rstrip(":")

    def prompt(self, place: Place, texts: list[str]) -> str:
        tags = "\n".join(f"- {t.label}" + (f" ({t.hint})" if t.hint else "") for t in self.vocab)
        posts = "\n".join(f"[{i}] {t}" for i, t in enumerate(texts, 1))
        where = " · ".join(x for x in (place.category_label, place.area_label) if x)
        return f"가게: {place.name}" + (f" ({where})" if where else "") + f"\n\n태그 목록\n{tags}\n\n글 목록\n{posts}"

    def count(self, place: Place, texts: list[str]) -> dict[str, int]:
        if not texts:
            return {}
        labels = [t.label for t in self.vocab]
        try:
            out = self.llm.extract(_SYSTEM, self.prompt(place, texts), [], _schema(labels))
        except Exception as e:  # SDK 마다 예외 종류가 달라서 모두 잡음
            raise TagJudgeError(f"OpenAI 태그 판단 실패: {type(e).__name__}: {e}") from e
        if not isinstance(out, dict) or not isinstance(out.get("posts"), list):
            raise TagJudgeError("OpenAI 응답 형식이 올바르지 않습니다")
        per_post: dict[int, set[str]] = {}
        for item in out["posts"]:
            try:
                i = int(item.get("index"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 1 <= i <= len(texts):  # 범위 밖·목록 밖 태그는 버림, 같은 글은 한 번만
                per_post.setdefault(i, set()).update(t for t in (item.get("tags") or []) if t in labels)
        counts: dict[str, int] = {}
        for tags in per_post.values():
            for t in tags:
                counts[t] = counts.get(t, 0) + 1
        return counts


# ------------------------------------------------------------------ 네이버 블로그 검색
@dataclass(frozen=True)
class BlogPost:
    title: str
    description: str


def _clean(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


class NaverBlogSearch:
    """네이버 검색 API '블로그'. 하루 25,000회. 결과의 제목·요약만 쓴다."""

    name = "naver_blog"
    ENDPOINTS = {
        "hub": ("https://naverapihub.apigw.ntruss.com/search/v1/blog", "X-NCP-APIGW-API-KEY-ID",
                "X-NCP-APIGW-API-KEY", "&format=json"),
        "legacy": ("https://openapi.naver.com/v1/search/blog.json", "X-Naver-Client-Id", "X-Naver-Client-Secret", ""),
    }

    def __init__(self, client_id: str | None = None, client_secret: str | None = None, timeout: float = 5.0,
                 cache_ttl: int = 24 * 3600, endpoint: str | None = None):
        self.cid = client_id or os.environ.get("NAVER_CLIENT_ID")
        self.secret = client_secret or os.environ.get("NAVER_CLIENT_SECRET")
        if not (self.cid and self.secret):
            raise ValueError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 필요합니다 (.env)")
        self.endpoint = (endpoint or os.environ.get("NAVER_SEARCH_ENDPOINT") or "hub").strip().lower()
        if self.endpoint not in self.ENDPOINTS:
            raise ValueError("NAVER_SEARCH_ENDPOINT 는 hub 또는 legacy 여야 합니다")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, list[BlogPost]]] = {}
        self._lock = threading.Lock()

    def search(self, query: str, display: int = BLOG_DISPLAY) -> list[BlogPost]:
        q = query.strip()
        if not q:
            return []
        key = f"{q}|{display}"
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_ttl:
            return hit[1]
        base, id_header, secret_header, extra = self.ENDPOINTS[self.endpoint]
        url = f"{base}?query={urllib.parse.quote(q)}&display={min(max(display, 1), 100)}&sort=sim{extra}"
        req = urllib.request.Request(url, headers={id_header: self.cid, secret_header: self.secret})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:  # HUB: {"error": {...}} / 개발자센터: {"errorCode", "errorMessage"}
                body = json.loads(e.read().decode("utf-8"))
                err = body.get("error") if isinstance(body.get("error"), dict) else body
                detail = " " + str(err.get("message") or err.get("errorMessage") or err.get("errorCode") or "")
            except Exception:
                pass
            hint = (" (키 확인. HUB 키면 콘솔의 Application 에 '검색 > 블로그' API 가 추가돼 있는지, "
                    "개발자센터 키면 NAVER_SEARCH_ENDPOINT=legacy)") if e.code in (401, 403) else ""
            hint = " (하루 호출 한도 초과)" if e.code == 429 else hint
            raise ExternalSearchError(f"네이버 블로그 검색 HTTP {e.code}{detail.rstrip()}{hint}") from e
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise ExternalSearchError(f"네이버 블로그 검색 연결 실패: {e}") from e
        posts = [BlogPost(_clean(it.get("title", "")), _clean(it.get("description", "")))
                 for it in data.get("items", [])]
        with self._lock:
            self._cache[key] = (time.time(), posts)
        return posts


# ------------------------------------------------------------------ 장소 → 태그
_GENERIC = {"카페", "커피", "식당", "맛집", "본점", "레스토랑", "술집", "공방", "베이커리", "디저트", "the", "cafe"}


def name_key(name: str) -> str:
    """글에 가게 이름이 나오는지 볼 때 쓰는 핵심 단어: 지점명(…점)·일반 단어를 뺀 가장 긴 단어."""
    toks = [t for t in re.split(r"[\s·,()\[\]/&+-]+", name or "") if t]
    core = [t for t in toks if not (len(t) >= 3 and t.endswith("점")) and t.lower() not in _GENERIC and len(t) >= 2]
    return _norm(max(core, key=len) if core else (name or ""))


@dataclass(frozen=True)
class PlaceTags:
    tags: list[tuple[str, int]]   # (태그, 언급한 글 수)
    post_count: int               # 가게 이름이 나온 글 수 (LLM 에 보낸 글)


class BlogTagger:
    def __init__(self, blog: NaverBlogSearch, judge: LLMTagJudge, top_n: int = TOP_N, workers: int = 4):
        self.blog = blog
        self.judge = judge
        self.top_n = top_n
        self.workers = workers
        self._cache: dict[str, PlaceTags] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return f"{self.blog.name}+{self.judge.name}"

    def query_of(self, p: Place) -> str:
        area = p.area_label or ""
        return f"{p.name} {area}".strip() if area and area not in p.name else p.name

    def tag_place(self, p: Place) -> PlaceTags:
        """블로그 검색 실패는 ExternalSearchError, LLM 실패는 TagJudgeError (tag_many 에서 빈 태그로 처리)."""
        with self._lock:
            if p.place_id in self._cache:
                return self._cache[p.place_id]
        key = name_key(p.name)
        posts = self.blog.search(self.query_of(p))
        texts = [f"{b.title} / {b.description}" for b in posts if key and key in _norm(f"{b.title} {b.description}")]
        res = PlaceTags(rank(self.judge.count(p, texts), self.judge.vocab, self.top_n), len(texts))
        with self._lock:  # 성공한 결과만 24시간 동안 캐시 (같은 가게 LLM 재호출 방지)
            self._cache[p.place_id] = res
        return res

    def tag_many(self, places: Iterable[Place], limit: int = MAX_PLACES_PER_REQUEST) -> tuple[dict[str, PlaceTags], bool]:
        """같은 가게는 한 번만, 최대 limit 곳. 반환: ({place_id: PlaceTags}, 하나라도 성공했는지)."""
        uniq: dict[str, Place] = {}
        for p in places:
            if p.place_id not in uniq and len(uniq) < limit:
                uniq[p.place_id] = p
        if not uniq:
            return {}, True

        def one(p: Place):
            try:
                return p.place_id, self.tag_place(p)
            except (ExternalSearchError, TagJudgeError) as e:
                log.warning("블로그 태그 실패 (%s): %s", p.name, e)  # 서버 터미널에 원인 표시
                return p.place_id, None

        with ThreadPoolExecutor(max_workers=min(self.workers, len(uniq))) as ex:
            results = list(ex.map(one, uniq.values()))
        out = {pid: r for pid, r in results if r is not None}
        return out, bool(out)


def get_tagger() -> Optional[BlogTagger]:
    """네이버 키 + OpenAI 키가 있고 BLOG_TAGS 가 off 가 아니면 BlogTagger, 아니면 None (태그 기능 꺼짐)."""
    if (os.environ.get("BLOG_TAGS") or "").strip().lower() in ("off", "0", "false", "no"):
        return None
    if not (os.environ.get("NAVER_CLIENT_ID") and os.environ.get("NAVER_CLIENT_SECRET")):
        return None
    if not os.environ.get("OPENAI_API_KEY"):
        log.warning("블로그 태그 꺼짐: OPENAI_API_KEY 가 없습니다 (.env)")
        return None
    try:
        from ..llm.providers import OpenAIProvider

        llm = OpenAIProvider(os.environ.get("TAG_LLM_MODEL") or DEFAULT_TAG_MODEL)
    except ImportError:
        log.warning("블로그 태그 꺼짐: openai 패키지가 없습니다 → python3 -m pip install openai")
        return None
    return BlogTagger(NaverBlogSearch(), LLMTagJudge(llm))
