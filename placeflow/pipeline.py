"""워크플로우 오케스트레이션.

  입력 ─▶ ①판별(router) ─▶ ②수집(fetch / 이미지 / OCR) ─▶ ③추출(LLM 또는 휴리스틱)
       ─▶ ④검증·정리(스키마, 택소노미) ─▶ ⑤장소 확정(지도 API) ─▶ ⑥신뢰도·상태 ─▶ PlaceRecord[]
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from . import geo as geo_mod
from . import enrich, normalize, prompts, taxonomy
from .fetchers import FetchedContent, fetch_link
from .fetchers.http import HttpClient, UrllibClient
from .heuristic import HeuristicExtractor
from .llm import ImageInput, inline_refs
from .models import (Address, Classification, Confidence, Content, Details, ExternalIds, ExtractedPlace,
                     ExtractionResult, FieldSource, GeoPoint, PipelineOutput, PlaceInfo, PlaceRecord, SourceInfo, SourceType,
                     Status)
from .router import route


@dataclass
class PipelineConfig:
    geo_accept: float = 0.75          # 지도 후보를 같은 장소로 인정하는 최소 점수
    complete_threshold: float = 0.7   # 이 이상이면 status=complete
    fallback_to_heuristic: bool = True
    ocr_accept: float = 0.5           # OCR + 규칙 기반 추출의 최소 확신도. 미만이면 비전 LLM 으로 다시 읽음


@dataclass
class Pipeline:
    llm: object = field(default_factory=HeuristicExtractor)
    geo: object | None = None
    ocr: object | None = None
    vision_fallback: object | None = None  # OCR 로 추출하지 못한 캡쳐만 다시 읽는 비전 LLM
    http: HttpClient = field(default_factory=UrllibClient)
    config: PipelineConfig = field(default_factory=PipelineConfig)
    link_body: bool = False  # TRD: 링크는 메타 정보만. True 는 평가·예전 화면(/legacy) 전용

    def __post_init__(self):
        self._schema = inline_refs(ExtractionResult.model_json_schema())
        self._system = prompts.system_prompt()

    # ------------------------------------------------------------------ public
    def run(self, value: str, extra_text: str | None = None, more_images: list[str] | None = None) -> PipelineOutput:
        """value: 링크/텍스트/이미지 경로. more_images: 같은 장소의 추가 캡쳐(메뉴·정보 탭 등)."""
        trace: list[dict] = []
        t0 = time.perf_counter()

        def step(_step, **info):
            trace.append({"step": _step, "ms": round((time.perf_counter() - t0) * 1000), **info})

        r = route(value, extra_text)
        step("route", kind=r.kind, source_type=r.source_type.value, ids=r.ids)

        # ② 수집
        images: list[ImageInput] = []
        title, body = "", ""
        source = SourceInfo(type=r.source_type, input_kind=r.kind)
        if r.kind == "link":
            fc: FetchedContent = fetch_link(r, self.http, body=self.link_body)
            source.url, source.final_url, source.fetch_status = fc.url, fc.final_url, fc.status
            if fc.source_type != r.source_type:  # 단축 링크가 풀린 경우
                source.type = fc.source_type
            r.ids.update(fc.ids)
            title, body = fc.title, fc.text if fc.text else fc.description
            if fc.description and fc.description not in body:
                body = f"{fc.description}\n{body}".strip()
            step("fetch", status=fc.status, chars=len(body), note=fc.note or None)
            if fc.status != "ok" and not extra_text and not title:
                msg = fc.note or "링크에서 내용을 가져오지 못했습니다. 캡쳐 이미지나 캡션을 함께 올려 주세요."
                return PipelineOutput(status=Status.needs_user_input, message=msg, trace=trace)
        elif r.kind == "image":
            paths = [Path(r.value)] + [Path(x) for x in (more_images or [])]
            source.file_name = ", ".join(p.name for p in paths)[:200]
            for path in paths:
                if not path.is_file() or path.stat().st_size == 0:
                    step("image", error="not_found_or_empty", file=path.name)
                    return PipelineOutput(status=Status.failed, trace=trace,
                                          message="이미지 파일을 찾을 수 없거나 비어 있습니다. 다시 올려 주세요.")
                if path.stat().st_size > 20_000_000:
                    return PipelineOutput(status=Status.failed, trace=trace, message="이미지가 너무 큽니다(20MB 초과).")
            source.fetch_status = "ok"
            if getattr(self.llm, "supports_vision", False):
                images = [ImageInput.from_path(p) for p in paths]
                step("image", mode="vision", count=len(images), bytes=sum(len(i.data) for i in images))
            elif self.ocr is not None:
                texts = []
                for i, path in enumerate(paths):
                    try:
                        t = self.ocr.read(str(path))
                    except Exception as e:
                        step("ocr_error", engine=self.ocr.name, file=path.name, error=str(e)[:200])
                        if self.vision_fallback is None:
                            return PipelineOutput(status=Status.failed, trace=trace, message=f"OCR 실패: {e}")
                        t = ""
                    texts.append(t.strip())
                    step("ocr", engine=self.ocr.name, file=path.name, chars=len(t))
                body = "\n\n".join(texts)
                from .heuristic import _place_screen_name

                names = {n for n in (_place_screen_name(t) for t in texts) if n}
                if len(names) > 1:
                    r.ids["capture_names"] = sorted(names)
                    step("capture_mismatch", names=sorted(names))
            elif self.vision_fallback is None:
                return PipelineOutput(status=Status.failed, trace=trace,
                                      message="이미지를 읽으려면 비전 LLM 또는 OCR(tesseract) 설정이 필요합니다.")
        else:
            body = r.value
            source.fetch_status = "ok"

        # ③ 추출
        user = prompts.build_user(source.type.value, source.url, title, body, extra_text, bool(images))
        result, extractor = self._extract(user, images, step)
        if r.kind == "image" and not images and self.vision_fallback is not None and self._needs_vision(result, r.ids):
            step("vision_fallback", reason="ocr_insufficient", count=len(paths))
            user = prompts.build_user(source.type.value, source.url, "", "", extra_text, True)
            retried, ident = self._extract(user, [ImageInput.from_path(p) for p in paths], step, self.vision_fallback)
            if retried is not None:  # 호출이 실패하면 OCR 결과를 그대로 둠
                result, extractor, body = retried, ident, ""
                r.ids.pop("capture_names", None)
            elif result is not None and not result.places:  # 호출 실패를 '장소 없음'으로 안내하지 않음
                result, extractor = None, ident
        if result is None:
            err = next((t.get("error") for t in reversed(trace) if t["step"] == "extract_error"), "알 수 없는 오류")
            return PipelineOutput(status=Status.failed, trace=trace,
                                  message=f"추출기({extractor}) 호출에 실패했습니다: {err}")
        if not result.places:
            msg = "장소 정보를 찾지 못했습니다. 상호명이 보이는 캡쳐를 올리거나 장소 이름을 메모로 적어 주세요."
            return PipelineOutput(status=Status.needs_user_input, message=msg, trace=trace)

        # ④~⑥ 장소별 정리
        raw_excerpt = normalize.mask_pii("\n".join(x for x in (title, body, extra_text) if x))
        records = [self._to_record(p, source, r.ids, extractor, raw_excerpt, step) for p in result.places]
        overall = Status.complete if all(x.status == Status.complete for x in records) else Status.needs_review
        step("done", records=len(records), status=overall.value)
        return PipelineOutput(status=overall, records=records, trace=trace)

    # ------------------------------------------------------------------ internals
    def _needs_vision(self, result: ExtractionResult | None, ids: dict) -> bool:
        """OCR + 규칙 기반 결과를 믿기 어려운 경우: 장소 없음, 낮은 확신도, 캡쳐마다 다른 상호."""
        if result is None or not result.places or ids.get("capture_names"):
            return True
        return any(p.confidence < self.config.ocr_accept for p in result.places)

    def _extract(self, user: str, images, step, provider=None) -> tuple[ExtractionResult | None, str]:
        provider = provider or self.llm
        ident = f"{provider.name}:{getattr(provider, 'model', '')}".rstrip(":")
        try:
            data = provider.extract(self._system, user, images, self._schema)
            res = ExtractionResult.model_validate(_sanitize(data))
            step("extract", provider=ident, places=len(res.places))
            return res, ident
        except (ValidationError, ValueError, KeyError, TypeError, RuntimeError, OSError) as e:
            step("extract_error", provider=ident, error=f"{type(e).__name__}: {str(e)[:400]}")
        except Exception as e:  # SDK 별 예외
            step("extract_error", provider=ident, error=f"{type(e).__name__}: {str(e)[:400]}")
        if self.config.fallback_to_heuristic and not isinstance(provider, HeuristicExtractor) and not images:
            data = HeuristicExtractor().extract(self._system, user, [], self._schema)
            res = ExtractionResult.model_validate(_sanitize(data))
            step("extract_fallback", provider="heuristic", places=len(res.places))
            return res, "heuristic(fallback)"
        return None, ident

    def _to_record(self, p: ExtractedPlace, source: SourceInfo, ids: dict, extractor: str,
                   raw_excerpt: str | None, step) -> PlaceRecord:
        reasons: list[str] = []
        primary, sub = p.primary_category, p.sub_category
        if not taxonomy.is_valid(primary, sub):
            sub = None

        cand, geo_score = geo_mod.resolve(self.geo, p.name, p.branch, p.address_hint, p.region_hint)
        # 네이버 플레이스 페이지에서 직접 읽은 정보가 있으면 그것도 후보로 (지도 API 없이도 좌표·주소 확보)
        npi = ids.get("naver_place_info")
        if npi and npi.get("name"):
            nc = geo_mod.GeoCandidate("naver_place", ids.get("naver_place_id"), npi["name"], npi.get("category"),
                                      npi.get("road_address"), npi.get("jibun_address"), npi.get("lat"), npi.get("lng"),
                                      npi.get("phone"), f"https://map.naver.com/p/entry/place/{ids.get('naver_place_id')}")
            ns = geo_mod.score_candidate(p.name, p.branch, p.address_hint, p.region_hint, nc)
            if ns >= geo_score:
                cand, geo_score = nc, ns
        geo_used = self.geo is not None or bool(npi)
        matched = cand is not None and geo_score >= self.config.geo_accept
        step("geo", name=p.name, provider=cand.provider if cand else getattr(self.geo, "name", None),
             candidate=cand.name if cand else None, score=geo_score, matched=matched)

        ext = ExternalIds(naver_place_id=ids.get("naver_place_id"))
        if matched:
            addr = normalize.parse_address(cand.road_address, cand.jibun_address)
            loc = GeoPoint(lat=cand.lat, lng=cand.lng) if cand.lat and cand.lng else None
            phone = p.phone or cand.phone
            if cand.provider == "kakao":
                ext.kakao_place_id = cand.provider_id
            map_url, provider_cat = cand.url, cand.category
            pc = geo_mod.category_from_candidate(cand)
            if pc and pc[0] != primary:
                if primary != "other":
                    reasons.append(f"카테고리 불일치: 추출={primary}, 지도={pc[0]} → 지도 기준 적용")
                primary, sub = pc[0], (pc[1] if pc[1] else None)
            elif pc and not sub:
                sub = pc[1]
        else:
            addr = normalize.parse_address(p.address_hint) if p.address_hint else Address()
            loc, phone, map_url, provider_cat = None, p.phone, None, None
            if geo_used:
                reasons.append("지도에서 같은 장소를 확정하지 못함" + (f" (최고 후보 '{cand.name}', {geo_score})" if cand else ""))
            if not p.address_hint:
                reasons.append("주소 정보 없음")

        if normalize.is_personal_phone(phone):
            phone = None
        if p.confidence < 0.5:
            reasons.append("상호명 확신도 낮음")
        if ids.get("capture_names"):
            reasons.append("캡쳐마다 다른 상호가 보입니다(" + ", ".join(ids["capture_names"]) +
                           ") — 같은 장소의 캡쳐만 함께 올려 주세요")

        if geo_used:
            overall = 0.5 * p.confidence + 0.5 * geo_score if matched else min(p.confidence, 0.6)
        else:
            overall = p.confidence * (0.85 if p.address_hint else 0.7)
        overall = round(overall, 3)
        if not geo_used:
            reasons.append("지도 보강 미실행(geo=none) — 위치 미확정")
        if overall < self.config.complete_threshold:
            reasons.append(f"전체 신뢰도 {overall} < {self.config.complete_threshold}")
        status = Status.complete if overall >= self.config.complete_threshold and not reasons else Status.needs_review

        info = taxonomy.PRIMARY[primary]
        details, prov = enrich.build_details(p, primary, sub, source=f"{source.type.value}:{extractor}",
                                             conf=p.confidence)
        if matched:
            prov["location"] = FieldSource(source=f"geo:{cand.provider}", confidence=geo_score)
            prov["address"] = FieldSource(source=f"geo:{cand.provider}", confidence=geo_score)
        elif p.address_hint:
            prov["address"] = FieldSource(source=f"{source.type.value}:{extractor}", confidence=p.confidence)
        missing = []
        if details.opening_hours is None or details.opening_hours.partial:
            missing.append("opening_hours")
        if not (matched and cand.lat) :
            missing.append("location")
        if details.est_cost_per_person is None:
            missing.append("est_cost_per_person")
        rec = PlaceRecord(
            record_id=str(uuid.uuid4()),
            dedupe_key=normalize.dedupe_key(p.name, p.branch, addr.sigungu, ext.naver_place_id, ext.kakao_place_id),
            status=status,
            review_reasons=reasons,
            sources=[source],
            place=PlaceInfo(name=p.name, branch=p.branch, address=addr, location=loc, phone=phone,
                            external_ids=ext, map_url=map_url, provider_category=provider_cat),
            classification=Classification(primary_category=primary, primary_label=info["label"],
                                          sub_category=sub, sub_label=info["sub"].get(sub) if sub else None,
                                          tags=p.tags, free_tags=p.free_tags[:5]),
            details=details,
            provenance=prov,
            missing_fields=missing,
            content=Content(summary=p.summary, highlights=p.highlights, cautions=p.cautions,
                            evidence=[e[:60] for e in p.evidence][:6], raw_excerpt=raw_excerpt),
            confidence=Confidence(overall=overall, extraction=p.confidence,
                                  geo_match=geo_score if geo_used else None),
            embedding_text="-",
            extractor=extractor,
        )
        rec.embedding_text = normalize.embedding_text(rec)
        return rec


def _sanitize(data: dict) -> dict:
    """LLM 이 자주 내는 사소한 형식 오류를 고칩니다 (스키마 밖 카테고리, 문자열 가격 등)."""
    if not isinstance(data, dict):
        raise ValueError("추출 결과가 JSON 객체가 아닙니다.")
    places = data.get("places") or []
    if not isinstance(places, list):
        places = []
    data["places"] = places = [p for p in places if isinstance(p, dict) and isinstance(p.get("name"), str)
                               and p["name"].strip()]
    for p in places:
        for key in ("tags", "free_tags", "highlights", "cautions", "evidence"):
            v = p.get(key)
            if v is not None and not isinstance(v, list):
                p[key] = [v] if isinstance(v, str) else []
            elif isinstance(v, list):
                p[key] = [x for x in v if isinstance(x, str)]
        if not isinstance(p.get("signature_menus"), list):
            p["signature_menus"] = []
        p["signature_menus"] = [m for m in p["signature_menus"] if isinstance(m, dict) and m.get("name")]
        if p.get("primary_category") not in taxonomy.PRIMARY:
            p["primary_category"] = "other"
        lvl = p.get("price_level")
        if lvl is not None and (not isinstance(lvl, int) or not 1 <= lvl <= 4):
            p["price_level"] = None
        for m in p.get("signature_menus", []) or []:
            if isinstance(m.get("price"), str):
                digits = "".join(ch for ch in m["price"] if ch.isdigit())
                m["price"] = int(digits) if digits else None
        c = p.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            p["confidence"] = 0.5
    return data
