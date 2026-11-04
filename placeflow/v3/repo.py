"""실험용 저장소 (JSON 파일 1개). 실제 백엔드는 같은 메서드 시그니처로 DB 구현체를 만들면 됩니다.

이 모듈이 지키는 V3 규칙
  - 하트 = SavedPlace 존재 여부 (저장/저장 해제)
  - 메모는 작성자 본인만 조회 (get_memo 는 owner_id 로만 찾음)
  - 코스 저장 시 version 비교 → 다르면 conflict (공동 코스 동시 수정)
  - 코스를 저장해도 SavedPlace 는 만들지 않음
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from .domain import Course, DomainError, Place, PrivateMemo, SavedPlace, now_iso


class JsonRepo:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = {"places": {}, "saved": [], "memos": [], "courses": {}, "couples": {}}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self._data.update(raw)

    # ------------------------------------------------------------ 내부
    def _flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ 커플 (실험용 최소 기능)
    def couple_members(self, user_id: str) -> list[str]:
        for c in self._data["couples"].values():
            if user_id in c["members"]:
                return list(c["members"])
        return [user_id]

    def couple_id_of(self, user_id: str) -> Optional[str]:
        for cid, c in self._data["couples"].items():
            if user_id in c["members"]:
                return cid
        return None

    # ------------------------------------------------------------ 장소
    def upsert_place(self, place: Place) -> Place:
        with self._lock:
            old = self._data["places"].get(place.place_id)
            if old:  # 비어 있던 값만 채움 (영업시간 등)
                merged = Place.model_validate(old)
                for k, v in place.model_dump().items():
                    if getattr(merged, k) in (None, "", "기타") and v not in (None, ""):
                        setattr(merged, k, v)
                if place.tag_post_count is not None:  # 블로그 태그는 새로 구한 값으로 바꿈
                    merged.tags, merged.tag_post_count = place.tags, place.tag_post_count
                place = merged
            self._data["places"][place.place_id] = place.model_dump(mode="json")
            self._flush()
            return place

    def get_place(self, place_id: str) -> Optional[Place]:
        d = self._data["places"].get(place_id)
        return Place.model_validate(d) if d else None

    # ------------------------------------------------------------ 보관함
    def save_place(self, owner_id: str, place: Place, origin: str = "search", source_url: Optional[str] = None,
                   share_with_couple: bool = False) -> SavedPlace:
        with self._lock:
            self.upsert_place(place)
            for i, s in enumerate(self._data["saved"]):
                if s["owner_id"] == owner_id and s["place_id"] == place.place_id:
                    sp = SavedPlace.model_validate(s)
                    sp.shared_with_couple = sp.shared_with_couple or share_with_couple
                    sp.source_url = sp.source_url or source_url
                    self._data["saved"][i] = sp.model_dump(mode="json")
                    self._flush()
                    return sp
            sp = SavedPlace(owner_id=owner_id, place_id=place.place_id, origin=origin, source_url=source_url,
                            shared_with_couple=share_with_couple)
            self._data["saved"].append(sp.model_dump(mode="json"))
            self._flush()
            return sp

    def unsave_place(self, owner_id: str, place_id: str) -> bool:
        with self._lock:
            before = len(self._data["saved"])
            self._data["saved"] = [s for s in self._data["saved"]
                                   if not (s["owner_id"] == owner_id and s["place_id"] == place_id)]
            self._flush()
            return len(self._data["saved"]) < before

    def set_share(self, owner_id: str, place_id: str, shared: bool) -> SavedPlace:
        with self._lock:
            for i, s in enumerate(self._data["saved"]):
                if s["owner_id"] == owner_id and s["place_id"] == place_id:
                    s["shared_with_couple"] = shared
                    self._flush()
                    return SavedPlace.model_validate(s)
        raise DomainError("not_saved", "보관함에 없는 장소입니다.")

    def list_saved(self, user_id: str, scope: str = "personal") -> list[tuple[SavedPlace, Place]]:
        """scope=personal: 내가 저장한 것 / couple: 커플 두 사람이 공동 보관함에 공유한 것."""
        members = self.couple_members(user_id)
        out = []
        for s in self._data["saved"]:
            sp = SavedPlace.model_validate(s)
            if scope == "personal" and sp.owner_id != user_id:
                continue
            if scope == "couple" and not (sp.shared_with_couple and sp.owner_id in members and len(members) > 1):
                continue
            p = self.get_place(sp.place_id)
            if p:
                out.append((sp, p))
        return sorted(out, key=lambda x: x[0].saved_at, reverse=True)

    def is_saved(self, user_id: str, place_id: str) -> bool:
        return any(s["owner_id"] == user_id and s["place_id"] == place_id for s in self._data["saved"])

    def recommendation_pool(self, user_id: str) -> list[tuple[Place, str]]:
        """추천 후보로 쓸 '저장한 장소': (장소, 'mine'|'couple'). 내 보관함 + 공동 보관함."""
        seen, out = set(), []
        for sp, p in self.list_saved(user_id, "personal"):
            seen.add(p.place_id)
            out.append((p, "mine"))
        for sp, p in self.list_saved(user_id, "couple"):
            if p.place_id not in seen:
                seen.add(p.place_id)
                out.append((p, "couple"))
        return out

    # ------------------------------------------------------------ 메모 (본인만)
    def set_memo(self, owner_id: str, target_type: str, target_id: str, text: str) -> PrivateMemo:
        with self._lock:
            memo = PrivateMemo(owner_id=owner_id, target_type=target_type, target_id=target_id, text=text)
            self._data["memos"] = [m for m in self._data["memos"] if not (
                m["owner_id"] == owner_id and m["target_type"] == target_type and m["target_id"] == target_id)]
            if text.strip():
                self._data["memos"].append(memo.model_dump(mode="json"))
            self._flush()
            return memo

    def get_memo(self, owner_id: str, target_type: str, target_id: str) -> Optional[PrivateMemo]:
        for m in self._data["memos"]:
            if m["owner_id"] == owner_id and m["target_type"] == target_type and m["target_id"] == target_id:
                return PrivateMemo.model_validate(m)
        return None

    # ------------------------------------------------------------ 코스
    def save_course(self, course: Course, expected_version: Optional[int] = None) -> Course:
        with self._lock:
            old = self._data["courses"].get(course.course_id)
            if old is not None:
                if expected_version is not None and old["version"] != expected_version:
                    raise DomainError("version_conflict", "다른 사람이 먼저 코스를 수정했어요. 새로 불러온 뒤 다시 저장해 주세요.")
                course.created_at = old["created_at"]
            course.version = (old["version"] if old else 0) + 1
            course.updated_at = now_iso()
            self._data["courses"][course.course_id] = course.model_dump(mode="json")
            self._flush()
            return course

    def get_course(self, course_id: str) -> Optional[Course]:
        d = self._data["courses"].get(course_id)
        return Course.model_validate(d) if d else None

    def list_courses(self, user_id: str, scope: str = "personal") -> list[Course]:
        members = self.couple_members(user_id)
        out = []
        for d in self._data["courses"].values():
            c = Course.model_validate(d)
            if scope == "personal" and c.scope == "personal" and c.owner_id == user_id:
                out.append(c)
            elif scope == "couple" and c.scope == "couple" and c.owner_id in members:
                out.append(c)
        return sorted(out, key=lambda c: c.updated_at, reverse=True)

    def delete_course(self, course_id: str) -> bool:
        with self._lock:
            ok = self._data["courses"].pop(course_id, None) is not None
            self._flush()
            return ok

    def reset(self):
        with self._lock:
            self._data = {"places": {}, "saved": [], "memos": [], "courses": {}, "couples": {}}
            self._flush()
